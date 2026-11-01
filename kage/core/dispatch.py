"""Dispatch (K12a core, K12b engine): the stateless half — failure classes, questions, card blocks, labels,
run ids, file helpers, the probe state and overviews — and the dispatcher: per-task leases and liveness,
routing and revival, the provider-switch plan, waiting for a provider to recover, keep-warm, the stranded
sweep, start / launch / tend / finalize, the breaker and the owner's run / failure / breaker cards. Process
control and the agent CLIs are injected; the provider adapters and the runner are the deployment's.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from kage import locale

HELD = 'dispatched'


ACTIVE = ('waiting', 'running')


UNHANDLED_FOR_REVIVAL = ('pending', 'queued', 'delivery_unknown', 'claimed', 'sending')


MAX_BATCH = 30


STRIP_ENV_PREFIX = ('CLAUDE_CODE_',)


STOP_GRACE_SECONDS = 15


PROGRESS_INTERVAL_SECONDS = 60
CONTROL_QUIET = ('running', 'success', 'retrying', 'fallback', 'resumed', 'handed_back')  # Phase 2 C1


PROGRESS_BACKOFF_SECONDS = (60, 900)  # 429: Retry-After or 60s doubling up to 15 min


RESERVATION_STALE_SECONDS = 120


DEGRADED = 'degraded'


STOP_DEGRADED = 3  # consumer_runs.stop_requested: 1 = the owner's stop, 2 = max_run_seconds, 3 = P38 health move


CAP_HANDOFF_FRACTION = 0.75  # a resume past this share of the context cap is told to leave status.md as the handoff


REPOSTING = 'card_reposting:'   # P38 (F49): a refused breaker / failure card claimed for its re-post (+ claim time)


REPOST_CLAIM_SECONDS = 600     # a claim older than this (the supervisor died before posting) is taken again


STRANDED_STATUSES = ('queued', 'delivery_unknown', 'claimed', 'sending')  # = the events_undelivered partial index


STRANDED_CONFIRM_SECONDS = 60  # a delivery's session must be seen dead on two sweeps at least this far apart


LIVE_PROOF_SECONDS = 1800  # after the CLI reported a session live (session_live), the stranded sweep leaves it alone


RUN_CONTEXT_FILE = 'run-context.md'  # P40.1: the Run context of a constant-first-message run (env rt.RUN_CONTEXT_ENV)


CACHE_TTL_SECONDS = {'1h': 3600, '5m': 300, 'default': 300}  # P40.1: Claude Code's default with a key / gateway is 5m


KEEP_WARM_RETRY_SECONDS = 300   # a failed refresh is tried again after this, while the prefix is still inside its TTL


KEEP_WARM_MAX_SECONDS = 180     # a refresh still running after this is killed (one API call takes seconds)


KEEP_WARM_KEEP_DAYS = 7         # keep-warm run dirs / rows older than this are pruned


KEEP_WARM_ARGS = ('--max-turns', '1', '--no-session-persistence')  # client-side only: the request is unchanged


KEEP_WARM_ID_RE = re.compile(r'k\d{8}T\d{6}Z-[0-9a-f]{6}')


SESSION_ID_CHARS = 13  # UUIDv7 ids start with a timestamp: sessions started in the same second share 8 characters


PROVIDER_WAIT = ('transient', 'rate_limited', 'quota')  # P44: provider-side classes a wait answers, not a card


WAIT_REASON = 'provider_wait'   # dispatches.wait_reason while a dispatch waits for a probe to see a provider back


PROBE_META = 'provider_probe'   # runtime_meta: {provider: {at, result, error_class, marker, fails, next_at, model, running}}


PROBE_TIMEOUT_SECONDS = 60      # a probe still running after this is killed and counts as down (transient)


PROBE_PROMPT = 'Reply with the single word OK.'


PROBE_CLAUDE_ARGS = (*KEEP_WARM_ARGS, '--tools', '', '--strict-mcp-config')  # one turn, no tool, no MCP, no transcript


PROBE_CODEX_ARGS = ('--ephemeral', '--sandbox', 'read-only')                 # no session file, nothing writable


DDL = '''
  CREATE TABLE IF NOT EXISTS keep_warm_runs (
    kw_id TEXT PRIMARY KEY, task TEXT NOT NULL, task_path TEXT, model TEXT, effort TEXT, prompt_sha TEXT,
    state TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, exit_code INTEGER, outcome TEXT, log_dir TEXT,
    pid INTEGER, pid_start TEXT);
  CREATE INDEX IF NOT EXISTS keep_warm_task ON keep_warm_runs(task, started_at);
  CREATE TABLE IF NOT EXISTS consumer_leases (
    task_key TEXT PRIMARY KEY, holder_kind TEXT NOT NULL, provider TEXT, session TEXT,
    pid INTEGER, pid_start TEXT, run_id TEXT, acquired_at REAL NOT NULL, heartbeat_at REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS consumer_runs (
    run_id TEXT PRIMARY KEY, dispatch_id TEXT, task TEXT NOT NULL, task_path TEXT, provider TEXT NOT NULL,
    session TEXT, model TEXT, effort TEXT, mode TEXT NOT NULL, trigger TEXT NOT NULL, event_ids TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL, started_at REAL, ended_at REAL, exit_code INTEGER,
    outcome TEXT, error_class TEXT, error_marker TEXT, log_dir TEXT, pid INTEGER, pid_start TEXT,
    stop_requested INTEGER NOT NULL DEFAULT 0, stop_signal_at REAL, handled INTEGER, unresolved INTEGER,
    health_markers INTEGER, route_reason TEXT);
  CREATE INDEX IF NOT EXISTS consumer_runs_state ON consumer_runs(state, started_at);
  CREATE INDEX IF NOT EXISTS consumer_runs_task ON consumer_runs(task, started_at);
  CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id TEXT PRIMARY KEY, task_key TEXT NOT NULL, task_id TEXT, task_path TEXT NOT NULL,
    state TEXT NOT NULL, trigger TEXT NOT NULL, reason TEXT, plan TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0, partial_retries INTEGER NOT NULL DEFAULT 0, next_at REAL,
    run_id TEXT, prompt TEXT, important INTEGER NOT NULL DEFAULT 0, outcome TEXT, error_class TEXT,
    failures TEXT NOT NULL DEFAULT '[]', card_channel TEXT, card_ts TEXT, card_stage TEXT,
    fail_card_ts TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, ended_at REAL,
    wait_reason TEXT, wait_since REAL);
  CREATE INDEX IF NOT EXISTS dispatches_task_state ON dispatches(task_key, state);
  CREATE INDEX IF NOT EXISTS dispatches_state ON dispatches(state, next_at);
  CREATE TABLE IF NOT EXISTS dispatch_pauses (
    task_key TEXT PRIMARY KEY, paused_at REAL NOT NULL, paused_by TEXT NOT NULL, reason TEXT,
    dispatch_id TEXT, resumed_at REAL, resumed_by TEXT);
  CREATE TABLE IF NOT EXISTS dispatch_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, watch_id TEXT, task_key TEXT, decision TEXT NOT NULL,
    reason TEXT NOT NULL, from_provider TEXT, from_session TEXT, to_provider TEXT, to_session TEXT,
    liveness TEXT, dispatch_id TEXT, at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS dispatch_routes_event ON dispatch_routes(event_id);
  CREATE TABLE IF NOT EXISTS dispatch_meta (
    task_key TEXT PRIMARY KEY, important INTEGER NOT NULL DEFAULT 0, category TEXT, updated_at REAL NOT NULL);
  -- P39: the task's launch preference, written only by an explicit launch (provider = an explicit pin, NULL = follow the
  -- intake role / routing category). A NEW table, not a dispatch_meta column: pre-P39 code writes dispatch_meta with a
  -- positional 4-value INSERT, which a 5th column would break on a rollback.
  CREATE TABLE IF NOT EXISTS dispatch_prefs (
    task_key TEXT PRIMARY KEY, provider TEXT, important INTEGER NOT NULL DEFAULT 0, category TEXT, source TEXT,
    updated_at REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS local_messages (
    event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, task_id TEXT, path TEXT NOT NULL, sender TEXT NOT NULL,
    created_at REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS agent_messages (
    id TEXT PRIMARY KEY, from_key TEXT NOT NULL, to_key TEXT NOT NULL, sender TEXT NOT NULL, event_id TEXT,
    path TEXT, hops INTEGER NOT NULL DEFAULT 0, breaker_id TEXT, created_at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS agent_messages_pair ON agent_messages(from_key, to_key, created_at);
  CREATE TABLE IF NOT EXISTS agent_breakers (
    id TEXT PRIMARY KEY, key_a TEXT NOT NULL, key_b TEXT NOT NULL, state TEXT NOT NULL, round_trips INTEGER,
    tripped_at REAL NOT NULL, decided_by TEXT, decided_at REAL, reset_at REAL, card_channel TEXT, card_ts TEXT,
    card_stage TEXT);
  CREATE INDEX IF NOT EXISTS agent_breakers_pair ON agent_breakers(key_a, key_b, state);
  CREATE INDEX IF NOT EXISTS events_held_receipt ON events(receipt) WHERE status='dispatched';
  -- P1b: run-card progress throttle (no bodies: a digest of the masked summary only) and button log.
  CREATE TABLE IF NOT EXISTS run_progress (
    dispatch_id TEXT PRIMARY KEY, run_id TEXT, posted_at REAL, digest TEXT, step INTEGER,
    backoff_until REAL, failures INTEGER NOT NULL DEFAULT 0, stage TEXT, updated_at REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS dispatch_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL, run_id TEXT, action TEXT NOT NULL,
    actor TEXT NOT NULL, result TEXT, at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS dispatch_actions_dispatch ON dispatch_actions(dispatch_id, at);
  -- P38 C1: deliveries whose consumer session died before consuming them (suspect -> held / rerouted / unowned).
  CREATE TABLE IF NOT EXISTS stranded_deliveries (
    event_id TEXT PRIMARY KEY, watch_id TEXT NOT NULL, provider TEXT NOT NULL, session TEXT NOT NULL,
    status TEXT NOT NULL, task_key TEXT, state TEXT NOT NULL, reason TEXT, dispatch_id TEXT, first_seen REAL NOT NULL,
    updated_at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS stranded_deliveries_state ON stranded_deliveries(state, updated_at);
  CREATE INDEX IF NOT EXISTS events_undelivered ON events(status, received)
    WHERE status IN ('queued','delivery_unknown','claimed','sending');
'''


def initialize(db):
    db.executescript(DDL)


FAILURE_PATTERNS = (
    ('session_live', r'already has an active writer|session is already (?:running|in use)|is already running'),
    ('session_missing', r'no conversation found|no rollout found|thread[^\n]{0,40}not found|session[^\n]{0,20}not '
                        r'found|no saved session|unknown (?:thread|session)'),
    ('model_unsupported', r'not supported by any configured account'),  # the relay's exact 404 wording
    ('model_unsupported', r'model[^\n]{0,80}(?:does not exist|not found|is not supported|not available)|'
                          r'issue with the selected model|\b404\b[^\n]{0,60}model'),
    ('auth', r'\b40[13]\b|unauthori[sz]ed|forbidden|invalid[ _-]?api[ _-]?key|authentication[_ ](?:error|failed)|'
             r'please run /login|not logged in|oauth token|permission_error|token (?:has )?expired'),
    ('quota', r'usage limit|insufficient[_ ]quota|exceeded your current quota|quota|limit reached|'
              r"you've hit your|hit your (?:session|usage|weekly|daily) limit|credit balance|billing[_ ]error|"
              r'spend(?:ing)? limit|daily limit'),
    ('rate_limited', r'\b429\b|too many requests|rate[_ ]?limit'),
    ('transient', r'\b(?:500|502|503|504|529)\b|overloaded|service unavailable|bad gateway|gateway timeout|'
                  r'timed? ?out|timeout|econnreset|etimedout|enotfound|econnrefused|connection (?:reset|refused|'
                  r'closed|error)|network|stream (?:disconnected|error)|socket hang up|internal[_ ]server[_ ]error|'
                  r'api_error|temporarily unavailable'),
)


FAILURE_RES = tuple((name, re.compile(pattern, re.I)) for name, pattern in FAILURE_PATTERNS)


MCP_NOISE_RE = re.compile(r'rmcp::|\bmcp[_ -]?(?:client|server|oauth)\b|AuthRequired', re.I)


RELAY_STATUS_RE = re.compile(r'unexpected status (\d{3})', re.I)


RETRY_SAME = ('transient', 'rate_limited', 'unknown', 'crashed')


SWITCH_NOW = ('quota', 'session_missing', 'provider_degraded')


NO_SWITCH = ('auth', 'model_unsupported', 'spawn_failed', 'run_timeout')


CLAUDE_RETRY_CATEGORY = {'rate_limit': 'rate_limited', 'overloaded': 'transient', 'authentication_failed': 'auth',
                         'billing_error': 'quota'}


def classify_failure(text):
    """(error_class, marker). `marker` is the short matched token only, never the message. P46.3: lines from an MCP
    server are dropped first, and the relay's last status being a 5xx wins over `auth`."""
    text = '\n'.join(line for line in str(text or '').splitlines() if not MCP_NOISE_RE.search(line))
    relay_5xx = (RELAY_STATUS_RE.findall(text) or ['-'])[-1].startswith('5')
    for name, regex in FAILURE_RES:
        match = None if name == 'auth' and relay_5xx else regex.search(text)
        if match:
            return name, re.sub(r'\s+', ' ', match.group(0))[:40].lower()
    return 'unknown', None


def run_failure(info, exit_info):
    """(error_class, marker) of a CLI run that did not end cleanly (`parse_run_log` info): the patterns over its errors,
    else Claude's api_retry category, else `crashed` when it left no exit.json. Shared by `finalize` and the P44 probe."""
    error_class, marker = classify_failure('\n'.join(info['errors']))
    for category in info['retry_categories']:
        if error_class == 'unknown' and category in CLAUDE_RETRY_CATEGORY:
            error_class, marker = CLAUDE_RETRY_CATEGORY[category], category
    if exit_info is None and error_class == 'unknown':
        error_class = 'crashed'
    return error_class, marker


def _read_tail(path, limit=262144):
    try:
        with open(path, 'rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - limit))
            return stream.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def _read_head(path, limit=65536):
    try:
        with open(path, 'rb') as stream:
            return stream.read(limit).decode('utf-8', 'replace')
    except OSError:
        return ''


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec='seconds') if ts else None


