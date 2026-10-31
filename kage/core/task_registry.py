"""Task registry (task = workspace), parent links and shadow routing. No chat I/O.

SQLite is authoritative for tasks; goal.md `parent:` lines are only a generated
mirror. Everything here is additive: watches and events are read, never rewritten.
Routing is unchanged; `routing_shadow` only records what task addressing WOULD do.

Current session rule ("the most recently successfully run session, any provider"):
a session successfully ran in task T at time t when, on a non-smoke watch whose cwd
maps to T and which that session owns, one of these is recorded:
  - registration (`watch.started`),
  - an owner resolve: `watch.last_relevant`, a Claude claim released with reason
    `resolved`, the card `action_ts` recorded by resolve, or (lower bound for
    resolves that store no time, e.g. Codex `irrelevant`) the `received` time of
    an event the owner handled,
  - live from P0 on: a resolve / registration / handoff observed by the runtime
    (wall-clock time of that operation).
The current session is the owner with the greatest t. Ties: owner of a running
watch, then the later registration, then provider/session order. `set-current`
(audited) overrides until another session succeeds later.

`TaskRegistry` binds the tasks root: the environment variable that overrides it and its default, and the same for the
root of the smoke-dispatch TEST workspaces (only directories with SMOKE_PREFIX under that root are tasks, so nothing
else there gains a task identity). The functions that need no root are module functions."""
import datetime as dt
import functools
import json
import os
from pathlib import Path
import re
import time
import uuid

SMOKE_PREFIX = 'smoke-dispatch-'
PROVIDERS = ('codex', 'claude')
STATES = ('active', 'missing', 'archived')
LINK_KINDS = ('parent',)
PARENT_LINE_RE = re.compile(r'^parent:\s*(\S.*?)\s*$', re.I)
HEADING_RE = re.compile(r'^#{1,6}\s+(.+?)\s*#*\s*$')
EXCLUDED_DISPOSITIONS = ('smoke_test',)
DDL = '''
  CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT, state TEXT NOT NULL DEFAULT 'active',
    current_provider TEXT, current_session TEXT, current_source TEXT, last_run_at REAL,
    parent_task_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_task_id);
  CREATE TABLE IF NOT EXISTS task_links (
    parent_task_id TEXT NOT NULL, child_task_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'parent',
    created_at REAL NOT NULL, created_by_session TEXT,
    PRIMARY KEY(parent_task_id, child_task_id, kind));
  CREATE INDEX IF NOT EXISTS task_links_child ON task_links(child_task_id);
  CREATE TABLE IF NOT EXISTS task_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, action TEXT NOT NULL,
    old TEXT, new TEXT, actor TEXT NOT NULL, reason TEXT, at REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS routing_shadow (
    event_id TEXT PRIMARY KEY, watch_id TEXT NOT NULL, task_id TEXT, actual_provider TEXT NOT NULL,
    actual_session TEXT NOT NULL, task_provider TEXT, task_session TEXT, match INTEGER NOT NULL,
    reason TEXT NOT NULL, delivery TEXT, computed_at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS routing_shadow_time ON routing_shadow(computed_at);
'''
BACKFILL_KEY = 'tasks_backfill_v1'
DURATION_RE = re.compile(r'(\d+(?:\.\d+)?)\s*([smhd])')
NEUTRAL_REASONS = ('unbound_per_session', 'smoke_watch')


def initialize(db):
    db.executescript(DDL)


def is_smoke_task(task_id):
    return bool(task_id) and str(task_id).startswith(SMOKE_PREFIX)


def provider_of(w):
    return w.get('provider') or 'codex'


def _under(path, top):
    if path == top or not path.startswith(top.rstrip(os.sep) + os.sep):
        return None
    name = path[len(top.rstrip(os.sep)) + 1:].split(os.sep, 1)[0]
    return name if name and not name.startswith('.') else None


