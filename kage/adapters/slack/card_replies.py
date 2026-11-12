"""Typed replies in a decision card's Slack thread (code, never the model).

- The owner's first hand-typed reply in an undecided card's thread retires the card's buttons (bot `chat.update`) and
  adds one line quoting its start; a card that waits for a verification code never quotes it. Once per card.
- A reply that STARTS with an unambiguous approve / reject phrase or an exact option label, and maps to exactly one
  option (`rt.buttons.parse_reply`), is recorded as the card's decision by the runtime (`rt.thread_reply_decision`,
  provenance thread_reply): the same receipt, decision event and card update as a click. The rest of the text is
  guidance the owner session reads in the thread; it is never stored and never makes an approval conditional.
- Code-owned cards (supervisor cards) have no watch: `settle` polls their threads and hands every reply to
  card_threads.route_reply, which decides / retires as here and delivers the text to the task the card is about.
- Only the owner's own hand-typed messages count (envelope trust = the owner key, authority = `<key>_intent`: agents'
  posts with the owner's token carry the assistant's app id, hidden metadata or a send-ledger entry). Delegates,
  colleagues and bots never retire buttons or decide. Kill switches: cards.retire_buttons_on_reply and
  cards.thread_reply_decisions.
- A card is OPEN (keyword-decidable, retirable, fast-polled) only while undecided, its decision watch not stopped and
  younger than OPEN_CARD_DAYS, and not continued by a newer card; a reply only concerns open cards posted before it.
  An open card's thread is polled at most every cards.open_poll_seconds.
- cards.retire_on plan_change: a reply that is not a clear decision retires an agent card's buttons only when it changes
  the plan (`rt.buttons.reply_class`); a question or unclear text keeps them. Every routed reply's class (decided /
  plan_change / kept) is recorded in card_reply_routes.reply_class.

`rt` is the runtime: its Slack client and identities, transactions, decision recording, card update, watch helpers and
the sibling modules (`rt.buttons`, `rt.envelopes`, `rt.card_threads`, `rt.worker_config`).
"""
from __future__ import annotations
import fnmatch
import json
import re
import time
from kage.core import control as _control

DDL = '''
  CREATE TABLE IF NOT EXISTS card_replies (
    event_id TEXT PRIMARY KEY, reply_ts TEXT, stage TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
    decided_ts TEXT, cursor TEXT, polled_at REAL, updated_at REAL NOT NULL);
  -- P38: every reply an approvals watch routed, whatever the outcome (once per reply; the card it went to, if any).
  CREATE TABLE IF NOT EXISTS card_reply_routes (
    channel TEXT NOT NULL, reply_ts TEXT NOT NULL, root TEXT NOT NULL, outcome TEXT NOT NULL, card_event TEXT,
    open_cards TEXT, at REAL NOT NULL, PRIMARY KEY (channel, reply_ts));
  -- 0.78.8 (F20): the owner's card-thread replies posted through an allowed app with their own user token (provenance)
  CREATE TABLE IF NOT EXISTS owner_api_replies (
    channel TEXT NOT NULL, ts TEXT NOT NULL, user TEXT NOT NULL, app TEXT NOT NULL, at REAL NOT NULL,
    PRIMARY KEY (channel, ts));
'''
RETRY_ATTEMPTS = 3
ROUTES_DAYS = 30         # P38: card_reply_routes kept this long (re-returned boundary replies + fresh-session context)
CODE_CARD_DAYS = 7       # code-owned card threads are polled this long (a decided card stops at once)
OPEN_CARD_DAYS = 7       # P39 (F22): an undecided card older than this no longer counts as open (buttons still work)
CLAIM_STALE = 120        # P39: a `routing` claim older than this died with its process (a killed push): taken again
SINCE_KEY = 'card_replies_since'   # runtime_meta: replies typed before this code ran never count as decisions
CODE_POLL = ((3600, 30), (86400, 120), (None, 600))   # (card age below, poll every N seconds)
TS_RE = re.compile(r'\d+\.\d+')


