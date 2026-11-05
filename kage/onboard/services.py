"""A generated deployment's services (O1c5e): `services.json`, written by `kage init`, and its launchd jobs.

services.json (`kage.services/v1`, the schema kage/service.py reads) names three services, each the deployment's own
program on its own root: `followup` (`scripts/kage.py run`), `dashboard` (`serve dashboard --port P`, loopback) and
`receiver` (`serve receiver`). Labels are `pro.kage.<key>.<name>`; logs go to the state directory. `written_by`
marks a file init owns: the commands below refuse any other (a deployment with its own service tool keeps it).

  services plan     the plists it would write and the commands it would run (JSON); writes nothing
  services start [--offline]
                    refuses unless every required doctor check but `services.running` passes (`doctor --json`
                    through the deployment's program; `--offline` = `doctor --offline`: no auth.test, the receiver's
                    own preflight still refuses a token of another workspace or bot) and the pinned release
                    verifies; then writes each plist
                    (0600, ~/Library/LaunchAgents/<label>.plist) and loads it (`launchctl bootout` then `bootstrap
                    gui/<uid>`); prints each job's state
  services stop     unloads each job and removes its plist (only a plist with this deployment's label)
  services status   each job's state (`launchctl print`)

launchd runs Kage's launcher from the pinned release (`<versions>/<version>-<commit12>/kage/service.py --deployment
<root>/deploy NAME`), which verifies the release against kage.lock before it runs the program. `launchctl` is found on
PATH (as doctor finds it), so a test puts its own first; nothing here sends a message.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys

SCHEMA = 'kage.services/v1'
WRITTEN_BY = 'kage init'
PROGRAM = 'scripts/kage.py'
DASHBOARD_PORT = 8777
LAUNCHD = {'KeepAlive': True, 'ProcessType': 'Background', 'RunAtLoad': True, 'ThrottleInterval': 10}
ARGS = (('followup', ['run']), ('dashboard', ['serve', 'dashboard', '--port', str(DASHBOARD_PORT)]),
        ('receiver', ['serve', 'receiver']))
TRANSIENT_PATH = ('/tmp/', '/private/tmp/', '/var/folders/', '/private/var/folders/')
NOT_A_GATE = ('services.running',)  # what `start` itself fixes


class ServicesError(RuntimeError):
    """A services command refused (said why); nothing was loaded."""


def label(key, name):
    return f'pro.kage.{key}.{name}'


def service_path(path):
    """The followup service's PATH (its agents' CLIs): the absolute, existing directories of `path`, in order, without
    temporary ones."""
    out = []
    for entry in str(path or '').split(os.pathsep):
        if entry.startswith('/') and not entry.startswith(TRANSIENT_PATH) and os.path.isdir(entry) and entry not in out:
            out.append(entry)
    return os.pathsep.join(out or ['/usr/bin', '/bin', '/usr/sbin', '/sbin'])


def spec(*, key, target, state, python, path):
    """services.json of a generated deployment (absolute paths: launchd has no working directory of its own)."""
    target, state = str(Path(target).absolute()), str(Path(state).absolute())
    services = {}
    for name, args in ARGS:
        log = f'{state}/logs/{name}.log'
        services[name] = {'label': label(key, name), 'python': python, 'script': PROGRAM, 'args': list(args),
                          'workdir': target, 'env': {'PATH': path} if name == 'followup' else {},
                          'stdout': log, 'stderr': log, 'launchd': dict(LAUNCHD)}
    return {'schema': SCHEMA, 'written_by': WRITTEN_BY, 'skills': target, 'services': services}


def _home():
    return Path(os.environ.get('HOME') or Path.home())


def release_dir(lock):
    pin = lock['kage']
    versions = Path(os.environ.get('KAGE_VERSIONS_DIR') or _home() / '.local/share/kage/versions')
    return versions / f"{pin['version']}-{pin['commit'][:12]}"


class Services:
    def __init__(self, root, key, *, run=subprocess.run, which=shutil.which, uid=None, agents=None):
        self.root, self.key, self.run, self.which = Path(root), key, run, which
        self.uid = os.getuid() if uid is None else uid
        self.agents = Path(agents) if agents else _home() / 'Library/LaunchAgents'
        try:
            self.spec = json.loads((self.root / 'services.json').read_text(encoding='utf-8'))
            self.lock = json.loads((self.root / 'kage.lock').read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ServicesError(f'deployment_unreadable: {type(exc).__name__}') from None
        if self.spec.get('schema') != SCHEMA or self.spec.get('written_by') != WRITTEN_BY:
            raise ServicesError('services_json_not_written_by_kage_init: this deployment has its own service tool')
        names = sorted((self.spec.get('services') or {}))
        if names != sorted(n for n, _ in ARGS):
            raise ServicesError(f'unexpected_services: {", ".join(names)}')
        for name, svc in self.spec['services'].items():
            if svc.get('label') != label(key, name):
                raise ServicesError(f'label_not_this_deployments: {name}')

    def plist(self, name):
        svc, release = self.spec['services'][name], release_dir(self.lock)
        data = {'Label': svc['label'],
                'ProgramArguments': [svc['python'], str(release / 'kage' / 'service.py'), '--deployment',
                                     str(self.root / 'deploy'), name],
                'WorkingDirectory': svc['workdir'], 'StandardOutPath': svc['stdout'],
                'StandardErrorPath': svc['stderr'], **svc['launchd']}
        if svc.get('env'):
            data['EnvironmentVariables'] = dict(svc['env'])
        return data

    def plist_path(self, name):
        return self.agents / f"{self.spec['services'][name]['label']}.plist"

    def _launchctl(self, *args):
        exe = self.which('launchctl')
        if not exe:
            raise ServicesError('no_launchctl: launchd services need macOS')
        return self.run([exe, *args], capture_output=True, text=True, timeout=30)

    def state(self, name):
        out = self._launchctl('print', f"gui/{self.uid}/{self.spec['services'][name]['label']}")
        if out.returncode != 0:
            return 'not_loaded'
        return 'running' if re.search(r'^\s*state = running\s*$', out.stdout, re.M) else 'loaded'

    def plan(self):
        return {'launch_agents': str(self.agents), 'release': release_dir(self.lock).name,
                'jobs': [{'name': name, 'label': self.spec['services'][name]['label'], 'plist': str(self.plist_path(name)),
                          'argv': self.plist(name)['ProgramArguments']} for name, _ in ARGS]}

    def gate(self, doctor):
        """Refuse unless the release verifies and every required doctor check but services.running passes."""
        from kage import service
        problems = service.release_problems(release_dir(self.lock), self.lock['kage'])
        if problems:
            raise ServicesError('release_does_not_verify: ' + ', '.join(problems[:5]))
        report = doctor()
        failed = [c['id'] for c in report.get('checks', []) if c.get('required') and c.get('ok') is False
                  and c.get('id') not in NOT_A_GATE]
        if failed or not report.get('checks'):
            raise ServicesError('doctor_required_checks_fail: ' + (', '.join(failed) or 'no report'))

    def start(self, doctor):
        self.gate(doctor)
        self.agents.mkdir(parents=True, exist_ok=True)
        for name, _ in ARGS:
            svc = self.spec['services'][name]
            Path(svc['stdout']).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.plist_path(name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(plistlib.dumps(self.plist(name), sort_keys=True))
            os.chmod(path, 0o600)
            self._launchctl('bootout', f"gui/{self.uid}/{svc['label']}")  # not loaded yet: launchctl says so, fine
            loaded = self._launchctl('bootstrap', f'gui/{self.uid}', str(path))
            if loaded.returncode != 0:
                raise ServicesError(f'bootstrap_failed: {name} (launchctl exit {loaded.returncode})')
        return self.status()

    def stop(self):
        for name, _ in ARGS:
            svc, path = self.spec['services'][name], self.plist_path(name)
            self._launchctl('bootout', f"gui/{self.uid}/{svc['label']}")
            try:
                if plistlib.loads(path.read_bytes()).get('Label') == svc['label']:
                    path.unlink()
            except (OSError, ValueError, plistlib.InvalidFileException):
                pass
        return self.status()

    def status(self):
        return {'jobs': {name: self.state(name) for name, _ in ARGS}}


def program_doctor(root, offline=False):
    """The deployment program's own `doctor --json` (the report the owner sees), as a dict."""
    out = subprocess.run([sys.executable, '-I', str(Path(root) / PROGRAM), 'doctor', '--json',
                          *(['--offline'] if offline else [])], capture_output=True, text=True, timeout=120,
                         stdin=subprocess.DEVNULL)
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {'checks': [], 'detail': f'doctor exit {out.returncode}'}


def main(assembly, argv, **inject):
    command = argv[0] if argv else ''
    offline = argv[1:] == ['--offline'] and command == 'start'
    if command not in ('plan', 'start', 'stop', 'status') or len(argv) != 1 + offline:
        print('usage: services plan|start [--offline]|stop|status', file=sys.stderr)
        return 2
    root = Path(assembly.program).parents[1]
    doctor = inject.pop('doctor', None) or (lambda: program_doctor(root, offline))
    try:
        s = Services(root, assembly.deployment.owner.key, **inject)
        result = {'plan': s.plan, 'start': lambda: s.start(doctor), 'stop': s.stop, 'status': s.status}[command]()
    except ServicesError as exc:
        print(json.dumps({'error': 'ServicesRefused', 'detail': str(exc)}), file=sys.stderr)
        return 78
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
