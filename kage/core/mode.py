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

The start card (O2d): while the mode is `observe`, once a day a code-owned, button-only card to the owner's private
destination says what observe-only mode recorded (counts per kind, the latest lines) with [开始干活] / [再看一天]; only
the owner's own click on [开始干活] moves the mode to `active`.

Owner corrections (O2f): on the console the owner may say what a shadow `triage` row should have been (a route and a
note); the correction is kept next to the row (`shadow_corrections`) as evidence for the next onboarding step and
changes nothing: no mode, dispatch or card reads it.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from kage import locale
from kage.core.secrets import check_secret_free

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
# O2f: created with the first correction only, so a store with nothing to correct is never written
CORRECTION_SCHEMA = '''CREATE TABLE IF NOT EXISTS shadow_corrections (shadow_id TEXT PRIMARY KEY, route TEXT NOT NULL,
                                                                  note TEXT, at REAL NOT NULL)'''
CORRECTION_NOTE_MAX = 300
SHADOW_ID_RE = re.compile(r'[0-9a-f]{16}')
AGENT_ENV = ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID')  # set in an agent session, never by the owner


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
        out['shadow']['recent'] = [dict(zip(('id', 'at', 'kind', 'target', 'what', 'why'), r)) for r in db.execute(
            'SELECT id, at, kind, target, what, why FROM shadow_actions ORDER BY at DESC LIMIT ?', (limit,))]
    except sqlite3.OperationalError:
        pass
    return out


def corrections(db):
    """O2f: the owner's corrections by shadow id, {route, note, at}; empty when none was ever made. Read-only."""
    try:
        return {r[0]: {'route': r[1], 'note': r[2], 'at': r[3]} for r in db.execute(
            'SELECT shadow_id, route, note, at FROM shadow_corrections')}
    except sqlite3.OperationalError:
        return {}


def correct(db, shadow_id, route, note=None, now=None, env=None):
    """O2f: the owner's correction of a shadow `triage` row: the route it should have had (kage.core.observe.ROUTES)
    and an optional note (at most 300 characters, never a credential). One per row; a new one replaces it. Never from an
    agent session (`env`: the process environment). KeyError for an id that is not a shadow row; ValueError otherwise."""
    from kage.core.observe import ROUTES  # observe imports this module
    env = os.environ if env is None else env
    if any(env.get(k) for k in AGENT_ENV):
        raise ValueError('only_the_owner_corrects_shadow_records')
    if not isinstance(shadow_id, str) or not SHADOW_ID_RE.fullmatch(shadow_id):
        raise ValueError('invalid_shadow_id')
    if route not in ROUTES:
        raise ValueError('route_must_be_one_of_' + '_'.join(ROUTES))
    if note is not None and not isinstance(note, str):
        raise ValueError('note_must_be_text')
    note = ' '.join((note or '').split()) or None
    if note is not None and len(note) > CORRECTION_NOTE_MAX:
        raise ValueError(f'note_must_be_at_most_{CORRECTION_NOTE_MAX}_characters')
    check_secret_free(note)
    now = time.time() if now is None else now
    try:
        row = db.execute('SELECT kind FROM shadow_actions WHERE id=?', (shadow_id,)).fetchone()
    except sqlite3.OperationalError:  # no shadow table: a store that never observed (an adopted deployment)
        row = None
    if row is None:
        raise KeyError('unknown_shadow_record')
    if row[0] != 'triage':
        raise ValueError('only_triage_records_are_corrected')
    with db:
        db.execute(CORRECTION_SCHEMA)
        db.execute('INSERT OR REPLACE INTO shadow_corrections (shadow_id, route, note, at) VALUES (?, ?, ?, ?)',
                   (shadow_id, route, note, now))
    return {'ok': True, 'id': shadow_id, 'correction': {'route': route, 'note': note, 'at': now}}


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


CARD_SCHEMA = '''
  CREATE TABLE IF NOT EXISTS mode_cards (event_id TEXT PRIMARY KEY, posted_at REAL NOT NULL, state TEXT NOT NULL,
                                         settled_at REAL);
  CREATE TABLE IF NOT EXISTS button_only_cards (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, at REAL NOT NULL);
'''
DIGEST_SECONDS = 86400   # one start card a day while the mode stays observe
CARD_TTL = 2 * 86400     # an unanswered card expires; the next one comes with the next day's digest
DIGEST_LINES = 5


def _owner_user(rt):
    from kage.core import control as _control
    return _control._owner_user(rt)


class StartCards:
    """O2d: the daily start card of an observe-only instance (a supervisor step; nothing while the mode is not observe,
    so an active instance only reads one row)."""

    def __init__(self, language, *, owner_key, owner_name):
        self.language, self.key, self.name = language, owner_key, owner_name

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def step(self, rt, db, now=None):
        if read(db) != 'observe':
            return {}
        now = time.time() if now is None else now
        db.executescript(SCHEMA + CARD_SCHEMA)
        out = {'settled': self.settle(rt, db, now)}
        if read(db) == 'observe':
            open_card = db.execute("SELECT 1 FROM mode_cards WHERE state='open'").fetchone()
            last = db.execute('SELECT max(posted_at) FROM mode_cards').fetchone()[0]
            if not open_card and (last is None or now - last >= DIGEST_SECONDS):
                out['posted'] = self.post(rt, db, now)
        return out

    def digest(self, db, since=None):
        where, args = ('WHERE at>=?', (since,)) if since else ('', ())
        counts = dict(db.execute(f'SELECT kind, count(*) FROM shadow_actions {where} GROUP BY kind', args))
        latest = [self.line(kind, what) for kind, what in db.execute(
            f'SELECT kind, what FROM shadow_actions {where} ORDER BY at DESC LIMIT ?', (*args, DIGEST_LINES))]
        return counts, latest

    def line(self, kind, what):
        """One shadow action as a card line: a triage row (O2c) as its route and why, any other row as recorded."""
        if kind == 'triage':
            try:
                value = json.loads(what)
                return self.msg('mode.triage_line', route=self.msg(f"mode.route.{value['route']}"), why=value['why'] or '')
            except (ValueError, KeyError, TypeError, LookupError):
                pass
        return what

    def post(self, rt, db, now):
        owner = _owner_user(rt)
        counts, latest = self.digest(db)
        total = sum(counts.values())
        summary = self.msg('mode.card.counts_sep').join(
            self.msg(f'mode.kind.{k}', n=n) if f'mode.kind.{k}' in locale.catalog(self.language)['messages'] else f'{k} {n}'
            for k, n in sorted(counts.items())) or self.msg('mode.card.nothing')
        lines = '\n'.join('• ' + ' '.join(str(w).split())[:140] for w in latest) or self.msg('mode.card.no_lines')
        question = self.msg('mode.card.question', owner=f'<@{owner}>' if owner else self.name, total=total,
                            summary=summary, lines=lines)
        base = getattr(rt, 'DASHBOARD_URL', None)  # O2e: the console's list of everything recorded
        if isinstance(base, str) and base.startswith(('http://', 'https://')):
            question += '\n' + self.msg('mode.card.link', url=base.rstrip('/') + '/onboarding')
        card = rt.supervisor_card(db, question, [f"start={self.msg('mode.card.start')}:approve",
                                                 f"later={self.msg('mode.card.later')}:other"],
                                  never_auto=True, kind='mode_start', refs={'mode': 'observe'}, now=now,
                                  first_line_max=None)
        with db:
            db.execute("INSERT OR IGNORE INTO mode_cards (event_id, posted_at, state) VALUES (?, ?, 'open')",
                       (card['event_id'], now))
            db.execute('INSERT OR IGNORE INTO button_only_cards VALUES (?, ?, ?)', (card['event_id'], 'mode_start', now))
        return card['event_id']

    def settle(self, rt, db, now):
        """Each open start card -> one outcome: only the owner's own button click (provenance slack_button, role approver)
        on [开始干活] moves observe -> active; [再看一天] keeps observing; anything else is refused; no click within
        CARD_TTL -> expired. The claim (open -> deciding) makes a second supervisor do nothing."""
        done = []
        owner = _owner_user(rt)
        for c in [dict(zip(('event_id', 'posted_at'), r)) for r in db.execute(
                "SELECT event_id, posted_at FROM mode_cards WHERE state='open' ORDER BY posted_at")]:
            try:
                d = db.execute('SELECT d.actor, d.provenance, d.role, d.choice, c.options FROM decisions d JOIN '
                               'escalation_cards c ON c.event_id=d.event_id WHERE d.event_id=?', (c['event_id'],)).fetchone()
            except sqlite3.OperationalError:
                d = None
            if d is None and now - c['posted_at'] < CARD_TTL:
                continue
            with db:
                claimed = db.execute("UPDATE mode_cards SET state='deciding' WHERE event_id=? AND state='open'",
                                     (c['event_id'],))
            if claimed.rowcount != 1:
                continue
            if d is None:
                state = 'expired'
            else:
                actor, provenance, role, choice, options = d
                kind = rt.buttons.resolve_choice(choice, rt.buttons.card_model(options))['kind']
                if not owner or actor != owner or provenance != 'slack_button' or role != 'approver':
                    state = 'refused'
                elif kind == 'approve':
                    try:
                        write(db, 'active', 'owner_button', 'the start card', now=now, expect='observe')
                        state = 'started'
                    except ValueError:  # the mode changed since the card was posted
                        state = 'stale'
                else:
                    state = 'later'
            with db:
                db.execute('UPDATE mode_cards SET state=?, settled_at=? WHERE event_id=?', (state, now, c['event_id']))
            self._result(rt, db, c['event_id'], self.msg(f'mode.result.{state}'))
            done.append({'event_id': c['event_id'], 'state': state})
        return done

    @staticmethod
    def _result(rt, db, card_event, text):
        """Best effort: a bot line in the card's thread saying what happened."""
        try:
            esc = db.execute('SELECT thread_ts FROM escalations WHERE event_id=?', (card_event,)).fetchone()
            card = db.execute('SELECT channel FROM escalation_cards WHERE event_id=?', (card_event,)).fetchone()
            if not esc or not card or not esc[0]:
                return None
            sent = rt.slack.api_call('chat.postMessage', rt.api_identity(bot=True), json_body={
                'channel': card[0], 'thread_ts': esc[0], 'text': text, 'unfurl_links': False})
            return (sent or {}).get('ts')
        except Exception:  # noqa: BLE001 - the outcome stands; only the receipt is missing
            return None
