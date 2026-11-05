"""The instance mode (onboarding design §2, O2a) and the observe-only guard (§3.4, O2b).

`instance.mode` lives in the store's `runtime_meta`: `installed` (kage init wrote the deployment) -> `observe` (the
required doctor checks passed when the owner started the services) -> `active` (the owner's verified button click)
<-> `paused` (the owner, the budget breaker); the owner may also go back to `observe`. Only the engine writes it, and
every change is a `mode_log` row. A store without the row predates O2 (an adopted deployment that has run for months):
it is `active`, so nothing changes for it.

While the mode is not `active` nothing leaves Kage on the owner's behalf: a Slack write (post, update, delete,
reaction, upload, channel change) goes out only to a destination the private-destination guard has verified (the
owner's own card channel or IM), every other one becomes a `shadow_actions` row {kind, target, what, why} and raises
`ShadowedWrite`; the dispatcher starts no run (its would-be runs are shadow rows too). Enforced in the transport and
the dispatcher, never in prompts.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
import time

MODES = ('installed', 'observe', 'active', 'paused')
KEY = 'instance.mode'
ABSENT = 'active'  # a store without the row predates O2
# (old, new): who may make the change
TRANSITIONS = {('installed', 'observe'): ('services',), ('observe', 'active'): ('owner_button',),
               ('active', 'paused'): ('owner', 'breaker'), ('paused', 'active'): ('owner',),
               ('active', 'observe'): ('owner',), ('paused', 'observe'): ('owner',), ('observe', 'paused'): ('owner',)}
SCHEMA = '''
  CREATE TABLE IF NOT EXISTS runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS mode_log (at REAL NOT NULL, old TEXT, new TEXT NOT NULL, actor TEXT NOT NULL,
                                       reason TEXT);
  CREATE TABLE IF NOT EXISTS shadow_actions (id TEXT PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, target TEXT,
                                             what TEXT NOT NULL, why TEXT NOT NULL);
'''
# Slack Web API methods that change something someone else can see; every other call is a read
WRITE_ENDPOINTS = frozenset((
    'chat.postMessage', 'chat.update', 'chat.delete', 'chat.postEphemeral', 'chat.scheduleMessage', 'chat.meMessage',
    'chat.unfurl', 'reactions.add', 'reactions.remove', 'pins.add', 'pins.remove', 'bookmarks.add', 'bookmarks.edit',
    'bookmarks.remove', 'files.upload', 'files.getUploadURLExternal', 'files.completeUploadExternal', 'files.delete',
    'conversations.create', 'conversations.join', 'conversations.invite', 'conversations.kick',
    'conversations.archive', 'conversations.rename', 'conversations.setTopic', 'conversations.setPurpose',
    'conversations.leave', 'conversations.mark', 'reminders.add', 'users.profile.set', 'dnd.setSnooze',
    'views.open', 'views.push', 'views.update', 'views.publish'))
DESTINATION_META = 'card_destination:'  # kage.adapters.slack.destinations: a verified verdict per channel
PRIVATE_VERDICTS = ('im', 'private')


class ShadowedWrite(RuntimeError):
    """A write the observe-only guard recorded instead of sending."""


def initialize(db):
    db.executescript(SCHEMA)


def read(db, default=ABSENT):
    """The instance mode; `default` when the store has no row (or no table yet)."""
    try:
        row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (KEY,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row[0] if row and row[0] in MODES else default


def write(db, new, actor, reason='', now=None, expect=None):
    """Move the mode to `new` as `actor` (a kind from TRANSITIONS: services, owner_button, owner, breaker, init).
    `init` writes the first row of a new deployment. Returns {'old', 'new'}; ValueError when not allowed."""
    if new not in MODES:
        raise ValueError(f'unknown_mode:{new}')
    now = time.time() if now is None else now
    initialize(db)
    row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (KEY,)).fetchone()
    old = row[0] if row else None
    if expect is not None and old != expect:
        raise ValueError(f'mode_is_{old or "unset"}_not_{expect}')
    if actor == 'init':
        if old is not None:
            return {'old': old, 'new': old}  # a re-run of init never moves a mode
    elif old == new:
        return {'old': old, 'new': new}
    elif actor not in TRANSITIONS.get((old or ABSENT, new), ()):
        raise ValueError(f'mode_change_not_allowed:{old or ABSENT}->{new}:{actor}')
    with db:
        db.execute('INSERT OR REPLACE INTO runtime_meta (key, value) VALUES (?, ?)', (KEY, new))
        db.execute('INSERT INTO mode_log (at, old, new, actor, reason) VALUES (?, ?, ?, ?, ?)',
                   (now, old, new, actor, str(reason or '')[:200]))
    return {'old': old, 'new': new}


def shadow(db, kind, target, what, why, now=None):
    """Record one action the guard did not take (idempotent: the same action is one row)."""
    now = time.time() if now is None else now
    key = hashlib.sha256(json.dumps([kind, target, what], sort_keys=True, default=str).encode()).hexdigest()[:16]
    initialize(db)
    with db:
        db.execute('INSERT OR IGNORE INTO shadow_actions (id, at, kind, target, what, why) VALUES (?, ?, ?, ?, ?, ?)',
                   (key, now, kind, None if target is None else str(target)[:200], str(what)[:500], str(why)[:200]))
    return key


def overview(db, limit=20):
    """The mode, its recent changes and the latest shadow actions (counts per kind); read-only."""
    out = {'mode': read(db), 'log': [], 'shadow': {'counts': {}, 'recent': []}}
    try:
        out['log'] = [dict(zip(('at', 'old', 'new', 'actor', 'reason'), r)) for r in db.execute(
            'SELECT at, old, new, actor, reason FROM mode_log ORDER BY at DESC LIMIT ?', (limit,))]
        out['shadow']['counts'] = dict(db.execute('SELECT kind, count(*) FROM shadow_actions GROUP BY kind'))
        out['shadow']['recent'] = [dict(zip(('at', 'kind', 'target', 'what', 'why'), r)) for r in db.execute(
            'SELECT at, kind, target, what, why FROM shadow_actions ORDER BY at DESC LIMIT ?', (limit,))]
    except sqlite3.OperationalError:
        pass
    return out


def _channel(payload):
    for key in ('channel', 'channel_id', 'channels'):
        value = (payload or {}).get(key)
        if value:
            return str(value).split(',')[0]
    return None


def _verified_private(db, channel):
    if not channel:
        return False
    try:
        row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (DESTINATION_META + channel,)).fetchone()
        return bool(row) and json.loads(row[0]).get('verdict') in PRIVATE_VERDICTS
    except (sqlite3.OperationalError, ValueError, AttributeError):
        return False


class WriteGuard:
    """The transport's observe-only guard for a generated deployment's store. Called with every Slack call before it
    is sent: returns when it may go, raises ShadowedWrite after recording it otherwise. Fails closed: a store it cannot
    read counts as `installed` (a generated deployment's store always has the row)."""

    def __init__(self, db_path, *, default='installed', ttl=5.0, clock=time.time):
        self.db_path, self.default, self.ttl, self.clock = db_path, default, ttl, clock
        self._cached = (None, 0.0)

    def mode(self, db):
        value, at = self._cached
        now = self.clock()
        if value is None or now - at > self.ttl:
            value = read(db, self.default)
            self._cached = (value, now)
        return value

    def __call__(self, endpoint, payload):
        if endpoint not in WRITE_ENDPOINTS:
            return
        path = Path(self.db_path() if callable(self.db_path) else self.db_path)
        try:  # never creates a store: a missing one cannot say the instance is active
            db = sqlite3.connect(path.absolute().as_uri() + '?mode=rw', uri=True, timeout=10)
        except sqlite3.Error:
            raise ShadowedWrite(f'observe_mode_store_unreadable:{endpoint}') from None
        try:
            try:
                mode = self.mode(db)
            except sqlite3.Error:
                mode = self.default
            if mode == 'active':
                return
            channel = _channel(payload)
            if endpoint in ('chat.postMessage', 'chat.update', 'chat.delete') and _verified_private(db, channel):
                return  # the owner's own verified card destination: doctor results, the digest, the start card
            shadow(db, 'slack_write', channel, json.dumps({'endpoint': endpoint, 'text': str(
                (payload or {}).get('text') or (payload or {}).get('name') or '')[:300]}, ensure_ascii=False),
                f'mode_{mode}')
        finally:
            db.close()
        raise ShadowedWrite(f'observe_mode:{mode}:{endpoint}')
