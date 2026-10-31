"""The owner's replies in card threads always reach the task the card is ABOUT (code, never the model).

- `card_task`: the task a card thread belongs to — the approvals-root owner (authoritative), else the newest card in the
  thread that names a task (its task id, else its decision watch's working directory), else the record a code-owned card
  was posted for (orphan / deadline / routine / blocker / stuck-claim), else the run / failure card's dispatch. Never
  the session that posted the card.
- `route_reply`: one of the owner's (or an in-window out-of-office delegate's) replies, claimed ONCE in
  card_reply_routes atomically with the cross-path dedupe (any event of that reply, a thread-reply decision, a retired
  card); then exactly the card logic of card_replies.route_one (only an OPEN card posted before the reply is decided or
  retired); then delivered — unless code already settled it (a code-owned card's decision) — as a pending event with the
  reply's Slack refs on the target task's inbox watch, where the dispatcher's normal rules apply. A closed or unknown
  task -> the inbox task.
- Two discovery paths besides the pollers: `push` — the Socket Mode receiver's realtime path (cards.realtime_replies;
  refs only, re-read from Slack here) — and `sweep` — a low-frequency reconcile (cards.reconcile_minutes) over the
  decision channels' card threads of the last cards.reconcile_days, from a per-thread cursor, skipping threads a live
  poller covers. Both start at the time this code first ran (no backfill unless asked for).
Only refs and hashes are stored; bodies are re-read through the API when a session needs them.

`rt` is the runtime (store helpers, Slack client, task registry, lifecycle, worker config, sibling modules).
"""
import hashlib
import json
import re
import time

TS_RE = re.compile(r'\d+\.\d+')
USER_RE = re.compile(r'U[A-Z0-9]{2,}')
CHANNEL_RE = re.compile(r'[CG][A-Z0-9]{2,}')
SINCE_KEY = 'card_threads_since'     # runtime_meta: replies typed before this code first ran are never swept
SWEEP_KEY = 'card_thread_sweep'      # runtime_meta: the last sweep (time + counts, no bodies)
SWEEP_MAX_THREADS = 40               # conversations.replies reads per sweep (the rest waits for the next one)
SWEEP_BUDGET = 15.0                  # wall-clock seconds of Slack reads per sweep (it runs inside the supervisor loop)
SWEEP_RESUME = 120                   # a sweep cut short by its budget / thread cap resumes this many seconds later
HISTORY_PAGES = 5
CONFIG_TTL = 300                     # what the receiver caches card-reply-config for
DDL = '''
  CREATE TABLE IF NOT EXISTS card_thread_cursors (
    channel TEXT NOT NULL, root TEXT NOT NULL, cursor TEXT NOT NULL, checked_at REAL NOT NULL,
    PRIMARY KEY (channel, root));
  -- Where route_reply delivered a reply (task_id), why (reason), the task the card is about (card_task) and which path
  -- saw it first (source: push / sweep / code_card_poll); the claim itself is the card_reply_routes row.
  -- An `undeliverable` reply (no task, not even the inbox task, has a session yet) keeps its root / author / digest /
  -- envelope (no body) here and is delivered by the next sweep.
  CREATE TABLE IF NOT EXISTS card_reply_targets (
    channel TEXT NOT NULL, reply_ts TEXT NOT NULL, task_id TEXT, source TEXT, reason TEXT, card_task TEXT,
    event_id TEXT, at REAL NOT NULL, root TEXT, author TEXT, digest TEXT, envelope TEXT, PRIMARY KEY (channel, reply_ts));
'''


def initialize(db):
    db.executescript(DDL)
    db.execute('INSERT OR IGNORE INTO runtime_meta VALUES (?,?)', (SINCE_KEY, f'{time.time():.6f}'))
    db.commit()


def config(rt):
    """cards.* with the P39 keys; a malformed config turns every P39 path off (fail closed, the pollers still run)."""
    try:
        return rt.worker_config.load()[0]['cards']
    except Exception:  # noqa: BLE001
        return {**rt.worker_config.DEFAULTS['cards'], 'task_thread': False, 'realtime_replies': False}


def _since(db):
    row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (SINCE_KEY,)).fetchone()
    return row[0] if row and TS_RE.fullmatch(str(row[0])) else f'{time.time():.6f}'


