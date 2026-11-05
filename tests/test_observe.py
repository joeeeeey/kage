"""O2c: a no-tools triage call per held intake dispatch while observing (kage.core.observe)."""
import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import types
import unittest

from kage.core import dispatch as D
from kage.core import mode as M
from kage.core import observe as O
from kage.core import worker_config as W
from tests.test_dispatch import core

DEFAULTS = json.loads((Path(__file__).with_name('fixtures') / 'worker_defaults.json').read_text())


class ObserveTriageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        raw = json.loads(json.dumps(DEFAULTS))
        raw['dispatch'].update(enabled=True)
        (self.tmp / 'config.json').write_text(json.dumps(raw))
        wc = W.WorkerConfig('en', defaults=DEFAULTS, config_env='KAGE_TEST_WORKER_CONFIG',
                            default_path=self.tmp / 'config.json', owner_key='owner', owner_name='Mira')
        self.inbox = wc.load()[0]['intake']['inbox_task']
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            'CREATE TABLE events (id TEXT PRIMARY KEY, watch_id TEXT, status TEXT, receipt TEXT, received REAL, '
            'disposition TEXT);'
            'CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);'
            'CREATE TABLE intake_bursts (burst_id TEXT PRIMARY KEY, event_id TEXT, refs TEXT, thread_root TEXT, '
            'channel TEXT, first_ts TEXT, last_ts TEXT, conv_type TEXT, created_at REAL);')
        D.initialize(self.db)
        self.commands, self.spawned = [], []

        def build_command(provider, mode, session, prompt, model, effort, *rest):
            self.commands.append({'provider': provider, 'prompt': prompt, 'args': rest})
            return ['fake-provider']

        def spawn(cmd, cwd, env, run_dir):
            self.spawned.append(Path(run_dir))
            return 4242

        self.history = {'messages': [{'ts': '10.0', 'user': 'U0000PEER01', 'text': 'Can you look at the failing build?'},
                                     {'ts': '9.0', 'user': 'U0000PEER01', 'text': 'unrelated older message'}]}
        rt = types.SimpleNamespace(
            __file__='/nonexistent/runtime.py', liveness=None, worker_config=wc, _meta_error=lambda *a: None,
            transaction=lambda db: contextlib.nullcontext(), api_identity=lambda bot=False: 'xoxb-test',
            slack=types.SimpleNamespace(api_call=lambda endpoint, token, params=None, json_body=None: self.history),
            DEPLOYMENT=types.SimpleNamespace(owner=types.SimpleNamespace(short_name='Mira', language='en')))
        self.d = D.Dispatcher(self.db, '/tmp/kage-test.sqlite', rt, probe=object(), spawn=spawn,
                              pid_start=lambda pid: 'start', killer=lambda pid, sig: self.killed.append(pid),
                              clock=lambda: 100.0, core=core(build_command=build_command))
        self.d.alive = lambda pid, start: self.running
        self.running, self.killed = True, []
        self.started = []
        self.d.start = lambda dispatch_id, now=None: self.started.append(dispatch_id)
        for task, did, eid in ((self.inbox, 'd-in', 'e1'), ('release-task', 'd-rel', 'e2')):
            self.db.execute("INSERT INTO dispatches (dispatch_id, task_key, task_id, task_path, state, trigger, reason, "
                            "plan, created_at, updated_at) VALUES (?, ?, ?, '/x', 'waiting', 'event', 'events', ?, 1, 1)",
                            (did, task, task, json.dumps([{'provider': 'claude', 'mode': 'fresh'}])))
            self.db.execute("INSERT INTO events VALUES (?, 'w', 'dispatched', ?, 1.0, NULL)", (eid, 'dispatch:' + did))
        self.db.execute("INSERT INTO intake_bursts VALUES ('b1', 'e1', ?, NULL, 'D0000PEER01', '10.0', '10.0', 'im', 1)",
                        (json.dumps(['10.0']),))
        self.db.commit()

    def answer(self, text):
        (self.spawned[-1] / 'last.md').write_text(text)
        (self.spawned[-1] / 'exit.json').write_text(json.dumps({'exit_code': 0}))
        self.running = False

    def test_nothing_while_active_or_installed(self):
        self.d.cycle(now=100.0)
        self.assertEqual(self.commands, [])
        self.assertEqual(sorted(self.started), ['d-in', 'd-rel'])
        M.write(self.db, 'installed', 'init')
        self.started.clear()
        self.d.cycle(now=101.0)
        self.assertEqual((self.commands, self.started), ([], []))  # installed: no run, no triage either

    def test_one_tool_less_call_per_held_intake_dispatch_and_a_shadow_triage_row(self):
        M.write(self.db, 'installed', 'init')
        M.write(self.db, 'observe', 'services')
        self.d.cycle(now=100.0)
        self.assertEqual(self.started, [])
        self.assertEqual(len(self.commands), 1)
        call = self.commands[0]
        self.assertEqual(call['args'][-1], D.PROBE_CLAUDE_ARGS)  # the probe's tool-off arguments
        self.assertIn('--tools', D.PROBE_CLAUDE_ARGS)
        self.assertIn('Can you look at the failing build?', call['prompt'])
        self.assertNotIn('unrelated older message', call['prompt'])  # only the burst's own messages
        self.d.cycle(now=110.0)
        self.assertEqual(len(self.commands), 1)  # one at a time, still running
        self.answer('Thinking...\n{"route": "task", "why": "A build failure needs work."}')
        self.d.cycle(now=120.0)
        rows = M.overview(self.db)['shadow']['recent']
        triage = [json.loads(r['what']) for r in rows if r['kind'] == 'triage']
        self.assertEqual(triage, [{'burst': 'b1', 'route': 'task', 'why': 'A build failure needs work.'}])
        self.assertNotIn('failing build', json.dumps(rows))  # the text is never stored
        self.assertEqual(self.db.execute("SELECT status, disposition FROM events WHERE id='e1'").fetchone()[:],
                         ('handled', 'observe'))
        self.assertEqual(self.db.execute("SELECT state, outcome FROM dispatches WHERE dispatch_id='d-in'").fetchone()[:],
                         ('done', 'observed'))
        self.assertFalse(self.spawned[0].exists())  # the scratch directory is removed
        self.assertEqual(self.db.execute("SELECT state FROM dispatches WHERE dispatch_id='d-rel'").fetchone()[0], 'waiting')

    def test_a_call_past_its_timeout_is_killed_and_recorded_unclear(self):
        M.write(self.db, 'installed', 'init')
        M.write(self.db, 'observe', 'services')
        self.d.cycle(now=100.0)
        self.d.cycle(now=100.0 + O.TRIAGE_TIMEOUT_SECONDS + 1)
        self.assertEqual(self.killed, [4242])
        triage = [json.loads(r['what']) for r in M.overview(self.db)['shadow']['recent'] if r['kind'] == 'triage']
        self.assertEqual((triage[0]['route'], triage[0]['why']), ('unclear', 'timeout'))

    def test_parse(self):
        self.assertEqual(O.parse('x {"route": "reply", "why": " ok \\n fine "} y'), {'route': 'reply', 'why': 'ok fine'})
        self.assertIsNone(O.parse('{"route": "launch the missiles"}'))
        self.assertIsNone(O.parse('no json here'))


if __name__ == '__main__':
    unittest.main()