def initialize(db):
    db.executescript(DDL)
    db.execute('INSERT OR IGNORE INTO runtime_meta VALUES (?,?)', (SINCE_KEY, f'{time.time():.6f}'))


def config(rt):
    try:
        return rt.worker_config.load()[0]['cards']
    except Exception:  # noqa: BLE001 - a malformed config never turns text into decisions, reacts, or threads cards
        return {**rt.worker_config.DEFAULTS['cards'], 'thread_reply_decisions': False, 'read_receipts': False,
                'task_thread': False}


def open_poll_seconds(rt, db, w, cfg=None):
    """Poll cap of a decision-card thread still waiting for the owner (no recorded decision), else None (normal ladder).

    P3.2: the task-level approvals watch inherits the fast cap while any card in its thread is still open; a card whose
    thread is handled by such an approvals watch does not fast-poll on its own (the approvals watch covers it)."""
    if w.get('state') != 'running':
        return None
    cfg = cfg or config(rt)
    seconds = cfg['open_poll_seconds']
    if not seconds:
        return None
    if w.get('kind') == rt.APPROVALS_KIND:
        return seconds if open_cards(rt, db, w['channel'], w['thread']) else None
    if w.get('kind') != 'decision':
        return None
    row = db.execute('SELECT c.channel, c.thread_root FROM escalations x JOIN escalation_cards c ON c.event_id=x.event_id'
                     ' WHERE x.watch_id=? AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.event_id=x.event_id)',
                     (w['id'],)).fetchone()
    if row is None:
        return None
    if rt._approvals_watch_active(db, row['channel'], row['thread_root']):
        return None  # the approvals watch fast-polls the whole thread instead
    return seconds


CARD_SQL = ('SELECT x.event_id, x.stage, x.thread_ts, x.watch_id, c.channel, c.options, c.never_auto, c.created, '
            'c.smoke, c.thread_root, c.task_id FROM escalations x JOIN escalation_cards c ON c.event_id=x.event_id ')
# P39 (F22, shared with the approvals fast poll and card_threads): an OPEN card — undecided, its decision watch not
# stopped (closed by its owner session, a resolved blocker), younger than OPEN_CARD_DAYS. Parameters: (supervisor stage, since).
OPEN_SQL = ("x.stage IN ('watching', ?) AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.event_id=c.event_id) AND "
            "c.created>? AND NOT EXISTS (SELECT 1 FROM watches cw WHERE cw.id=x.watch_id AND "
            "json_extract(cw.data,'$.state')='stopped')")


def card(db, eid):
    return db.execute(CARD_SQL + 'WHERE x.event_id=?', (eid,)).fetchone()


def open_cards(rt, db, channel, root, before=None, now=None):
    """P39 (F22): the OPEN cards of one card thread (newest first) — a card threaded under `root`, or the root card
    itself. `before` (a reply's ts): only cards already posted when that reply was typed (P38 F01)."""
    now = time.time() if now is None else now
    rows = db.execute(CARD_SQL + 'WHERE c.channel=? AND coalesce(c.thread_root, x.thread_ts)=? AND ' + OPEN_SQL +
                      ' ORDER BY c.created DESC, x.thread_ts DESC',
                      (channel, root, rt.SUPERVISOR_STAGE, now - OPEN_CARD_DAYS * 86400)).fetchall()
    return [r for r in rows if r['thread_ts'] and TS_RE.fullmatch(str(r['thread_ts'])) and
            (before is None or float(r['thread_ts']) < float(before)) and not continued(rt, db, r['event_id'], before)]


def continued(rt, db, eid, before=None):
    """P46: a newer live card continues this one (`--about` it) — posted before the reply `before`, when given. Such a
    card no longer waits for the owner and is not open: their typed answer in the thread belongs to the new card."""
    at = db.execute("SELECT min(CAST(x.thread_ts AS REAL)) FROM escalation_cards n JOIN escalations x ON "
                    "x.event_id=n.event_id WHERE n.about=? AND x.stage IN ('watching', ?)",
                    (eid, rt.SUPERVISOR_STAGE)).fetchone()[0]
    return at is not None and (before is None or at < float(before))


