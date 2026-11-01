"""Phase 2 C1: the control entry (kage.core.control), its config section and the console's endpoints.

A small runtime stand-in (real worker config, envelopes, store) around an in-memory store; the end-to-end path with the
assembled runtime, the dispatcher and a fake provider is in the deployment's suite."""
import contextlib
import email.message
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import types
import unittest
from unittest import mock

from kage.core import control as C
from kage.core import envelope as E
from kage.core import secrets as S
from kage.core import worker_config as W
from kage.core.store import transaction

DEFAULTS = json.loads((Path(__file__).with_name('fixtures') / 'worker_defaults.json').read_text())
SESSION = '11111111-1111-4111-8111-111111111111'
RUN_SESSION = '22222222-2222-4222-8222-222222222222'
TARGET_SESSION = '33333333-3333-4333-8333-333333333333'


def wconfig(tmp):
    return W.WorkerConfig('en', defaults=DEFAULTS, config_env='KAGE_TEST_WORKER_CONFIG',
                          default_path=Path(tmp) / 'config.json', owner_key='owner', owner_name='Mira')


def write_config(tmp, **control):
    raw = json.loads(json.dumps(DEFAULTS))
    if control is not None:
        raw['control'] = control
    (Path(tmp) / 'config.json').write_text(json.dumps(raw))


