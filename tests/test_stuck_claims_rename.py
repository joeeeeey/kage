import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import types
import unittest

from kage.adapters.slack import stuck_claims as sc
from kage.core import task_rename as tr

OWNER = 'U0000TEST01'
T0 = 1793400000.0


def runtime(minutes=30, claimed=None, behind=1):
    @contextlib.contextmanager
    def transaction(db):
        db.execute('BEGIN IMMEDIATE')
        try:
            yield
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    cards, replies = [], []

    def supervisor_card(db, text, options, kind, footer, now):
        cards.append({'text': text, 'options': options, 'kind': kind, 'footer': footer})
        return {'event_id': f'card{len(cards)}', 'channel': 'C0000TEST01', 'thread_ts': f'{int(now)}.000100'}
    claude = types.SimpleNamespace(
        stuck_claims=lambda db, before: claimed or {},
        queued_behind=lambda db, session: behind,
        exact_claimed=lambda db, session, ids: True,
        requeue_claimed=lambda db, session, ids, reason, now: list(ids),
        settle_claimed=lambda db, session, ids, reason, now: list(ids[:1]))
    rt = types.SimpleNamespace(
        transaction=transaction, supervisor_card=supervisor_card, safe_error=lambda e: type(e).__name__,
        DASHBOARD_URL='http://127.0.0.1:9', claude=claude,
        worker_config=types.SimpleNamespace(load=lambda: ({'dispatch': {'stuck_claim_minutes': minutes}}, None)),
        registry=types.SimpleNamespace(task_id_for_cwd=lambda cwd: str(cwd).rsplit('/', 1)[-1] if cwd else None),
        rules=types.SimpleNamespace(_card_reply=lambda rt, db, event, text, ref, kind: replies.append(text)),
        buttons=types.SimpleNamespace(resolve_choice=lambda choice, model: {'id': choice}, card_model=lambda options: None))
    return rt, cards, replies


class StuckClaimsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE watches (id TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE consumer_runs (task TEXT, provider TEXT, session TEXT, started_at REAL);
          CREATE TABLE tasks (task_id TEXT, current_provider TEXT, current_session TEXT);
          CREATE TABLE stranded_deliveries (session TEXT, state TEXT);
          CREATE TABLE decisions (event_id TEXT, choice TEXT, actor TEXT, decided_at REAL);
          CREATE TABLE escalation_cards (event_id TEXT, options TEXT);
          CREATE TABLE escalations (event_id TEXT, thread_ts TEXT);
          CREATE TABLE claude_claims (event_id TEXT, released_at REAL);
          CREATE TABLE events (id TEXT, status TEXT);''')
        sc.initialize(self.db)
        self.db.execute('INSERT INTO watches VALUES (?,?)', ('w1', json.dumps(
            {'provider': 'claude', 'session': 's1', 'cwd': '/tasks/local-task-20261031-owner-inbox'})))
        self.zh = sc.StuckClaims('zh-CN', owner_user=OWNER, task_prefix=re.compile(r'^local-task-\d{8}-'))
        self.en = sc.StuckClaims('en', owner_user=OWNER, task_prefix=re.compile(r'^local-task-\d{8}-'))

    def test_wording_from_the_catalog(self):
        self.assertEqual(self.zh.options[0], 'requeue=重新投递给该会话:approve')
        self.assertEqual(self.en.options[2], 'later=Leave it for now:reject')
        self.assertEqual(self.zh._age(90000), '1 天')
        self.assertEqual(self.zh._age(30), '1 分钟')
        self.assertEqual(self.en._age(7300), '2 h')
        text = self.zh.card_text('local-task-20261031-owner-inbox', 2, 4000, 3)
        self.assertTrue(text.startswith('要你选：owner-inbox 会话领了 2 条消息，1 小时还没处理完，后面 3 条新消息被堵住了\n'))
        self.assertIn('a Claude session claimed 1 messages', self.en.card_text(None, 1, 60, 1))
        self.assertEqual(self.zh.reply['requeue'].format(n=2), '🔁 已把 2 条重新投递给该会话，它会重新领取。')

    def test_scan_cards_once_and_the_owner_settles(self):
        rt, cards, replies = runtime(claimed={'s1': [('e1', T0 - 4000), ('e2', T0 - 4000)]})
        out = self.zh.scan(rt, self.db, now=T0)
        self.assertEqual([o['action'] for o in out], ['carded'])
        self.assertEqual(cards[0]['footer'], '领取卡住 · 详情：<http://127.0.0.1:9/health>')
        self.assertEqual(cards[0]['options'], self.zh.options)
        self.assertEqual(self.zh.scan(rt, self.db, now=T0 + 60), [])          # one card per stuck batch
        self.db.execute("INSERT INTO escalation_cards VALUES ('card1', '[]')")
        self.db.execute("INSERT INTO decisions VALUES ('card1', 'settle', 'U0000TEST05', ?)", (T0,))
        self.assertEqual(self.zh.settle(rt, self.db, now=T0 + 70), [])        # someone else's click does nothing
        self.db.execute("UPDATE decisions SET actor=?", (OWNER,))
        done = self.zh.settle(rt, self.db, now=T0 + 80)
        self.assertEqual((done[0]['choice'], done[0]['changed'], done[0]['unchanged']), ('settle', ['e1'], ['e2']))
        self.assertEqual(replies, ['✅ 已把 1 条标记为已处理，不会再投递；排在后面的消息会继续投给该会话。'])
        self.assertEqual(sc.overview(self.db, rt.claude, 30, now=T0)['cards'][0]['state'], 'resolved')

    def test_off_and_behind_nothing(self):
        rt, cards, _ = runtime(minutes=0, claimed={'s1': [('e1', T0 - 4000)]})
        self.assertEqual(self.zh.scan(rt, self.db, now=T0), [])
        rt, cards, _ = runtime(claimed={'s1': [('e1', T0 - 4000)]}, behind=0)
        self.assertEqual((self.zh.scan(rt, self.db, now=T0), cards), ([], []))


class TaskRenameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / 'tasks'
        (self.root / 'local-task-20261019-inbox').mkdir(parents=True)
        self.db_path = self.tmp / 'state.sqlite'
        db = sqlite3.connect(self.db_path)
        db.executescript('CREATE TABLE tasks (task_id TEXT PRIMARY KEY, path TEXT);'
                         'CREATE TABLE watches (id TEXT PRIMARY KEY, data TEXT);')
        old = str(self.root / 'local-task-20261019-inbox')
        db.execute('INSERT INTO tasks VALUES (?,?)', ('local-task-20261019-inbox', old))
        db.execute('INSERT INTO watches VALUES (?,?)', ('w1', json.dumps({'cwd': old + '/sub', 'task_id': 'local-task-20261019-inbox',
                                                                          'note': 'local-task-20261019-inbox in free text'})))
        db.commit()
        db.close()
        config = {'intake': {'inbox_task': 'local-task-20261019-inbox'}, 'dispatch': {'long_lived_tasks': []}}
        self.rt = types.SimpleNamespace(
            registry=types.SimpleNamespace(root=lambda: self.root), pid_birth=lambda pid: None,
            connect=lambda path: sqlite3.connect(path, isolation_level=None),
            worker_config=types.SimpleNamespace(load=lambda: (config, None), config_path=lambda: self.tmp / 'config.json',
                                                set_many=lambda changes, who, dry_run=False: {'changes': [
                                                    {'key': k} for k, _ in changes]}))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_parse_maps(self):
        self.assertEqual(tr.parse_maps(['a=b', 'c=d']), [('a', 'b'), ('c', 'd')])
        for bad in (['a'], ['a=a'], [], ['a=b', 'b=c']):
            with self.assertRaises(tr.RenameError):
                tr.parse_maps(bad)

    def test_dry_run_then_apply_then_already_migrated(self):
        maps = ['local-task-20261019-inbox=inbox']
        dry = tr.run(self.rt, self.db_path, maps, now=T0)
        self.assertEqual((dry['ok'], dry['dry_run'], dry['total_changes']), (True, True, 3))
        self.assertTrue((self.root / 'local-task-20261019-inbox').is_dir())
        done = tr.run(self.rt, self.db_path, maps, apply=True, backup_dir=self.tmp / 'backups', now=T0)
        self.assertTrue(done['applied'])
        self.assertTrue((self.root / 'inbox').is_dir())
        self.assertEqual(os.readlink(self.root / 'local-task-20261019-inbox'), 'inbox')
        db = sqlite3.connect(self.db_path)
        self.assertEqual(db.execute('SELECT task_id, path FROM tasks').fetchone(), ('inbox', str(self.root / 'inbox')))
        data = json.loads(db.execute('SELECT data FROM watches').fetchone()[0])
        db.close()
        self.assertEqual((data['task_id'], data['cwd']), ('inbox', str(self.root / 'inbox' / 'sub')))
        self.assertIn('local-task-20261019-inbox', data['note'])                # free text is never rewritten
        self.assertTrue(tr.run(self.rt, self.db_path, maps, apply=True, backup_dir=self.tmp / 'backups',
                               now=T0 + 1)['already_migrated'])


if __name__ == '__main__':
    unittest.main()