@functools.lru_cache(maxsize=8192)
def _task_id(cwd, base):
    path, top = os.path.normpath(cwd), os.path.normpath(base)
    if path == top or path.startswith(top.rstrip(os.sep) + os.sep):
        return _under(path, top)
    try:  # e.g. /tmp vs /private/tmp on macOS
        return _under(os.path.realpath(cwd), os.path.realpath(base))
    except OSError:
        return None


def _alias(name, base):
    """P39: a renamed task's old name stays a symlink `<root>/OLD -> NEW` (task-rename); a first-component symlink that
    points at a directory directly under the root IS that task. Outside the lru_cache (the link may appear or go at any
    time) and one lstat when it is not a link, so it stays cheap in loops."""
    link = os.path.join(base, name)
    try:
        if not os.path.islink(link):
            return name
        target, top = os.path.realpath(link), os.path.realpath(base)
    except (OSError, ValueError):
        return name
    resolved = os.path.basename(target)
    if os.path.dirname(target) == top and resolved and not resolved.startswith('.') and os.path.isdir(target):
        return resolved
    return name


def read_title(path):
    goal = Path(path) / 'goal.md'
    try:
        with goal.open(encoding='utf-8', errors='replace') as stream:
            for _, line in zip(range(80), stream):
                match = HEADING_RE.match(line.strip())
                if match:
                    return match[1][:200]
    except OSError:
        pass
    return None


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec='seconds') if ts else None


def actor_label(actor):
    provider, session = actor
    if session:
        return f'{provider}:{session}'
    try:
        import getpass
        return 'local:' + getpass.getuser()
    except Exception:  # noqa: BLE001
        return 'local:unknown'


def _row(db, task_id):
    row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    return dict(row) if row else None


def _all_watches(db):
    return [json.loads(r[0]) for r in db.execute('SELECT data FROM watches')]


def session_success(db, watches):
    """{(provider, session): {'at', 'running', 'started'}} over non-smoke watches (rule in module doc)."""
    ids = {w['id'] for w in watches}
    handled = {}
    for row in db.execute("SELECT watch_id, max(received), max(CAST(action_ts AS REAL)) FROM events "
                          "WHERE status='handled' AND coalesce(disposition,'') NOT IN (%s) GROUP BY watch_id"
                          % ','.join('?' * len(EXCLUDED_DISPOSITIONS)), EXCLUDED_DISPOSITIONS):
        if row[0] in ids:
            handled[row[0]] = max(x or 0 for x in row[1:])
    resolved = {}
    for row in db.execute("SELECT e.watch_id, max(c.released_at) FROM claude_claims c JOIN events e ON e.id=c.event_id "
                          "WHERE c.release_reason='resolved' GROUP BY e.watch_id"):
        if row[0] in ids:
            resolved[row[0]] = row[1] or 0
    out = {}
    for w in watches:
        if w.get('smoke'):
            continue
        key = (provider_of(w), w['session'])
        at = max(float(w.get('started') or 0), float(w.get('last_relevant') or 0),
                 handled.get(w['id'], 0), resolved.get(w['id'], 0))
        item = out.setdefault(key, {'at': 0.0, 'running': False, 'started': 0.0})
        item['at'] = max(item['at'], at)
        item['running'] = item['running'] or w.get('state') == 'running'
        item['started'] = max(item['started'], float(w.get('started') or 0))
    return out


def pick_current(success):
    if not success:
        return None
    key = max(success, key=lambda k: (success[k]['at'], success[k]['running'], success[k]['started'], k))
    return key, success[key]['at']


def running_session(db, task_id):
    """P38: (provider, session) of the task's newest RUNNING headless run, or None. A fresh run — every run of a
    long-lived task, a fallback, a context-cap rotation — is the task's live consumer while it works, although
    `current_session` only moves to it when it succeeds."""
    row = db.execute("SELECT provider, session FROM consumer_runs WHERE task=? AND state='running' AND session IS NOT NULL "
                     'ORDER BY started_at DESC LIMIT 1', (task_id,)).fetchone()
    return (row[0], row[1]) if row else None


