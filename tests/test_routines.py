import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import types
import unittest
import zoneinfo

from kage.core import routines as r

TZ = zoneinfo.ZoneInfo('Asia/Shanghai')
OWNER = 'U0000TEST01'
BUTTONS = types.SimpleNamespace(COPY_FIRST_LINE_MAX=90, scrub_copy=lambda text: text)
T0 = 1793400000.0  # 2026-10-31 (a Saturday) 08:00 in Shanghai
YAML = '''# my routines
routines:
  - id: weekly-review
    schedule: "7 10 * * 1"
    prompt: "Review the open rules"
    scope: "this task only"
  - id: pulse
    every: 30m
    prompt: 'Check the queue'
    scope: queue
    enabled: false
'''


def routines(language='en'):
    return r.Routines(language, tz=TZ, owner_key='owner', owner_name='Mira', owner_user=lambda rt: OWNER,
                      ask_owner=lambda rt: rt.ask, buttons=BUTTONS,
                      run_id_env='TEST_RUN_ID')


class PureTest(unittest.TestCase):
    def test_parse_cron_and_human(self):
        doc = r.parse(YAML)
        self.assertEqual([e['id'] for e in doc['routines']], ['weekly-review', 'pulse'])
        with self.assertRaisesRegex(ValueError, 'line_2'):
            r.parse('routines:\n\t- id: x\n')
        R = routines()
        spec = r.parse_cron('7 10 * * 1')
        nxt = R.cron_next(spec, T0)
        self.assertEqual(R.iso(nxt), '2026-11-02T10:07:00+08:00')
        self.assertEqual(R.cron_prev(spec, nxt + 60), nxt)
        self.assertEqual(R.human({'schedule': '7 10 * * 1,3'}), 'weekly on Mon, Wed 10:07')
        self.assertEqual(routines('zh-CN').human({'schedule': '7 10 * * 1,3'}), '每周一、三 10:07')
        self.assertEqual(R.human({'every': '30m'}), 'every 30m')
        self.assertEqual(r.parse_every('30m'), 1800)
        with self.assertRaises(ValueError):
            r.parse_every('5m')                                   # under the 15-minute floor
        self.assertEqual(R.confirm_options, ['enable=Enable:approve', 'skip=Do not enable:reject'])
        self.assertIn("one of Mira's standing instructions", R.instruction)
        self.assertIn('owner_intent', R.instruction)
        self.assertIn('not_owner', R.confirm_reply)

    def test_cards(self):
        entry = {'id': 'weekly-review', 'schedule': '7 10 * * 1', 'prompt': 'Review the open rules', 'scope': 'this task'}
        self.assertEqual(routines('zh-CN').confirmation_card_text('local-task-x', entry).splitlines()[0],
                         'local-task-x 里新写了定时任务 weekly-review（每周一 10:07）：Review the open rules，问你要不要启用。')
        self.assertTrue(routines().proposal_card_text('task-a', entry).startswith('an agent wants to add a routine to task-a'))


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / 'tasks'
        (self.root / 'task-a').mkdir(parents=True)
        (self.root / 'task-a' / 'routines.yaml').write_text(YAML)
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('CREATE TABLE decisions (event_id TEXT, choice TEXT, actor TEXT, decided_at REAL, note TEXT);'
                              'CREATE TABLE escalation_cards (event_id TEXT, options TEXT);'
                              'CREATE TABLE tasks (task_id TEXT, current_provider TEXT, current_session TEXT);'
                              'CREATE TABLE events (id TEXT, status TEXT); CREATE TABLE dispatches (dispatch_id TEXT, state TEXT);')
        r.initialize(self.db)
        self.cards, self.replies, self.events = [], [], []

        @contextlib.contextmanager
        def transaction(db):
            db.execute('BEGIN IMMEDIATE')
            try:
                yield
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

        def card(db, text, options, **kw):
            self.cards.append((text, options, kw['kind']))
            return {'event_id': f'card{len(self.cards)}', 'warning': None}
        config = {'routines': {'enabled': True, 'scan_seconds': 60, 'max_per_task': 20, 'max_runs_per_day': 48,
                               'min_interval_minutes': 15}, 'dispatch': {'max_event_age_hours': 48}}
        root = self.root
        self.rt = types.SimpleNamespace(
            transaction=transaction, supervisor_card=card, safe_error=lambda e: type(e).__name__, DASHBOARD_URL='http://x',
            worker_config=types.SimpleNamespace(load=lambda: (config, None)),
            asks=types.SimpleNamespace(check_secret_free=lambda text: None),
            registry=types.SimpleNamespace(root=lambda: root, smoke_root=lambda: root / 'none', SMOKE_PREFIX='smoke-',
                                           task_id_for_cwd=lambda cwd: Path(cwd).name, is_smoke_task=lambda t: False,
                                           task_dir=lambda t: root / t, ensure_task=lambda db, t, now: None),
            buttons=types.SimpleNamespace(resolve_choice=lambda c, m: {'kind': 'approve' if c == 'enable' else 'reject'},
                                          card_model=lambda o: o),
            rules=types.SimpleNamespace(_card_reply=lambda rt, db, event, text, ref, kind=None: self.replies.append(text)),
            envelopes=types.SimpleNamespace(envelope=lambda *a: {'envelope': a}),
            enqueue_local_event=lambda db, task, path, provider, session, kind, file, ref, env, now: (
                self.events.append((task, session, env)) or ('ev1', None)),
            _meta_error=lambda db, key, exc: None, __file__=str(self.tmp / 'runtime.py'))
        self.R = routines()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_confirm_then_fire(self):
        sched = self.R.Scheduler(self.db, self.tmp / 'state.sqlite', self.rt, clock=lambda: T0)
        self.assertLimitsOk(sched.step())
        states = {row['routine_id']: row['state'] for row in self.db.execute('SELECT * FROM routine_state')}
        self.assertEqual(states, {'weekly-review': 'awaiting_confirmation', 'pulse': 'disabled'})
        self.assertEqual([c[2] for c in self.cards], ['routine_confirmation'])
        self.db.execute("INSERT INTO escalation_cards VALUES ('card1', '[]')")
        self.db.execute("INSERT INTO decisions VALUES ('card1', 'enable', 'U0000TEST05', ?, NULL)", (T0 + 1,))
        self.assertEqual(self.R.settle_confirmations(self.rt, self.db, now=T0 + 2)[0]['outcome'], 'not_owner')
        self.db.execute('UPDATE routine_confirmations SET card_settled_at=NULL, status=?', ('pending',))
        self.db.execute('UPDATE decisions SET actor=?', (OWNER,))
        self.assertEqual(self.R.settle_confirmations(self.rt, self.db, now=T0 + 3)[0]['outcome'], 'confirmed')
        self.db.execute("INSERT INTO tasks VALUES ('task-a', 'claude', 's1')")
        sched.scan(T0 + 4, self.rt.worker_config.load()[0]['routines'])
        due = self.db.execute("SELECT next_at FROM routine_state WHERE routine_id='weekly-review'").fetchone()[0]
        out = sched.fire_due(due + 1, self.rt.worker_config.load()[0]['routines'])
        self.assertEqual((out[0]['state'], out[0]['delivery']), ('fired', 'event'))
        self.assertEqual(self.events[0][2]['envelope'][2:4], ('owner', 'owner_intent'))
        text = next((self.root / 'task-a' / 'history' / 'routines').glob('*.md')).read_text()
        self.assertIn("- authority: Mira's standing instruction (owner_intent)", text)

    def assertLimitsOk(self, result):
        self.assertIn(result, ('ok', 'fired:0'))


if __name__ == '__main__':
    unittest.main()
