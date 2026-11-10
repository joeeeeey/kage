import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

from kage.adapters.providers import liveness
from kage.core import runner

SESSION = '0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000'


def fake_runner(answers):
    """answers: {first argv word: (returncode, stdout) | exception}"""
    def run(cmd, **kw):
        answer = answers.get(Path(cmd[0]).name)
        if isinstance(answer, BaseException):
            raise answer
        rc, out = answer or (1, '')
        return types.SimpleNamespace(returncode=rc, stdout=out)
    return run


class LivenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'sessions').mkdir()
        self.codex = self.tmp / 'codex'
        (self.codex / 'sessions' / '2026' / '10' / '31').mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def probe(self, answers):
        return liveness.Probe(runner=fake_runner(answers), claude_sessions_dir=self.tmp / 'sessions',
                              codex_home=self.codex, claude_projects_dir=self.tmp / 'projects', ttl=20, clock=lambda: 1000.0,
                              which=lambda name: '/usr/bin/' + name)  # lsof present (a slim image has none: /proc)

    def test_claude_states(self):
        listed = json.dumps([{'sessionId': SESSION, 'status': 'busy', 'pid': 42, 'kind': 'interactive'}])
        self.assertEqual(self.probe({'claude': (0, listed)}).check('claude', SESSION)['state'], liveness.LIVE_BUSY)
        self.assertEqual(self.probe({'claude': (0, '[]')}).check('claude', SESSION)['state'], liveness.DEAD)
        self.assertEqual(self.probe({'claude': (1, '')}).check('claude', SESSION)['state'], liveness.UNKNOWN)
        (self.tmp / 'sessions' / '42.json').write_text(json.dumps({'sessionId': SESSION, 'pid': 42, 'status': 'idle',
                                                                   'procStart': 'Sat Oct 31 01:00:00 2026'}))
        alive = self.probe({'claude': (1, ''), 'ps': (0, 'Sat Oct 31 01:00:00 2026\n')}).check('claude', SESSION)
        self.assertEqual((alive['state'], alive['source']), (liveness.LIVE_IDLE, 'session_file'))
        reused = self.probe({'claude': (0, '[]'), 'ps': (0, 'Sun Nov  1 09:00:00 2026\n')}).check('claude', SESSION)
        self.assertEqual(reused['state'], liveness.DEAD)                    # the pid was reused: not that session
        self.assertEqual(self.probe({}).check('claude', 'not-a-session')['source'], 'invalid_session')

    def test_codex_states(self):
        rollout = self.codex / 'sessions' / '2026' / '10' / '31' / f'rollout-2026-10-31T01-00-00-{SESSION}.jsonl'
        rollout.write_text('first\n' + json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started'}}) + '\n')
        held = f'p77\nn{rollout}\n'
        state = self.probe({'lsof': (0, held)}).check('codex', SESSION)
        self.assertEqual((state['state'], state['holders']), (liveness.LIVE_BUSY, [77]))
        self.assertEqual(self.probe({'lsof': (1, '')}).check('codex', SESSION)['state'], liveness.DEAD)
        self.assertEqual(self.probe({'lsof': (2, '')}).check('codex', SESSION)['state'], liveness.UNKNOWN)
        self.assertEqual(self.probe({'lsof': subprocess.TimeoutExpired('lsof', 20)}).check('codex', SESSION)['state'],
                         liveness.UNKNOWN)
        self.assertEqual(self.probe({}).session_bytes('codex', SESSION), rollout.stat().st_size)
        self.assertIsNone(self.probe({}).session_bytes('claude', SESSION))

    def test_codex_liveness_from_proc_without_lsof(self):
        # 0.78.5 (F17): a slim Linux image has no lsof: the holders come from /proc
        rollout = self.codex / 'sessions' / '2026' / '10' / '31' / f'rollout-2026-10-31T01-00-00-{SESSION}.jsonl'
        rollout.write_text('first\n' + json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete'}}) + '\n')
        proc = self.tmp / 'proc'
        (proc / 'self' / 'fd').mkdir(parents=True)
        for pid, comm, target in (('77', 'codex\n', rollout), ('88', 'python3\n', rollout), ('99', 'codex\n', self.tmp)):
            (proc / pid / 'fd').mkdir(parents=True)
            (proc / pid / 'comm').write_text(comm)
            os.symlink(target, proc / pid / 'fd' / '5')
        def probe(root):
            return liveness.Probe(runner=fake_runner({}), claude_sessions_dir=self.tmp / 'sessions', codex_home=self.codex,
                                  claude_projects_dir=self.tmp / 'projects', ttl=20, clock=lambda: 1000.0,
                                  which=lambda name: None, proc_root=root)
        state = probe(proc).check('codex', SESSION)
        self.assertEqual((state['state'], state['holders']), (liveness.LIVE_IDLE, [77]))  # python3 holding it: not codex
        os.unlink(proc / '77' / 'fd' / '5')
        self.assertEqual(probe(proc).check('codex', SESSION)['state'], liveness.DEAD)
        self.assertEqual(probe(self.tmp / 'no-proc').check('codex', SESSION)['state'], liveness.UNKNOWN)

    def test_probes_are_cached(self):
        calls = []
        probe = liveness.Probe(runner=lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=0, stdout='[]'),
                               claude_sessions_dir=self.tmp / 'sessions', ttl=20, clock=lambda: 1000.0)
        probe.check('claude', SESSION)
        probe.check('claude', SESSION)
        self.assertEqual(len(calls), 1)
        self.assertTrue(liveness.is_live(liveness.LIVE_IDLE) and not liveness.is_live(liveness.DEAD))


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp())
        os.chmod(self.run, 0o700)

    def tearDown(self):
        shutil.rmtree(self.run)

    def test_a_run_and_its_files(self):
        rendered = []
        log = types.SimpleNamespace(provider_of=lambda d: None, update=lambda d, p: rendered.append(('update', p)),
                                    finish=lambda d, info, p, stderr_tail=None: rendered.append(('finish', info['exit_code'],
                                                                                                 stderr_tail)))
        child = [sys.executable, '-c', 'import sys; print("{\\"type\\": \\"turn.completed\\"}"); '
                                       'print("boom", file=sys.stderr); sys.exit(3)']
        self.assertEqual(runner.main(['--run-dir', str(self.run), '--', *child], load_run_log=lambda: log), 0)
        exit_info = json.loads((self.run / 'exit.json').read_text())
        self.assertEqual((exit_info['exit_code'], exit_info['stopped']), (3, False))
        self.assertIn('turn.completed', (self.run / 'events.jsonl').read_text())
        self.assertEqual(oct((self.run / 'stderr.log').stat().st_mode & 0o777), '0o600')
        self.assertEqual(rendered[-1], ('finish', 3, 'boom\n'))
        self.assertEqual(json.loads((self.run / 'child.json').read_text())['runner_pid'], os.getpid())

    def test_refusals(self):
        self.assertEqual(runner.main(['--run-dir', str(self.run)]), 2)
        os.chmod(self.run, 0o755)
        self.assertEqual(runner.main(['--run-dir', str(self.run), '--', 'true']), 2)
        os.chmod(self.run, 0o700)
        self.assertEqual(runner.main(['--run-dir', str(self.run), '--', str(self.run / 'missing-binary')]), 127)
        self.assertEqual(json.loads((self.run / 'exit.json').read_text())['error'], 'FileNotFoundError')
        self.assertEqual(runner._provider(self.run, ['/usr/local/bin/claude', '-p'], None), 'claude')
        self.assertEqual(runner._provider(self.run, ['codex'], None), 'codex')


if __name__ == '__main__':
    unittest.main()
