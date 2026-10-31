"""The dashboard's data sources: the participant / avatar cache (kage.adapters.slack.avatars), the message previews
(kage.adapters.slack.previews) and the inbox trace (kage.core.trace), bound to a synthetic owner."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import types
import unittest
import unittest.mock
import zoneinfo

from kage.adapters.slack import avatars as A
from kage.adapters.slack import previews as P
from kage.core import trace as T

OWNER, TEAM = 'U0000TEST01', 'T0000TEST01'


def transport(identity):
    return lambda: types.SimpleNamespace(default_user_token=lambda: 'xoxp-test', api_call=lambda method, token: identity)


class AvatarsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / 'state.sqlite'
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        A.initialize(self.db)
        self.db.executescript('CREATE TABLE watches (id TEXT PRIMARY KEY, data TEXT);')
        self.core = A.Avatars('en', owner_key='owner', owner_name='Mira', owner_slack_user=OWNER, slack_workspace=TEAM,
                              guard_env='TEST_NO_NETWORK', slack_transport=transport({'team_id': TEAM, 'user_id': OWNER}))

    def test_owner_first_and_named_before_a_profile(self):
        self.core.observe(self.db, 'C0000TEST01', [{'ts': '1.000001', 'user': 'U0000TEST02'},
                                                   {'ts': '2.000001', 'user': OWNER, 'bot_id': 'B0000TEST01'},
                                                   {'ts': '3.000001', 'user': 'U0000TEST03', 'bot_id': 'B0000TEST02'}])
        people = self.core.participants(self.db, 'C0000TEST01')
        self.assertEqual([p['id'] for p in people['participants']], [OWNER, 'U0000TEST02'])
        self.assertEqual(people['participants'][0]['name'], 'Mira')
        self.assertTrue(people['participants'][0]['is_owner'])

    def test_sync_checks_the_owner_identity_and_stores_profiles(self):
        self.db.execute('INSERT INTO watches VALUES (?, ?)', ('w', json.dumps(
            {'channel': 'C0000TEST01', 'thread': '1.000001', 'state': 'running', 'peers': ['U0000TEST02']})))
        self.db.commit()

        def api(method, token, params=None):
            if method == 'conversations.info':
                return {'channel': {'name': 'ops'}}
            if method == 'conversations.replies':
                return {'messages': [{'ts': '1.000002', 'thread_ts': '1.000001', 'user': 'U0000TEST02'}]}
            return {'user': {'profile': {'display_name': 'Ava', 'image_72': 'https://avatars.slack-edge.com/a.png'}}}
        worker = A.CacheWorker(self.path, api, 'xoxp-test', downloader=lambda url: (b'png', 'image/png'), core=self.core)
        worker.sync_once(100.0)
        names = dict(self.db.execute('SELECT user_id, name FROM dashboard_profiles'))
        self.assertEqual(names, {OWNER: 'Ava', 'U0000TEST02': 'Ava'})
        wrong = A.Avatars('en', owner_key='owner', owner_name='Mira', owner_slack_user=OWNER, slack_workspace=TEAM,
                          guard_env='TEST_NO_NETWORK', slack_transport=transport({'team_id': 'T0000TEST09', 'user_id': OWNER}))
        with self.assertRaisesRegex(ValueError, 'unexpected_slack_identity'):
            A.CacheWorker(self.path, core=wrong).sync_once(200.0)

    def test_the_guard_variable_refuses_downloads(self):
        with unittest.mock.patch.dict(os.environ, {'TEST_NO_NETWORK': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'TEST_NO_NETWORK=1'):
                self.core.download_avatar('https://avatars.slack-edge.com/a.png')
        with self.assertRaisesRegex(ValueError, 'avatar_host_rejected'):
            self.core.download_avatar('https://example.com/a.png')


class PreviewsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / 'state.sqlite'
        db = sqlite3.connect(self.path)
        db.executescript('CREATE TABLE watches (id TEXT PRIMARY KEY, data TEXT);')
        db.execute('INSERT INTO watches VALUES (?, ?)', ('w', json.dumps(
            {'id': 'w', 'channel': 'C0000TEST01', 'thread': '1.000001', 'secret_input': True})))
        db.commit()
        db.close()

    def core(self, language='en'):
        return P.Previews(language, owner_key='owner', owner_name='Mira', owner_slack_user=OWNER, slack_workspace=TEAM,
                          slack_transport=transport({'team_id': TEAM, 'user_id': OWNER}),
                          mask_codes=lambda text: text.replace('123456', '******'))

    def test_a_thread_preview_in_the_owner_language_with_codes_masked(self):
        def api(method, token, params=None):
            return {'messages': [{'ts': '1.000001', 'user': OWNER, 'text': 'code 123456', 'latest_reply': '1.000001'}]}
        with unittest.mock.patch('threading.Timer'):
            result = P.PreviewService(self.path, api, 'xoxp-test', clock=lambda: 50.0, core=self.core()).preview('w')
        self.assertEqual(result['note'], 'Latest messages, at most 20; bodies are only cached briefly in memory')
        self.assertEqual(result['messages'][0]['text'], 'code ******')
        self.assertEqual(result['messages'][0]['author']['name'], 'Mira')
        with unittest.mock.patch('threading.Timer'):
            zh = P.PreviewService(self.path, api, 'xoxp-test', clock=lambda: 50.0, core=self.core('zh-CN')).preview('w')
        self.assertIn('20', zh['note'])

    def test_unknown_watch_and_wrong_identity(self):
        service = P.PreviewService(self.path, lambda *a, **kw: {}, 'xoxp-test', core=self.core())
        with self.assertRaises(P.PreviewError) as caught:
            service.preview('missing')
        self.assertEqual((caught.exception.code, caught.exception.status), ('watch_not_found', 404))
        wrong = P.Previews('en', owner_key='owner', owner_name='Mira', owner_slack_user=OWNER, slack_workspace=TEAM,
                           slack_transport=transport({'team_id': TEAM, 'user_id': 'U0000TEST09'}), mask_codes=str)
        with unittest.mock.patch('threading.Timer'), self.assertRaises(P.PreviewError) as caught:
            P.PreviewService(self.path, core=wrong).preview('w')
        self.assertEqual(caught.exception.code, 'unexpected_slack_identity')


class TraceTest(unittest.TestCase):
    def core(self, language='en', key='owner'):
        run_log = types.SimpleNamespace(short=lambda text, limit: text[:limit], mask=lambda text: text)
        return T.TraceCore(language, tz=zoneinfo.ZoneInfo('Asia/Shanghai'), owner_key=key, owner_name='Mira',
                           owner_slack_user=OWNER, ticket_ref_reason='ticket_ref', ticket_team='TEST', run_log=run_log)

    def test_owner_values_follow_the_key(self):
        c = self.core()
        self.assertEqual(c.inbox_tasks, ('owner-inbox', 'owner-oncall'))
        self.assertEqual((c.owner_wait, c.views[1]), ('owner', 'owner'))
        self.assertIn('needs_owner', c.outcome_order)
        self.assertEqual(c.route_reason_text('ticket_ref'), 'the TEST ticket in the message matches exactly one task')
        self.assertEqual(c.route_reason_text('handoff_from:task-1'), 'handed off by task task-1')
        self.assertEqual(c.disposition['card_owner'], 'card to Mira')

    def test_durations_and_clock_in_the_owner_language(self):
        self.assertEqual([self.core().dur(s) for s in (5, 125, 7300, 200000)], ['5 s', '2 min 5 s', '2 h 1 min', '2 d 7 h'])
        self.assertEqual(self.core('zh-CN').dur(125), '2 分 5 秒')
        self.assertEqual(self.core().clock(0), '01-01 08:00:00')

    def test_an_empty_store_and_unknown_references(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / 'state.sqlite'
        sqlite3.connect(path).close()
        c = self.core()
        db = T.open_db(path)
        self.addCleanup(db.close)
        self.assertEqual(c.inbox(T.Store(db, 100.0, None, False, core=c)), [])
        with self.assertRaises(KeyError):
            c.trace_ref(path, 'in0000000000abcd', now=100.0)
        with self.assertRaises(ValueError):
            c.trace_ref(path, '!!', now=100.0)
        self.assertEqual(T.parse_ref('C0000TEST01/1700000000.000001')['kind'], 'msg')

    def test_a_trace_names_its_stages_in_the_owner_language(self):
        c = self.core()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        db = sqlite3.connect(tmp / 'x.sqlite')
        self.addCleanup(db.close)
        trace = T.Trace(T.Store(db, 100.0, None, False, core=c), {'kind': 'burst', 'ref': 'x'}, core=c)
        trace.add('received', c.done, 90.0, 'got it')
        trace.add('route', c.owner_wait, 95.0, 'waiting')
        out = trace.finish()
        self.assertEqual([s['label'] for s in out['stages']][:4], ['received', 'filter / merge', 'triage', 'route'])
        self.assertEqual((out['status'], out['outcome']), ('owner', 'needs_owner'))


if __name__ == '__main__':
    unittest.main()
