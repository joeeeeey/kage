"""The store-owned Claude outbox and advisory native CLI wake signals. No chat I/O.

Events for a Claude consumer session are queued in the store; the session claims a batch, works it and releases
it; a wake file tells a live Claude CLI that work is waiting (advisory: the store is the truth). `settle_claimed`
(ClaudeDelivery: it records the owner's decision) and `requeue_claimed` act on exact claimed rows."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

PROVIDERS = ('codex', 'claude')


def provider(w):
    value = w.get('provider') or 'codex'
    if value not in PROVIDERS:
        raise ValueError('unsupported_provider')
    return value


def actor():
    # A Claude child may inherit CODEX_THREAD_ID; its explicit native binding wins.
    if os.environ.get('CLAUDE_CODE_SESSION_ID'):
        return 'claude', os.environ['CLAUDE_CODE_SESSION_ID']
    return 'codex', os.environ.get('CODEX_THREAD_ID')


def require_session(session):
    uuid.UUID(session)
    if actor() != ('claude', session):
        raise ValueError('current_claude_session_must_match_outbox_owner')


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS claude_wakes (
        session_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
        signaled INTEGER NOT NULL DEFAULT 0, last_signal REAL, last_error TEXT);
      CREATE TABLE IF NOT EXISTS claude_claims (
        event_id TEXT NOT NULL, session_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        claimed_at REAL NOT NULL, released_at REAL, release_reason TEXT, PRIMARY KEY (event_id, batch_id));
      CREATE INDEX IF NOT EXISTS claude_claims_batch ON claude_claims(session_id,batch_id);
      CREATE INDEX IF NOT EXISTS watches_owner ON watches(
        coalesce(json_extract(data,'$.provider'),'codex'),json_extract(data,'$.session'));
    ''')
    _claims_keep_history(db)
    db.commit()


def _claims_keep_history(db):
    """P38 G4: claims keep their history — one row per (event, batch), never overwritten by a later claim of the same
    event (a re-queued batch, a second consumer). Idempotent copy of a pre-P38 table keyed by event_id alone; older
    code keeps working on the new table (its INSERT OR REPLACE then adds a row per batch)."""
    pk = [r[1] for r in sorted(db.execute('PRAGMA table_info(claude_claims)'), key=lambda r: r[5]) if r[5]]
    if pk != ['event_id']:
        return
    db.executescript('''
      BEGIN IMMEDIATE;
      CREATE TABLE claude_claims_p38 (
        event_id TEXT NOT NULL, session_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        claimed_at REAL NOT NULL, released_at REAL, release_reason TEXT, PRIMARY KEY (event_id, batch_id));
      INSERT INTO claude_claims_p38 SELECT event_id, session_id, batch_id, claimed_at, released_at, release_reason
        FROM claude_claims;
      DROP TABLE claude_claims;
      ALTER TABLE claude_claims_p38 RENAME TO claude_claims;
      CREATE INDEX IF NOT EXISTS claude_claims_batch ON claude_claims(session_id,batch_id);
      COMMIT;
    ''')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
        # Once, with the copy: claims the pre-P38 approvals rebind left open on events no longer claimed would read as
        # a fresh stuck batch the next time the event is claimed — close them (the reason keeps the audit).
        with transaction(db):
            db.execute("UPDATE claude_claims SET released_at=?,release_reason='closed_at_p38_migration' WHERE released_at "
                       "IS NULL AND event_id NOT IN (SELECT id FROM events WHERE status='claimed')", (time.time(),))


@contextlib.contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def wake_path(path, session):
    # Use a DB-specific namespace even for several databases in the same directory.
    uuid.UUID(session)
    dbpath = Path(path).expanduser().resolve()
    namespace = hashlib.sha256(str(dbpath).encode()).hexdigest()[:16]
    return dbpath.parent / 'claude-wake' / namespace / (session + '.json')


def ensure_path(path, session):
    target = wake_path(path, session)
    for directory in [target.parent.parent, target.parent]:
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ValueError('claude_wake_directory_must_be_private_0700')
    return target


def bump(db, session):
    uuid.UUID(session)
    db.execute('INSERT INTO claude_wakes(session_id,generation) VALUES (?,1) '
               'ON CONFLICT(session_id) DO UPDATE SET generation=generation+1', (session,))


def queue_watch(db, wid):
    with transaction(db):
        row = db.execute('SELECT data FROM watches WHERE id=?', (wid,)).fetchone()
        if row is None:
            raise ValueError('unknown_watch')
        w = json.loads(row[0])
        if provider(w) != 'claude' or w['state'] == 'stopped':
            return
        receipt = 'claude-outbox:' + w['session'] + ':' + uuid.uuid4().hex
        changed = db.execute("UPDATE events SET status='queued',receipt=? WHERE watch_id=? AND status='pending'",
                             (receipt, wid)).rowcount
        if changed:
            w['last_delivery'] = 'queued'
            db.execute('UPDATE watches SET data=? WHERE id=?', (json.dumps(w, ensure_ascii=False), wid))
            bump(db, w['session'])


def publish_wakes(db, path, limit=20):
    # A failed write/daemon crash cannot lose an event: generation remains dirty.
    for row in db.execute('SELECT * FROM claude_wakes WHERE generation>signaled ORDER BY last_signal LIMIT ?', (limit,)).fetchall():
        session, generation = row['session_id'], row['generation']
        try:
            target = ensure_path(path, session)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'provider': 'claude', 'session_id': session, 'generation': generation}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            with transaction(db):
                db.execute('UPDATE claude_wakes SET signaled=?,last_signal=?,last_error=NULL WHERE session_id=?',
                           (generation, time.time(), session))
        except (OSError, ValueError):
            with transaction(db):
                db.execute("UPDATE claude_wakes SET last_error='wake_write_failed' WHERE session_id=?", (session,))


OWNER_SQL = "coalesce(json_extract(w.data,'$.provider'),'codex')='claude' AND json_extract(w.data,'$.session')=? AND json_extract(w.data,'$.state')!='stopped'"


def claim(db, session, now=None):
    require_session(session)
    now = time.time() if now is None else now
    with transaction(db):
        # One outstanding batch per session, across every watch. Never auto-expire a
        # claim: a timed-out consumer might already have produced external effects.
        if db.execute("SELECT 1 FROM events e JOIN watches w ON w.id=e.watch_id WHERE " + OWNER_SQL + " AND e.status='claimed' LIMIT 1", (session,)).fetchone():
            return None
        rows = db.execute("SELECT e.* FROM events e JOIN watches w ON w.id=e.watch_id WHERE " + OWNER_SQL + " AND e.status='queued' ORDER BY e.received,e.id LIMIT 30", (session,)).fetchall()
        if not rows:
            return None
        batch = uuid.uuid4().hex
        for e in rows:
            db.execute("UPDATE events SET status='claimed' WHERE id=? AND status='queued'", (e['id'],))
            db.execute('INSERT INTO claude_claims(event_id,session_id,batch_id,claimed_at) VALUES (?,?,?,?)',
                       (e['id'], session, batch, now))
        return {'batch_id': batch, 'session_id': session, 'events': [dict(e) for e in rows]}


def wait(db, session, timeout=900):
    """Claim one batch or return None at the deadline (CLI: exit 0 with a batch, 124 without).

    The deadline is wall-clock (time.time): on macOS the monotonic clock pauses while
    the Mac sleeps, which stretched a 1h wait to 4h. Short sleeps notice wake-up quickly.
    """
    require_session(session)
    if timeout < 0 or timeout > 86400:
        raise ValueError('timeout_must_be_0_to_86400_seconds')
    end = time.time() + timeout
    while True:
        result = claim(db, session)
        if result:
            return result
        remaining = end - time.time()
        if remaining <= 0:
            return None
        time.sleep(min(1, remaining))


def resolved(db, session):
    # Called inside the resolve/stop transaction. A signal suppressed by an active
    # batch must be reissued once that batch settles; files aren't the queue.
    if db.execute("SELECT 1 FROM events e JOIN watches w ON w.id=e.watch_id WHERE " + OWNER_SQL + " AND e.status='queued' LIMIT 1", (session,)).fetchone():
        bump(db, session)


def settle_claims(db, event_ids, reason, now=None):
    """Inside the caller's transaction: a handled/settled event's claim is closed for audit."""
    now = time.time() if now is None else now
    for event_id in event_ids:
        db.execute('UPDATE claude_claims SET released_at=?,release_reason=? WHERE event_id=? AND released_at IS NULL',
                   (now, reason, event_id))


def release(db, session, batch, reason):
    require_session(session)
    if not reason.strip() or len(reason) > 300:
        raise ValueError('reconciliation_reason_required_max_300_chars')
    with transaction(db):
        ids = [r[0] for r in db.execute("SELECT e.id FROM events e JOIN watches w ON w.id=e.watch_id JOIN claude_claims c ON c.event_id=e.id WHERE " + OWNER_SQL + " AND e.status='claimed' AND c.session_id=? AND c.batch_id=? AND c.released_at IS NULL", (session, session, batch))]
        for eid in ids:
            db.execute("UPDATE events SET status='queued' WHERE id=?", (eid,))
            db.execute('UPDATE claude_claims SET released_at=?,release_reason=? WHERE event_id=? AND batch_id=? AND '
                       'released_at IS NULL', (time.time(), reason, eid, batch))
        if ids:
            bump(db, session)
    return {'released': ids, 'batch_id': batch}


def stuck_claims(db, older_than):
    """P38 C2: {session: [(event_id, claimed_at)]} of open claims on still-claimed events that block `claim` (the watch
    is still owned by that session and not stopped), for sessions whose oldest such claim was taken at or before
    `older_than`."""
    found = {}
    for r in db.execute("SELECT c.session_id, c.event_id, c.claimed_at FROM claude_claims c JOIN events e ON "
                        "e.id=c.event_id JOIN watches w ON w.id=e.watch_id WHERE c.released_at IS NULL AND "
                        "e.status='claimed' AND coalesce(json_extract(w.data,'$.provider'),'codex')='claude' AND "
                        "json_extract(w.data,'$.session')=c.session_id AND json_extract(w.data,'$.state')!='stopped' "
                        'ORDER BY c.claimed_at'):
        found.setdefault(r['session_id'], []).append((r['event_id'], r['claimed_at']))
    return {s: rows for s, rows in found.items() if rows[0][1] <= older_than}


def queued_behind(db, session):
    return db.execute("SELECT count(*) FROM events e JOIN watches w ON w.id=e.watch_id WHERE " + OWNER_SQL +
                      " AND e.status='queued'", (session,)).fetchone()[0]


def exact_claimed(db, session, event_ids):
    """The ids still claimed under an open claim of `session` on a watch it still owns (writers hold a transaction)."""
    if not event_ids:
        return []
    return [r[0] for r in db.execute(
        "SELECT e.id FROM events e JOIN watches w ON w.id=e.watch_id JOIN claude_claims c ON c.event_id=e.id WHERE "
        + OWNER_SQL + " AND e.status='claimed' AND c.session_id=? AND c.released_at IS NULL AND e.id IN (%s) "
        'ORDER BY e.received, e.id' % ','.join('?' * len(event_ids)), (session, session, *event_ids))]


def requeue_claimed(db, session, event_ids, reason, now=None):
    """The owner's choice only (never automatic: the consumer may already have acted on the batch): the exact
    claimed rows go back to the queue and their claims close, in one transaction that re-verifies each row; the
    session's waiter re-claims them. Returns the ids moved."""
    uuid.UUID(session)
    now = time.time() if now is None else now
    with transaction(db):
        moved = exact_claimed(db, session, event_ids)
        for eid in moved:
            db.execute("UPDATE events SET status='queued' WHERE id=? AND status='claimed'", (eid,))
        settle_claims(db, moved, reason, now)
        if moved:
            bump(db, session)
    return moved