# ---------------------------------------------------------------------------
# Which task a card thread is about
# ---------------------------------------------------------------------------

ORIGINS = (  # code-owned cards: the record each was posted for names the task
    ('SELECT task_id FROM orphan_cards WHERE card_event=?', 'orphan_card'),
    ('SELECT task_id FROM task_deadlines WHERE card_event=?', 'deadline_card'),
    ('SELECT task_id FROM routine_confirmations WHERE card_event=?', 'routine_card'),
    ('SELECT task_id FROM routine_proposals WHERE card_event=?', 'routine_card'),
    ('SELECT task_id FROM automation_gaps WHERE card_event=? OR stage2_event=?', 'blocker_card'),
    ('SELECT task_id FROM stuck_claims WHERE card_event=?', 'stuck_claim_card'),
    ('SELECT task_id FROM interaction_records WHERE event_id=?', 'card'),
)


def _one(db, sql, args):
    try:
        row = db.execute(sql, args).fetchone()
    except Exception:  # noqa: BLE001 - a table of a feature that never initialized here
        return None
    return row[0] if row and row[0] else None


def card_task(rt, db, channel, root):
    """(task_id, why) of the task card thread (channel, root) is about, or (None, 'unknown')."""
    tid = _one(db, 'SELECT task_id FROM approvals_roots WHERE channel=? AND root_ts=? AND task_id IS NOT NULL',
               (channel, root))
    if tid:
        return tid, 'approvals_root'
    for r in db.execute('SELECT c.event_id, c.task_id, x.watch_id FROM escalation_cards c JOIN escalations x ON '
                        'x.event_id=c.event_id WHERE c.channel=? AND coalesce(c.thread_root, x.thread_ts)=? '
                        'ORDER BY c.created DESC', (channel, root)).fetchall():
        if r['task_id']:
            return r['task_id'], 'card'
        if r['watch_id']:
            try:
                tid = rt.registry.task_id_for_cwd(rt.load(db, r['watch_id']).get('cwd'))
            except Exception:  # noqa: BLE001 - a missing / malformed watch names no task
                tid = None
            if tid:
                return tid, 'card_watch'
        for sql, why in ORIGINS:
            tid = _one(db, sql, (r['event_id'],) * sql.count('?'))
            if tid:
                return tid, why
    tid = _one(db, 'SELECT task_id FROM dispatches WHERE card_channel=? AND (card_ts=? OR fail_card_ts=?) AND task_id '
               'IS NOT NULL ORDER BY created_at DESC LIMIT 1', (channel, root, root))
    if tid:
        return tid, 'run_card'
    return None, 'unknown'