def strict(db, eid):
    """An intake draft card's approve option sends the draft verbatim: extra text never counts as that approval."""
    try:
        row = db.execute('SELECT kind FROM intake_briefs WHERE card_event=?', (eid,)).fetchone()
    except Exception:  # noqa: BLE001 - no intake table: not a draft card
        return False
    return bool(row and row['kind'] == 'draft')


def _secret(rt, db, row):
    # P38: also a card sharing its thread with a card that waits for a verification code (a pre-P38 thread): a reply
    # there is never quoted onto a card.
    if rt._secret_thread(db, row['channel'], row['thread_root'] or row['thread_ts']):
        return True
    if not row['watch_id']:
        return False
    try:
        return bool(rt.load(db, row['watch_id']).get('secret_input'))
    except ValueError:
        return False


def route_one(rt, db, channel, root, m, now, cfg):
    """One of the owner's replies in card thread `root` against its OPEN cards (F22) that were already posted when they typed
    (F01): the newest takes it — a clear choice decides it when it is the only one open, other text retires its buttons —
    else pure guidance. Returns (result, [open card event ids]). No dedupe here: callers claim the reply first."""
    cards = open_cards(rt, db, channel, root, before=m['ts'], now=now)
    if not cards:
        return {'ts': m['ts'], 'action': 'guidance_no_open_card'}, []
    row = cards[0]  # the newest card that was already open when the owner typed
    result = handle(rt, db, row, m, now, cfg, secret=_secret(rt, db, row),
                    code_owned=row['stage'] == rt.SUPERVISOR_STAGE, allow_decision=len(cards) == 1)
    return {**result, 'code_owned': row['stage'] == rt.SUPERVISOR_STAGE}, [r['event_id'] for r in cards]


def claimed(db, channel, ts, now=None):
    """P39: a path already routed (or is routing) this reply — a `routing` claim older than CLAIM_STALE is dead (its
    process was killed between the claim and the delivery) and may be taken again."""
    now = time.time() if now is None else now
    row = db.execute('SELECT outcome, at FROM card_reply_routes WHERE channel=? AND reply_ts=?', (channel, ts)).fetchone()
    return row is not None and not (row['outcome'] == 'routing' and row['at'] < now - CLAIM_STALE)


def _record_route(rt, db, channel, reply_ts, root, result, open_cards, now):
    with rt.transaction(db):
        db.execute('INSERT OR REPLACE INTO card_reply_routes (channel,reply_ts,root,outcome,card_event,open_cards,at,'
                   'reply_class) VALUES (?,?,?,?,?,?,?,?)',
                   (channel, reply_ts, root, str(result.get('action')), result.get('event_id'), json.dumps(open_cards),
                    now, result.get('reply_class')))


def routed(db, channel, ts):
    """P38 (F25): how an approvals watch routed the owner's reply (channel, ts) — the card it went to and the cards that
    were open then — for the event context of a fresh session that has no memory of the thread. None if not routed.
    P39: also the task the card thread is about and how the reply got here (card_threads.route_reply)."""
    try:
        row = db.execute('SELECT * FROM card_reply_routes WHERE channel=? AND reply_ts=?', (channel, ts)).fetchone()
    except Exception:  # noqa: BLE001 - a pre-P38 database without the table
        return None
    if row is None:
        return None
    card_ts = None
    if row['card_event']:
        found = db.execute('SELECT thread_ts FROM escalations WHERE event_id=?', (row['card_event'],)).fetchone()
        card_ts = found[0] if found else None
    out = {'outcome': row['outcome'], 'card_event': row['card_event'], 'card_ts': card_ts, 'root': row['root'],
           'open_cards': json.loads(row['open_cards'] or '[]')}
    try:  # P39: where card_threads delivered it and why (a database from before P39 has no such table)
        target = db.execute('SELECT * FROM card_reply_targets WHERE channel=? AND reply_ts=?', (channel, ts)).fetchone()
    except Exception:  # noqa: BLE001
        target = None
    if target is not None:
        out.update({k: target[k] for k in ('task_id', 'source', 'reason', 'card_task') if target[k]})
    return out