def hook_config(path, script, executable):
    import shlex
    command = ' '.join(shlex.quote(str(x)) for x in [executable, script, '--db', Path(path).resolve(), 'claude-hook'])
    return {'hooks': {
        'SessionStart': [{'hooks': [
            {'type': 'command', 'command': command + ' --bind', 'timeout': 10},
            {'type': 'command', 'command': command, 'asyncRewake': True, 'timeout': 10}]}],
        'FileChanged': [{'hooks': [{'type': 'command', 'command': command, 'asyncRewake': True, 'timeout': 10}]}]}}


class ClaudeDelivery:
    """The step that records the owner's decision on stuck claims (disposition `settled_by_<owner key>`)."""

    def __init__(self, owner_key):
        self.key = owner_key

    def settle_claimed(self, db, session, event_ids, reason, now=None):
        """The owner's choice only: the exact claimed rows are settled as handled (nothing is delivered again) and
        their claims close; queued events behind them wake the session. Returns the ids settled."""
        uuid.UUID(session)
        now = time.time() if now is None else now
        with transaction(db):
            done = exact_claimed(db, session, event_ids)
            for eid in done:
                db.execute(f"UPDATE events SET status='handled',disposition='settled_by_{self.key}' WHERE id=? AND status='claimed'",
                           (eid,))
            settle_claims(db, done, reason, now)
            resolved(db, session)
        return done
