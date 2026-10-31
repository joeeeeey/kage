import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import types
import unittest
import urllib.error

from kage.adapters.slack import destinations
from kage.core import authority

OWNER, DEPUTY, OTHER = 'U0000TEST01', 'U0000TEST04', 'U0000TEST05'
PRIVATE, PUBLIC, AWAY = 'C0000TEST01', 'C0000TEST02', 'C0000TEST03'
AUTH = authority.Authority(owner_user=OWNER, owner_key='mira', user_re=re.compile(r'U[A-Z0-9]+'),
                           channel_re=re.compile(r'[CG][A-Z0-9]+'), dm_re=re.compile(r'D[A-Z0-9]+'))
T0 = dt.datetime(2026, 7, 1, 12, tzinfo=dt.timezone.utc).timestamp()


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / 'decisions.json'

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, doc, mode=0o600):
        self.path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        os.chmod(self.path, mode)
        return AUTH.load_config(self.path)

    def ooo(self, **kw):
        return {'decision_channel': PRIVATE, 'ooo': {'active': True, 'start': '2026-07-01T00:00:00Z',
                                                     'end': '2026-07-02T00:00:00+00:00', 'delegates': [DEPUTY],
                                                     'delegate_scope': 'all_except_never_auto', 'channel': AWAY, **kw}}

    def test_missing_file_means_owner_only_in_the_dm(self):
        config = AUTH.load_config(self.tmp / 'none.json')
        self.assertEqual((config['decision_channel'], config['approvers'], config['source']), (OWNER, [OWNER], 'default'))

    def test_valid_config(self):
        config = self.write(self.ooo())
        self.assertEqual((config['decision_channel'], config['approvers'], config['ooo']['channel']),
                         (PRIVATE, [OWNER], AWAY))
        self.assertEqual(self.write({'escalation_channel': PRIVATE, 'approvers': [OWNER, OWNER]})['approvers'], [OWNER])

    def test_every_refusal(self):
        cases = [('{', 'invalid_decision_config_json'), ([], 'invalid_decision_config_json'),
                 ({'decision_channel': PRIVATE, 'escalation_channel': PUBLIC}, 'conflicting'),
                 ({'decision_channel': OWNER}, 'decision_channel'), ({'approvers': []}, 'approvers'),
                 ({'approvers': ['C1']}, 'approvers'), ({'ooo': {'active': 'yes'}}, 'invalid_decision_config_ooo'),
                 ({'ooo': {'delegate_scope': 'all'}}, 'delegate_scope'), ({'ooo': {'active': True}}, 'requires_start'),
                 ({'ooo': {'start': '2026-07-01T00:00:00'}}, 'requires_timezone'), ({'ooo': {'start': 7}}, 'ooo_start'),
                 ({'ooo': {'start': '2026-07-02T00:00:00Z', 'end': '2026-07-01T00:00:00Z'}}, 'end_must_follow'),
                 ({'decision_channel': PRIVATE, 'ooo': {'channel': PRIVATE}}, 'must_differ'),
                 (self.ooo(channel=None), 'require_ooo_channel'), ({'ooo': {'delegates': ['x']}}, 'ooo_delegates')]
        for doc, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                self.write(doc)
        with self.assertRaisesRegex(ValueError, 'group_or_world_writable'):
            self.write({}, mode=0o664)

    def test_routing_and_authority(self):
        config = self.write(self.ooo())
        inside, after = T0, T0 + 86400 * 2
        self.assertEqual(authority.card_route(config, inside)['channel'], AWAY)
        self.assertEqual(authority.card_route(config, inside, never_auto=True)['channel'], PRIVATE)
        self.assertEqual(authority.card_route(config, after)['channel'], PRIVATE)
        self.assertEqual(authority.authorize(config, OWNER, after), 'approver')
        self.assertEqual(authority.authorize(config, DEPUTY, inside), 'ooo_delegate')
        self.assertIsNone(authority.authorize(config, DEPUTY, inside, never_auto=True))
        self.assertIsNone(authority.authorize(config, DEPUTY, after))
        self.assertIsNone(authority.authorize(config, OTHER, inside))
        self.assertEqual(authority.decision_channels(config), {PRIVATE, AWAY})

    def test_private_destinations(self):
        config = self.write({'decision_channel': PRIVATE})
        self.assertTrue(AUTH.private_card_destination(config, PRIVATE))
        self.assertTrue(AUTH.private_card_destination(config, OWNER))
        self.assertFalse(AUTH.private_card_destination(config, 'D0000TEST01'))   # a DM id only while unconfigured
        self.assertTrue(AUTH.private_card_destination(AUTH.load_config(self.tmp / 'x'), 'D0000TEST01'))
        with self.assertRaisesRegex(ValueError, f'^mira_card_destination_not_private:{PUBLIC}$'):
            AUTH.assert_private_card_destination(config, PUBLIC)