def handle(rt, db, row, m, now, cfg, secret=False, code_owned=False, allow_decision=True):
    """One verified reply of the owner: a clear choice becomes the decision; otherwise the buttons retire once. P3.2:
    `allow_decision=False` (several cards open in the thread) retires the buttons but leaves the decision to confirm.
    P41: with cards.retire_on plan_change only a clear choice or a change of plan retires an agent card's buttons;
    `reply_class` (decided / plan_change / kept) is returned for card_reply_routes (none for a verification-code card)."""
    eid = row['event_id']
    if db.execute('SELECT 1 FROM decisions WHERE event_id=?', (eid,)).fetchone():
        return {'event_id': eid, 'ts': m['ts'], 'action': 'already_decided'}
    if allow_decision and _control.button_only(db, eid):
        allow_decision = False  # Phase 2 C1b: only the owner's click decides this card (a control confirm card)
    parsed = rt.buttons.parse_reply(m.get('text'), rt.buttons.card_model(row['options']), strict=strict(db, eid))
    klass = None if secret else 'decided' if parsed['option'] is not None else rt.buttons.reply_class(m.get('text'))
    if parsed['option'] is not None and allow_decision and cfg['thread_reply_decisions']:
        result = rt.thread_reply_decision(db, eid, m['ts'], now=now)
        if result.get('ok'):
            with rt.transaction(db):
                db.execute('INSERT INTO card_replies (event_id,reply_ts,stage,decided_ts,updated_at) VALUES '
                           "(?,?,'decided',?,?) ON CONFLICT(event_id) DO UPDATE SET stage='decided',"
                           'decided_ts=excluded.decided_ts,reply_ts=coalesce(card_replies.reply_ts,excluded.reply_ts),'
                           'updated_at=excluded.updated_at', (eid, m['ts'], m['ts'], now))
            return {'event_id': eid, 'ts': m['ts'], 'action': 'decided', 'option_id': result.get('option_id'),
                    'kind': result.get('kind'), 'guidance': parsed['guidance'], 'reply_class': klass}
        parsed = {**parsed, 'reason': 'not_recorded:' + str(result.get('reason'))}
    if not cfg['retire_buttons_on_reply']:
        return {'event_id': eid, 'ts': m['ts'], 'action': 'retire_disabled', 'reason': parsed['reason'],
                'reply_class': klass}
    if klass == 'kept' and not code_owned and cfg.get('retire_on', 'plan_change') == 'plan_change':
        # P41: a question / background request / unclear text: the card is not touched, the buttons stay.
        return {'event_id': eid, 'ts': m['ts'], 'action': 'kept', 'reason': parsed['reason'], 'reply_class': klass}
    plan_change = klass == 'plan_change' and cfg.get('retire_on', 'plan_change') == 'plan_change'
    return {**retire(rt, db, row, m, now, secret, code_owned, plan_change), 'reason': parsed['reason'],
            'reply_class': klass}


