"""O1c5e: a generated deployment's services — init writes services.json (followup / dashboard / receiver, labels of
this owner, absolute paths), Kage's launcher plans each one from it, and `services plan|start|stop|status` loads the
launchd jobs only after the doctor gate, through a fake launchctl (nothing here reaches launchd)."""
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import unittest
from unittest import mock

from kage import app
from kage.onboard import services

from kage.ports import secrets

from tests.test_init import APP_TOKEN, BOT_TOKEN, Base

FAKE_LAUNCHCTL = '''#!/usr/bin/env python3
import json, os, plistlib, sys
from pathlib import Path
state_file = Path(os.environ['FAKE_LAUNCHCTL_STATE'])
state = json.loads(state_file.read_text()) if state_file.exists() else {}
args = sys.argv[1:]
with open(os.environ['FAKE_LAUNCHCTL_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[0] == 'bootstrap':
    data = plistlib.loads(Path(args[2]).read_bytes())
    state[data['Label']] = data['ProgramArguments']
elif args[0] == 'bootout':
    if state.pop(args[1].rsplit('/', 1)[1], None) is None:
        sys.exit(3)
elif args[0] == 'print':
    if args[1].rsplit('/', 1)[1] not in state:
        sys.exit(113)
    print('\\tstate = running')
state_file.write_text(json.dumps(state))
'''


def report(*failing):
    checks = [{'id': i, 'required': True, 'ok': i not in failing} for i in
              ('slack.bot_identity', 'state.store', 'services.running', 'secrets.port', 'owner.identity')]
    return {'ok': not failing, 'checks': checks}