def new_run_id(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('r%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:6]


PROVIDER_NAME = {'codex': 'Codex', 'claude': 'Claude'}


def _button(text, action, value, style=None):
    button = {'type': 'button', 'text': {'type': 'plain_text', 'text': text, 'emoji': True},
              'action_id': 'sf_dispatch:' + action, 'value': value}
    if style:
        button['style'] = style
    return button


def pair_key(a, b):
    return tuple(sorted((a, b)))


def _mrkdwn(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('`', "'")


def _plan_providers(plan):
    """P44: the providers a dispatch can run on (its plan's, in order) = the providers it waits for."""
    return list(dict.fromkeys(step['provider'] for step in json.loads(plan or '[]')))


def probe_state(db):
    """P44: the providers' probe state (runtime_meta PROBE_META): well-formed provider entries only, so a damaged
    value never freezes the waits."""
    row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (PROBE_META,)).fetchone()
    try:
        state = json.loads(row[0]) if row else {}
    except ValueError:
        return {}
    return {p: s for p, s in state.items() if p in PROVIDER_NAME and isinstance(s, dict) and
            isinstance(s.get('next_at') or 0, (int, float)) and isinstance(s.get('fails', 0), int)
            } if isinstance(state, dict) else {}


def progress_backoff(exc, failures):
    """(seconds, stage) after a failed chat.update; honours Retry-After on HTTP 429 / `ratelimited`."""
    low, high = PROGRESS_BACKOFF_SECONDS
    exponential = min(high, low * 2 ** min(int(failures), 4))
    if getattr(exc, 'code', None) == 429 or 'ratelimited' in str(exc):
        try:
            retry_after = float((getattr(exc, 'headers', None) or {}).get('Retry-After') or 0)
        except (TypeError, ValueError, AttributeError):
            retry_after = 0
        return min(high, max(exponential, retry_after)), 'rate_limited'
    return min(high, max(120, exponential)), 'failed'


class Dispatcher:
    def __init__(self, db, db_path, rt, *, probe=None, spawn=None, pid_start=None, killer=None, clock=time.time,
                 python=None, script=None, core):
        self.core = core
        self.db, self.db_path, self.rt = db, str(db_path), rt
        self.clock = clock
        self.probe = probe or rt.liveness.Probe(ttl=self._cfg_value('liveness_cache_seconds', 20))
        self.pid_start = pid_start or self.probe.ps_start
        self.killer = killer or os.killpg
        self.spawn = spawn or self._spawn
        self.python = python or sys.executable
        self.script = str(script or Path(rt.__file__).resolve())
        self.children = {}
        self.runs_root = Path(self.db_path).expanduser().resolve().parent / 'runs'

    # ---- config ----
    def config(self):
        config, _ = self.rt.worker_config.load()
        return config

    def _control_task(self, d):
        """True for a dispatch of the control task while the control entry is on."""
        try:
            return self.rt.worker_config.intake_role(self.config(), d['task_id'] or d['task_key']) == 'control'
        except Exception:  # noqa: BLE001 - a malformed config: the cards as before
            return False

    def _cfg_value(self, key, default):
        try:
            return self.config()['dispatch'][key]
        except Exception:  # noqa: BLE001 - a malformed config never breaks delivery
            return default

    # ---- small SQL helpers ----
    def _dispatch(self, dispatch_id):
        row = self.db.execute('SELECT * FROM dispatches WHERE dispatch_id=?', (dispatch_id,)).fetchone()
        return dict(row) if row else None

    def _run(self, run_id):
        row = self.db.execute('SELECT * FROM consumer_runs WHERE run_id=?', (run_id,)).fetchone()
        return dict(row) if row else None

    def _held_events(self, dispatch_id):
        return [r[0] for r in self.db.execute("SELECT id FROM events WHERE status='dispatched' AND receipt=? "
                                              'ORDER BY received,id', ('dispatch:' + dispatch_id,))]

    def _task_dispatch(self, task_key, states):
        row = self.db.execute('SELECT * FROM dispatches WHERE task_key=? AND state IN (%s) ORDER BY created_at DESC '
                              'LIMIT 1' % ','.join('?' * len(states)), (task_key, *states)).fetchone()
        return dict(row) if row else None

    def _route_log(self, decision, reason, w=None, event_ids=(), task_key=None, to=None, live=None, dispatch_id=None):
        now = self.clock()
        rows = list(event_ids) or [None]
        with self.rt.transaction(self.db):
            for eid in rows:
                self.db.execute('INSERT INTO dispatch_routes (event_id,watch_id,task_key,decision,reason,from_provider,'
                                'from_session,to_provider,to_session,liveness,dispatch_id,at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                (eid, w and w['id'], task_key, decision, reason, w and self.rt.claude.provider(w),
                                 w and w['session'], to and to[0], to and to[1],
                                 json.dumps(live, sort_keys=True, default=str) if live else None, dispatch_id, now))

    def paused(self, task_key):
        row = self.db.execute('SELECT * FROM dispatch_pauses WHERE task_key=? AND resumed_at IS NULL',
                              (task_key,)).fetchone()
        return dict(row) if row else None

    def alive(self, pid, pid_start):
        """Own children: Popen.poll (also reaps zombies). Others: pid alive with the same start time."""
        if not pid:
            return False
        child = self.children.get(pid)
        if child is not None:
            return child.poll() is None
        if not pid_start:
            return False
        return self.pid_start(pid) == pid_start

    # ---- task identity ----
    def task_for_watch(self, w, config=None):
        """{'key','task_id','path','current'} for a watch; None when routing must stay per-session/legacy."""
        config = config or self.config()
        provider = self.rt.claude.provider(w)
        tid = self.rt.registry.task_id_for_cwd(w.get('cwd'))
        if tid:
            row = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (tid,)).fetchone()
            if row is None:
                with self.rt.transaction(self.db):
                    self.rt.registry.ensure_task(self.db, tid, self.clock())
                row = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (tid,)).fetchone()
            current = (row['current_provider'], row['current_session']) if row['current_session'] else None
            return {'key': tid, 'task_id': tid, 'path': row['path'], 'title': row['title'], 'current': current}
        if config['dispatch']['unbound_watches'] != 'revive':
            return None
        return {'key': f'session:{provider}:{w["session"]}', 'task_id': None, 'path': w['cwd'], 'title': None,
                'current': (provider, w['session'])}

    def task_for_key(self, task_key):
        if task_key.startswith('session:'):
            _, provider, session = task_key.split(':', 2)
            row = self.db.execute("SELECT data FROM watches WHERE json_extract(data,'$.session')=? AND "
                                  "coalesce(json_extract(data,'$.provider'),'codex')=? LIMIT 1", (session, provider)).fetchone()
            path = json.loads(row[0])['cwd'] if row else None
            return {'key': task_key, 'task_id': None, 'path': path, 'title': None, 'current': (provider, session)}
        row = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_key,)).fetchone()
        if row is None:
            return None
        return {'key': task_key, 'task_id': task_key, 'path': row['path'], 'title': row['title'],
                'current': (row['current_provider'], row['current_session']) if row['current_session'] else None}

    def task_owners(self, task, exclude=()):
        """Sessions owning running non-smoke watches of the task, most recently successful first."""
        if not task['task_id']:
            return []
        watches = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM watches')]
        mine = [w for w in watches if self.rt.registry.task_id_for_cwd(w.get('cwd')) == task['task_id']
                and not w.get('smoke') and w.get('state') == 'running']
        success = self.rt.registry.session_success(self.db, mine)
        return [k for k, _ in sorted(success.items(), key=lambda kv: -kv[1]['at']) if k not in set(exclude)]

    # ---- leases ----
    def lease(self, task_key):
        row = self.db.execute('SELECT * FROM consumer_leases WHERE task_key=?', (task_key,)).fetchone()
        return dict(row) if row else None

    def lease_valid(self, lease):
        if lease is None:
            return False
        if lease['holder_kind'] == 'headless':
            if lease['pid'] is None:  # reserved, spawn in progress
                return self.clock() - lease['heartbeat_at'] < RESERVATION_STALE_SECONDS
            return self.alive(lease['pid'], lease['pid_start'])
        state = self.probe.check(lease['provider'], lease['session'])['state']
        return state in self.rt.liveness.LIVE

    def note_interactive(self, task, provider, session, live):
        """Record/refresh the interactive consumer of a task; never displaces a live headless lease."""
        now = self.clock()
        pid = live.get('pid') if isinstance(live.get('pid'), int) else None
        with self.rt.transaction(self.db):
            row = self.lease(task['key'])
            if row and row['holder_kind'] == 'headless' and (row['pid'] is None or self.alive(row['pid'], row['pid_start'])):
                return False
            if row and row['holder_kind'] == 'interactive' and (row['provider'], row['session']) == (provider, session):
                self.db.execute('UPDATE consumer_leases SET heartbeat_at=?,pid=?,pid_start=? WHERE task_key=?',
                                (now, pid, row['pid_start'] if row['pid'] == pid else None, task['key']))
                return True
            self.db.execute('INSERT OR REPLACE INTO consumer_leases VALUES (?,?,?,?,?,?,?,?,?)',
                            (task['key'], 'interactive', provider, session, pid, None, None, now, now))
        return True

    def task_consumer(self, task_id):
        """P38 G1: the task's consumer (registry.task_consumer) with the interactive lease proven by this probe."""
        return self.rt.registry.task_consumer(self.db, task_id, lease_live=self.lease_valid)

    def release_lease(self, task_key, run_id=None):
        if run_id:
            self.db.execute('DELETE FROM consumer_leases WHERE task_key=? AND run_id=?', (task_key, run_id))
        else:
            self.db.execute('DELETE FROM consumer_leases WHERE task_key=?', (task_key,))

    # ---- watch rebinding ----
    def rebind_watch(self, wid, provider, session, reason):
        """Inside no transaction. Move a dead owner's watch (and its unhandled events) to `session`."""
        rt = self.rt
        now = self.clock()
        with rt.transaction(self.db):
            w = rt.load(self.db, wid)
            if (rt.claude.provider(w), w['session']) == (provider, session):
                return w
            old = (rt.claude.provider(w), w['session'])
            twin = self.db.execute(
                "SELECT data FROM watches WHERE id!=? AND coalesce(json_extract(data,'$.provider'),'codex')=? AND "
                "json_extract(data,'$.session')=? AND json_extract(data,'$.channel')=? AND json_extract(data,'$.thread') IS ? "
                "AND coalesce(json_extract(data,'$.scope'),'thread')=? AND coalesce(json_extract(data,'$.kind'),'peer')=?",
                (wid, provider, session, w['channel'], w['thread'], w.get('scope', 'thread'), w.get('kind', 'peer'))).fetchone()
            # P38: an EXPIRED watch only reaches here for what landed after it expired (a decision on its card): its
            # older backlog stays exactly as it was, never re-sent to the new owner.
            floor = float(w.get('expired_at') or 0) if w.get('state') == 'expired' else -1.0
            claimed = [r[0] for r in self.db.execute("SELECT id FROM events WHERE watch_id=? AND status='claimed' AND "
                                                     'received>?', (wid, floor))]
            rt.claude.settle_claims(self.db, claimed, 'owner_dead_rerouted', now)
            self.db.execute("UPDATE events SET status='pending',receipt=NULL WHERE watch_id=? AND status IN "
                            "('queued','delivery_unknown','claimed','sending') AND received>?", (wid, floor))
            if twin:
                # The new owner already follows this exact source: merge instead of duplicating the watch.
                target = json.loads(twin[0])
                if target.get('stop_reason') == rt.INBOX_RETIRED:
                    # P38: a long-lived task's retired inbox watch takes events again (a stopped watch never delivers).
                    target.update(state='inbox', reopened_at=now)
                    target.pop('stop_reason', None)
                    rt.save(self.db, target)
                for e in self.db.execute("SELECT id,channel,ts,digest FROM events WHERE watch_id=? AND status!='handled' "
                                         'AND received>?', (wid, floor)).fetchall():
                    dup = self.db.execute('SELECT 1 FROM events WHERE watch_id=? AND channel=? AND ts=? AND digest=?',
                                          (target['id'], e['channel'], e['ts'], e['digest'])).fetchone()
                    if dup:
                        self.db.execute("UPDATE events SET status='handled',disposition='superseded' WHERE id=?", (e['id'],))
                    else:
                        self.db.execute('UPDATE events SET watch_id=? WHERE id=?', (target['id'], e['id']))
                # Moved pending events still deliver: due_watches covers every non-stopped watch with pending events.
                w.update(state='stopped', stop_reason='merged_into_task_session', stopped_at=now,
                         merged_into=target['id'])
                rt.save(self.db, w)
                result = target
            else:
                history = list(w.get('owner_history') or [])[-9:]
                history.append({'provider': old[0], 'session': old[1], 'until': now, 'reason': reason})
                w.update(session=session, provider=provider, owner_pid=None, owner_birth=None, owner_history=history,
                         routed_by='dispatcher', routed_at=now, route_reason=reason)
                w.pop('recovery_events', None)
                if (w.get('rebind_waiting') or {}).get('task_id') in (None, w.get('task_id')):
                    # P38: an approvals rebind that waited on this owner is moot now — but a waiting launch-from-card
                    # handoff to ANOTHER task still stands (the reconcile completes it once nothing is in flight).
                    w.pop('rebind_waiting', None)
                rt.save(self.db, w)
                result = w
            if provider == 'claude':
                rt.claude.bump(self.db, session)
        return result

    # ---- route (delivery time) ----
    def route(self, w, now=None):
        rt = self.rt
        now = self.clock() if now is None else now
        pending = [r[0] for r in self.db.execute("SELECT id FROM events WHERE watch_id=? AND status='pending' "
                                                 'ORDER BY received,id', (w['id'],))]
        if not pending or w['state'] == 'stopped':
            return {'action': 'as_today', 'reason': 'no_pending'}
        try:
            config = self.config()
        except ValueError as exc:
            rt._meta_error(self.db, 'dispatch_config_error', exc)
            return {'action': 'as_today', 'reason': 'config_invalid'}
        if not config['dispatch']['enabled']:
            return {'action': 'as_today', 'reason': 'dispatch_disabled'}
        if w.get('smoke') and not w.get('smoke_dispatch'):
            return {'action': 'as_today', 'reason': 'smoke_watch'}
        # Never act on a closed watch or on old backlog: a revived session would answer weeks-old
        # messages with stale context. Such events stay for the owner to review (dispatch-status lists them).
        # P38: except what landed AFTER the watch expired — an expired watch polls nothing, so that is the owner's decision /
        # an ask answer queued by code on the card now; under fresh-per-event tasks its owner is a finished run.
        if w['state'] == 'expired':
            pending = [r[0] for r in self.db.execute(
                "SELECT id FROM events WHERE watch_id=? AND status='pending' AND received>? ORDER BY received,id",
                (w['id'], float(w.get('expired_at') or now)))]
            if not pending:
                return {'action': 'as_today', 'reason': 'watch_not_running'}
        owed_since = now - config['dispatch']['max_event_age_hours'] * 3600
        fresh = [r[0] for r in self.db.execute(
            "SELECT id FROM events WHERE watch_id=? AND status='pending' AND received>=? ORDER BY received,id",
            (w['id'], owed_since)) if r[0] in set(pending)]
        if not fresh:
            return {'action': 'as_today', 'reason': 'stale_backlog'}
        pending = fresh
        task = self.task_for_watch(w, config)
        if task is None:
            return {'action': 'as_today', 'reason': 'unbound_legacy'}
        pause = self.paused(task['key'])
        if (pause or {}).get('reason') == self.core.takeover_reason and not (
                w.get('kind') == 'decision' and float(w.get('started') or 0) > pause['paused_at']):
            # P43: the owner took the task over in his terminal: everything waits for his handback, even while the session is
            # live there (after his ⏹ a live session still gets its events, as before) — except the answer to a card
            # posted since then, i.e. by the session he drives.
            d = self._hold(task, pending, w, 'task_paused', now, state='paused')
            return {'action': 'held', 'reason': 'task_paused', 'dispatch_id': d}
        provider, session = rt.claude.provider(w), w['session']
        running = self._task_dispatch(task['key'], ('running',))
        if float(w.get('dispatch_live_until') or 0) > now:
            live = {'state': rt.liveness.LIVE_BUSY, 'source': 'recent_active_writer'}
        else:
            live = self.probe.check(provider, session)
        run = self._run(running['run_id']) if running and running['run_id'] else None
        own_run = run is not None and (run['provider'], run['session']) == (provider, session)
        if live['state'] in rt.liveness.LIVE and not own_run:
            # Today's delivery, unchanged. (the owner's own interactive session is never blocked.)
            self.note_interactive(task, provider, session, live)
            self._route_log('as_today', 'watch_session_live', w, pending, task['key'], (provider, session), live)
            return {'action': 'as_today', 'reason': 'watch_session_live'}
        if running is not None:
            d = self._hold(task, pending, w, 'task_run_active', now, follow_up_of=running,
                           include_owed=live['state'] == rt.liveness.DEAD, owed_since=owed_since)
            return {'action': 'held', 'reason': 'task_run_active', 'dispatch_id': d}
        if live['state'] == rt.liveness.UNKNOWN:
            self._route_log('as_today', 'watch_liveness_unknown', w, pending, task['key'], (provider, session), live)
            return {'action': 'as_today', 'reason': 'watch_liveness_unknown'}
        return self._owner_dead(task, w, pending, live, config, now, owed_since)

    def _owner_dead(self, task, w, pending, live, config, now, owed_since, revive='revive'):
        """The watch's session is dead: hold for the task's paused / waiting / failed dispatch, rebind to a live session
        of the task, or revive the task headless (one dispatch per task; the lease and the global cap apply at start).
        Shared by `route` (pending events) and P38 `sweep_stranded` (deliveries a dead session never consumed)."""
        rt = self.rt
        provider, session = rt.claude.provider(w), w['session']
        pause = self.paused(task['key'])
        if pause:
            d = self._hold(task, pending, w, 'task_paused', now, state='paused', include_owed=True, owed_since=owed_since)
            return {'action': 'held', 'reason': 'task_paused', 'dispatch_id': d}
        waiting = self._task_dispatch(task['key'], ('waiting',))
        if waiting:
            d = self._hold(task, pending, w, 'dispatch_waiting', now, into=waiting, include_owed=True, owed_since=owed_since)
            return {'action': 'held', 'reason': 'dispatch_waiting', 'dispatch_id': d}
        failed = self._task_dispatch(task['key'], ('failed',))
        if failed and not self._auto_reattempt(failed, config, now):
            d = self._hold(task, pending, w, f'task_failed_waiting_for_{self.core.key}', now, into=failed, include_owed=True, owed_since=owed_since)
            return {'action': 'held', 'reason': f'task_failed_waiting_for_{self.core.key}', 'dispatch_id': d}
        # P38 G1: the same consumer the approvals reconcile binds to (a live interactive lease before current_session),
        # so this reroute and the reconcile never pull one watch back and forth.
        consumer = self.task_consumer(task['task_id']) if task['task_id'] else None
        current = consumer[:2] if consumer else task['current'] or (provider, session)
        if current != (provider, session):
            cur_live = self.probe.check(*current)
            if cur_live['state'] in rt.liveness.LIVE:
                self.rebind_watch(w['id'], current[0], current[1], 'rerouted_to_live_current')
                self.note_interactive(task, current[0], current[1], cur_live)
                self._route_log('rerouted', 'rerouted_to_live_current', w, pending, task['key'], current, cur_live)
                return {'action': 'rerouted', 'reason': 'rerouted_to_live_current', 'to': current}
            if cur_live['state'] == rt.liveness.UNKNOWN:
                self._route_log('as_today', 'current_liveness_unknown', w, pending, task['key'], current, cur_live)
                return {'action': 'as_today', 'reason': 'current_liveness_unknown'}
        for candidate in self.task_owners(task, exclude=(current, (provider, session))):
            cand_live = self.probe.check(*candidate)
            if cand_live['state'] in rt.liveness.LIVE:
                self.rebind_watch(w['id'], candidate[0], candidate[1], 'rerouted_to_live_task_session')
                self.note_interactive(task, candidate[0], candidate[1], cand_live)
                self._route_log('rerouted', 'rerouted_to_live_task_session', w, pending, task['key'], candidate, cand_live)
                return {'action': 'rerouted', 'reason': 'rerouted_to_live_task_session', 'to': candidate}
        plan = self.plan_for(task, current, config)
        d = self._hold(task, pending, w, revive, now, plan=plan, live=live, merge_held=True, include_owed=True, owed_since=owed_since)
        return {'action': 'held', 'reason': revive, 'dispatch_id': d}

    def _auto_reattempt(self, failed, config, now):
        infra = failed['error_class'] in RETRY_SAME + SWITCH_NOW
        return infra and now - (failed['ended_at'] or failed['updated_at']) >= config['dispatch']['failed_cooldown_seconds']

    def plan_for(self, task, current, config, only=None):
        """Provider steps: resume the current session, then FRESH sessions of the fallback providers.

        P38: a long-lived task, or a session past the context cap, starts FRESH instead of resuming (`why` long_lived /
        context_cap; a resume near the cap carries cap_handoff). With provider health on, a fresh start avoids a
        degraded provider (degraded_reroute), fallbacks try healthy providers first, and a task displaced from its
        preferred provider by a fallback / reroute returns to it once it is healthy (auto_return: a fresh session on
        the preferred provider, the displaced session kept as the next step). `only` (the owner's retry on one provider) is
        honored exactly: no fallbacks, no health routing."""
        wc = self.rt.worker_config
        session = current[1] if current else None
        rotate = handoff = None
        if session and config['dispatch']['long_lived_fresh'] and wc.long_lived_task(config, task['task_id']):
            rotate = 'long_lived'
        elif session:
            size = self._session_bytes(current, config)
            cap = config['dispatch']['max_session_context_mb'] * 1024 * 1024
            if size is not None and size >= cap:
                rotate = 'context_cap'
            elif size is not None and size >= cap * CAP_HANDOFF_FRACTION:
                handoff = 'cap_handoff'
        if only is not None:
            if session and current[0] == only and not rotate:
                return [self._step(only, 'resume', session, handoff)]
            return [self._step(only, 'fresh', None, rotate if session and current[0] == only else None)]
        health = self._health(config)
        preferred = self._preferred(task, config)  # P39: the pinned launch provider first
        displaced = (health is not None and session is not None and current[0] != preferred and
                     health.get(preferred) != DEGRADED and self._displaced(task, current, config))
        if session and not rotate:
            steps = [self._step(current[0], 'resume', session, handoff)]
            if displaced:
                steps.insert(0, self._step(preferred, 'fresh', None, 'auto_return'))
        else:
            start, why = preferred, rotate
            if rotate == 'context_cap' and not displaced:
                start = current[0]  # a new session on the same provider
            elif rotate and displaced:
                why = 'auto_return'
            if (health or {}).get(start) == DEGRADED:
                alt = next((p for p in [preferred] + wc.fallback_after(config, preferred) if health.get(p) != DEGRADED),
                           None)
                if alt is not None and alt != start:
                    start, why = alt, 'degraded_reroute'
            steps = [self._step(start, 'fresh', None, why)]
        for provider in self._by_health(wc.fallback_after(config, steps[0]['provider']), health):
            steps.append(self._step(provider, 'fresh', None))
        return steps

    # ---- P38: plan inputs ----
    @staticmethod
    def _step(provider, mode, session, why=None):
        step = {'provider': provider, 'mode': mode, 'session': session}
        if why:
            step['why'] = why
        return step

    def _prefs(self, task_key):
        """P39: the task's stored launch preference {provider (pin or None), important, category}: its dispatch_prefs
        row, else (a task launched before P39) dispatch_meta's important / category with no pin."""
        row = self.db.execute('SELECT provider, important, category FROM dispatch_prefs WHERE task_key=?',
                              (task_key,)).fetchone()
        if row is None:
            row = self.db.execute('SELECT NULL AS provider, important, category FROM dispatch_meta WHERE task_key=?',
                                  (task_key,)).fetchone()
        if row is None:
            return {'provider': None, 'important': False, 'category': None}
        return {'provider': row['provider'] if row['provider'] in self.rt.worker_config.PROVIDERS else None,
                'important': bool(row['important']), 'category': row['category'] or None}

    def _preferred(self, task, config, prefs=None):
        """P39: the provider a task should run on — its pinned launch provider, else the intake role provider
        (<owner key>-inbox / <owner key>-oncall), else its routing category's provider (`worker_config.preferred_provider`)."""
        prefs = self._prefs(task['key']) if prefs is None else prefs
        return prefs['provider'] or self.rt.worker_config.preferred_provider(config, task['task_id'], prefs['category'])

    def _session_bytes(self, current, config):
        """The session's on-disk transcript size (None: cap off or transcript not found — then it cannot resume)."""
        if not config['dispatch']['max_session_context_mb']:
            return None
        try:
            return self.probe.session_bytes(*current)
        except Exception as exc:  # noqa: BLE001 - the cap never blocks a dispatch
            self.rt._meta_error(self.db, 'dispatch_context_size_error', exc)
            return None

    def _health(self, config):
        """{provider: status} when the provider-health monitor is on (missing = healthy), else None."""
        if not config['dispatch']['provider_health']['enabled']:
            return None
        try:
            return self.rt.provider_health.status_map(self.db)
        except Exception as exc:  # noqa: BLE001 - health never breaks routing
            self.rt._meta_error(self.db, 'provider_health_read_error', exc)
            return {}

    @staticmethod
    def _by_health(order, health):
        """`order` with degraded providers moved last (stable; unchanged when health is off or all are healthy)."""
        return sorted(order, key=lambda p: (health or {}).get(p) == DEGRADED)

    def _displaced(self, task, current, config):
        """The task's current session exists only because of a fallback or a health reroute (a dispatcher fallback
        step or a degraded_reroute run created it), the owner did not pin it (`set-current`), and no return attempt failed
        within the health window (then it waits a window before trying again)."""
        if not task.get('task_id'):
            return False
        row = self.db.execute('SELECT current_source FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone()
        if row and row[0] == 'manual':
            return False
        since = self.clock() - config['dispatch']['provider_health']['window_minutes'] * 60
        if self.db.execute("SELECT 1 FROM consumer_runs WHERE task=? AND route_reason='auto_return' AND outcome='failed' "
                           'AND ended_at>? LIMIT 1', (task['key'], since)).fetchone():
            return False
        return self.db.execute("SELECT 1 FROM consumer_runs WHERE task=? AND provider=? AND session=? AND mode='fresh' "
                               "AND (trigger='fallback' OR route_reason='degraded_reroute') LIMIT 1",
                               (task['key'], current[0], current[1])).fetchone() is not None

    def _hold(self, task, event_ids, w, reason, now, *, into=None, follow_up_of=None, state='waiting', plan=None,
              live=None, merge_held=False, include_owed=False, owed_since=None):
        """Mark events dispatched for one dispatch of the task (existing or new) and log the route."""
        rt = self.rt
        created = False
        with rt.transaction(self.db):
            target = into
            if follow_up_of is not None:
                target = self._task_dispatch(task['key'], ('waiting',))
            if target is None and state == 'paused':
                target = self._task_dispatch(task['key'], ('paused',))
            if target is None:
                if plan is None:
                    plan = self.plan_for(task, task['current'] or (rt.claude.provider(w), w['session']), self.config())
                important = self._prefs(task['key'])['important']  # P39: the stored launch preference
                dispatch_id = 'd' + uuid.uuid4().hex[:15]
                self.db.execute('INSERT INTO dispatches (dispatch_id,task_key,task_id,task_path,state,trigger,reason,plan,'
                                'next_at,important,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                (dispatch_id, task['key'], task['task_id'], task['path'], state,
                                 self._trigger_of(event_ids), reason, json.dumps(plan), now, int(bool(important)), now, now))
                target = self._dispatch(dispatch_id)
                created = True
            dispatch_id = target['dispatch_id']
            receipt = 'dispatch:' + dispatch_id
            ids = list(event_ids)
            if include_owed:
                # Everything this dead consumer still owes on the watch goes with the batch (P38: on an expired watch,
                # only what landed after it expired — older backlog is left alone).
                floor = owed_since if owed_since is not None else 0
                if w.get('state') == 'expired':
                    floor = max(floor, float(w.get('expired_at') or 0) + 1e-6)
                ids += [r[0] for r in self.db.execute(
                    "SELECT id FROM events WHERE watch_id=? AND status IN ('queued','delivery_unknown','claimed','sending') "
                    'AND received>=?', (w['id'], floor))]
                claimed = [r[0] for r in self.db.execute("SELECT id FROM events WHERE watch_id=? AND status='claimed' "
                                                         'AND received>=?', (w['id'], floor))]
                rt.claude.settle_claims(self.db, claimed, 'owner_dead_dispatched', now)
            for eid in dict.fromkeys(ids):
                self.db.execute("UPDATE events SET status='dispatched',receipt=? WHERE id=? AND status!='handled'",
                                (receipt, eid))
            if merge_held:
                for other in self.db.execute(f"SELECT dispatch_id FROM dispatches WHERE task_key=? AND dispatch_id!=? AND state IN ('needs_{self.core.key}','failed')", (task['key'], dispatch_id)).fetchall():
                    self.db.execute("UPDATE events SET receipt=? WHERE status='dispatched' AND receipt=?",
                                    (receipt, 'dispatch:' + other[0]))
                    self.db.execute("UPDATE dispatches SET state='superseded',updated_at=? WHERE dispatch_id=?",
                                    (now, other[0]))
            self.db.execute('UPDATE dispatches SET updated_at=? WHERE dispatch_id=?', (now, dispatch_id))
        self._route_log('held', reason, w, event_ids, task['key'], None, live, dispatch_id)
        if created and state == 'paused':  # the owner needs a [▶️ 恢复] button for the held events
            taken = (self.paused(task['key']) or {}).get('reason') == self.core.takeover_reason
            self._card_status(dispatch_id, 'paused', actor=self.core.takeover_actor if taken else None)
        return dispatch_id

    def _trigger_of(self, event_ids):
        kinds = self._event_kinds(event_ids)
        return kinds[0] if kinds else 'event'

    def _event_kinds(self, event_ids):
        kinds = []
        for eid in event_ids:
            row = self.db.execute('SELECT e.kind, m.kind FROM events x LEFT JOIN event_envelopes e ON e.event_id=x.id '
                                  'LEFT JOIN local_messages m ON m.event_id=x.id WHERE x.id=?', (eid,)).fetchone()
            kinds.append('note' if row and row[1] == 'note' else (row and row[0]) or 'slack_message')
        return kinds

    # ---- supervisor cycle ----
    def cycle(self, now=None):
        """One pass: reap finished runs, capture fresh sessions, enforce timeouts, start waiting dispatches."""
        now = self.clock() if now is None else now
        for run in [dict(r) for r in self.db.execute("SELECT * FROM consumer_runs WHERE state='running'")]:
            try:
                self._tend(run, now)
            except Exception as exc:  # noqa: BLE001 - one bad run never stops the loop
                self.rt._meta_error(self.db, 'dispatch_tend_error', exc)
        self._recover_stranded(now)
        try:
            config = self.config()
        except ValueError as exc:
            self.rt._meta_error(self.db, 'dispatch_config_error', exc)
            return
        if not config['dispatch']['enabled']:
            return
        for d in [dict(r) for r in self.db.execute("SELECT * FROM dispatches WHERE state='waiting' AND wait_reason IS NULL "
                                                   'AND coalesce(next_at,0)<=? ORDER BY created_at', (now,))]:
            try:
                self.start(d['dispatch_id'], now=now)
            except Exception as exc:  # noqa: BLE001
                self.rt._meta_error(self.db, 'dispatch_start_error', exc)

    def _recover_stranded(self, now):
        """A process that died between reserving the lease and spawning leaves a `running` dispatch
        without a run row; after the reservation went stale it becomes startable again."""
        with self.rt.transaction(self.db):
            for d in self.db.execute("SELECT d.dispatch_id, d.task_key, d.run_id FROM dispatches d LEFT JOIN consumer_runs r "
                                     "ON r.run_id=d.run_id WHERE d.state='running' AND r.run_id IS NULL").fetchall():
                lease = self.lease(d['task_key'])
                if lease and lease['run_id'] == d['run_id'] and now - lease['heartbeat_at'] < RESERVATION_STALE_SECONDS:
                    continue
                self.db.execute('DELETE FROM consumer_leases WHERE task_key=? AND run_id=? AND pid IS NULL',
                                (d['task_key'], d['run_id']))
                self.db.execute("UPDATE dispatches SET state='waiting',run_id=NULL,next_at=?,updated_at=? WHERE dispatch_id=?",
                                (now, now, d['dispatch_id']))

    def running_count(self):
        return self.db.execute("SELECT count(*) FROM consumer_runs WHERE state='running'").fetchone()[0]

    def capacity(self, config):
        """P39: {running, intake_running, background_running, concurrency, reserved_intake_slots} — intake = runs of the
        <owner key>-inbox / <owner key>-oncall tasks (`worker_config.intake_role`)."""
        tasks = [r[0] for r in self.db.execute("SELECT task FROM consumer_runs WHERE state='running'")]
        intake = sum(1 for t in tasks if self.rt.worker_config.intake_role(config, t))
        return {'running': len(tasks), 'intake_running': intake, 'background_running': len(tasks) - intake,
                'concurrency': config['dispatch']['concurrency'],
                'reserved_intake_slots': config['dispatch']['reserved_intake_slots']}

    def has_capacity(self, task_id, config):
        """P39 reserved intake lane: an intake task may start while fewer than concurrency + reserved runs are running;
        any other task while the background runs plus the intake runs beyond the reserve stay under concurrency
        (reserved 0 = one shared pool, exactly as before)."""
        cap = self.capacity(config)
        if self.rt.worker_config.intake_role(config, task_id):
            return cap['running'] < cap['concurrency'] + cap['reserved_intake_slots']
        return (cap['background_running'] + max(0, cap['intake_running'] - cap['reserved_intake_slots'])
                < cap['concurrency'])

    # ---- P40.1: keep the intake tasks' cached prompt prefix warm ----
    def keep_warm(self, now=None):
        """Supervisor step: refresh the cached prompt prefix of each intake task (<owner key>-inbox / <owner key>-oncall) that runs on
        Claude with a constant first message, so its first run after a quiet hour still reads it. Due when no real Claude
        run of the task ended within keep_warm.interval_minutes, the last use (a real run's end or a warmed refresh's
        start) is still inside the class's cache TTL — an already expired prefix is left to the next real run, which
        writes it anyway — and a real run ended within max_idle_hours. Returns the ids of the refreshes started."""
        now = self.clock() if now is None else now
        rt = self.rt
        self._reap_keep_warm(now)
        try:
            config = self.config()
        except ValueError:
            return []
        spec = rt.worker_config.keep_warm_spec(config)
        if not spec:
            return []
        health = self._health(config) or {}
        started = []
        for role in ('inbox', 'oncall'):
            task_id = config['intake'][f'{role}_task']
            if config['intake'][f'{role}_provider'] != 'claude' or health.get('claude') == DEGRADED:
                continue
            profile = rt.worker_config.lean_profile(config, task_id)
            if not profile['enabled'] or profile['first_message'] != 'constant' or self.paused(task_id):
                continue
            last = self._keep_warm_last(task_id)
            if last is None or not self._keep_warm_due(last, profile, spec, now):
                continue
            try:
                kw_id = self._start_keep_warm(task_id, last['task_path'], profile, config, now)
            except Exception as exc:  # noqa: BLE001 - a refresh never affects delivery or dispatch
                rt._meta_error(self.db, 'keep_warm_error', exc)
                continue
            if kw_id:
                started.append(kw_id)
        return started

    def _keep_warm_last(self, task_id):
        """The latest real Claude run of the task (None: it never ran on Claude, nothing to keep warm) and its latest
        refreshes."""
        run = self.db.execute("SELECT state, started_at, ended_at, task_path FROM consumer_runs WHERE task=? AND "
                              "provider='claude' ORDER BY started_at DESC, rowid DESC LIMIT 1", (task_id,)).fetchone()
        if run is None:
            return None
        kw = self.db.execute('SELECT state, started_at FROM keep_warm_runs WHERE task=? ORDER BY started_at DESC LIMIT 1',
                             (task_id,)).fetchone()
        warmed = self.db.execute("SELECT max(started_at) FROM keep_warm_runs WHERE task=? AND outcome='warmed'",
                                 (task_id,)).fetchone()[0]
        return {'run_state': run['state'], 'run_end': run['ended_at'] or run['started_at'], 'task_path': run['task_path'],
                'kw_state': kw['state'] if kw else None, 'kw_at': kw['started_at'] if kw else None, 'warmed_at': warmed}

    @staticmethod
    def _keep_warm_due(last, profile, spec, now):
        ttl, interval = CACHE_TTL_SECONDS[profile['cache_ttl']], spec['interval_minutes'] * 60
        if interval >= ttl or 'running' in (last['run_state'], last['kw_state']):
            return False
        if spec['max_idle_hours'] and now - last['run_end'] >= spec['max_idle_hours'] * 3600:
            return False
        used = max(last['run_end'], last['warmed_at'] or 0)
        if not interval <= now - used < ttl:
            return False
        # A failed refresh (relay error, ...) is tried again a little later while the prefix is still alive.
        return last['kw_at'] is None or now - last['kw_at'] >= min(interval, KEEP_WARM_RETRY_SECONDS)

    def _start_keep_warm(self, task_id, cwd, profile, config, now):
        """Spawn one refresh: the first request of a real fresh run of the task — `_launch`'s constant -p message,
        `_claude_settings`, `build_command`, the same cwd, model and effort — with a no-op Run context, every tool denied
        by the hook (matcher *), one API call (--max-turns 1) and no transcript. Recorded in keep_warm_runs only."""
        rt = self.rt
        if not cwd or not Path(cwd).is_dir():
            return None
        kw_id = 'k' + new_run_id(now)[1:]
        model, effort, yolo = rt.worker_config.model_for_task(config, 'claude', False, task_id)
        root = self.runs_root / 'keep-warm'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir = root / kw_id
        run_dir.mkdir(mode=0o700)
        session = str(uuid.uuid4())
        d = {'dispatch_id': None, 'task_id': task_id, 'task_key': task_id, 'task_path': cwd, 'trigger': 'keep_warm',
             'prompt': None}
        step = {'provider': 'claude', 'mode': 'fresh', 'session': session, 'why': 'long_lived'}
        prompt, _ = rt.dispatch_prompt_parts(self.db, self.db_path, task=self.task_for_key(task_id), dispatch=d,
                                             step=step, run_id=kw_id, events=[], previous=[], context_file=True)
        _write_private(run_dir / 'prompt.md', prompt)
        _write_private(run_dir / RUN_CONTEXT_FILE, rt.KEEP_WARM_CONTEXT)
        settings, extra, summary = self._claude_settings(profile, cwd, kw_id)
        settings['hooks']['PreToolUse'][0]['matcher'] = '*'  # dispatch-hook answers deny for every tool
        cmd = self.core.build_command('claude', 'fresh', session, prompt, model, effort, yolo, run_dir, settings,
                            [*extra, *KEEP_WARM_ARGS], None, profile['drop_tools'])
        env = self._env(kw_id, task_id, 0, 'claude', config)
        env[rt.RUN_CONTEXT_ENV] = str(run_dir / RUN_CONTEXT_FILE)
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        meta = {'kw_id': kw_id, 'task': task_id, 'cwd': cwd, 'model': model, 'effort': effort, 'started_at': _iso(now),
                'argv_without_prompt': ['<prompt.md>' if a == prompt else a for a in cmd],
                'context': {**summary, 'layout': 'p40.1', 'first_message': 'constant', 'keep_warm': True,
                            'prompt_sha256': prompt_sha},
                'env_injected': list(self.last_env_injected)}
        _write_private(run_dir / 'meta.json', json.dumps(meta, ensure_ascii=False, indent=2))
        pid = self.spawn(cmd, cwd, env, run_dir)
        with rt.transaction(self.db):
            self.db.execute('INSERT INTO keep_warm_runs (kw_id,task,task_path,model,effort,prompt_sha,state,started_at,'
                            'log_dir,pid,pid_start) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                            (kw_id, task_id, cwd, model, effort, prompt_sha, 'running', now, str(run_dir), pid,
                             self.pid_start(pid)))
        return kw_id

    def _reap_keep_warm(self, now):
        """End finished refreshes (`warmed` when the log holds the API call's usage, else `failed`; `timeout` when one
        still ran after KEEP_WARM_MAX_SECONDS and was killed) and prune those older than KEEP_WARM_KEEP_DAYS."""
        rt = self.rt
        for row in [dict(r) for r in self.db.execute("SELECT * FROM keep_warm_runs WHERE state='running'")]:
            exit_info = self._exit_info(row)
            alive = exit_info is None and self.alive(row['pid'], row['pid_start'])
            if alive and now - row['started_at'] <= KEEP_WARM_MAX_SECONDS:
                continue
            if alive:
                try:
                    self.killer(row['pid'], signal.SIGKILL)
                except OSError:
                    pass
            child = self.children.pop(row['pid'], None)
            if child is not None:
                child.poll()
            usage = rt.cache_report.claude_usage(Path(row['log_dir'], 'events.jsonl')) if row['log_dir'] else None
            outcome = 'warmed' if usage and usage['first_input'] else 'timeout' if alive else 'failed'
            with rt.transaction(self.db):
                self.db.execute("UPDATE keep_warm_runs SET state='ended',ended_at=?,exit_code=?,outcome=? WHERE kw_id=?",
                                (now, (exit_info or {}).get('exit_code'), outcome, row['kw_id']))
        cutoff = now - KEEP_WARM_KEEP_DAYS * 86400
        root = (self.runs_root / 'keep-warm').resolve()
        for row in self.db.execute("SELECT log_dir FROM keep_warm_runs WHERE state='ended' AND started_at<?",
                                   (cutoff,)).fetchall():
            path = Path(row['log_dir'] or '')
            if KEEP_WARM_ID_RE.fullmatch(path.name) and path.parent.resolve() == root:
                shutil.rmtree(path, ignore_errors=True)
        with rt.transaction(self.db):
            self.db.execute("DELETE FROM keep_warm_runs WHERE state='ended' AND started_at<?", (cutoff,))

    # ---- P44: wait for a provider to come back ----
    def probe_providers(self, now=None):
        """Supervisor step (every KEEP_WARM_CHECK_SECONDS, beside keep_warm): reap the providers' probes, release the
        dispatches waiting for a provider that answered, fail (card, as before) those waiting longer than
        provider_wait_hours, and start ONE probe per provider that a due waiting dispatch needs (none in flight, its
        backoff passed). Switched off (provider_wait_hours 0 / probe_enabled false) it releases every wait. Never an
        event, dispatch, lease, run row or Slack post of its own. Returns {provider: ok | down | other | started}."""
        now = self.clock() if now is None else now
        try:
            config = self.config()
        except ValueError:
            return {}
        cfg, out = config['dispatch'], {}
        for provider, entry in probe_state(self.db).items():
            if entry.get('running'):
                try:
                    result = self._reap_probe(provider, entry['running'], cfg, now)
                except Exception as exc:  # noqa: BLE001 - a damaged probe record is dropped, never freezes the waits
                    self.rt._meta_error(self.db, 'provider_probe_error', exc)
                    result = self._probe_result(provider, 'crashed', None, cfg, now)
                if result:
                    out[provider] = result
        if not cfg['enabled']:
            return out
        if not (cfg['provider_wait_hours'] and cfg['probe_enabled']):
            self._release_waits(None, now)  # they run now; a provider-side failure then fails as before
            return out
        for d in self._waits():
            if self.paused(d['task_key']):  # ⏹ / takeover raced the failure: held like any paused dispatch
                with self.rt.transaction(self.db):
                    self.db.execute("UPDATE dispatches SET state='paused',wait_reason=NULL,wait_since=NULL,updated_at=? "
                                    "WHERE dispatch_id=? AND state='waiting'", (now, d['dispatch_id']))
            elif now - (d['wait_since'] or now) >= cfg['provider_wait_hours'] * 3600:
                self._fail(d, d['error_class'] or 'transient', now)
        waits, state = self._waits(), probe_state(self.db)
        for provider in self.rt.worker_config.PROVIDERS:
            entry = state.get(provider) or {}
            due = [d for d in waits if provider in _plan_providers(d['plan']) and (d['next_at'] or 0) <= now]
            if entry.get('fails') and not entry.get('running') and not any(
                    provider in _plan_providers(d['plan']) for d in waits):  # nothing waits for it: backoff restarts
                state[provider] = {**entry, 'fails': 0, 'next_at': None}
                self._probe_save(state)
            if not due or entry.get('running') or (entry.get('next_at') or 0) > now:
                continue
            try:
                self._start_probe(provider, due[0], config, now)
                out[provider] = 'started'
            except Exception as exc:  # noqa: BLE001 - a probe that cannot start cannot tell: the real run decides
                self.rt._meta_error(self.db, 'provider_probe_error', exc)
                out[provider] = self._probe_result(provider, 'spawn_failed', type(exc).__name__, cfg, now)
        return out

    def _waits(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM dispatches WHERE state='waiting' AND wait_reason=? "
                                                 'ORDER BY created_at', (WAIT_REASON,))]

    def _start_probe(self, provider, d, config, now):
        """One minimal real call with the model / effort the dispatcher uses for `d` (the oldest due wait): a constant
        one-line prompt, one turn, no tool, nothing persisted, in a fresh temp directory, with keep-warm's environment
        (`_env`: agent variables stripped, the relay variables passed through). Only the probe state records it."""
        model, effort, _ = self.rt.worker_config.model_for_task(config, provider, bool(d['important']), d['task_id'])
        run_dir = Path(tempfile.mkdtemp(prefix=self.core.probe_dir_prefix))
        if provider == 'claude':
            cmd = self.core.build_command('claude', 'fresh', str(uuid.uuid4()), PROBE_PROMPT, model, effort, (), run_dir, None,
                                PROBE_CLAUDE_ARGS)
        else:
            cmd = self.core.build_command('codex', 'fresh', None, PROBE_PROMPT, model, effort, PROBE_CODEX_ARGS, run_dir)
        try:
            pid = self.spawn(cmd, str(run_dir), self._env('p' + new_run_id(now)[1:], 'provider-probe', 0, provider, config),
                             run_dir)
        except Exception:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        state = probe_state(self.db)
        state[provider] = {**(state.get(provider) or {}), 'model': model, 'running': {
            'pid': pid, 'pid_start': self.pid_start(pid), 'started_at': now, 'dir': str(run_dir)}}
        self._probe_save(state)

    def _reap_probe(self, provider, running, cfg, now):
        """The probe's result once it exited (or was killed after PROBE_TIMEOUT_SECONDS); None while it runs."""
        run_dir = Path(running['dir'])
        exit_info = self._exit_info({'log_dir': run_dir})
        alive = exit_info is None and self.alive(running['pid'], running.get('pid_start'))
        if alive and now - running['started_at'] <= PROBE_TIMEOUT_SECONDS:
            return None
        if alive:
            try:
                self.killer(running['pid'], signal.SIGKILL)
            except OSError:
                pass
        child = self.children.pop(running['pid'], None)
        if child is not None:
            child.poll()
        info = self.core.parse_run_log(provider, _read_tail(run_dir / 'events.jsonl'), _read_tail(run_dir / 'stderr.log', 8192),
                             _read_tail(run_dir / 'last.md', 4096) if (run_dir / 'last.md').exists() else None)
        if alive:
            error_class, marker = 'transient', 'probe_timeout'
        elif (exit_info or {}).get('error'):
            error_class, marker = 'spawn_failed', str(exit_info['error'])[:40]
        elif info['cli_ok'] is not False and exit_info and exit_info.get('exit_code') == 0:
            error_class, marker = None, None
        else:
            error_class, marker = run_failure(info, exit_info)
        if run_dir.name.startswith(self.core.probe_dir_prefix) and run_dir.is_dir() and not run_dir.is_symlink():
            shutil.rmtree(run_dir, ignore_errors=True)
        return self._probe_result(provider, error_class, marker, cfg, now)

    def _probe_result(self, provider, error_class, marker, cfg, now):
        """Record a probe: `ok`, `down` (a provider-side class: backoff doubles, the waits move to the next probe) or
        `other` (auth, model, spawn, unknown...: a wait cannot fix it, so the waits run and fail as before, with a
        card). `ok` and `other` release the provider's waits and reset its backoff."""
        result = 'ok' if error_class is None else 'down' if error_class in PROVIDER_WAIT else 'other'
        state = probe_state(self.db)
        fails = (state.get(provider) or {}).get('fails', 0) + 1 if result == 'down' else 0
        state[provider] = {**(state.get(provider) or {}), 'at': now, 'result': result, 'error_class': error_class,
                           'marker': marker, 'fails': fails, 'running': None,
                           'next_at': now + self._probe_interval(cfg, fails) if fails else None}
        self._probe_save(state)
        if result != 'down':
            self._release_waits(provider, now)
            return result
        for d in self._waits():
            providers = _plan_providers(d['plan'])
            if provider not in providers:
                continue
            next_at = max(d['next_at'] or now, min((state.get(p) or {}).get('next_at') or now for p in providers))
            if next_at != d['next_at']:
                with self.rt.transaction(self.db):
                    self.db.execute('UPDATE dispatches SET next_at=? WHERE dispatch_id=?', (next_at, d['dispatch_id']))
                self._card_status(d['dispatch_id'], WAIT_REASON)
        return result

    def _probe_save(self, state):
        with self.rt.transaction(self.db):
            self.db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (PROBE_META, json.dumps(state)))

    def _release_waits(self, provider, now):
        """The dispatches waiting for `provider` (None = all) become startable now (`cycle` starts them)."""
        ids = [d['dispatch_id'] for d in self._waits() if provider is None or provider in _plan_providers(d['plan'])]
        with self.rt.transaction(self.db):
            for dispatch_id in ids:
                self.db.execute("UPDATE dispatches SET wait_reason=NULL,next_at=?,updated_at=? WHERE dispatch_id=? AND "
                                "state='waiting' AND wait_reason=?", (now, now, dispatch_id, WAIT_REASON))
        return ids

    @staticmethod
    def _probe_interval(cfg, failures):
        """probe_min_seconds, doubling per consecutive failure, at most probe_max_seconds."""
        return min(cfg['probe_max_seconds'], cfg['probe_min_seconds'] * 2 ** min(max(0, failures - 1), 16))

    # ---- P38 C1: deliveries a dead session never consumed ----
    def sweep_stranded(self, now=None):
        """Supervisor step. An event queued to (Claude outbox / Codex queue) or claimed by a session that then died
        without consuming it would wait forever: `route` only acts on pending events. Once that session is seen dead on
        two sweeps STRANDED_CONFIRM_SECONDS apart (the runtime's liveness probe — an interactive Claude session is live
        through `claude agents` / its session file, never by a process-name match) and no run of ours is that session,
        the OWNING task gets `route`'s dead-owner treatment: hold for its running / paused / waiting / failed dispatch,
        rebind to a live session of it, or revive it (lease + global cap at start). Idempotent: moved events leave the
        candidate set; `stranded_deliveries` keeps one row per event (suspect -> held / rerouted / unowned; P39: ->
        handled once its event is)."""
        rt = self.rt
        now = self.clock() if now is None else now
        try:
            config = self.config()
        except ValueError as exc:
            rt._meta_error(self.db, 'dispatch_config_error', exc)
            return []
        with rt.transaction(self.db):
            self.db.execute('DELETE FROM stranded_deliveries WHERE updated_at<?', (now - 7 * 86400,))
            # P39: a revived / rerouted / unowned delivery whose event was handled since (or is gone) is done: `handled`
            # (terminal), so the health counts stop showing it as held.
            self.db.execute("UPDATE stranded_deliveries SET state='handled',updated_at=? WHERE state IN "
                            "('held','rerouted','unowned') AND NOT EXISTS (SELECT 1 FROM events e WHERE "
                            "e.id=stranded_deliveries.event_id AND e.status!='handled')", (now,))
        if not config['dispatch']['enabled'] or not config['dispatch']['stranded_revive']:
            return []
        owed_since = now - config['dispatch']['max_event_age_hours'] * 3600
        by_watch = {}
        for r in self.db.execute(  # literal statuses, so SQLite can use the events_undelivered partial index
                "SELECT e.id, e.watch_id, e.status FROM events e JOIN watches w ON w.id=e.watch_id WHERE e.status IN (%s) "
                "AND (json_extract(w.data,'$.state') NOT IN ('stopped','expired') OR (json_extract(w.data,'$.state')="
                "'stopped' AND json_extract(w.data,'$.kind')='approvals') OR (json_extract(w.data,'$.state')='expired' "
                "AND e.received>coalesce(json_extract(w.data,'$.expired_at'),e.received))) AND e.received>=? "
                'ORDER BY e.received, e.id'
                % ','.join(f"'{s}'" for s in STRANDED_STATUSES), (owed_since,)).fetchall():
            by_watch.setdefault(r['watch_id'], []).append(r)
        ours = {tuple(r) for r in self.db.execute("SELECT provider, session FROM consumer_runs WHERE state='running' AND "
                                                  "session IS NOT NULL UNION SELECT provider, session FROM consumer_leases "
                                                  "WHERE holder_kind='headless' AND session IS NOT NULL")}
        results = []
        for wid, items in by_watch.items():
            w = rt.load(self.db, wid)
            ids = [r['id'] for r in items]
            owner = (rt.claude.provider(w), w['session'])
            if (w.get('smoke') and not w.get('smoke_dispatch')) or owner in ours or self._recently_live(w, owner, now):
                self._stranded_clear(ids)  # a smoke watch, our own run is that session, or it proved to be live
                continue
            live = self.probe.check(*owner)
            if live['state'] != rt.liveness.DEAD:
                self._stranded_clear(ids)  # live or unknown: today's delivery stands
                continue
            if now - self._stranded_seen(items, w, owner, now) < STRANDED_CONFIRM_SECONDS:
                continue
            if w.get('state') == 'stopped':
                # P38 (F03 d): a stopped approvals watch never delivers again — what its dead session never consumed goes
                # to the task's consumer (claims settled), the way its unconsumed pending replies already went.
                moved = rt._hand_off_stopped_approvals(self.db, w, now, self, in_flight=True)
                self._stranded_mark(ids, 'rerouted' if moved else 'unowned', w.get('task_id'), 'stopped_watch_handed_off',
                                    None, now)
                results.append({'watch_id': wid, 'events': ids, 'task': w.get('task_id'), 'action': 'rerouted',
                                'reason': 'stopped_watch_handed_off', 'moved': moved})
                continue
            task = self.owning_task(w, ids, config)
            if task is None:
                self._stranded_mark(ids, 'unowned', None, None, None, now)
                continue
            running = self._task_dispatch(task['key'], ('running',))
            if running is not None:
                d = self._hold(task, ids, w, 'stranded_task_run_active', now, follow_up_of=running, include_owed=True,
                               owed_since=owed_since)
                result = {'action': 'held', 'reason': 'stranded_task_run_active', 'dispatch_id': d}
            else:
                result = self._owner_dead(task, w, ids, live, config, now, owed_since, revive='stranded_revive')
            if result['action'] in ('held', 'rerouted'):  # as_today (a current session of unknown liveness): next sweep
                self._stranded_mark(ids, result['action'], task['key'], result['reason'], result.get('dispatch_id'), now)
            results.append({'watch_id': wid, 'events': ids, 'task': task['key'], **result})
        return results

    def _recently_live(self, w, owner, now):
        """`route`'s live window after a session_live hand-back, or a run of ours that the CLI refused within the last
        LIVE_PROOF_SECONDS because that session had an active writer (the probe misjudged it): do not revive over it."""
        if float(w.get('dispatch_live_until') or 0) > now:
            return True
        return self.db.execute("SELECT 1 FROM consumer_runs WHERE provider=? AND session=? AND error_class='session_live' "
                               'AND ended_at>? LIMIT 1', (owner[0], owner[1], now - LIVE_PROOF_SECONDS)).fetchone() is not None

    def owning_task(self, w, event_ids, config):
        """P38 C1: the task a delivery belongs to, even when its watch names none (a card / ask-<owner key> watch registered
        from a cwd outside the workspace): the watch's task or cwd, else the task of the card it carries (or of the
        card's approvals thread), of the dispatched runs that used the session, or whose current session it is; else
        the per-session key of an unbound watch (`task_for_watch`)."""
        if w.get('task_id') or self.rt.registry.task_id_for_cwd(w.get('cwd')):
            return self.task_for_watch(w, config)
        provider, session = self.rt.claude.provider(w), w['session']
        marks = ','.join('?' * len(event_ids))
        cards = (f'SELECT c.event_id, c.task_id, c.channel, c.thread_root FROM escalation_cards c WHERE c.event_id IN '
                 f'(SELECT event_id FROM decisions WHERE delivered_event IN ({marks})) OR c.event_id IN '
                 f'(SELECT event_id FROM escalations WHERE watch_id=?)')
        lookups = ((f'SELECT task_id FROM ({cards}) WHERE task_id IS NOT NULL', (*event_ids, w['id'])),
                   (f'SELECT a.task_id FROM ({cards}) c JOIN approvals_roots a ON a.channel=c.channel AND '
                    'a.root_ts=c.thread_root WHERE a.task_id IS NOT NULL', (*event_ids, w['id'])),
                   ("SELECT task FROM consumer_runs WHERE provider=? AND session=? AND task NOT LIKE 'session:%' "
                    'ORDER BY started_at DESC LIMIT 1', (provider, session)),
                   ('SELECT task_id FROM tasks WHERE current_provider=? AND current_session=? LIMIT 1', (provider, session)))
        for sql, args in lookups:
            row = self.db.execute(sql, args).fetchone()
            task = self.task_for_key(row[0]) if row and row[0] else None
            if task is not None:
                return task
        return self.task_for_watch(w, config)

    def _stranded_seen(self, items, w, owner, now):
        """Record the events as suspects (kept while the same session stays dead) -> when they were first seen so."""
        with self.rt.transaction(self.db):
            for r in items:
                self.db.execute(
                    'INSERT INTO stranded_deliveries (event_id,watch_id,provider,session,status,state,first_seen,updated_at) '
                    "VALUES (?,?,?,?,?,'suspect',?,?) ON CONFLICT(event_id) DO UPDATE SET watch_id=excluded.watch_id,"
                    "status=excluded.status,updated_at=excluded.updated_at,first_seen=CASE WHEN stranded_deliveries.state "
                    "IN ('suspect','unowned') AND stranded_deliveries.session=excluded.session THEN "
                    "stranded_deliveries.first_seen ELSE excluded.first_seen END,state=CASE WHEN stranded_deliveries.state="
                    "'unowned' AND stranded_deliveries.session=excluded.session THEN 'unowned' ELSE 'suspect' END,"
                    'provider=excluded.provider,session=excluded.session,task_key=NULL,reason=NULL,dispatch_id=NULL',
                    (r['id'], w['id'], owner[0], owner[1], r['status'], now, now))
        return min(self.db.execute('SELECT first_seen FROM stranded_deliveries WHERE event_id=?', (r['id'],)).fetchone()[0]
                   for r in items)

    def _stranded_mark(self, event_ids, state, task_key, reason, dispatch_id, now):
        with self.rt.transaction(self.db):
            for eid in event_ids:
                self.db.execute('UPDATE stranded_deliveries SET state=?,task_key=?,reason=?,dispatch_id=?,updated_at=? '
                                'WHERE event_id=?', (state, task_key, reason, dispatch_id, now, eid))

    def _stranded_clear(self, event_ids):
        with self.rt.transaction(self.db):
            self.db.execute("DELETE FROM stranded_deliveries WHERE state IN ('suspect','unowned') AND event_id IN (%s)"
                            % ','.join('?' * len(event_ids)), event_ids)

    def start(self, dispatch_id, now=None):
        """Start the next step of a waiting dispatch if capacity and the task lease allow. Returns run_id or None."""
        rt = self.rt
        now = self.clock() if now is None else now
        config = self.config()
        d = self._dispatch(dispatch_id)
        if d is None or d['state'] != 'waiting' or d.get('wait_reason'):  # P44: a provider wait starts once released
            return None
        if self.paused(d['task_key']):
            with rt.transaction(self.db):
                self.db.execute("UPDATE dispatches SET state='paused',updated_at=? WHERE dispatch_id=? AND state='waiting'",
                                (now, dispatch_id))
            return None
        events = self._held_events(dispatch_id)
        if not events and d['trigger'] != 'launch':
            with rt.transaction(self.db):
                self.db.execute("UPDATE dispatches SET state='done',outcome='nothing_to_do',ended_at=?,updated_at=? "
                                "WHERE dispatch_id=? AND state='waiting'", (now, now, dispatch_id))
            return None
        lease = self.lease(d['task_key'])
        if lease is not None and lease['holder_kind'] == 'interactive':
            if d['trigger'] != 'launch' and self.lease_valid(lease):
                return self._yield_to_interactive(d, lease, events, now)
            with rt.transaction(self.db):
                self.release_lease(d['task_key'])
            lease = None
        if lease is not None and self.lease_valid(lease):
            return None  # another headless consumer of this task is alive: never two
        plan = json.loads(d['plan'])
        fresh_attempt = not (d['step'] or d['retries'] or d['partial_retries'])
        if fresh_attempt and d['trigger'] != 'launch' and d['reason'] != f'{self.core.key}_retry':
            # First attempt: resolve the plan now, the task's current session may have changed while waiting.
            task = self.task_for_key(d['task_key'])
            if task is not None:
                plan = self.plan_for(task, task['current'], config)
        if fresh_attempt and d.get('wait_since'):
            # P44: back from a provider wait: a provider whose last probe failed is tried last (recorded as a reroute,
            # so the task returns to its preferred provider later, as after any health reroute).
            down = {p for p, s in probe_state(self.db).items() if (s or {}).get('result') == 'down'}
            order = sorted(plan, key=lambda s: s['provider'] in down)
            if order[0] is not plan[0] and not order[0].get('why'):
                order[0] = {**order[0], 'why': 'degraded_reroute'}
            plan = order
        if d['step'] >= len(plan):
            return self._fail(d, d['error_class'] or 'unknown', now)
        step = dict(plan[d['step']])
        if step['mode'] == 'fresh' and step['provider'] == 'claude' and not step.get('session'):
            step['session'] = str(uuid.uuid4())
            plan[d['step']] = step
        run_id = new_run_id(now)
        with rt.transaction(self.db):
            if not self.has_capacity(d['task_id'], config):  # P39: the intake lane has its reserved slots
                return None
            if self.paused(d['task_key']):  # P43: a hold placed since the check above (the next cycle parks it)
                return None
            if step['mode'] == 'resume' and self.db.execute(
                    "SELECT 1 FROM consumer_runs WHERE state='running' AND provider=? AND session=?",
                    (step['provider'], step.get('session'))).fetchone():
                return None  # P38: that session is already being run (e.g. under another task key): wait for it
            current = self.lease(d['task_key'])
            if current is not None and current['holder_kind'] == 'headless' and (
                    current['pid'] is None and now - current['heartbeat_at'] < RESERVATION_STALE_SECONDS
                    or self.alive(current['pid'], current['pid_start'])):
                return None
            self.db.execute('INSERT OR REPLACE INTO consumer_leases VALUES (?,?,?,?,?,?,?,?,?)',
                            (d['task_key'], 'headless', step['provider'], step.get('session'), None, None, run_id, now, now))
            self.db.execute("UPDATE dispatches SET state='running',run_id=?,plan=?,updated_at=? WHERE dispatch_id=?",
                            (run_id, json.dumps(plan), now, dispatch_id))
        try:
            return self._launch(d, step, run_id, events[:MAX_BATCH], config, now)
        except Exception as exc:  # noqa: BLE001 - spawn/prompt failure is a run outcome, never a lost event
            with rt.transaction(self.db):
                self.release_lease(d['task_key'], run_id)
                self.db.execute("UPDATE dispatches SET state='waiting',updated_at=? WHERE dispatch_id=?", (now, dispatch_id))
            self._record_spawn_failure(d, step, run_id, events, exc, now)
            return None

    def _yield_to_interactive(self, d, lease, events, now):
        """A live interactive session of the task appeared: hand the batch to it (today's delivery)."""
        rt = self.rt
        watches = {r[0] for r in self.db.execute('SELECT watch_id FROM events WHERE id IN (%s)' % ','.join('?' * len(events)),
                                                  events)} if events else set()
        for wid in watches:
            self.rebind_watch(wid, lease['provider'], lease['session'], 'yield_to_interactive')
        with rt.transaction(self.db):
            for eid in events:
                self.db.execute("UPDATE events SET status='pending',receipt=NULL WHERE id=? AND status='dispatched'", (eid,))
            self.db.execute("UPDATE dispatches SET state='superseded',outcome='interactive_session_live',ended_at=?,"
                            'updated_at=? WHERE dispatch_id=?', (now, now, d['dispatch_id']))
        self._route_log('rerouted', 'yield_to_interactive', None, events, d['task_key'],
                        (lease['provider'], lease['session']), None, d['dispatch_id'])
        return None

    def _env(self, run_id, task_key, hops, provider=None, config=None):
        env = self.core.strip_agent_env(dict(os.environ))
        env.update(**{self.core.run_id_env: run_id}, **{self.core.task_env: task_key}, **{self.core.hops_env: str(hops)})
        self.last_env_injected = []
        if provider == 'claude' and config is not None:
            names = [n for n in config['dispatch'].get('claude_env_passthrough') or [] if not env.get(n)]
            if names:
                found = self.login_env(config['dispatch'].get('login_shell') or '/bin/zsh', names)
                for name in names:
                    if found.get(name):
                        env[name] = found[name]
                        self.last_env_injected.append(name)
        return env

    def login_env(self, shell, names, ttl=300):
        """Whitelisted variables from the owner's login shell (launchd does not source ~/.zshrc). Values are
        never logged or persisted; only names are recorded on the run."""
        key = (shell, tuple(sorted(names)))
        cached = getattr(self, '_login_env_cache', {}).get(key)
        if cached and self.clock() - cached[0] < ttl:
            return cached[1]
        # Static parse first: deterministic and independent of a TTY (an interactive zsh started by the
        # launchd supervisor returned nothing live). The shell is only a fallback for missing names.
        found = self.core.rc_exports(names)
        if found:  # never block the supervisor on a shell when the startup files already answer
            self._login_env_cache = {**getattr(self, '_login_env_cache', {}), key: (self.clock(), found)}
            return found
        base = {k: os.environ[k] for k in ('HOME', 'USER', 'LOGNAME', 'PATH') if k in os.environ}
        base['TERM'] = 'dumb'
        try:
            out = subprocess.run([shell, '-ic', 'env -0'], stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=20, env=base).stdout
        except (OSError, subprocess.SubprocessError):
            out = b''
        for item in out.split(b'\0'):
            name, sep, value = item.partition(b'=')
            name = name.decode('utf-8', 'replace').rsplit('\n', 1)[-1]
            if sep and name in names and value:
                found[name] = value.decode('utf-8', 'replace')
        self._login_env_cache = {**getattr(self, '_login_env_cache', {}), key: (self.clock(), found)}
        return found

    def _launch(self, d, step, run_id, events, config, now):
        rt = self.rt
        provider, mode, session = step['provider'], step['mode'], step.get('session')
        # P2: <owner key>-inbox / <owner key>-oncall use intake.<role>_model/_effort on their configured provider.
        model, effort, yolo = rt.worker_config.model_for_task(config, provider, bool(d['important']), d['task_id'])
        run_dir = self.runs_root / run_id
        self.runs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.runs_root, 0o700)
        run_dir.mkdir(mode=0o700)
        if session:
            for wid in {r[0] for r in self.db.execute('SELECT watch_id FROM events WHERE id IN (%s)'
                                                      % ','.join('?' * len(events)), events)} if events else ():
                self.rebind_watch(wid, provider, session, 'dispatch_' + mode)
        previous = json.loads(d['failures'] or '[]')
        cwd = self._run_cwd(d, step)
        # P40: stable instructions + Run context. P40.1: a fresh Claude run of a `constant` class gets the instructions as
        # its whole -p message (the same bytes every run) and reads the Run context from run-context.md.
        profile = rt.worker_config.lean_profile(config, d['task_id'])
        layout = self.core.prompt_layout(profile, provider, mode)
        rules, context = rt.dispatch_prompt_parts(self.db, self.db_path, task=self.task_for_key(d['task_key']),
                                                  dispatch=d, step=step, run_id=run_id, events=events, previous=previous,
                                                  context_file=layout == 'constant')
        prompt, system_prompt = {'constant': (rules, None), 'split': (rt.SPLIT_LEAD + '\n' + context, rules),
                                 'single': (rules + '\n' + context, None)}[layout]
        _write_private(run_dir / 'prompt.md', prompt)
        if system_prompt:
            _write_private(run_dir / 'system-prompt.md', system_prompt)
        if layout == 'constant':
            _write_private(run_dir / RUN_CONTEXT_FILE, context)
        settings = extra = summary = None
        if provider == 'claude':
            settings, extra, summary = self._claude_settings(profile, cwd, run_id)
        cmd = self.core.build_command(provider, mode, session, prompt, model, effort, yolo, run_dir, settings, extra or (),
                            system_prompt, profile['drop_tools'] if provider == 'claude' else ())
        hops = max([0] + [int(r[0] or 0) for r in self.db.execute(
            'SELECT hops FROM event_envelopes WHERE event_id IN (%s)' % ','.join('?' * len(events)), events)]) if events else 0
        if d['retries']:
            trigger = 'transient_retry'
        elif d['partial_retries']:
            trigger = 'partial_retry'
        elif d['step']:
            trigger = 'fallback'
        elif d['reason'] == f'{self.core.key}_retry':
            trigger = f'{self.core.key}_retry'
        else:
            trigger = d['trigger']
        meta = {'run_id': run_id, 'dispatch_id': d['dispatch_id'], 'task': d['task_key'], 'task_path': d['task_path'],
                'provider': provider, 'mode': mode, 'session': session, 'model': model, 'effort': effort,
                'trigger': trigger, 'why': step.get('why'), 'event_ids': events, 'started_at': _iso(now),
                'argv_without_prompt': ['<prompt.md>' if a == prompt else '<system-prompt.md>' if a == system_prompt
                                        else a for a in cmd],
                # P40: what the run loads besides its prompt, and the instructions' version (hash of the stable part).
                # P40.1: layout p40.1 = constant first message (prompt_sha256 is then the same for every run).
                'context': {**(summary or {'class': profile['class'], 'lean': profile['enabled']}),
                            'layout': 'p40.1' if layout == 'constant' else 'p40',
                            'rules_in_system_prompt': layout == 'split',
                            'rules_sha256': hashlib.sha256(rules.encode()).hexdigest()[:16],
                            **({'first_message': 'constant', 'context_file': RUN_CONTEXT_FILE,
                                'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()[:16]}
                               if layout == 'constant' else {})}}
        meta['cwd'] = cwd
        env = self._env(run_id, d['task_key'], hops, provider, config)
        if layout == 'constant':
            env[rt.RUN_CONTEXT_ENV] = str(run_dir / RUN_CONTEXT_FILE)
        meta['env_injected'] = list(self.last_env_injected)  # names only, never values
        _write_private(run_dir / 'meta.json', json.dumps(meta, ensure_ascii=False, indent=2))
        pid = self.spawn(cmd, cwd, env, run_dir)
        pid_start = self.pid_start(pid)
        attempt = 1 + self.db.execute('SELECT count(*) FROM consumer_runs WHERE dispatch_id=?',
                                      (d['dispatch_id'],)).fetchone()[0]
        with rt.transaction(self.db):
            self.db.execute('INSERT INTO consumer_runs (run_id,dispatch_id,task,task_path,provider,session,model,effort,mode,'
                            'trigger,event_ids,attempt,state,started_at,log_dir,pid,pid_start,route_reason) VALUES '
                            '(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (run_id, d['dispatch_id'], d['task_key'], d['task_path'], provider, session, model, effort,
                             mode, trigger, json.dumps(events), attempt, 'running', now, str(run_dir), pid, pid_start,
                             step.get('why')))
            self.db.execute('UPDATE consumer_leases SET pid=?,pid_start=?,session=?,heartbeat_at=? WHERE task_key=? '
                            'AND run_id=?', (pid, pid_start, session, now, d['task_key'], run_id))
        if step.get('why') in ('long_lived', 'context_cap', 'auto_return', 'degraded_reroute'):
            current = (self.task_for_key(d['task_key']) or {}).get('current')
            self._switch_log(d, step['why'], current, (provider, session), events, now)
        if d['trigger'] == 'launch' and session:
            self._register_current(d, provider, session, now, 'launch')
        self._card_status(d['dispatch_id'], 'running')
        return run_id

    def _claude_settings(self, profile, cwd, run_id):
        """(settings, extra argv, summary) of a Claude run: the interactive-tool hook plus the class's lean keys. P40.1:
        shared by `_launch` and `keep_warm`, so a refresh sends exactly a real run's request."""
        lean, extra, summary = self.core.lean_claude(profile, cwd)
        return {**self.core.claude_settings(self.python, self.script, self.db_path, run_id), **lean}, extra, summary

    def _switch_log(self, d, why, frm, to, events, now):
        """P38: one dispatch_routes row per session / provider switch (decision `switched`, reason = why)."""
        with self.rt.transaction(self.db):
            self.db.execute('INSERT INTO dispatch_routes (event_id,watch_id,task_key,decision,reason,from_provider,'
                            'from_session,to_provider,to_session,liveness,dispatch_id,at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            (events[0] if events else None, None, d['task_key'], 'switched', why, frm and frm[0],
                             frm and frm[1], to[0], to[1], None, d['dispatch_id'], now))

    def _run_cwd(self, d, step):
        """Resume where that session registered its watches (Claude resolves --resume per project
        directory); fresh sessions and anything outside the task start in the task workspace."""
        task_path = d['task_path']
        if step['mode'] != 'resume' or not step.get('session'):
            return task_path
        row = self.db.execute("SELECT json_extract(data,'$.cwd') FROM watches WHERE json_extract(data,'$.session')=? AND "
                              "coalesce(json_extract(data,'$.provider'),'codex')=? AND "
                              "coalesce(json_extract(data,'$.kind'),'peer')!='task_inbox' "
                              "ORDER BY json_extract(data,'$.started') DESC LIMIT 1",
                              (step['session'], step['provider'])).fetchone()
        cwd = row[0] if row else None
        if cwd and Path(cwd).is_dir():
            top, path = os.path.normpath(task_path), os.path.normpath(cwd)
            if d['task_id'] is None or path == top or path.startswith(top.rstrip(os.sep) + os.sep):
                return cwd
        return task_path

    def _register_current(self, d, provider, session, now, source):
        task_id = d['task_id']
        if not task_id:
            return
        with self.rt.transaction(self.db):
            self.rt.registry.ensure_task(self.db, task_id, now)
            self.db.execute('UPDATE tasks SET current_provider=?,current_session=?,current_source=?,last_run_at=?,'
                            'updated_at=? WHERE task_id=?', (provider, session, source, now, now, task_id))
            self.rt.registry.audit(self.db, task_id, 'dispatch_current', None,
                                   {'current_provider': provider, 'current_session': session, 'source': source},
                                   'dispatcher', d['dispatch_id'], now)

    def _record_spawn_failure(self, d, step, run_id, events, exc, now):
        with self.rt.transaction(self.db):
            self.db.execute('INSERT OR IGNORE INTO consumer_runs (run_id,dispatch_id,task,task_path,provider,session,mode,'
                            'trigger,event_ids,state,started_at,ended_at,outcome,error_class,error_marker,route_reason) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (run_id, d['dispatch_id'], d['task_key'], d['task_path'], step['provider'], step.get('session'),
                             step['mode'], d['trigger'], json.dumps(events), 'ended', now, now, 'failed', 'spawn_failed',
                             type(exc).__name__, step.get('why')))
        self._after_failure(self._dispatch(d['dispatch_id']), self._run(run_id), 'spawn_failed', now)

    def _spawn(self, cmd, cwd, env, run_dir):
        child = subprocess.Popen([self.python, '-I', str(self.core.runner), '--run-dir', str(run_dir), '--', *cmd], cwd=cwd,
                                 env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        self.children[child.pid] = child
        return child.pid

    def _exit_info(self, run):
        try:
            return json.loads(Path(run['log_dir'], 'exit.json').read_text())
        except (OSError, ValueError, TypeError):
            return None

    def _tend(self, run, now):
        rt = self.rt
        exit_info = self._exit_info(run)
        if exit_info is None and run['pid'] and not run['pid_start'] and run['pid'] not in self.children:
            # `ps` failed at spawn time: never declare a possibly-running consumer dead because of that.
            started = self.pid_start(run['pid'])
            if started:
                with rt.transaction(self.db):
                    self.db.execute('UPDATE consumer_runs SET pid_start=? WHERE run_id=?', (started, run['run_id']))
                    self.db.execute('UPDATE consumer_leases SET pid_start=? WHERE run_id=?', (started, run['run_id']))
                run = self._run(run['run_id'])
        alive = exit_info is None and self.alive(run['pid'], run['pid_start'])
        if self.capture_session(run, now):
            run = self._run(run['run_id'])
        if alive:
            with rt.transaction(self.db):
                self.db.execute('UPDATE consumer_leases SET heartbeat_at=? WHERE task_key=? AND run_id=?',
                                (now, run['task'], run['run_id']))
            markers = int(self.render(run, now=now).get('markers') or 0)
            if markers != (run.get('health_markers') or 0):  # P38: the provider-health monitor reads it live
                with rt.transaction(self.db):
                    self.db.execute('UPDATE consumer_runs SET health_markers=? WHERE run_id=?', (markers, run['run_id']))
            try:
                self.progress(run, now)
            except Exception as exc:  # noqa: BLE001 - the progress line never affects the run
                rt._meta_error(self.db, 'dispatch_progress_error', exc)
            limit = self._cfg_value('max_run_seconds', 7200)
            if not run['stop_requested'] and now - run['started_at'] > limit:
                self._signal(run, signal.SIGTERM, 2, now)
            elif run['stop_requested'] and run['stop_signal_at'] and now - run['stop_signal_at'] > STOP_GRACE_SECONDS:
                self._signal(run, signal.SIGKILL, run['stop_requested'], now)
            elif not run['stop_requested'] and self._stuck_on_degraded(run, now):
                # P38: a long-lived run crawling on a degraded provider blocks every later event of the task (one
                # consumer per task): stop it; finalize moves its batch to the next plan step, a healthy provider.
                self._signal(run, signal.SIGTERM, STOP_DEGRADED, now)
                d = self._dispatch(run['dispatch_id'])
                self._switch_log(d, 'stuck_on_degraded', (run['provider'], run['session']), (None, None),
                                 json.loads(run['event_ids']), now)
            return
        child = self.children.pop(run['pid'], None)
        if child is not None:
            child.poll()
        self.finalize(run['run_id'], exit_info, now)

    def _stuck_on_degraded(self, run, now):
        """A long-lived task's run on a degraded provider, running for its class's slow threshold or more (P39:
        slow_run_minutes; the <owner key>-oncall task oncall_slow_run_minutes, 0 = never), whose dispatch has a later step on a
        provider that is not degraded."""
        try:
            config = self.config()
        except ValueError:
            return False
        if not config['dispatch']['provider_health']['enabled']:
            return False
        d = self._dispatch(run['dispatch_id']) if run.get('dispatch_id') else None
        slow = self.rt.worker_config.slow_run_seconds(config, d['task_id']) if d else None
        if d is None or d['run_id'] != run['run_id'] or not slow or now - (run['started_at'] or now) < slow:
            return False
        health = self._health(config) or {}
        if health.get(run['provider']) != DEGRADED:
            return False
        return any(step['provider'] != run['provider'] and health.get(step['provider']) != DEGRADED
                   for step in json.loads(d['plan'])[d['step'] + 1:])

    def capture_session(self, run, now=None):
        """Fresh Codex run: bind the thread id from `thread.started` as soon as it appears."""
        if run['provider'] != 'codex' or run['mode'] != 'fresh' or run['session'] or not run.get('log_dir'):
            return False
        session = self.core.parse_run_log('codex', _read_head(Path(run['log_dir'], 'events.jsonl')))['session']
        if not session or not self.rt.liveness.UUID_RE.fullmatch(session):
            return False
        self._bind_fresh_session(run, session, self.clock() if now is None else now)
        return True

    def _bind_fresh_session(self, run, session, now):
        rt = self.rt
        with rt.transaction(self.db):
            self.db.execute('UPDATE consumer_runs SET session=? WHERE run_id=? AND session IS NULL', (session, run['run_id']))
            self.db.execute('UPDATE consumer_leases SET session=? WHERE task_key=? AND run_id=?',
                            (session, run['task'], run['run_id']))
            d = self._dispatch(run['dispatch_id'])
            plan = json.loads(d['plan'])
            if d['step'] < len(plan):
                plan[d['step']]['session'] = session
                self.db.execute('UPDATE dispatches SET plan=? WHERE dispatch_id=?', (json.dumps(plan), d['dispatch_id']))
        events = json.loads(run['event_ids'])
        for wid in {r[0] for r in self.db.execute('SELECT watch_id FROM events WHERE id IN (%s)'
                                                  % ','.join('?' * len(events)), events)} if events else ():
            self.rebind_watch(wid, 'codex', session, 'dispatch_fresh')
        if d['trigger'] == 'launch':
            self._register_current(d, 'codex', session, now, 'launch')

    def _signal(self, run, sig, reason_code, now):
        if run['pid'] and self.alive(run['pid'], run['pid_start']):
            try:
                self.killer(run['pid'], sig)
            except OSError:
                pass
        with self.rt.transaction(self.db):
            self.db.execute('UPDATE consumer_runs SET stop_requested=?,stop_signal_at=? WHERE run_id=?',
                            (reason_code, now, run['run_id']))

    # ---- P1b: readable log + progress line ----
    def render(self, run, exit_info=None, final=False, now=None):
        """Best effort: bring readable.log up to date (and close it with one 结束 line when final)."""
        if not run.get('log_dir'):
            return {}
        try:
            if final:
                return self.rt.run_log.finish(run['log_dir'], exit_info, run['provider'], now=now,
                                              stderr_tail=_read_tail(Path(run['log_dir']) / 'stderr.log', 4096))
            return self.rt.run_log.update(run['log_dir'], run['provider'], now=now)
        except Exception as exc:  # noqa: BLE001 - logs are for humans; they never affect the run
            self.rt._meta_error(self.db, 'run_log_error', exc)
            return {}

    def _progress_row(self, dispatch_id):
        row = self.db.execute('SELECT * FROM run_progress WHERE dispatch_id=?', (dispatch_id,)).fetchone()
        return dict(row) if row else None

    def _progress_save(self, dispatch_id, run_id, now, **fields):
        with self.rt.transaction(self.db):
            self.db.execute('INSERT OR IGNORE INTO run_progress (dispatch_id, run_id, updated_at) VALUES (?,?,?)',
                            (dispatch_id, run_id, now))
            sets = ', '.join(f'{k}=?' for k in fields)
            self.db.execute(f'UPDATE run_progress SET run_id=?, updated_at=?{", " + sets if sets else ""} '
                            'WHERE dispatch_id=?', (run_id, now, *fields.values(), dispatch_id))

    def progress(self, run, now=None):
        """Edit the run card with "进行中 · 第 N 步 · …" (≤ 1/min per card, only on change, 429 backoff)."""
        rt = self.rt
        now = self.clock() if now is None else now
        if not self._cfg_value('progress_updates', True) or not self._cfg_value('notify_cards', True):
            return 'disabled'
        d = self._dispatch(run['dispatch_id']) if run.get('dispatch_id') else None
        if d is None or not d['card_ts'] or d['state'] != 'running' or d['run_id'] != run['run_id']:
            return 'no_card'
        state = rt.run_log.read_state(run['log_dir']) if run.get('log_dir') else {}
        summary = state.get('last_summary')
        if not summary:
            return 'nothing_yet'
        steps = int(state.get('steps') or 0)
        key = hashlib.sha256(f'{steps}\n{summary}'.encode()).hexdigest()[:16]
        row = self._progress_row(d['dispatch_id'])
        if row:
            if now < (row['backoff_until'] or 0):
                return 'backoff'
            if row['digest'] == key:
                return 'unchanged'
            if row['posted_at'] and now - row['posted_at'] < PROGRESS_INTERVAL_SECONDS:
                return 'throttled'
        line = self.core.progress_line(steps, summary, now - (run['started_at'] or now))
        text = self._card_text(d)
        try:
            rt.slack.api_call('chat.update', rt.api_identity(bot=True), json_body={
                'channel': d['card_channel'], 'ts': d['card_ts'], 'text': text,
                'blocks': self.core.run_card_blocks(text, line, d['dispatch_id'])})
        except Exception as exc:  # noqa: BLE001
            failures = (row or {}).get('failures') or 0
            delay, stage = progress_backoff(exc, failures)
            self._progress_save(d['dispatch_id'], run['run_id'], now, backoff_until=now + delay,
                                failures=failures + 1, stage=stage + ':' + rt.safe_error(exc))
            return stage
        self._progress_save(d['dispatch_id'], run['run_id'], now, posted_at=now, digest=key, step=steps,
                            backoff_until=None, failures=0, stage='posted')
        return 'posted'

    def _run_brief(self, d, status):
        """' · 共 N 步 · 用时 X' for a closed run card (final state on exit)."""
        if status not in ('success', 'failed', f'needs_{self.core.key}') or not d.get('run_id'):
            return ''
        if not self._cfg_value('progress_updates', True):
            return ''
        run = self._run(d['run_id'])
        if not run or not run.get('log_dir') or not run.get('started_at'):
            return ''
        state = self.rt.run_log.read_state(run['log_dir'])
        parts = [self.core.msg('dispatch.Dispatcher._run_brief.1', int=int(state['steps']))] if state.get('steps') else []
        parts.append(self.core.msg('dispatch.Dispatcher._run_brief.2') + self.rt.run_log.duration((run['ended_at'] or self.clock()) - run['started_at']))
        return ' · ' + ' · '.join(parts)

    # ---- outcome ----
    def finalize(self, run_id, exit_info=None, now=None):
        rt = self.rt
        now = self.clock() if now is None else now
        run = self._run(run_id)
        if run is None or run['state'] != 'running':
            return None
        exit_info = exit_info if exit_info is not None else self._exit_info(run)
        rendered = self.render(run, exit_info, final=True, now=now)
        run_dir = Path(run['log_dir'])
        last = _read_tail(run_dir / 'last.md', 16384) if (run_dir / 'last.md').exists() else None
        stderr = _read_tail(run_dir / 'stderr.log', 8192)
        info = self.core.parse_run_log(run['provider'], _read_tail(run_dir / 'events.jsonl'), stderr, last)
        # P38: overload / reconnect markers in the stream (counted while rendering) or the CLI's own stderr lines; the
        # same reconnect may be logged in both, so the larger count wins.
        markers = max(int(rendered.get('markers') or 0),
                      sum(1 for line in stderr.splitlines() if self.rt.run_log.HEALTH_MARKER_RE.search(line)))
        events = json.loads(run['event_ids'])
        statuses = {r[0]: r[1] for r in self.db.execute('SELECT id,status FROM events WHERE id IN (%s)'
                                                        % ','.join('?' * len(events)), events)} if events else {}
        handled = [e for e in events if statuses.get(e) == 'handled']
        unresolved = [e for e in events if statuses.get(e) != 'handled']
        exit_code = exit_info.get('exit_code') if exit_info else None
        cli_ok = info['cli_ok'] if info['cli_ok'] is not None else exit_code == 0
        cli_ok = bool(cli_ok and exit_code in (0, None) and exit_info is not None)
        error_class, marker = None, None
        if not unresolved and (events or cli_ok):
            outcome = 'success'  # even if the owner pressed stop meanwhile: the batch is settled (the pause stays)
        elif run['stop_requested'] == 1:
            outcome, error_class = 'stopped', 'stopped'
        elif run['stop_requested'] == 2:
            outcome, error_class = 'failed', 'run_timeout'
        elif run['stop_requested'] == STOP_DEGRADED:
            outcome, error_class = 'failed', 'provider_degraded'
        elif cli_ok:
            outcome, error_class = ('partial', 'task_unresolved') if handled else ('unresolved', 'task_unresolved')
        else:
            error_class, marker = run_failure(info, exit_info)
            outcome = 'failed'
        with rt.transaction(self.db):
            won = self.db.execute('UPDATE consumer_runs SET state=?,ended_at=?,exit_code=?,outcome=?,error_class=?,'
                                  'error_marker=?,handled=?,unresolved=?,health_markers=?,session=coalesce(session,?) '
                                  'WHERE run_id=? AND state=?',
                                  ('ended', now, exit_code, outcome, error_class, marker, len(handled), len(unresolved),
                                   markers, info['session'] if run['provider'] == 'codex' else None, run_id,
                                   'running')).rowcount
            if won:
                self.release_lease(run['task'], run_id)
        if not won:
            return None  # another process finalized this run first
        run = self._run(run_id)
        d = self._dispatch(run['dispatch_id'])
        if outcome == 'stopped':
            with rt.transaction(self.db):
                self.db.execute("UPDATE dispatches SET state='paused',outcome='stopped',updated_at=? WHERE dispatch_id=?",
                                (now, d['dispatch_id']))
            return outcome
        if outcome == 'success':
            if run['mode'] == 'fresh' and run['session'] and d['task_id']:
                # the owner: after a fallback the task stays on the provider that succeeded.
                source = 'launch' if d['trigger'] == 'launch' and not d['step'] else 'fallback'
                self._register_current(d, run['provider'], run['session'], now, source)
            remaining = self._held_events(d['dispatch_id'])
            with rt.transaction(self.db):
                if remaining:  # batch cap or events added while running: continue right away
                    self.db.execute("UPDATE dispatches SET state='waiting',next_at=?,retries=0,partial_retries=0,"
                                    'wait_since=NULL,updated_at=? WHERE dispatch_id=?', (now, now, d['dispatch_id']))
                    self._pin_plan(d, run)
                else:
                    self.db.execute("UPDATE dispatches SET state='done',outcome='success',wait_since=NULL,ended_at=?,"
                                    'updated_at=? WHERE dispatch_id=?', (now, now, d['dispatch_id']))
            self._promote_follow_up(d, now)
            self._card_status(d['dispatch_id'], 'success' if not remaining else 'running')
            if not remaining:
                try:  # P34: a success that left no owner / trigger (suggested next steps and stopped) -> card the owner.
                    rt.task_lifecycle.closure_guard(rt, self.db, run, info.get('final'), now, self.probe)
                except Exception as exc:  # noqa: BLE001 - the guard never affects the run outcome
                    rt._meta_error(self.db, 'closure_guard_error', exc)
            return outcome
        question = self.core.final_question(info['final']) if unresolved and cli_ok else None
        if question:
            self._ask_owner_from_run(d, run, question, now)
            with rt.transaction(self.db):
                self.db.execute(f"UPDATE dispatches SET state='needs_{self.core.key}',outcome='needs_{self.core.key}',ended_at=?,updated_at=? WHERE dispatch_id=?", (now, now, d['dispatch_id']))
            self._promote_follow_up(d, now)
            self._card_status(d['dispatch_id'], f'needs_{self.core.key}')
            return f'needs_{self.core.key}'
        if outcome == 'partial' and d['partial_retries'] < self._cfg_value('partial_retries', 1):
            with rt.transaction(self.db):
                self.db.execute("UPDATE dispatches SET state='waiting',partial_retries=partial_retries+1,next_at=?,"
                                'updated_at=? WHERE dispatch_id=?', (now, now, d['dispatch_id']))
                self._pin_plan(d, run)
            self._card_status(d['dispatch_id'], 'retrying')
            return outcome
        self._after_failure(d, run, error_class, now)
        return outcome

    def _pin_plan(self, d, run):
        """Inside a transaction: later attempts of this step resume the session this run used."""
        plan = json.loads(self._dispatch(d['dispatch_id'])['plan'])
        if d['step'] < len(plan) and run['session']:
            plan[d['step']].update(session=run['session'], mode='resume')
            if plan[d['step']].get('why') != 'cap_handoff':  # P38: a fresh-session reason no longer applies
                plan[d['step']].pop('why', None)
            self.db.execute('UPDATE dispatches SET plan=? WHERE dispatch_id=?', (json.dumps(plan), d['dispatch_id']))

    def _promote_follow_up(self, d, now):
        """A waiting follow-up dispatch of the same task becomes startable once the run ended."""
        with self.rt.transaction(self.db):
            self.db.execute("UPDATE dispatches SET next_at=? WHERE task_key=? AND state='waiting' AND dispatch_id!=?",
                            (now, d['task_key'], d['dispatch_id']))

    def _after_failure(self, d, run, error_class, now):
        rt = self.rt
        backoff = list(self._cfg_value('retry_backoff_seconds', [30, 120]))
        failures = json.loads(d['failures'] or '[]')
        failures.append({'provider': run['provider'] if run else None, 'session': run and run['session'],
                         'error_class': error_class, 'run_id': run and run['run_id'], 'at': now})
        plan = json.loads(d['plan'])
        with rt.transaction(self.db):
            self.db.execute('UPDATE dispatches SET failures=?,error_class=?,updated_at=? WHERE dispatch_id=?',
                            (json.dumps(failures), error_class, now, d['dispatch_id']))
        if error_class == 'session_live':
            return self._hand_back_live(d, run, now)
        if plan[d['step']].get('why') == 'auto_return' and d['step'] + 1 < len(plan) and error_class != 'task_unresolved':
            # P38: the return to the preferred provider is a probe; if the provider side failed (overload, auth, model,
            # spawn, ...), the batch goes straight back to the displaced session (the next step) — no retries, no card.
            with rt.transaction(self.db):
                self.db.execute("UPDATE dispatches SET state='waiting',step=step+1,retries=0,partial_retries=0,next_at=?,"
                                'updated_at=? WHERE dispatch_id=?', (now, now, d['dispatch_id']))
            self._card_status(d['dispatch_id'], 'fallback', error_class)
            return 'fallback'
        if error_class in RETRY_SAME and d['retries'] < len(backoff):
            with rt.transaction(self.db):
                self.db.execute("UPDATE dispatches SET state='waiting',retries=retries+1,next_at=?,updated_at=? "
                                'WHERE dispatch_id=?', (now + backoff[d['retries']], now, d['dispatch_id']))
                if run:
                    self._pin_plan(d, run)
            self._card_status(d['dispatch_id'], 'retrying', error_class)
            return 'retry_same'
        if error_class in RETRY_SAME + SWITCH_NOW and d['step'] + 1 < len(plan):
            with rt.transaction(self.db):
                self.db.execute("UPDATE dispatches SET state='waiting',step=step+1,retries=0,partial_retries=0,next_at=?,"
                                'updated_at=? WHERE dispatch_id=?', (now, now, d['dispatch_id']))
            self._card_status(d['dispatch_id'], 'fallback', error_class)
            return 'fallback'
        try:
            if self._provider_wait(self._dispatch(d['dispatch_id']), error_class, failures, now):
                return WAIT_REASON
        except Exception as exc:  # noqa: BLE001 - the wait never strands a batch: fail as before
            self.rt._meta_error(self.db, 'provider_wait_error', exc)
        return self._fail(self._dispatch(d['dispatch_id']), error_class, now)

    def _provider_wait(self, d, error_class, failures, now):
        """P44: instead of a failure card, wait for a provider to come back when the retries and fallbacks ran out on a
        provider-side class, or the run ended task_unresolved after such failures (at most unresolved_retries times):
        `waiting` with wait_reason provider_wait (kept out of `cycle` until a probe releases it), the plan from step 0
        with retries reset, next_at = its next probe, the run card saying so. False (fail as before) for any other class,
        with the feature off (provider_wait_hours 0 / probe_enabled false) and once the wait lasted provider_wait_hours.
        A dispatch that comes back after a release waits longer each time (its failures since the wait began)."""
        try:
            cfg = self.config()['dispatch']
        except ValueError:
            return False
        ok = self.db.execute("SELECT max(ended_at) FROM consumer_runs WHERE dispatch_id=? AND outcome='success'",
                             (d['dispatch_id'],)).fetchone()[0] or 0  # an earlier batch's failures do not count
        earlier = [x.get('error_class') for x in failures[:-1] if (x.get('at') or 0) > ok]
        if error_class == 'task_unresolved':
            waits = any(c in PROVIDER_WAIT for c in earlier) and earlier.count(error_class) < cfg['unresolved_retries']
        else:
            waits = error_class in PROVIDER_WAIT
        since = d['wait_since'] or now
        if not (waits and cfg['provider_wait_hours'] and cfg['probe_enabled']) or \
                now - since >= cfg['provider_wait_hours'] * 3600:
            return False
        tried = sum(1 for x in failures if (x.get('at') or 0) > since) if d['wait_since'] else 0
        scheduled = [(probe_state(self.db).get(p) or {}).get('next_at') or 0 for p in _plan_providers(d['plan'])]
        next_at = max(now + self._probe_interval(cfg, tried), min(scheduled or [0]))
        with self.rt.transaction(self.db):
            self.db.execute("UPDATE dispatches SET state='waiting',wait_reason=?,wait_since=?,step=0,retries=0,"
                            'partial_retries=0,next_at=?,updated_at=? WHERE dispatch_id=?',
                            (WAIT_REASON, since, next_at, now, d['dispatch_id']))
        self.rt._meta_note(self.db, 'provider_wait_note', f'{d["dispatch_id"]} {d["task_key"]} {error_class}')
        self._card_status(d['dispatch_id'], WAIT_REASON)
        return True

    def _hand_back_live(self, d, run, now):
        """The session turned out to have an active writer: deliver as today for a while."""
        events = self._held_events(d['dispatch_id'])
        until = now + 300
        for wid in {r[0] for r in self.db.execute('SELECT watch_id FROM events WHERE id IN (%s)'
                                                  % ','.join('?' * len(events)), events)} if events else ():
            with self.rt.transaction(self.db):
                w = self.rt.load(self.db, wid)
                w['dispatch_live_until'] = until
                self.rt.save(self.db, w)
        with self.rt.transaction(self.db):
            for eid in events:
                self.db.execute("UPDATE events SET status='pending',receipt=NULL WHERE id=? AND status='dispatched'", (eid,))
            self.db.execute("UPDATE dispatches SET state='done',outcome='session_live',ended_at=?,updated_at=? "
                            'WHERE dispatch_id=?', (now, now, d['dispatch_id']))
        self._card_status(d['dispatch_id'], 'handed_back')
        return 'handed_back'

    def _fail(self, d, error_class, now):
        with self.rt.transaction(self.db):
            self.db.execute("UPDATE dispatches SET state='failed',outcome='failed',error_class=?,wait_reason=NULL,"
                            'wait_since=NULL,ended_at=?,updated_at=? WHERE dispatch_id=?',
                            (error_class, now, now, d['dispatch_id']))
        self._card_status(d['dispatch_id'], 'failed', error_class)
        self._post_failure_card(self._dispatch(d['dispatch_id']), now)
        return 'failed'

    # ---- ask-<owner key> on behalf of a dispatched session ----
    def _ask_owner_from_run(self, d, run, question, now):
        session = run['session']
        if not session:
            return None
        task = self.task_for_key(d['task_key'])
        # P3.1 copy standard: the session's own question leads (its question paragraph first; ids / jargon dropped,
        # since it cannot be sent back to fix); "后台会话停下来问你" and the run link go to the footer.
        parts = [p.strip() for p in re.split(r'\n\s*\n', str(question or '')) if p.strip()]
        if len(parts) > 1 and self.core.question_hints.search(parts[-1]):
            parts = [parts[-1]] + parts[:-1]
        text = self.rt.buttons.scrub_copy('\n'.join(parts)) or self.core.msg('dispatch.Dispatcher._ask_owner_from_run.1')
        try:
            return self.core.ask_owner(self.rt)(self.db, self.db_path, session, run['provider'], task['path'] if task else d['task_path'],
                                    text[:1500], [self.core.msg('dispatch.Dispatcher._ask_owner_from_run.2'), self.core.msg('dispatch.Dispatcher._ask_owner_from_run.3')],
                                    trusted_owner=True, lint=False,
                                    footer=self.core.msg('dispatch.Dispatcher._ask_owner_from_run.4', dashboard_url=self.rt.DASHBOARD_URL, run_id=run['run_id']))
        except Exception as exc:  # noqa: BLE001 - fall back to the failure card
            self.rt._meta_error(self.db, f'dispatch_ask_{self.core.key}_error', exc)
            return None

    # ---- the owner actions (receiver CLIs) ----
    def stop(self, dispatch_id, actor, now=None):
        now = self.clock() if now is None else now
        d = self._dispatch(dispatch_id)
        if d is None:
            return {'ok': False, 'reason': 'unknown_dispatch'}
        killed = self.hold(d['task_key'], actor, f'{self.core.key}_stop', now, dispatch_id)
        self._card_status(dispatch_id, 'paused', actor=actor)
        if d['fail_card_ts']:
            self._close_failure_card(d, self.core.msg('dispatch.Dispatcher.stop.1', actor=actor, clock=self.core._clock(now), tz_name=self.core.tz_name))
        return {'ok': True, 'action': 'stop', 'dispatch_id': dispatch_id, 'task': d['task_key'], 'killed_runs': killed,
                'ephemeral': self.core.msg('dispatch.Dispatcher.stop.ephemeral')}

    def resume(self, dispatch_id, actor, now=None):
        now = self.clock() if now is None else now
        d = self._dispatch(dispatch_id)
        if d is None:
            return {'ok': False, 'reason': 'unknown_dispatch'}
        if not self.paused(d['task_key']):
            return {'ok': True, 'duplicate': True, 'action': 'resume', 'dispatch_id': dispatch_id,
                    'ephemeral': self.core.msg('dispatch.Dispatcher.resume.ephemeral')}
        keep = self.release(d['task_key'], actor, now, dispatch_id)
        self._card_status(dispatch_id, 'resumed', actor=actor)
        return {'ok': True, 'action': 'resume', 'dispatch_id': dispatch_id, 'restarted': keep,
                'ephemeral': self.core.msg('dispatch.Dispatcher.resume.ephemeral_2')}

    def hold(self, task_key, actor, reason, now, dispatch_id=None):
        """Pause the task (dispatch_pauses: new events queue in a `paused` dispatch, nothing starts) and SIGTERM its
        running runs (stop_requested 1, SIGKILL after STOP_GRACE_SECONDS); returns their run ids. the owner's ⏹ (`stop`) and
        P43 `takeover` (reason <owner key>_takeover, no dispatch id)."""
        killed = []
        with self.rt.transaction(self.db):
            self.db.execute('INSERT OR REPLACE INTO dispatch_pauses VALUES (?,?,?,?,?,?,?)',
                            (task_key, now, actor, reason, dispatch_id, None, None))
            self.db.execute(f"UPDATE dispatches SET state='paused',wait_reason=NULL,wait_since=NULL,updated_at=? WHERE task_key=? AND state IN ('waiting','failed','needs_{self.core.key}')", (now, task_key))
        for run in [dict(r) for r in self.db.execute("SELECT * FROM consumer_runs WHERE task=? AND state='running'",
                                                     (task_key,))]:
            self._signal(run, signal.SIGTERM, 1, now)
            killed.append(run['run_id'])
        return killed

    def release(self, task_key, actor, now, prefer=None, plan=None):
        """End the task's pause and re-queue its paused dispatches as one waiting dispatch (`prefer` when it is paused,
        else the oldest); returns its id. ▶️ (`resume`) and P43 `handback`, whose `plan` (step 0) resumes the session
        the owner worked in also for a launch / fallback dispatch, which `start` does not re-plan."""
        with self.rt.transaction(self.db):
            self.db.execute('UPDATE dispatch_pauses SET resumed_at=?,resumed_by=? WHERE task_key=?',
                            (now, actor, task_key))
            paused = [r[0] for r in self.db.execute("SELECT dispatch_id FROM dispatches WHERE task_key=? AND state='paused' "
                                                    'ORDER BY created_at', (task_key,))]
            keep = prefer if prefer in paused else (paused[0] if paused else None)
            for other in paused:
                if other != keep:
                    self.db.execute("UPDATE events SET receipt=? WHERE status='dispatched' AND receipt=?",
                                    ('dispatch:' + keep, 'dispatch:' + other))
                    self.db.execute("UPDATE dispatches SET state='superseded',updated_at=? WHERE dispatch_id=?", (now, other))
            if keep:
                self.db.execute("UPDATE dispatches SET state='waiting',retries=0,partial_retries=0,wait_reason=NULL,"
                                'wait_since=NULL,next_at=?,updated_at=? WHERE dispatch_id=?', (now, now, keep))
                if plan:
                    self.db.execute('UPDATE dispatches SET plan=?,step=0 WHERE dispatch_id=?', (json.dumps(plan), keep))
        return keep

    def retry(self, dispatch_id, mode, actor, now=None):
        now = self.clock() if now is None else now
        d = self._dispatch(dispatch_id)
        if d is None:
            return {'ok': False, 'reason': 'unknown_dispatch'}
        if d['state'] in ACTIVE:
            return {'ok': True, 'duplicate': True, 'dispatch_id': dispatch_id, 'ephemeral': self.core.msg('dispatch.Dispatcher.retry.ephemeral')}
        task = self.task_for_key(d['task_key'])
        if task is None:
            return {'ok': False, 'reason': 'unknown_task'}
        only = {'auto': None, 'codex': 'codex', 'claude': 'claude'}[mode]
        plan = self.plan_for(task, task['current'], self.config(), only=only)
        with self.rt.transaction(self.db):
            self.db.execute('UPDATE dispatch_pauses SET resumed_at=?,resumed_by=? WHERE task_key=? AND resumed_at IS NULL',
                            (now, actor, d['task_key']))
            for other in self.db.execute(f"SELECT dispatch_id FROM dispatches WHERE task_key=? AND dispatch_id!=? AND state IN ('paused','failed','needs_{self.core.key}')", (d['task_key'], dispatch_id)).fetchall():
                self.db.execute("UPDATE events SET receipt=? WHERE status='dispatched' AND receipt=?",
                                ('dispatch:' + dispatch_id, 'dispatch:' + other[0]))
                self.db.execute("UPDATE dispatches SET state='superseded',updated_at=? WHERE dispatch_id=?", (now, other[0]))
            self.db.execute(f"UPDATE dispatches SET state='waiting',reason='{self.core.key}_retry',plan=?,step=0,retries=0,partial_retries=0,failures='[]',error_class=NULL,wait_reason=NULL,wait_since=NULL,next_at=?,updated_at=? WHERE dispatch_id=?",
                            (json.dumps(plan), now, now, dispatch_id))
        label = {'auto': self.core.msg('dispatch.Dispatcher.retry.auto'), 'codex': self.core.msg('dispatch.Dispatcher.retry.codex'), 'claude': self.core.msg('dispatch.Dispatcher.retry.claude')}[mode]
        self._close_failure_card(d, self.core.msg('dispatch.Dispatcher.retry.1', label=label, actor=actor, clock=self.core._clock(now), tz_name=self.core.tz_name))
        self._card_status(dispatch_id, 'running')
        return {'ok': True, 'action': 'retry', 'mode': mode, 'dispatch_id': dispatch_id, 'plan': plan,
                'ephemeral': self.core.msg('dispatch.Dispatcher.retry.ephemeral_2')}

    # ---- launch-task (P2 intake) ----
    def launch(self, task, provider, prompt, *, important=False, category=None, now=None, reason='launch_task',
               inherit=False, persist=True):
        """A new dispatch that starts a fresh session of the task. P39 launch preferences: `persist` (explicit intent —
        launch-task, intake-launch, oncall-incident, a skill task the owner approved) records this launch's provider pin (an
        explicit provider only; `auto` clears it), important and category as the task's preference (dispatch_prefs, and
        dispatch_meta for older readers). `inherit` (a system re-dispatch — orphan / deadline card, routine, feedback
        drafting; always with persist=False) fills what the caller left open from it: `auto` -> the pinned provider, else
        the preferred one (`_preferred`); important / category -> the stored ones. A non-persisting launch never
        touches the stored preference."""
        rt = self.rt
        wc = rt.worker_config
        now = self.clock() if now is None else now
        config = self.config()
        prefs = self._prefs(task['key']) if inherit else {'provider': None, 'important': False, 'category': None}
        important, category = bool(important) or prefs['important'], category or prefs['category']
        if provider == 'auto':  # P38: healthy providers first; P39: the preferred provider leads
            preferred = self._preferred(task, config, {**prefs, 'category': category})
            order = self._by_health([preferred] + wc.fallback_after(config, preferred), self._health(config))
            plan = [self._step(p, 'fresh', None) for p in order]
            if plan[0]['provider'] != preferred:
                plan[0]['why'] = 'degraded_reroute'  # recorded on the run, so the task returns once it is healthy
        else:
            plan = [self._step(p, 'fresh', None) for p in [provider] + wc.fallback_after(config, provider)]
        if plan[0]['provider'] == 'claude':
            plan[0]['session'] = str(uuid.uuid4())
        dispatch_id = 'd' + uuid.uuid4().hex[:15]
        with rt.transaction(self.db):
            if persist and not inherit:  # an inherited launch never overwrites the stored preference
                self.db.execute('INSERT OR REPLACE INTO dispatch_meta VALUES (?,?,?,?)',
                                (task['key'], int(important), category, now))
                self.db.execute('INSERT OR REPLACE INTO dispatch_prefs (task_key,provider,important,category,source,'
                                'updated_at) VALUES (?,?,?,?,?,?)',
                                (task['key'], provider if provider != 'auto' else None, int(important), category,
                                 reason, now))
            self.db.execute('INSERT INTO dispatches (dispatch_id,task_key,task_id,task_path,state,trigger,reason,plan,next_at,'
                            'prompt,important,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (dispatch_id, task['key'], task['task_id'], task['path'], 'waiting', 'launch', reason,
                             json.dumps(plan), now, prompt, int(important), now, now))
        if plan[0]['session']:
            self._register_current(self._dispatch(dispatch_id), 'claude', plan[0]['session'], now, 'launch')
        return dispatch_id

    # ---- cards (the assistant, the owner's language, the owner-only channel) ----
    def _card_channel(self):
        return self.rt.buttons.load_config()['decision_channel']

    def _card_text(self, d):
        task = self.task_for_key(d['task_key']) or {}
        name = d['task_id'] or d['task_key']
        test = '🧪 *TEST* · ' if self.rt.registry.is_smoke_task(d['task_id']) else ''
        title = self.core.msg('dispatch.Dispatcher._card_text.1', escape=self.rt.buttons.escape(task.get('title'))) if task.get('title') else ''
        kinds = self._event_kinds(self._held_events(d['dispatch_id']) or json.loads(
            (self._run(d['run_id']) or {}).get('event_ids') or '[]'))
        trigger = (self.core.msg('dispatch.Dispatcher._card_text.2') + d['reason'].split(':', 1)[1] if str(d.get('reason') or '').startswith('routine:') else
                   self.core.msg('dispatch.Dispatcher._card_text.3') + d['reason'].split(':', 1)[1] if str(d.get('reason') or '').startswith('gap_skill:') else
                   self.core.msg('dispatch.Dispatcher._card_text.4') if str(d.get('reason') or '').startswith('deadline_wake:') else  # P42
                   self.core.msg('dispatch.Dispatcher._card_text.5') if d['trigger'] == 'launch' else self.core.trigger_summary(kinds))
        run = self._run(d['run_id']) if d['run_id'] else None
        if run:
            mode = self.core.msg('dispatch.Dispatcher._card_text.6') if run['mode'] == 'resume' else self.core.msg('dispatch.Dispatcher._card_text.7')  # P43: the session id is on the 会话 line
            why = self.core.why_label.get(run.get('route_reason') or '')
            who = (f"{PROVIDER_NAME.get(run['provider'], run['provider'])} · {run['model']} · {run['effort']} · {mode}"
                   + (self.core.msg('dispatch.Dispatcher._card_text.8', why=why) if why else ''))
            provider, session = run['provider'], run['session']
        else:
            step = json.loads(d['plan'])[min(d['step'], len(json.loads(d['plan'])) - 1)]
            who = PROVIDER_NAME.get(step['provider'], step['provider']) + (self.core.msg('dispatch.Dispatcher._card_text.9') if step['mode'] == 'resume' else self.core.msg('dispatch.Dispatcher._card_text.10'))
            provider, session = step['provider'], step.get('session')
        tag = self.core.session_tag(self.rt.DASHBOARD_URL, d['task_key'], provider, session, run and run['run_id'])
        return (self.core.msg('dispatch.Dispatcher._card_text.11', test=test, escape=self.rt.buttons.escape(name), title=title, trigger=trigger, who=who, tag=tag))

    def _status_line(self, d, status, error_class=None, actor=None):
        now = self.clock()
        run = self._run(d['run_id']) if d['run_id'] else None
        rid = f" · run `{run['run_id']}`" if run else ''
        reason = self.core.error_labels.get(error_class or d['error_class'] or '', error_class or '')
        lines = {
            'running': self.core.msg('dispatch.Dispatcher._status_line.1', rid=rid),
            'retrying': self.core.msg('dispatch.Dispatcher._status_line.2', reason=reason, rid=rid),
            'fallback': self.core.msg('dispatch.Dispatcher._status_line.3', reason=reason),
            'success': self.core.msg('dispatch.Dispatcher._status_line.4', clock=self.core._clock(now), rid=rid),
            f'needs_{self.core.key}': self.core.msg('dispatch.Dispatcher._status_line.5', clock=self.core._clock(now)),
            'failed': self.core.msg('dispatch.Dispatcher._status_line.6', reason=reason, clock=self.core._clock(now)),
            'paused': self.core.msg('dispatch.Dispatcher._status_line.7', actor=actor, clock=self.core._clock(now)) if actor else self.core.msg('dispatch.Dispatcher._status_line.8', clock=self.core._clock(now)),
            'resumed': self.core.msg('dispatch.Dispatcher._status_line.9', actor=actor, clock=self.core._clock(now)) if actor else self.core.msg('dispatch.Dispatcher._status_line.10', clock=self.core._clock(now)),
            'handed_back': self.core.msg('dispatch.Dispatcher._status_line.11', clock=self.core._clock(now)),
        }
        if actor == self.core.takeover_actor:  # P43: the owner's terminal (fsess take / back), not a Slack user to mention
            lines.update(paused=self.core.msg('dispatch.Dispatcher._status_line.12', clock=self.core._clock(now)),
                         resumed=self.core.msg('dispatch.Dispatcher._status_line.13', clock=self.core._clock(now)))
        if status == WAIT_REASON:  # P44: e.g. ⏳ 等 Claude / Codex 恢复 · 已等 3 小时 · 下次探测 17:30
            names = ' / '.join(PROVIDER_NAME.get(p, p) for p in _plan_providers(d['plan']))
            return (self.core.msg('dispatch.Dispatcher._status_line.14', names=names, waited=self.core._waited(now - (d['wait_since'] or now)), clock=self.core._clock(d['next_at'] or now)))
        return lines.get(status, status) + self._run_brief(d, status)

    def _card_status(self, dispatch_id, status, error_class=None, actor=None):
        """Post the per-dispatch notification card once, then edit its status line (best effort)."""
        if not self._cfg_value('notify_cards', True):
            return None
        d = self._dispatch(dispatch_id)
        if d is None:
            return None
        if status in CONTROL_QUIET and self._control_task(d):
            return None  # Phase 2 C1: a control run's answer is its result; only a failure / pause / wait gets a card
        rt = self.rt
        text = self._card_text(d)
        line = self._status_line(d, status, error_class, actor)
        paused = status == 'paused'
        closed = status in ('success', 'handed_back')
        blocks = self.core.run_card_blocks(text, line, dispatch_id, paused=paused, closed=closed)
        try:
            token = rt.api_identity(bot=True)
            if d['card_ts']:
                rt.slack.api_call('chat.update', token, json_body={'channel': d['card_channel'], 'ts': d['card_ts'],
                                                                   'text': text, 'blocks': blocks})
                stage = 'updated:' + status
            else:
                channel = self._card_channel()
                # P38 (F49): Slack-verified private destination (cached); refused = card_failed, retried next status.
                rt.buttons.require_private_destination(rt, self.db, channel, 'dispatch_card')
                sent = rt.slack.api_call('chat.postMessage', token, json_body={
                    'channel': channel, 'text': text, 'blocks': blocks, 'unfurl_links': False, 'unfurl_media': False,
                    'metadata': rt.agent_metadata('dispatch_card', None, dispatch_id=dispatch_id, task=d['task_key'])})
                with rt.transaction(self.db):
                    self.db.execute('UPDATE dispatches SET card_channel=?,card_ts=? WHERE dispatch_id=?',
                                    (channel, sent['ts'], dispatch_id))
                    rt._ledger(self.db, channel, sent['ts'], 'dispatch_card', dispatch_id, None)
                stage = 'posted:' + status
        except Exception as exc:  # noqa: BLE001 - a card failure never affects the run
            stage = 'card_failed:' + rt.safe_error(exc)
        if not stage.startswith('card_failed'):
            # Any card edit counts against the progress line's once-a-minute budget.
            self._progress_save(dispatch_id, d['run_id'], self.clock(), posted_at=self.clock())
        with rt.transaction(self.db):
            self.db.execute('UPDATE dispatches SET card_stage=? WHERE dispatch_id=?', (stage, dispatch_id))
        return stage

    def _failure_text(self, d):
        rt = self.rt
        failures = json.loads(d['failures'] or '[]')
        name = d['task_id'] or d['task_key']
        test = '🧪 *TEST* · ' if rt.registry.is_smoke_task(d['task_id']) else ''
        providers = {f['provider'] for f in failures if f.get('provider')}
        # P3.1 copy standard: one plain sentence (what happened, what the owner is asked), then why, then what is kept.
        task = f'`{rt.buttons.escape(name)}`'
        if d['error_class'] == 'task_unresolved':
            first = self.core.msg('dispatch.Dispatcher._failure_text.1', task=task)
        elif len(providers) > 1:
            first = self.core.msg('dispatch.Dispatcher._failure_text.2', task=task)
        elif d['error_class'] in NO_SWITCH:
            first = self.core.msg('dispatch.Dispatcher._failure_text.3', task=task)
        else:
            first = self.core.msg('dispatch.Dispatcher._failure_text.4', task=task)
        detail = self.core.msg('dispatch.Dispatcher._failure_text.5').join(f"{PROVIDER_NAME.get(f['provider'], f['provider'] or '?')} {self.core.error_labels.get(f['error_class'], f['error_class'])}"
                          for f in failures[-4:]) or self.core.error_labels.get(d['error_class'] or '', '')
        held = len(self._held_events(d['dispatch_id']))
        return (f'{test}<@{self.core.owner_user(rt)}> {first}\n' + (self.core.msg('dispatch.Dispatcher._failure_text.6', detail=detail) if detail else '') +
                self.core.msg('dispatch.Dispatcher._failure_text.7', held=held))

    def _post_failure_card(self, d, now):
        rt = self.rt
        text = self._failure_text(d)
        try:
            token = rt.api_identity(bot=True)
            channel = d['card_channel'] or self._card_channel()
            # P38 (F49): the run card's stored channel (or the configured one) must still be private according to Slack.
            rt.buttons.require_private_destination(rt, self.db, channel, 'dispatch_failure_card',
                                                   configured=not d['card_channel'])
            sent = rt.slack.api_call('chat.postMessage', token, json_body={
                'channel': channel, 'text': text, 'blocks': self.core.failure_card_blocks(text, d['dispatch_id']),
                'unfurl_links': False, 'unfurl_media': False,
                'metadata': rt.agent_metadata('dispatch_failure_card', None, dispatch_id=d['dispatch_id'],
                                              task=d['task_key'])})
            with rt.transaction(self.db):
                self.db.execute('UPDATE dispatches SET fail_card_ts=?,card_channel=coalesce(card_channel,?) '
                                'WHERE dispatch_id=?', (sent['ts'], channel, d['dispatch_id']))
                rt._ledger(self.db, channel, sent['ts'], 'dispatch_failure_card', d['dispatch_id'], None)
        except Exception as exc:  # noqa: BLE001
            with rt.transaction(self.db):
                self.db.execute('UPDATE dispatches SET card_stage=? WHERE dispatch_id=?',
                                ('failure_card_failed:' + rt.safe_error(exc), d['dispatch_id']))

    def _close_failure_card(self, d, line):
        rt = self.rt
        if not d['fail_card_ts']:
            return
        try:
            text = self._failure_text(self._dispatch(d['dispatch_id']))
            rt.slack.api_call('chat.update', rt.api_identity(bot=True), json_body={
                'channel': d['card_channel'], 'ts': d['fail_card_ts'], 'text': text,
                'blocks': self.core.failure_card_blocks(text, d['dispatch_id'], closed_line=line)})
        except Exception as exc:  # noqa: BLE001
            rt._meta_error(self.db, 'dispatch_card_error', exc)

    # ---- agent-to-agent circuit breaker ----
    def breaker_check(self, from_key, to_key, now=None):
        """('ok'|'paused'|'tripped'|'stopped', breaker_id, round_trips) BEFORE recording this message."""
        now = self.clock() if now is None else now
        a, b = pair_key(from_key, to_key)
        row = self.db.execute("SELECT * FROM agent_breakers WHERE key_a=? AND key_b=? ORDER BY tripped_at DESC LIMIT 1",
                              (a, b)).fetchone()
        if row and row['state'] == 'paused':
            return 'paused', row['id'], row['round_trips']
        if row and row['state'] == 'stopped' and now - (row['decided_at'] or row['tripped_at']) < 86400:
            return 'stopped', row['id'], row['round_trips']
        since = now - 3600
        if row and row['state'] == 'continued' and row['reset_at']:
            since = max(since, row['reset_at'])
        n_ft = self.db.execute('SELECT count(*) FROM agent_messages WHERE from_key=? AND to_key=? AND created_at>?',
                               (from_key, to_key, since)).fetchone()[0] + 1
        n_tf = self.db.execute('SELECT count(*) FROM agent_messages WHERE from_key=? AND to_key=? AND created_at>?',
                               (to_key, from_key, since)).fetchone()[0]
        trips = min(n_ft, n_tf)
        limit = self.config()['circuit_breaker']['round_trips_per_hour']
        if trips > limit:
            return 'tripped', None, trips
        return 'ok', None, trips

    def trip_breaker(self, from_key, to_key, trips, now=None):
        rt = self.rt
        now = self.clock() if now is None else now
        a, b = pair_key(from_key, to_key)
        breaker_id = 'b' + uuid.uuid4().hex[:15]
        with rt.transaction(self.db):
            self.db.execute('INSERT INTO agent_breakers (id,key_a,key_b,state,round_trips,tripped_at) VALUES (?,?,?,?,?,?)',
                            (breaker_id, a, b, 'paused', trips, now))
        self._post_breaker_card(breaker_id, a, b, trips)
        return breaker_id

    def _post_breaker_card(self, breaker_id, a, b, trips):
        rt = self.rt
        limit = self.config()['circuit_breaker']['round_trips_per_hour']
        text = (self.core.msg('dispatch.Dispatcher._post_breaker_card.1', owner_user=self.core.owner_user(rt), escape=rt.buttons.escape(a), escape2=rt.buttons.escape(b), trips=trips, limit=limit))
        try:
            channel = self._card_channel()
            rt.buttons.require_private_destination(rt, self.db, channel, 'breaker_card')  # P38 (F49): fail closed
            sent = rt.slack.api_call('chat.postMessage', rt.api_identity(bot=True), json_body={
                'channel': channel, 'text': text, 'blocks': self.core.breaker_card_blocks(text, breaker_id),
                'unfurl_links': False, 'unfurl_media': False,
                'metadata': rt.agent_metadata('breaker_card', None, breaker_id=breaker_id)})
            with rt.transaction(self.db):
                self.db.execute("UPDATE agent_breakers SET card_channel=?,card_ts=?,card_stage='posted' WHERE id=?",
                                (channel, sent['ts'], breaker_id))
                rt._ledger(self.db, channel, sent['ts'], 'breaker_card', breaker_id, None)
        except Exception as exc:  # noqa: BLE001
            with rt.transaction(self.db):
                self.db.execute('UPDATE agent_breakers SET card_stage=? WHERE id=?',
                                ('card_failed:' + rt.safe_error(exc), breaker_id))

    def repost_refused_cards(self, now=None):
        """P38 (F49) supervisor step (daemon health): a breaker or failure card the private-destination guard refused
        (nothing was posted) is posted once the destination check passes — without it a paused pair or a failed run has
        no decision path (the click handler needs the stored card channel / ts). The cached verdict is peeked first, so a
        channel that still refuses costs no post attempt and no new refusal record. Only rows still refused and still
        undecided are taken, each CLAIMED (a `…reposting:<time>` stage) right before its post, so a post whose
        bookkeeping then fails is never posted again by the next sweep; a claim older than REPOST_CLAIM_SECONDS (a crash
        before the post) is taken again rather than leaving the card missing for good."""
        rt = self.rt
        now = self.clock() if now is None else now
        refused = 'card_failed:' + rt.buttons.DestinationRefused.__name__
        try:
            decisions = rt.buttons.load_config()
        except ValueError:
            return []  # malformed decisions config: every post refuses anyway

        def passes(channel, configured=True):
            if configured and not rt.buttons.private_card_destination(decisions, channel):
                return False
            return rt.buttons.destination_check(rt, self.db, channel)['verdict'] in rt.buttons.PRIVATE_VERDICTS
        def due(stage, prefix):
            if stage == prefix + refused:
                return True
            claim = str(stage or '')
            if not claim.startswith(prefix + REPOSTING):
                return False
            try:
                return float(claim[len(prefix + REPOSTING):]) < now - REPOST_CLAIM_SECONDS
            except ValueError:
                return True

        def claimed(table, key, ident, stage, prefix, extra):
            with rt.transaction(self.db):
                return self.db.execute(f'UPDATE {table} SET card_stage=? WHERE {key}=? AND card_stage=? AND {extra}',
                                       (f'{prefix}{REPOSTING}{now:.0f}', ident, stage)).rowcount == 1
        out = []
        for breaker_id, stage in [tuple(r) for r in self.db.execute(
                "SELECT id, card_stage FROM agent_breakers WHERE state='paused' AND card_ts IS NULL AND (card_stage=? OR "
                'card_stage LIKE ?)', (refused, REPOSTING + '%'))]:
            if not due(stage, ''):
                continue
            if not passes(decisions['decision_channel']):
                break  # same channel for every breaker card
            if not claimed('agent_breakers', 'id', breaker_id, stage, '', "state='paused' AND card_ts IS NULL"):
                continue
            row = self.db.execute('SELECT * FROM agent_breakers WHERE id=?', (breaker_id,)).fetchone()
            if row is not None:
                self._post_breaker_card(row['id'], row['key_a'], row['key_b'], row['round_trips'])
                out.append({'breaker_id': breaker_id, 'stage': self.db.execute(
                    'SELECT card_stage FROM agent_breakers WHERE id=?', (breaker_id,)).fetchone()[0]})
        for dispatch_id in [r[0] for r in self.db.execute(
                "SELECT dispatch_id FROM dispatches WHERE state='failed' AND fail_card_ts IS NULL AND (card_stage=? OR "
                'card_stage LIKE ?)', ('failure_' + refused, 'failure_' + REPOSTING + '%'))]:
            d = self._dispatch(dispatch_id)
            if not (d and d['state'] == 'failed' and not d['fail_card_ts'] and due(d['card_stage'], 'failure_')):
                continue
            if not passes(d['card_channel'] or decisions['decision_channel'], configured=not d['card_channel']):
                continue
            if not claimed('dispatches', 'dispatch_id', dispatch_id, d['card_stage'], 'failure_',
                           "state='failed' AND fail_card_ts IS NULL"):
                continue
            self._post_failure_card(self._dispatch(dispatch_id), now)
            if self._dispatch(dispatch_id)['fail_card_ts']:  # posted: the refusal stage would be stale
                with rt.transaction(self.db):
                    self.db.execute('UPDATE dispatches SET card_stage=? WHERE dispatch_id=?',
                                    ('failure_card_reposted', dispatch_id))
            out.append({'dispatch_id': dispatch_id, 'stage': self._dispatch(dispatch_id)['card_stage']})
        return out

    def breaker_decide(self, breaker_id, choice, actor, now=None):
        rt = self.rt
        now = self.clock() if now is None else now
        row = self.db.execute('SELECT * FROM agent_breakers WHERE id=?', (breaker_id,)).fetchone()
        if row is None:
            return {'ok': False, 'reason': 'unknown_breaker'}
        if row['state'] != 'paused':
            return {'ok': True, 'duplicate': True, 'breaker_id': breaker_id, 'state': row['state'],
                    'ephemeral': self.core.msg('dispatch.Dispatcher.breaker_decide.ephemeral')}
        held = [r[0] for r in self.db.execute("SELECT id FROM events WHERE status='dispatched' AND receipt=?",
                                              ('breaker:' + breaker_id,))]
        with rt.transaction(self.db):
            if choice == 'continue':
                self.db.execute("UPDATE agent_breakers SET state='continued',decided_by=?,decided_at=?,reset_at=? WHERE id=?",
                                (actor, now, now, breaker_id))
                for eid in held:
                    self.db.execute("UPDATE events SET status='pending',receipt=NULL WHERE id=? AND status='dispatched'",
                                    (eid,))
            else:
                self.db.execute("UPDATE agent_breakers SET state='stopped',decided_by=?,decided_at=? WHERE id=?",
                                (actor, now, breaker_id))
                for eid in held:
                    self.db.execute("UPDATE events SET status='handled',disposition='breaker_stopped' WHERE id=? AND "
                                    "status='dispatched'", (eid,))
        line = (self.core.msg('dispatch.Dispatcher.breaker_decide.1', actor=actor, clock=self.core._clock(now), tz_name=self.core.tz_name) if choice == 'continue' else
                self.core.msg('dispatch.Dispatcher.breaker_decide.2', actor=actor, clock=self.core._clock(now), tz_name=self.core.tz_name))
        if row['card_ts']:
            try:
                rt.slack.api_call('chat.update', rt.api_identity(bot=True), json_body={
                    'channel': row['card_channel'], 'ts': row['card_ts'], 'text': line,
                    'blocks': self.core.breaker_card_blocks(line, breaker_id, closed_line=line)})
            except Exception as exc:  # noqa: BLE001
                rt._meta_error(self.db, 'breaker_card_error', exc)
        return {'ok': True, 'breaker_id': breaker_id, 'choice': choice, 'released' if choice == 'continue' else 'dropped': held,
                'ephemeral': self.core.msg('dispatch.Dispatcher.breaker_decide.3') if choice == 'continue' else self.core.msg('dispatch.Dispatcher.breaker_decide.4')}

    # ---- status ----
    def status(self, task_key=None, limit=20):
        where, args = ('WHERE task_key=?', (task_key,)) if task_key else ('', ())
        dispatches = [dict(r) for r in self.db.execute(f'SELECT * FROM dispatches {where} ORDER BY created_at DESC LIMIT ?',
                                                       (*args, limit))]
        for d in dispatches:
            d.pop('prompt', None)
            d['held_events'] = len(self._held_events(d['dispatch_id']))
        runs = [dict(r) for r in self.db.execute(
            'SELECT * FROM consumer_runs ' + ('WHERE task=? ' if task_key else '') + 'ORDER BY started_at DESC LIMIT ?',
            (*args, limit))]
        leases = [dict(r) for r in self.db.execute('SELECT * FROM consumer_leases' + (' WHERE task_key=?' if task_key else ''),
                                                   args)]
        pauses = [dict(r) for r in self.db.execute('SELECT * FROM dispatch_pauses WHERE resumed_at IS NULL' +
                                                   (' AND task_key=?' if task_key else ''), args)]
        try:  # P39: the reserved intake lane's occupancy
            capacity = self.capacity(self.config())
        except ValueError:
            capacity = None
        return {'ok': True, 'running': self.running_count(), 'capacity': capacity, 'dispatches': dispatches, 'runs': runs,
                'leases': leases, 'paused': pauses, 'provider_wait': provider_wait_overview(self.db, self.clock())}


