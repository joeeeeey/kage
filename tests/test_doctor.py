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
import unittest.mock

from kage.onboard import doctor as D, identity
from kage.ports import secrets

OWNER = types.SimpleNamespace(slack_workspace='T0000TEST01', slack_user='U0000TEST01',
                              assistant_slack_bot_user='U0000TEST02', timezone='Europe/Lisbon')


class Probe(D.Probe):
    def __init__(self, auth=None, states=None, versions=None, tz='Europe/Lisbon', now=1000.0, launchd=True, modules=None,
                 liveness=True):
        super().__init__(environ={}, clock=lambda: now,
                         which=lambda binary: {'launchctl': '/bin/launchctl' if launchd else None,
                                               'lsof': '/usr/sbin/lsof' if liveness else None}.get(binary))
        self.auth, self.states, self.versions, self.tz, self.calls = auth or {}, states or {}, versions or {}, tz, []
        self.modules = {'websockets': '13.1'} if modules is None else modules

    def module_version(self, python, module):
        return self.modules.get(module)

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

    def test_the_assistant_voice_has_no_user_token_to_check(self):
        """F6: voice assistant skips the user token (none by design); as_owner requires it; an adopted deployment
        (voice unknown) checks a configured one and skips an empty one."""
        empty = {'bot': lambda: 'bot-token', 'user': lambda: ''}
        probe = self.healthy_probe()
        report = self.run_doctor(probe, tokens=empty, voice='assistant')
        user = self.by_id(report)['slack.user_identity']
        self.assertEqual((user['ok'], user['required']), (None, False))
        self.assertIn('voice assistant', user['detail'])
        self.assertNotIn('slack.user_scopes', self.by_id(report))
        self.assertEqual(probe.calls, ['bot-token'])
        self.assertNotIn('slack.user_identity', report['failed'])
        probe = self.healthy_probe()
        report = self.run_doctor(probe, voice='assistant')  # a stray user token is not read either
        self.assertEqual(probe.calls, ['bot-token'])
        report = self.run_doctor(self.healthy_probe(), tokens=empty, voice='as_owner')
        self.assertIn('slack.user_identity', report['failed'])
        self.assertEqual(self.by_id(report)['slack.user_identity']['detail'], 'no user token (LookupError)')
        self.assertTrue(self.run_doctor(self.healthy_probe(), voice='as_owner')['ok'])
        probe = self.healthy_probe()
        report = self.run_doctor(probe, tokens=empty)
        self.assertIsNone(self.by_id(report)['slack.user_identity']['ok'])
        self.assertEqual(probe.calls, ['bot-token'])
        self.assertTrue(report['ok'], report['failed'])

    def test_the_voice_of_a_deployment(self):
        home = Path(self.tmp.name) / 'home'
        store = secrets.FileStore('mira', home / '.config/kage/mira/secrets')
        self.assertIsNone(D.voice('mira', None, home))  # its own transport reads its tokens
        self.assertEqual(D.voice('mira', store, home), 'assistant')
        store.put(secrets.SLACK_USER, 'xoxp-' + '0000-TEST')
        self.assertEqual(D.voice('mira', store, home), 'as_owner')
        answers = home / '.config/kage/mira/answers.json'
        answers.write_text(json.dumps({'slack': {'voice': 'assistant'}}))
        self.assertEqual(D.voice('mira', store, home), 'assistant')  # the answers decide

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

    def test_without_launchd_the_supervisor_heartbeat_judges_the_services(self):
        # 0.77.1 (F10b): Linux has no launchd; the deployment's own supervisor runs the services
        report = self.run_doctor(self.healthy_probe(launchd=False, states={}))
        self.assertTrue(report['ok'], report['failed'])
        self.assertIn('no launchd', self.by_id(report)['services.running']['detail'])
        self.db.execute('UPDATE runtime SET heartbeat=1.0')
        report = self.run_doctor(self.healthy_probe(launchd=False, states={}))
        self.assertEqual(sorted(report['failed']), ['runtime.heartbeat', 'services.running'])
        self.assertIn('supervisor', self.by_id(report)['services.running']['fix'])

    def test_the_receiver_needs_websockets(self):
        # 0.77.1 (F10c): the Socket Mode receiver imports websockets (>= 11) in the services' interpreter
        self.assertIn('13.1', self.by_id(self.run_doctor(self.healthy_probe()))['receiver.websockets']['detail'])
        for modules in ({}, {'websockets': '10.4'}):
            report = self.run_doctor(self.healthy_probe(modules=modules))
            self.assertEqual(report['failed'], ['receiver.websockets'], modules)
            self.assertIn('pip install --user', self.by_id(report)['receiver.websockets']['fix'])
        (Path(self.tmp.name) / 'services.json').write_text(json.dumps({'services': {'followup': {'label': 'test.followup'}}}))
        self.assertNotIn('receiver.websockets', self.by_id(self.run_doctor(self.healthy_probe(modules={}))))

    def test_the_live_probe_reads_a_module_version_through_the_interpreter(self):
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0 if 'websockets' in argv[-1] else 1, stdout='13.1\n')
        probe = D.Probe(environ={'HOME': '/nonexistent'}, runner=runner)
        self.assertEqual(probe.module_version('/usr/bin/python3', 'websockets'), '13.1')
        self.assertEqual(calls[0][:2], ['/usr/bin/python3', '-c'])
        self.assertIsNone(probe.module_version('/usr/bin/python3', 'nothere'))

    def test_codex_needs_a_liveness_tool(self):
        # 0.78.5 (F17): no lsof and no /proc -> a Codex task's events are never dispatched
        report = self.run_doctor(self.healthy_probe(versions={'claude': 'claude 9.9', 'codex': 'codex 1.0'}))
        self.assertIn('lsof', self.by_id(report)['liveness.codex']['detail'])
        probe = self.healthy_probe(versions={'claude': 'claude 9.9', 'codex': 'codex 1.0'}, liveness=False)
        with unittest.mock.patch.object(D.os.path, 'isdir', return_value=False):
            report = self.run_doctor(probe)
        self.assertEqual(report['failed'], ['liveness.codex'])
        with unittest.mock.patch.object(D.os.path, 'isdir', return_value=True):  # Linux: /proc instead
            self.assertIn('/proc', self.by_id(self.run_doctor(probe))['liveness.codex']['detail'])
        self.assertNotIn('liveness.codex', self.by_id(self.run_doctor(self.healthy_probe())))  # no codex: no check

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

    def test_a_store_the_runtime_never_used_is_not_a_stopped_runtime(self):
        db = sqlite3.connect(':memory:')  # O1c4: init's onboarding row only, no runtime table yet
        db.execute('CREATE TABLE onboarding_steps (step TEXT PRIMARY KEY, status TEXT, at REAL, detail TEXT)')
        checks = self.by_id(self.run_doctor(self.healthy_probe(), db=db))
        self.assertIsNone(checks['runtime.heartbeat']['ok'])
        self.assertEqual(checks['runtime.heartbeat']['detail'], 'the runtime has not started yet')

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


