"""O1c2 acceptance: a deployment made only of Kage's neutral templates (owner `mira`, the example profile) assembles,
answers --help, passes the offline doctor and completes one local event cycle (note -> context -> resolve), with a
scratch HOME, no Slack token, no sibling skill and no network."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import kage
from kage import packs
from kage.ports import secrets

KAGE_ROOT = Path(kage.__file__).resolve().parents[1]
EXAMPLE = KAGE_ROOT / 'examples/northwind-mira/deploy'
SWITCHES = ('access', 'asks', 'assembly', 'authority', 'avatars', 'blockers', 'buttons', 'cards', 'claims', 'cli',
            'commands', 'dashboard', 'dashboard_rest', 'dashboard_web', 'decisions', 'delivery', 'dispatch_cards',
            'dispatch_core', 'dispatch_engine', 'dispatch_rest', 'envelope', 'escalation', 'followup_core', 'health',
            'intake_cli', 'intake_core', 'intake_engine', 'intake_playbook', 'intake_rest', 'learning', 'lifecycle',
            'liveness', 'loop', 'memory', 'previews', 'profile', 'prompts', 'providers', 'receiver', 'registry',
            'rename', 'routines', 'rules', 'runlog', 'runner', 'slack_client', 'store', 'threads', 'trace',
            'trace_rest', 'transport', 'worker_config')
SESSION = '00000000-0000-4000-8000-0000000acc01'


def build(root):
    shutil.copytree(EXAMPLE, root / 'deploy')
    owner = json.loads((root / 'deploy/owner.json').read_text())
    owner['owner']['key'] = 'mira'
    (root / 'deploy/owner.json').write_text(json.dumps(owner))
    pins = {}
    for name, pack_name, rel in (('default.access', 'default.access', 'deploy/packs/default/access.py'),
                                 ('default.playbook', 'mira.playbook', 'deploy/packs/mira/playbook.py')):
        data = packs.template(name)
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
        pins[pack_name] = {'path': rel, 'sha256': hashlib.sha256(data).hexdigest()}
    lock = {'schema': 'kage.lock/v1', 'kage': {'version': kage.__version__}, 'modules': dict.fromkeys(SWITCHES, 'kage'),
            'packs': pins}
    (root / 'kage.lock').write_text(json.dumps(lock))
    (root / 'scripts').mkdir()
    (root / 'scripts/program.py').write_text('# the deployment program\n')
    instance = json.loads((root / 'deploy/instance.json').read_text())
    instance['paths']['inbox_rules'] = '~/kage/mira/inbox-rules.md'
    instance['assembly'] = {'program': 'scripts/program.py', 'lock': 'kage.lock', 'runtime_version': 'accept-1'}
    (root / 'deploy/instance.json').write_text(json.dumps(instance))


class MinimalDeploymentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.root = self.tmp / 'home', self.tmp / 'root'
        self.home.mkdir()
        build(self.root)
        self.env = {k: v for k, v in os.environ.items()  # no token, no other deployment, no dispatched run identity
                    if not k.startswith(('SLACK_', 'KAGE_')) and '_DISPATCH_' not in k and 'RUN_CONTEXT' not in k
                    and k not in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID')}
        bin_dir = self.tmp / 'bin'  # an agent CLI the doctor can find on any machine (CI has none)
        bin_dir.mkdir()
        (bin_dir / 'claude').write_text('#!/bin/sh\necho "0.0.0 (test agent)"\n')
        os.chmod(bin_dir / 'claude', 0o755)
        self.env.update({'HOME': str(self.home), 'KAGE_NO_REAL_SLACK': '1', 'KAGE_SECRETS_BACKEND': 'file',
                         'PYTHONPATH': str(KAGE_ROOT),
                         'PYTHONDONTWRITEBYTECODE': '1', 'PATH': f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}'})

    def kage(self, *args, session=None):
        env = dict(self.env, **({'CLAUDE_CODE_SESSION_ID': session} if session else {}))
        return subprocess.run([sys.executable, '-m', 'kage', '--deployment', str(self.root / 'deploy'), *args],
                              capture_output=True, text=True, env=env, timeout=120, stdin=subprocess.DEVNULL)

    def test_no_sibling_skill_or_deployment_module(self):
        instance = json.loads((self.root / 'deploy/instance.json').read_text())
        self.assertNotIn('modules', instance['assembly'])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['deploy', 'kage.lock', 'scripts'])

    def test_help_doctor_and_one_local_event_cycle(self):
        out = self.kage('--help')
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn('ask-mira', out.stdout)
        state = self.home / '.local/share/kage/state-mira'
        doctor = self.kage('doctor', '--offline')
        self.assertNotEqual(doctor.returncode, 0)            # read-only: it reports the missing state directory
        self.assertIn('state.private', doctor.stdout + doctor.stderr)
        state.mkdir(parents=True, mode=0o700)
        os.chmod(state.parent, 0o700)
        doctor = self.kage('doctor', '--offline')
        self.assertIn('FAIL  secrets.port: file store: slack-bot-token absent', doctor.stdout)  # no token stored yet
        store = secrets.FileStore('mira', self.home / '.config/kage/mira/secrets')
        store.put(secrets.SLACK_BOT, 'xoxb-' + 'acceptance')
        store.put(secrets.SLACK_APP, 'xapp-' + 'acceptance')
        doctor = self.kage('doctor', '--offline')
        self.assertIn('doctor: all required checks pass', doctor.stdout + doctor.stderr, doctor.stdout[-3000:])
        self.assertNotIn('acceptance', doctor.stdout + doctor.stderr)  # presence only, never a value
        task = self.home / 'kage/tasks/t1'
        task.mkdir(parents=True)
        (task / 'goal.md').write_text('# Goal\n\nAcceptance task.\n')
        (task / 'status.md').write_text('<!-- rev: 1 -->\n# Status\n')
        first = self.kage('note', '--task', 't1', '--text', 'registers the task; no session yet, kept in history')
        self.assertEqual(json.loads(first.stdout)['reason'], 'task_without_current_session_note_kept_in_history')
        set_current = self.kage('tasks', 'set-current', '--task', 't1', '--provider', 'claude', '--session', SESSION,
                                '--reason', 'acceptance')
        self.assertEqual(set_current.returncode, 0, set_current.stderr[-2000:])
        note = self.kage('note', '--task', 't1', '--text', 'acceptance: one local event')
        self.assertEqual(note.returncode, 0, note.stderr[-2000:])
        event = json.loads(note.stdout)['event']
        self.assertTrue(event)
        context = self.kage('context', '--event', event, session=SESSION)
        self.assertEqual(context.returncode, 0, context.stderr[-2000:])
        self.assertTrue(json.loads(context.stdout)['local'])
        resolve = self.kage('resolve', '--event', event, '--disposition', 'observe', session=SESSION)
        self.assertEqual(resolve.returncode, 0, resolve.stderr[-2000:])
        with sqlite3.connect(state / 'state.sqlite') as db:
            self.assertEqual(db.execute('SELECT status, disposition FROM events WHERE id=?', (event,)).fetchone(),
                             ('handled', 'observe'))


if __name__ == '__main__':
    unittest.main()