def provider_wait_overview(db, now, limit=20):
    """P44 read model (dispatch-status, health, the dashboard): per provider the last probe (time, result, class,
    model), its consecutive failures, the next probe and how many dispatches wait for it; the waiting dispatches and the
    oldest wait. Ids, times and classes only."""
    probes = probe_state(db)
    columns = {r[1] for r in db.execute('PRAGMA table_info(dispatches)')}
    rows = [dict(r) for r in db.execute("SELECT dispatch_id, task_key, plan, wait_since, next_at FROM dispatches WHERE "
                                        "state='waiting' AND wait_reason=? ORDER BY wait_since", (WAIT_REASON,))
            ] if 'wait_reason' in columns else []
    providers = {}
    for provider in PROVIDER_NAME:
        p = probes.get(provider) or {}
        providers[provider] = {'last_probe_at': p.get('at'), 'result': p.get('result'), 'error_class': p.get('error_class'),
                               'model': p.get('model'), 'fails': p.get('fails', 0), 'next_probe_at': p.get('next_at'),
                               'probing': bool(p.get('running')),
                               'waiting': sum(1 for r in rows if provider in _plan_providers(r['plan']))}
    oldest = min((r['wait_since'] for r in rows if r['wait_since']), default=None)
    return {'providers': providers, 'waiting': len(rows), 'oldest_wait_since': oldest,
            'oldest_wait_hours': round((now - oldest) / 3600, 1) if oldest else None,
            'dispatches': [{'dispatch_id': r['dispatch_id'], 'task_key': r['task_key'],
                            'providers': _plan_providers(r['plan']), 'wait_since': r['wait_since'],
                            'next_probe_at': r['next_at']} for r in rows[:limit]]}


