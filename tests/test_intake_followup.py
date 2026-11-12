"""0.79.2 (F28): a reply in a thread with the bot reaches intake without an @mention; top-level channel messages are never
merged into one burst; intake-bootstrap keeps an existing role task's own heading and title."""
import json
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
import unittest.mock

from kage.core import intake as I
from tests.test_intake import BOT, core

PERSON, CH, ROOT = 'U0000ADA001', 'C0000OPS001', '1700000000.000100'


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.core = core()
        self.core.initialize(self.db)
        self.db.executescript('CREATE TABLE agent_sends (channel TEXT, ts TEXT); '
                              'CREATE TABLE watches (id TEXT PRIMARY KEY, data TEXT); '
                              'CREATE TABLE ask_cards (id TEXT, channel TEXT, ts TEXT, thread TEXT, dry INTEGER, '
                              'smoke INTEGER, state TEXT, watch_id TEXT, created_at REAL); '
                              'CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.rt = types.SimpleNamespace(
            envelopes=types.SimpleNamespace(people_path=lambda: Path('/nonexistent/people.json')),
            message_envelope=lambda base, m, root: {'trust': 'colleague', 'authority': 'none'})
        self.engine = I.Intake(self.db, ':memory:', self.rt, core=self.core)

    def verdict(self, text='cc relay', root=ROOT, identity='bot'):
        ctx = {'oncall': set(), 'cfg': {'identity': identity}, 'now': 1700000100.0}
        m = {'user': PERSON, 'ts': '1700000005.000300', 'text': text, 'thread_ts': root}
        return self.engine.verdict(m, CH, 'channel', root, ctx)

    def test_a_reply_in_a_thread_with_the_bot_is_addressed_to_it(self):
        self.assertEqual(self.verdict(), 'no_bot_mention')  # a thread the bot never took part in: as before
        self.assertEqual(self.verdict(root=None), 'no_bot_mention')
        self.assertEqual(self.verdict(f'<@{BOT}> cc relay'), 'accept')
        cases = (
            ('INSERT INTO agent_sends VALUES (?, ?)', (CH, ROOT)),  # the thread root is our own post
            ('INSERT INTO intake_bindings (channel,thread_root,task_id,created_at) VALUES (?,?,?,?)',
             (CH, ROOT, 'owner-inbox', 1.0)),  # intake bound the thread to a task
            ("INSERT INTO ask_cards VALUES ('c1',?,?,NULL,0,0,'open','w1',1.0)", (CH, ROOT)),  # a task asked there
            ('INSERT INTO watches VALUES (?, ?)', ('w2', json.dumps({'state': 'running', 'channel': CH, 'thread': ROOT,
                                                                     'kind': 'followup'}))),  # a task waits on it
        )
        for sql, args in cases:
            with self.subTest(sql=sql.split()[2]):
                self.db.execute(sql, args)
                self.engine._watch_cache = None
                self.assertEqual(self.verdict(), 'accept')
                self.db.execute(f'DELETE FROM {sql.split()[2]}')
                self.engine._watch_cache = None
                self.assertEqual(self.verdict(), 'no_bot_mention')
        # the bot answered the request whose thread this is (an intake reply, e.g. a clarifying question)
        self.db.execute("INSERT INTO intake_bursts (burst_id,conv_key,channel,conv_type,first_ts,last_ts,msg_count,refs,"
                        "state,created_at,updated_at) VALUES ('in1','k',?,'channel',?,?,1,'[]','routed',1,1)", (CH, ROOT, ROOT))
        self.assertEqual(self.verdict(), 'no_bot_mention')  # not answered yet
        self.db.execute("INSERT INTO intake_sends (id,burst_id,kind,channel,ts,state,created_at,updated_at) VALUES "
                        "('s1','in1','intake_reply',?,'1700000002.000100','sent',1,1)", (CH,))
        self.assertEqual(self.verdict(), 'accept')
        # an owner-identity deployment: thread replies reach tasks through their watches, unchanged
        self.assertEqual(self.verdict(identity=None), 'accept')
        self.db.execute('DELETE FROM intake_sends')
        self.assertEqual(self.verdict(identity=None), 'accept')

    def test_a_followup_push_is_taken_only_in_a_thread_with_the_bot(self):
        cfg = {'intake': {'enabled': True, 'identity': 'bot', 'oncall_channels': []}}
        message = {'ts': '1700000005.000300', 'user': PERSON, 'text': 'cc relay', 'thread_ts': ROOT}
        rt = types.SimpleNamespace(worker_config=types.SimpleNamespace(load=lambda: (cfg, None)),
                                   api_identity=lambda bot=False: 'bot-token',
                                   slack=types.SimpleNamespace(api_call=lambda method, token, params=None: {
                                       'messages': [message]}), **vars(self.rt))
        self.db.execute('INSERT INTO runtime_meta VALUES (?, ?)', (I.STATE_KEY, json.dumps({'baseline': 1.0})))
        push = lambda **kw: self.core.intake_push(rt, self.db, ':memory:', **{  # noqa: E731
            'channel': CH, 'ts': '1700000005.000300', 'user': PERSON, 'thread_ts': ROOT, 'followup': True, **kw})
        self.assertEqual(push(thread_ts=None)['reason'], 'invalid_refs')  # a follow-up is a thread reply
        seen = []
        with unittest.mock.patch.object(I.Intake, 'ingest', lambda engine, ms, *a: seen.append(ms[0]['ts'])):
            self.assertEqual(push(), {'ok': True, 'outcome': 'skipped', 'reason': 'not_bot_thread'})
            self.assertEqual(seen, [])
            message['text'] = f'<@{BOT}> cc relay'  # a mention (its app_mention event may come second): taken
            push()
            self.assertEqual(len(seen), 1)
            message['text'] = 'cc relay'
            self.db.execute('INSERT INTO agent_sends VALUES (?, ?)', (CH, ROOT))
            push()
            self.assertEqual(len(seen), 2)

    def burst(self, ts, root=None, conv='channel', channel=CH, now=1700000100.0):
        m = {'user': PERSON, 'ts': ts, 'text': 'x'}
        ctx = {'now': now, 'cfg': {'burst_seconds': 30, 'alert_dedupe_minutes': 30}}
        return self.engine.add_to_burst(m, channel, conv, root, 'human', ctx)

    def test_top_level_channel_messages_are_never_merged(self):
        # two @mentions 5 s apart from one person: two conversations, each answered in its own thread
        first, how1 = self.burst('1700000006.000100')
        second, how2 = self.burst('1700000011.000100')
        self.assertNotEqual(first, second)
        threads = [I._reply_thread(self.db.execute('SELECT * FROM intake_bursts WHERE burst_id=?', (b,)).fetchone())
                   for b in (first, second)]
        self.assertEqual(threads, ['1700000006.000100', '1700000011.000100'])
        # lines in one thread, and lines in one DM, still merge
        a, _ = self.burst('1700000012.000100', root=ROOT)
        b, _ = self.burst('1700000014.000100', root=ROOT)
        self.assertEqual(a, b)
        c, _ = self.burst('1700000015.000100', conv='im', channel='D0000DM0001')
        d, _ = self.burst('1700000017.000100', conv='im', channel='D0000DM0001')
        self.assertEqual(c, d)
        self.assertEqual(self.db.execute('SELECT count(*) FROM intake_bursts').fetchone()[0], 4)