class SecretsAndIdentityChecksTest(unittest.TestCase):
    def test_secrets_port(self):
        self.assertEqual(D.port_checks(None)[0]['ok'], None)  # the deployment's own transport
        store = secrets.MemoryStore('mira')
        store.put(secrets.SLACK_BOT, 'xoxb-' + 'doctor')
        c = D.port_checks(store)[0]
        self.assertEqual((c['required'], c['ok']), (True, False))
        self.assertIn('slack-app-token absent', c['detail'])
        store.put(secrets.SLACK_APP, 'xapp-' + 'doctor')
        c = D.port_checks(store)[0]
        self.assertTrue(c['ok'])
        self.assertNotIn('doctor', c['detail'])

        class Broken(secrets.MemoryStore):
            def get(self, name):
                raise secrets.SecretError('keychain_read_failed_rc_51')
        c = D.port_checks(Broken('mira'))[0]
        self.assertEqual((c['ok'], c['detail']), (False, 'memory store: keychain_read_failed_rc_51'))

    def test_owner_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            os.chmod(state, 0o700)
            owner = types.SimpleNamespace(slack_workspace='T0000TEST', slack_user='U0000OWNER')
            self.assertEqual(D.identity_checks(owner, state)[0]['ok'], None)  # adopted
            identity.prove_by_user_token(state, {'team_id': 'T0000TEST', 'user_id': 'U0000OWNER'})
            self.assertTrue(D.identity_checks(owner, state)[0]['ok'])
            (state / identity.RECORD).write_text('{"schema": "other"}')
            c = D.identity_checks(owner, state)[0]
            self.assertEqual((c['ok'], c['detail']), (False, 'identity record unreadable (identity_record_malformed)'))


if __name__ == '__main__':
    unittest.main()
