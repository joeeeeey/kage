import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kage.core import task_registry as tr

T0 = 1793400000.0
S1, S2 = '0199aaaa-0000-7000-8000-000000000001', '0199aaaa-0000-7000-8000-000000000002'
STORE = '''
  CREATE TABLE watches (id TEXT PRIMARY KEY, data TEXT NOT NULL);
  CREATE TABLE events (id TEXT PRIMARY KEY, watch_id TEXT, status TEXT, disposition TEXT, received REAL, action_ts TEXT);
  CREATE TABLE claude_claims (event_id TEXT, session_id TEXT, claimed_at REAL, released_at REAL, release_reason TEXT);
  CREATE TABLE consumer_runs (task TEXT, provider TEXT, session TEXT, state TEXT, started_at REAL);
  CREATE TABLE consumer_leases (task_key TEXT, holder_kind TEXT, provider TEXT, session TEXT);
'''


class TaskRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks, self.smoke = self.tmp / 'tasks', self.tmp / 'smoke'
        for d in ('task-a', 'task-b', '.hidden'):
            (self.tasks / d).mkdir(parents=True)
        (self.tasks / 'task-a' / 'goal.md').write_text('# Goal: move the store\n\nparent: task-b\n')
        (self.smoke / 'smoke-dispatch-1').mkdir(parents=True)
        os.symlink(self.tasks / 'task-b', self.tasks / 'task-old')  # a renamed task's old name
        self.reg = tr.TaskRegistry(root_env='TEST_TASKS_ROOT', default_root=self.tasks,
                                   smoke_root_env='TEST_SMOKE_ROOT', default_smoke_root=self.smoke)
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(STORE)
        tr.initialize(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def watch(self, wid, cwd, session, provider='codex', **extra):
        w = {'id': wid, 'cwd': str(cwd), 'session': session, 'provider': provider, 'state': 'running', 'started': T0, **extra}
        self.db.execute('INSERT INTO watches VALUES (?,?)', (wid, json.dumps(w)))
        return w

    def test_task_identity_of_a_cwd(self):
        r = self.reg
        self.assertEqual(r.task_id_for_cwd(self.tasks / 'task-a' / 'sub'), 'task-a')
        self.assertEqual(r.task_id_for_cwd(self.tasks / 'task-old'), 'task-b')
        self.assertIsNone(r.task_id_for_cwd(self.tasks))
        self.assertIsNone(r.task_id_for_cwd(self.tasks / '.hidden'))
        self.assertIsNone(r.task_id_for_cwd(self.tmp / 'elsewhere'))
        self.assertEqual(r.task_id_for_cwd(self.smoke / 'smoke-dispatch-1'), 'smoke-dispatch-1')
        self.assertIsNone(r.task_id_for_cwd(self.smoke / 'smoke-dispatch-1', base=self.tasks))
        self.assertEqual(r.task_dir('smoke-dispatch-1'), self.smoke / 'smoke-dispatch-1')
        self.assertEqual(r.task_dir('task-a'), self.tasks / 'task-a')
        with patch.dict(os.environ, {'TEST_TASKS_ROOT': str(self.tmp)}):
            self.assertEqual(r.root(), self.tmp)
            self.assertEqual(r.task_id_for_cwd(self.tasks / 'task-a'), 'tasks')
        self.assertEqual(tr.read_title(self.tasks / 'task-a'), 'Goal: move the store')

    def test_current_session_backfill_and_overrides(self):
        self.watch('w1', self.tasks / 'task-a', S1, last_relevant=T0 + 50)
        self.watch('w2', self.tasks / 'task-a', S2, provider='claude')
        self.db.execute("INSERT INTO events VALUES ('e1','w2','handled',NULL,?,NULL)", (T0 + 80,))
        self.watch('w3', self.tmp / 'elsewhere', S1)
        report = self.reg.backfill(self.db, now=T0 + 100)
        self.assertEqual((report['tasks_created'], report['watches_unbound_per_session'], report['multi_session_tasks']),
                         (1, 1, 1))
        row = tr._row(self.db, 'task-a')
        self.assertEqual((row['current_provider'], row['current_session'], row['title']), ('claude', S2, 'Goal: move the store'))
        self.assertEqual(self.reg.backfill(self.db, now=T0 + 200)['tasks'][0]['action'], 'keep')
        self.assertEqual(self.reg.note_success(self.db, 'codex', S1, self.tasks / 'task-a', T0 + 300, 'resolve'), 'task-a')
        self.assertEqual(tr.task_consumer(self.db, 'task-a'), ('codex', S1, 'current'))
        self.db.execute("INSERT INTO consumer_runs VALUES ('task-a','claude',?,'running',?)", (S2, T0 + 310))
        self.assertEqual(tr.task_consumer(self.db, 'task-a'), ('claude', S2, 'running'))
        with self.assertRaisesRegex(ValueError, 'unsupported_provider'):
            tr.set_current(self.db, 'task-a', 'other', S1, 'local:test')
        self.assertTrue(tr.set_current(self.db, 'task-a', 'codex', S2, 'local:test', now=T0 + 400)['ok'])
        self.reg.ensure_task(self.db, 'task-b', T0)
        tr.link_parent(self.db, 'task-a', 'task-b', 'local:test', now=T0)
        with self.assertRaisesRegex(ValueError, 'cycle'):
            tr.link_parent(self.db, 'task-b', 'task-a', 'local:test', now=T0)
        shown = self.reg.show_task(self.db, 'task-a')
        self.assertEqual([a['action'] for a in shown['audit']], ['set_current', 'link_parent'])
        self.assertEqual(shown['task']['owners'], [f'claude:{S2}', f'codex:{S1}'])
        listed = self.reg.list_tasks(self.db)
        self.assertEqual((listed['root'], [t['task_id'] for t in listed['tasks']]), (str(self.tasks), ['task-a', 'task-b']))
        self.assertEqual(tr.sync_parent_lines(self.db)['changes'], [])

    def test_shadow_routing(self):
        w1 = self.watch('w1', self.tasks / 'task-a', S1)
        w2 = self.watch('w2', self.tasks / 'task-a', S2, provider='claude')
        w3 = self.watch('w3', self.tmp / 'elsewhere', S1)
        self.reg.note_success(self.db, 'codex', S1, self.tasks / 'task-a', T0, 'resolve')
        self.assertEqual(self.reg.classify_route(self.db, w1)[3], 'match')
        self.assertEqual(self.reg.classify_route(self.db, w2)[3], 'multi_session_task_cross_provider')
        self.assertEqual(self.reg.classify_route(self.db, w3)[3], 'unbound_per_session')
        self.assertEqual(self.reg.record_shadow(self.db, w2, ['e1', 'e2'], 'push', now=T0), 2)
        self.assertEqual(self.reg.record_shadow(self.db, w1, ['e3'], 'push', now=T0 + 1), 1)
        report = tr.shadow_report(self.db)
        self.assertEqual((report['total'], report['matched'], report['zero_diff']), (3, 1, False))
        self.assertEqual(report['tasks_with_diffs'][0]['diffs'], 2)
        self.assertEqual(tr.parse_since('2h', now=T0), T0 - 7200)
        with self.assertRaisesRegex(ValueError, 'timezone'):
            tr.parse_since('2026-10-31T00:00:00')


if __name__ == '__main__':
    unittest.main()