def retire(rt, db, row, m, now, secret=False, code_owned=False, plan_change=False, line=None):
    """Once per card: buttons and hint off (code-owned cards keep them), one reply line on. Best effort. P42: `line`
    replaces the reply line (a lapsed deadline card: no reply, `m` carries no ts)."""
    eid = row['event_id']
    with rt.transaction(db):
        state = db.execute('SELECT * FROM card_replies WHERE event_id=?', (eid,)).fetchone()
        if state is not None and (state['stage'] in ('retired', 'decided') or
                                  (state['stage'] == 'failed' and state['attempts'] >= RETRY_ATTEMPTS)):
            return {'event_id': eid, 'ts': m['ts'], 'action': 'already_' + state['stage']}
        if state is not None and state['reply_ts'] and state['reply_ts'] != m['ts']:
            return {'event_id': eid, 'ts': m['ts'], 'action': 'retry_pending'}  # the first reply is retried by settle
        db.execute('INSERT INTO card_replies (event_id,reply_ts,stage,attempts,updated_at) VALUES (?,?,?,1,?) '
                   "ON CONFLICT(event_id) DO UPDATE SET stage='retiring',attempts=card_replies.attempts+1,"
                   'reply_ts=coalesce(card_replies.reply_ts,excluded.reply_ts),updated_at=excluded.updated_at',
                   (eid, m['ts'], 'retiring', now))
    model = rt.buttons.card_model(row['options'])
    try:
        user_token, bot_token = rt.api_identity(), rt.api_identity(bot=True)
        message = rt.slack.api_call('conversations.replies', user_token, params={
            'channel': row['channel'], 'ts': row['thread_ts'], 'limit': 1})['messages'][0]
        if message.get('ts') != row['thread_ts'] or message.get('user') != rt.BOT:
            raise RuntimeError('card_readback_failed')
        line = line or rt.buttons.reply_line(rt.buttons.excerpt(m.get('text')), secret=secret, code_owned=code_owned,
                                             labels=[o['label'] for o in model], plan_change=plan_change)
        rt.slack.api_call('chat.update', bot_token, json_body={
            'channel': row['channel'], 'ts': row['thread_ts'], 'text': message.get('text', ''),
            'blocks': rt.buttons.retired_blocks(message.get('blocks'), line, keep_buttons=code_owned)})
        stage, error = 'retired', None
    except Exception as exc:  # noqa: BLE001 - retried by settle (RETRY_ATTEMPTS)
        stage, error = 'failed', rt.safe_error(exc)
    with rt.transaction(db):
        db.execute('UPDATE card_replies SET stage=?,error=?,updated_at=? WHERE event_id=?', (stage, error, now, eid))
    if stage == 'retired' and db.execute('SELECT 1 FROM decisions WHERE event_id=?', (eid,)).fetchone():
        # A click landed between our read and our update: put its outcome back on the card.
        rt._update_card(db, eid, model, user_token, bot_token)
    return {'event_id': eid, 'ts': m['ts'], 'action': stage, 'error': error}


def _read_reply(rt, row, ts):
    found = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
        'channel': row['channel'], 'ts': row['thread_ts'], 'oldest': ts, 'latest': ts, 'inclusive': 'true',
        'limit': 2, 'include_all_metadata': 'true'})
    return next((x for x in found.get('messages', []) if x.get('ts') == ts), None)


def _poll_every(age):
    for limit, seconds in CODE_POLL:
        if limit is None or age < limit:
            return seconds
    return CODE_POLL[-1][1]


