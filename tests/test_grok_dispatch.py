"""Exercise generated deployment CLI, supervisor and runner with an isolated fake Grok binary."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from tests import test_dispatch_run

FAKE_GROK = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if '--version' in args:
    print('1.0.0 test')
    sys.exit(0)
if Path('.malformed-grok').exists():
    print('{')
    sys.exit(0)
if Path('.fail-grok').exists():
    print(json.dumps({'error': 'HTTP 401 invalid api key'}))
    sys.exit(1)
sid = args[args.index('-r' if '-r' in args else '-s') + 1]
assert os.environ.get('GROK_SESSION_ID') == sid
assert not os.environ.get('CODEX_THREAD_ID')
assert not os.environ.get('CLAUDE_CODE_SESSION_ID')
prompt = args[args.index('-p') + 1]
context = prompt
path = os.environ.get('KAGE_RUN_CONTEXT')
if path and Path(path).exists():
    context += Path(path).read_text()
cli = re.search(r'`(python3 \S+ --db \S+) ', prompt)
done = []
for event in sorted(set(re.findall(r'event=([0-9a-f]{24})', context))):
    base = cli.group(1).split() if cli else []
    result = subprocess.run([sys.executable, *base[1:], 'resolve', '--event', event, '--disposition', 'observe'],
                            capture_output=True, text=True)
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    done.append(event)
history = Path.home() / '.grok/sessions/test' / sid / 'chat_history.jsonl'
if '-r' in args:
    assert history.exists(), 'resume did not find the original session'
else:
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text('session remembered\n')
print(json.dumps({'text': json.dumps(done), 'sessionId': sid, 'stopReason': 'EndTurn',
                  'usage': {'inputTokens': 12, 'outputTokens': 3}}))
'''


class GrokDispatchTest(test_dispatch_run.DispatchRunTest):
    def setUp(self):
        super().setUp()
        (self.bin / 'grok').write_text(FAKE_GROK)
        (self.bin / 'grok').chmod(0o755)
        self.env.pop('GROK_SESSION_ID', None)
        self.env.pop('GROK_AGENT', None)
        # Only the fake: a spawn failure must never fall through to a Grok CLI installed on the test host.
        self.env['PATH'] = os.pathsep.join(d for d in self.env.get('PATH', '').split(os.pathsep)
                                           if d == str(self.bin) or not (Path(d) / 'grok').exists())

    def rows(self, task):
        with sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM consumer_runs WHERE task=? ORDER BY started_at', (task,))]

    def wait_runs(self, supervisor, task, predicate):
        deadline = time.time() + 90
        while time.time() < deadline:
            rows = self.rows(task)
            if predicate(rows):
                return rows
            self.assertIsNone(supervisor.poll(), self.diag())
            time.sleep(.5)
        self.fail(self.diag())

    def test_a_launch_is_dispatched_through_the_generated_runner(self):
        self.ok('config', 'set', 'dispatch.enabled', 'true')
        self.ok('config', 'set', 'routing.fallback_order', '["claude", "codex"]')
        task = self.home / 'kage-tasks/t1'; task.mkdir()
        (task / 'goal.md').write_text('# Goal\nProvider acceptance.\n')
        (task / 'status.md').write_text('<!-- rev: 1 -->\n# Status\n')
        prompt = self.tmp / 'prompt.md'; prompt.write_text('Complete the acceptance run.\n')
        log = open(self.tmp / 'supervisor.log', 'w'); self.addCleanup(log.close)
        supervisor = subprocess.Popen([sys.executable, str(self.target / 'scripts/kage.py'), 'run'], env=self.env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=str(self.tmp))
        self.addCleanup(lambda: (supervisor.terminate(), supervisor.wait(20)))
        self.wait_for_supervisor(supervisor)
        # The generated deployment starts installed; this isolated acceptance store is active.
        from kage.core.mode import KEY
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE runtime_meta SET value=? WHERE key=?', ('active', KEY))
        self.ok('launch-task', '--task', str(task), '--provider', 'grok', '--prompt-file', str(prompt), '--wait-seconds', '60')
        runs = self.wait_runs(supervisor, 't1', lambda rows: rows and rows[-1]['state'] == 'ended')
        first = runs[0]
        self.assertEqual((first['provider'], first['mode'], first['outcome']), ('grok', 'fresh', 'success'), self.diag())
        self.assertTrue((Path(first['log_dir']) / 'readable.log').read_text().strip())
        event = json.loads(self.ok('note', '--task', 't1', '--text', 'Resume and resolve this event.').stdout)['event']
        runs = self.wait_runs(supervisor, 't1', lambda rows: len(rows) >= 2 and rows[-1]['state'] == 'ended')
        self.assertEqual((runs[-1]['provider'], runs[-1]['mode'], runs[-1]['session'], runs[-1]['outcome']),
                         ('grok', 'resume', first['session'], 'success'), self.diag())
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT status FROM events WHERE id=?', (event,)).fetchone()[0], 'handled')
        (task / '.fail-grok').touch()
        self.ok('note', '--task', 't1', '--text', 'Exercise provider fallback.')
        runs = self.wait_runs(supervisor, 't1', lambda rows: len(rows) >= 4 and rows[-1]['state'] == 'ended')
        self.assertEqual((runs[-2]['provider'], runs[-2]['error_class']), ('grok', 'auth'), self.diag())
        self.assertEqual((runs[-1]['provider'], runs[-1]['mode'], runs[-1]['outcome']),
                         ('claude', 'fresh', 'success'), self.diag())

        self.ok('config', 'set', 'dispatch.retry_backoff_seconds', '[]')
        (task / '.fail-grok').unlink()
        (task / '.malformed-grok').touch()
        self.ok('launch-task', '--task', str(task), '--provider', 'grok', '--allow-existing', '--prompt-file', str(prompt), '--wait-seconds', '60')
        runs = self.wait_runs(supervisor, 't1', lambda rows: len(rows) >= 6 and rows[-1]['state'] == 'ended')
        self.assertEqual((runs[-2]['provider'], runs[-2]['outcome']), ('grok', 'failed'), self.diag())
        self.assertEqual((runs[-1]['provider'], runs[-1]['outcome']), ('claude', 'success'), self.diag())
        (self.bin / 'grok').unlink()
        # Use a missing binary rather than whatever may be installed on the test host.
        (self.bin / 'grok').write_text('#!/nonexistent/grok-interpreter\n')
        (self.bin / 'grok').chmod(0o755)
        self.ok('launch-task', '--task', str(task), '--provider', 'grok', '--allow-existing', '--prompt-file', str(prompt), '--wait-seconds', '60')
        runs = self.wait_runs(supervisor, 't1', lambda rows: len(rows) >= 8 and rows[-1]['state'] == 'ended')
        self.assertEqual((runs[-2]['provider'], runs[-2]['error_class']), ('grok', 'spawn_failed'), self.diag())
        self.assertEqual((runs[-1]['provider'], runs[-1]['outcome']), ('claude', 'success'), self.diag())