def task_consumer(db, task_id, lease_live=None):
    """P38 G1: the ONE answer to "which session consumes this task's events now", shared by the approvals reconcile and
    the dispatcher's live-session reroute so the two never pull a watch back and forth: the running headless run, else
    the interactive lease the dispatcher recorded for a live session of the task (only when `lease_live(lease)` proves
    it live — no prober, no interactive answer), else `current_session`. (provider, session, source) or None."""
    running = running_session(db, task_id)
    if running:
        return (*running, 'running')
    if lease_live is not None:
        lease = db.execute("SELECT * FROM consumer_leases WHERE task_key=? AND holder_kind='interactive' AND session IS "
                           'NOT NULL', (task_id,)).fetchone()
        if lease is not None and lease_live(dict(lease)):
            return (lease['provider'], lease['session'], 'interactive')
    row = _row(db, task_id)
    if row is None or not row['current_session']:
        return None
    return (row['current_provider'] or 'codex', row['current_session'], 'current')


def audit(db, task_id, action, old, new, actor, reason=None, now=None):
    db.execute('INSERT INTO task_audit (task_id,action,old,new,actor,reason,at) VALUES (?,?,?,?,?,?,?)',
               (task_id, action, json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False),
                actor, reason, time.time() if now is None else now))


def set_current(db, task_id, provider, session, actor, reason=None, now=None):
    now = time.time() if now is None else now
    if provider not in PROVIDERS:
        raise ValueError('unsupported_provider')
    uuid.UUID(str(session))
    row = _row(db, task_id)
    if row is None:
        raise ValueError('unknown_task')
    old = {k: row[k] for k in ('current_provider', 'current_session', 'current_source', 'last_run_at')}
    db.execute("UPDATE tasks SET current_provider=?,current_session=?,current_source='manual',last_run_at=?,"
               'updated_at=? WHERE task_id=?', (provider, session, now, now, task_id))
    new = {'current_provider': provider, 'current_session': session, 'current_source': 'manual', 'last_run_at': now}
    audit(db, task_id, 'set_current', old, new, actor, reason, now)
    return {'ok': True, 'task_id': task_id, 'old': old, 'new': new}


def link_parent(db, child, parent, actor, session=None, kind='parent', reason=None, now=None):
    now = time.time() if now is None else now
    if kind not in LINK_KINDS:
        raise ValueError('unsupported_link_kind')
    if child == parent:
        raise ValueError('task_cannot_be_its_own_parent')
    child_row, parent_row = _row(db, child), _row(db, parent)
    if child_row is None or parent_row is None:
        raise ValueError('unknown_task')
    seen, cursor = set(), parent
    while cursor:  # refuse cycles: the new parent must not descend from the child
        if cursor == child:
            raise ValueError('parent_link_would_create_a_cycle')
        if cursor in seen:
            break
        seen.add(cursor)
        cursor = (_row(db, cursor) or {}).get('parent_task_id')
    old = {'parent_task_id': child_row['parent_task_id']}
    db.execute('UPDATE tasks SET parent_task_id=?,updated_at=? WHERE task_id=?', (parent, now, child))
    db.execute('INSERT OR IGNORE INTO task_links (parent_task_id,child_task_id,kind,created_at,created_by_session) '
               'VALUES (?,?,?,?,?)', (parent, child, kind, now, session))
    audit(db, child, 'link_parent', old, {'parent_task_id': parent}, actor, reason, now)
    return {'ok': True, 'task_id': child, 'parent_task_id': parent, 'previous_parent': old['parent_task_id']}