class CardReplies:
    """Reading the owner's own replies: `owner_typed` (the owner key is the envelope trust it needs) and the
    per-card, approvals-thread and code-owned-card pollers that call it."""

    def __init__(self, owner_key, owner_user):
        self.key, self.owner_user = owner_key, owner_user
        self._people_cache = {}  # 0.78.8: user id -> (monotonic time, is a person), for owner_api_post

    def owner_typed(self, rt, db, channel, m, card_ts):
        """The owner's own hand-typed reply in this card's thread (0.77.0: or an owner-like approver's, see
        kage.core.authority.owner_like_users); never agents' posts with the owner's token, other apps or bots. 0.78.8
        (F20): or their own reply through an allowed app with their user token (`owner_api_post`)."""
        ts = str(m.get('ts') or '')
        user = m.get('user')
        if not user or (user != self.owner_user and user not in _control.owner_like(rt)) or \
                m.get('thread_ts') != card_ts or not TS_RE.fullmatch(ts) or \
                float(ts) <= float(card_ts):
            return False
        ledger = db.execute('SELECT 1 FROM agent_sends WHERE channel=? AND ts=?', (channel, ts)).fetchone() is not None
        env = rt.envelopes.classify_message(m, thread_root=card_ts, ledger_hit=ledger, people=rt.envelopes.load_people())
        if env.get('rule') in ('agent_foreign_app', 'system_bot') and (m.get('bot_id') or m.get('app_id')):
            return self.owner_api_post(rt, db, channel, m) is not None  # never ours: our app / marker / ledger came first
        return env['author_kind'] == 'human' and env['trust'] == self.key and env['authority'] == f'{self.key}_intent'

    def owner_api_post(self, rt, db, channel, m, ttl=600, clock=time.monotonic):
        """0.78.8 (F20): the app's name when `m` is the owner's (or an owner-like approver's) own message posted through
        an app with their user token, as F14 for intake: a bot-identity deployment (`intake.identity: bot`, so no agent
        posts with the owner's token), not our app / marker / send ledger (the caller's envelope says a foreign app), a
        plain message, the app's name allowed by `intake.delegate_bot_names`, and users.info says a person (cached
        `ttl` s). Recorded in owner_api_replies (the decision's provenance says via api). Else None."""
        user, ts = str(m.get('user') or ''), str(m.get('ts') or '')
        if m.get('subtype') not in (None, 'thread_broadcast') or \
                (user != self.owner_user and user not in _control.owner_like(rt)):
            return None
        try:
            intake = rt.worker_config.load()[0]['intake']
        except Exception:  # noqa: BLE001 - a broken config grants nothing
            return None
        if intake.get('identity') != 'bot':
            return None
        profile = m.get('bot_profile') if isinstance(m.get('bot_profile'), dict) else {}
        name = str(profile.get('name') or m.get('username') or '')
        if not name or not any(fnmatch.fnmatchcase(name.lower(), str(n).lower())
                               for n in intake.get('delegate_bot_names') or ()):
            return None
        hit = self._people_cache.get(user)
        if hit is None or clock() - hit[0] >= ttl:
            try:
                info = rt.slack.api_call('users.info', rt.api_identity(bot=True), params={'user': user}).get('user') or {}
                person = info.get('is_bot') is False and not info.get('deleted')
            except Exception:  # noqa: BLE001 - asked again next time; nothing counts meanwhile
                return None
            hit = self._people_cache[user] = (clock(), person)
        if not hit[1]:
            return None
        try:
            row = (channel, ts, user, name[:80], time.time())
            if db.in_transaction:  # the caller's transaction commits it
                db.execute('INSERT OR IGNORE INTO owner_api_replies (channel,ts,user,app,at) VALUES (?,?,?,?,?)', row)
            else:
                with rt.transaction(db):
                    db.execute('INSERT OR IGNORE INTO owner_api_replies (channel,ts,user,app,at) VALUES (?,?,?,?,?)', row)
        except Exception:  # noqa: BLE001 - the reply still counts; only its provenance line is missing
            pass
        return name

    def process_watch(self, rt, db, w, messages, now=None):
        """tick(), after ingest: the owner's new replies in a decision watch's card thread.

        P3.2: when the card belongs to a task approvals thread with a live task-level watch, that watch (process_thread)
        is the single authority for the whole thread (newest-open-card routing, dedupe), so this per-card path stands down."""
        now = time.time() if now is None else now
        if w.get('kind') != 'decision' or not messages or not w.get('thread'):
            return []
        row = db.execute(CARD_SQL + 'WHERE x.watch_id=? AND x.thread_ts=?', (w['id'], w['thread'])).fetchone()
        if row is None:
            return []
        if rt._approvals_watch_active(db, row['channel'], row['thread_root']):
            return []
        cfg = config(rt)
        out = []
        for channel, m in sorted(messages, key=lambda item: float(item[1].get('ts') or 0)):
            if channel == row['channel'] and self.owner_typed(rt, db, channel, m, row['thread_ts']):
                if claimed(db, channel, m['ts'], now):
                    continue  # P39: the realtime push / reconcile sweep already handled this reply (one reply, once)
                result = handle(rt, db, row, m, now, cfg, secret=bool(w.get('secret_input')) or _secret(rt, db, row))
                _record_route(rt, db, channel, m['ts'], row['thread_root'] or row['thread_ts'], result, [row['event_id']],
                              now)  # P41: the per-card path records its reply's class too
                out.append(result)
        return out

    def process_thread(self, rt, db, w, messages, now=None):
        """tick(), after ingest, for a task's approvals-thread watch: route each of the owner's new replies in the thread.

        A clear approve / reject keyword maps to the one still-open card (the newest), unless several cards are open at once
        (then it is unclear which card the owner means: the buttons of the newest retire, the reply is delivered as guidance, and
        the owner session confirms it with propose-decision --meaning). Other text just retires the newest card's buttons and
        reaches the current session as guidance. A reply on a thread whose cards are all decided is pure guidance.

        P38 (F01): a reply only ever concerns cards that were already posted when the owner typed it — a card whose own ts is
        newer than the reply (a child's never-auto card threaded under the brief after his "好的") is never decided or
        retired by it, however late the poll reads it. Every routed reply is recorded once with its outcome, so Slack's
        inclusive `oldest` re-returning a boundary message never routes it a second time (to a card that appeared since)."""
        now = time.time() if now is None else now
        if w.get('kind') != rt.APPROVALS_KIND or not messages or not w.get('thread'):
            return []
        cfg = config(rt)
        out = []
        for channel, m in sorted(messages, key=lambda item: float(item[1].get('ts') or 0)):
            if channel != w['channel'] or not self.owner_typed(rt, db, channel, m, w['thread']):
                continue
            # Slack's conversations.replies `oldest` is inclusive, so a poll re-returns the previous poll's boundary message.
            # A reply that was already routed must not be routed again (it would hit the next open card).
            if db.execute("SELECT 1 FROM decisions WHERE source_ts=? AND provenance='thread_reply'", (m['ts'],)).fetchone() \
                    or db.execute('SELECT 1 FROM card_replies WHERE reply_ts=?', (m['ts'],)).fetchone() \
                    or claimed(db, channel, m['ts'], now):
                continue
            result, cards = route_one(rt, db, channel, w['thread'], m, now, cfg)
            _record_route(rt, db, channel, m['ts'], w['thread'], result, cards, now)
            out.append(result)
        return out

    def settle(self, rt, db, now=None):
        """Supervisor step: poll the threads of undecided code-owned cards; retry a failed button retirement. P39: every
        reply of the owner's found here goes through card_threads.route_reply (claimed once; decide / retire as before; free text
        reaches the task the card is about — it used to reach nobody)."""
        now = time.time() if now is None else now
        if db.execute('SELECT 1 FROM card_reply_routes WHERE at<? LIMIT 1', (now - ROUTES_DAYS * 86400,)).fetchone():
            with rt.transaction(db):
                db.execute('DELETE FROM card_reply_routes WHERE at<?', (now - ROUTES_DAYS * 86400,))
        cfg = config(rt)
        out = {'polled': [], 'retried': []}
        meta = db.execute('SELECT value FROM runtime_meta WHERE key=?', (SINCE_KEY,)).fetchone()
        since = meta[0] if meta and TS_RE.fullmatch(str(meta[0])) else '0.0'
        # P37: rule-distillation (promote) cards are code-owned too, but their thread free-text is "redraft with this
        # guidance" (feedback_learning.settle_redraft), never a decision or a buttons-retire, so they are excluded here.
        rows = db.execute(CARD_SQL + 'LEFT JOIN card_replies r ON r.event_id=x.event_id WHERE x.stage=? AND '
                          'x.thread_ts IS NOT NULL AND c.dry=0 AND c.created>? AND NOT EXISTS (SELECT 1 FROM decisions d '
                          "WHERE d.event_id=x.event_id) AND (r.stage IS NULL OR r.stage NOT IN ('decided')) "
                          'AND NOT EXISTS (SELECT 1 FROM promote_cards pc WHERE pc.card_event=x.event_id)',
                          (rt.SUPERVISOR_STAGE, now - CODE_CARD_DAYS * 86400)).fetchall()
        for row in rows:
            root = row['thread_root'] or row['thread_ts']  # P39: a code-owned card threaded under a root is read there
            if rt._approvals_watch_active(db, row['channel'], root):
                continue  # P3.2: the task-level approvals watch polls this thread and routes its replies
            state = db.execute('SELECT * FROM card_replies WHERE event_id=?', (row['event_id'],)).fetchone()
            if state is not None and state['polled_at'] and now - state['polled_at'] < _poll_every(now - row['created']):
                continue
            cursor = (state['cursor'] if state is not None else None) or max(row['thread_ts'], since, key=float)
            try:
                found = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
                    'channel': row['channel'], 'ts': root, 'oldest': cursor, 'limit': 100,
                    'include_all_metadata': 'true'})
                messages = sorted((m for m in found.get('messages', []) if TS_RE.fullmatch(str(m.get('ts') or '')) and
                                   float(m['ts']) > float(cursor)), key=lambda m: float(m['ts']))
                error = None
            except Exception as exc:  # noqa: BLE001 - polled again later
                messages, error = [], rt.safe_error(exc)
            high = max([cursor] + [m['ts'] for m in messages], key=float)
            with rt.transaction(db):
                # updated_at is the retry clock of a failed retirement: a poll never moves it.
                db.execute('INSERT INTO card_replies (event_id,cursor,polled_at,error,updated_at) VALUES (?,?,?,?,?) '
                           'ON CONFLICT(event_id) DO UPDATE SET cursor=excluded.cursor,polled_at=excluded.polled_at,'
                           'error=coalesce(excluded.error,card_replies.error)', (row['event_id'], high, now, error, now))
            for i, m in enumerate(messages):
                try:
                    if not cfg['task_thread']:  # kill switch: the P3.1 / P38 code-owned path (decide / retire), no routing
                        if self.owner_typed(rt, db, row['channel'], m, root):
                            out['polled'].append(handle(rt, db, row, m, now, cfg, code_owned=True))
                    elif rt.card_threads.eligible_author(rt, db, row['channel'], root, m):  # the owner, or an in-window delegate
                        out['polled'].append(rt.card_threads.route_reply(rt, db, row['channel'], root, m, now,
                                                                         'code_card_poll'))
                except Exception as exc:  # noqa: BLE001 - P39: this reply (and the rest) is read again on the next poll
                    with rt.transaction(db):
                        db.execute('UPDATE card_replies SET cursor=?,error=? WHERE event_id=?',
                                   (max([cursor] + [x['ts'] for x in messages[:i]], key=float), rt.safe_error(exc),
                                    row['event_id']))
                    break
        for state in db.execute("SELECT * FROM card_replies WHERE stage IN ('failed','retiring') AND attempts<? AND "
                                'reply_ts IS NOT NULL AND updated_at<?', (RETRY_ATTEMPTS, now - 60)).fetchall():
            if now - state['updated_at'] < 60 * state['attempts']:
                continue
            row = card(db, state['event_id'])
            if row is None or db.execute('SELECT 1 FROM decisions WHERE event_id=?', (row['event_id'],)).fetchone():
                continue
            try:
                m = _read_reply(rt, row, state['reply_ts'])
            except Exception:  # noqa: BLE001 - next settle
                continue
            if m is not None and self.owner_typed(rt, db, row['channel'], m, row['thread_ts']):
                with rt.transaction(db):
                    db.execute("UPDATE card_replies SET stage='failed' WHERE event_id=? AND stage='retiring'",
                               (row['event_id'],))
                secret = _secret(rt, db, row)
                out['retried'].append(retire(rt, db, row, m, now, secret=secret,
                                             code_owned=row['stage'] == rt.SUPERVISOR_STAGE,
                                             plan_change=not secret and cfg.get('retire_on') == 'plan_change' and
                                             rt.buttons.parse_reply(m.get('text'), rt.buttons.card_model(row['options']))
                                             ['option'] is None and rt.buttons.reply_class(m.get('text')) == 'plan_change'))
        return out