def task_closed(rt, db, task_id, config=None):
    """A task that can no longer take a reply: no workspace, archived, a terminal closure marker in status.md, or a
    terminal Linear state read since its last dispatch. The long-lived intake tasks are never closed, nor is a task that
    is working again (a running run, unhandled work handed to it, a dispatch newer than its closure marker)."""
    if config is None:
        try:
            config = rt.worker_config.load()[0]
        except Exception:  # noqa: BLE001
            config = rt.worker_config.DEFAULTS
    if rt.worker_config.intake_role(config, task_id):
        return False
    if rt.registry.running_session(db, task_id):
        return False
    if db.execute("SELECT 1 FROM events e JOIN watches w ON w.id=e.watch_id WHERE e.status!='handled' AND "
                  "json_extract(w.data,'$.kind')='task_inbox' AND json_extract(w.data,'$.task_id')=? LIMIT 1",
                  (task_id,)).fetchone():
        return False  # work was handed to it (a reopened task): not closed while it has unhandled events
    row = db.execute('SELECT path, state FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    path = row['path'] if row else str(rt.registry.task_dir(task_id))
    if (row and row['state'] == 'archived') or not rt.registry.task_dir(task_id).is_dir():
        return True
    if db.execute("SELECT 1 FROM events e JOIN watches w ON w.id=e.watch_id WHERE e.status!='handled' AND "
                  "json_extract(w.data,'$.kind')='task_inbox' AND json_extract(w.data,'$.task_id')=? LIMIT 1",
                  (task_id,)).fetchone():
        return False  # work was handed to it (a reopened task): not closed while it has unhandled events
    if rt.task_lifecycle.TERMINAL_RE.search(rt.task_lifecycle._status_text(path)):
        try:
            marked = (rt.registry.task_dir(task_id) / 'status.md').stat().st_mtime
        except OSError:
            marked = 0.0
        latest = _one(db, 'SELECT max(created_at) FROM dispatches WHERE task_id=? OR task_key=?', (task_id, task_id))
        return not (latest and float(latest) > marked)  # a dispatch after the marker: reopened
    read = _one(db, 'SELECT terminal FROM task_linear_reads WHERE task_id=? AND dispatches=(SELECT count(*) FROM '
                'dispatches WHERE task_id=? OR task_key=?)', (task_id, task_id, task_id))
    return bool(read)


def target(rt, db, channel, root):
    """(task to deliver to, card task, reason): the card's task while it is open, else the inbox task."""
    tid, why = card_task(rt, db, channel, root)
    try:
        inbox = rt.worker_config.load()[0]['intake']['inbox_task']
    except Exception:  # noqa: BLE001
        inbox = rt.worker_config.DEFAULTS['intake']['inbox_task']
    if tid is None:
        return inbox, None, 'card_task_unknown'
    if task_closed(rt, db, tid):
        return inbox, tid, 'card_task_closed'
    if rt.registry.task_consumer(db, tid) is None:
        return inbox, tid, 'card_task_has_no_session'
    return tid, tid, why


# ---------------------------------------------------------------------------
# One reply, once
# ---------------------------------------------------------------------------

def handled(rt, db, channel, ts, now=None):
    """Any path already took this reply: routed by an approvals watch / this router (a claim whose process died is
    not — card_replies.claimed), ingested as an event by any watch, recorded as a thread-reply decision, or it retired a
    card's buttons."""
    return rt.card_replies.claimed(db, channel, ts, now) or db.execute(
        'SELECT 1 FROM events WHERE channel=? AND ts=? UNION ALL '
        "SELECT 1 FROM decisions WHERE card_channel=? AND source_ts=? AND provenance='thread_reply' UNION ALL "
        'SELECT 1 FROM card_replies r JOIN escalation_cards c ON c.event_id=r.event_id WHERE c.channel=? AND '
        'r.reply_ts=? LIMIT 1', (channel, ts) * 3).fetchone() is not None


def _deliver(rt, db, task_id, channel, root, ts, author, digest, env, now):
    """Inside a transaction: a pending event with the reply's Slack refs on `task_id`'s inbox watch (its consumer),
    or None when no event can be created (another watch has it, or the task has no workspace / session)."""
    if db.execute('SELECT 1 FROM events WHERE channel=? AND ts=? LIMIT 1', (channel, ts)).fetchone():
        return None
    row = db.execute('SELECT path FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    consumer = rt.registry.task_consumer(db, task_id)
    if row is None or consumer is None or not rt.registry.task_dir(task_id).is_dir():
        return None
    w = rt.ensure_inbox_watch(db, task_id, row['path'], consumer[0] or 'codex', consumer[1], now)
    eid = hashlib.sha256(f'{w["id"]}/{channel}/{ts}/{digest}'.encode()).hexdigest()[:24]
    db.execute('INSERT OR IGNORE INTO events (id,watch_id,channel,ts,author,digest,received,status) VALUES '
               '(?,?,?,?,?,?,?,?)', (eid, w['id'], channel, ts, author, digest, now, 'pending'))
    db.execute('INSERT OR IGNORE INTO event_sources VALUES (?,?)', (eid, root))
    rt.envelopes.store(db, eid, env, now)
    return eid


def retry_undeliverable(rt, db, now):
    """Replies that no task could take when they were routed (sweep step): deliver them now if one can."""
    done = []
    for r in db.execute('SELECT * FROM card_reply_targets WHERE event_id IS NULL AND envelope IS NOT NULL').fetchall():
        deliver_to, _, reason = target(rt, db, r['channel'], r['root'])
        with rt.transaction(db):
            eid = _deliver(rt, db, deliver_to, r['channel'], r['root'], r['reply_ts'], r['author'], r['digest'],
                           json.loads(r['envelope']), now)
            if eid or db.execute('SELECT 1 FROM events WHERE channel=? AND ts=? LIMIT 1',
                                 (r['channel'], r['reply_ts'])).fetchone():
                db.execute('UPDATE card_reply_targets SET event_id=?,task_id=?,reason=?,envelope=NULL WHERE channel=? '
                           'AND reply_ts=?', (eid, deliver_to, reason, r['channel'], r['reply_ts']))
                done.append(r['reply_ts'])
    return done


RETIRED = 'retired'  # stop_reason of an approvals watch the reconcile retired (F03 c): replies are swept / pushed


# ---------------------------------------------------------------------------
# Discovery: realtime push and the reconcile sweep
# ---------------------------------------------------------------------------

def decision_channels(rt):
    try:
        return sorted(rt.buttons.decision_channels(rt.buttons.load_config()))
    except Exception:  # noqa: BLE001 - no / malformed decisions config: nothing to sweep or accept
        return []


def _db_path(db):
    row = next((r for r in db.execute('PRAGMA database_list') if r[1] == 'main'), None)
    return row[2] if row else None


def _covered(rt, db, channel, root, now):
    """A live poller reads this thread anyway: a running approvals watch, a running per-card decision watch of the
    root card itself, or settle's poll of an undecided young code-owned root card."""
    row = db.execute('SELECT data FROM watches WHERE id=?', (rt._approvals_wid(channel, root),)).fetchone()
    if row and json.loads(row[0]).get('state') == 'running':
        return True
    for r in db.execute("SELECT x.stage, x.watch_id, c.created FROM escalations x JOIN escalation_cards c ON "
                        "c.event_id=x.event_id WHERE c.channel=? AND x.thread_ts=? AND NOT EXISTS (SELECT 1 FROM "
                        "decisions d WHERE d.event_id=x.event_id)", (channel, root)).fetchall():
        if r['stage'] == rt.SUPERVISOR_STAGE and now - r['created'] < rt.card_replies.CODE_CARD_DAYS * 86400:
            return True
        if r['watch_id']:
            w = db.execute('SELECT data FROM watches WHERE id=?', (r['watch_id'],)).fetchone()
            if w and json.loads(w[0]).get('state') == 'running':
                return True
    return False


class CardThreads:
    """Telling the owner's and delegates' replies from everyone else's (`eligible_author`, the owner key is the
    author kind) and the routing, receiver, realtime and sweep paths that use it."""

    def __init__(self, owner_key, owner_user):
        self.key, self.owner_user = owner_key, owner_user

    def eligible_author(self, rt, db, channel, root, m, config=None):
        """The owner key for the owner's own hand-typed reply in this thread, 'delegate' for an in-window OOO
        delegate's human reply, else None (bots, agents' posts with the owner's token, everyone else)."""
        ts = str(m.get('ts') or '')
        if m.get('thread_ts') != root or not TS_RE.fullmatch(ts) or float(ts) <= float(root) or m.get('subtype') or \
                m.get('bot_id'):
            return None
        if rt.card_replies.owner_typed(rt, db, channel, m, root):
            return self.key
        user = str(m.get('user') or '')
        if not USER_RE.fullmatch(user) or user in (self.owner_user, rt.BOT) or m.get('app_id'):
            return None
        try:
            config = config or rt.buttons.load_config()
        except ValueError:
            return None
        if user in rt.envelopes.active_delegates(config, ts, rt.buttons):
            meta = m.get('metadata') if isinstance(m.get('metadata'), dict) else {}
            if meta.get('event_type') != rt.AGENT_EVENT_TYPE:
                return 'delegate'
        return None

    def route_reply(self, rt, db, channel, root, m, now=None, source='sweep'):
        """Route one reply (already verified as the owner's / a delegate's by the caller, re-checked here). Returns a dict with
        `outcome`: duplicate | routed | decided_by_code | skipped (+ reason) and, when delivered, `event_id` / `task_id`."""
        now = time.time() if now is None else now
        ts = str(m.get('ts') or '')
        cfg = config(rt)
        if not cfg['task_thread']:
            return {'ts': ts, 'outcome': 'skipped', 'reason': 'task_thread_off'}  # the pre-P3.2 per-card watches only
        author = self.eligible_author(rt, db, channel, root, m)
        if author is None:
            return {'ts': ts, 'outcome': 'skipped', 'reason': f'not_{self.key}_or_delegate'}
        if rt._secret_thread(db, channel, root):
            return {'ts': ts, 'outcome': 'skipped', 'reason': 'secret_thread'}  # a verification code: its own watch only
        if _one(db, 'SELECT 1 FROM promote_cards WHERE card_channel=? AND card_ts=?', (channel, root)):
            return {'ts': ts, 'outcome': 'skipped', 'reason': 'rule_card_thread'}  # P37: settle_redraft owns that text
        stopped = db.execute('SELECT data FROM watches WHERE id=?', (rt._approvals_wid(channel, root),)).fetchone()
        stopped = json.loads(stopped[0]) if stopped else None
        if stopped and stopped.get('state') == 'stopped' and stopped.get('stop_reason') not in (None, RETIRED):
            return {'ts': ts, 'outcome': 'skipped', 'reason': 'thread_stopped:' + str(stopped.get('stop_reason'))}
        deliver_to, about, reason = target(rt, db, channel, root)
        with rt.transaction(db):
            if handled(rt, db, channel, ts, now):
                return {'ts': ts, 'outcome': 'duplicate'}
            db.execute('INSERT OR REPLACE INTO card_reply_routes (channel,reply_ts,root,outcome,card_event,open_cards,at) '
                       'VALUES (?,?,?,?,?,?,?)', (channel, ts, root, 'routing', None, '[]', now))
            db.execute('INSERT OR REPLACE INTO card_reply_targets (channel,reply_ts,task_id,source,reason,card_task,at) '
                       'VALUES (?,?,?,?,?,?,?)', (channel, ts, deliver_to, source, reason, about, now))
        try:
            result, cards = ({'ts': ts, 'action': 'delegate_conversation'}, []) if author == 'delegate' else \
                rt.card_replies.route_one(rt, db, channel, root, m, now, rt.card_replies.config(rt))
        except BaseException:
            # A Slack / DB error inside the decision step: release the claim so the next push / poll / sweep routes the
            # reply again (and records the owner's typed choice); a process killed here leaves a claim that goes stale instead.
            with rt.transaction(db):
                db.execute("DELETE FROM card_reply_routes WHERE channel=? AND reply_ts=? AND outcome='routing'", (channel, ts))
                db.execute('DELETE FROM card_reply_targets WHERE channel=? AND reply_ts=? AND event_id IS NULL', (channel, ts))
            raise
        # A code-owned card's typed choice is applied by code and wakes nobody — unless the owner wrote more than the choice
        # ("我来跟进，另外截止往后推一天"): then the task gets his text too.
        code_decided = result.get('action') == 'decided' and result.get('code_owned') and not result.get('guidance')
        eid, outcome = None, 'decided_by_code'
        digest = hashlib.sha256(json.dumps({'text': m.get('text'), 'files': [f.get('id') for f in m.get('files') or []
                                                                             if isinstance(f, dict)]},
                                           sort_keys=True).encode()).hexdigest()
        with rt.transaction(db):
            db.execute('UPDATE card_reply_routes SET outcome=?,card_event=?,open_cards=?,reply_class=? WHERE channel=? AND '
                       'reply_ts=?', (str(result.get('action')), result.get('event_id'), json.dumps(cards),
                                      result.get('reply_class'), channel, ts))  # P41: decided / plan_change / kept
            if not code_decided:
                env = rt.message_envelope({'channel': channel, 'thread': root}, m, root)
                eid = _deliver(rt, db, deliver_to, channel, root, ts, m.get('user'), digest, env, now)
                outcome = 'routed' if eid else 'duplicate' if db.execute(
                    'SELECT 1 FROM events WHERE channel=? AND ts=? LIMIT 1', (channel, ts)).fetchone() else 'undeliverable'
                # Undeliverable (no session anywhere yet): the refs wait here for the next sweep — never a body.
                db.execute('UPDATE card_reply_targets SET event_id=?,root=?,author=?,digest=?,envelope=? WHERE channel=? AND '
                           'reply_ts=?', (eid, root, m.get('user'), digest,
                                          json.dumps(env, ensure_ascii=False) if outcome == 'undeliverable' else None,
                                          channel, ts))
            else:  # the approvals poll may have ingested it meanwhile (a push racing the supervisor): code decided it
                db.execute("UPDATE events SET status='handled',disposition='decided_by_code' WHERE channel=? AND ts=? AND "
                           "status='pending'", (channel, ts))
        try:  # P3.3 control plane receipt (also for code-owned cards, F28): 👀, or ✅ / ❌ for a recorded decision
            provider = (rt.registry.task_consumer(db, deliver_to) or ('codex',))[0]
            decided = {ts: result['kind']} if result.get('action') == 'decided' and result.get('kind') else {}
            rt.ingest_receipt(db, [(channel, ts)], provider, now, decided)
        except Exception as exc:  # noqa: BLE001 - a receipt never blocks routing
            rt._meta_error(db, 'read_receipt_error', exc)
        return {'ts': ts, 'outcome': outcome, 'action': result.get('action'), 'card_event': result.get('event_id'),
                'task_id': deliver_to, 'card_task': about, 'reason': reason, 'event_id': eid}

    def receiver_config(self, rt, db, now=None):
        """`card-reply-config` for the Socket Mode receiver: is the push on, which channels and authors to forward."""
        now = time.time() if now is None else now
        cfg = config(rt)
        channels = decision_channels(rt)
        users = [self.owner_user]
        try:
            decisions = rt.buttons.load_config()
            ooo = decisions.get('ooo') or {}
            if ooo.get('active'):
                users += [u for u in ooo.get('delegates') or [] if USER_RE.fullmatch(str(u))]
        except Exception:  # noqa: BLE001
            pass
        return {'ok': True, 'enabled': bool(cfg.get('realtime_replies') and cfg['task_thread'] and channels),
                'channels': channels, 'users': sorted(set(users)), 'ttl_seconds': CONFIG_TTL}

    def push(self, rt, db, channel, ts, root, user, now=None):
        """The receiver's realtime path: refs only; the message is re-read and re-verified here."""
        now = time.time() if now is None else now
        if not (CHANNEL_RE.fullmatch(str(channel)) and TS_RE.fullmatch(str(ts)) and TS_RE.fullmatch(str(root)) and
                USER_RE.fullmatch(str(user))) or ts == root:
            return {'ok': False, 'outcome': 'refused', 'reason': 'invalid_refs'}
        if not config(rt).get('realtime_replies'):
            return {'ok': True, 'outcome': 'skipped', 'reason': 'realtime_replies_off'}
        if channel not in decision_channels(rt):
            return {'ok': False, 'outcome': 'refused', 'reason': 'not_a_decision_channel'}
        if handled(rt, db, channel, ts, now):
            return {'ok': True, 'outcome': 'duplicate'}
        found = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
            'channel': channel, 'ts': root, 'oldest': ts, 'latest': ts, 'inclusive': 'true', 'limit': 2,
            'include_all_metadata': 'true'})
        m = next((x for x in found.get('messages') or [] if isinstance(x, dict) and x.get('ts') == ts), None)
        if m is None or m.get('user') != user:
            return {'ok': False, 'outcome': 'refused', 'reason': 'message_not_found'}
        result = self.route_reply(rt, db, channel, root, m, now, 'push')
        rt.start(_db_path(db))  # deliver within seconds (no-op when the supervisor runs)
        return {'ok': result['outcome'] in ('routed', 'decided_by_code', 'duplicate'), **result,
                'reason': result.get('reason') or result['outcome']}

    def sweep(self, rt, db, now=None, force=False, backfill_seconds=None, dry_run=False, clock=time.monotonic):
        """Supervisor step (every cards.reconcile_minutes; `force` = the CLI): the owner's replies in the decision channels' card
        threads of the last cards.reconcile_days that no live poller covers, from each thread's cursor (never before this
        code first ran, unless `backfill_seconds`). One conversations.history pass per channel finds threads with a newer
        reply; at most SWEEP_MAX_THREADS conversations.replies reads per sweep."""
        now = time.time() if now is None else now
        cfg = config(rt)
        out = {'at': now, 'threads': 0, 'routed': [], 'skipped': 0, 'deferred': 0, 'error': None, 'dry_run': dry_run}
        if not cfg['task_thread'] or not cfg.get('reconcile_minutes'):
            return {**out, 'error': 'disabled'}
        meta = db.execute('SELECT value FROM runtime_meta WHERE key=?', (SWEEP_KEY,)).fetchone()
        try:
            last = float(json.loads(meta[0])['at']) if meta else 0.0
        except (TypeError, ValueError, KeyError):
            last = 0.0
        if not force and now - last < cfg['reconcile_minutes'] * 60:
            return None
        floor = _since(db)
        if backfill_seconds:
            floor = f'{min(float(floor), now - float(backfill_seconds)):.6f}'
        # Roots of the last reconcile_days, whatever their age relative to P39's install: `floor` bounds the REPLIES read
        # (never one typed before P39 first ran), not the threads (a pre-install card thread still gets its late reply).
        oldest = f'{now - cfg["reconcile_days"] * 86400 - 1:.6f}'
        token = rt.api_identity()
        reads, deadline = 0, clock() + SWEEP_BUDGET
        try:
            out['retried'] = retry_undeliverable(rt, db, now)
            for channel in decision_channels(rt):
                parents, cursor = [], None
                for _ in range(HISTORY_PAGES):
                    params = {'channel': channel, 'oldest': oldest, 'limit': 200}
                    if cursor:
                        params['cursor'] = cursor
                    page = rt.slack.api_call('conversations.history', token, params=params)
                    parents += [p for p in page.get('messages') or [] if isinstance(p, dict) and p.get('latest_reply')]
                    cursor = (page.get('response_metadata') or {}).get('next_cursor')
                    if not cursor:
                        break
                for p in sorted(parents, key=lambda x: float(x.get('ts') or 0)):
                    root, latest = str(p.get('ts') or ''), str(p.get('latest_reply') or '')
                    if not TS_RE.fullmatch(root) or not TS_RE.fullmatch(latest):
                        continue
                    row = db.execute('SELECT cursor FROM card_thread_cursors WHERE channel=? AND root=?',
                                     (channel, root)).fetchone()
                    start = max([row[0] if row else root, floor] if not backfill_seconds else [root, floor], key=float)
                    if float(latest) <= float(start):
                        continue
                    if _covered(rt, db, channel, root, now):
                        out['skipped'] += 1
                        continue
                    if reads >= SWEEP_MAX_THREADS or clock() >= deadline:
                        out['deferred'] += 1
                        continue
                    reads += 1
                    out['threads'] += 1
                    found = rt.slack.api_call('conversations.replies', token, params={
                        'channel': channel, 'ts': root, 'oldest': start, 'limit': 200, 'include_all_metadata': 'true'})
                    high = start
                    for m in sorted((x for x in found.get('messages') or [] if isinstance(x, dict) and
                                     TS_RE.fullmatch(str(x.get('ts') or ''))), key=lambda x: float(x['ts'])):
                        if float(m['ts']) <= float(start) or m['ts'] == root:
                            continue
                        high = max(high, m['ts'], key=float)
                        if self.eligible_author(rt, db, channel, root, m) is None or handled(rt, db, channel, m['ts'], now):
                            continue
                        if dry_run:
                            out['routed'].append({'channel': channel, 'root': root, 'ts': m['ts'], 'dry_run': True})
                            continue
                        out['routed'].append(self.route_reply(rt, db, channel, root, m, now, 'sweep'))
                    if not dry_run:
                        with rt.transaction(db):
                            db.execute('INSERT INTO card_thread_cursors (channel,root,cursor,checked_at) VALUES (?,?,?,?) '
                                       'ON CONFLICT(channel,root) DO UPDATE SET cursor=excluded.cursor,'
                                       'checked_at=excluded.checked_at', (channel, root, high, now))
        except Exception as exc:  # noqa: BLE001 - recorded; the next sweep retries from the same cursors
            out['error'] = rt.safe_error(exc)
        if not dry_run:
            # Deferred threads (thread cap / time budget): the next sweep comes SWEEP_RESUME seconds later, not a full
            # interval later.
            at = now - cfg['reconcile_minutes'] * 60 + SWEEP_RESUME if out['deferred'] else now
            with rt.transaction(db):
                db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (SWEEP_KEY, json.dumps(
                    {'at': at, 'swept_at': now, 'threads': out['threads'], 'routed': len(out['routed']),
                     'skipped': out['skipped'], 'deferred': out['deferred'], 'error': out['error']})))
                db.execute('DELETE FROM card_thread_cursors WHERE checked_at<?', (now - 2 * cfg['reconcile_days'] * 86400,))
                db.execute('DELETE FROM card_reply_targets WHERE at<?', (now - rt.card_replies.ROUTES_DAYS * 86400,))
        return out
