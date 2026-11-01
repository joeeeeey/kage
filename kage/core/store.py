"""The runtime's store: one SQLite file (WAL, a private 0700 directory, the file 0600), its core tables, the per-module
tables (each module's own `initialize`, handed in by the deployment in the runtime's order) and the additive column
migrations. Table and column names never change; a migration only adds (nullable columns, new tables), never rewrites a
row. `transaction` is the one write transaction shape (BEGIN IMMEDIATE, commit or roll back).
"""
from __future__ import annotations
import contextlib
import os
from pathlib import Path
import sqlite3

CORE_DDL = '''
      CREATE TABLE IF NOT EXISTS watches (
        id TEXT PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, watch_id TEXT NOT NULL, channel TEXT NOT NULL,
        ts TEXT NOT NULL, author TEXT NOT NULL, digest TEXT NOT NULL,
        received REAL NOT NULL, status TEXT NOT NULL, receipt TEXT,
        disposition TEXT, action_ts TEXT);
      CREATE INDEX IF NOT EXISTS events_pending ON events(watch_id) WHERE status='pending';
      CREATE INDEX IF NOT EXISTS events_watch_status ON events(watch_id,status,received);
      CREATE INDEX IF NOT EXISTS events_source_digest ON events(channel,ts,digest);
      CREATE INDEX IF NOT EXISTS events_received ON events(received DESC,id);
      CREATE INDEX IF NOT EXISTS watches_due ON watches(json_extract(data,'$.state'),json_extract(data,'$.next_poll'));
      CREATE TABLE IF NOT EXISTS event_sources (
        event_id TEXT PRIMARY KEY, root TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS runtime_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS escalations (
        event_id TEXT PRIMARY KEY, stage TEXT NOT NULL, thread_ts TEXT,
        watch_id TEXT, client_msg_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS runtime (
        id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER, birth TEXT, heartbeat REAL);
      -- Additive tables only: older code keeps positional INSERTs into escalations.
      CREATE TABLE IF NOT EXISTS escalation_cards (
        event_id TEXT PRIMARY KEY, channel TEXT NOT NULL, options TEXT NOT NULL DEFAULT '[]',
        never_auto INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
        smoke INTEGER NOT NULL DEFAULT 0, dry INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS decisions (
        event_id TEXT PRIMARY KEY, choice TEXT NOT NULL, actor TEXT NOT NULL,
        role TEXT NOT NULL, decided_at REAL NOT NULL, card_channel TEXT NOT NULL,
        card_ts TEXT NOT NULL, receipt_ts TEXT, delivered_event TEXT,
        stage TEXT NOT NULL, last_error TEXT, note TEXT,
        provenance TEXT NOT NULL DEFAULT 'slack_button', proposal_id TEXT, source_ts TEXT);
      CREATE INDEX IF NOT EXISTS decisions_receipt ON decisions(card_channel,receipt_ts);
      CREATE INDEX IF NOT EXISTS decisions_delivered ON decisions(delivered_event);
      CREATE TABLE IF NOT EXISTS decision_proposals (
        id TEXT PRIMARY KEY, event_id TEXT NOT NULL, choice TEXT NOT NULL, note TEXT,
        source_ts TEXT NOT NULL, proposer_user TEXT NOT NULL, owner_session TEXT,
        proposed_at REAL NOT NULL, status TEXT NOT NULL, closed_reason TEXT,
        message_ts TEXT, decided_by TEXT, decided_at REAL, message_stage TEXT,
        rejected_event TEXT, last_error TEXT);
      CREATE INDEX IF NOT EXISTS decision_proposals_event ON decision_proposals(event_id,status);
      CREATE INDEX IF NOT EXISTS decision_proposals_rejected ON decision_proposals(rejected_event);
      -- Ask cards: the agent asks other people in the owner's voice; answers carry no authority.
      CREATE TABLE IF NOT EXISTS ask_cards (
        id TEXT PRIMARY KEY, watch_id TEXT, channel TEXT NOT NULL, ts TEXT, thread TEXT,
        owner_session TEXT NOT NULL, provider TEXT NOT NULL, responders TEXT NOT NULL,
        options TEXT NOT NULL, confirm_mode TEXT NOT NULL, state TEXT NOT NULL,
        remind_at REAL, reminded_at REAL, expire_at REAL, answer TEXT, receipt_ts TEXT,
        question TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'zh', reminder_text TEXT,
        hint INTEGER NOT NULL DEFAULT 1, remind_after REAL NOT NULL, expire_after REAL NOT NULL,
        created_at REAL NOT NULL, card_stage TEXT, answer_event TEXT, expired_event TEXT,
        reminder_ts TEXT, smoke INTEGER NOT NULL DEFAULT 0, last_error TEXT,
        dry INTEGER NOT NULL DEFAULT 0);
      CREATE INDEX IF NOT EXISTS ask_cards_open ON ask_cards(state,expire_at,remind_at);
      CREATE INDEX IF NOT EXISTS ask_cards_owner ON ask_cards(owner_session,created_at);
      CREATE INDEX IF NOT EXISTS ask_cards_answer ON ask_cards(answer_event);
      CREATE INDEX IF NOT EXISTS ask_cards_expired ON ask_cards(expired_event);
      CREATE TABLE IF NOT EXISTS ask_proposals (
        id TEXT PRIMARY KEY, card_id TEXT NOT NULL, option_id TEXT NOT NULL, note TEXT,
        source_ts TEXT NOT NULL, proposer_user TEXT NOT NULL, owner_session TEXT,
        proposed_at REAL NOT NULL, status TEXT NOT NULL, closed_reason TEXT, message_ts TEXT,
        decided_by TEXT, decided_at REAL, message_stage TEXT, rejected_event TEXT, last_error TEXT);
      CREATE INDEX IF NOT EXISTS ask_proposals_card ON ask_proposals(card_id,status);
      CREATE INDEX IF NOT EXISTS ask_proposals_rejected ON ask_proposals(rejected_event);
      -- Local send ledger: every message this runtime posts (never bodies).
      CREATE TABLE IF NOT EXISTS agent_sends (
        channel TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT,
        session TEXT, created REAL NOT NULL, PRIMARY KEY(channel,ts));
'''
# Additive columns after the module tables, in the order the runtime grew them (P38 run markers and switch reasons, the
# P3.2 / P36 card-thread columns, …).
COLUMNS = (
    ('consumer_runs', 'health_markers', 'INTEGER'),
    ('consumer_runs', 'route_reason', 'TEXT'),
    ('decision_proposals', 'meaning', 'TEXT'),
    ('rule_checks', 'summary', 'TEXT'),
    ('tasks', 'approvals_channel', 'TEXT'),
    ('tasks', 'approvals_root_ts', 'TEXT'),
    ('escalation_cards', 'task_id', 'TEXT'),
    ('escalation_cards', 'thread_root', 'TEXT'),
)
APPROVALS_ROOTS_DDL = (
    'CREATE TABLE IF NOT EXISTS approvals_roots ('
    '  channel TEXT NOT NULL, root_ts TEXT NOT NULL, task_id TEXT, home INTEGER NOT NULL DEFAULT 0,'
    '  created REAL NOT NULL, PRIMARY KEY (channel, root_ts));'
    'CREATE INDEX IF NOT EXISTS approvals_roots_task ON approvals_roots(task_id);')
