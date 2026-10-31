import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from kage.core import store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def module_tables(self, db):
        # what the runtime's module initializers create before the column migrations run
        db.executescript('CREATE TABLE IF NOT EXISTS consumer_runs (run_id TEXT); CREATE TABLE IF NOT EXISTS rule_checks (id TEXT);'
                         'CREATE TABLE IF NOT EXISTS tasks (task_id TEXT); CREATE TABLE IF NOT EXISTS card_reply_routes (c TEXT);'
                         'CREATE TABLE IF NOT EXISTS task_deadlines (id TEXT); CREATE TABLE IF NOT EXISTS dispatches (id TEXT);')

    def test_connect_creates_a_private_store_and_is_idempotent(self):
        calls = []
        path = self.tmp / 'state' / 'state.sqlite'
        db = store.connect(path, initializers=(lambda db: calls.append('a'), self.module_tables))
        self.assertEqual(calls, ['a'])
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), '0o700')
        self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        schema = list(db.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name"))
        self.assertIn('agent_sends', {r[1] for r in schema})
        self.assertIn('mentions', {r[1] for r in db.execute('PRAGMA table_info(agent_sends)')})
        db.close()
        again = store.connect(path, initializers=(self.module_tables,))
        self.assertEqual(list(again.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name")), schema)

    def test_a_shared_state_directory_is_refused(self):
        shared = self.tmp / 'shared'
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        with self.assertRaisesRegex(ValueError, 'state_directory_must_be_private_0700'):
            store.connect(shared / 's.sqlite')

    def test_add_column_and_transaction(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        db.execute('CREATE TABLE t (a TEXT)')
        self.assertTrue(store.add_column(db, 't', 'b', 'TEXT'))
        self.assertFalse(store.add_column(db, 't', 'b', 'TEXT'))
        with self.assertRaises(sqlite3.OperationalError):
            store.add_column(db, 'missing', 'b', 'TEXT ZZZ (')
        with self.assertRaises(RuntimeError):
            with store.transaction(db):
                db.execute("INSERT INTO t VALUES ('x', 'y')")
                raise RuntimeError('boom')
        self.assertEqual(db.execute('SELECT count(*) FROM t').fetchone()[0], 0)
        with store.transaction(db):
            db.execute("INSERT INTO t VALUES ('x', 'y')")
        self.assertEqual(db.execute('SELECT count(*) FROM t').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