class KeepOwnTitlesTest(unittest.TestCase):
    def test_an_existing_role_task_keeps_its_heading_and_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'desk-inbox').mkdir()
            (root / 'desk-inbox' / 'goal.md').write_text('# Ops desk (bot)\n\nold policy\n', encoding='utf-8')
            db = sqlite3.connect(':memory:')
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT)')
            db.execute("INSERT INTO tasks VALUES ('desk-inbox', 'Ops desk (bot)')")
            cfg = {'intake': {'inbox_task': 'desk-inbox', 'oncall_task': 'desk-oncall'}}
            rt = types.SimpleNamespace(worker_config=types.SimpleNamespace(load=lambda: (cfg, None)),
                                       registry=types.SimpleNamespace(root=lambda: root),
                                       transaction=lambda db_: db_)

            def bootstrap():  # the pack: the policy goal rewritten from its template, the oncall task created
                for task_id, title in (('desk-inbox', 'Default inbox'), ('desk-oncall', 'Default oncall')):
                    (root / task_id).mkdir(exist_ok=True)
                    (root / task_id / 'goal.md').write_text(f'# {title}\n\nnew policy\n', encoding='utf-8')
                    db.execute('INSERT OR REPLACE INTO tasks VALUES (?, ?)', (task_id, title))
                return {'ok': True}
            self.assertEqual(I.keep_own_titles(rt, db, bootstrap), {'ok': True})
            self.assertEqual((root / 'desk-inbox' / 'goal.md').read_text(encoding='utf-8'),
                             '# Ops desk (bot)\n\nnew policy\n')
            self.assertEqual((root / 'desk-oncall' / 'goal.md').read_text(encoding='utf-8'),
                             '# Default oncall\n\nnew policy\n')  # created now: the pack's own text
            self.assertEqual(dict(db.execute('SELECT task_id, title FROM tasks').fetchall()),
                             {'desk-inbox': 'Ops desk (bot)', 'desk-oncall': 'Default oncall'})


if __name__ == '__main__':
    unittest.main()
