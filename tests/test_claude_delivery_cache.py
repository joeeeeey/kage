import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kage.adapters.providers import cache_report, claude_delivery as cd

SESSION = '0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000'


def store():
    db = sqlite3.connect(':memory:', isolation_level=None)  # the runtime's mode: explicit BEGIN IMMEDIATE
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE watches (id TEXT PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE events (id TEXT PRIMARY KEY, watch_id TEXT, status TEXT, receipt TEXT, disposition TEXT,
                           received REAL);''')
    cd.initialize(db)
    db.execute('INSERT INTO watches VALUES (?,?)', ('w1', json.dumps({'provider': 'claude', 'session': SESSION,
                                                                     'state': 'running'})))
    for i in range(3):
        db.execute('INSERT INTO events (id, watch_id, status, received) VALUES (?,?,?,?)', (f'e{i}', 'w1', 'pending', i))
    db.commit()
    return db


class OutboxTest(unittest.TestCase):
    def test_queue_claim_release_and_settle(self):
        db = store()
        cd.queue_watch(db, 'w1')
        self.assertEqual(db.execute("SELECT count(*) FROM events WHERE status='queued'").fetchone()[0], 3)
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': SESSION}):
            batch = cd.claim(db, SESSION, now=10.0)
            self.assertEqual(len(batch['events']), 3)
            self.assertIsNone(cd.claim(db, SESSION))                       # one outstanding batch per session
        done = cd.ClaudeDelivery('mira').settle_claimed(db, SESSION, ['e0', 'e1'], 'handled elsewhere', now=11.0)
        self.assertEqual(sorted(done), ['e0', 'e1'])
        self.assertEqual({r[0] for r in db.execute("SELECT disposition FROM events WHERE status='handled'")},
                         {'settled_by_mira'})
        with self.assertRaisesRegex(ValueError, 'must_match'):
            cd.claim(db, SESSION)                                          # not this session's process

    def test_wakes_and_hook(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            db = store()
            cd.bump(db, SESSION)
            cd.publish_wakes(db, tmp / 'state.sqlite')
            row = db.execute('SELECT * FROM claude_wakes').fetchone()
            self.assertEqual((row['generation'], row['signaled'], row['last_error']), (1, 1, None))
            hooks = cd.hook_config(tmp / 'state.sqlite', '/x/runtime.py', '/py')['hooks']
            self.assertEqual(sorted(hooks), ['FileChanged', 'SessionStart'])
            self.assertEqual(cd.provider({}), 'codex')
            with self.assertRaisesRegex(ValueError, 'unsupported_provider'):
                cd.provider({'provider': 'other'})
        finally:
            shutil.rmtree(tmp)


class CacheReportTest(unittest.TestCase):
    def test_usage_and_summaries(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            claude = tmp / 'claude.jsonl'
            claude.write_text('\n'.join(json.dumps(e) for e in (
                {'type': 'system', 'subtype': 'init'},
                {'type': 'assistant', 'message': {'usage': {'input_tokens': 10, 'cache_creation_input_tokens': 100,
                                                            'cache_read_input_tokens': 0}}},
                {'type': 'result', 'num_turns': 2, 'total_cost_usd': 0.5,
                 'usage': {'input_tokens': 20, 'cache_read_input_tokens': 300, 'output_tokens': 7}})) + '\n')
            usage = cache_report.claude_usage(claude)
            self.assertEqual((usage['cache_read_input_tokens'], usage['first_input'], usage['api_calls'], usage['turns'],
                              usage['total_read'], usage['cost_usd']), (0, 110, 1, 2, 300, 0.5))   # a cold first call
            codex = tmp / 'codex.jsonl'
            codex.write_text(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 50,
                                                                              'cached_input_tokens': 40}}) + '\n')
            codex_usage = cache_report.codex_usage(codex)
            self.assertEqual((codex_usage['input_tokens'], codex_usage['cache_read_input_tokens'], codex_usage['turns']),
                             (10, 40, 1))
            self.assertIsNone(cache_report.claude_usage(tmp / 'missing.jsonl'))
            self.assertEqual([cache_report._bucket(g) for g in (None, 10, 600, 7200)], ['first', 'le_5m', '5m_1h', 'gt_1h'])  # no earlier run: 'first'
            self.assertEqual(cache_report._median([3, 1, 2]), 2)
        finally:
            shutil.rmtree(tmp)


if __name__ == '__main__':
    unittest.main()