class FakeRuntime:
    def __init__(self, answers):
        self.answers, self.calls, self.notes = answers, [], []
        self.slack = types.SimpleNamespace(api_call=self.api_call)

    def api_call(self, endpoint, token, params=None):
        self.calls.append(params['channel'])
        answer = self.answers[params['channel']]
        if isinstance(answer, Exception):
            raise answer
        return {'ok': True, 'channel': answer}

    def api_identity(self, bot=False):
        return 'xoxb-' + 'fake'

    def safe_error(self, exc):
        return type(exc).__name__

    def _meta_note(self, db, key, text):
        self.notes.append((key, text))


class DestinationsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT)')
        self.config = {'decision_channel': PRIVATE, 'approvers': [OWNER],
                       'ooo': {'active': False, 'start': None, 'end': None, 'delegates': [], 'delegate_scope': 'none',
                               'channel': None}}
        self.dest = destinations.Destinations(AUTH, owner_user=OWNER, owner_key='mira', load_config=lambda: self.config,
                                              stored_destinations=(), report_next='Fix the destination.',
                                              refresh_command='kage card-destination-check --refresh')

    def test_verdicts(self):
        v = self.dest.verdict
        self.assertEqual([v({'is_mpim': True}), v({'is_ext_shared': True}), v({'is_shared': True}),
                          v({'is_shared': True, 'is_org_shared': True, 'is_private': True}), v({'is_im': True, 'user': OWNER}),
                          v({'is_im': True, 'user': OTHER}), v({'is_private': True}), v({})],
                         ['mpim', 'ext_shared', 'ext_shared', 'private', 'im', 'im_not_mira', 'private', 'public'])

    def test_require_checks_slack_and_caches(self):
        rt = FakeRuntime({PRIVATE: {'is_private': True}, PUBLIC: {}})
        self.assertEqual(self.dest.require(rt, self.db, PRIVATE, 'card', self.config), 'private')
        self.assertEqual(self.dest.require(rt, self.db, PRIVATE, 'card', self.config), 'private')
        self.assertEqual(rt.calls, [PRIVATE])                                   # second answer from the cache
        self.config['decision_channel'] = PUBLIC                                # a public channel configured by mistake
        with self.assertRaisesRegex(destinations.DestinationRefused, f'^mira_card_destination_not_private:{PUBLIC}:public$'):
            self.dest.require(rt, self.db, PUBLIC, 'card')
        with self.assertRaisesRegex(destinations.DestinationRefused, 'not_a_configured_destination'):
            self.dest.require(rt, self.db, 'C0000TEST09', 'card')
        self.assertEqual(self.dest.require(rt, self.db, OWNER, 'card', self.config), 'im')   # the DM: no Slack call
        overview = self.dest.overview(self.db)
        self.assertEqual((overview['status'], overview['refused_total']), ('refusing', 2))
        self.assertEqual(overview['command'], 'kage card-destination-check --refresh')

    def test_transient_failure_keeps_a_recent_private_verdict(self):
        rt = FakeRuntime({PRIVATE: {'is_private': True}})
        self.dest.check(rt, self.db, PRIVATE, now=1000.0)
        rt.answers[PRIVATE] = urllib.error.URLError('down')
        kept = self.dest.check(rt, self.db, PRIVATE, now=1000.0 + destinations.DESTINATION_TTL + 1)
        self.assertEqual((kept['verdict'], kept['transient']), ('private', 'URLError'))
        rt.answers[PRIVATE] = RuntimeError('Slack API error: missing_scope')
        refused = self.dest.check(rt, self.db, PRIVATE, now=1000.0 + destinations.DESTINATION_TTL * 3, refresh=True)
        self.assertEqual((refused['verdict'], refused['error']), ('error', 'RuntimeError'))
        self.assertTrue(destinations.transient_failure(RuntimeError('Slack API error: ratelimited')))
        self.assertFalse(destinations.transient_failure(RuntimeError('Slack API error: channel_not_found')))


if __name__ == '__main__':
    unittest.main()