class ServicesTest(Base):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        (self.bin / 'launchctl').write_text(FAKE_LAUNCHCTL)
        os.chmod(self.bin / 'launchctl', 0o755)
        (self.bin / 'claude').write_text('#!/bin/sh\necho "0.0.0 (test agent)"\n')
        os.chmod(self.bin / 'claude', 0o755)
        patch = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'KAGE_VERSIONS_DIR': str(self.release.parent),
            'FAKE_LAUNCHCTL_STATE': str(self.tmp / 'launchd.json'), 'FAKE_LAUNCHCTL_LOG': str(self.tmp / 'launchd.log'),
            'PATH': f'{self.bin}{os.pathsep}/tmp/kage-transient-dir{os.pathsep}{os.environ.get("PATH", "")}'})
        patch.start()
        self.addCleanup(patch.stop)
        self.records()
        self.init()
        self.assembly = app.App(self.target / 'deploy')

    def calls(self):
        log = self.tmp / 'launchd.log'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def svc(self, **kw):
        return services.Services(self.target, 'mira', which=lambda name: str(self.bin / name), **kw)

    def test_init_writes_this_owners_services(self):
        spec = json.loads((self.target / 'services.json').read_text())
        self.assertEqual((spec['schema'], spec['written_by'], spec['skills']),
                         (services.SCHEMA, services.WRITTEN_BY, str(self.target.absolute())))
        self.assertEqual({n: s['label'] for n, s in spec['services'].items()},
                         {'followup': 'pro.kage.mira.followup', 'dashboard': 'pro.kage.mira.dashboard',
                          'receiver': 'pro.kage.mira.receiver'})
        self.assertEqual([spec['services'][n]['args'] for n in ('followup', 'dashboard', 'receiver')],
                         [['run'], ['serve', 'dashboard', '--port', '8777'], ['serve', 'receiver']])
        path = spec['services']['followup']['env']['PATH'].split(os.pathsep)
        kept = [p for p in os.environ['PATH'].split(os.pathsep)
                if p.startswith('/') and not p.startswith(services.TRANSIENT_PATH) and os.path.isdir(p)]
        self.assertEqual(path, list(dict.fromkeys(kept)))  # in order, absolute, existing, nothing temporary
        self.assertNotIn(str(self.bin), path)
        state = self.home / '.local/share/kage/state-mira'
        self.assertEqual(spec['services']['receiver']['stdout'], f'{state.absolute()}/logs/receiver.log')
        self.assertEqual(stat.S_IMODE((self.target / 'services.json').stat().st_mode), 0o600)

    def test_kages_launcher_plans_every_service(self):
        for name, args in services.ARGS:
            out = subprocess.run([sys.executable, str(self.release / 'kage/service.py'), '--deployment',
                                  str(self.target / 'deploy'), name], capture_output=True, text=True, timeout=60,
                                 env={**os.environ, 'KAGE_SERVICE_DRY_RUN': '1'})
            self.assertEqual(out.returncode, 0, out.stderr)
            plan = json.loads(out.stdout)
            self.assertEqual(plan['argv'][1:], [str(self.target.absolute() / 'scripts/kage.py'), *args])
            self.assertEqual(plan['env_added']['KAGE_DEPLOYMENT_DIR'], str(self.target.absolute() / 'deploy'))

    def test_plan_writes_nothing(self):
        plan = self.svc().plan()
        self.assertEqual([j['label'] for j in plan['jobs']],
                         ['pro.kage.mira.followup', 'pro.kage.mira.dashboard', 'pro.kage.mira.receiver'])
        self.assertEqual(plan['jobs'][0]['argv'][1], str(self.release / 'kage/service.py'))
        self.assertFalse((self.home / 'Library/LaunchAgents').exists())
        self.assertEqual(self.calls(), [])

    def test_start_needs_the_doctor_then_loads_and_stop_unloads(self):
        s = self.svc()
        with self.assertRaisesRegex(services.ServicesError, 'doctor_required_checks_fail: slack.bot_identity'):
            s.start(lambda: report('slack.bot_identity', 'services.running'))
        self.assertFalse((self.home / 'Library/LaunchAgents').exists())
        self.assertEqual(self.calls(), [])
        status = s.start(lambda: report('services.running'))
        self.assertEqual(status, {'jobs': {'followup': 'running', 'dashboard': 'running', 'receiver': 'running'}})
        for name, _ in services.ARGS:
            path = self.home / f'Library/LaunchAgents/pro.kage.mira.{name}.plist'
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            data = plistlib.loads(path.read_bytes())
            self.assertEqual(data['ProgramArguments'][2:], ['--deployment', str(self.target / 'deploy'), name])
            self.assertEqual(data['KeepAlive'], True)
        self.assertEqual([c[0] for c in self.calls() if c[0] != 'print'], ['bootout', 'bootstrap'] * 3)
        self.assertEqual(s.stop(), {'jobs': {'followup': 'not_loaded', 'dashboard': 'not_loaded',
                                             'receiver': 'not_loaded'}})
        self.assertEqual(list((self.home / 'Library/LaunchAgents').iterdir()), [])

    def test_refusals(self):
        (self.release / 'kage/serve/receiver.py').write_text('raise SystemExit(0)\n')
        with self.assertRaisesRegex(services.ServicesError, 'release_does_not_verify'):
            self.svc().start(lambda: report())
        spec = json.loads((self.target / 'services.json').read_text())
        for change, code in ((lambda d: d.pop('written_by'), 'not_written_by_kage_init'),
                             (lambda d: d['services']['receiver'].update(label='com.example.other'), 'label_not_this'),
                             (lambda d: d['services'].pop('dashboard'), 'unexpected_services')):
            data = json.loads(json.dumps(spec))
            change(data)
            (self.target / 'services.json').write_text(json.dumps(data))
            with self.assertRaisesRegex(services.ServicesError, code):
                self.svc()
        self.assertEqual(self.calls(), [])

    def program(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('SLACK_') and k != 'PYTHONPATH'}
        site = self.tmp / 'site/websockets'  # 0.77.1: the receiver's package for doctor (CI has none)
        site.mkdir(parents=True, exist_ok=True)
        (site / '__init__.py').write_text("__version__ = '13.1'\n")
        env['PYTHONPATH'] = str(site.parent)
        return subprocess.run([sys.executable, str(self.target / 'scripts/kage.py'), *args], capture_output=True,
                              text=True, env=env, timeout=180, stdin=subprocess.DEVNULL, cwd=str(self.tmp))

    def test_through_the_program(self):
        plan = self.program('services', 'plan')
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(len(json.loads(plan.stdout)['jobs']), 3)
        refused = self.program('services', 'start')  # the online doctor: no real Slack here, so the bot check fails
        self.assertEqual(refused.returncode, 78, refused.stderr)
        self.assertIn('doctor_required_checks_fail', refused.stderr)
        self.assertEqual([c for c in self.calls() if c[0] != 'print'], [])
        self.assertEqual(self.program('services', 'start', '--now').returncode, 2)
        store = secrets.for_owner('mira', home=self.home)
        store.put(secrets.SLACK_BOT, BOT_TOKEN)
        store.put(secrets.SLACK_APP, APP_TOKEN)
        doctor = self.program('doctor', '--offline', '--json')
        self.assertEqual(json.loads(doctor.stdout)['failed'], ['services.running'], doctor.stdout[-2000:])


if __name__ == '__main__':
    unittest.main()