class Runtime:
    """The runtime names kage.core.control uses, over a real sqlite store in a temp dir."""

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.__file__ = str(self.tmp / 'program.py')
        self.worker_config = wconfig(tmp)
        env = E.Envelopes(owner_key='owner', owner_user='U0000OWNER1')
        self.envelopes = types.SimpleNamespace(envelope=E.envelope, ENVELOPES=env)
        self.actor = ('claude', None)
        self.claude = types.SimpleNamespace(actor=lambda: self.actor, provider=lambda w: w.get('provider', 'codex'))
        self.started, self.launched, self.resolved = [], [], []
        self.registry = types.SimpleNamespace(
            root=lambda: self.tmp / 'tasks', task_id_for_cwd=self._task_for_cwd,
            task_consumer=self._consumer, running_session=self._running, ensure_task=self._ensure_task,
            read_title=lambda path: Path(path).name)
        self.intake = types.SimpleNamespace(run_bootstrap=self._bootstrap)
        self.OWNER, self.DASHBOARD_URL, self.cards = 'U0000OWNER1', 'http://127.0.0.1:9999', []
        from kage.core import decisions as Dec
        self.buttons = types.SimpleNamespace(resolve_choice=Dec.resolve_choice,
                                             card_model=lambda stored: Dec.card_model(stored, []))
        self.run_log = types.SimpleNamespace(read_state=lambda d: {'last_summary': 'working on it'})
        self.db = sqlite3.connect(str(self.tmp / 'state.sqlite'))
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT, state TEXT, path TEXT, current_provider TEXT,
                              current_session TEXT, updated_at REAL);
          CREATE TABLE watches (id TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE events (id TEXT PRIMARY KEY, watch_id TEXT, channel TEXT, ts TEXT, author TEXT, digest TEXT,
                               received REAL, status TEXT, receipt TEXT, disposition TEXT, action_ts TEXT);
          CREATE TABLE local_messages (event_id TEXT, kind TEXT, task_id TEXT, path TEXT, sender TEXT, created_at REAL);
          CREATE TABLE consumer_runs (run_id TEXT, task TEXT, provider TEXT, session TEXT, state TEXT, started_at REAL,
                                      log_dir TEXT, task_path TEXT);
          CREATE TABLE dispatches (dispatch_id TEXT, task_key TEXT, state TEXT, trigger TEXT, wait_reason TEXT,
                                   created_at REAL, updated_at REAL, run_id TEXT);
          CREATE TABLE escalations (event_id TEXT PRIMARY KEY, stage TEXT, thread_ts TEXT, watch_id TEXT, client_msg_id TEXT);
          CREATE TABLE escalation_cards (event_id TEXT PRIMARY KEY, channel TEXT, options TEXT, never_auto INTEGER,
                                         created REAL, task_id TEXT, dry INTEGER DEFAULT 0);
          CREATE TABLE decisions (event_id TEXT PRIMARY KEY, choice TEXT, actor TEXT, role TEXT, decided_at REAL,
                                  card_channel TEXT, card_ts TEXT, provenance TEXT, note TEXT);
        ''')
        E.initialize(self.db)
        self.db.commit()
        self.running = {}

    # -- tasks
    def add_task(self, task_id, session=None, provider='claude'):
        path = self.tmp / 'tasks' / task_id
        path.mkdir(parents=True, exist_ok=True)
        (path / 'status.md').write_text('<!-- rev: 1 -->\n# Status\nAll good.\n')
        with transaction(self.db):
            self.db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?)',
                            (task_id, task_id, 'active', str(path), provider, session, time.time()))
        return path

    def _task_for_cwd(self, cwd):
        try:
            rel = Path(cwd).resolve().relative_to((self.tmp / 'tasks').resolve())
        except (ValueError, TypeError):
            return None
        return rel.parts[0] if rel.parts else None

    def _consumer(self, db, task_id):
        if task_id in self.running:
            return (*self.running[task_id], 'running')
        row = db.execute('SELECT current_provider, current_session FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        return (row[0], row[1], 'current') if row and row[1] else None

    def _running(self, db, task_id):
        return self.running.get(task_id)

    def _ensure_task(self, db, task_id, now):
        db.execute('INSERT OR IGNORE INTO tasks (task_id, state, path, updated_at) VALUES (?,?,?,?)',
                   (task_id, 'active', str(self.tmp / 'tasks' / task_id), now))

    def _bootstrap(self, title, brief, context, *, linear, agent, root, folder_name=None, **kw):
        name = folder_name or 'new-' + hashlib.sha256(title.encode()).hexdigest()[:6]
        (Path(root) / name).mkdir(parents=True, exist_ok=True)
        return {'workspace': str(Path(root) / name)}

    # -- what control calls on the runtime
    def _resolve_task_arg(self, db, task):
        row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone()
        if row is None:
            raise ValueError('unknown_task')
        return dict(row)

    def enqueue_local_event(self, db, task_id, task_path, provider, session, kind, path, sender, env, now, hold=None):
        wid = hashlib.sha256(f'inbox/{task_id}/{provider}/{session}'.encode()).hexdigest()[:20]
        with transaction(db):
            db.execute('INSERT OR IGNORE INTO watches VALUES (?,?)', (wid, json.dumps(
                {'id': wid, 'task_id': task_id, 'provider': provider, 'session': session, 'state': 'inbox',
                 'cwd': str(task_path)})))
            eid = hashlib.sha256(f'{wid}/{now}/{path}'.encode()).hexdigest()[:24]
            db.execute("INSERT INTO events (id,watch_id,channel,ts,author,digest,received,status) VALUES "
                       "(?,?,'LOCAL',?,?,?,?,'pending')", (eid, wid, f'{now:.6f}', sender, 'd', now))
            E.store(db, eid, env, now)
            db.execute('INSERT INTO local_messages VALUES (?,?,?,?,?,?)', (eid, kind, task_id, str(path), sender, now))
        return eid, wid

    def load(self, db, wid):
        return json.loads(db.execute('SELECT data FROM watches WHERE id=?', (wid,)).fetchone()[0])

    def dispatch_consumer(self, db, e):
        if e['status'] != 'dispatched':
            return None
        w = self.load(db, e['watch_id'])
        run = self.running.get(w['task_id'])
        return {'provider': run[0], 'session': run[1], 'task_path': w['cwd']} if run else None

    def by_dispatch(self, db, e):
        c = self.dispatch_consumer(db, e)
        return bool(c and (c['provider'], c['session']) == self.actor)

    def resolve(self, db, eid, disposition, now=None):
        self.resolved.append(eid)
        with transaction(db):
            db.execute("UPDATE events SET status='handled', disposition=? WHERE id=?", (disposition, eid))

    def launch_task(self, db, db_path, path, provider, prompt, **kw):
        tid = Path(path).name
        self.launched.append((tid, provider, prompt))
        if db.execute('SELECT 1 FROM tasks WHERE task_id=?', (tid,)).fetchone() is None:
            self.add_task(tid)
        return {'task_id': tid, 'dispatch_id': 'd-' + tid}

    def ensure_inbox_watch(self, db, task_id, path, provider, session, now):
        wid = hashlib.sha256(f'inbox/{task_id}/{provider}/{session}'.encode()).hexdigest()[:20]
        db.execute('INSERT OR IGNORE INTO watches VALUES (?,?)', (wid, json.dumps(
            {'id': wid, 'task_id': task_id, 'provider': provider, 'session': session, 'state': 'inbox', 'cwd': str(path)})))
        return {'id': wid}

    def start(self, db_path):
        self.started.append(db_path)

    def supervisor_card(self, db, question, options, *, never_auto=True, kind=None, refs=None, now=None, task_id=None,
                        first_line_max=None, **kw):
        """The code-owned card path, recorded: options as escalation_cards stores them."""
        from kage.core import decisions as Dec
        eid = hashlib.sha256(f'card/{len(self.cards)}/{question}'.encode()).hexdigest()[:24]
        model = [dict(zip(('id', 'label', 'kind'), Dec.parse_option(o)), recommended=False) for o in options]
        with transaction(db):
            db.execute('INSERT INTO escalations VALUES (?,?,?,?,?)', (eid, 'supervisor', f'{3000 + len(self.cards)}.000001',
                                                                     None, 'c'))
            db.execute('INSERT INTO escalation_cards (event_id,channel,options,never_auto,created,task_id) VALUES '
                       '(?,?,?,?,?,?)', (eid, 'C0TESTCARDS', json.dumps(model), int(never_auto), now, task_id))
        self.cards.append({'event_id': eid, 'question': question, 'options': options, 'never_auto': never_auto,
                           'kind': kind, 'task_id': task_id})
        return {'event_id': eid, 'thread_ts': f'{3000 + len(self.cards)}.000001', 'channel': 'C0TESTCARDS'}

    def click(self, eid, option='confirm', provenance='slack_button', role='approver', actor=None, at=None):
        """What `decide` (a click) or a typed thread decision records."""
        with transaction(self.db):
            self.db.execute('INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)', (eid, f'opt:{option}', actor or self.OWNER,
                                                                             role, at or time.time(), 'C0TESTCARDS', '1.1',
                                                                             provenance, None))


class ControlConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wc = wconfig(self.tmp)

    def test_a_config_without_the_section_reads_and_validates_as_before(self):
        raw = json.loads(json.dumps(DEFAULTS))
        (Path(self.tmp) / 'config.json').write_text(json.dumps(raw))
        config, _ = self.wc.load()
        self.assertNotIn('control', config)
        self.assertIsNone(self.wc.intake_role(config, 'owner-control'))
        self.assertNotIn('owner-control', self.wc.long_lived_ids(config))
        self.assertEqual(W.control_settings(config, 'owner')['task'], 'owner-control')
        self.assertFalse(W.control_settings(config, 'owner')['enabled'])

    def test_the_section_is_validated_when_present(self):
        for bad in ({'enabled': 'yes'}, {'task': 'bad id!'}, {'provider': 'gemini'}, {'effort': 'huge'},
                    {'retention_days': 0}, {'unknown': 1}, {'model': 'bad model'}):
            write_config(self.tmp, **bad)
            with self.assertRaises(ValueError, msg=bad):
                self.wc.load()
        write_config(self.tmp, enabled=True, task='mira-control', provider='codex', model='gpt-x', effort='high')
        config, _ = self.wc.load()
        self.assertEqual(self.wc.intake_role(config, 'mira-control'), 'control')
        self.assertIn('mira-control', self.wc.long_lived_ids(config))
        self.assertEqual(self.wc.preferred_provider(config, 'mira-control', None), 'codex')
        self.assertEqual(self.wc.model_for_task(config, 'codex', task_id='mira-control')[:2], ('gpt-x', 'high'))
        self.assertEqual(self.wc.model_for_task(config, 'claude', task_id='mira-control')[:2],
                         ('claude-sonnet-5-5', 'xhigh'))  # a fallback provider keeps its own defaults
        self.assertEqual(self.wc.lean_profile(config, 'mira-control')['class'], 'control')

    def test_off_is_no_role_and_the_intake_roles_are_unchanged(self):
        write_config(self.tmp, enabled=False)
        config, _ = self.wc.load()
        self.assertIsNone(self.wc.intake_role(config, 'owner-control'))
        self.assertEqual(self.wc.intake_role(config, 'example-inbox'), 'inbox')
        self.assertEqual(self.wc.intake_role({'intake': {}}, 'example-oncall'), 'oncall')

    def test_set_value_opens_the_section(self):
        (Path(self.tmp) / 'config.json').write_text(json.dumps(DEFAULTS))
        result = self.wc.set_value('control.enabled', 'true', 'test')
        self.assertTrue(result['ok'])
        config, _ = self.wc.load()
        self.assertEqual(config['control'], {'enabled': True})
        with self.assertRaises(ValueError):
            self.wc.set_value('control.nope', '1', 'test')
        self.wc.set_many([('control.model', 'claude-opus-5-5'), ('control.effort', 'high')], 'test')
        self.assertEqual(self.wc.load()[0]['control'], {'enabled': True, 'model': 'claude-opus-5-5', 'effort': 'high'})


class ControlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        write_config(self.tmp, enabled=True, messages_per_hour=5, relays_per_hour=3, launches_per_day=1)
        self.rt = Runtime(self.tmp)
        self.addCleanup(self.rt.db.close)
        self.db = self.rt.db
        self.config = self.rt.worker_config.load()[0]
        self.c = C.Control('en', owner_key='owner', owner_name='Mira')
        self.facts = C.console_facts({'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors',
                                      'Sec-Fetch-Dest': 'empty', 'User-Agent': 'Mozilla/5.0 Chrome/140 Safari/537'})

    T = 1800000000.0  # one fixed instant for every call that compares stored times (clock-proof)

    def submit(self, text='How is the release going?', client='client-0001', **kw):
        kw.setdefault('now', self.T)
        return self.c.submit(self.db, self.config, text, client_id=client, facts=self.facts, **kw)

    def bootstrap_control(self):
        path = self.rt.add_task('owner-control', session=SESSION)
        return path

    def pump_and_hold(self):
        """pump, then make the control run the holder of the events (dispatched to the running run)."""
        self.c.pump(self.rt, self.db)
        self.rt.running['owner-control'] = ('claude', RUN_SESSION)
        with transaction(self.db):
            self.db.execute("UPDATE events SET status='dispatched'")
            for wid, data in self.db.execute('SELECT id, data FROM watches').fetchall():  # the dispatcher binds the run
                self.db.execute('UPDATE watches SET data=? WHERE id=?', (json.dumps({**json.loads(data),
                                                                                     'session': RUN_SESSION}), wid))
        self.rt.actor = ('claude', RUN_SESSION)

    # ---- sources
    def test_off_refuses_and_stores_nothing(self):
        write_config(self.tmp, enabled=False)
        with self.assertRaises(ValueError) as caught:
            self.c.submit(self.db, self.rt.worker_config.load()[0], 'hi', client_id='client-0001')
        self.assertEqual(str(caught.exception), 'control_off')
        self.assertFalse(C.present(self.db))

    def test_a_message_is_stored_with_its_facts_and_a_retry_is_idempotent(self):
        m = self.submit()
        self.assertEqual(m['state'], 'received')
        self.assertEqual(m['facts']['ua'], 'chrome')
        self.assertTrue(m['facts']['browser_like'])
        again = self.submit()
        self.assertEqual(again['id'], m['id'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_messages').fetchone()[0], 1)
        follow = self.submit('And the other one?', client='client-0002', conversation=m['conversation'])
        self.assertEqual(follow['conversation'], m['conversation'])

    def test_a_script_request_is_recorded_as_one(self):
        facts = C.console_facts({'User-Agent': 'curl/8.4'})
        self.assertEqual((facts['ua'], facts['browser_like']), ('curl', False))
        m = self.c.submit(self.db, self.config, 'stop everything', client_id='client-curl1', facts=facts, now=self.T)
        self.assertIn('possibly a script', self.c._facts_line(self.c._row(self.db, m['id'])))

    def test_refusals(self):
        for text, client, code in (('', 'client-0001', 'text_empty'), ('x' * (C.MAX_TEXT + 1), 'client-0001', 'text_too_long'),
                                   ('hi', 'short', 'client_id_invalid')):
            with self.assertRaises(ValueError) as caught:
                self.submit(text, client)
            self.assertEqual(str(caught.exception), code)
        with self.assertRaises(ValueError) as caught:
            self.submit('the key is ghp_' + 'a' * 30, 'client-0003')
        self.assertEqual(str(caught.exception), S.ERROR)
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_messages').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.submit(conversation='cv0000000000000a')
        for i in range(5):
            self.submit(f'q{i}', f'client-r{i:04d}')
        with self.assertRaises(ValueError) as caught:
            self.submit('one more', 'client-r9999')
        self.assertEqual(str(caught.exception), 'rate_limited')

    # ---- pump
    def test_pump_waits_for_the_control_task(self):
        self.submit()
        self.assertEqual(self.c.pump(self.rt, self.db), {'queued': 0, 'waiting': 'control_task_not_bootstrapped'})
        self.assertEqual(self.c.overview(self.db, self.config)['waiting'], 'control_task_not_bootstrapped')

    def test_pump_makes_one_owner_intent_event_with_the_body_file(self):
        path = self.bootstrap_control()
        first = self.submit('Earlier question')
        self.c.reply  # noqa: B018 - the history below is the earlier turn
        m = self.submit('How is the release going?', 'client-0002', conversation=first['conversation'])
        self.assertEqual(self.c.pump(self.rt, self.db), {'queued': 2})
        rows = self.db.execute("SELECT * FROM control_messages WHERE role='owner' ORDER BY created_at").fetchall()
        self.assertEqual({r['state'] for r in rows}, {'queued'})
        env = E.lookup(self.db, rows[1]['event_id'])
        self.assertEqual((env['kind'], env['author_kind'], env['trust'], env['authority']),
                         ('control_message', 'human', 'owner', 'owner_intent'))
        local = self.db.execute('SELECT * FROM local_messages WHERE event_id=?', (rows[1]['event_id'],)).fetchone()
        self.assertEqual(local['kind'], 'control')
        body = Path(local['path']).read_text()
        self.assertTrue(Path(local['path']).is_relative_to(path / 'history' / 'control'))
        self.assertIn('How is the release going?', body)
        self.assertIn('Earlier question', body)  # the conversation's earlier turn
        self.assertIn('Request facts: console · chrome · browser request', body)
        self.assertIn(f'control-reply --message {m["id"]}', body)
        self.assertTrue(self.rt.started)
        self.assertEqual(oct(os.stat(local['path']).st_mode & 0o777), '0o600')

    # ---- the run's commands
    def test_only_the_control_run_holding_the_message_may_act(self):
        self.bootstrap_control()
        m = self.submit()
        self.c.pump(self.rt, self.db)
        for actor in (('claude', None), ('claude', TARGET_SESSION)):
            self.rt.actor = actor
            with self.assertRaises(ValueError) as caught:
                self.c.reply(self.rt, self.db, 'db', m['id'], now=self.T + 1, text='answer')
            self.assertEqual(str(caught.exception), 'only_the_control_run_holding_the_message_may_do_this')
        self.pump_and_hold()
        with self.assertRaises(ValueError):
            self.c.reply(self.rt, self.db, 'db', 'cm0000000000000f', 'answer')
        # a session in another task's directory is refused even when it is the holder
        with mock.patch('os.getcwd', return_value=str(self.rt.add_task('other-task'))):
            with self.assertRaises(ValueError):
                self.c.reply(self.rt, self.db, 'db', m['id'], now=self.T + 1, text='answer')

    def test_reply_answers_once_resolves_the_event_and_allows_followups(self):
        self.bootstrap_control()
        m = self.submit()
        self.pump_and_hold()
        out = self.c.reply(self.rt, self.db, 'db', m['id'], now=self.T + 1, text='It ships tomorrow.', needs_answer=False)
        self.assertTrue(out['ok'])
        row = self.c._row(self.db, m['id'])
        self.assertEqual(row['state'], 'answered')
        self.assertEqual(self.rt.resolved, [row['event_id']])
        with self.assertRaises(ValueError) as caught:
            self.c.reply(self.rt, self.db, 'db', m['id'], now=self.T + 1, text='again')
        self.assertEqual(str(caught.exception), 'already_answered_use_--followup')
        for i in range(C.MAX_FOLLOWUPS):
            self.c.reply(self.rt, self.db, 'db', m['id'], now=self.T + 1, text=f'update {i}', followup=True)
        with self.assertRaises(ValueError):
            self.c.reply(self.rt, self.db, 'db', m['id'], now=self.T + 1, text='one too many', followup=True)
        view = self.c.overview(self.db, self.config, m['conversation'])
        self.assertEqual([x['role'] for x in view['messages']], ['owner', 'control', 'control', 'control', 'control'])
        self.rt.actor = ('claude', TARGET_SESSION)  # a later control run bound to the same watch
        with transaction(self.db):
            for wid, data in self.db.execute('SELECT id, data FROM watches').fetchall():
                self.db.execute('UPDATE watches SET data=? WHERE id=?', (json.dumps({**json.loads(data),
                                                                                     'session': TARGET_SESSION}), wid))
        with self.assertRaises(ValueError) as caught:
            self.c.reply(self.rt, self.db, 'db', m['id'], now=self.T + 1, text='not mine', followup=True)
        self.assertEqual(str(caught.exception), 'only_the_control_run_holding_the_message_may_do_this')
        self.rt.actor = ('claude', RUN_SESSION)
        with self.assertRaises(ValueError):
            self.c.reply(self.rt, self.db, 'db', m['id'], now=self.T + 1, text='the token is xoxb-1234567890-abcdef', followup=True)

    def propose_and_confirm(self, m, task, note=None, **click):
        out = self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task=task, note=note)
        self.assertEqual(out['state'], 'proposed')
        self.rt.click(out['card_event'], **click)
        settled = self.c.settle(self.rt, self.db, now=self.T + 60)
        return out, settled

    def test_a_console_relay_waits_for_the_owners_click_then_carries_the_stored_words(self):
        self.bootstrap_control()
        target = self.rt.add_task('release-task', session=TARGET_SESSION)
        m = self.submit('Hold the release until I review it tomorrow.')
        self.pump_and_hold()
        out = self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='release-task', note='the release is this task')
        self.assertEqual((out['state'], out['mode'], out['task_id']), ('proposed', 'event', 'release-task'))
        self.assertIn('click', out['next'])
        card = self.rt.cards[-1]
        self.assertEqual((card['kind'], card['never_auto'], card['task_id']), ('control_proposal', True, 'owner-control'))
        self.assertIn('release-task', card['question'])
        self.assertIn('Hold the release until I review it tomorrow.', card['question'])
        self.assertIn(f"/control?conversation={m['conversation']}", card['question'])
        self.assertTrue(C.button_only(self.db, out['card_event']))
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_relays').fetchone()[0], 0)  # nothing ran yet
        self.assertEqual(self.c.settle(self.rt, self.db, now=self.T + 60), [])  # no click: still waiting
        self.rt.click(out['card_event'])
        settled = self.c.settle(self.rt, self.db, now=self.T + 60)
        self.assertEqual(settled[0]['state'], 'confirmed')
        relay = self.db.execute('SELECT * FROM control_relays').fetchone()
        env = E.lookup(self.db, relay['event_id'])
        self.assertEqual((env['kind'], env['trust'], env['authority'], env['rule']),
                         ('owner_relay', 'owner', 'owner_intent', 'control_relay'))
        body = Path(self.db.execute('SELECT path FROM local_messages WHERE event_id=?', (relay['event_id'],)).fetchone()[0])
        self.assertTrue(body.is_relative_to(target / 'history' / 'messages'))
        text = body.read_text()
        self.assertIn('Hold the release until I review it tomorrow.', text)
        self.assertIn('source: console (console · chrome · browser request', text)
        self.assertIn("Control task's note (no authority): the release is this task", text)
        view = self.c.overview(self.db, self.config, m['conversation'])
        self.assertEqual(view['messages'][0]['relays'][0]['task_id'], 'release-task')
        self.assertEqual(view['messages'][0]['proposals'][0]['state'], 'confirmed')
        self.assertTrue(view['messages'][-1]['runtime'])  # the runtime's note under the message
        # a second click / settle does nothing
        self.assertEqual(self.c.settle(self.rt, self.db, now=self.T + 120), [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_relays').fetchone()[0], 1)
        with self.assertRaises(ValueError) as caught:
            self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='release-task')
        self.assertEqual(str(caught.exception), 'already_relayed_to_this_task')
        with self.assertRaises(ValueError):
            self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='owner-control')
        with self.assertRaises(ValueError):
            self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='other', note='two\nlines' + 'x' * 400)

    def test_only_the_owners_own_button_click_runs_it(self):
        write_config(self.tmp, enabled=True, relays_per_hour=10)
        self.bootstrap_control()
        for name in ('a-task', 'b-task', 'c-task', 'd-task'):
            self.rt.add_task(name, session=TARGET_SESSION)
        m = self.submit('Pause.')
        self.pump_and_hold()
        for task, click, state in (('a-task', {'provenance': 'thread_reply'}, 'refused'),
                                   ('b-task', {'role': 'ooo_delegate'}, 'refused'),
                                   ('c-task', {'option': 'cancel'}, 'cancelled')):
            out, settled = self.propose_and_confirm(m, task, **click)
            self.assertEqual(settled[0]['state'], state, task)
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_relays').fetchone()[0], 0)
        out = self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='d-task')
        self.assertEqual(self.c.settle(self.rt, self.db, now=self.T + C.PROPOSAL_TTL - 1), [])
        self.assertEqual(self.c.settle(self.rt, self.db, now=self.T + C.PROPOSAL_TTL + 1)[0]['state'], 'expired')
        self.rt.click(out['card_event'])  # a click after the expiry runs nothing
        self.assertEqual(self.c.settle(self.rt, self.db, now=self.T + C.PROPOSAL_TTL + 60), [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_relays').fetchone()[0], 0)
        states = [x['state'] for x in self.c.overview(self.db, self.config, m['conversation'])['messages'][0]['proposals']]
        self.assertEqual(states, ['refused', 'refused', 'cancelled', 'expired'])

    def test_the_actor_must_be_the_owner_and_no_owner_id_refuses(self):
        write_config(self.tmp, enabled=True, relays_per_hour=10)
        self.bootstrap_control()
        for name in ('a-task', 'b-task'):
            self.rt.add_task(name, session=TARGET_SESSION)
        m = self.submit('Pause.')
        self.pump_and_hold()
        out, settled = self.propose_and_confirm(m, 'a-task', actor='U0000OTHER1')  # an approver that is not the owner
        self.assertEqual(settled[0]['state'], 'refused')
        self.rt.DEPLOYMENT = types.SimpleNamespace(owner=types.SimpleNamespace(slack_user='U0000OWNER1'))
        del self.rt.OWNER  # the profile is the source; the hub's name is not needed
        out = self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='b-task')
        self.rt.click(out['card_event'], actor='U0000OWNER1')
        self.rt.DEPLOYMENT = types.SimpleNamespace(owner=types.SimpleNamespace(slack_user=''))  # no owner id at all
        self.assertEqual(self.c.settle(self.rt, self.db, now=self.T + 60)[0]['state'], 'refused')
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_relays').fetchone()[0], 0)

    def test_no_console_address_means_no_link_on_the_card(self):
        self.bootstrap_control()
        self.rt.add_task('a-task', session=TARGET_SESSION)
        m = self.submit('Pause.')
        self.pump_and_hold()
        self.rt.add_task('b-task', session=TARGET_SESSION)
        for task, base in (('a-task', ''), ('b-task', None)):  # an empty address, then none at all
            if base is None:
                del self.rt.DASHBOARD_URL
            else:
                self.rt.DASHBOARD_URL = base
            self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task=task)
            self.assertNotIn('/control?', self.rt.cards[-1]['question'])
            self.assertIn("console's control page", self.rt.cards[-1]['question'])

    def test_a_proposal_settles_once_even_when_a_second_supervisor_settles_meanwhile(self):
        self.bootstrap_control()
        self.rt.add_task('release-task', session=TARGET_SESSION)
        m = self.submit('Hold the release.')
        self.pump_and_hold()
        out = self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='release-task')
        self.rt.click(out['card_event'])
        nested = []
        c, rt, db = self.c, self.rt, self.db

        class Racing:  # another supervisor settles between this one's read of the click and its claim
            def __init__(self):
                self.fired = False

            def execute(self, sql, *args):
                if 'FROM decisions d' in sql and not self.fired:
                    self.fired = True
                    nested.append(c.settle(rt, db, now=1800000061.0))
                return db.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(db, name)
        first = self.c.settle(self.rt, Racing(), now=self.T + 60)
        self.assertEqual([x['state'] for x in nested[0]], ['confirmed'])
        self.assertEqual(first, [])  # the claim was taken: this one runs nothing
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_relays').fetchone()[0], 1)

    def test_the_decision_recorder_refuses_a_typed_reply_on_a_confirm_card(self):
        from kage.core import escalation as Esc
        self.bootstrap_control()
        self.rt.add_task('release-task', session=TARGET_SESSION)
        m = self.submit('Hold the release.')
        self.pump_and_hold()
        out = self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='release-task')
        me = types.SimpleNamespace(rt=types.SimpleNamespace(_fail=lambda reason: {'ok': False, 'reason': reason},
                                                            SUPERVISOR_STAGE='supervisor'))
        result = Esc.EscalationCore.thread_reply_decision(me, self.db, out['card_event'], '9999.000001')
        self.assertEqual(result, {'ok': False, 'reason': 'only_a_button_click_decides_this_card'})

    def test_an_altered_or_expired_stored_text_fails_the_proposal(self):
        self.bootstrap_control()
        self.rt.add_task('release-task', session=TARGET_SESSION)
        m = self.submit('Hold the release.')
        self.pump_and_hold()
        out = self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='release-task')
        with transaction(self.db):
            self.db.execute("UPDATE control_messages SET text='Delete the release branch.' WHERE message_id=?", (m['id'],))
        self.rt.click(out['card_event'])
        settled = self.c.settle(self.rt, self.db, now=self.T + 60)
        self.assertEqual((settled[0]['state'], settled[0]['error']), ('failed', 'stored_text_does_not_match_its_digest'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_relays').fetchone()[0], 0)
        notes = [x['text'] for x in self.c.overview(self.db, self.config, m['conversation'])['messages'] if x.get('runtime')]
        self.assertIn('stored_text_does_not_match_its_digest', notes[-1])
        with self.assertRaises(ValueError) as caught:  # and the run cannot propose it again
            self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='release-task')
        self.assertEqual(str(caught.exception), 'stored_text_does_not_match_its_digest')

    def test_relay_to_a_task_without_a_session_dispatches_it_and_proposals_are_limited(self):
        self.bootstrap_control()
        for name in ('a-task', 'b-task', 'c-task', 'd-task'):
            self.rt.add_task(name)
        m = self.submit('Everyone: pause.')
        self.pump_and_hold()
        out, settled = self.propose_and_confirm(m, 'a-task')
        self.assertEqual(out['mode'], 'dispatch')
        self.assertIn('no session now', self.rt.cards[-1]['question'])
        self.assertEqual(self.rt.launched[0][0], 'a-task')
        self.assertIn('Everyone: pause.', self.rt.launched[0][2])
        self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='b-task')
        self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='c-task')
        with self.assertRaises(ValueError) as caught:  # relays_per_hour 3: done + still asking
            self.c.relay(self.rt, self.db, 'db', m['id'], now=self.T, task='d-task')
        self.assertEqual(str(caught.exception), 'relay_rate_limited')

    def test_launch_is_a_proposal_and_a_ticket_only_when_asked(self):
        self.bootstrap_control()
        m = self.submit('Start a task to compare the two vendors, and open a ticket for it.')
        self.pump_and_hold()
        out = self.c.launch(self.rt, self.db, 'db', m['id'], now=self.T, title='Compare the two vendors', linear=True)
        self.assertEqual((out['state'], out['kind'], out['linear']), ('proposed', 'launch', True))
        self.assertIn('open a ticket', self.rt.cards[-1]['question'])
        self.assertEqual(self.rt.launched, [])
        made = []
        self.rt.intake.run_bootstrap = lambda title, brief, context, **kw: (made.append(kw), self.rt._bootstrap(
            title, brief, context, **kw))[1]
        self.rt.click(out['card_event'])
        self.assertEqual(self.c.settle(self.rt, self.db, now=self.T + 60)[0]['state'], 'confirmed')
        self.assertEqual(made[0]['linear'], True)
        self.assertIn('Start a task to compare the two vendors', self.rt.launched[-1][2])
        with self.assertRaises(ValueError) as caught:
            self.c.launch(self.rt, self.db, 'db', m['id'], now=self.T, title='Again')
        self.assertEqual(str(caught.exception), 'launch_rate_limited')
        write_config(self.tmp, enabled=True, allow_launch=False)
        with self.assertRaises(ValueError) as caught:
            self.c.launch(self.rt, self.db, 'db', m['id'], now=self.T, title='Again')
        self.assertEqual(str(caught.exception), 'control_launch_off')

    # ---- sweep
    def test_sweep_states_timeout_retry_and_retention(self):
        T = self.T
        self.bootstrap_control()
        m = self.submit()
        self.c.pump(self.rt, self.db, now=T)
        self.rt.running['owner-control'] = ('claude', RUN_SESSION)
        with transaction(self.db):
            self.db.execute("UPDATE events SET status='dispatched'")
        self.db.execute("INSERT INTO consumer_runs VALUES ('r1','owner-control','claude',?,'running',?,NULL,NULL)",
                        (RUN_SESSION, T))
        self.db.commit()
        self.c.sweep(self.rt, self.db, now=T + 5)
        row = self.c._row(self.db, m['id'])
        self.assertEqual((row['state'], row['run_id']), ('working', 'r1'))
        # the run ended without an answer
        self.rt.running.clear()
        with transaction(self.db):
            self.db.execute("UPDATE events SET status='handled'")
        self.c.sweep(self.rt, self.db, now=T + 10)
        row = self.c._row(self.db, m['id'])
        self.assertEqual((row['state'], row['error']), ('failed', 'no_reply'))
        again = self.c.retry(self.db, self.config, m['id'], now=T + 20)
        self.assertEqual((again['state'], again['error']), ('received', None))
        self.c.pump(self.rt, self.db, now=T + 20)
        self.c.sweep(self.rt, self.db, now=T + 20 + 14 * 60)  # queued 14 minutes: still waiting
        self.assertEqual(self.c._row(self.db, m['id'])['state'], 'queued')
        self.c.sweep(self.rt, self.db, now=T + 20 + 16 * 60)
        self.assertEqual(self.c._row(self.db, m['id'])['error'], 'timeout')
        self.c.sweep(self.rt, self.db, now=T + 29 * 86400)
        self.assertIsNotNone(self.c._row(self.db, m['id'])['text'])  # inside control.retention_days
        self.c.sweep(self.rt, self.db, now=T + 31 * 86400)
        row = self.c._row(self.db, m['id'])
        self.assertIsNone(row['text'])
        self.assertTrue(self.c.public(self.db, row)['expired'])
        with self.assertRaises(ValueError):
            self.c.retry(self.db, self.config, m['id'], now=T + 31 * 86400)

    def test_context_digest_has_no_message_bodies(self):
        T = self.T
        self.rt.add_task('release-task', session=TARGET_SESSION)
        with transaction(self.db):
            self.db.execute('UPDATE tasks SET updated_at=?', (T - 3600,))
            self.db.execute("INSERT INTO consumer_runs VALUES ('r9','release-task','codex',?,'running',?,'/x',NULL)",
                            (TARGET_SESSION, T - 60))
        out = self.c.context(self.rt, self.db, now=T)
        self.assertEqual(out['tasks'][0]['task_id'], 'release-task')
        self.assertIn('All good.', out['tasks'][0]['status_head'])
        self.assertEqual(out['running_runs'][0]['last_summary'], 'working on it')
        self.assertEqual(out['open_owner_cards'], [])
        self.assertEqual(out['open_blockers'], {'error': 'OperationalError'})  # a missing table hides nothing else
        self.assertEqual(self.c.context(self.rt, self.db, now=T + 2 * 86400)['tasks'], [])  # outside the 24 h

    def test_bootstrap_creates_registers_and_binds(self):
        def launch(db, db_path, path, provider, prompt, **kw):
            with transaction(db):
                db.execute('UPDATE tasks SET current_session=?, current_provider=? WHERE task_id=?',
                           (SESSION, provider, Path(path).name))
            return {'dispatch_id': 'd1', 'state': 'running'}
        self.rt.launch_task = launch
        out = self.c.bootstrap(self.rt, self.db, 'db')
        self.assertTrue(out['created'] and out['goal_written'])
        goal = (Path(self.tmp) / 'tasks' / 'owner-control' / 'goal.md').read_text()
        self.assertIn('control-relay', goal)
        self.assertIn('Only Mira', goal)
        self.assertEqual(out['current'], {'provider': 'claude', 'session': SESSION})
        again = self.c.bootstrap(self.rt, self.db, 'db')
        self.assertFalse(again['created'])
        self.assertEqual(self.c.status(self.rt, self.db)['bootstrapped'], True)


class QuietControlRunCardsTest(unittest.TestCase):
    """A control run's card statuses post nothing except a failure / question / pause / wait; any other task's cards
    are untouched (the dispatcher's _card_status, with the rest of its body stubbed)."""

    def card_status(self, task, status, control_on=True):
        from kage.core import dispatch as D
        posted = []
        me = types.SimpleNamespace(
            _cfg_value=lambda key, default: True, _dispatch=lambda did: {'task_id': task, 'task_key': task},
            config=lambda: {'control': {'enabled': control_on}, 'intake': {}},
            rt=types.SimpleNamespace(worker_config=wconfig(tempfile.mkdtemp())),
            _card_text=lambda d: posted.append(status) or (_ for _ in ()).throw(RuntimeError('posted')))
        me._control_task = lambda d: D.Dispatcher._control_task(me, d)
        try:
            D.Dispatcher._card_status(me, 'd1', status)
        except RuntimeError:
            pass
        return posted

    def test_quiet_statuses(self):
        for status in ('running', 'success', 'retrying', 'fallback', 'resumed', 'handed_back'):
            self.assertEqual(self.card_status('owner-control', status), [], status)
            self.assertEqual(self.card_status('owner-control', status, control_on=False), [status], status)
            self.assertEqual(self.card_status('some-task', status), [status], status)
        for status in ('failed', 'needs_owner', 'paused'):
            self.assertEqual(self.card_status('owner-control', status), [status], status)


class ConsoleEndpointTest(unittest.TestCase):
    """The dashboard's /api/control endpoints over a real loopback server."""

    def setUp(self):
        from kage.observe import dashboard as D
        self.D = D
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        write_config(self.tmp, enabled=True)
        wc = wconfig(self.tmp)
        hooks = {name: types.SimpleNamespace() for name in (
            'avatars', 'poll_schedule', 'previews', 'claude_delivery', 'run_log', 'task_registry', 'rule_engine',
            'routines', 'decision_buttons', 'blockers', 'feedback_learning', 'provider_health', 'stuck_claims',
            'dispatcher', 'cache_report', 'trace')}
        hooks['avatars'] = types.SimpleNamespace(initialize=lambda db: None, seed_events=lambda db: None)
        hooks['previews'] = types.SimpleNamespace(PreviewService=lambda path: None)
        wc.PROVIDERS, wc.EFFORTS, wc.digest = W.PROVIDERS, W.EFFORTS, W.digest
        core = D.DashboardCore('zh-CN', tz=None, owner_key='owner', owner_name='Mira', web=Path(self.tmp),
                               default_design_html='x', design_workspace='x', known_channels={}, tail_command='tail',
                               worker_config=wc, process_alive=lambda pid, birth: False, **hooks)
        self.db_path = Path(self.tmp) / 'state.sqlite'
        sqlite3.connect(self.db_path).close()
        self.server = D.DashboardServer(('127.0.0.1', 0), self.db_path, web=self.tmp, core=core)  # bound, never served
        self.addCleanup(self.server.server_close)
        self.host = f'127.0.0.1:{self.server.server_port}'

    def call(self, method, path, body=None, headers=None):
        """One request through the server's Handler without a socket (the suite is offline)."""
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b'')
        base = {'Host': self.host}
        if method == 'POST':
            base.update({'Origin': f'http://{self.host}', 'Content-Type': 'application/json',
                         'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty',
                         'User-Agent': 'Mozilla/5.0 Safari/605', 'Content-Length': str(len(data))})
        base.update(headers or {})
        h = self.D.Handler.__new__(self.D.Handler)
        h.server, h.rfile, h.wfile = self.server, io.BytesIO(data), io.BytesIO()
        h.headers = email.message.Message()
        for k, v in base.items():
            if v is not None:
                h.headers[k] = v
        h.path, h.command, h.request_version = path, method, 'HTTP/1.1'
        h.requestline, h.client_address, h.close_connection = f'{method} {path} HTTP/1.1', ('127.0.0.1', 0), True
        getattr(h, 'do_' + method)()
        head, _, payload = h.wfile.getvalue().partition(b'\r\n\r\n')
        return int(head.split(b' ', 2)[1]), json.loads(payload or b'null')

    def token(self):
        status, data = self.call('GET', '/api/control?token=new')
        self.assertEqual(status, 200)
        return data['page_token']

    def test_get_without_a_store_and_a_token_only_on_request(self):
        status, data = self.call('GET', '/api/control')
        self.assertEqual(status, 200)
        self.assertTrue(data['enabled'])
        self.assertEqual(data['conversations'], [])
        self.assertNotIn('page_token', data)
        self.assertEqual(self.call('GET', '/api/control', headers={'Host': 'evil.example:80'})[0], 403)

    def test_a_message_goes_in_with_its_request_facts(self):
        token = self.token()
        status, data = self.call('POST', '/api/control/messages', {'text': '进度如何？', 'client_id': 'client-0001'},
                                 {'X-Page-Token': token})
        self.assertEqual(status, 200, data)
        self.assertEqual((data['message']['facts']['ua'], data['message']['facts']['browser_like']), ('safari', True))
        status, data = self.call('GET', '/api/control?conversation=' + data['message']['conversation'])
        self.assertEqual(data['messages'][0]['text'], '进度如何？')
        # the same token still works for the next message; a script without Sec-Fetch headers is recorded as one
        status, data = self.call('POST', '/api/control/messages', {'text': 'again', 'client_id': 'client-0002'},
                                 {'X-Page-Token': token, 'Sec-Fetch-Site': None, 'Sec-Fetch-Mode': None,
                                  'Sec-Fetch-Dest': None, 'User-Agent': 'curl/8'})
        self.assertEqual(status, 200)
        self.assertEqual((data['message']['facts']['ua'], data['message']['facts']['browser_like']), ('curl', False))

    def test_forged_and_malformed_requests_are_refused(self):
        token = self.token()
        body = {'text': 'hi', 'client_id': 'client-0001'}
        cases = [({'X-Page-Token': token, 'Origin': 'http://evil.example'}, body, 403),
                 ({'X-Page-Token': token, 'Sec-Fetch-Site': 'cross-site'}, body, 403),
                 ({'X-Page-Token': 'forged'}, body, 403),
                 ({}, body, 403),
                 ({'X-Page-Token': token, 'Content-Type': 'text/plain'}, body, 415),
                 ({'X-Page-Token': token}, b'x' * 9000, 400),
                 ({'X-Page-Token': token}, {'text': 'hi', 'client_id': 'client-0001', 'extra': 1}, 400),
                 ({'X-Page-Token': token, 'Host': 'localhost.evil:1'}, body, 403)]
        for headers, payload, expected in cases:
            self.assertEqual(self.call('POST', '/api/control/messages', payload, headers)[0], expected, headers)
        self.assertEqual(self.call('POST', '/api/control/messages', {'text': 'token ghp_' + 'b' * 30,
                                                                     'client_id': 'client-0009'},
                                   {'X-Page-Token': token}), (400, {'error': S.ERROR}))
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM control_messages').fetchone()[0], 0)

    def test_off_refuses_and_the_settings_save_uses_up_its_token(self):
        write_config(self.tmp, enabled=False)
        token = self.token()
        self.assertEqual(self.call('POST', '/api/control/messages', {'text': 'hi', 'client_id': 'client-0001'},
                                   {'X-Page-Token': token}), (409, {'error': 'control_off'}))
        _, state = self.call('GET', '/api/control')
        status, data = self.call('POST', '/api/control/settings', {'changes': {'enabled': True}, 'digest': state['digest']},
                                 {'X-Page-Token': token})
        self.assertEqual(status, 200, data)
        self.assertNotEqual(data['page_token'], token)
        self.assertEqual(self.call('POST', '/api/control/settings', {'changes': {'enabled': False},
                                                                     'digest': data['digest']},
                                   {'X-Page-Token': token})[0], 403)
        self.assertEqual(self.call('POST', '/api/control/settings', {'changes': {'task': 'x'}, 'digest': data['digest']},
                                   {'X-Page-Token': data['page_token']})[0], 400)


if __name__ == '__main__':
    unittest.main()