def stranded_overview(db, now, limit=20):
    """P38 C1 read model (dashboard / CLI): stranded-delivery counts and the latest rows. Ids and states only."""
    if 'stranded_deliveries' not in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return {'counts': {}, 'recent': []}
    counts = {r[0]: r[1] for r in db.execute('SELECT state, count(*) FROM stranded_deliveries WHERE updated_at>=? '
                                             'GROUP BY state', (now - 86400,))}
    recent = [dict(r) for r in db.execute('SELECT event_id, provider, status, task_key, state, reason, dispatch_id, '
                                          'first_seen, updated_at FROM stranded_deliveries ORDER BY updated_at DESC '
                                          'LIMIT ?', (limit,))]
    return {'counts': counts, 'recent': recent}


def event_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class DispatchCore:
    """The owner-bound half of the dispatcher: the owner's time zone, the card words, labels and questions in
    the owner's language, the runtime's environment names and the deployment's hooks (the provider
    adapters, the runner, the owner's shell exports); the Dispatcher holds one."""

    def __init__(self, language, *, tz, tz_name, owner_key, owner_name, run_id_env, task_env, hops_env, run_context_env, probe_dir_prefix, runner, build_command, parse_run_log, prompt_layout, lean_claude, claude_settings, rc_exports, owner_user, ask_owner):
        self.language = language
        self.tz = tz
        self.tz_name = tz_name
        self.run_id_env = run_id_env
        self.task_env = task_env
        self.hops_env = hops_env
        self.run_context_env = run_context_env
        self.probe_dir_prefix = probe_dir_prefix
        self.runner = runner
        self.build_command = build_command
        self.parse_run_log = parse_run_log
        self.prompt_layout = prompt_layout
        self.lean_claude = lean_claude
        self.claude_settings = claude_settings
        self.rc_exports = rc_exports
        self.owner_user = owner_user
        self.ask_owner = ask_owner
        self.key, self.name = owner_key, owner_name
        self.holding = ('paused', 'failed', f'needs_{self.key}')
        self.strip_env_exact = ('CODEX_THREAD_ID', 'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_EFFORT', 'CLAUDE_ENV_FILE',
                                self.run_id_env, self.task_env, self.hops_env, self.run_context_env)
        self.why_label = {'long_lived': self.msg('dispatch.why_label.long_lived'), 'context_cap': self.msg('dispatch.why_label.context_cap'),
                          'cap_handoff': self.msg('dispatch.why_label.cap_handoff'), 'auto_return': self.msg('dispatch.why_label.auto_return'),
                          'degraded_reroute': self.msg('dispatch.why_label.degraded_reroute')}
        self.takeover_actor = f'{self.key}-terminal'  # P43: `takeover` / `handback` (fsess take / back) in the owner's own terminal
        self.takeover_reason = f'{self.key}_takeover'  # dispatch_pauses.reason of a takeover hold (the owner's ⏹ is '<owner key>_stop')
        self.error_labels = {
                 'transient': self.msg('dispatch.error_labels.transient'), 'rate_limited': self.msg('dispatch.error_labels.rate_limited'), 'quota': self.msg('dispatch.error_labels.quota'),
                 'auth': self.msg('dispatch.error_labels.auth'), 'model_unsupported': self.msg('dispatch.error_labels.model_unsupported'), 'session_missing': self.msg('dispatch.error_labels.session_missing'),
                 'session_live': self.msg('dispatch.error_labels.session_live'), 'unknown': self.msg('dispatch.error_labels.unknown'), 'task_unresolved': self.msg('dispatch.error_labels.task_unresolved'),
                 'run_timeout': self.msg('dispatch.error_labels.run_timeout'), 'spawn_failed': self.msg('dispatch.error_labels.spawn_failed'), 'stopped': self.msg('dispatch.error_labels.stopped'), 'crashed': self.msg('dispatch.error_labels.crashed'),
                 'provider_degraded': self.msg('dispatch.error_labels.provider_degraded'),
             }
        self.question_hints = re.compile(self.pattern('dispatch.question_hints.1'), re.I)
        self.trigger_label = {'slack_message': self.msg('dispatch.trigger_label.slack_message'), 'decision_answer': self.msg('dispatch.trigger_label.decision_answer'), 'ask_answer': self.msg('dispatch.trigger_label.ask_answer'),
                              'agent_message': self.msg('dispatch.trigger_label.agent_message'), 'note': self.msg('dispatch.trigger_label.note'), 'routine': self.msg('dispatch.trigger_label.routine'), 'launch': self.msg('dispatch.trigger_label.launch')}

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def final_question(self, text, limit=1200):
        """The question a run ended on, if its final message asks the owner something (deterministic)."""
        text = str(text or '').strip()
        if not text:
            return None
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
        tail = '\n\n'.join(paragraphs[-2:]) if paragraphs else text
        if not self.question_hints.search(tail):
            return None
        return tail[-limit:]

    def _label(self, text, n):
        text = re.sub(r'<[@!#][^>]*>', '', ' '.join(str(text or '').split()))
        return (text[:28] + '…') if len(text) > 29 else (text or self.msg('dispatch._label.1', n=n))

    def hook_question(self, tool_name, tool_input):
        """AskUserQuestion / ExitPlanMode tool input -> (Chinese card question, option specs)."""
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        if tool_name == 'ExitPlanMode':
            plan = str(tool_input.get('plan') or '').strip()
            body = plan[:1100] + ('…' if len(plan) > 1100 else '') if plan else self.msg('dispatch.hook_question.1')
            return self.msg('dispatch.hook_question.2') + body, [self.msg('dispatch.hook_question.3'),
                                                                        self.msg('dispatch.hook_question.4')]
        questions = [q for q in tool_input.get('questions') or [] if isinstance(q, dict) and q.get('question')]
        if not questions:
            return self.msg('dispatch.hook_question.5'), []
        # P3.1 copy standard: the question itself leads; "后台会话在终端里提问" is in the card footer.
        lines = []
        for q in questions[:4]:
            lines.append(str(q['question']) + (self.msg('dispatch.hook_question.6') + str(q.get('header')) + self.msg('dispatch.hook_question.7') if q.get('header') else ''))
        options, labels = [], set()
        first = questions[0]
        for n, option in enumerate(first.get('options') or [], 1):
            label = self._label(option.get('label') if isinstance(option, dict) else option, n)
            if label in labels:
                continue
            labels.add(label)
            options.append(f'o{n}={label}')
        if len(options) > 4:
            options = options[:3] + [self.msg('dispatch.hook_question.8')]
        if len(options) < 2:
            options = [self.msg('dispatch.hook_question.9'), self.msg('dispatch.hook_question.10')]
        if len(questions) > 1:
            lines.append(self.msg('dispatch.hook_question.11'))
        return '\n'.join(lines)[:1500], options

    def strip_agent_env(self, env):
        return {k: v for k, v in env.items() if k not in self.strip_env_exact and not k.startswith(STRIP_ENV_PREFIX)}

    def _clock(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz).strftime('%H:%M')

    def trigger_summary(self, kinds):
        counts = {}
        for kind in kinds:
            counts[kind] = counts.get(kind, 0) + 1
        if not counts:
            return self.msg('dispatch.trigger_summary.1')
        return self.msg('dispatch.trigger_summary.2').join(self.msg('dispatch.trigger_summary.4', get=self.trigger_label.get(k, self.msg('dispatch.trigger_summary.3')), n=n) for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))

    def session_tag(self, dashboard, task_key, provider, session, run_id=None, link=True):
        """P43 card line: the session working on the task, its latest run page and the terminal takeover command, e.g.
        `会话 codex 01a16cde-fe49 · <…/runs/<run>|看板> · 终端接手：fsess take <task>`. Shows what is known: no id yet (a fresh
        run before the CLI reported it) reads `（id 稍后显示）`; no provider (a footer already names it) leaves it out."""
        sid = str(session or '')[:SESSION_ID_CHARS]
        head = self.msg('dispatch.session_tag.1') + (f' {provider}' if provider else '') + (f' {sid}' if sid else self.msg('dispatch.session_tag.2'))
        url = f'{dashboard}/runs/{run_id}' if run_id else f'{dashboard}/runs?task={task_key}'
        target = sid if str(task_key or '').startswith('session:') else task_key  # takeover takes a unique session prefix
        return ' · '.join(x for x in (head, link and self.msg('dispatch.session_tag.3', url=url), target and self.msg('dispatch.session_tag.4', target=target)) if x)

    def run_card_blocks(self, text, status_line, dispatch_id, paused=False, closed=False):
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}},
                  {'type': 'context', 'block_id': 'sf_dispatch_status',
                   'elements': [{'type': 'mrkdwn', 'text': status_line}]}]
        if not closed:
            first = _button(self.msg('dispatch.run_card_blocks.1'), 'resume', dispatch_id, 'primary') if paused else \
                _button(self.msg('dispatch.run_card_blocks.2'), 'stop', dispatch_id, 'danger')
            blocks.append({'type': 'actions', 'block_id': 'sf_dispatch_actions', 'elements': [
                first, _button(self.msg('dispatch.run_card_blocks.3'), 'iterm', dispatch_id), _button(self.msg('dispatch.run_card_blocks.4'), 'view', dispatch_id)]})
        return blocks

    def failure_card_blocks(self, text, dispatch_id, closed_line=None):
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]
        if closed_line:
            return blocks + [{'type': 'context', 'block_id': 'sf_dispatch_status',
                              'elements': [{'type': 'mrkdwn', 'text': closed_line}]}]
        return blocks + [{'type': 'actions', 'block_id': 'sf_dispatch_retry', 'elements': [
            _button(self.msg('dispatch.failure_card_blocks.1'), 'retry', dispatch_id, 'primary'), _button(self.msg('dispatch.failure_card_blocks.2'), 'retry_codex', dispatch_id),
            _button(self.msg('dispatch.failure_card_blocks.3'), 'retry_claude', dispatch_id), _button(self.msg('dispatch.failure_card_blocks.4'), 'stop', dispatch_id, 'danger')]}]

    def breaker_card_blocks(self, text, breaker_id, closed_line=None):
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]
        if closed_line:
            return blocks + [{'type': 'context', 'block_id': 'sf_breaker_status',
                              'elements': [{'type': 'mrkdwn', 'text': closed_line}]}]
        return blocks + [{'type': 'actions', 'block_id': 'sf_breaker_actions', 'elements': [
            {'type': 'button', 'text': {'type': 'plain_text', 'text': self.msg('dispatch.breaker_card_blocks.text'), 'emoji': True},
             'action_id': 'sf_breaker:continue', 'value': breaker_id, 'style': 'primary'},
            {'type': 'button', 'text': {'type': 'plain_text', 'text': self.msg('dispatch.breaker_card_blocks.text_2'), 'emoji': True},
             'action_id': 'sf_breaker:stop', 'value': breaker_id, 'style': 'danger'}]}]

    def progress_line(self, steps, summary, elapsed_seconds):
        """The run card's live status line. `summary` is already masked and mention-free (run_log.short)."""
        minutes = max(0, int(elapsed_seconds // 60))
        return self.msg('dispatch.progress_line.1', int=int(steps), mrkdwn=_mrkdwn(summary), minutes=minutes)

    def _waited(self, seconds):
        minutes = max(0, int(seconds // 60))
        return self.msg('dispatch._waited.1', value=minutes // 60) if minutes >= 60 else self.msg('dispatch._waited.2', minutes=minutes)


EXPORT_RE = re.compile(r"^\s*export\s+([A-Z][A-Z0-9_]{1,63})=(?:\"([^\"$`\\]*)\"|'([^']*)'|([^\s\"'$`;#]+))\s*(?:#.*)?$")


def rc_exports(names, home=None, *, rc_files):
    """Last literal `export NAME=value` per name across the owner's shell startup files `rc_files` (relative to `home`;
    comments ignored). Values containing expansions are skipped (the login-shell fallback evaluates those)."""
    home = Path(home or os.path.expanduser('~'))
    found = {}
    for rc in rc_files:
        try:
            lines = (home / rc).read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            m = EXPORT_RE.match(line)
            if m and m.group(1) in names:
                value = next(g for g in m.groups()[1:] if g is not None)
                if value:
                    found[m.group(1)] = value
    return found
