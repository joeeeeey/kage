"""0.78.0: `python3 -m kage.migrate` moves a task registry between stores (export read-only, import as a plan first)."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from kage import migrate as M

DDL = '''
CREATE TABLE tasks (task_id TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT, state TEXT NOT NULL DEFAULT 'active',
  current_provider TEXT, current_session TEXT, current_source TEXT, last_run_at REAL, parent_task_id TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, approvals_channel TEXT, approvals_root_ts TEXT);
CREATE TABLE task_links (parent_task_id TEXT NOT NULL, child_task_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'parent',
  created_at REAL NOT NULL, created_by_session TEXT, PRIMARY KEY(parent_task_id, child_task_id, kind));
CREATE TABLE dispatch_prefs (task_key TEXT PRIMARY KEY, provider TEXT, important INTEGER NOT NULL DEFAULT 0,
  category TEXT, source TEXT, updated_at REAL NOT NULL);
CREATE TABLE dispatch_meta (task_key TEXT PRIMARY KEY, important INTEGER NOT NULL DEFAULT 0, category TEXT,
  updated_at REAL NOT NULL);
CREATE TABLE routine_state (task_id TEXT NOT NULL, routine_id TEXT NOT NULL, digest TEXT NOT NULL,
  definition TEXT NOT NULL, file TEXT NOT NULL, provenance TEXT NOT NULL, state TEXT NOT NULL, error TEXT,
  first_seen REAL NOT NULL, updated_at REAL NOT NULL, last_slot REAL, next_at REAL, last_run_id TEXT,
  last_fired_at REAL, PRIMARY KEY(task_id, routine_id));
CREATE TABLE routine_confirmations (task_id TEXT NOT NULL, routine_id TEXT NOT NULL, digest TEXT NOT NULL,
  status TEXT NOT NULL, via TEXT, card_event TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
  updated_at REAL NOT NULL, decided_by TEXT, decided_at REAL, card_settled_at REAL, error TEXT,
  smoke INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(task_id, routine_id, digest));
CREATE TABLE task_deadlines (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_path TEXT, due REAL NOT NULL,
  note TEXT NOT NULL, disposition TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, updated_at REAL NOT NULL);
'''
OLD, NEW = '/srv/old-root/work', '/srv/new-root/work'
SESSION = '00000000-0000-4000-8000-00000000test'


def store(path, extra=''):
    db = sqlite3.connect(str(path))
    db.executescript(DDL + extra)
    db.commit()
    return db


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / 'src.sqlite'
        db = store(self.src)
        db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('task-a', f'{OLD}/task-a', 'A', 'active', 'claude', SESSION, 'dispatch', 5.0, None, 1.0, 2.0, None, None))
        db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('task-b', '/elsewhere/task-b', 'B', 'closed', None, None, None, None, 'task-a', 1.0, 2.0, None, None))
        db.execute("INSERT INTO task_links VALUES ('task-a','task-b','parent',1.0,NULL)")
        db.execute("INSERT INTO dispatch_prefs VALUES ('task-a','codex',1,'infra','owner',3.0)")
        db.execute("INSERT INTO dispatch_meta VALUES ('task-a',1,'infra',3.0)")
        db.execute('INSERT INTO routine_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('task-a', 'daily', 'd1', '{}', f'{OLD}/task-a/routines.yaml', 'owner', 'active', None, 1.0, 2.0,
                    None, 9.0, None, None))
        db.execute("INSERT INTO routine_confirmations VALUES ('task-a','daily','d1','approved','button',NULL,1,1.0,2.0,"
                   "'UTEST0000001',2.0,NULL,NULL,0)")
        db.execute(f"INSERT INTO task_deadlines VALUES ('dl1','task-a','{OLD}/task-a',10.0,'check','pending',1.0,1.0)")
        db.execute(f"INSERT INTO task_deadlines VALUES ('dl2','task-a','{OLD}/task-a',10.0,'old','done',1.0,1.0)")
        db.commit()
        db.close()
        self.dst = self.tmp / 'dst.sqlite'
        db = store(self.dst)
        db.execute("INSERT INTO tasks (task_id,path,state,created_at,updated_at) VALUES ('task-a','/srv/new-root/work/task-a',"
                   "'active',7.0,7.0)")  # the destination's own row for the same task
        db.commit()
        db.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, sql):
        with sqlite3.connect(str(self.dst)) as db:
            return db.execute(sql).fetchall()

    def test_export_reads_only_and_writes_a_private_file(self):
        before = hashlib.sha256(self.src.read_bytes()).hexdigest()
        out = M.write_export(M.export(self.src, now=1.0), self.tmp / 'registry.json')
        self.assertEqual(hashlib.sha256(self.src.read_bytes()).hexdigest(), before)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        doc = json.loads(out.read_text())
        self.assertEqual(doc['schema'], M.SCHEMA)
        self.assertEqual({t: len(d['rows']) for t, d in doc['tables'].items()},
                         {'tasks': 2, 'task_links': 1, 'dispatch_prefs': 1, 'dispatch_meta': 1, 'routine_state': 1,
                          'routine_confirmations': 1, 'task_deadlines': 1})  # only the pending deadline
        with self.assertRaises(FileExistsError):  # never overwrites
            M.write_export(doc, out)

    def test_import_plans_first_then_writes_with_rewritten_paths(self):
        doc = M.export(self.src)
        before = self.dst.read_bytes()
        plan = M.import_(self.dst, doc, [f'{OLD}={NEW}'])
        self.assertFalse(plan['applied'])
        self.assertEqual(self.dst.read_bytes(), before)  # a dry run writes nothing
        self.assertEqual((plan['tables']['tasks']['new'], plan['tables']['tasks']['existing'],
                          plan['tables']['tasks']['rewritten']), (1, 1, 1))
        done = M.import_(self.dst, doc, [f'{OLD}={NEW}'], apply=True)
        self.assertTrue(done['applied'])
        self.assertEqual(self.rows("SELECT path, current_session FROM tasks WHERE task_id='task-a'"),
                         [('/srv/new-root/work/task-a', None)])  # the destination's own row stays
        self.assertEqual(self.rows("SELECT path, parent_task_id FROM tasks WHERE task_id='task-b'"),
                         [('/elsewhere/task-b', 'task-a')])  # a path outside the rule stays as it was
        self.assertEqual(self.rows('SELECT file FROM routine_state'), [(f'{NEW}/task-a/routines.yaml',)])
        self.assertEqual(self.rows('SELECT task_path FROM task_deadlines'), [(f'{NEW}/task-a',)])
        self.assertEqual(self.rows('SELECT provider, category FROM dispatch_prefs'), [('codex', 'infra')])
        self.assertEqual(self.rows('SELECT status FROM routine_confirmations'), [('approved',)])
        again = M.import_(self.dst, doc, [f'{OLD}={NEW}'], apply=True)  # idempotent
        self.assertEqual(sum(t['new'] for t in again['tables'].values()), 0)
        replaced = M.import_(self.dst, doc, [f'{OLD}={NEW}'], replace=True, apply=True)
        self.assertEqual(replaced['tables']['tasks']['replaced'], 2)
        self.assertEqual(self.rows("SELECT path, current_session, current_provider FROM tasks WHERE task_id='task-a'"),
                         [(f'{NEW}/task-a', SESSION, 'claude')])  # session ids kept

    def test_refusals(self):
        doc = M.export(self.src)
        for rules in (['nope'], ['relative=/x'], [f'{OLD}/={NEW}']):
            with self.assertRaises(M.Refused):
                M.import_(self.dst, doc, rules)
        with self.assertRaises(M.Refused):
            M.import_(self.dst, {'schema': 'other'})
        with self.assertRaises(M.Refused):
            M.import_(self.tmp / 'missing.sqlite', doc)
        bare = self.tmp / 'bare.sqlite'
        sqlite3.connect(str(bare)).close()
        with self.assertRaisesRegex(M.Refused, 'destination_tables_missing'):
            M.import_(bare, doc)
        with self.assertRaisesRegex(M.Refused, 'no_store'):
            M.export(self.tmp / 'missing.sqlite')

    def test_columns_only_one_side_has(self):
        with sqlite3.connect(str(self.src)) as db:
            db.execute('ALTER TABLE dispatch_meta ADD COLUMN newer TEXT')
        doc = M.export(self.src)
        plan = M.import_(self.dst, doc, apply=True)
        self.assertEqual(plan['tables']['dispatch_meta']['columns_dropped'], ['newer'])
        self.assertEqual(self.rows('SELECT category FROM dispatch_meta'), [('infra',)])

    def test_the_command_line(self):
        out, err = io.StringIO(), io.StringIO()
        target = self.tmp / 'r.json'
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(M.main(['export', '--db', str(self.src), '--out', str(target)]), 0)
            self.assertEqual(M.main(['import', '--db', str(self.dst), '--file', str(target), '--rewrite',
                                     f'{OLD}={NEW}']), 0)
            self.assertEqual(M.main(['import', '--db', str(self.dst), '--file', str(target), '--rewrite', 'bad']), 1)
        self.assertIn('"applied": false', out.getvalue())
        self.assertIn('bad_rewrite', err.getvalue())


if __name__ == '__main__':
    unittest.main()
