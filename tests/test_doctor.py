"""O1a: `doctor` checks over fake probes. Offline by construction (no auth.test reaches Slack)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest

from kage.onboard import doctor as D

OWNER = types.SimpleNamespace(slack_workspace='T0000TEST01', slack_user='U0000TEST01',
                              assistant_slack_bot_user='U0000TEST02', timezone='Europe/Lisbon')


class Probe(D.Probe):
    def __init__(self, auth=None, states=None, versions=None, tz='Europe/Lisbon', now=1000.0):
        super().__init__(environ={}, clock=lambda: now)
        self.auth, self.states, self.versions, self.tz, self.calls = auth or {}, states or {}, versions or {}, tz, []

    def auth_test(self, token):
        self.calls.append(token)
        return self.auth[token]

    def launchd_state(self, label):
        return self.states.get(label)

    def version(self, binary):
        return self.versions.get(binary)

    def system_timezone(self):
        return self.tz


BOT_OK = ({'ok': True, 'team_id': 'T0000TEST01', 'user_id': 'U0000TEST02', 'bot_id': 'B0000TEST01'},
          {'x-oauth-scopes': ','.join(D.BOT_SCOPES)})
USER_OK = ({'ok': True, 'team_id': 'T0000TEST01', 'user_id': 'U0000TEST01'}, {'x-oauth-scopes': ','.join(D.USER_SCOPES)})
HEALTH = {'providers': {'providers': [{'provider': 'claude', 'status': 'healthy'}]},
          'stranded': {'counts': {'handled': 3}}, 'stuck_claims': {'blocked': []},
          'card_destination': {'status': 'ok', 'refused_24h': 0, 'destinations': [{'verdict': 'private'}]}}


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'state').mkdir(mode=0o700)
        self.db_path = root / 'state' / 'state.sqlite'
        self.db = sqlite3.connect(self.db_path)
        self.db.execute('CREATE TABLE runtime (id INTEGER PRIMARY KEY, heartbeat REAL)')
        self.db.execute('INSERT INTO runtime VALUES (1, 990.0)')
        (root / 'deploy').mkdir()
        (root / 'deploy' / 'owner.json').write_text('{"schema": "kage.owner/v1"}')
        (root / 'services.json').write_text(json.dumps({'services': {'followup': {'label': 'test.followup'},
                                                                     'receiver': {'label': 'test.receiver'}}}))
        self.deployment = root / 'deploy'
        self.tokens = {'bot': lambda: 'bot-token', 'user': lambda: 'user-token'}

    def run_doctor(self, probe, **kw):
        args = dict(probe=probe, tokens=self.tokens, db_path=self.db_path, db=self.db, health=HEALTH,
                    profile_dir=self.deployment, providers=('claude', 'codex'))
        args.update(kw)
        return D.run(OWNER, **args)

    def healthy_probe(self, **kw):
        base = dict(auth={'bot-token': BOT_OK, 'user-token': USER_OK},
                    states={'test.followup': 'running', 'test.receiver': 'running'}, versions={'claude': 'claude 9.9'})
        base.update(kw)
        return Probe(**base)

    def by_id(self, report):
        return {c['id']: c for c in report['checks']}

    def test_a_healthy_install_passes_and_reads_slack_twice(self):
        probe = self.healthy_probe()
        report = self.run_doctor(probe)
        self.assertTrue(report['ok'], report['failed'])
        self.assertEqual(report['warnings'], ['provider.codex'])
        self.assertEqual(probe.calls, ['bot-token', 'user-token'])
        checks = self.by_id(report)
        for name in ('slack.bot_identity', 'slack.user_identity', 'slack.bot_scopes', 'state.private', 'state.database',
                     'runtime.heartbeat', 'cards.destination_private', 'services.running', 'provider.any',
                     'clock.timezone', 'secrets.not_in_deployment'):
            self.assertIs(checks[name]['ok'], True, name)
        self.assertIn('all required checks pass', D.text(report))

    def test_offline_makes_no_slack_call(self):
        probe = self.healthy_probe()
        report = self.run_doctor(probe, offline=True)
        self.assertEqual(probe.calls, [])
        self.assertIsNone(self.by_id(report)['slack.bot_identity']['ok'])
        self.assertTrue(report['ok'])

    def test_the_wrong_identities_fail(self):
        wrong_bot = ({**BOT_OK[0], 'user_id': 'U0000OTHER1'}, BOT_OK[1])
        user_as_bot = ({**USER_OK[0], 'bot_id': 'B0000TEST01'}, USER_OK[1])
        report = self.run_doctor(self.healthy_probe(auth={'bot-token': wrong_bot, 'user-token': user_as_bot}))
        checks = self.by_id(report)
        self.assertEqual(checks['slack.bot_identity']['detail'], 'not_the_expected_identity')
        self.assertIn('a_bot_token_where_the_owner_token_belongs', checks['slack.user_identity']['detail'])
        self.assertEqual(report['failed'], ['slack.bot_identity', 'slack.user_identity'])

    def test_missing_scopes_warn_and_name_them(self):
        thin = (BOT_OK[0], {'x-oauth-scopes': 'chat:write'})
        report = self.run_doctor(self.healthy_probe(auth={'bot-token': thin, 'user-token': USER_OK}))
        check = self.by_id(report)['slack.bot_scopes']
        self.assertIs(check['ok'], False)
        self.assertIn('not granted: groups:history', check['detail'])
        self.assertTrue(report['ok'])  # a warning, not a failure

    def test_a_missing_user_token_is_skipped_a_missing_bot_token_fails(self):
        def missing():
            raise RuntimeError('no token')
        tokens = {'bot': missing, 'user': missing}
        report = self.run_doctor(self.healthy_probe(), tokens=tokens)
        checks = self.by_id(report)
        self.assertIs(checks['slack.bot_identity']['ok'], False)
        self.assertIsNone(checks['slack.user_identity']['ok'])

    def test_state_services_heartbeat_and_destination_failures(self):
        os.chmod(self.db_path.parent, 0o755)
        self.db.execute('UPDATE runtime SET heartbeat=1.0')
        health = {**HEALTH, 'card_destination': {'status': 'refused', 'refused_24h': 2,
                                                 'destinations': [{'verdict': 'public'}]}}
        probe = self.healthy_probe(states={'test.followup': 'running', 'test.receiver': 'loaded'})
        report = self.run_doctor(probe, health=health)
        self.assertEqual(sorted(report['failed']), ['cards.destination_private', 'runtime.heartbeat', 'services.running',
                                                    'state.private'])
        self.assertIn('not running: receiver', self.by_id(report)['services.running']['detail'])
        self.assertIn('chmod 700', self.by_id(report)['state.private']['fix'])

    def test_a_token_in_the_deployment_fails_without_printing_it(self):
        secret = 'xoxb-' + '1234567890-abcdefghij'
        (self.deployment / 'instance.json').write_text(json.dumps({'oops': secret}))
        report = self.run_doctor(self.healthy_probe())
        self.assertEqual(report['failed'], ['secrets.not_in_deployment'])
        self.assertNotIn(secret, json.dumps(report) + D.text(report))

    def test_no_agent_cli_and_another_timezone(self):
        report = self.run_doctor(self.healthy_probe(versions={}, tz='Asia/Shanghai'))
        self.assertIn('provider.any', report['failed'])
        self.assertIn('clock.timezone', report['warnings'])

    def test_without_a_deployment_directory_the_file_checks_are_skipped(self):
        report = self.run_doctor(self.healthy_probe(), profile_dir=None, db=None, health=None)
        checks = self.by_id(report)
        self.assertIsNone(checks['services.running']['ok'])
        self.assertIsNone(checks['secrets.not_in_deployment']['ok'])
        self.assertTrue(report['ok'])

    def test_the_deployment_is_found_without_the_services_environment(self):
        root = Path(self.tmp.name)
        self.assertEqual(D.deployment_root(self.deployment, 'mira', {'HOME': '/nonexistent'}), root)
        # a profile elsewhere (e.g. inside a skill): the conventional checkout <KAGE_HOME>/deploy-<key>
        elsewhere = root / 'skill' / 'deploy'
        elsewhere.mkdir(parents=True)
        home = root / 'kagehome'
        (home / 'deploy-mira').mkdir(parents=True)
        (home / 'deploy-mira' / 'services.json').write_text('{"services": {}}')
        self.assertEqual(D.deployment_root(elsewhere, 'mira', {'KAGE_HOME': str(home)}), home / 'deploy-mira')
        # the services' own variable wins
        self.assertEqual(D.deployment_root(elsewhere, 'mira', {'KAGE_DEPLOYMENT_DIR': str(self.deployment),
                                                               'KAGE_HOME': str(home)}), root)
        self.assertIsNone(D.deployment_root(elsewhere, 'nobody', {'KAGE_HOME': str(home)}))

    def test_held_stranded_rows_are_not_a_warning(self):
        health = {**HEALTH, 'stranded': {'counts': {'handled': 7, 'held': 1}}}
        check = self.by_id(self.run_doctor(self.healthy_probe(), health=health))['runtime.stranded']
        self.assertIs(check['ok'], True)
        self.assertEqual(check['detail'], '0 stranded deliveries open, 1 held (their task is paused)')
        health = {**HEALTH, 'stranded': {'counts': {'pending': 2}}}
        self.assertIs(self.by_id(self.run_doctor(self.healthy_probe(), health=health))['runtime.stranded']['ok'], False)

    def test_an_unreadable_store_is_a_finding_not_a_crash(self):
        class Broken:
            def execute(self, *a):
                raise sqlite3.OperationalError('unable to open database file')
        report = self.run_doctor(self.healthy_probe(), db=Broken())
        checks = self.by_id(report)
        self.assertIs(checks['state.database']['ok'], False)
        self.assertEqual(checks['runtime.heartbeat']['detail'], 'store unreadable (OperationalError)')

    def test_the_live_probe_refuses_slack_in_tests(self):
        with self.assertRaisesRegex(RuntimeError, 'no_transport'):
            D.Probe(environ={}).auth_test('t')  # never an unguarded call
        refusing = types.SimpleNamespace(api_call=lambda method, token: (_ for _ in ()).throw(
            RuntimeError('real_slack_disabled: refuses ' + method)))
        with self.assertRaisesRegex(RuntimeError, 'refuses auth.test'):
            D.Probe(transport=refusing).auth_test('t')  # the transport's guard comes first

    def test_a_slack_error_code_and_unknown_scopes_are_reported(self):
        class P(Probe):
            def auth_test(self, token):
                if token == 'bot-token':
                    raise RuntimeError('Slack API error: invalid_auth')
                return USER_OK[0], None
        report = self.run_doctor(P(states={'test.followup': 'running', 'test.receiver': 'running'},
                                   versions={'claude': 'c'}))
        checks = self.by_id(report)
        self.assertEqual(checks['slack.bot_identity']['detail'], 'auth.test failed (slack:invalid_auth)')
        self.assertIsNone(checks['slack.user_scopes']['ok'])


if __name__ == '__main__':
    unittest.main()
