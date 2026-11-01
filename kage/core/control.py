"""The owner's control entry (总控): the owner asks how any piece of work stands, or says what a task should do, and one
long-lived control task answers and passes instructions on (Phase 2, C1: the console; C2 adds the card channel).

- Sources. `submit` records an owner message from the console (the dashboard's same-origin, page-token POST) with its
  request facts (`facts`: which headers the request carried, the user-agent family), so a forged request — anything
  running as the owner on this machine could send one — is visible on the page and on every relay. Nothing an agent
  writes ever becomes an owner message.
- `pump` (supervisor step): each received owner message becomes ONE local event on the control task's inbox watch —
  envelope `control_message`, the owner's trust and intent — with a body file in the control workspace (the message,
  the conversation's earlier turns, how to answer). The dispatcher runs the control task like the intake tasks
  (`worker_config.intake_role` = 'control': the reserved lane, a fresh session per run, the inbox's lean profile).
- The run answers with `reply`, passes an instruction on with `relay` — the runtime attaches the owner's STORED words
  and their source to the target's event (envelope `owner_relay`, the owner's intent); the run passes only ids and a
  one-line note of its own, which carries no authority — and starts new work with `launch`. All three refuse unless the
  caller is the control task's run that holds the message.
- `sweep` (supervisor step): queued -> working while a run holds the message; unanswered after the run, or after
  control.answer_timeout_minutes -> failed (the page offers a retry); console text past control.retention_days is
  dropped (ids, states and digests stay).

States: received -> queued -> working -> answered | failed; `refused` is never stored (the request gets the code).
Tables are created on first use (an instance that never turns the entry on keeps its database as it was).
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from kage import locale
from kage.core import secrets as _secrets
from kage.core import task_memory as _memory
from kage.core.store import transaction as _transaction
from kage.core.worker_config import control_settings

SOURCES = ('console',)                 # C2 adds 'slack'
STATES = ('received', 'queued', 'working', 'answered', 'failed')
OPEN_STATES = ('received', 'queued', 'working')
MAX_TEXT = 4000                        # characters of one owner message
MAX_REPLY = 12000                      # characters of one reply
MAX_NOTE = 300
MAX_FOLLOWUPS = 3                      # replies after the answer (a relay's outcome)
PUMP_BATCH = 10
PROPOSAL_TTL = 24 * 3600               # a confirm card nobody clicked closes as expired
CARD_WORDS_MAX = 600                   # the owner's words on a confirm card; the whole text is on the console
MESSAGE_ID_RE = re.compile(r'cm[0-9a-f]{14}')
CONVERSATION_ID_RE = re.compile(r'cv[0-9a-f]{14}')
CLIENT_ID_RE = re.compile(r'[A-Za-z0-9_-]{8,64}')
UA_FAMILIES = (('edg/', 'edge'), ('chrome/', 'chrome'), ('firefox/', 'firefox'), ('safari/', 'safari'),
               ('curl/', 'curl'), ('python', 'python'), ('wget/', 'wget'), ('node', 'node'))
DDL = '''
  CREATE TABLE IF NOT EXISTS control_conversations (
    conversation_id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, smoke INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
  -- role owner: the owner's message (text: console only; digest always); role control: a reply (reply_to = the
  -- owner message). facts: the request facts (JSON), shown with the message and carried by every relay.
  CREATE TABLE IF NOT EXISTS control_messages (
    message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, source TEXT NOT NULL, role TEXT NOT NULL,
    client_id TEXT, text TEXT, digest TEXT NOT NULL, facts TEXT, state TEXT NOT NULL,
    needs_answer INTEGER NOT NULL DEFAULT 0, reply_to TEXT, event_id TEXT, run_id TEXT, holder TEXT, error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, smoke INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
    updated_at REAL NOT NULL);
  CREATE UNIQUE INDEX IF NOT EXISTS control_messages_client ON control_messages (source, client_id)
    WHERE client_id IS NOT NULL;
  CREATE INDEX IF NOT EXISTS control_messages_state ON control_messages (state, created_at);
  CREATE INDEX IF NOT EXISTS control_messages_conversation ON control_messages (conversation_id, created_at);
  CREATE TABLE IF NOT EXISTS control_relays (
    relay_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, task_id TEXT NOT NULL, mode TEXT NOT NULL, event_id TEXT,
    dispatch_id TEXT, note TEXT, at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS control_relays_message ON control_relays (message_id);
  -- C1b: a relay / launch from a console message waits for the owner's button click on a confirm card (code-owned,
  -- never_auto: no delegate); state proposed -> confirmed (done) | cancelled | expired | refused | failed.
  CREATE TABLE IF NOT EXISTS control_proposals (
    proposal_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, kind TEXT NOT NULL, task_id TEXT, title TEXT, note TEXT,
    linear INTEGER NOT NULL DEFAULT 0, mode TEXT, state TEXT NOT NULL, card_event TEXT, created_at REAL NOT NULL,
    decided_at REAL, settled_at REAL, relay_id TEXT, error TEXT);
  CREATE INDEX IF NOT EXISTS control_proposals_state ON control_proposals (state, created_at);
  -- Cards only a button click decides: a typed thread reply never records their decision (card_replies.handle).
  CREATE TABLE IF NOT EXISTS button_only_cards (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, at REAL NOT NULL);
'''


def initialize(db):
    db.executescript(DDL)


def present(db):
    """True once the control tables exist (the entry was used here)."""
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='control_messages'").fetchone() is not None


def digest(text):
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def ua_family(agent):
    text = str(agent or '').lower()
    if not text:
        return 'none'
    for needle, name in UA_FAMILIES:
        if needle in text:
            return name
    return 'other'


def console_facts(headers):
    """The request facts of a console message: which Fetch Metadata headers the request carried and the user-agent
    family (a browser sends all three Sec-Fetch headers; a script usually none)."""
    get = headers.get if hasattr(headers, 'get') else (lambda k, d=None: d)
    sec = {k: (get(f'Sec-Fetch-{k.title()}') or None) for k in ('site', 'mode', 'dest')}
    return {'source': 'console', 'sec_fetch': sec, 'browser_like': all(sec.values()), 'ua': ua_family(get('User-Agent'))}


def _new_id(prefix):
    return prefix + uuid.uuid4().hex[:14]


def _one_line(text, limit):
    value = ' '.join(str(text or '').split())
    if not value or len(value) > limit:
        raise ValueError(f'note_must_be_one_line_of_at_most_{limit}_characters')
    return value


class Control:
    """The owner-bound half: the owner's key and name (the control task's default name, the authority it carries) and
    the owner's language (the body files and the console's words)."""

    def __init__(self, language, *, owner_key, owner_name):
        self.language = language
        self.key, self.name = owner_key, owner_name

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    # ---- configuration -------------------------------------------------------------------------------------------
    def settings(self, config):
        return control_settings(config, self.key)

    def _config(self, rt):
        try:
            return rt.worker_config.load()[0]
        except Exception:  # noqa: BLE001 - a malformed config turns the entry off (fail closed)
            return {}

    def enabled(self, rt):
        config = self._config(rt)
        return bool(config) and self.settings(config)['enabled'], config

    # ---- the console source ----------------------------------------------------------------------------------------
    def submit(self, db, config, text, *, client_id, conversation=None, facts=None, source='console', smoke=False,
               now=None):
        """Record one owner message (the console's POST; the caller checked the request). Returns the stored message (an
        idempotent retry with the same client_id returns the first one). Raises ValueError with a code: control_off,
        text_empty / text_too_long, client_id_invalid, unknown_conversation, rate_limited, or the secret code (nothing
        stored)."""
        now = time.time() if now is None else now
        cfg = self.settings(config)
        if not cfg['enabled']:
            raise ValueError('control_off')
        if source not in SOURCES:
            raise ValueError('unknown_source')
        if not isinstance(client_id, str) or not CLIENT_ID_RE.fullmatch(client_id):
            raise ValueError('client_id_invalid')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('text_empty')
        text = _memory.clean_text(text).strip()
        if len(text) > MAX_TEXT:
            raise ValueError('text_too_long')
        initialize(db)
        known = db.execute('SELECT * FROM control_messages WHERE source=? AND client_id=?', (source, client_id)).fetchone()
        if known is not None:
            return self.public(db, dict(known))
        _secrets.check_secret_free(text)
        if conversation is not None:
            if not isinstance(conversation, str) or not CONVERSATION_ID_RE.fullmatch(conversation):
                raise ValueError('unknown_conversation')
            if db.execute('SELECT 1 FROM control_conversations WHERE conversation_id=? AND source=?',
                          (conversation, source)).fetchone() is None:
                raise ValueError('unknown_conversation')
        recent = db.execute("SELECT count(*) FROM control_messages WHERE role='owner' AND source=? AND created_at>?",
                            (source, now - 3600)).fetchone()[0]
        if recent >= cfg['messages_per_hour']:
            raise ValueError('rate_limited')
        mid = _new_id('cm')
        with _transaction(db):
            if conversation is None:
                conversation = _new_id('cv')
                db.execute('INSERT INTO control_conversations VALUES (?,?,?,?,?,?)',
                           (conversation, source, _title(text), int(bool(smoke)), now, now))
            else:
                db.execute('UPDATE control_conversations SET updated_at=? WHERE conversation_id=?', (now, conversation))
            db.execute('INSERT INTO control_messages (message_id,conversation_id,source,role,client_id,text,digest,facts,'
                       "state,smoke,created_at,updated_at) VALUES (?,?,?,'owner',?,?,?,?,'received',?,?,?)",
                       (mid, conversation, source, client_id, text, digest(text),
                        json.dumps(facts or {'source': source}, ensure_ascii=False, sort_keys=True), int(bool(smoke)),
                        now, now))
        return self.public(db, self._row(db, mid))

    def retry(self, db, config, message_id, now=None):
        """A failed owner message is queued again (same id; the page's retry button)."""
        now = time.time() if now is None else now
        if not self.settings(config)['enabled']:
            raise ValueError('control_off')
        if not present(db):
            raise ValueError('unknown_message')
        m = self._row(db, message_id)
        if m is None or m['role'] != 'owner':
            raise ValueError('unknown_message')
        if m['state'] != 'failed':
            raise ValueError('only_a_failed_message_can_be_retried')
        if m['text'] is None:
            raise ValueError('message_text_expired')
        with _transaction(db):
            db.execute("UPDATE control_messages SET state='received',error=NULL,event_id=NULL,run_id=NULL,holder=NULL,"
                       "updated_at=? WHERE message_id=? AND state='failed'", (now, message_id))
        return self.public(db, self._row(db, message_id))

    # ---- supervisor steps ----------------------------------------------------------------------------------------
    def pump(self, rt, db, now=None):
        """Each received owner message -> one pending local event on the control task (oldest first, PUMP_BATCH per
        step). The control task must exist and have a current session (`control-bootstrap`); until then messages wait
        in `received` and the page says why."""
        if not present(db):  # first: an instance that never used the entry does nothing here, not even read the clock
            return {'queued': 0}
        now = time.time() if now is None else now
        on, config = self.enabled(rt)
        if not on:
            return {'queued': 0}
        rows = [dict(r) for r in db.execute("SELECT * FROM control_messages WHERE role='owner' AND state='received' "
                                            'ORDER BY created_at LIMIT ?', (PUMP_BATCH,))]
        if not rows:
            return {'queued': 0}
        cfg = self.settings(config)
        task = db.execute('SELECT * FROM tasks WHERE task_id=?', (cfg['task'],)).fetchone()
        if task is None or not task['current_session'] or not Path(task['path']).is_dir():
            reason = 'control_task_not_bootstrapped'
            with _transaction(db):
                db.execute("UPDATE control_messages SET error=?,updated_at=? WHERE role='owner' AND state='received' AND "
                           "coalesce(error,'')!=?", (reason, now, reason))
            return {'queued': 0, 'waiting': reason}
        queued = 0
        for m in rows:
            body = self._body(rt, db, m, cfg, now)
            path = _memory._write_new(_memory._unique(Path(task['path']) / 'history' / 'control' /
                                                      dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y-%m-%d'),
                                                      f"{_memory.utc_stamp(now)}-{m['message_id']}"), body)
            env = rt.envelopes.envelope('control_message', 'human', rt.envelopes.ENVELOPES.owner_trust,
                                        rt.envelopes.ENVELOPES.owner_intent, f"control_{m['source']}", None, 0)
            eid, _ = rt.enqueue_local_event(db, cfg['task'], task['path'], task['current_provider'] or cfg['provider'],
                                            task['current_session'], 'control', path, self.key, env, now)
            with _transaction(db):
                db.execute("UPDATE control_messages SET state='queued',event_id=?,error=NULL,attempts=attempts+1,"
                           "updated_at=? WHERE message_id=? AND state='received'", (eid, now, m['message_id']))
            queued += 1
        if queued:
            rt.start(_db_path(db))
        return {'queued': queued}

    def sweep(self, rt, db, now=None):
        """queued -> working while a run holds the message's event; an event handled (or a run ended) without an answer
        -> failed `no_reply`; open past control.answer_timeout_minutes -> failed `timeout`; console text older than
        control.retention_days dropped."""
        if not present(db):
            return {}
        now = time.time() if now is None else now
        config = self._config(rt)
        cfg = self.settings(config)
        out = {'working': 0, 'failed': 0, 'expired': 0}
        limit = cfg['answer_timeout_minutes'] * 60
        running = rt.registry.running_session(db, cfg['task'])  # a control run still working may answer yet
        for m in [dict(r) for r in db.execute("SELECT * FROM control_messages WHERE role='owner' AND state IN "
                                              "('queued','working')")]:
            e = db.execute('SELECT * FROM events WHERE id=?', (m['event_id'],)).fetchone() if m['event_id'] else None
            consumer = rt.dispatch_consumer(db, e) if e is not None else None
            run = None
            if consumer:
                run = db.execute("SELECT run_id FROM consumer_runs WHERE session=? AND state='running' ORDER BY "
                                 'started_at DESC LIMIT 1', (consumer['session'],)).fetchone()
            if (e is None or e['status'] == 'handled') and running is None:
                self._fail(db, m['message_id'], 'no_reply', now)
                out['failed'] += 1
            elif (m['state'] == 'queued' and now - m['updated_at'] > limit) or now - m['created_at'] > 2 * limit:
                self._fail(db, m['message_id'], 'timeout', now)
                out['failed'] += 1
            elif m['state'] == 'queued' and consumer:
                with _transaction(db):
                    db.execute("UPDATE control_messages SET state='working',run_id=?,updated_at=? WHERE message_id=? AND "
                               "state='queued'", (run[0] if run else None, now, m['message_id']))
                out['working'] += 1
        cutoff = now - cfg['retention_days'] * 86400
        with _transaction(db):
            cur = db.execute('UPDATE control_messages SET text=NULL WHERE text IS NOT NULL AND created_at<?', (cutoff,))
            out['expired'] = cur.rowcount
        return out

    def _fail(self, db, message_id, code, now):
        with _transaction(db):
            db.execute("UPDATE control_messages SET state='failed',error=?,updated_at=? WHERE message_id=? AND state IN "
                       "('received','queued','working')", (code, now, message_id))

    # ---- what the control run does ---------------------------------------------------------------------------------
    def _held(self, rt, db, message_id):
        """The owner message `message_id` if the caller is the control task's run that holds it, else ValueError."""
        if not isinstance(message_id, str) or not MESSAGE_ID_RE.fullmatch(message_id) or not present(db):
            raise ValueError('unknown_message')
        m = self._row(db, message_id)
        if m is None or m['role'] != 'owner':
            raise ValueError('not_an_owner_message')
        if m['state'] not in ('queued', 'working', 'answered'):
            raise ValueError(f"message_is_{m['state']}")
        e = db.execute('SELECT * FROM events WHERE id=?', (m['event_id'],)).fetchone() if m['event_id'] else None
        if e is None:
            raise ValueError('message_not_queued')
        w = rt.load(db, e['watch_id'])
        cfg = self.settings(self._config(rt))
        if w.get('task_id') != cfg['task']:
            raise ValueError('not_a_control_message')
        provider, session = rt.claude.actor()
        if not session:
            raise ValueError('only_the_control_run_holding_the_message_may_do_this')
        held = rt.by_dispatch(db, e) or (rt.claude.provider(w), w['session']) == (provider, session)
        holder = f'{provider}:{session}'
        if not held or (m['holder'] and m['holder'] != holder) or \
                rt.registry.task_id_for_cwd(os.getcwd()) not in (None, cfg['task']):
            raise ValueError('only_the_control_run_holding_the_message_may_do_this')
        if not m['holder']:  # the first session that acts on it keeps it (a later run cannot reply / relay for it)
            with _transaction(db):
                db.execute('UPDATE control_messages SET holder=? WHERE message_id=? AND holder IS NULL', (holder, message_id))
            m['holder'] = holder
        return m, e, cfg

    def show(self, rt, db, message_id):
        """The held message with its conversation's earlier turns (for the run; the body file has the same)."""
        m, _, cfg = self._held(rt, db, message_id)
        return {'ok': True, 'message': self.public(db, m), 'history': self._history(db, m, cfg['history_turns'])}

    def reply(self, rt, db, db_path, message_id, text, *, needs_answer=False, followup=False, now=None):
        """The control run's answer to the owner (stored for the console). Exactly one answer per message; up to
        MAX_FOLLOWUPS later `--followup` replies (a relay's outcome). The answer also resolves the message's event."""
        now = time.time() if now is None else now
        m, e, _ = self._held(rt, db, message_id)
        if not str(text or '').strip():
            raise ValueError('reply_empty')
        text = _memory.clean_text(str(text)).strip()
        if len(text) > MAX_REPLY:
            raise ValueError('reply_too_long')
        _secrets.check_secret_free(text)
        answered = m['state'] == 'answered'
        if answered and not followup:
            raise ValueError('already_answered_use_--followup')
        if followup and not answered:
            raise ValueError('answer_first')
        if followup and db.execute("SELECT count(*) FROM control_messages WHERE role='control' AND reply_to=?",
                                   (message_id,)).fetchone()[0] > MAX_FOLLOWUPS:
            raise ValueError('too_many_followups')
        rid = _new_id('cm')
        with _transaction(db):
            db.execute('INSERT INTO control_messages (message_id,conversation_id,source,role,text,digest,facts,state,'
                       "needs_answer,reply_to,smoke,created_at,updated_at) VALUES (?,?,?,'control',?,?,?,'answered',?,?,?,"
                       '?,?)', (rid, m['conversation_id'], m['source'], text, digest(text),
                                json.dumps({'followup': bool(followup)}), int(bool(needs_answer)), message_id, m['smoke'],
                                now, now))
            if not answered:
                db.execute("UPDATE control_messages SET state='answered',needs_answer=?,error=NULL,updated_at=? WHERE "
                           'message_id=?', (int(bool(needs_answer)), now, message_id))
            db.execute('UPDATE control_conversations SET updated_at=? WHERE conversation_id=?', (now, m['conversation_id']))
        if not answered and e['status'] != 'handled':
            try:
                rt.resolve(db, e['id'], 'observe', now=now)
            except ValueError:
                pass  # the run resolves it itself (e.g. the watch moved on); the answer stands
        return {'ok': True, 'message_id': message_id, 'reply_id': rid, 'followup': bool(followup),
                'needs_answer': bool(needs_answer)}

    def relay(self, rt, db, db_path, message_id, task, note=None, now=None):
        """Pass the owner's message on to `task`. A Slack message of the owner's (C2) is relayed at once; a console
        message only proposes it: the owner gets a confirm card and the relay runs on their button click (`settle`),
        because the console's only proof of the owner is a page token anything running as them could fetch. The relay
        itself (`_relay`) writes the owner's STORED words and their source into the target's history and delivers them
        as an `owner_relay` event (the owner's trust and intent), or dispatches the task when it has no session yet. The
        run passes only the ids and an optional one-line note of its own, labelled as carrying no authority."""
        now = time.time() if now is None else now
        m, _, cfg = self._held(rt, db, message_id)
        row = rt._resolve_task_arg(db, task)
        if row['task_id'] == cfg['task']:
            raise ValueError('cannot_relay_to_the_control_task')
        if note is not None:
            note = _one_line(note, MAX_NOTE)
            _secrets.check_secret_free(note)
        if self._count_since(db, now - 3600) >= cfg['relays_per_hour']:
            raise ValueError('relay_rate_limited')
        if db.execute('SELECT 1 FROM control_relays WHERE message_id=? AND task_id=?', (message_id, row['task_id'])).fetchone() \
                or db.execute("SELECT 1 FROM control_proposals WHERE message_id=? AND task_id=? AND kind='relay' AND state "
                              "IN ('proposed','deciding')", (message_id, row['task_id'])).fetchone():
            raise ValueError('already_relayed_to_this_task')
        self._original(m)  # the stored words must still be there and match their digest
        if m['source'] == 'console':
            mode = 'event' if rt.registry.task_consumer(db, row['task_id']) else 'dispatch'
            return self._propose(rt, db, m, cfg, 'relay', now, task=row, note=note, mode=mode)
        return self._relay(rt, db, db_path, m, row, note, now)

    def _relay(self, rt, db, db_path, m, row, note, now):
        original = self._original(m)
        body = self._relay_body(m, original, note, now)
        workspace = Path(row['path'])
        path = _memory._write_new(_memory._unique(workspace / 'history' / 'messages',
                                                  f"{_memory.utc_stamp(now)}-from-{self.key}-control"), body)
        relay_id = _new_id('cr')
        out = {'ok': True, 'relay_id': relay_id, 'task_id': row['task_id'], 'file': str(path), 'event': None,
               'dispatch_id': None, 'authority': rt.envelopes.ENVELOPES.owner_intent,
               'source': json.loads(m['facts'] or '{}').get('source')}
        consumer = rt.registry.task_consumer(db, row['task_id'])
        if consumer:
            env = rt.envelopes.envelope('owner_relay', 'human', rt.envelopes.ENVELOPES.owner_trust,
                                        rt.envelopes.ENVELOPES.owner_intent, 'control_relay', None, 0)
            eid, _ = rt.enqueue_local_event(db, row['task_id'], row['path'], consumer[0], consumer[1], 'control_relay',
                                            path, self.key, env, now)
            out.update(mode='event', event=eid, to=f'{consumer[0]}:{consumer[1]}')
            rt.start(db_path)
        else:
            launched = rt.launch_task(db, db_path, row['path'], 'auto', body, wait_seconds=0, now=now, persist=False)
            out.update(mode='dispatch', dispatch_id=launched.get('dispatch_id'))
        with _transaction(db):
            db.execute('INSERT INTO control_relays VALUES (?,?,?,?,?,?,?,?)',
                       (relay_id, m['message_id'], row['task_id'], out['mode'], out['event'], out['dispatch_id'], note, now))
        return out

    def launch(self, rt, db, db_path, message_id, title, *, linear=False, now=None):
        """New work from the owner's message: a new task workspace (the deployment's workspace bootstrap) whose first
        session gets the owner's words as its brief (control.allow_launch; control.launches_per_day). A tracker ticket
        only with `linear` (only when the owner asked for one). From a console message: a confirm card first."""
        now = time.time() if now is None else now
        m, _, cfg = self._held(rt, db, message_id)
        if not cfg['allow_launch']:
            raise ValueError('control_launch_off')
        title = _one_line(title, 120)
        _secrets.check_secret_free(title)
        if self._count_since(db, now - 86400, launch=True) >= cfg['launches_per_day']:
            raise ValueError('launch_rate_limited')
        if getattr(rt.intake, 'run_bootstrap', None) is None:
            raise ValueError('no_workspace_bootstrap_in_this_deployment')
        self._original(m)
        if m['source'] == 'console':
            return self._propose(rt, db, m, cfg, 'launch', now, title=title, linear=linear)
        return self._launch(rt, db, db_path, m, cfg, title, linear, now)

    def _launch(self, rt, db, db_path, m, cfg, title, linear, now):
        body = self._relay_body(m, self._original(m), None, now)
        data = rt.intake.run_bootstrap(title, title, body, linear=bool(linear), agent=cfg['provider'], root=rt.registry.root())
        launched = rt.launch_task(db, db_path, data['workspace'], 'auto', body, wait_seconds=0, now=now, persist=False)
        relay_id = _new_id('cr')
        with _transaction(db):
            db.execute('INSERT INTO control_relays VALUES (?,?,?,?,?,?,?,?)',
                       (relay_id, m['message_id'], launched['task_id'], 'launch', None, launched.get('dispatch_id'), title,
                        now))
        return {'ok': True, 'relay_id': relay_id, 'task_id': launched['task_id'], 'workspace': data['workspace'],
                'dispatch_id': launched.get('dispatch_id'), 'authority': rt.envelopes.ENVELOPES.owner_intent,
                'linear': bool(linear)}

    def _count_since(self, db, since, launch=False):
        """Relays (or launches) done plus proposals made since `since`: a run cannot flood the owner with cards."""
        kind = "mode='launch'" if launch else "mode!='launch'"
        done = db.execute(f'SELECT count(*) FROM control_relays WHERE {kind} AND at>?', (since,)).fetchone()[0]
        pkind = "kind='launch'" if launch else "kind='relay'"
        asked = db.execute(f'SELECT count(*) FROM control_proposals WHERE {pkind} AND created_at>? AND '
                           "state NOT IN ('confirmed')", (since,)).fetchone()[0]
        return done + asked

    # ---- C1b: the confirm card ---------------------------------------------------------------------------------------
    def _propose(self, rt, db, m, cfg, kind, now, task=None, note=None, mode=None, title=None, linear=False):
        """A confirm card to the owner (code-owned, never_auto, button only) for a relay / launch from a console
        message; nothing runs until `settle` sees the owner's own click."""
        pid = _new_id('cp')
        words = self._original(m)
        short = words if len(words) <= CARD_WORDS_MAX else words[:CARD_WORDS_MAX].rstrip() + self.msg('control.card.more')
        base = getattr(rt, 'DASHBOARD_URL', None)  # no configured console address: no link rather than a broken one
        link = f"{base}/control?conversation={m['conversation_id']}" if isinstance(base, str) and \
            base.startswith(('http://', 'https://')) else None
        if kind == 'relay':
            what = self.msg('control.card.relay_' + mode, task=task['task_id'], title=task.get('title') or task['task_id'])
        else:
            what = self.msg('control.card.launch_' + ('ticket' if linear else 'local'), title=title)
        question = self.msg('control.card.question', what=what, words=short, link=link) if link else \
            self.msg('control.card.question_nolink', what=what, words=short)
        card = rt.supervisor_card(db, question, [f"confirm={self.msg('control.card.confirm')}:approve",
                                                 f"cancel={self.msg('control.card.cancel')}:reject"],
                                  never_auto=True, kind='control_proposal', refs={'proposal': pid}, now=now,
                                  task_id=cfg['task'], first_line_max=None)
        with _transaction(db):
            db.execute('INSERT INTO control_proposals (proposal_id,message_id,kind,task_id,title,note,linear,mode,state,'
                       "card_event,created_at) VALUES (?,?,?,?,?,?,?,?,'proposed',?,?)",
                       (pid, m['message_id'], kind, task['task_id'] if task else None, title, note, int(bool(linear)), mode,
                        card['event_id'], now))
            db.execute('INSERT OR IGNORE INTO button_only_cards VALUES (?,?,?)', (card['event_id'], 'control_proposal', now))
        return {'ok': True, 'state': 'proposed', 'proposal_id': pid, 'kind': kind,
                'task_id': task['task_id'] if task else None, 'title': title, 'mode': mode, 'linear': bool(linear),
                'card_event': card['event_id'],
                'next': self.msg('control.card.next', name=self.name)}

    def settle(self, rt, db, now=None):
        """Supervisor step: each proposal's confirm card -> exactly one outcome. Only the owner's own button click
        (decision provenance slack_button, role approver) on 确认 runs the relay / launch; 取消 cancels; a typed reply or
        anyone else's decision runs nothing (refused); no click within PROPOSAL_TTL -> expired. The claim
        (proposed -> deciding) makes a second click or a second supervisor do nothing."""
        if not present(db):
            return []
        now = time.time() if now is None else now
        done = []
        owner = _owner_user(rt)  # None: every decision is refused (fail closed)
        for p in [dict(r) for r in db.execute("SELECT * FROM control_proposals WHERE state='proposed' ORDER BY created_at")]:
            d = db.execute('SELECT d.*, c.options FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                           'WHERE d.event_id=?', (p['card_event'],)).fetchone() if p['card_event'] else None
            if d is None and now - p['created_at'] < PROPOSAL_TTL:
                continue
            with _transaction(db):
                claimed = db.execute("UPDATE control_proposals SET state='deciding',decided_at=? WHERE proposal_id=? AND "
                                     "state='proposed'", (d['decided_at'] if d is not None else None, p['proposal_id']))
            if claimed.rowcount != 1:
                continue
            m = self._row(db, p['message_id'])
            result, error, relay_id = None, None, None
            if d is None:
                state = 'expired'
            elif not owner or d['actor'] != owner or d['provenance'] != 'slack_button' or d['role'] != 'approver':
                state = 'refused'
            elif rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))['kind'] != 'approve':
                state = 'cancelled'
            else:
                try:
                    if p['kind'] == 'relay':
                        result = self._relay(rt, db, _db_path(db), m, rt._resolve_task_arg(db, p['task_id']), p['note'], now)
                    else:
                        result = self._launch(rt, db, _db_path(db), m, self.settings(self._config(rt)), p['title'],
                                              p['linear'], now)
                    state, relay_id = 'confirmed', result['relay_id']
                except Exception as exc:  # noqa: BLE001 - recorded on the proposal; the owner sees it on the console
                    state, error = 'failed', _code(exc)
            with _transaction(db):
                db.execute('UPDATE control_proposals SET state=?,settled_at=?,relay_id=?,error=? WHERE proposal_id=?',
                           (state, now, relay_id, error, p['proposal_id']))
            note = self.msg(f'control.proposal.{state}', task=(result or {}).get('task_id') or p['task_id'] or p['title'],
                            error=error or '')
            self._console_note(db, m, note, now)
            _card_result(rt, db, p['card_event'], note, p['proposal_id'])
            done.append({'proposal_id': p['proposal_id'], 'state': state, 'error': error})
        return done

    def _console_note(self, db, m, text, now):
        """A line from the runtime under the owner's message on the console (a control-role row, not the run's answer)."""
        if m is None:
            return
        rid = _new_id('cm')
        with _transaction(db):
            db.execute('INSERT INTO control_messages (message_id,conversation_id,source,role,text,digest,facts,state,'
                       "reply_to,smoke,created_at,updated_at) VALUES (?,?,?,'control',?,?,?,'answered',?,?,?,?)",
                       (rid, m['conversation_id'], m['source'], text, digest(text), json.dumps({'runtime': True}),
                        m['message_id'], m['smoke'], now, now))

    # ---- the control task ----------------------------------------------------------------------------------------
    def bootstrap(self, rt, db, db_path, *, launch=True, rewrite_goal=False, wait_seconds=90.0, now=None):
        """Idempotent: create the control task's workspace (the deployment's workspace bootstrap, a plain folder name),
        write its goal.md / status.md once, register it, start its first session and bind its inbox watch — the same
        steps intake-bootstrap takes for the inbox. Works while the entry is off (the switch is separate)."""
        now = time.time() if now is None else now
        config = self._config(rt)
        cfg = self.settings(config)
        task_id = cfg['task']
        path = rt.registry.root() / task_id
        report = {'ok': True, 'task_id': task_id, 'workspace': str(path), 'created': False, 'goal_written': False,
                  'launched': None, 'enabled': cfg['enabled']}
        if not path.is_dir():
            title = self.msg('control.task.title', name=self.name)
            data = rt.intake.run_bootstrap(title, title, self.msg('control.task.context'), linear=False,
                                           agent=cfg['provider'], root=rt.registry.root(), folder_name=task_id)
            if Path(data['workspace']).resolve() != path.resolve():
                raise ValueError('bootstrap_created_an_unexpected_workspace_name')
            report['created'] = True
        cli = f'python3 {Path(rt.__file__).resolve()} --db {db_path}'
        if report['created'] or rewrite_goal:
            (path / 'goal.md').write_text(self.msg('control.task.goal', name=self.name, cli=cli) + '\n', encoding='utf-8')
            report['goal_written'] = True
        if report['created']:
            (path / 'status.md').write_text(self.msg('control.task.status', name=self.name, at=_iso(now)) + '\n',
                                            encoding='utf-8')
        with _transaction(db):
            rt.registry.ensure_task(db, task_id, now)
            db.execute("UPDATE tasks SET state='active',title=?,path=?,updated_at=? WHERE task_id=?",
                       (rt.registry.read_title(path), str(path), now, task_id))
        row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        pending = db.execute("SELECT dispatch_id FROM dispatches WHERE task_key=? AND trigger='launch' AND state IN "
                             "('waiting','running')", (task_id,)).fetchone()
        if not row['current_session'] and launch and pending:
            report['launched'] = {'dispatch_id': pending[0], 'state': 'in_progress'}
        elif not row['current_session'] and launch:
            launched = rt.launch_task(db, db_path, str(path), cfg['provider'], self.msg('control.task.launch', name=self.name),
                                      wait_seconds=wait_seconds, now=now, persist=False)
            report['launched'] = {k: launched.get(k) for k in ('dispatch_id', 'run_id', 'provider', 'session', 'state')}
            row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if row['current_session']:
            with _transaction(db):
                w = rt.ensure_inbox_watch(db, task_id, str(path), row['current_provider'], row['current_session'], now)
            report.update(current={'provider': row['current_provider'], 'session': row['current_session']},
                          inbox_watch=w['id'])
        else:
            report.update(current=None, inbox_watch=None,
                          pending='no current session yet; rerun control-bootstrap once its first run has started')
        initialize(db)
        return report

    def status(self, rt, db):
        """The entry's switch, task and the counts of its messages by state (no text)."""
        config = self._config(rt)
        cfg = self.settings(config)
        task = db.execute('SELECT task_id, current_provider, current_session, path FROM tasks WHERE task_id=?',
                          (cfg['task'],)).fetchone()
        out = {'ok': True, 'enabled': cfg['enabled'], 'task': cfg['task'], 'provider': cfg['provider'],
               'model': cfg['model'], 'effort': cfg['effort'],
               'bootstrapped': bool(task and task['current_session'] and Path(task['path']).is_dir()),
               'role': rt.worker_config.intake_role(config, cfg['task']) if config else None, 'messages': {}}
        if present(db):
            out['messages'] = {r[0]: r[1] for r in db.execute(
                "SELECT state, count(*) FROM control_messages WHERE role='owner' GROUP BY state")}
            out['relays_last_hour'] = db.execute('SELECT count(*) FROM control_relays WHERE at>?',
                                                 (time.time() - 3600,)).fetchone()[0]
        return out

    # ---- the digest the run starts from ----------------------------------------------------------------------------
    def context(self, rt, db, now=None, hours=24):
        """One compact JSON digest (ids, titles, states, counts, the head of each status.md; never message bodies):
        tasks touched in the last `hours`, running runs, waiting dispatches, open owner cards, open blockers, recent
        failures, and the intake items of the period."""
        now = time.time() if now is None else now
        since = now - hours * 3600
        out = {'now': _iso(now), 'hours': hours}

        def section(name, fn):
            try:
                out[name] = fn()
            except Exception as exc:  # noqa: BLE001 - one missing table never hides the rest
                out[name] = {'error': type(exc).__name__}

        def tasks():
            rows = db.execute('SELECT task_id, title, state, path, current_provider, updated_at FROM tasks WHERE '
                              'updated_at>? OR task_id IN (SELECT task_key FROM dispatches WHERE updated_at>?) ORDER BY '
                              'updated_at DESC LIMIT 40', (since, since)).fetchall()
            items = []
            for r in rows:
                last = db.execute('SELECT state, trigger, updated_at FROM dispatches WHERE task_key=? ORDER BY updated_at '
                                  'DESC LIMIT 1', (r['task_id'],)).fetchone()
                items.append({'task_id': r['task_id'], 'title': r['title'], 'state': r['state'],
                              'updated': _iso(r['updated_at']), 'provider': r['current_provider'],
                              'last_dispatch': dict(last) if last else None, 'status_head': _status_head(r['path'])})
            return items

        def runs():
            items = []
            for r in db.execute("SELECT run_id, task, provider, started_at, log_dir FROM consumer_runs WHERE "
                                "state='running' ORDER BY started_at").fetchall():
                summary = None
                try:
                    summary = rt.run_log.read_state(r['log_dir']).get('last_summary') if r['log_dir'] else None
                except Exception:  # noqa: BLE001
                    pass
                items.append({'run_id': r['run_id'], 'task': r['task'], 'provider': r['provider'],
                              'started': _iso(r['started_at']), 'last_summary': (summary or '')[:300] or None})
            return items

        def waiting():
            return [dict(r) for r in db.execute("SELECT task_key AS task, state, wait_reason, created_at FROM dispatches "
                                                "WHERE state='waiting' ORDER BY created_at LIMIT 20").fetchall()]

        def cards():
            rows = db.execute('SELECT c.task_id, count(*) AS n FROM escalation_cards c WHERE c.dry=0 AND NOT EXISTS '
                              '(SELECT 1 FROM decisions d WHERE d.event_id=c.event_id) AND c.created>? GROUP BY c.task_id '
                              'ORDER BY n DESC LIMIT 20', (now - 7 * 86400,)).fetchall()
            return [dict(r) for r in rows]

        def blockers():
            rows = db.execute("SELECT task_id, system, status FROM automation_gaps WHERE status IN ('open','reported') "
                              'ORDER BY rowid DESC LIMIT 20').fetchall()
            return [dict(r) for r in rows]

        def failures():
            rows = db.execute("SELECT task_key AS task, state, updated_at FROM dispatches WHERE state IN "
                              "('failed','gave_up') AND updated_at>? ORDER BY updated_at DESC LIMIT 20", (since,)).fetchall()
            return [{**dict(r), 'updated_at': _iso(r['updated_at'])} for r in rows]

        def intake():
            rows = db.execute('SELECT route, coalesce(handled_task, target_task) AS task, count(*) AS n FROM intake_bursts '
                              'WHERE created_at>? GROUP BY 1, 2 ORDER BY n DESC LIMIT 30', (since,)).fetchall()
            return [dict(r) for r in rows]

        for name, fn in (('tasks', tasks), ('running_runs', runs), ('waiting_dispatches', waiting),
                         ('open_owner_cards', cards), ('open_blockers', blockers), ('recent_failures', failures),
                         ('intake', intake)):
            section(name, fn)
        return out

    # ---- the console's read model --------------------------------------------------------------------------------
    def overview(self, db, config, conversation=None, limit=40):
        cfg = self.settings(config)
        out = {'enabled': cfg['enabled'], 'task': cfg['task'], 'provider': cfg['provider'], 'model': cfg['model'],
               'effort': cfg['effort'], 'allow_launch': cfg['allow_launch'], 'limits': {
                   'messages_per_hour': cfg['messages_per_hour'], 'max_text': MAX_TEXT},
               'conversations': [], 'messages': [], 'conversation': None, 'waiting': None}
        if db is None or not present(db):
            return out
        out['conversations'] = [
            {**dict(r), 'created_at': _iso(r['created_at']), 'updated_at': _iso(r['updated_at'])}
            for r in db.execute("SELECT c.conversation_id, c.source, c.title, c.created_at, c.updated_at, (SELECT state "
                                "FROM control_messages m WHERE m.conversation_id=c.conversation_id AND m.role='owner' "
                                'ORDER BY m.created_at DESC LIMIT 1) AS last_state FROM control_conversations c ORDER BY '
                                'c.updated_at DESC LIMIT ?', (limit,)).fetchall()]
        waiting = db.execute("SELECT error FROM control_messages WHERE role='owner' AND state='received' AND error IS NOT "
                             'NULL LIMIT 1').fetchone()
        out['waiting'] = waiting[0] if waiting else None
        if conversation is not None:
            if not CONVERSATION_ID_RE.fullmatch(str(conversation)):
                raise KeyError(conversation)
            row = db.execute('SELECT * FROM control_conversations WHERE conversation_id=?', (conversation,)).fetchone()
            if row is None:
                raise KeyError(conversation)
            out['conversation'] = {**dict(row), 'created_at': _iso(row['created_at']), 'updated_at': _iso(row['updated_at'])}
            out['messages'] = [self.public(db, dict(r)) for r in db.execute(
                'SELECT * FROM control_messages WHERE conversation_id=? ORDER BY rowid', (conversation,))]  # as written
        return out

    def public(self, db, m):
        """A message as the console shows it: text, state, request facts, relays (task, mode, time), error."""
        out = {'id': m['message_id'], 'conversation': m['conversation_id'], 'role': m['role'], 'source': m['source'],
               'text': m['text'], 'expired': m['text'] is None, 'state': m['state'],
               'needs_answer': bool(m['needs_answer']), 'reply_to': m['reply_to'], 'error': m['error'],
               'run_id': m['run_id'], 'facts': json.loads(m['facts'] or '{}'), 'created_at': _iso(m['created_at']),
               'updated_at': _iso(m['updated_at']), 'relays': []}
        if m['role'] == 'owner':
            out['relays'] = [{'task_id': r['task_id'], 'mode': r['mode'], 'at': _iso(r['at'])} for r in db.execute(
                'SELECT task_id, mode, at FROM control_relays WHERE message_id=? ORDER BY at', (m['message_id'],))]
            out['proposals'] = [{'kind': r['kind'], 'task_id': r['task_id'], 'title': r['title'], 'mode': r['mode'],
                                 'linear': bool(r['linear']), 'state': r['state'], 'error': r['error'],
                                 'at': _iso(r['created_at'])} for r in db.execute(
                'SELECT * FROM control_proposals WHERE message_id=? ORDER BY created_at', (m['message_id'],))] \
                if _has_table(db, 'control_proposals') else []
        else:
            out['runtime'] = bool(json.loads(m['facts'] or '{}').get('runtime'))
        return out

    # ---- helpers -------------------------------------------------------------------------------------------------
    def _row(self, db, message_id):
        row = db.execute('SELECT * FROM control_messages WHERE message_id=?', (message_id,)).fetchone()
        return dict(row) if row else None

    def _history(self, db, m, turns):
        if not turns:
            return []
        rows = db.execute('SELECT role, text, created_at FROM control_messages WHERE conversation_id=? AND rowid<(SELECT '
                          'rowid FROM control_messages WHERE message_id=?) ORDER BY rowid DESC LIMIT ?',
                          (m['conversation_id'], m['message_id'], turns)).fetchall()  # the turns written before it
        return [{'role': r['role'], 'text': r['text'], 'at': _iso(r['created_at'])} for r in reversed(rows)]

    def _original(self, m):
        """The owner's stored words, checked against the digest recorded with them."""
        if m['text'] is None:
            raise ValueError('message_text_expired')
        if digest(m['text']) != m['digest']:
            raise ValueError('stored_text_does_not_match_its_digest')
        return m['text']

    def _facts_line(self, m):
        facts = json.loads(m['facts'] or '{}')
        if facts.get('source') == 'console':
            sec = facts.get('sec_fetch') or {}
            return self.msg('control.facts.console', ua=facts.get('ua') or 'none',
                            sec=self.msg('control.facts.sec_present' if facts.get('browser_like') else
                                         'control.facts.sec_absent'),
                            site=sec.get('site') or '-')
        return str(facts.get('source') or m['source'])

    def _body(self, rt, db, m, cfg, now):
        cli = f'python3 {Path(rt.__file__).resolve()} --db {_db_path(db)}'
        history = self._history(db, m, cfg['history_turns'])
        lines = [self.msg('control.body.head', id=m['message_id'], source=m['source'], at=_iso(m['created_at'])),
                 self.msg('control.body.facts', facts=self._facts_line(m)), '']
        if history:
            lines.append(self.msg('control.body.history'))
            for h in history:
                who = self.msg('control.body.owner' if h['role'] == 'owner' else 'control.body.control', name=self.name)
                lines.append(f"- {who} ({h['at']}): " + ' '.join(str(h['text'] or self.msg('control.body.expired')).split())[:600])
            lines.append('')
        lines += [self.msg('control.body.message_begin', name=self.name), m['text'], self.msg('control.body.message_end'), '',
                  self.msg('control.body.how', cli=cli, id=m['message_id'], name=self.name)]
        return '\n'.join(lines) + '\n'

    def _relay_body(self, m, original, note, now):
        lines = [self.msg('control.relay.head', name=self.name),
                 '', self.msg('control.relay.meta', id=m['message_id'], source=m['source'], at=_iso(m['created_at']),
                              facts=self._facts_line(m), authority=f'{self.key}_intent'),
                 '', self.msg('control.relay.begin', name=self.name), original, self.msg('control.relay.end')]
        if note:
            lines += ['', self.msg('control.relay.note', note=note)]
        lines += ['', self.msg('control.relay.tail', name=self.name)]
        return '\n'.join(lines) + '\n'


