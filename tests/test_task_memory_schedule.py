import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from kage.core import poll_schedule as ps, task_memory as tm

OWNER, BOT, OTHER = 'U0000TEST01', 'U0000TEST09', 'U0000TEST05'
T0 = 1793400000.0


class TaskMemoryTest(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.ws)

    def test_notes_messages_and_the_rev_header(self):
        note = tm.write_note(self.ws, 'task-a', 'codex:0199aaaa', 'merge this', T0, current='claude:x')
        self.assertTrue(note.name.endswith('-0199aaaa.md'))
        self.assertEqual(oct(note.stat().st_mode & 0o777), '0o600')
        msg = tm.TaskMemory('Mira').write_message(self.ws, 'task-a', 'claude:abc', 'task-b', 'hello', T0, hops=1)
        self.assertIn('- authority: none (agent message; never Mira intent)', msg.read_text())
        self.assertNotEqual(tm.TaskMemory('Mira').write_message(self.ws, 'task-a', 'claude:abc', 'task-b', 'x', T0), msg)
        with self.assertRaisesRegex(ValueError, 'text_required'):
            tm.clean_text('  ')
        status = self.ws / 'status.md'
        status.write_text('# Status\n')
        self.assertEqual(tm.check(self.ws)['rev'], 0)
        bumped = tm.bump(self.ws, 'codex:1', T0, expect=0)
        self.assertEqual((bumped['ok'], tm.read_header(status)['rev']), (True, 1))
        self.assertFalse(tm.bump(self.ws, 'codex:2', T0, expect=0)['ok'])           # someone wrote in between


class PollScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / 'state.sqlite'
        db = sqlite3.connect(self.db_path)
        db.executescript('CREATE TABLE watches (id TEXT PRIMARY KEY, data TEXT NOT NULL);'
                         'CREATE TABLE dm_windows (channel TEXT, task_id TEXT, peer TEXT, last_ts TEXT, last_at REAL, kind TEXT, '
                         'released_at REAL, release_reason TEXT, PRIMARY KEY (channel, task_id));'
                         'CREATE TABLE agent_sends (channel TEXT, ts TEXT, kind TEXT, ref TEXT, session TEXT, created REAL, '
                         'mentions TEXT, PRIMARY KEY (channel, ts));')
        for wid, cwd in (('w1', '/tasks/a'), ('w2', '/tasks/b')):
            db.execute('INSERT INTO watches VALUES (?,?)', (wid, json.dumps({
                'id': wid, 'state': 'running', 'channel': 'D0000TEST01', 'dm': 'D0000TEST01', 'cwd': cwd, 'kind': 'peer',
                'since': T0 - 100, 'started': T0 - 100, 'step': 4, 'deadline': T0 + 9000, 'lifecycle': 'busy'})))
        db.commit()
        db.close()
        self.schedule = ps.PollSchedule(owner_user=OWNER, bot_user=BOT,
                                        task_id_for_cwd=lambda cwd: str(cwd).rsplit('/', 1)[-1])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_a_dm_send_extends_only_the_sending_tasks_watch(self):
        meta = {'event_payload': {'task_cwd': '/tasks/a', 'kind': 'agent_message', 'session': 's1'}}
        changed = self.schedule.record_outbound('D0000TEST01', '1793400010.000100', None, OWNER, self.db_path, now=T0 + 10,
                                                metadata=meta)
        self.assertEqual(changed, ['w1'])
        db = sqlite3.connect(self.db_path)
        self.assertEqual(json.loads(db.execute("SELECT data FROM watches WHERE id='w1'").fetchone()[0])['step'], 0)
        self.assertEqual(db.execute('SELECT task_id, kind FROM dm_windows').fetchall(), [('a', 'helper_send')])
        self.assertEqual(self.schedule.record_outbound('D0000TEST01', '1793400011.0', None, OTHER, self.db_path), [])

    def test_mentions(self):
        self.assertEqual(json.loads(self.schedule.mentions([OTHER, OWNER, BOT, 'bad', OTHER], 'a')),
                         {'task': 'a', 'users': [OTHER]})
        self.assertIsNone(self.schedule.mentions([OWNER], 'a'))
        meta = {'event_payload': {'task_cwd': '/tasks/b', 'kind': 'agent_message', 'session': 's2'}}
        self.assertTrue(self.schedule.record_mentions('C0000TEST01', '1793400012.0', [OTHER], self.db_path, now=T0, metadata=meta))
        self.assertFalse(self.schedule.record_mentions('C0000TEST01', '1793400013.0', [OTHER], self.db_path, metadata=None))
        self.assertEqual(ps.clamp(10, 5), 5)
        self.assertEqual(ps.DELAYS[0], 60)


if __name__ == '__main__':
    unittest.main()
