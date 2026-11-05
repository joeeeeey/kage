"""O1c5f: a generated deployment dispatches. With dispatch switched on, the deployment's own supervisor (`scripts/kage.py
run`) starts a launched task's first session through the generated scripts/dispatch_runner.py on the pinned release;
the agent is a fake `claude` (it answers the liveness probe, resolves whatever events its Run context names through
the program, and prints a stream-json result). Scratch HOME, no Slack, no network."""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import unittest

from tests.test_init import Base

FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""A stand-in for `claude -p`: resolves every event its Run context names, through the CLI its prompt names."""
import json, os, re, subprocess, sys
args = sys.argv[1:]
if '--version' in args:
    print('0.0.0 (test agent)')
    sys.exit(0)
if args[:1] == ['agents']:  # the liveness probe: no interactive session here
    print('[]')
    sys.exit(0)
prompt = args[args.index('-p') + 1] if '-p' in args else ''
session = args[args.index('--resume') + 1] if '--resume' in args else args[args.index('--session-id') + 1]
context = prompt
path = os.environ.get('KAGE_RUN_CONTEXT')
if path and os.path.exists(path):
    context += open(path).read()
cli = re.search(r'`(python3 \S+ --db \S+) ', prompt)
env = {**os.environ, 'CLAUDE_CODE_SESSION_ID': session}
done = []
for event in sorted(set(re.findall(r'event=([0-9a-f]{24})', context))):
    base = cli.group(1).split() if cli else []
    subprocess.run([sys.executable, *base[1:], 'context', '--event', event], env=env, capture_output=True)
    r = subprocess.run([sys.executable, *base[1:], 'resolve', '--event', event, '--disposition', 'observe'], env=env,
                       capture_output=True, text=True)
    done.append([event, r.returncode])
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': session}))
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': session,
                  'result': json.dumps(done)}))
'''


class DispatchRunTest(Base):
    def setUp(self):
        super().setUp()
        self.records()
        self.init()
        self.bin = self.home / 'bin'  # not under /tmp's transient prefixes only by name: the PATH is the env's
        self.bin.mkdir()
        (self.bin / 'claude').write_text(FAKE_CLAUDE)
        os.chmod(self.bin / 'claude', 0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('SLACK_', 'KAGE_')) and '_DISPATCH_' not in k
                    and 'RUN_CONTEXT' not in k and k not in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'PYTHONPATH')}
        self.env.update({'HOME': str(self.home), 'KAGE_NO_REAL_SLACK': '1', 'KAGE_NO_AUTOSTART': '1', 'KAGE_SECRETS_BACKEND': 'file',
                         'KAGE_VERSIONS_DIR': str(self.release.parent), 'PYTHONDONTWRITEBYTECODE': '1',
                         'PATH': f'{self.bin}{os.pathsep}{os.environ.get("PATH", "")}'})
        self.db = self.home / '.local/share/kage/state-mira/state.sqlite'

    def program(self, *args, session=None):
        env = {**self.env, **({'CLAUDE_CODE_SESSION_ID': session} if session else {})}
        return subprocess.run([sys.executable, str(self.target / 'scripts/kage.py'), *args], capture_output=True,
                              text=True, env=env, timeout=120, stdin=subprocess.DEVNULL, cwd=str(self.tmp))

    def ok(self, *args, **kw):
        out = self.program(*args, **kw)
        self.assertEqual(out.returncode, 0, (args, out.stderr[-2000:]))
        return out

    def wait_for_supervisor(self, supervisor, seconds=60):
        """Until the supervisor has written its heartbeat (its dispatcher tables exist from then on)."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.assertIsNone(supervisor.poll(), (self.tmp / 'supervisor.log').read_text()[-2000:])
            try:
                with sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True) as db:
                    if db.execute('SELECT heartbeat FROM runtime WHERE id=1').fetchone():
                        return
            except sqlite3.OperationalError:
                pass
            time.sleep(0.5)
        self.fail('the supervisor wrote no heartbeat')

    def diag(self):
        with sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True) as db:
            rows = {t: db.execute(f'SELECT * FROM {t}').fetchall() for t in ('dispatches', 'consumer_runs')
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()}
            meta = db.execute("SELECT key, substr(value,1,300) FROM runtime_meta WHERE key LIKE '%error%'").fetchall()
            events = db.execute('SELECT id, watch_id, status FROM events').fetchall()
        return json.dumps({'rows': rows, 'meta': meta, 'events': events,
                           'log': (self.tmp / 'supervisor.log').read_text()[-1500:]}, default=str)[:6000]

    def test_a_launch_is_dispatched_through_the_generated_runner(self):
        self.ok('config', 'set', 'dispatch.enabled', 'true')
        task = self.home / 'kage-tasks/t1'
        task.mkdir()
        (task / 'goal.md').write_text('# Goal\n\nDispatch acceptance.\n')
        (task / 'status.md').write_text('<!-- rev: 1 -->\n# Status\n')
        (self.tmp / 'prompt.md').write_text('Acceptance launch: read goal.md, then finish.\n')
        log = open(self.tmp / 'supervisor.log', 'w')
        self.addCleanup(log.close)
        supervisor = subprocess.Popen([sys.executable, str(self.target / 'scripts/kage.py'), 'run'], env=self.env,
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=str(self.tmp))
        self.addCleanup(lambda: (supervisor.terminate(), supervisor.wait(20)))
        self.wait_for_supervisor(supervisor)
        launched = self.ok('launch-task', '--task', str(task), '--provider', 'claude', '--prompt-file',
                           str(self.tmp / 'prompt.md'), '--wait-seconds', '60')
        self.assertIn('t1', launched.stdout)
        run = None
        deadline = time.time() + float(os.environ.get('DISPATCH_TEST_SECONDS', '120'))
        while time.time() < deadline and run is None:
            time.sleep(1)
            with sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                run = db.execute("SELECT * FROM consumer_runs WHERE task='t1' AND state='ended'").fetchone()
            self.assertIsNone(supervisor.poll(), self.diag())
        self.assertIsNotNone(run, self.diag())
        self.assertEqual((run['provider'], run['mode'], run['exit_code'], run['outcome']),
                         ('claude', 'fresh', 0, 'success'), self.diag())
        run_dir = Path(run['log_dir'])
        self.assertEqual(json.loads((run_dir / 'exit.json').read_text())['exit_code'], 0)
        self.assertIn('"type": "result"', (run_dir / 'events.jsonl').read_text())
        self.assertTrue(re.search(r'\S', (run_dir / 'readable.log').read_text()))  # rendered by the runner's run_log
        child = json.loads((run_dir / 'child.json').read_text())
        self.assertNotEqual(child['pid'], child['runner_pid'])


if __name__ == '__main__':
    unittest.main()