# ---- module helpers ------------------------------------------------------------------------------------------------
def _owner_user(rt):
    """The owner's chat user id from the deployment profile (the assembled runtime always has it), else the hub's
    OWNER; None when neither is there."""
    try:
        user = rt.DEPLOYMENT.owner.slack_user
    except AttributeError:
        user = getattr(rt, 'OWNER', None)
    return user if isinstance(user, str) and user else None


def button_only(db, eid):
    """C1b: a card only a button click decides (a control confirm card): a typed reply in its thread never records its
    decision (card_replies.handle, escalation.thread_reply_decision). False on a database that never had the table."""
    try:
        return db.execute('SELECT 1 FROM button_only_cards WHERE event_id=?', (eid,)).fetchone() is not None
    except Exception:  # noqa: BLE001 - no such table: no button-only card
        return False


def _code(exc):
    text = str(exc)
    return text if text and len(text) <= 120 and all(c.isalnum() or c in '_:-.' for c in text) else type(exc).__name__


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _card_result(rt, db, card_event, text, proposal_id):
    """Best effort: a bot reply in the confirm card's thread saying what happened."""
    try:
        esc = db.execute('SELECT thread_ts FROM escalations WHERE event_id=?', (card_event,)).fetchone()
        card = db.execute('SELECT channel FROM escalation_cards WHERE event_id=?', (card_event,)).fetchone()
        if not esc or not card or not esc['thread_ts']:
            return None
        sent = rt.slack.api_call('chat.postMessage', rt.api_identity(bot=True), json_body={
            'channel': card['channel'], 'thread_ts': esc['thread_ts'], 'text': text, 'unfurl_links': False,
            'metadata': rt.agent_metadata('control_proposal_result', None, proposal_id=proposal_id)})
        rt.record_send(db, card['channel'], (sent or {}).get('ts'), 'control_proposal_result', proposal_id)
        return (sent or {}).get('ts')
    except Exception:  # noqa: BLE001 - the outcome stands; only the receipt is missing
        return None


def _db_path(db):
    row = next((r for r in db.execute('PRAGMA database_list') if r[1] == 'main'), None)
    return row[2] if row else None


def _iso(ts):
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).isoformat(timespec='seconds')


def _title(text):
    line = ' '.join(str(text).split())
    return line[:60] + ('…' if len(line) > 60 else '')


def _status_head(path, limit=700):
    """The first lines of a task's status.md after its rev header (the snapshot's current state), at most `limit`."""
    try:
        text = (Path(path) / 'status.md').read_text(encoding='utf-8')
    except (OSError, TypeError, ValueError):
        return None
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith('<!--')]
    return '\n'.join(lines)[:limit] or None
