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
import time
import unittest
from unittest import mock

import kage
from kage import app, packs
from kage.onboard import identity, manifest, tokens
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
        self.env.update({'HOME': str(self.home), 'KAGE_NO_REAL_SLACK': '1', 'KAGE_NO_AUTOSTART': '1', 'KAGE_SECRETS_BACKEND': 'file',
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

    def test_tokens_stored_and_the_owner_verified(self):
        """O1c3: tokens checked, then stored in the owner's store; the owner proven by the one-time code sent to the
        bot; Kage's transport reads the token from the store; doctor verifies both; no token outside the store."""
        state = self.home / '.local/share/kage/state-mira'
        state.mkdir(parents=True, mode=0o700)
        os.chmod(state.parent, 0o700)
        store = secrets.FileStore('mira', self.home / '.config/kage/mira/secrets')
        bot_token, app_token = 'xoxb-' + '0000-ACCEPT', 'xapp-' + '1-A0000ACCEPT-0000-abc'
        team, owner_id, dms = 'T0000TEST01', 'U0000TEST01', []

        def slack(method, token, params=None):
            if method == 'auth.test' and token == bot_token:
                return ({'ok': True, 'team_id': team, 'user_id': 'U0000BOT01', 'bot_id': 'B0000TEST01'},
                        {'x-oauth-scopes': ','.join(manifest.BOT_SCOPES)})
            if method == 'apps.connections.open' and token == app_token:
                return {'ok': True, 'url': 'wss://example.invalid/socket'}, {}
            if method == 'conversations.list' and token == bot_token:  # F5: Slackbot's DM is listed first
                return {'ok': True, 'channels': [{'id': 'D0000SLACKBOT', 'user': 'USLACKBOT'},
                                                 {'id': 'D0000TEST01', 'user': owner_id}]}, {}
            if method == 'conversations.history' and params['channel'] == 'D0000SLACKBOT':
                return {'ok': False, 'error': 'channel_not_found'}, {}
            if method == 'conversations.history' and token == bot_token:
                return {'ok': True, 'messages': [m for m in dms if float(m['ts']) > float(params['oldest'])]}, {}
            raise AssertionError(method)

        with self.assertRaises(tokens.TokenRefused):
            tokens.admit(store, tokens.BOT, bot_token, call=slack, team_id='T0000OTHER')
        self.assertFalse(store.has(tokens.BOT))  # refused before storage
        bot = tokens.admit(store, tokens.BOT, bot_token, call=slack)
        tokens.admit(store, tokens.APP, app_token, call=slack)
        code = identity.new_challenge(state)
        dms.append({'type': 'message', 'user': owner_id, 'team': team, 'text': code, 'ts': f'{time.time() + 1:.6f}'})
        record = identity.prove_by_dm(state, bot_token, bot, call=slack)
        self.assertEqual((record['method'], record['team_id'], record['user_id']), ('dm_code', team, owner_id))
        env = {'HOME': str(self.home), 'KAGE_SECRETS_BACKEND': 'file', 'KAGE_NO_REAL_SLACK': ''}
        with mock.patch.dict(os.environ, env):
            a = app.App(self.root / 'deploy')
            a.verify()
            self.assertEqual(a.leaf('slack_transport').default_bot_token(), bot_token)
        doctor = self.kage('doctor', '--offline')
        out = doctor.stdout + doctor.stderr
        self.assertIn('ok    owner.identity: verified (dm_code)', out)
        self.assertIn('ok    secrets.port: file store: slack-bot-token present, slack-app-token present', out)
        self.assertIn('doctor: all required checks pass', out, out[-3000:])
        for base in (self.root, self.home):
            for f in base.rglob('*'):
                if f.is_file() and store.root not in f.parents:
                    data = f.read_bytes()
                    for secret in (bot_token, app_token, code):
                        self.assertNotIn(secret.encode(), data, f)

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

    def test_the_owner_introduces_herself_and_gets_candidates(self):
        """O3a end to end: the introduction goes to the private directory (never the store), the estimate costs
        nothing, one no-tools call records candidates, an agent session is refused, nothing is created without a store."""
        from tests.test_onboard_intro import FAKE, INTRO
        (self.tmp / 'bin' / 'claude').write_text(FAKE)
        add = subprocess.run([sys.executable, '-m', 'kage', '--deployment', str(self.root / 'deploy'), 'onboard', 'intro',
                              'add', '-'], input=INTRO, capture_output=True, text=True, env=self.env, timeout=120)
        self.assertEqual(add.returncode, 0, add.stderr[-2000:])
        self.assertTrue(json.loads(add.stdout)['stored'])
        self.assertEqual(len(json.loads(self.kage('onboard', 'intro', 'list').stdout)['versions']), 1)
        estimate = self.kage('onboard', 'extract', '--estimate')
        self.assertLess(json.loads(estimate.stdout)['estimate']['usd'], 1.0)
        state = self.home / '.local/share/kage/state-mira'
        missing = self.kage('onboard', 'extract')
        self.assertNotEqual(missing.returncode, 0)  # no store yet: never created by onboarding
        self.assertFalse((state / 'state.sqlite').exists())
        state.mkdir(parents=True, mode=0o700)
        sqlite3.connect(state / 'state.sqlite').close()
        refused = self.kage('onboard', 'extract', session=SESSION)
        self.assertIn('onboarding_only_from_the_owners_terminal', refused.stderr)
        run = self.kage('onboard', 'extract')
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        self.assertEqual((json.loads(run.stdout)['status'], json.loads(run.stdout)['items']), ('done', 6))
        items = self.kage('onboard', 'items')
        self.assertIn('6 candidate(s); nothing is applied until they are confirmed', items.stdout)
        with sqlite3.connect(state / 'state.sqlite') as db:
            self.assertNotIn(INTRO.strip(), '\n'.join(db.iterdump()))
        # O3c: no Slack here, so verification records errors (never a guess); an adopted deployment (no mode row) gets
        # the private confirmed profile and a proposal, its owner.json untouched
        verify = self.kage('onboard', 'verify')
        self.assertEqual(verify.returncode, 0, verify.stderr[-2000:])
        self.assertNotIn('verified', json.loads(verify.stdout)['results'])
        with sqlite3.connect(state / 'state.sqlite') as db:
            db.execute("UPDATE onboarding_items SET status='accepted' WHERE target='owner'")
        before = (self.root / 'deploy/owner.json').read_bytes()
        applied = self.kage('onboard', 'apply')
        self.assertEqual(applied.returncode, 0, applied.stderr[-2000:])
        self.assertEqual((json.loads(applied.stdout)['applied'], json.loads(applied.stdout)['deployment']), (2, 'adopted'))
        self.assertEqual((self.root / 'deploy/owner.json').read_bytes(), before)
        self.assertTrue((self.home / '.local/share/kage/private/mira/proposal.json').is_file())
        self.assertIn('onboarding_only_from_the_owners_terminal', self.kage('onboard', 'apply', session=SESSION).stderr)


if __name__ == '__main__':
    unittest.main()
