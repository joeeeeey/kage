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
from collections import OrderedDict, deque
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from kage import locale
from kage.core import secrets as _secrets
from kage.core import task_memory as _memory
from kage.core import store as _store
from kage.core.store import transaction as _transaction
from kage.core.worker_config import control_settings

SOURCES = ('console', 'slack')        # C2: the owner's top-level messages in the card channel
STATES = ('received', 'queued', 'working', 'answered', 'failed')
OPEN_STATES = ('received', 'queued', 'working')
MAX_TEXT = 4000                        # characters of one owner message
MAX_REPLY = 12000                      # characters of one reply
MAX_NOTE = 300
MAX_FOLLOWUPS = 3                      # replies after the answer (a relay's outcome)
PUMP_BATCH = 10
PROPOSAL_TTL = 24 * 3600               # a confirm card nobody clicked closes as expired
CARD_WORDS_MAX = 600                   # the owner's words on a confirm card; the whole text is on the console
SINCE_KEY = 'control_slack_since'      # runtime_meta: Slack messages before this never become control messages
POLL_KEY = 'control_slack_poll'        # runtime_meta: the last fallback poll (time + counts, no bodies)
SINCE_GRACE = 60                       # seconds before the entry first saw the card channel that still count
PUSH_KEY = 'control_slack_push'        # runtime_meta: the receiver's last control-push
HISTORY_MAX_SKIP = 600                 # the poll skips the history read while the receiver pushes, but reads this often
PAGE_LIMIT = 200                       # messages per conversations.history / .replies page
HISTORY_PAGES = 5                      # history pages per poll; a longer gap goes on from Slack's page cursor next poll
REPLY_PAGES = 2                        # replies pages per control thread per poll; the rest next poll, from its cursor
THREAD_ACTIVE = 86400                  # a control thread's replies are polled only within a day of its last turn:
THREAD_HOT = 3600                      # every poll within an hour of it,
THREAD_IDLE_POLL = 300                 # then every 5 minutes
THREAD_POLL_MAX = 5                    # control threads per poll (the least recently polled first)
WORDS_TTL = 60                         # C3: the console keeps Slack words it re-read in memory this long (never on disk)
WORDS_ERROR_TTL = 15                   # ... and a failed read this long
WORDS_READS_PER_MINUTE = 6             # Slack reads the console may make a minute (one per opened conversation)
WORDS_MAX_CACHED = 32                  # conversations held in memory
WORDS_PAGE = 200                       # messages of one conversation's thread read at once
TEXT_STATES = ('stored', 'expired', 'in_slack', 'slack', 'slack_edited', 'unreadable', 'busy')
SLACK_MARKUP_RE = re.compile(r'<([^<>]*)>')   # C3.1: Slack's markup in a message's stored text (see slack_plain)
SLACK_URL_RE = re.compile(r'(?:https?|mailto|ftp|tel):', re.I)
TS_RE = re.compile(r'\d+\.\d+')
CHANNEL_RE = re.compile(r'[CG][A-Z0-9]{2,}')
USER_RE = re.compile(r'[UW][A-Z0-9]{2,}')
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
  -- C2: a conversation the owner started with a top-level message in the card channel: its thread (the message's ts) is
  -- that conversation; `cursor` = the newest reply the fallback poll read; `updated_at` = its last turn (the owner's
  -- message or the answer); `polled_at` = the fallback poll's last read of it.
  CREATE TABLE IF NOT EXISTS control_threads (
    channel TEXT NOT NULL, root_ts TEXT NOT NULL, conversation_id TEXT NOT NULL, cursor TEXT, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, polled_at REAL, PRIMARY KEY (channel, root_ts));
'''


def initialize(db):
    db.executescript(DDL)
    # C2: a Slack message's refs (its text stays in Slack: only the digest is stored)
    for column in ('channel', 'ts'):
        _store.add_column(db, 'control_messages', column, 'TEXT')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS control_messages_slack ON control_messages (channel, ts) WHERE channel IS '
               'NOT NULL AND role=\'owner\'')
    db.commit()


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

    # ---- C2: the card channel ------------------------------------------------------------------------------------
    def card_channel_on(self, rt):
        """(on, cfg): the entry is on and its card-channel source too."""
        on, config = self.enabled(rt)
        cfg = self.settings(config) if config else None
        return bool(on and cfg and cfg['card_channel']), cfg

    def _since(self, db, now):
        """The instant the entry first saw the card channel (minus SINCE_GRACE): older messages never count."""
        row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (SINCE_KEY,)).fetchone()
        if row is None:
            with _transaction(db):
                db.execute('INSERT OR IGNORE INTO runtime_meta VALUES (?,?)', (SINCE_KEY, f'{now - SINCE_GRACE:.6f}'))
            row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (SINCE_KEY,)).fetchone()
        return float(row[0])

    def typed(self, rt, db, channel, m, root=None):
        """The owner's own hand-typed message: a top-level message (`root` None) or a reply in thread `root`. Exactly the
        classification card replies use (kage.adapters.slack.envelope: our marker, our app id, our send ledger, another
        app, a bot -> not the owner's), plus: the owner's user id, no subtype, no bot id / app id / bot profile."""
        owner = _owner_user(rt)
        ts = str(m.get('ts') or '')
        if not owner or m.get('user') != owner or not TS_RE.fullmatch(ts) or m.get('subtype') or m.get('bot_id') or \
                m.get('app_id') or m.get('bot_profile'):
            return False
        thread = m.get('thread_ts')
        if root is None:
            if thread not in (None, '', ts):
                return False
        elif thread != root or not TS_RE.fullmatch(str(root)) or float(ts) <= float(root):
            return False
        try:
            ledger = db.execute('SELECT 1 FROM agent_sends WHERE channel=? AND ts=?', (channel, ts)).fetchone() is not None
        except Exception:  # noqa: BLE001 - a store without the send ledger: nothing was sent from it
            ledger = False
        if ledger:
            return False
        env = rt.envelopes.classify_message(m, thread_root=root or ts, ledger_hit=False, people=rt.envelopes.load_people())
        e = rt.envelopes.ENVELOPES
        return env['author_kind'] == 'human' and env['trust'] == e.owner_trust and env['authority'] == e.owner_intent

    def _read(self, rt, channel, ts, root=None):
        """One message read back from Slack (the owner's token, read-only), or None."""
        if root and root != ts:
            found = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
                'channel': channel, 'ts': root, 'oldest': ts, 'latest': ts, 'inclusive': 'true', 'limit': 2,
                'include_all_metadata': 'true'})
        else:
            found = rt.slack.api_call('conversations.history', rt.api_identity(), params={
                'channel': channel, 'latest': ts, 'inclusive': 'true', 'limit': 1, 'include_all_metadata': 'true'})
        return next((x for x in found.get('messages') or [] if isinstance(x, dict) and x.get('ts') == ts), None)

    def _root(self, db, m):
        """The thread root of a Slack control message's conversation."""
        root = json.loads(m.get('facts') or '{}').get('root')
        if root or db is None:
            return root or m['ts']
        row = db.execute('SELECT root_ts FROM control_threads WHERE conversation_id=?', (m['conversation_id'],)).fetchone()
        return row[0] if row else m['ts']

    def _record_slack(self, rt, db, cfg, channel, m, root, via, now):
        """A verified Slack message of the owner's -> an owner message (refs + digest, no text). `root` None starts a
        conversation whose thread is this message. Returns its id, or None (already recorded / rate limited)."""
        ts = str(m['ts'])
        text = m.get('text') or ''
        recent = db.execute("SELECT count(*) FROM control_messages WHERE role='owner' AND source='slack' AND created_at>?",
                            (now - 3600,)).fetchone()[0]
        if recent >= cfg['messages_per_hour']:
            return None
        facts = {'source': 'slack', 'channel': channel, 'via': via, 'hand_typed': True, 'root': root or ts,
                 'plain_digest': digest(slack_plain(text))}  # C3.1: the words as typed (Slack may re-render its markup)
        mid = _new_id('cm')
        with _transaction(db):
            if db.execute("SELECT 1 FROM control_messages WHERE channel=? AND ts=? AND role='owner'", (channel, ts)).fetchone():
                return None
            if root is None:
                conversation = _new_id('cv')
                db.execute('INSERT INTO control_conversations VALUES (?,?,?,?,?,?)', (conversation, 'slack', None, 0, now, now))
                db.execute('INSERT INTO control_threads (channel,root_ts,conversation_id,cursor,created_at,updated_at) '
                           'VALUES (?,?,?,?,?,?)', (channel, ts, conversation, ts, now, now))
            else:
                row = db.execute('SELECT conversation_id FROM control_threads WHERE channel=? AND root_ts=?',
                                 (channel, root)).fetchone()
                if row is None:
                    return None
                conversation = row[0]
                db.execute('UPDATE control_conversations SET updated_at=? WHERE conversation_id=?', (now, conversation))
                db.execute('UPDATE control_threads SET updated_at=? WHERE channel=? AND root_ts=?', (now, channel, root))
            db.execute('INSERT INTO control_messages (message_id,conversation_id,source,role,client_id,text,digest,facts,'
                       "state,smoke,created_at,updated_at,channel,ts) VALUES (?,?,'slack','owner',NULL,NULL,?,?,'received',"
                       '0,?,?,?,?)', (mid, conversation, digest(text), json.dumps(facts, sort_keys=True), now, now, channel, ts))
        try:  # the control-plane receipt the owner sees on the message (👀 + the provider's emoji)
            rt.ingest_receipt(db, [(channel, ts)], cfg['provider'], now)
        except Exception:  # noqa: BLE001 - a receipt never blocks the message
            pass
        return mid

    def slack_push(self, rt, db, channel, ts, user, now=None):
        """The receiver's realtime path for a NEW top-level message in a decision channel: refs only; the message is
        read back and verified here (`typed`). Nothing while the entry or its card channel is off."""
        now = time.time() if now is None else now
        if not (CHANNEL_RE.fullmatch(str(channel)) and TS_RE.fullmatch(str(ts)) and USER_RE.fullmatch(str(user))):
            return {'ok': False, 'outcome': 'refused', 'reason': 'invalid_refs'}
        on, cfg = self.card_channel_on(rt)
        if not on:
            return {'ok': True, 'outcome': 'skipped', 'reason': 'control_off'}
        if channel not in rt.card_threads.decision_channels(rt):
            return {'ok': False, 'outcome': 'refused', 'reason': 'not_a_decision_channel'}
        if user != _owner_user(rt):
            return {'ok': True, 'outcome': 'skipped', 'reason': 'not_the_owner'}
        initialize(db)
        with _transaction(db):  # the receiver is forwarding: the fallback poll may skip its history read meanwhile
            db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (PUSH_KEY, f'{now:.6f}'))
        if float(ts) <= self._since(db, now):
            return {'ok': True, 'outcome': 'skipped', 'reason': 'before_the_entry'}
        if db.execute("SELECT 1 FROM control_messages WHERE channel=? AND ts=? AND role='owner'", (channel, ts)).fetchone():
            return {'ok': True, 'outcome': 'duplicate'}
        m = self._read(rt, channel, ts)
        if m is None:
            return {'ok': False, 'outcome': 'refused', 'reason': 'message_not_found'}
        if not self.typed(rt, db, channel, m):
            return {'ok': True, 'outcome': 'skipped', 'reason': 'not_hand_typed'}
        mid = self._record_slack(rt, db, cfg, channel, m, None, 'push', now)
        if mid:
            rt.start(_db_path(db))
        return {'ok': True, 'outcome': 'recorded' if mid else 'duplicate_or_rate_limited', 'message_id': mid}

    def thread_reply(self, rt, db, channel, root, m, now=None, source='push'):
        """The owner's reply in a control conversation's thread: the next message of that conversation. None when the
        entry or its card channel is off, or the thread is not a control thread (card routing goes on as before)."""
        if not present(db) or not _has_table(db, 'control_threads'):
            return None
        if db.execute('SELECT 1 FROM control_threads WHERE channel=? AND root_ts=?', (channel, root)).fetchone() is None:
            return None
        on, cfg = self.card_channel_on(rt)
        if not on:
            return None
        now = time.time() if now is None else now
        ts = str(m.get('ts') or '')
        if not self.typed(rt, db, channel, m, root):
            return {'ts': ts, 'outcome': 'skipped', 'reason': 'not_hand_typed_control_thread'}
        mid = self._record_slack(rt, db, cfg, channel, m, root, source, now)
        if mid:
            rt.start(_db_path(db))
        return {'ts': ts, 'outcome': 'control' if mid else 'duplicate', 'message_id': mid}

    def slack_poll(self, rt, db, now=None, force=False):
        """Supervisor step, every control.channel_poll_seconds (the receiver's push is the fast path), on the owner's
        user token. Per decision channel: conversations.history from its cursor for the owner's new top-level messages,
        paged (HISTORY_PAGES per poll; a longer gap goes on from Slack's page cursor next poll, and the cursor moves only
        once everything up to the walk's end is read); skipped while the receiver pushed within the interval, but at least
        every HISTORY_MAX_SKIP. Then conversations.replies for the control threads that are due (THREAD_ACTIVE,
        THREAD_HOT, THREAD_IDLE_POLL; at most THREAD_POLL_MAX), from each thread's cursor. Never before the entry
        first saw the channel; nothing while it is off. `force` (the CLI) reads everything that is active now."""
        if not present(db):  # the entry was never set up here (control-bootstrap creates the tables): nothing at all
            return None
        on, cfg = self.card_channel_on(rt)
        if not on:
            return None
        now = time.time() if now is None else now
        initialize(db)
        last = _meta_json(db, POLL_KEY).get('at') or 0.0
        if not force and now - last < cfg['channel_poll_seconds']:
            return None
        since = self._since(db, now)
        pushed = _meta_float(db, PUSH_KEY)
        out = {'at': now, 'recorded': 0, 'reads': 0, 'history': {}, 'threads': 0, 'error': None}
        owner = _owner_user(rt)
        token = None
        try:
            for channel in rt.card_threads.decision_channels(rt):
                key = f'control_cursor:{channel}'
                state = _meta_json(db, key)
                state['cursor'] = max(str(state.get('cursor') or f'{since:.6f}'), f'{since:.6f}', key=float)
                if not force and not state.get('walk') and now - pushed < cfg['channel_poll_seconds'] and \
                        now - (state.get('read_at') or 0) < HISTORY_MAX_SKIP:
                    out['history'][channel] = 'skipped_receiver_pushed'
                    continue
                token = token or rt.api_identity()
                out['history'][channel] = self._poll_history(rt, db, cfg, token, channel, key, state, owner, now, out)
            due = [dict(r) for r in db.execute('SELECT * FROM control_threads WHERE updated_at>? ORDER BY '
                                               'coalesce(polled_at,0), updated_at DESC', (now - THREAD_ACTIVE,))]
            due = [t for t in due if force or now - t['updated_at'] <= THREAD_HOT or
                   now - (t['polled_at'] or 0) >= THREAD_IDLE_POLL][:THREAD_POLL_MAX]
            for t in due:
                token = token or rt.api_identity()
                out['threads'] += 1
                self._poll_thread(rt, db, cfg, token, t, now, out)
        except Exception as exc:  # noqa: BLE001 - recorded; the next poll retries from the same cursors
            out['error'] = type(exc).__name__
        with _transaction(db):
            db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (POLL_KEY, json.dumps(out)))
        if out['recorded']:
            rt.start(_db_path(db))
        return out

    def _poll_history(self, rt, db, cfg, token, channel, key, state, owner, now, out):
        """One channel's new top-level messages, newest page first (Slack's order). A walk = every message in (cursor,
        latest]; it may span polls (Slack's page cursor kept in `walk`); the cursor moves to the newest ts read only
        when the walk is complete. Messages are recorded as soon as read (a re-read is a duplicate)."""
        walk = state.get('walk') or {'latest': f'{now:.6f}', 'page': None, 'high': state['cursor']}
        try:
            for _ in range(HISTORY_PAGES):
                params = {'channel': channel, 'oldest': state['cursor'], 'latest': walk['latest'], 'limit': PAGE_LIMIT,
                          'include_all_metadata': 'true', **({'cursor': walk['page']} if walk['page'] else {})}
                page = rt.slack.api_call('conversations.history', token, params=params)
                out['reads'] += 1
                for m in sorted((x for x in page.get('messages') or [] if isinstance(x, dict) and
                                 TS_RE.fullmatch(str(x.get('ts') or ''))), key=lambda x: float(x['ts'])):
                    if float(m['ts']) <= float(state['cursor']) or float(m['ts']) > float(walk['latest']):
                        continue
                    walk['high'] = max(walk['high'], m['ts'], key=float)
                    if m.get('user') != owner or m.get('thread_ts') not in (None, '', m['ts']):
                        continue
                    if self.typed(rt, db, channel, m) and self._record_slack(rt, db, cfg, channel, m, None, 'poll', now):
                        out['recorded'] += 1
                walk['page'] = (page.get('response_metadata') or {}).get('next_cursor') or None
                if not walk['page']:
                    if page.get('has_more'):
                        raise RuntimeError('incomplete_slack_pagination')
                    state = {'cursor': walk['high'], 'read_at': now}
                    return 'read'
            # the page cap: the walk goes on next poll; the cursor stays where everything before it is read
            state = {**state, 'walk': walk, 'read_at': now}
            out['error'] = out['error'] or 'history_page_cap'
            return 'page_cap'
        except Exception:
            if walk.get('page'):  # Slack's page cursor may be the problem (expired): the next walk starts again
                state = {k: v for k, v in state.items() if k != 'walk'}
            raise
        finally:
            with _transaction(db):
                db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (key, json.dumps(state, sort_keys=True)))

    def _poll_thread(self, rt, db, cfg, token, t, now, out):
        """The owner's replies in one control thread, oldest first (Slack's order), from its cursor; REPLY_PAGES per
        poll, the cursor saved after each page (the rest next poll)."""
        cursor = start = t['cursor'] or t['root_ts']
        page_cursor = None
        try:
            for _ in range(REPLY_PAGES):  # one query (oldest = the thread's cursor) paged with Slack's page cursor
                found = rt.slack.api_call('conversations.replies', token, params={
                    'channel': t['channel'], 'ts': t['root_ts'], 'oldest': start, 'limit': PAGE_LIMIT,
                    'include_all_metadata': 'true', **({'cursor': page_cursor} if page_cursor else {})})
                out['reads'] += 1
                for m in sorted((x for x in found.get('messages') or [] if isinstance(x, dict) and
                                 TS_RE.fullmatch(str(x.get('ts') or ''))), key=lambda x: float(x['ts'])):
                    if m['ts'] == t['root_ts'] or float(m['ts']) <= float(cursor):
                        continue
                    if self.typed(rt, db, t['channel'], m, t['root_ts']) and \
                            self._record_slack(rt, db, cfg, t['channel'], m, t['root_ts'], 'poll', now):
                        out['recorded'] += 1
                    cursor = m['ts']  # after it is handled: a failure leaves the cursor before it
                page_cursor = (found.get('response_metadata') or {}).get('next_cursor') or None
                if not page_cursor:
                    return
            out['error'] = out['error'] or 'replies_page_cap'
        finally:
            with _transaction(db):
                db.execute('UPDATE control_threads SET cursor=?, polled_at=? WHERE channel=? AND root_ts=?',
                           (cursor, now, t['channel'], t['root_ts']))

    def _slack_post(self, rt, db, m, text, kind):
        """A bot post in the thread of a Slack control conversation (the owner-only card channel, guarded like every
        owner-facing post); returns its ts."""
        channel, root = m['channel'], self._root(db, m)
        rt.buttons.require_private_destination(rt, db, channel, kind)
        sent = rt.slack.api_call('chat.postMessage', rt.api_identity(bot=True), json_body={
            'channel': channel, 'thread_ts': root, 'text': text, 'unfurl_links': False, 'unfurl_media': False,
            'metadata': rt.agent_metadata(kind, None, message=m['message_id'])})
        ts = (sent or {}).get('ts')
        rt.record_send(db, channel, ts, kind, m['message_id'])
        return ts

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
        blocked = self.dispatch_blocked(db, config, cfg)
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
            if blocked:
                self._paused_note(rt, db, m, blocked)
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
        blocked = self.dispatch_blocked(db, config, cfg)
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
                self._failed_note(rt, db, m, 'no_reply')
                out['failed'] += 1
            elif blocked and m['state'] == 'queued':
                # C4: dispatch cannot run the control task now (switched off, or the task paused): the queue's clock
                # waits with it, so the message is answered after the resume instead of failing meanwhile
                with _transaction(db):
                    db.execute("UPDATE control_messages SET updated_at=? WHERE message_id=? AND state='queued'",
                               (now, m['message_id']))
            elif (m['state'] == 'queued' and now - m['updated_at'] > limit) or \
                    (m['state'] == 'working' and now - m['created_at'] > 2 * limit):
                self._fail(db, m['message_id'], 'timeout', now)
                self._failed_note(rt, db, m, 'timeout')
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

    def dispatch_blocked(self, db, config, cfg):
        """C4: why the control task's run cannot start now — 'dispatch_off' (dispatch.enabled false) or 'task_paused'
        (the control task paused) — else None. dispatch.install_hold does not block it (the control entry keeps
        running through an install's quiet moment)."""
        if not (config.get('dispatch') or {}).get('enabled'):
            return 'dispatch_off'
        try:
            if db.execute('SELECT 1 FROM dispatch_pauses WHERE task_key=? AND resumed_at IS NULL', (cfg['task'],)).fetchone():
                return 'task_paused'
        except Exception:  # noqa: BLE001 - no pause table yet: nothing is paused
            pass
        return None

    def _paused_note(self, rt, db, m, why):
        """C4: a Slack message that cannot be dispatched now gets one short note in its thread at once (best effort),
        instead of a silent wait."""
        if m['source'] != 'slack':
            return
        try:
            self._slack_post(rt, db, m, self.msg('control.slack.paused', why=self.msg(f'control.paused.{why}')),
                             'control_paused')
        except Exception:  # noqa: BLE001 - the message still waits and is answered after the resume
            pass

    def _failed_note(self, rt, db, m, code):
        """C2: a Slack message that failed gets one short note in its thread (best effort)."""
        if m['source'] != 'slack':
            return
        try:
            self._slack_post(rt, db, m, self.msg('control.slack.failed', code=code), 'control_failed')
        except Exception:  # noqa: BLE001 - the console shows the failure anyway
            pass

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
        if m['state'] not in ('queued', 'working', 'answered') and not (m['state'] == 'failed' and m['error'] == 'timeout'):
            raise ValueError(f"message_is_{m['state']}")  # C4: a run reaching a timed-out message still answers it
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
        out = {'ok': True, 'message': self.public(db, m), 'history': self._history(db, m, cfg['history_turns'])}
        if m['source'] == 'slack':  # C2: the words re-read from Slack (never stored), the thread so far
            out['message']['text'] = self._original(m, rt)
            root = self._root(db, m)
            found = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
                'channel': m['channel'], 'ts': root, 'limit': 50, 'include_all_metadata': 'true'})
            out['thread'] = [{'role': 'owner' if x.get('user') == _owner_user(rt) and not x.get('bot_id') else 'other',
                              'ts': x.get('ts'), 'text': x.get('text')}
                             for x in found.get('messages') or [] if isinstance(x, dict) and
                             float(x.get('ts') or 0) <= float(m['ts'])]
        return out

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
        facts = {'followup': bool(followup)}
        stored = text
        if m['source'] == 'slack':  # C2: the answer goes into the message's thread (bot); C3: its text is stored too (the
            # run's own words, under the same retention as a console answer; the owner's Slack words never are)
            facts['slack_ts'] = self._slack_post(rt, db, m, text, 'control_reply')
        with _transaction(db):
            db.execute('INSERT INTO control_messages (message_id,conversation_id,source,role,text,digest,facts,state,'
                       "needs_answer,reply_to,smoke,created_at,updated_at) VALUES (?,?,?,'control',?,?,?,'answered',?,?,?,"
                       '?,?)', (rid, m['conversation_id'], m['source'], stored, digest(text),
                                json.dumps(facts), int(bool(needs_answer)), message_id, m['smoke'],
                                now, now))
            if not answered:
                db.execute("UPDATE control_messages SET state='answered',needs_answer=?,error=NULL,updated_at=? WHERE "
                           'message_id=?', (int(bool(needs_answer)), now, message_id))
            db.execute('UPDATE control_conversations SET updated_at=? WHERE conversation_id=?', (now, m['conversation_id']))
            if m['source'] == 'slack':  # the answer is a turn of the thread: the poll reads it as active again
                db.execute('UPDATE control_threads SET updated_at=? WHERE conversation_id=?', (now, m['conversation_id']))
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
        self._original(m, rt)  # the stored words must still be there and match their digest
        if m['source'] == 'console' or cfg['confirm_slack']:
            mode = 'event' if rt.registry.task_consumer(db, row['task_id']) else 'dispatch'
            return self._propose(rt, db, m, cfg, 'relay', now, task=row, note=note, mode=mode)
        return self._relay(rt, db, db_path, m, row, note, now)

    def _relay(self, rt, db, db_path, m, row, note, now):
        original = self._original(m, rt)
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
        self._original(m, rt)
        if m['source'] == 'console' or cfg['confirm_slack']:
            return self._propose(rt, db, m, cfg, 'launch', now, title=title, linear=linear)
        return self._launch(rt, db, db_path, m, cfg, title, linear, now)

    def _launch(self, rt, db, db_path, m, cfg, title, linear, now):
        body = self._relay_body(m, self._original(m, rt), None, now)
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
        words = self._original(m, rt)
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
    def overview(self, db, config, conversation=None, limit=40, words=None):
        """The console's view. `words` (C3, the dashboard's SlackWords): a Slack conversation's messages whose text is
        not stored get it re-read from Slack, in memory only (see `text_state`)."""
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
            thread = db.execute('SELECT channel, root_ts FROM control_threads WHERE conversation_id=?',
                                (conversation,)).fetchone() if _has_table(db, 'control_threads') else None
            if thread is not None:  # C2: a conversation the owner started in the card channel
                out['conversation']['slack'] = {'channel': thread[0], 'root': thread[1]}
            rows = [dict(r) for r in db.execute(
                'SELECT * FROM control_messages WHERE conversation_id=? ORDER BY rowid', (conversation,))]  # as written
            out['messages'] = [self.public(db, r) for r in rows]
            if thread is not None:
                out['conversation']['words'] = self._fill_words(rows, out['messages'], thread[0], thread[1], words)
        return out

    def _fill_words(self, rows, messages, channel, root, words):
        """C3: the text of a Slack conversation's messages that are not stored (the owner's words; answers from before
        C3 or past retention), re-read from Slack through `words` with the owner's token. Each message gets
        `text_state`: slack (read now), slack_edited (changed in Slack since it was recorded: the current text, never
        relayed), unreadable (无法读取原文), busy (the read budget is used up: `retry_after`). Returns the read's
        summary for the conversation."""
        want = {}
        for r, p in zip(rows, messages):
            if p['text_state'] != 'in_slack':
                continue
            ts = r['ts'] if r.get('channel') and r.get('ts') else json.loads(r['facts'] or '{}').get('slack_ts')
            if ts and TS_RE.fullmatch(str(ts)):
                want[str(ts)] = (r, p)
            else:
                p['text_state'] = 'unreadable'
        if not want:
            return {'state': 'none'}
        if words is None:
            return {'state': 'not_read'}
        try:
            found, info = words.thread(channel, root)
        except WordsUnavailable as exc:
            for r, p in want.values():
                p['text_state'] = exc.state
                if exc.retry_after:
                    p['retry_after'] = exc.retry_after
            return {'state': exc.state, 'error': exc.code, 'retry_after': exc.retry_after}
        for ts, (r, p) in want.items():
            m = found.get(ts)
            if m is None:
                p['text_state'] = 'unreadable'
                continue
            raw = str(m.get('text') or '')
            plain = slack_plain(raw)
            p['text'] = plain[:MAX_REPLY]
            if r['role'] != 'owner':  # C3.1: the run's own words as Slack keeps them (link markup, escaping): never 'edited'
                p['text_state'] = 'slack'
            else:  # the owner's words: unchanged when the raw text, or the same words as typed, match what was recorded
                recorded = {r['digest'], json.loads(r['facts'] or '{}').get('plain_digest')}
                p['text_state'] = 'slack' if recorded & {digest(raw), digest(plain)} else 'slack_edited'
        return info

    def public(self, db, m):
        """A message as the console shows it: text, state, request facts, relays (task, mode, time), error."""
        out = {'id': m['message_id'], 'conversation': m['conversation_id'], 'role': m['role'], 'source': m['source'],
               'text': m['text'], 'expired': m['text'] is None, 'state': m['state'],
               'needs_answer': bool(m['needs_answer']), 'reply_to': m['reply_to'], 'error': m['error'],
               'run_id': m['run_id'], 'facts': {k: v for k, v in json.loads(m['facts'] or '{}').items() if k != 'plain_digest'},
               'created_at': _iso(m['created_at']),
               'updated_at': _iso(m['updated_at']), 'relays': []}
        # C3: where the text comes from: stored (console words, the run's answers), expired (past retention), in_slack (a
        # Slack message whose text is not stored: the owner's words, or an answer from before C3 / past retention;
        # the dashboard re-reads it, see _fill_words)
        out['text_state'] = 'stored' if m['text'] is not None else 'expired'
        if m.get('channel') and m.get('ts'):  # C2: a Slack message (the console links to it; its text stays in Slack)
            out['slack'] = {'channel': m['channel'], 'ts': m['ts']}
            out['expired'] = False
            out['text_state'] = 'in_slack'
        elif m['source'] == 'slack':
            out['slack'] = {'reply_ts': json.loads(m['facts'] or '{}').get('slack_ts')}
            out['expired'] = False
            if m['text'] is None:
                out['text_state'] = 'in_slack'
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

    def _original(self, m, rt=None):
        """The owner's words, checked against the digest recorded with them: console text from the store; a Slack
        message re-read from Slack (its text is never stored)."""
        if m['source'] == 'slack':
            if rt is None:
                raise ValueError('slack_message_needs_the_runtime')
            found = self._read(rt, m['channel'], m['ts'], self._root(None, m))
            text = (found or {}).get('text')
            if text is None:
                raise ValueError('slack_message_not_found')
            if digest(text) != m['digest']:
                raise ValueError('stored_text_does_not_match_its_digest')
            return text
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
        if facts.get('source') == 'slack':
            return self.msg('control.facts.slack', via=facts.get('via') or '-')
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
                words = h['text'] if h['text'] is not None else self.msg(
                    'control.body.slack_turn' if m['source'] == 'slack' else 'control.body.expired')
                lines.append(f"- {who} ({h['at']}): " + ' '.join(str(words).split())[:600])
            lines.append('')
        if m['source'] == 'slack':  # C2: the words stay in Slack; the run reads them with control-show
            lines += [self.msg('control.body.slack_message', name=self.name, cli=cli, id=m['message_id'],
                               link=_permalink(rt, m['channel'], m['ts'])), '']
        else:
            lines += [self.msg('control.body.message_begin', name=self.name), m['text'], self.msg('control.body.message_end'),
                      '']
        lines += [self.msg('control.body.how', cli=cli, id=m['message_id'], name=self.name)]
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


def slack_plain(raw):
    """A message's text as it was typed, from the form Slack stores (C3.1). Every <…> in Slack's stored text is markup
    (a typed < > & arrives escaped), so each becomes plain text, then &amp; &lt; &gt; are undone:
    - links: <url> -> url; <url|label> -> the label when it is the url (with or without its scheme), else 'label (url)';
    - people and channels: <@U…> -> @U…, <@U…|name> -> @name; <#C…|name> -> #name (#C… without a name);
    - <!here> / <!channel> / <!everyone> -> @here / @channel / @everyone; <!subteam^S…|@team> -> its label;
      <!date^…|fallback> -> the fallback; any other <!…|label> -> the label.
    No markup form leaves a < or >; the result is plain text (the page escapes it)."""
    def token(m):
        body, _, label = m.group(1).partition('|')
        if body[:2] in ('@U', '@W', '@B'):
            return '@' + (label or body[1:])
        if body[:2] in ('#C', '#G', '#D'):
            return '#' + (label or body[1:])
        if body.startswith('!'):
            word = body[1:]
            if word in ('here', 'channel', 'everyone'):
                return '@' + word
            if word.startswith('subteam^'):
                return label or '@' + word.split('^', 1)[1]
            if word.startswith('date^'):
                return label or word.split('^')[1]
            return label or '@' + word
        if SLACK_URL_RE.match(body):
            if not label or label == body or label.rstrip('/') == body.split(':', 1)[-1].lstrip('/').rstrip('/'):
                return label or body
            return f'{label} ({body})'
        return label or body
    return html.unescape(SLACK_MARKUP_RE.sub(token, str(raw)))


class WordsUnavailable(Exception):
    """No Slack words for the console now: `state` unreadable (无法读取原文) or busy (try again in `retry_after` s)."""

    def __init__(self, state, code, retry_after=None):
        super().__init__(code)
        self.state, self.code, self.retry_after = state, code, retry_after


class SlackWords:
    """C3: Slack conversations' words for the console, re-read on demand with the owner's user token (read-only: one
    conversations.replies of the conversation's thread), held only in this process's memory for WORDS_TTL (a failed
    read WORDS_ERROR_TTL); at most WORDS_READS_PER_MINUTE reads a minute, one at a time per thread, and a 429 pauses
    every read for its Retry-After. Nothing is written anywhere. `reader` has authorize() and request(method, params)
    (the dashboard's preview service: the owner's user token, its identity checked)."""

    def __init__(self, reader, clock=time.time):
        self.reader, self.clock = reader, clock
        self.lock = threading.Lock()
        self.cache = OrderedDict()   # (channel, root) -> (expires, ({ts: message}, info) | WordsUnavailable)
        self.reads = deque()         # times of the reads of the last minute
        self.cooldown = 0.0
        self.inflight = set()

    def thread(self, channel, root):
        """({ts: message}, info) of the thread rooted at `root`, from memory or one read; raises WordsUnavailable."""
        if not (CHANNEL_RE.fullmatch(str(channel)) and TS_RE.fullmatch(str(root))):
            raise WordsUnavailable('unreadable', 'invalid_thread')
        key, now = (channel, str(root)), self.clock()
        with self.lock:
            for k, (expires, _) in list(self.cache.items()):
                if expires <= now:
                    self.cache.pop(k, None)
            hit = self.cache.get(key)
            if hit is not None:
                if isinstance(hit[1], WordsUnavailable):
                    raise hit[1]
                return hit[1][0], {**hit[1][1], 'state': 'cached'}
            if self.cooldown > now:
                raise WordsUnavailable('busy', 'ratelimited', max(1, int(self.cooldown - now + 0.999)))
            while self.reads and self.reads[0] <= now - 60:
                self.reads.popleft()
            if len(self.reads) >= WORDS_READS_PER_MINUTE:
                raise WordsUnavailable('busy', 'read_budget', max(1, int(self.reads[0] + 60 - now + 0.999)))
            if key in self.inflight:
                raise WordsUnavailable('busy', 'read_in_progress', 2)
            self.reads.append(now)
            self.inflight.add(key)
        entry = None
        try:
            self.reader.authorize()
            page = self.reader.request('conversations.replies', {'channel': channel, 'ts': str(root), 'oldest': str(root),
                                                                 'inclusive': True, 'limit': WORDS_PAGE})
            found = {str(m['ts']): m for m in page.get('messages') or []
                     if isinstance(m, dict) and TS_RE.fullmatch(str(m.get('ts') or ''))}
            if str(root) not in found:
                raise WordsUnavailable('unreadable', 'message_not_found')
            info = {'state': 'read', 'read_at': _iso(now), 'expires_at': _iso(now + WORDS_TTL),
                    'partial': bool(page.get('has_more'))}
            entry = (now + WORDS_TTL, (found, info))
            return found, info
        except WordsUnavailable as exc:
            entry = (now + WORDS_ERROR_TTL, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - the page says 无法读取原文; the code names the kind only
            code = getattr(exc, 'code', None)
            if code == 429 or code == 'ratelimited' or 'ratelimited' in str(exc):
                try:
                    delay = max(WORDS_ERROR_TTL, int(float(exc.headers.get('Retry-After', 60))))
                except (AttributeError, TypeError, ValueError):
                    delay = 60
                with self.lock:
                    self.cooldown = max(self.cooldown, self.clock() + delay)
                error = WordsUnavailable('busy', 'ratelimited', delay)
            else:
                delay = WORDS_ERROR_TTL
                error = WordsUnavailable('unreadable', code if isinstance(code, str) and re.fullmatch(
                    r'[a-z_]{1,60}', code) else type(exc).__name__)
            entry = (now + delay, error)
            raise error from None
        finally:
            with self.lock:
                self.inflight.discard(key)
                if entry is not None:
                    self.cache[key] = entry
                    self.cache.move_to_end(key)
                    while len(self.cache) > WORDS_MAX_CACHED:
                        self.cache.popitem(last=False)


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


def _permalink(rt, channel, ts):
    try:
        base = rt.DEPLOYMENT.owner.slack_workspace_url
    except AttributeError:
        base = None
    if not base or not channel or not ts:
        return f'{channel}/{ts}'
    return f"{base}/archives/{channel}/p{str(ts).replace('.', '')}"


def _code(exc):
    text = str(exc)
    return text if text and len(text) <= 120 and all(c.isalnum() or c in '_:-.' for c in text) else type(exc).__name__


def _meta_json(db, key):
    """A runtime_meta JSON object ({} when missing or not an object)."""
    row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (key,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _meta_float(db, key):
    row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (key,)).fetchone()
    try:
        return float(row[0]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


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