# Later additive columns (dry smoke cards, P41 card-reply classes and deadline asks, P42 wake deadlines, P44 provider
# waits, P46 typed approvals and send mentions).
LATER_COLUMNS = (
    ('escalation_cards', 'dry', 'INTEGER NOT NULL DEFAULT 0'),
    ('ask_cards', 'dry', 'INTEGER NOT NULL DEFAULT 0'),
    ('card_reply_routes', 'reply_class', 'TEXT'),
    ('escalation_cards', 'about', 'TEXT'),
    ('task_deadlines', 'ask', 'TEXT'),
    ('task_deadlines', 'about_event', 'TEXT'),
    ('task_deadlines', 'about_pr', 'TEXT'),
    ('task_deadlines', 'wake', 'INTEGER'),
    ('task_deadlines', 'wake_result', 'TEXT'),
    ('dispatches', 'wait_reason', 'TEXT'),
    ('dispatches', 'wait_since', 'REAL'),
    ('rule_checks', 'typed_ref', 'TEXT'),
    ('rule_checks', 'typed_approved_at', 'REAL'),
    ('agent_sends', 'mentions', 'TEXT'),
)


def connect(path, initializers=()):
    """Open (creating if needed) the store at `path` and bring its schema up to date. `initializers`: the runtime's
    per-module `initialize(db)` callables, in order."""
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError('state_directory_must_be_private_0700')
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript(CORE_DDL)
    db.commit()
    for initialize in initializers:
        initialize(db)
    for table, column, decl in COLUMNS:
        add_column(db, table, column, decl)
    db.executescript(APPROVALS_ROOTS_DDL)
    for table, column, decl in LATER_COLUMNS:
        add_column(db, table, column, decl)
    db.commit()
    os.chmod(path, 0o600)
    return db


def add_column(db, table, column, decl):
    """Additive nullable column; safe when two processes race to add it."""
    if column in {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
        return False
    try:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')
    except sqlite3.OperationalError as exc:
        if 'duplicate column' not in str(exc):
            raise
        return False
    return True


@contextlib.contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise
