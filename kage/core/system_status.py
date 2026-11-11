"""System status (0.78.6, F18): disk, memory, CPU and processes of the host the runtime runs on. Stdlib only, Linux
first (cgroup v2 / v1, /proc), macOS shows what it can. Deterministic: no model runs here, no message text is read.

`probe(...)` is the cheap live read the console makes on every page load: statvfs of the filesystems holding the home
directory and the state database, the memory and CPU of the cgroup (else /proc/meminfo, /proc/stat), the load average
and the process counts — no directory walk. `walk_sizes` sums the watched directories (agent sessions, the task
workspaces, the state directory and its backups, `system.watch_paths`) within a time budget; the supervisor runs it at
most every `system.consumers_minutes` in a background thread and stores the result, the console shows the stored one.

`Monitor.evaluate` (a supervisor step, every `system.check_minutes`) compares disk and memory with the thresholds in the
worker config (`system.*`, a sparse section: missing keys take SYSTEM_DEFAULTS) and posts the owner ONE notice per
breach episode in the decision channel: a level reached (warning, critical; warning -> critical is a new notice), and
one when the value is back under the warning line by RECOVER_MARGIN points. A failed post is retried on the next pass.
`system.enabled=false`: no checks, no notices (the console still shows the live numbers).
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from kage import locale
from kage.core.worker_config import SYSTEM_DEFAULTS

OK, WARNING, CRITICAL = 'ok', 'warning', 'critical'
RANK = {OK: 0, WARNING: 1, CRITICAL: 2}
METRICS = ('disk', 'memory')
RECOVER_MARGIN = 3  # percentage points under a line before a level is left (no notice flapping around a threshold)
WALK_SECONDS = 120  # the budget of one directory-size pass (all watched directories together)
WALK_ENTRIES = 2_000_000
CGROUP = Path('/sys/fs/cgroup')
PROC = Path('/proc')

DDL = '''
  CREATE TABLE IF NOT EXISTS system_alerts (
    metric TEXT PRIMARY KEY, level TEXT NOT NULL, since REAL, value REAL, announced TEXT NOT NULL DEFAULT 'ok',
    notice_ts TEXT, notice_error TEXT, updated_at REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, metric TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT, at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS system_events_at ON system_events(at);
  CREATE TABLE IF NOT EXISTS system_snapshot (
    id INTEGER PRIMARY KEY CHECK (id=1), checked_at REAL, status TEXT, consumers TEXT, consumers_at REAL);
'''


def initialize(db):
    db.executescript(DDL)
    db.commit()


def settings(config):
    """The `system` section of a loaded config with its defaults."""
    raw = (config or {}).get('system') if isinstance(config, dict) else None
    return {**SYSTEM_DEFAULTS, **(raw if isinstance(raw, dict) else {})}


# ---------------------------------------------------------------------------
# Live probe (cheap: files and syscalls, no walk)
# ---------------------------------------------------------------------------

def _read(path):
    try:
        return Path(path).read_text(encoding='ascii', errors='replace').strip()
    except OSError:
        return None


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if part is not None and whole else None


def _mount_point(path):
    path = Path(path).resolve()
    dev = path.stat().st_dev
    while path.parent != path and path.parent.stat().st_dev == dev:
        path = path.parent
    return str(path)


def disks(paths):
    """One entry per filesystem holding one of `paths` ({label: path}); df semantics: pct = used / (used + available)."""
    out, seen = [], {}
    for label, path in paths.items():
        try:
            path = Path(path).expanduser()
            st, fs = path.stat(), os.statvfs(path)
        except OSError:
            continue
        if st.st_dev in seen:
            seen[st.st_dev]['holds'].append(label)
            continue
        total, avail = fs.f_blocks * fs.f_frsize, fs.f_bavail * fs.f_frsize
        used = (fs.f_blocks - fs.f_bfree) * fs.f_frsize
        inodes_used = fs.f_files - fs.f_ffree if fs.f_files else None
        try:
            mount = _mount_point(path)
        except OSError:
            mount = str(path)
        entry = {'mount': mount, 'holds': [label], 'total': total, 'used': used, 'free': avail,
                 'pct': _pct(used, used + avail), 'inodes_pct': _pct(inodes_used, fs.f_files) if inodes_used is not None else None}
        seen[st.st_dev] = entry
        out.append(entry)
    return out


def _cgroup_dir(proc=PROC, cgroup=CGROUP):
    """The process's own cgroup v2 directory (a container usually sees '0::/' = the root), else None."""
    text = _read(proc / 'self' / 'cgroup') or ''
    for line in text.splitlines():
        if line.startswith('0::'):
            rel = line[3:].strip().lstrip('/')
            for d in (cgroup / rel, cgroup):
                if (d / 'cgroup.controllers').exists():
                    return d
    return None


def _kv(text):
    out = {}
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0].rstrip(':')] = _int(parts[1])
    return out


def memory(proc=PROC, cgroup=CGROUP):
    """{source, used, limit, pct, oom_kills?}. cgroup v2: the working set (memory.current - inactive_file, what the OOM
    killer cannot reclaim) against memory.max (or the host's MemTotal when unlimited); cgroup v1 the same with its file
    names; /proc/meminfo: MemTotal - MemAvailable; macOS: the physical size only."""
    meminfo = _kv(_read(proc / 'meminfo'))
    host_total = meminfo.get('MemTotal') * 1024 if meminfo.get('MemTotal') else None
    d = _cgroup_dir(proc, cgroup)
    if d is not None and (d / 'memory.current').exists():
        current = _int(_read(d / 'memory.current'))
        limit = _int(_read(d / 'memory.max'))  # 'max' = unlimited -> None
        inactive = _kv(_read(d / 'memory.stat')).get('inactive_file') or 0
        events = _kv(_read(d / 'memory.events'))
        if current is not None:
            used = max(current - inactive, 0)
            total = limit or host_total
            return {'source': 'cgroup2', 'used': used, 'limit': total, 'limited': bool(limit), 'pct': _pct(used, total),
                    'oom_kills': events.get('oom_kill')}
    v1 = cgroup / 'memory'
    if (v1 / 'memory.usage_in_bytes').exists():
        usage = _int(_read(v1 / 'memory.usage_in_bytes'))
        limit = _int(_read(v1 / 'memory.limit_in_bytes'))
        inactive = _kv(_read(v1 / 'memory.stat')).get('total_inactive_file') or 0
        if limit and host_total and limit >= host_total:
            limit = None  # v1 writes a huge number for "no limit"
        if usage is not None:
            used = max(usage - inactive, 0)
            total = limit or host_total
            return {'source': 'cgroup1', 'used': used, 'limit': total, 'limited': bool(limit), 'pct': _pct(used, total)}
    if host_total and meminfo.get('MemAvailable') is not None:
        used = host_total - meminfo['MemAvailable'] * 1024
        return {'source': 'meminfo', 'used': used, 'limit': host_total, 'limited': False, 'pct': _pct(used, host_total)}
    try:
        total = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (ValueError, OSError, AttributeError):
        total = None
    return {'source': 'sysconf' if total else None, 'used': None, 'limit': total, 'limited': False, 'pct': None}


def cpu_sample(proc=PROC, cgroup=CGROUP):
    """{source, busy_seconds, at, limit_cores}: the cgroup's CPU time (cpu.stat usage_usec) and quota (cpu.max), else
    the host's busy time from /proc/stat; None busy on macOS (the load average is shown instead)."""
    cores = None
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = os.cpu_count()
    d = _cgroup_dir(proc, cgroup)
    if d is not None and (d / 'cpu.stat').exists():
        usage = _kv(_read(d / 'cpu.stat')).get('usage_usec')
        quota = (_read(d / 'cpu.max') or 'max').split()
        limit = cores
        if len(quota) == 2 and quota[0] != 'max' and _int(quota[0]) and _int(quota[1]):
            limit = round(_int(quota[0]) / _int(quota[1]), 2)
        if usage is not None:
            return {'source': 'cgroup2', 'busy_seconds': usage / 1e6, 'at': time.time(), 'limit_cores': limit,
                    'quota': len(quota) == 2 and quota[0] != 'max'}
    line = (_read(proc / 'stat') or '').splitlines()[:1]
    if line and line[0].startswith('cpu '):
        fields = [_int(x) or 0 for x in line[0].split()[1:]]
        busy = sum(fields[:3]) + sum(fields[5:8])  # user nice system + irq softirq steal (idle / iowait excluded)
        try:
            tick = os.sysconf('SC_CLK_TCK')
        except (ValueError, OSError, AttributeError):
            tick = 100
        return {'source': 'proc_stat', 'busy_seconds': busy / tick, 'at': time.time(), 'limit_cores': cores,
                'quota': False}
    return {'source': None, 'busy_seconds': None, 'at': time.time(), 'limit_cores': cores, 'quota': False}


def cpu(sample, previous=None):
    """The sample plus the usage rate since `previous` (cores and % of the limit) and the load average."""
    out = dict(sample)
    out['cores_used'] = out['pct'] = None
    if previous and sample.get('busy_seconds') is not None and previous.get('busy_seconds') is not None \
            and previous.get('source') == sample.get('source') and sample['at'] - previous['at'] >= 1:
        rate = max(sample['busy_seconds'] - previous['busy_seconds'], 0) / (sample['at'] - previous['at'])
        out['cores_used'] = round(rate, 2)
        out['pct'] = _pct(rate, sample.get('limit_cores'))
    try:
        out['loadavg'] = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        out['loadavg'] = None
    return out


def _parents(proc=PROC):
    """{pid: ppid} of every process: /proc on Linux, `ps` elsewhere (None when neither works)."""
    if (proc / 'self' / 'stat').exists():
        out = {}
        for entry in os.scandir(proc):
            if not entry.name.isdigit():
                continue
            text = _read(Path(entry.path) / 'stat')
            if text and ')' in text:
                fields = text.rsplit(')', 1)[1].split()
                if len(fields) > 1 and fields[1].isdigit():
                    out[int(entry.name)] = int(fields[1])
        return out
    try:
        done = subprocess.run(['ps', '-A', '-o', 'pid=,ppid='], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    out = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = int(parts[1])
    return out


def processes(supervisor_pid=None, proc=PROC, cgroup=CGROUP):
    """{total, kage_children, pids_current, pids_max}: every process, the supervisor's descendants (agent CLIs and what
    they started), and the cgroup's pids limit when it has one."""
    parents = _parents(proc)
    children = None
    if parents is not None and supervisor_pid:
        kids, found, frontier = {}, set(), {supervisor_pid}
        for pid, ppid in parents.items():
            kids.setdefault(ppid, []).append(pid)
        while frontier:
            nxt = {c for p in frontier for c in kids.get(p, ()) if c not in found}
            found |= nxt
            frontier = nxt
        children = len(found)
    d = _cgroup_dir(proc, cgroup)
    pids_current = _int(_read(d / 'pids.current')) if d is not None else None
    pids_max = _int(_read(d / 'pids.max')) if d is not None else None
    return {'total': len(parents) if parents is not None else None, 'kage_children': children,
            'pids_current': pids_current, 'pids_max': pids_max}


def probe(paths, *, supervisor_pid=None, previous_cpu=None, proc=PROC, cgroup=CGROUP):
    """The live status: {at, platform, disks, memory, cpu, processes}. `paths` = {label: path} of the filesystems to
    report (home, state)."""
    sample = cpu_sample(proc, cgroup)
    return {'at': time.time(), 'platform': sys.platform, 'disks': disks(paths), 'memory': memory(proc, cgroup),
            'cpu': cpu(sample, previous_cpu), 'cpu_sample': sample,
            'processes': processes(supervisor_pid, proc, cgroup)}


# ---------------------------------------------------------------------------
# Directory sizes (the slow part: the supervisor's background thread)
# ---------------------------------------------------------------------------

def walk_sizes(paths, budget=WALK_SECONDS, max_entries=WALK_ENTRIES, clock=time.monotonic):
    """[{label, path, bytes, files, partial}] for the existing directories in `paths` ({label: path}), biggest first.
    Allocated size (st_blocks), symlinks not followed, other filesystems still counted; `partial` when the shared
    budget ran out (the sum so far is a lower bound)."""
    deadline, entries, out = clock() + budget, 0, []
    for label, path in paths.items():
        root = Path(path).expanduser()
        if not root.is_dir() or root.is_symlink():
            continue
        size = files = 0
        partial = False
        stack = [str(root)]
        while stack:
            if entries >= max_entries or clock() >= deadline:
                partial = True
                break
            top = stack.pop()
            try:
                with os.scandir(top) as it:
                    for entry in it:
                        entries += 1
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        size += getattr(st, 'st_blocks', 0) * 512 or st.st_size
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            files += 1
            except OSError:
                continue
        out.append({'label': label, 'path': str(root), 'bytes': size, 'files': files, 'partial': partial})
    return sorted(out, key=lambda c: -c['bytes'])


def watched_paths(spec, *, home, state_dir, workspace=None):
    """{label: path} the size pass covers: agent sessions, task workspaces, the state directory, its backups, and the
    configured `system.watch_paths` (labelled by their path)."""
    home = Path(home)
    out = {'claude_sessions': home / '.claude' / 'projects', 'codex_sessions': home / '.codex'}
    if workspace:
        out['workspaces'] = Path(workspace).expanduser()
    out['state'] = Path(state_dir)
    out['backups'] = Path(state_dir) / 'backups'
    for extra in spec.get('watch_paths') or ():
        out.setdefault(str(extra), Path(str(extra).replace('~', str(home), 1)) if str(extra).startswith('~') else Path(extra))
    return out


# ---------------------------------------------------------------------------
# Levels, the stored snapshot, the read model
# ---------------------------------------------------------------------------

def values(status):
    """{metric: (pct, detail)} the thresholds apply to: the fullest reported filesystem, the memory working set."""
    out = {}
    full = max((d for d in status.get('disks') or () if d.get('pct') is not None), key=lambda d: d['pct'], default=None)
    if full:
        out['disk'] = (full['pct'], {'mount': full['mount'], 'free': full['free'], 'total': full['total']})
    mem = status.get('memory') or {}
    if mem.get('pct') is not None:
        out['memory'] = (mem['pct'], {'used': mem['used'], 'limit': mem['limit']})
    return out


def lines(spec, metric):
    return spec[f'{metric}_warn_pct'], spec[f'{metric}_crit_pct']


def level_for(value, current, warn, crit):
    """The level for `value`, staying at the current one until the value is RECOVER_MARGIN points under its line."""
    if value >= crit or (current == CRITICAL and value >= crit - RECOVER_MARGIN):
        return CRITICAL
    if value >= warn or (current in (WARNING, CRITICAL) and value >= warn - RECOVER_MARGIN):
        return WARNING
    return OK


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def stored(db):
    """(alerts {metric: row}, snapshot row or None, recent events) — empty when the supervisor never ran the step."""
    tables = _tables(db)
    alerts = {r['metric']: dict(r) for r in db.execute('SELECT * FROM system_alerts')} if 'system_alerts' in tables else {}
    snap = db.execute('SELECT * FROM system_snapshot WHERE id=1').fetchone() if 'system_snapshot' in tables else None
    events = [{'metric': r['metric'], 'kind': r['kind'], 'detail': json.loads(r['detail'] or 'null'), 'at': r['at']}
              for r in db.execute('SELECT * FROM system_events ORDER BY at DESC, id DESC LIMIT 20')] \
        if 'system_events' in tables else []
    return alerts, (dict(snap) if snap else None), events


def overview(db, config, paths, *, supervisor_pid=None, previous_cpu=None, now=None):
    """The console's read model (总控 and /health): the live probe, the stored directory sizes and alert levels, the
    thresholds, and `alert` = the worst level now (live values against the thresholds, or a stored level not yet
    recovered). Read-only; counts and sizes only."""
    now = time.time() if now is None else now
    spec = settings(config)
    status = probe(paths, supervisor_pid=supervisor_pid, previous_cpu=previous_cpu)
    alerts, snap, events = stored(db) if db is not None else ({}, None, [])
    if status['cpu']['pct'] is None and snap and snap.get('status'):  # else the rate since the supervisor's last check
        old = (json.loads(snap['status']) or {}).get('cpu_sample')
        if old:
            status['cpu'] = cpu(status['cpu_sample'], old)
    levels = {}
    for metric, (value, _) in values(status).items():
        levels[metric] = level_for(value, (alerts.get(metric) or {}).get('level', OK), *lines(spec, metric))
    worst = max(list(levels.values()) or [OK], key=RANK.get)
    running = None
    if db is not None and 'consumer_runs' in _tables(db):
        running = db.execute("SELECT count(*) FROM consumer_runs WHERE state='running'").fetchone()[0]
    status['processes']['agent_runs'] = running
    sample = status.pop('cpu_sample', None)
    return {'cpu_sample': sample, 'generated_at': now, 'enabled': spec['enabled'], 'alert': worst, 'levels': levels,
            'thresholds': {k: spec[k] for k in ('disk_warn_pct', 'disk_crit_pct', 'memory_warn_pct', 'memory_crit_pct')},
            'check_minutes': spec['check_minutes'], 'consumers_minutes': spec['consumers_minutes'], **status,
            'consumers': json.loads(snap['consumers']) if snap and snap.get('consumers') else None,
            'consumers_at': snap.get('consumers_at') if snap else None,
            'checked_at': snap.get('checked_at') if snap else None,
            'stored': {m: {k: a[k] for k in ('level', 'since', 'value', 'announced', 'notice_error')}
                       for m, a in alerts.items()}, 'events': events}


def human_bytes(n):
    if n is None:
        return '?'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(n) < 1024 or unit == 'TiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


# ---------------------------------------------------------------------------
# The monitor (a supervisor step) and its notice
# ---------------------------------------------------------------------------

class Monitor:
    """The supervisor's check and its notice, in the owner's language. `post(rt, db, metric, kind, text)` sends a
    notice and returns its id (default: the provider-health notice channel, with this step's context line)."""

    def __init__(self, language, *, post=None, walk=walk_sizes):
        self.language, self._post, self.walk = language, post, walk
        self._thread = None
        self._result = None
        self._walk_started = 0.0
        self._last_cpu = None
        self._ready = False

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def post(self, rt, db, metric, kind, text):
        if self._post is not None:
            return self._post(rt, db, metric, kind, text)
        return rt.provider_health.post_notice(rt, db, 'system', f'{metric}_{kind}', text,
                                              context_key='system.notice_context')

    def _store_walk(self, rt, db, snap):
        if self._result is None:
            return
        at, sizes = self._result
        self._result = None
        if sizes is None:
            return
        with rt.transaction(db):
            db.execute('INSERT INTO system_snapshot (id, consumers, consumers_at) VALUES (1,?,?) ON CONFLICT(id) '
                       'DO UPDATE SET consumers=excluded.consumers, consumers_at=excluded.consumers_at',
                       (json.dumps(sizes), at))
        snap.update(consumers=json.dumps(sizes), consumers_at=at)

    def _walk_in_background(self, paths):
        def run():
            try:
                self._result = (time.time(), self.walk(paths))
            except Exception:  # noqa: BLE001 - a failed pass is retried at the next due time
                self._result = (time.time(), None)
        self._thread = threading.Thread(target=run, name='kage-system-sizes', daemon=True)
        self._thread.start()

    def evaluate(self, rt, db, db_path, now=None, supervisor_pid=None, wait_walk=False):
        """One pass (self-gated by `system.enabled` / `check_minutes`). Returns the level changes and notices."""
        now = time.time() if now is None else now
        try:
            config = rt.worker_config.load()[0]
        except ValueError as exc:  # a malformed config never notices by accident
            rt._meta_error(db, 'system_status_config_error', exc)
            return []
        spec = settings(config)
        if not spec['enabled']:
            return []
        if not self._ready:
            initialize(db)
            self._ready = True
        snap = db.execute('SELECT * FROM system_snapshot WHERE id=1').fetchone()
        snap = dict(snap) if snap else {}
        state_dir = Path(db_path).expanduser().parent
        registry = getattr(rt, 'registry', None)
        try:
            workspace = registry.root() if registry is not None and callable(getattr(registry, 'root', None)) else None
        except Exception:  # noqa: BLE001 - no workspace line then
            workspace = None
        # directory sizes: a finished background pass is stored; a due one is started (never blocks the loop)
        self._store_walk(rt, db, snap)
        due = now - max(snap.get('consumers_at') or 0, self._walk_started) >= spec['consumers_minutes'] * 60
        if due and (self._thread is None or not self._thread.is_alive()):
            self._walk_started = now  # a failed pass is not retried before the next due time
            self._walk_in_background(watched_paths(spec, home=Path.home(), state_dir=state_dir, workspace=workspace))
            if wait_walk:
                self._thread.join()
                self._store_walk(rt, db, snap)
        if now - (snap.get('checked_at') or 0) < spec['check_minutes'] * 60 - 1:
            return []
        status = probe({'home': Path.home(), 'state': state_dir}, supervisor_pid=supervisor_pid, previous_cpu=self._last_cpu)
        self._last_cpu = status['cpu_sample']
        with rt.transaction(db):
            db.execute('INSERT INTO system_snapshot (id, checked_at, status) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET '
                       'checked_at=excluded.checked_at, status=excluded.status', (now, json.dumps(status)))
        sizes = json.loads(snap['consumers']) if snap.get('consumers') else []
        out = []
        current = values(status)
        for metric in METRICS:
            row = db.execute('SELECT * FROM system_alerts WHERE metric=?', (metric,)).fetchone()
            row = dict(row) if row else {'metric': metric, 'level': OK, 'since': None, 'value': None, 'announced': OK,
                                         'notice_ts': None, 'notice_error': None}
            if metric not in current:
                continue
            value, detail = current[metric]
            level = level_for(value, row['level'], *lines(spec, metric))
            event = None
            if level != row['level']:
                event = level
                row.update(level=level, since=now)
            row['value'] = value
            kind = None
            if RANK[level] > RANK[row['announced']]:
                kind = level
            elif level == OK and row['announced'] != OK:
                kind = 'recovered'
            elif RANK[level] < RANK[row['announced']]:
                row['announced'] = level  # critical -> warning: a later critical notices again
            self._save(rt, db, row, now, event, {'value': value, **detail} if event else None)
            if event:
                out.append({'metric': metric, 'event': event, 'value': value})
            if not kind:
                continue
            try:
                text = self.notice_text(metric, kind, value, detail, spec, sizes, status)
                row.update(announced=level, notice_ts=self.post(rt, db, metric, kind, text), notice_error=None)
                self._save(rt, db, row, now, 'notice_' + kind, {'value': value})
                out.append({'metric': metric, 'event': 'notice_' + kind})
            except Exception as exc:  # noqa: BLE001 - the level stands; only the notice is missing (retried next pass)
                row['notice_error'] = rt.safe_error(exc)
                self._save(rt, db, row, now)
        return out

    def _save(self, rt, db, row, now, event=None, detail=None):
        with rt.transaction(db):
            db.execute('INSERT INTO system_alerts (metric,level,since,value,announced,notice_ts,notice_error,updated_at) '
                       'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(metric) DO UPDATE SET level=excluded.level,'
                       'since=excluded.since,value=excluded.value,announced=excluded.announced,'
                       'notice_ts=excluded.notice_ts,notice_error=excluded.notice_error,updated_at=excluded.updated_at',
                       (row['metric'], row['level'], row['since'], row['value'], row['announced'], row['notice_ts'],
                        row['notice_error'], now))
            if event:
                db.execute('INSERT INTO system_events (metric,kind,detail,at) VALUES (?,?,?,?)',
                           (row['metric'], event, json.dumps(detail, sort_keys=True), now))

    def notice_text(self, metric, kind, value, detail, spec, sizes, status):
        """The copy standard: the first line is one plain sentence (what, how full, which line it crossed)."""
        warn, crit = lines(spec, metric)
        pct = f'{value:g}'
        if kind == 'recovered':
            return self.msg(f'system.{metric}_recovered', pct=pct, warn=warn, **_fmt(detail))
        text = self.msg(f'system.{metric}_{kind}', pct=pct, warn=warn, crit=crit, **_fmt(detail))
        sizes = [c for c in sizes or () if c.get('bytes')]  # an empty directory says nothing about a full disk
        if metric == 'disk' and sizes:
            top = self.msg('system.joiner').join(f"{c['path']} {human_bytes(c['bytes'])}" for c in sizes[:3])
            text += self.msg('system.disk_top', top=top)
        if metric == 'memory':
            runs = (status.get('processes') or {}).get('kage_children')
            if runs is not None:
                text += self.msg('system.memory_children', n=runs)
        return text


def _fmt(detail):
    return {k: (human_bytes(v) if k in ('free', 'total', 'used', 'limit') else v) for k, v in (detail or {}).items()}