def sync_parent_lines(db, dry_run=True):
    """Compare goal.md `parent:` mirror lines with SQLite. P0 reports only; it never writes goal.md."""
    if not dry_run:
        raise ValueError('sync_parent_lines_writes_are_not_enabled_in_p0_use_--dry-run')
    changes = []
    for row in db.execute('SELECT task_id,path,parent_task_id FROM tasks ORDER BY task_id'):
        goal = Path(row['path']) / 'goal.md'
        current = None
        try:
            with goal.open(encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    match = PARENT_LINE_RE.match(line.strip())
                    if match:
                        current = match[1]
                        break
            exists = True
        except OSError:
            exists = False
        desired = row['parent_task_id']
        action = ('none' if current == desired else 'add' if current is None else
                  'remove' if desired is None else 'replace')
        if not exists and desired is not None:
            action = 'goal_md_missing'
        if action != 'none':
            changes.append({'task_id': row['task_id'], 'goal_md': str(goal), 'current': current,
                            'desired': f'parent: {desired}' if desired else None, 'action': action})
    return {'ok': True, 'dry_run': True, 'authoritative': 'sqlite', 'changes': changes}


def parse_since(value, now=None):
    if value in (None, ''):
        return None
    now = time.time() if now is None else now
    text = str(value).strip()
    match = DURATION_RE.fullmatch(text.lower())
    if match:
        return now - float(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('since_must_be_epoch_iso8601_or_duration_like_3d') from None
    if parsed.tzinfo is None:
        raise ValueError('since_iso8601_requires_timezone')
    return parsed.timestamp()


def shadow_report(db, since=None, top=20):
    since_ts = parse_since(since)
    rows = [dict(r) for r in db.execute('SELECT * FROM routing_shadow WHERE computed_at>=? ORDER BY computed_at',
                                        (since_ts or 0,))]
    def rate(items):
        return round(sum(r['match'] for r in items) / len(items), 4) if items else None
    bound = [r for r in rows if r['reason'] not in NEUTRAL_REASONS]
    reasons, diffs = {}, {}
    for r in rows:
        reasons[r['reason']] = reasons.get(r['reason'], 0) + 1
        if not r['match'] and r['reason'] not in NEUTRAL_REASONS:
            item = diffs.setdefault(r['task_id'] or '-', {'task_id': r['task_id'], 'diffs': 0, 'reasons': {},
                                                          'actual': set(), 'task_target': None})
            item['diffs'] += 1
            item['reasons'][r['reason']] = item['reasons'].get(r['reason'], 0) + 1
            item['actual'].add(f"{r['actual_provider']}:{r['actual_session']}")
            item['task_target'] = f"{r['task_provider']}:{r['task_session']}" if r['task_session'] else None
    tasks = sorted(diffs.values(), key=lambda x: (-x['diffs'], x['task_id'] or ''))[:top]
    for item in tasks:
        item['actual'] = sorted(item['actual'])
    return {'ok': True, 'since': _iso(since_ts) if since_ts else None, 'total': len(rows),
            'matched': sum(r['match'] for r in rows), 'match_rate': rate(rows),
            'task_bound': {'total': len(bound), 'matched': sum(r['match'] for r in bound), 'match_rate': rate(bound)},
            'reasons': dict(sorted(reasons.items())),
            'diff_reasons': {k: v for k, v in sorted(reasons.items()) if k not in ('match', *NEUTRAL_REASONS)},
            'zero_diff': all(r['match'] for r in bound),
            'first': _iso(rows[0]['computed_at']) if rows else None, 'last': _iso(rows[-1]['computed_at']) if rows else None,
            'tasks_with_diffs': tasks}


class TaskRegistry:
    """The root-bound half of the registry: task identity of a cwd, task rows, listings and shadow routing."""

    def __init__(self, *, root_env, default_root, smoke_root_env, default_smoke_root):
        self.root_env, self.default_root = root_env, default_root
        self.smoke_root_env, self.default_smoke_root = smoke_root_env, default_smoke_root

    def root(self):
        return Path(os.environ.get(self.root_env) or self.default_root).expanduser()

    def smoke_root(self):
        return Path(os.environ.get(self.smoke_root_env) or self.default_smoke_root).expanduser()

    def task_dir(self, task_id):
        """Workspace directory of a task id (smoke-dispatch TEST tasks live under the smoke root)."""
        return (self.smoke_root() if is_smoke_task(task_id) else self.root()) / task_id

    def task_id_for_cwd(self, cwd, base=None):
        """Workspace directory name under the tasks root; None for the root itself, hidden dirs or outside it. A cwd under
        a renamed task's old-name symlink maps to the new name (P39)."""
        if not cwd:
            return None
        top = str(base or self.root())
        found = _task_id(str(cwd), top)
        if found is not None:
            return _alias(found, top)
        if base is None:
            smoke = _task_id(str(cwd), str(self.smoke_root()))
            if smoke and smoke.startswith(SMOKE_PREFIX):
                return smoke
        return found

    def _task_watches(self, db, task_id, watches=None):
        return [w for w in (watches if watches is not None else _all_watches(db))
                if self.task_id_for_cwd(w.get('cwd')) == task_id]

    def ensure_task(self, db, task_id, now, created_at=None):
        """Inside a transaction: create the task row once (path/title/state from the workspace)."""
        if task_id is None or _row(db, task_id) is not None:
            return False
        path = self.task_dir(task_id)
        db.execute('INSERT OR IGNORE INTO tasks (task_id,path,title,state,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                   (task_id, str(path), read_title(path), 'active' if path.is_dir() else 'missing',
                    created_at if created_at is not None else now, now))
        return True

    def backfill(self, db, now=None, dry_run=False):
        """One task per distinct task cwd; fills only missing rows / missing current sessions."""
        now = time.time() if now is None else now
        watches = _all_watches(db)
        groups, unbound = {}, 0
        for w in watches:
            tid = self.task_id_for_cwd(w.get('cwd'))
            if tid is None:
                unbound += 1
            else:
                groups.setdefault(tid, []).append(w)
        report = {'ok': True, 'dry_run': dry_run, 'watches': len(watches), 'watches_bound': len(watches) - unbound,
                  'watches_unbound_per_session': unbound, 'tasks_seen': len(groups), 'tasks_created': 0,
                  'current_filled': 0, 'multi_session_tasks': 0, 'tasks': []}
        for tid in sorted(groups):
            success = session_success(db, groups[tid])
            if len(success) > 1:
                report['multi_session_tasks'] += 1
            picked = pick_current(success)
            existing = _row(db, tid)
            entry = {'task_id': tid, 'owners': len(success),
                     'current': f'{picked[0][0]}:{picked[0][1]}' if picked else None,
                     'last_run_at': _iso(picked[1]) if picked else None,
                     'action': 'create' if existing is None else 'fill_current' if not existing['current_session'] else 'keep'}
            report['tasks'].append(entry)
            if entry['action'] == 'create':
                report['tasks_created'] += 1
            elif entry['action'] == 'fill_current' and picked:
                report['current_filled'] += 1
            if entry['action'] == 'keep' or dry_run:
                continue
            if existing is None:
                self.ensure_task(db, tid, now, min(float(w.get('started') or now) for w in groups[tid]))
            if picked:
                db.execute("UPDATE tasks SET current_provider=?,current_session=?,current_source='backfill',last_run_at=?,"
                           'updated_at=? WHERE task_id=? AND current_session IS NULL',
                           (picked[0][0], picked[0][1], picked[1], now, tid))
        return report

    def note_success(self, db, provider, session, cwd, at, source):
        """Inside a transaction: `session` just ran successfully in the task of `cwd`."""
        tid = self.task_id_for_cwd(cwd)
        if tid is None or not session:
            return None
        self.ensure_task(db, tid, at)
        db.execute('UPDATE tasks SET current_provider=?,current_session=?,current_source=?,last_run_at=?,updated_at=? '
                   'WHERE task_id=? AND (last_run_at IS NULL OR last_run_at<=?)',
                   (provider, session, source, at, at, tid, at))
        return tid

    def summary(self, db, row, watches):
        mine = self._task_watches(db, row['task_id'], watches)
        return {**row, 'last_run_at_iso': _iso(row['last_run_at']), 'watches': len(mine),
                'running_watches': sum(w.get('state') == 'running' for w in mine),
                'owners': sorted({f'{provider_of(w)}:{w["session"]}' for w in mine if not w.get('smoke')})}

    def list_tasks(self, db, include_missing=True):
        watches = _all_watches(db)
        rows = [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY coalesce(last_run_at,0) DESC, task_id')]
        return {'ok': True, 'root': str(self.root()),
                'tasks': [self.summary(db, r, watches) for r in rows if include_missing or r['state'] != 'missing']}

    def show_task(self, db, task_id):
        row = _row(db, task_id)
        if row is None:
            raise ValueError('unknown_task')
        watches = self._task_watches(db, task_id)
        success = session_success(db, watches)
        return {'ok': True, 'task': self.summary(db, row, watches),
                'sessions': [{'provider': k[0], 'session': k[1], 'last_success_at': _iso(v['at']), 'running': v['running']}
                             for k, v in sorted(success.items(), key=lambda item: -item[1]['at'])],
                'watches': [{'id': w['id'], 'provider': provider_of(w), 'session': w['session'], 'state': w.get('state'),
                             'kind': w.get('kind'), 'channel': w.get('channel'), 'thread': w.get('thread'),
                             'smoke': bool(w.get('smoke'))} for w in watches],
                'links': [dict(r) for r in db.execute('SELECT * FROM task_links WHERE parent_task_id=? OR child_task_id=? '
                                                      'ORDER BY created_at', (task_id, task_id))],
                'audit': [dict(r) for r in db.execute('SELECT * FROM task_audit WHERE task_id=? ORDER BY id', (task_id,))]}

    def classify_route(self, db, w, cache=None):
        """Task-addressed target for an event delivered to watch `w`, and why it may differ."""
        actual = (provider_of(w), w['session'])
        tid = self.task_id_for_cwd(w.get('cwd'))
        if tid is None:
            return tid, actual, True, 'unbound_per_session'
        cache = {} if cache is None else cache
        row = cache.get(tid) or _row(db, tid)
        cache[tid] = row
        target = (row['current_provider'], row['current_session']) if row else (None, None)
        match = target == actual
        if w.get('smoke'):  # test cards are neutral in the report, whatever the task state
            return tid, target, match, 'smoke_watch'
        if row is None:
            return tid, target, False, 'task_not_registered'
        if not target[1]:
            return tid, target, False, 'task_without_current_session'
        if match:
            return tid, target, True, 'match'
        if row['current_source'] == 'manual':
            return tid, target, False, 'manual_override'
        owners = {(provider_of(x), x['session']) for x in self._task_watches(db, tid) if not x.get('smoke')}
        if target not in owners:
            return tid, target, False, 'current_session_owns_no_watch'
        if target[0] != actual[0]:
            return tid, target, False, 'multi_session_task_cross_provider'
        return tid, target, False, 'multi_session_task'

    def record_shadow(self, db, w, event_ids, delivery, now=None):
        """Own transaction; INSERT OR IGNORE keeps the first delivery of each event."""
        if not event_ids:
            return 0
        now = time.time() if now is None else now
        tid, target, match, reason = self.classify_route(db, w)
        db.execute('BEGIN IMMEDIATE')
        try:
            for eid in event_ids:
                db.execute('INSERT OR IGNORE INTO routing_shadow (event_id,watch_id,task_id,actual_provider,actual_session,'
                           'task_provider,task_session,match,reason,delivery,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (eid, w['id'], tid, provider_of(w), w['session'], target[0], target[1], int(bool(match)),
                            reason, delivery, now))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return len(event_ids)
