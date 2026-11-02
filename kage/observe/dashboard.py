"""Loopback-only, metadata-only Slack follow-up observatory.

P1b adds three read-only pages and one write:
  /runs, /runs/<run-id>  dispatched consumer runs and their live, masked readable log
  /backlog               "旧积压": events the stale guard leaves for the owner (expired watch / too old)
  /settings              ~/.config/<worker_slug>/config.json; the ONLY new POST is the
                         settings save (same-origin + per-page token), written by worker_config.set_many
                         with change-log actor "dashboard".
P2 adds one read-only page:
  /intake                "收件": intake cadence / API budget / backoff, recent bursts and their routes,
                         triage records and conversation bindings (metadata only, never message text).
P3 adds two read-only pages:
  /rules                 "规则": P47 rule registry list (title, how checked, incident, status); active rules version,
                         weekly outcome shares, approval latency, the owner's overrides,
                         recent checks (ids, rules, outcome, approval; never plans or emails), proposals, backtest.
  /routines              "定时": routines per task (schedule, next / last run, provenance, state), recent runs,
                         routine proposals and invalid routines.yaml files.
P3.1 adds one read-only page:
  /gaps, /gaps/<id>      "缺口": capability blockers and the automation gap ledger (counts by system / type / week,
                         open blockers, most common paths, the owner's manual takeovers, skill proposals). The list and
                         /api/gaps carry system / operation / type / status only; `tried` / `unblock` text is on the
                         detail view; verification codes are never stored anywhere.
P3.1 cards add two detail views of /rules (the technical record a Slack card links to instead of showing it):
  /rules?check=<rc…>     /api/rules/checks/<id>: the agent's sentence, matched rules, failed checks, never-auto
                         shapes, facts, approvers, the plan (it may name the person), approval and outcome.
  /rules?proposal=<rp…>  /api/rules/proposals/<id>: a rule proposal's reason, exact text or patch lines, evidence.
P39 adds one read-only page:
  /design                "设计": the architecture design document (runbooks/architecture-design.md rendered by
                         build_review_html.py into ONE private file, `--design-html`), embedded in a sandboxed iframe
                         from /design/doc (that file's bytes only: no symlink, no listing, 16 MB cap, a CSP that forbids
                         every network fetch); /api/design reports exists / size / mtime / short sha256 / external refs.
P40 adds one read-only panel to /health (and /api/cache): the prompt-cache report of the last 24 h of dispatched runs
(`cache_report.report`: per task / provider / context profile the cold-start rate, first-call input / read / write and
durations; numbers only), memoized for CACHE_REPORT_SECONDS. P40.1 adds the keep-warm refreshes as their own summary
rows (`keep_warm`; never counted as runs).
P44 adds to /health the provider probes (per provider: last probe, result, next probe) and the dispatches waiting for a
provider to come back (count, oldest wait), and to /settings the 等 AI 恢复 keys (dispatch.provider_wait_hours, …).
P48 turns /intake into the inbox trace (trace_chain.py, read-only, metadata only, no Slack call):
  /api/inbox             every inbox item of the last 1-14 days (bursts, messages intake skipped or a task's watch
                         took, the owner's card-thread replies, local agent messages) with its 8-stage pipeline and status;
                         `view` / `task` filter it, `q` searches (a Slack link, CHANNEL/TS, burst / event / task id, a name).
  /api/trace?ref=…       one item's whole chain: 收到 · 过滤 / 合并 · 分诊 · 路由 · 派发 · 卡片 · 回复 · 结清, with the
                         steps behind them and the last masked summary lines of the runs' local logs.
  /intake?trace=<ref>    deep link: the page opens that item's trace.
The database is still opened read-only; run logs are read, never written, from here.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from kage import locale
from kage.core import control as _control

_OWNER_DEFAULT = object()  # a parameter whose default is the owner's value, filled in at call time


INBOX_MEMO_SECONDS = 4  # P48: the inbox list is rebuilt at most this often (each poll filters the memoized items)


INBOX_LIMIT = 300


CACHE_REPORT_SECONDS = 60  # the panel re-reads the run logs at most once a minute


PROVIDERS = {
    'codex': {'id': 'codex', 'label': 'Codex', 'delivery_supported': True, 'status': 'available', 'delivery_path': 'codex_queue', 'avatar_url': '/assets/codex-agent.png'},
    'claude': {'id': 'claude', 'label': 'Claude', 'delivery_supported': True, 'status': 'available', 'delivery_path': 'sqlite_outbox_native_hook_or_waiter', 'consumer_required': True, 'auto_resume': False, 'avatar_url': '/assets/claude-agent.png', 'logo_url': '/assets/claude-agent.png'},
}


COUNTERS = ('pending', 'queued', 'claimed', 'handled', 'sending', 'delivery_unknown')


EVENT_LIMIT = 200


WATCH_LIMIT = 2000


RUN_ID_RE = re.compile(r'r\d{8}T\d{6}Z-[0-9a-f]{6}')


RUN_FILTERS = ('all', 'running', 'failed', 'today')


FAILED_OUTCOMES = ('failed', 'unresolved', 'partial', 'stopped')


RUN_LIMIT = 300


LOG_INITIAL_LINES = 400


LOG_CHUNK_BYTES = 262144


LEGACY_RENDER_BYTES = 2 * 1024 * 1024


BACKLOG_STATUSES = ('pending', 'queued', 'delivery_unknown', 'claimed', 'sending')


BACKLOG_LIMIT = 500


PAGE_TOKEN_TTL = 1800


PAGE_TOKEN_MAX = 32


SETTINGS_BODY_LIMIT = 32768


EDITABLE_KEYS = (
    'providers.claude.model_default', 'providers.claude.model_important', 'providers.claude.effort',
    'providers.codex.model', 'providers.codex.effort',
    'routing.categories', 'routing.fallback_order',
    'dispatch.enabled', 'dispatch.progress_updates', 'dispatch.concurrency', 'dispatch.max_event_age_hours',
    'dispatch.claude_env_passthrough',
    'circuit_breaker.round_trips_per_hour',
    'intake.enabled', 'intake.channels', 'intake.oncall_channels', 'intake.burst_seconds', 'intake.poll_seconds',
    'intake.alert_dedupe_minutes', 'intake.dm_queries', 'intake.mention_search', 'intake.delegate_bot_names',  # P42.1
    'intake.inbox_provider', 'intake.inbox_model', 'intake.inbox_effort',
    'intake.oncall_provider', 'intake.oncall_model', 'intake.oncall_effort',
    'ask.remind_after', 'ask.expire_after',
    'rules.enabled', 'rules.proposals_enabled', 'rules.typed_approval_max_age_hours', 'routines.enabled',  # P46
    'blockers.enabled', 'blockers.skill_proposals_enabled',
    'cards.retire_buttons_on_reply', 'cards.thread_reply_decisions', 'cards.read_receipts', 'cards.open_poll_seconds',
    'cards.task_thread', 'cards.retire_on', 'cards.max_open_per_task',  # P47
    'tasks.max_wakes', 'tasks.wake_window_hours', 'tasks.stale_card_days',  # P42
    'dispatch.provider_wait_hours', 'dispatch.probe_enabled', 'dispatch.probe_min_seconds',  # P44
    'dispatch.probe_max_seconds', 'dispatch.unresolved_retries',
)


CONTROL_QUERY = {'conversation'}  # S2: the query keys the control page (/) reads; any other key on / is an old scene URL


OPS_PAGES = ('/runs', '/backlog', '/settings', '/intake', '/rules', '/routines', '/gaps', '/learning', '/health',
             '/design')


PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


CONTROL_BODY_LIMIT = 8192  # Phase 2 C1: one control POST (a message of at most kage.core.control.MAX_TEXT characters)
CONTROL_EDITABLE = ('enabled', 'provider', 'model', 'effort')  # what the /control page may set (control.<key>)


DESIGN_MAX_BYTES = 16 * 1024 * 1024


DESIGN_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
              "font-src data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'; sandbox allow-scripts")


DESIGN_REF_RE = re.compile(rb'''\b(src|href)\s*=\s*["']?\s*https?://''', re.I)


RULES_WEEKS = 8


RULES_RECENT = 60


INTAKE_LIMIT = 200


def read_db(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    return db


def slack_link(channel, ts=None):
    if not re.fullmatch(r'[CDG][A-Z0-9]+', str(channel)):
        return None
    base = f'https://app.slack.com/archives/{channel}'
    return base + '/p' + str(ts).replace('.', '') if ts and re.fullmatch(r'\d+\.\d+', str(ts)) else base


def process_alive(pid, birth):
    if not isinstance(pid, int) or pid <= 1 or not birth:
        return False
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                                capture_output=True, text=True, timeout=2)
        return result.stdout.strip() == birth
    except (OSError, subprocess.TimeoutExpired):
        return False


def runs_root(path):
    return Path(path).expanduser().resolve().parent / 'runs'


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _run_dir(path, run_id, log_dir=None):
    """The run's private directory, only if it is exactly runs/<run-id> (no symlinks, no other paths)."""
    if not RUN_ID_RE.fullmatch(str(run_id)):
        return None
    root = runs_root(path)
    candidate = root / run_id
    if log_dir and Path(log_dir).expanduser().resolve() != candidate.resolve():
        return None
    if not candidate.is_dir() or candidate.is_symlink():
        return None
    return candidate


def _event_counts(db, event_ids):
    if not event_ids:
        return 0, 0
    handled = db.execute("SELECT count(*) FROM events WHERE status='handled' AND id IN (%s)" % ','.join('?' * len(event_ids)),
                         event_ids).fetchone()[0]
    return handled, len(event_ids)


RUN_SELECT = ('SELECT r.*, d.state AS dispatch_state, d.card_channel AS card_channel, d.card_ts AS card_ts, '
              'd.fail_card_ts AS fail_card_ts FROM consumer_runs r LEFT JOIN dispatches d ON d.dispatch_id=r.dispatch_id')


def _loads(text, default):
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 1)


class DesignDocError(Exception):
    """The design document cannot be served: `code` is enumerated, `status` is the /design/doc HTTP status."""
    STATUS = {'missing': 404, 'symlink_refused': 403, 'not_regular_file': 403, 'too_large': 413, 'unreadable': 503}

    def __init__(self, code):
        super().__init__(code)
        self.code, self.status = code, self.STATUS[code]


def read_design_doc(path, limit=None):
    """The generated design HTML, read-only. The file itself must not be a symlink (lstat + O_NOFOLLOW), must be a
    regular file (a FIFO / device / directory is refused without blocking: O_NONBLOCK, then fstat) and at most `limit`
    bytes (checked on fstat and while reading). Returns (stat_result, bytes); raises DesignDocError."""
    path, limit = str(path), DESIGN_MAX_BYTES if limit is None else limit
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise DesignDocError('symlink_refused')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                     | getattr(os, 'O_CLOEXEC', 0))
    except (FileNotFoundError, NotADirectoryError):
        raise DesignDocError('missing') from None
    except OSError as exc:
        raise DesignDocError('symlink_refused' if exc.errno in (errno.ELOOP, errno.EMLINK) else 'unreadable') from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise DesignDocError('not_regular_file')
        if st.st_size > limit:
            raise DesignDocError('too_large')
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(1 << 20, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:  # grew while being read
                raise DesignDocError('too_large')
        return st, b''.join(chunks)
    except OSError:
        raise DesignDocError('unreadable') from None
    finally:
        os.close(fd)


def _settings_changes(payload):
    if not isinstance(payload, dict) or set(payload) - {'changes', 'digest', 'dry_run'}:
        raise ValueError('invalid_request_body')
    changes = payload.get('changes')
    if not isinstance(changes, list) or not 1 <= len(changes) <= len(EDITABLE_KEYS):
        raise ValueError('invalid_request_body')
    out, seen = [], set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'key', 'value'} or change['key'] not in EDITABLE_KEYS \
                or change['key'] in seen:
            raise ValueError('setting_not_editable_here')
        seen.add(change['key'])
        out.append((change['key'], change['value']))
    digest = payload.get('digest')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{16}', digest):
        raise ValueError('invalid_request_body')
    if not isinstance(payload.get('dry_run', False), bool):
        raise ValueError('invalid_request_body')
    return out, digest, bool(payload.get('dry_run'))


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_path, web=_OWNER_DEFAULT, enrich_profiles=False, design_html=None, *, core):
        self.core = core
        web = self.core.web if web is _OWNER_DEFAULT else web
        if address[0] != '127.0.0.1':
            raise ValueError('dashboard_must_bind_loopback')
        self.db_path, self.web = Path(db_path), Path(web)
        self.design_html = Path(design_html or self.core.default_design_html).expanduser()  # P39: the one file /design/doc serves
        self.csrf = secrets.token_urlsafe(32)
        self.page_tokens = {}  # settings page token -> expiry (monotonic)
        self.token_lock = threading.Lock()
        self.previews = self.core.previews.PreviewService(self.db_path)
        # C3: the owner's Slack words for /api/control, re-read through the preview service's owner token (memory only)
        self.control_words = _control.SlackWords(self.previews)
        self.cache_lock = threading.Lock()
        self.cached = None
        self.cached_at = 0
        self.inbox_lock = threading.Lock()
        self.inbox_memo = {}  # P48: days -> (monotonic time, items)
        self.avatar_worker = None
        if self.db_path.exists():
            with contextlib.closing(sqlite3.connect(self.db_path, timeout=5)) as db:
                self.core.avatars.initialize(db)
                self.core.avatars.seed_events(db)
        super().__init__(address, Handler)
        if enrich_profiles and self.db_path.exists():
            self.avatar_worker = self.core.avatars.CacheWorker(self.db_path)
            self.avatar_worker.start()

    def server_close(self):
        if self.avatar_worker:
            self.avatar_worker.stop_event.set()
        super().server_close()

    def issue_page_token(self):
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        with self.token_lock:
            self.page_tokens = {t: exp for t, exp in self.page_tokens.items() if exp > now}
            while len(self.page_tokens) >= PAGE_TOKEN_MAX:
                self.page_tokens.pop(min(self.page_tokens, key=self.page_tokens.get))
            self.page_tokens[token] = now + PAGE_TOKEN_TTL
        return token

    def check_page_token(self, token, consume=False):
        now = time.monotonic()
        with self.token_lock:
            match = next((t for t in self.page_tokens if secrets.compare_digest(t, str(token or ''))), None)
            if match is None or self.page_tokens[match] <= now:
                return False
            if consume:
                self.page_tokens.pop(match, None)
            return True

    def inbox(self, days):
        with self.inbox_lock:
            at, items = self.inbox_memo.get(days, (None, None))
            if at is None or time.monotonic() - at >= INBOX_MEMO_SECONDS:
                items = self.core.inbox_items(self.db_path, days)
                self.inbox_memo[days] = (time.monotonic(), items)
            return items

    def state(self):
        with self.cache_lock:
            if self.cached is None or time.monotonic()-self.cached_at >= 1:
                self.cached = self.core.snapshot(self.db_path)
                self.cached_at = time.monotonic()
            return {**self.cached, 'csrf_token': self.csrf}


class Handler(BaseHTTPRequestHandler):
    server_version = 'FollowupObservatory/1'

    def log_message(self, format, *args):
        # No request URLs, headers, local paths or metadata in service logs.
        pass

    def valid_host(self):
        host = self.headers.get('Host', '')
        port = self.server.server_port
        return host in (f'127.0.0.1:{port}', f'localhost:{port}')

    def respond(self, status, payload, content_type='application/json; charset=utf-8', csp=PAGE_CSP):
        data = json.dumps(payload, ensure_ascii=False).encode() if not isinstance(payload, bytes) else payload
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', csp)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, {'error': 'invalid_host'})
        path = urlsplit(self.path).path
        try:
            match = re.fullmatch(r'/api/avatars/(U[A-Z0-9]+)', path)
            if match:
                with contextlib.closing(read_db(self.server.db_path)) as db:
                    row = db.execute('SELECT avatar,mime FROM dashboard_profiles WHERE user_id=?', (match[1],)).fetchone()
                if not row or not row['avatar']:
                    return self.respond(404, {'error': 'avatar_pending'})
                return self.respond(200, row['avatar'], row['mime'])
            if path.startswith('/assets/'):
                name = path.removeprefix('/assets/')
                if name not in ('codex-agent.png', 'claude-agent.png'):
                    return self.respond(404, {'error': 'not_found'})
                return self.respond(200, (self.server.web/'assets'/name).read_bytes(), 'image/png')
            if path == '/api/state':
                return self.respond(200, self.server.state())
            query = parse_qs(urlsplit(self.path).query)
            if path == '/api/control':  # Phase 2 C1: the control entry (a page token only when the page asks for one)
                conversation = (query.get('conversation') or [None])[0]
                token = self.server.issue_page_token() if (query.get('token') or [''])[0] == 'new' else None
                # C3: a Slack conversation's words are re-read from Slack only for the page itself (or a local tool),
                # never for another site's request
                words = self.server.control_words if conversation and self.headers.get(
                    'Sec-Fetch-Site', 'none') in ('same-origin', 'none') else None
                try:
                    return self.respond(200, self.server.core.control_state(self.server.db_path, conversation, token,
                                                                            words=words))
                except KeyError:
                    return self.respond(404, {'error': 'unknown_conversation'})
            if path in ('/', '/control'):  # S2: the control entry (总控) is the console's home; /control stays as an alias
                if path == '/' and set(parse_qs(urlsplit(self.path).query, keep_blank_values=True)) - CONTROL_QUERY:
                    # an old scene URL with its own parameters: the scene
                    return self.redirect('/scene', self.path.partition('?')[2])
                return self.respond(200, (self.server.web/'control.html').read_bytes(), 'text/html; charset=utf-8')
            if path in ('/scene', '/index.html'):  # S2: the scene (联动场景) moved from / to /scene
                if path == '/index.html':
                    return self.redirect('/scene', self.path.partition('?')[2])
                return self.respond(200, (self.server.web/'index.html').read_bytes(), 'text/html; charset=utf-8')
            if path == '/api/runs':
                run_filter = (query.get('filter') or ['all'])[0]
                task = (query.get('task') or [None])[0]
                if task is not None and (len(task) > 200 or any(ord(c) < 32 for c in task)):
                    return self.respond(400, {'error': 'invalid_task'})
                try:
                    return self.respond(200, self.server.core.list_runs(self.server.db_path, run_filter, task or None))
                except ValueError as exc:
                    if str(exc) == 'invalid_filter':
                        return self.respond(400, {'error': 'invalid_filter'})
                    raise
            match = re.fullmatch(r'/api/runs/([A-Za-z0-9-]{1,40})(/log)?', path)
            if match:
                try:
                    if match[2]:
                        cursor = (query.get('cursor') or [None])[0]
                        if cursor is not None and not re.fullmatch(r'[bl]:\d{1,12}', cursor):
                            return self.respond(400, {'error': 'invalid_cursor'})
                        return self.respond(200, self.server.core.run_log_chunk(self.server.db_path, match[1], cursor))
                    return self.respond(200, self.server.core.run_detail(self.server.db_path, match[1]))
                except KeyError:
                    return self.respond(404, {'error': 'run_not_found'})
            if path == '/api/backlog':
                return self.respond(200, self.server.core.stale_backlog(self.server.db_path))
            if path == '/api/intake':
                return self.respond(200, self.server.core.intake_overview(self.server.db_path))
            if path == '/api/inbox':
                view, task, q = ((query.get(k) or [None])[0] for k in ('view', 'task', 'q'))
                days = (query.get('days') or [str(self.server.core.trace.LIST_DAYS)])[0]
                if (view or 'all') not in self.server.core.trace.VIEWS or not re.fullmatch(r'\d{1,2}', days) or \
                        not 1 <= int(days) <= self.server.core.trace.LIST_MAX_DAYS or \
                        any(v is not None and (len(v) > 600 or any(ord(c) < 32 for c in v)) for v in (task, q)):
                    return self.respond(400, {'error': 'invalid_inbox_query'})
                items = self.server.inbox(int(days))
                return self.respond(200, self.server.core.inbox_view(self.server.db_path, items, view or 'all', task or None,
                                                    (q or '').strip() or None))
            if path == '/api/trace':
                try:
                    return self.respond(200, self.server.core.trace_detail(self.server.db_path, (query.get('ref') or [''])[0]))
                except KeyError:
                    return self.respond(404, {'error': 'trace_not_found'})
                except ValueError as exc:
                    if str(exc) != 'invalid_ref':
                        raise
                    return self.respond(400, {'error': 'invalid_ref'})
            if path == '/api/rules':
                return self.respond(200, self.server.core.rules_overview(self.server.db_path))
            match = re.fullmatch(r'/api/rules/(checks|proposals)/([a-z0-9]{1,40})', path)
            if match:
                try:
                    if match[1] == 'checks':
                        return self.respond(200, self.server.core.rule_check_detail(self.server.db_path, match[2]))
                    return self.respond(200, self.server.core.rule_proposal_detail(self.server.db_path, match[2]))
                except KeyError:
                    return self.respond(404, {'error': match[1][:-1] + '_not_found'})
            if path == '/api/routines':
                return self.respond(200, self.server.core.routines_overview(self.server.db_path))
            if path == '/api/gaps':
                return self.respond(200, self.server.core.gaps_overview(self.server.db_path))
            if path == '/api/learning':
                return self.respond(200, self.server.core.learning_overview(self.server.db_path))
            if path == '/api/health':
                return self.respond(200, self.server.core.health_overview(self.server.db_path))
            if path == '/api/cache':
                return self.respond(200, self.server.core.cache_overview(self.server.db_path))
            match = re.fullmatch(r'/api/gaps/([a-z0-9]{1,40})', path)
            if match:
                try:
                    return self.respond(200, self.server.core.gap_detail(self.server.db_path, match[1]))
                except KeyError:
                    return self.respond(404, {'error': 'gap_not_found'})
            if path == '/api/settings':
                return self.respond(200, self.server.core.settings_state(self.server.issue_page_token()))
            if path == '/api/design':
                return self.respond(200, self.server.core.design_status(self.server.design_html))
            if path == '/design/doc':
                return self.design_doc()
            if path in OPS_PAGES or re.fullmatch(r'/runs/[A-Za-z0-9-]{1,40}', path) or \
                    re.fullmatch(r'/gaps/[a-z0-9]{1,40}', path):
                return self.respond(200, (self.server.web/'ops.html').read_bytes(), 'text/html; charset=utf-8')
            assets = {'/base.css': ('base.css', 'text/css; charset=utf-8'),
                      '/shell.js': ('shell.js', 'text/javascript; charset=utf-8'),
                      '/md.js': ('md.js', 'text/javascript; charset=utf-8'),
                      '/app.css': ('app.css', 'text/css; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/ops.css': ('ops.css', 'text/css; charset=utf-8'),
                      '/ops.js': ('ops.js', 'text/javascript; charset=utf-8'),
                      '/trace.css': ('trace.css', 'text/css; charset=utf-8'),
                      '/trace.js': ('trace.js', 'text/javascript; charset=utf-8'),
                      '/control.css': ('control.css', 'text/css; charset=utf-8'),
                      '/control.js': ('control.js', 'text/javascript; charset=utf-8')}
            if path in assets:
                name, mime = assets[path]
                return self.respond(200, (self.server.web/name).read_bytes(), mime)
            return self.respond(404, {'error': 'not_found'})
        except (sqlite3.Error, ValueError, OSError, TypeError):
            return self.respond(503, {'error': 'local_state_unavailable'})

    def redirect(self, target, query=''):
        """S2: a same-origin redirect to one of the console's own pages (a fixed path), the request's raw query carried
        over unchanged; a query with a control character, a space or a backslash is refused instead."""
        if any(ord(c) < 32 or c in ' \\' for c in query) or len(query) > 2000:
            return self.respond(400, {'error': 'invalid_query'})
        location = target + ('?' + query if query else '')
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', PAGE_CSP)
        self.end_headers()

    def design_doc(self):
        """GET /design/doc (P39): the generated design HTML's bytes, for the sandboxed iframe on /design, under a CSP that
        forbids every network fetch. A missing / refused / oversized file gets a short static error page instead."""
        try:
            data = read_design_doc(self.server.design_html)[1]
        except DesignDocError as exc:
            page = (self.server.core.msg('dashboard.Handler.design_doc.1', code=exc.code))
            return self.respond(exc.status, page.encode(), 'text/html; charset=utf-8', csp=DESIGN_CSP)
        return self.respond(200, data, 'text/html; charset=utf-8', csp=DESIGN_CSP)

    def do_POST(self):
        if not self.valid_host():
            return self.respond(403, {'error': 'invalid_host'})
        if self.headers.get('Origin') != f"http://{self.headers.get('Host')}":
            return self.respond(403, {'error': 'same_origin_required'})
        if urlsplit(self.path).path == '/api/settings':
            return self.post_settings()
        if urlsplit(self.path).path in ('/api/control/messages', '/api/control/retry', '/api/control/settings'):
            return self.post_control(urlsplit(self.path).path)
        if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.server.csrf):
            return self.respond(403, {'error': 'invalid_csrf_token'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.respond(415, {'error': 'json_required'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 1024:
                raise ValueError()
            payload = json.loads(self.rfile.read(size) or b'{}')
            path = urlsplit(self.path).path
            if path == '/api/sessions/stop':
                if (not isinstance(payload, dict) or set(payload) != {'provider', 'session_id'}
                        or any(not isinstance(payload[k], str) or not payload[k].strip()
                               or len(payload[k]) > 200 for k in ('provider', 'session_id'))):
                    raise ValueError()
            elif payload != {}:
                raise ValueError()
        except (ValueError, UnicodeError):
            return self.respond(400, {'error': 'invalid_request_body'})
        if path == '/api/sessions/stop':
            try:
                with self.server.cache_lock:
                    result = self.server.core.stop_session(self.server.db_path, payload['provider'], payload['session_id'])
                    self.server.cached = None
                return self.respond(200, result)
            except KeyError:
                return self.respond(404, {'error': 'session_not_found'})
            except (sqlite3.Error, ValueError, OSError):
                return self.respond(503, {'error': 'local_state_unavailable'})
        match = re.fullmatch(r'/api/watches/([a-zA-Z0-9_-]{1,100})/(stop|preview)', path)
        if not match:
            return self.respond(404, {'error': 'not_found'})
        if match[2] == 'preview':
            try:
                return self.respond(200,self.server.previews.preview(match[1]))
            except self.server.core.previews.PreviewError as exc:
                return self.respond(exc.status,{'error':exc.code,'retry_after':exc.retry_after})
            except (sqlite3.Error,ValueError,OSError):
                return self.respond(503,{'error':'preview_unavailable','retry_after':15})
        try:
            with self.server.cache_lock:
                result = self.server.core.stop_watch(self.server.db_path, match[1])
                self.server.cached = None
            return self.respond(200, result)
        except KeyError:
            return self.respond(404, {'error': 'watch_not_found'})
        except (sqlite3.Error, ValueError, OSError):
            return self.respond(503, {'error': 'local_state_unavailable'})


    def post_control(self, path):
        """Phase 2 C1, the control entry's writes: same-origin (checked by the caller), a live page token (a message
        does not use it up; a settings save does), JSON only, at most CONTROL_BODY_LIMIT bytes. The request's facts
        (Fetch Metadata headers present or not, user-agent family) are stored with a message and shown with it."""
        if self.headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin':
            return self.respond(403, {'error': 'same_origin_required'})
        token = self.headers.get('X-Page-Token', '')
        if not self.server.check_page_token(token):
            return self.respond(403, {'error': 'invalid_page_token'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.respond(415, {'error': 'json_required'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= CONTROL_BODY_LIMIT:
                raise ValueError()
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            return self.respond(400, {'error': 'invalid_request_body'})
        core = self.server.core
        try:
            if path == '/api/control/messages':
                result = core.control_submit(self.server.db_path, payload, _control.console_facts(self.headers))
            elif path == '/api/control/retry':
                result = core.control_retry(self.server.db_path, payload)
            else:
                result = core.control_save(payload)
                self.server.check_page_token(token, consume=True)
                result['page_token'] = self.server.issue_page_token()
        except ValueError as exc:
            code = str(exc) if re.fullmatch(r'[a-z0-9_]{1,120}', str(exc)) else 'invalid_request'
            status = 409 if code in ('control_off', 'worker_config_changed_since_loaded_reload') else \
                429 if code.endswith('rate_limited') else 400
            return self.respond(status, {'error': code})
        except (sqlite3.Error, OSError):
            return self.respond(503, {'error': 'local_state_unavailable'})
        return self.respond(200, result)

    def post_settings(self):
        """The settings save: same-origin (checked by the caller), a live per-page token, JSON only."""
        if self.headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin':
            return self.respond(403, {'error': 'same_origin_required'})
        token = self.headers.get('X-Page-Token', '')
        if not self.server.check_page_token(token):
            return self.respond(403, {'error': 'invalid_page_token'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.respond(415, {'error': 'json_required'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= SETTINGS_BODY_LIMIT:
                raise ValueError()
            payload = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError):
            return self.respond(400, {'error': 'invalid_request_body'})
        try:
            result = self.server.core.save_settings(payload)
        except ValueError as exc:
            code = str(exc) if re.fullmatch(r'[a-z0-9_]{1,120}', str(exc)) else 'invalid_settings'
            return self.respond(409 if code == 'worker_config_changed_since_loaded_reload' else 400, {'error': code})
        except OSError:
            return self.respond(503, {'error': 'config_write_failed'})
        if not result['dry_run'] and result['changes']:
            self.server.check_page_token(token, consume=True)  # one save per token; the page gets a new one
            result['page_token'] = self.server.issue_page_token()
        else:
            result['page_token'] = token
        result['log'] = self.server.core.worker_config.change_log(limit=60)
        try:
            result['config'] = self.server.core.worker_config.load()[0]
        except ValueError:
            result['config'] = None
        return self.respond(200, result)


class DashboardCore:
    """The owner-bound half of the dashboard: the owner's time zone, key and name (stored values), the page
    words in the owner's language, the deployment's pages directory, design document and workspace,
    channel names and tail command, and the runtime modules the read models read (hooks); the
    DashboardServer holds one and its Handler reaches it through the server."""

    def __init__(self, language, *, tz, owner_key, owner_name, web, default_design_html, design_workspace, known_channels, tail_command, avatars, poll_schedule, previews, claude_delivery, run_log, worker_config, task_registry, rule_engine, routines, decision_buttons, blockers, feedback_learning, provider_health, stuck_claims, dispatcher, cache_report, trace, process_alive):
        self.language = language
        self.tz = tz
        self.web = web
        self.default_design_html = default_design_html
        self.design_workspace = design_workspace
        self.known_channels = known_channels
        self.tail_command = tail_command
        self.avatars = avatars
        self.poll_schedule = poll_schedule
        self.previews = previews
        self.claude_delivery = claude_delivery
        self.run_log = run_log
        self.worker_config = worker_config
        self.task_registry = task_registry
        self.rule_engine = rule_engine
        self.routines = routines
        self.decision_buttons = decision_buttons
        self.blockers = blockers
        self.feedback_learning = feedback_learning
        self.provider_health = provider_health
        self.stuck_claims = stuck_claims
        self.dispatcher = dispatcher
        self.cache_report = cache_report
        self.trace = trace
        self.process_alive = process_alive
        self.key, self.name = owner_key, owner_name
        self._cache_memo = {}

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def watch_visuals(self, w, now):
        interval=self.poll_schedule.DELAYS[min(max(0,int(w.get('step',0))),len(self.poll_schedule.DELAYS)-1)]
        minutes=interval//60
        styles={1:('fresh',self.msg('dashboard.watch_visuals.1'),'#16a34a'),2:('warm',self.msg('dashboard.watch_visuals.2'),'#0891b2'),5:('steady',self.msg('dashboard.watch_visuals.3'),'#2563eb'),10:('quiet',self.msg('dashboard.watch_visuals.4'),'#d97706'),15:('cold',self.msg('dashboard.watch_visuals.5'),'#8b8fa8')}
        key,label,color=styles.get(minutes,styles[15])
        if w.get('state')!='running':key,label,color='closed',self.msg('dashboard.watch_visuals.6'),'#94a3b8'
        duration=None;start=None;deadline=w.get('deadline')
        if w.get('lifecycle')=='idle' and isinstance(deadline,(int,float)):
            duration=w.get('inactivity_seconds') or 172800;start=deadline-duration
        elif isinstance(deadline,(int,float)):
            if w.get('fixed_deadline') is not None or w.get('lifecycle')=='fixed':
                duration=w.get('duration_seconds') or max(0,deadline-w.get('started',deadline));start=deadline-duration
            elif w.get('lifecycle')=='sliding' or not w.get('lifecycle'):
                duration=900 if w.get('mode')=='observe' else 3600;start=deadline-duration
        progress=min(1,max(0,(now-start)/duration)) if duration and duration>0 and start is not None else None
        return {'lifecycle_progress':progress,'window_started_at':start,'window_seconds':duration,
                'remaining_seconds':max(0,deadline-now) if isinstance(deadline,(int,float)) else None,
                'freshness':{'key':key,'label':self.msg('dashboard.watch_visuals.7', label=label, minutes=minutes) if key!='closed' else label,'color':color,'interval_minutes':minutes}}

    def snapshot(self, path, now=None):
        now = time.time() if now is None else now
        providers = {key: dict(value) for key, value in PROVIDERS.items()}
        summary = dict(sessions=0, watches=0, running=0, stopped=0, expired=0,
                       pending=0, queued=0, claimed=0, handled=0, uncertain=0, errors=0)
        result = dict(schema_version=1, generated_at=now, providers=[], supervisor={
            'pid': None, 'alive': False, 'heartbeat': None, 'heartbeat_age_seconds': None,
            'version': None, 'health': 'unknown'}, summary=summary, sessions=[], watches=[], events=[],
            limits={'events': EVENT_LIMIT, 'watches': WATCH_LIMIT, 'events_truncated': False,
                    'watches_truncated': False})
        if not Path(path).is_file():
            result['providers'] = list(providers.values())
            result['notice'] = 'state_database_missing'
            return result
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'watches', 'events'} <= tables:
                raise ValueError('incompatible_state_database')
            watch_rows = db.execute('SELECT id,data FROM watches ORDER BY id LIMIT ?', (WATCH_LIMIT+1,)).fetchall()
            result['limits']['watches_truncated'] = len(watch_rows) > WATCH_LIMIT
            counts = {}
            for row in db.execute('SELECT watch_id,status,count(*) AS n FROM events GROUP BY watch_id,status'):
                counts.setdefault(row['watch_id'], {})[row['status']] = row['n']
            sessions, watch_map = {}, {}
            for row in watch_rows[:WATCH_LIMIT]:
                w = json.loads(row['data'])
                provider = str(w.get('provider') or 'codex')
                providers.setdefault(provider, {'id': provider, 'label': provider, 'delivery_supported': False, 'status': 'unknown'})
                session_id = str(w.get('session') or 'unknown')
                session_key = f'{provider}:{session_id}'
                cwd = str(w.get('cwd') or '')
                session = sessions.setdefault(session_key, {
                    'key': session_key, 'id': session_id, 'provider': provider,
                    'label': Path(cwd).name or session_id, 'cwd': cwd,
                    'watch_ids': [], 'channels': [], 'running': 0, 'watch_count': 0,
                    'pending': 0, 'queued': 0, 'claimed': 0, 'handled': 0, 'uncertain': 0, 'last_activity': None})
                event_counts = {name: counts.get(row['id'], {}).get(name, 0) for name in COUNTERS}
                watch = {key: w.get(key) for key in (
                    'channel', 'thread', 'mode', 'state', 'deadline', 'since', 'started',
                    'next_poll', 'last_poll', 'polls', 'last_error', 'stop_reason', 'peers',
                    'last_activity', 'inactivity_seconds', 'expiry_pending_check', 'last_delivery')}
                watch.update(id=row['id'], session_key=session_key, session_id=session_id,
                             provider=provider, scope=w.get('scope', 'thread'),
                             lifecycle=w.get('lifecycle') or ('fixed' if w.get('fixed_deadline') is not None else 'sliding'),
                             poll_interval_seconds=self.poll_schedule.DELAYS[min(max(0,int(w.get('step',0))),len(self.poll_schedule.DELAYS)-1)],
                             cadence_activity_ts=w.get('cadence_activity_ts'),
                             context_all=bool(w.get('context_all')), root_count=len(w.get('fetch_state', {}).get('roots', [])),
                             event_counts=event_counts, slack_url=slack_link(w.get('channel'), w.get('thread')))
                watch.update(self.watch_visuals(w,now))
                watch_map[row['id']] = watch
                session['watch_ids'].append(row['id'])
                if watch['channel'] not in session['channels']:
                    session['channels'].append(watch['channel'])
                session['watch_count'] += 1
                session['running'] += int(watch['state'] == 'running')
                for key in ('pending', 'queued', 'claimed', 'handled'):
                    session[key] += event_counts[key]
                    summary[key] += event_counts[key]
                uncertain = event_counts['sending'] + event_counts['delivery_unknown']
                session['uncertain'] += uncertain
                summary['uncertain'] += uncertain
                activity = w.get('last_poll') or w.get('started')
                if isinstance(activity, (int, float)):
                    session['last_activity'] = max(session['last_activity'] or 0, activity)
                if watch['state'] in ('running', 'stopped', 'expired'):
                    summary[watch['state']] += 1
                summary['errors'] += int(bool(watch['last_error']))
            wake_rows = {r['session_id']: dict(r) for r in db.execute('SELECT * FROM claude_wakes')} if 'claude_wakes' in tables else {}
            eligible_counts = {}
            for watch in watch_map.values():
                if watch['state'] != 'stopped':
                    bucket = eligible_counts.setdefault(watch['session_key'], {'queued': 0, 'claimed': 0})
                    for key in bucket:
                        bucket[key] += watch['event_counts'][key]
            for session in sessions.values():
                session['delivery_path'] = providers[session['provider']].get('delivery_path', 'unsupported')
                if session['provider'] == 'claude':
                    wake = wake_rows.get(session['id'], {})
                    eligible = eligible_counts.get(session['key'], {'queued': 0, 'claimed': 0})
                    session['delivery'] = {'path': session['delivery_path'], 'consumer_required': True,
                        'auto_resume': False, 'queued': eligible['queued'], 'claimed': eligible['claimed'],
                        'last_signal': wake.get('last_signal'), 'last_error': wake.get('last_error'),
                        'signal_pending': wake.get('generation', 0) > wake.get('signaled', 0),
                        'consumer_status': 'claimed_unresolved' if eligible['claimed'] else 'awaiting_consumer' if eligible['queued'] else 'no_pending_events'}
            result['watches'] = sorted(watch_map.values(), key=lambda w: (w['state'] != 'running', -(w['started'] or 0)))
            result['sessions'] = sorted(sessions.values(), key=lambda s: (not s['running'], -(s['last_activity'] or 0)))
            summary.update(watches=len(watch_map), sessions=len(sessions))
            claim_rows = {r['event_id']: dict(r) for r in db.execute('SELECT c.event_id,c.batch_id,c.claimed_at FROM claude_claims c JOIN (SELECT id FROM events ORDER BY received DESC,id LIMIT 200) e ON e.id=c.event_id ORDER BY c.claimed_at')} if 'claude_claims' in tables else {}  # newest claim wins (P38 G4 history)
            event_rows = db.execute('SELECT * FROM events ORDER BY received DESC,id LIMIT ?', (EVENT_LIMIT+1,)).fetchall()
            result['limits']['events_truncated'] = len(event_rows) > EVENT_LIMIT
            for row in event_rows[:EVENT_LIMIT]:
                if row['watch_id'] not in watch_map:
                    continue
                w = watch_map[row['watch_id']]
                event = {key: row[key] for key in ('id', 'watch_id', 'channel', 'ts', 'author', 'received',
                                                   'status', 'receipt', 'disposition', 'action_ts')}
                root = db.execute('SELECT root FROM event_sources WHERE event_id=?', (row['id'],)).fetchone() if 'event_sources' in tables else None
                event.update(claim=claim_rows.get(event['id']), session_key=w['session_key'], provider=w['provider'], root=root[0] if root else w['thread'],
                             slack_url=slack_link(row['channel'], row['ts']))
                result['events'].append(event)
            runtime = db.execute('SELECT * FROM runtime WHERE id=1').fetchone() if 'runtime' in tables else None
            if runtime:
                heartbeat = runtime['heartbeat']
                age = max(0, now-heartbeat) if heartbeat else None
                alive = self.process_alive(runtime['pid'], runtime['birth'])
                version = db.execute("SELECT value FROM runtime_meta WHERE key='version'").fetchone() if 'runtime_meta' in tables else None
                result['supervisor'] = dict(pid=runtime['pid'], alive=alive, heartbeat=heartbeat,
                    heartbeat_age_seconds=age, version=version[0] if version else None,
                    health='offline' if not alive else 'stale' if age is None or age > 60 else 'healthy')
            self.avatars.enrich(db, result)
        result['providers'] = list(providers.values())
        return result

    def _today_start(self, now):
        local = dt.datetime.fromtimestamp(now, self.tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _public_run(self, db, path, row, now):
        events = json.loads(row['event_ids'] or '[]')
        handled, total = _event_counts(db, events)
        run_dir = _run_dir(path, row['run_id'], row['log_dir'])
        state = self.run_log.read_state(run_dir) if run_dir else {}
        keys = row.keys()
        return {
            'run_id': row['run_id'], 'dispatch_id': row['dispatch_id'], 'task': row['task'], 'provider': row['provider'],
            'session': row['session'], 'model': row['model'], 'effort': row['effort'], 'mode': row['mode'],
            'trigger': row['trigger'], 'attempt': row['attempt'], 'state': row['state'], 'outcome': row['outcome'],
            'error_class': row['error_class'], 'started_at': row['started_at'], 'ended_at': row['ended_at'],
            'elapsed_seconds': max(0, (row['ended_at'] or now) - (row['started_at'] or now)),
            'exit_code': row['exit_code'], 'handled': handled, 'events': total,
            'steps': int(state.get('steps') or 0), 'last_summary': state.get('last_summary'),
            'has_log': bool(run_dir), 'dispatch_state': row['dispatch_state'] if 'dispatch_state' in keys else None,
            'card_url': slack_link(row['card_channel'], row['card_ts']) if 'card_ts' in keys and row['card_ts'] else None,
            'fail_card_url': (slack_link(row['card_channel'], row['fail_card_ts'])
                              if 'fail_card_ts' in keys and row['fail_card_ts'] else None),
        }

    def list_runs(self, path, run_filter='all', task=None, now=None, limit=RUN_LIMIT):
        now = time.time() if now is None else now
        result = {'generated_at': now, 'filter': run_filter, 'task': task, 'runs': [], 'dispatches': [], 'tasks': [],
                  'counts': {'running': 0, 'failed': 0, 'today': 0, 'all': 0}}
        if run_filter not in RUN_FILTERS:
            raise ValueError('invalid_filter')
        if not Path(path).is_file():
            result['notice'] = 'state_database_missing'
            return result
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            if not {'consumer_runs', 'dispatches'} <= _tables(db):
                result['notice'] = 'dispatcher_tables_missing'
                return result
            where, args = [], []
            if run_filter == 'running':
                where.append("r.state='running'")
            elif run_filter == 'failed':
                where.append('r.outcome IN (%s)' % ','.join('?' * len(FAILED_OUTCOMES)))
                args += FAILED_OUTCOMES
            elif run_filter == 'today':
                where.append('r.started_at>=?')
                args.append(self._today_start(now))
            if task:
                where.append('r.task=?')
                args.append(task)
            sql = RUN_SELECT + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY r.started_at DESC LIMIT ?'
            result['runs'] = [self._public_run(db, path, row, now) for row in db.execute(sql, (*args, limit))]
            counts = result['counts']
            counts['all'] = db.execute('SELECT count(*) FROM consumer_runs').fetchone()[0]
            counts['running'] = db.execute("SELECT count(*) FROM consumer_runs WHERE state='running'").fetchone()[0]
            counts['failed'] = db.execute('SELECT count(*) FROM consumer_runs WHERE outcome IN (%s)'
                                          % ','.join('?' * len(FAILED_OUTCOMES)), FAILED_OUTCOMES).fetchone()[0]
            counts['today'] = db.execute('SELECT count(*) FROM consumer_runs WHERE started_at>=?',
                                         (self._today_start(now),)).fetchone()[0]
            result['tasks'] = [r[0] for r in db.execute('SELECT DISTINCT task FROM consumer_runs ORDER BY task')]
            dwhere, dargs = f"state IN ('waiting','running','paused','failed','needs_{self.key}')", []
            if task:
                dwhere += ' AND task_key=?'
                dargs.append(task)
            for d in db.execute(f'SELECT * FROM dispatches WHERE {dwhere} ORDER BY updated_at DESC LIMIT 50', dargs):
                held = db.execute("SELECT count(*) FROM events WHERE status='dispatched' AND receipt=?",
                                  ('dispatch:' + d['dispatch_id'],)).fetchone()[0]
                plan = json.loads(d['plan'] or '[]')
                step = plan[min(d['step'], len(plan) - 1)] if plan else {}
                result['dispatches'].append({
                    'dispatch_id': d['dispatch_id'], 'task': d['task_key'], 'state': d['state'], 'trigger': d['trigger'],
                    'reason': d['reason'], 'error_class': d['error_class'], 'held_events': held, 'run_id': d['run_id'],
                    'next_provider': step.get('provider'), 'next_mode': step.get('mode'), 'next_at': d['next_at'],
                    'updated_at': d['updated_at'], 'card_url': slack_link(d['card_channel'], d['card_ts']) if d['card_ts'] else None,
                    'fail_card_url': slack_link(d['card_channel'], d['fail_card_ts']) if d['fail_card_ts'] else None})
        return result

    def run_detail(self, path, run_id, now=None):
        now = time.time() if now is None else now
        if not RUN_ID_RE.fullmatch(str(run_id)):
            raise KeyError('run_not_found')
        if not Path(path).is_file():
            raise KeyError('run_not_found')
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            tables = _tables(db)
            if 'consumer_runs' not in tables:
                raise KeyError('run_not_found')
            row = db.execute(RUN_SELECT + ' WHERE r.run_id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError('run_not_found')
            result = self._public_run(db, path, row, now)
            events = json.loads(row['event_ids'] or '[]')
            envelopes = 'event_envelopes' in tables
            result['event_list'] = []
            for eid in events[:100]:
                e = db.execute('SELECT id,channel,ts,status,disposition,received FROM events WHERE id=?', (eid,)).fetchone()
                if e is None:
                    continue
                kind = db.execute('SELECT kind FROM event_envelopes WHERE event_id=?', (eid,)).fetchone() if envelopes else None
                result['event_list'].append({'id': e['id'], 'status': e['status'], 'disposition': e['disposition'],
                                             'received': e['received'], 'kind': kind[0] if kind else None,
                                             'slack_url': slack_link(e['channel'], e['ts'])})
            d = db.execute('SELECT * FROM dispatches WHERE dispatch_id=?', (row['dispatch_id'],)).fetchone()
            if d is not None:
                result['dispatch'] = {'dispatch_id': d['dispatch_id'], 'state': d['state'], 'trigger': d['trigger'],
                                      'reason': d['reason'], 'outcome': d['outcome'], 'error_class': d['error_class'],
                                      'card_stage': d['card_stage'],
                                      'runs': [r[0] for r in db.execute('SELECT run_id FROM consumer_runs WHERE dispatch_id=? '
                                                                        'ORDER BY started_at', (d['dispatch_id'],))]}
            if 'run_progress' in tables and row['dispatch_id']:
                prog = db.execute('SELECT posted_at,step,backoff_until,failures,stage FROM run_progress WHERE dispatch_id=?',
                                  (row['dispatch_id'],)).fetchone()
                result['progress'] = dict(prog) if prog else None
            if 'dispatch_actions' in tables and row['dispatch_id']:
                result['actions'] = [dict(r) for r in db.execute(
                    'SELECT action,actor,result,at FROM dispatch_actions WHERE dispatch_id=? ORDER BY at DESC LIMIT 20',
                    (row['dispatch_id'],))]
        run_dir = _run_dir(path, run_id, row['log_dir'])
        if run_dir:
            try:
                meta = json.loads((run_dir / 'meta.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                meta = {}
            result['cwd'] = meta.get('cwd')
            result['env_injected'] = [n for n in meta.get('env_injected') or [] if isinstance(n, str)]
            try:
                exit_info = json.loads((run_dir / 'exit.json').read_text())
                result['exit'] = {'exit_code': exit_info.get('exit_code'), 'stopped': bool(exit_info.get('stopped'))}
            except (OSError, ValueError):
                result['exit'] = None
        result['tail_command'] = f'{self.tail_command} {run_id}'
        return result

    def run_log_chunk(self, path, run_id, cursor=None, now=None):
        """Readable lines after `cursor` ('b:<byte offset>' of readable.log, or 'l:<line>' for runs that only
        have a raw stream, rendered in memory). Read-only: the dashboard never writes run files."""
        now = time.time() if now is None else now
        if not RUN_ID_RE.fullmatch(str(run_id)) or not Path(path).is_file():
            raise KeyError('run_not_found')
        with contextlib.closing(read_db(path)) as db:
            if 'consumer_runs' not in _tables(db):
                raise KeyError('run_not_found')
            row = db.execute('SELECT run_id,provider,state,outcome,log_dir,started_at,ended_at FROM consumer_runs '
                             'WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise KeyError('run_not_found')
        result = {'run_id': run_id, 'state': row['state'], 'outcome': row['outcome'], 'lines': [], 'cursor': cursor,
                  'source': None, 'complete': row['state'] != 'running', 'reset': False}
        run_dir = _run_dir(path, run_id, row['log_dir'])
        if run_dir is None:
            result['source'] = 'missing'
            return result
        readable = run_dir / self.run_log.READABLE
        kind, _, value = str(cursor or '').partition(':')
        if readable.exists():
            result['source'] = 'readable'
            if kind == 'b' and value.isdigit():
                text, offset = self.run_log.read_from(readable, int(value), LOG_CHUNK_BYTES)
                result['lines'] = text.splitlines()
            else:
                size = readable.stat().st_size
                start = max(0, size - LOG_CHUNK_BYTES)
                text, offset = self.run_log.read_from(readable, start, LOG_CHUNK_BYTES + 1)
                lines = text.splitlines()
                if start > 0 and lines:
                    lines = lines[1:]  # first line may be partial
                result['lines'] = lines[-LOG_INITIAL_LINES:]
                result['reset'] = True
            result['cursor'] = f'b:{offset}'
        else:
            events = run_dir / self.run_log.EVENTS
            lines = []
            if events.exists():
                fd = os.open(events, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    seen = min(now, os.fstat(stream.fileno()).st_mtime)
                    size = stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, size - LEGACY_RENDER_BYTES))
                    lines, _ = self.run_log.render_text(row['provider'], stream.read().decode('utf-8', 'replace'), seen)
            start = int(value) if kind == 'l' and value.isdigit() and int(value) <= len(lines) else None
            result['source'] = 'rendered'
            result['reset'] = start is None
            result['lines'] = lines[start:] if start is not None else lines[-LOG_INITIAL_LINES:]
            result['cursor'] = f'l:{len(lines)}'
        state = self.run_log.read_state(run_dir)
        result['steps'] = int(state.get('steps') or 0)
        result['last_summary'] = state.get('last_summary')
        return result

    def stale_backlog(self, path, now=None, max_age_hours=None):
        """Unhandled events the dispatcher's stale guard leaves alone: expired watch, or older than
        dispatch.max_event_age_hours on a watch that is not stopped. Metadata only, read-only."""
        now = time.time() if now is None else now
        if max_age_hours is None:
            try:
                max_age_hours = self.worker_config.load()[0]['dispatch']['max_event_age_hours']
            except (ValueError, KeyError, TypeError, OSError):
                max_age_hours = self.worker_config.DEFAULTS['dispatch']['max_event_age_hours']
        cutoff = now - max_age_hours * 3600
        result = {'generated_at': now, 'max_event_age_hours': max_age_hours, 'items': [], 'tasks': [], 'truncated': False,
                  'counts': {'expired_watch': 0, 'older_than_max_age': 0}}
        if not Path(path).is_file():
            result['notice'] = 'state_database_missing'
            return result
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            rows = db.execute('SELECT e.id,e.watch_id,e.channel,e.ts,e.received,e.status,w.data FROM events e '
                              'JOIN watches w ON w.id=e.watch_id WHERE e.status IN (%s) ORDER BY e.received'
                              % ','.join('?' * len(BACKLOG_STATUSES)), BACKLOG_STATUSES).fetchall()
        tasks = {}
        for row in rows:
            w = json.loads(row['data'])
            state = w.get('state')
            if state == 'stopped':
                continue
            if state == 'expired':
                reason = 'expired_watch'
            elif (row['received'] or now) < cutoff:
                reason = 'older_than_max_age'
            else:
                continue
            provider = str(w.get('provider') or 'codex')
            task = self.task_registry.task_id_for_cwd(w.get('cwd')) or f"session:{provider}:{str(w.get('session') or '')[:8]}"
            result['counts'][reason] += 1
            bucket = tasks.setdefault(task, {'task': task, 'count': 0, 'oldest': row['received'], 'newest': row['received']})
            bucket['count'] += 1
            bucket['oldest'] = min(bucket['oldest'], row['received'])
            bucket['newest'] = max(bucket['newest'], row['received'])
            if len(result['items']) >= BACKLOG_LIMIT:
                result['truncated'] = True
                continue
            result['items'].append({
                'event_id': row['id'], 'task': task, 'status': row['status'], 'reason': reason, 'received': row['received'],
                'age_seconds': max(0, now - (row['received'] or now)), 'slack_url': slack_link(row['channel'], row['ts']),
                'watch': {'id': row['watch_id'], 'state': state, 'channel': w.get('channel'), 'thread': w.get('thread'),
                          'mode': w.get('mode'), 'kind': w.get('kind', 'peer'), 'provider': provider,
                          'session': str(w.get('session') or '')[:8], 'expired_at': w.get('expired_at'),
                          'stop_reason': w.get('stop_reason'), 'smoke': bool(w.get('smoke')),
                          'slack_url': slack_link(w.get('channel'), w.get('thread'))}})
        result['tasks'] = sorted(tasks.values(), key=lambda t: (-t['count'], t['oldest']))
        result['total'] = sum(result['counts'].values())
        return result

    def intake_overview(self, path, now=None):
        """收件 page: metadata only (ids, timestamps, routes, counts, fingerprints); never message bodies."""
        now = time.time() if now is None else now
        result = {'generated_at': now, 'state': {}, 'bursts': [], 'triage': [], 'bindings': [], 'filtered_24h': {},
                  'counts': {}, 'config': None, 'config_error': None, 'channels': [], 'tasks': {}}
        try:
            config = self.worker_config.load()[0]
            intake = config['intake']
            result['config'] = intake
            def channel_id(entry):
                value = str(self.known_channels.get(str(entry).lower(), entry))
                return value if re.fullmatch(r'[CG][A-Z0-9]{2,}', value) else None
            oncall_ids = {channel_id(c) for c in intake['oncall_channels']} - {None}
            result['channels'] = [{'entry': c, 'id': channel_id(c), 'oncall': channel_id(c) in oncall_ids}
                                  for c in intake['channels']]
        except (ValueError, KeyError, OSError) as exc:
            result['config_error'] = str(exc)[:200]
            intake = None
        if not Path(path).is_file():
            result['notice'] = 'state_database_missing'
            return result
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            tables = _tables(db)
            if 'intake_bursts' not in tables:
                result['notice'] = 'intake_tables_missing'
                return result
            row = db.execute("SELECT value FROM runtime_meta WHERE key='intake_state'").fetchone()
            state = _loads(row[0] if row else None, {})
            hour = sum(c for at, c in state.get('calls_last_hour') or [] if isinstance(at, (int, float)) and at > now - 3600)
            result['state'] = {k: state.get(k) for k in ('status', 'baseline', 'next_cycle_at', 'covered_until', 'errors',
                                                          'last_error', 'last_error_at', 'cycles', 'rate_limited',
                                                          'unresolved_channels')}
            result['state'].update(last_cycle=state.get('last_cycle'), backoff=state.get('backoff') or {},
                                   api_calls_last_hour=hour)
            result['counts'] = {r[0]: r[1] for r in db.execute('SELECT state,count(*) FROM intake_bursts GROUP BY state')}
            result['filtered_24h'] = {r[0]: r[1] for r in db.execute(
                'SELECT reason,count(*) FROM intake_seen WHERE seen_at>? GROUP BY reason', (now - 86400,))}
            records = {}
            for r in db.execute('SELECT * FROM triage_records ORDER BY created_at DESC LIMIT ?', (INTAKE_LIMIT,)):
                item = {k: r[k] for k in ('id', 'burst_id', 'sender', 'channel', 'intent', 'clarity', 'disposition',
                                          'target_task', 'confidence', 'basis', 'person_style', 'note', 'recorded_by',
                                          'created_at', 'corrected_at')}
                item[f'needs_{self.key}'] = bool(r[f'needs_{self.key}'])
                item['smoke'] = bool(r['smoke'])
                item['correction'] = _loads(r['correction'], {}) or None
                result['triage'].append(item)
                records.setdefault(r['burst_id'], []).append(r['disposition'])
            for b in db.execute('SELECT b.*, e.status AS event_status FROM intake_bursts b LEFT JOIN events e ON '
                                'e.id=b.event_id ORDER BY b.created_at DESC LIMIT ?', (INTAKE_LIMIT,)):
                root = b['thread_root']
                result['bursts'].append({
                    'burst_id': b['burst_id'], 'channel': b['channel'], 'conv_type': b['conv_type'], 'thread_root': root,
                    'sender': b['sender'], 'sender_kind': b['sender_kind'], 'msg_count': b['msg_count'],
                    'first_ts': b['first_ts'], 'last_ts': b['last_ts'], 'state': b['state'], 'route': b['route'],
                    'route_reason': b['route_reason'], 'target_task': b['target_task'], 'handled_task': b['handled_task'],
                    'event_status': b['event_status'], 'person_style': b['person_style'],
                    'auto_execute': None if b['auto_execute'] is None else bool(b['auto_execute']),
                    'alert_fp': b['alert_fp'], 'alert_service': b['alert_service'], 'alert_state': b['alert_state'],
                    'dup_count': b['dup_count'], 'task_refs': _loads(b['task_refs'], []), 'pr_refs': _loads(b['pr_refs'], []),
                    'error': b['error'], 'smoke': bool(b['smoke']), 'created_at': b['created_at'], 'routed_at': b['routed_at'],
                    'triage': records.get(b['burst_id'], []),
                    'slack_url': None if b['smoke'] else slack_link(b['channel'], root or b['first_ts'])})
            for r in db.execute('SELECT * FROM intake_bindings ORDER BY created_at DESC LIMIT 100'):
                result['bindings'].append({'channel': r['channel'], 'thread_root': r['thread_root'] or None,
                                           'task_id': r['task_id'], 'burst_id': r['burst_id'], 'watch_id': r['watch_id'],
                                           'expires_at': r['expires_at'], 'error': r['error'], 'created_at': r['created_at'],
                                           'active': r['expires_at'] is None or r['expires_at'] > now,
                                           'slack_url': slack_link(r['channel'], r['thread_root'] or None)})
            if intake:
                for role in ('inbox', 'oncall'):
                    task_id = intake[f'{role}_task']
                    row = db.execute('SELECT current_provider,current_session,state FROM tasks WHERE task_id=?',
                                     (task_id,)).fetchone() if 'tasks' in tables else None
                    result['tasks'][role] = {'task_id': task_id, 'registered': row is not None,
                                             'current_provider': row['current_provider'] if row else None,
                                             'current_session': (row['current_session'] or '')[:8] if row else None}
        return result

    def _inbox_tasks(self):
        try:
            intake = self.worker_config.load()[0]['intake']
            return tuple(x for x in (intake.get('inbox_task'), intake.get('oncall_task')) if x) or self.trace.INBOX_TASKS
        except (ValueError, KeyError, OSError):
            return self.trace.INBOX_TASKS

    def inbox_items(self, path, days=_OWNER_DEFAULT, now=None):
        """P48: every inbox item of the last `days` days with its pipeline (trace summaries; run logs are not read)."""
        days = self.trace.LIST_DAYS if days is _OWNER_DEFAULT else days
        now = time.time() if now is None else now
        if not Path(path).is_file():
            return []
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            return self.trace.inbox(self.trace.Store(db, now, None, False, self._inbox_tasks()), days)

    def inbox_view(self, path, items, view='all', task=None, q=None, now=None):
        """/api/inbox: the counts per filter, the tasks items went to, and the items of one view (or of a search)."""
        counts = {v: sum(self.trace.in_view(x, v) for x in items) for v in self.trace.VIEWS}
        tasks = {}
        for x in items:
            if self.trace.in_view(x, 'all') or self.trace.in_view(x, 'agent'):
                for t in x.get('tasks') or ():
                    tasks[t] = tasks.get(t, 0) + 1
        result = {'view': view, 'task': task, 'q': q, 'counts': counts, 'search': None, 'generated_at': now or time.time(),
                  'tasks': [{'task_id': t, 'count': n} for t, n in sorted(tasks.items(), key=lambda kv: (-kv[1], kv[0]))]}
        if q:
            parsed = self.trace.parse_ref(q)
            found = []
            if parsed and parsed['kind'] != 'task':
                with contextlib.closing(read_db(path)) as db:
                    db.execute('BEGIN')
                    store = self.trace.Store(db, now, None, False, self._inbox_tasks())
                    found = [self.trace.summary(self.trace.build(store, x)) for x in self.trace.resolve(store, parsed)[:50]]
            elif parsed:
                found = [x for x in items if parsed['id'] in (x.get('tasks') or ())]
            if not found:  # a name, a channel, part of a task id: matched against the metadata the list already shows
                needle = q.lower()
                found = [x for x in items if needle in ' '.join(str(v) for v in (
                    x.get('ref'), x.get('channel'), x.get('where'), (x.get('who') or {}).get('name'), (x.get('who') or {}).get('id'),
                    ' '.join(x.get('tasks') or ()), x.get('headline'))).lower()]
            result['search'] = {'kind': (parsed or {}).get('kind'), 'found': len(found)}
            picked = found
        else:
            picked = [x for x in items if self.trace.in_view(x, view) and (not task or task in (x.get('tasks') or ()))]
        result.update(items=picked[:INBOX_LIMIT], total=len(picked))
        return result

    def trace_detail(self, path, ref, now=None):
        """/api/trace: one item's stages and steps (KeyError: nothing matches; ValueError: not a reference)."""
        if not Path(path).is_file():
            raise KeyError('trace_not_found')
        return self.trace.trace_ref(path, ref, now=now, runs_root=runs_root(path), inbox_tasks=self._inbox_tasks())

    def _approval(self, row):
        """Approval label of a rule check from its joined decision (same rule as rule-status, read-only)."""
        if row['outcome'] in ('auto', 'route'):
            return 'not_required'
        if row['outcome'] == 'clarify':
            return 'clarify_first'
        if not row['card_event']:
            return 'not_requested'
        if row['d_choice'] is None:
            return 'pending'
        try:
            kind = self.decision_buttons.resolve_choice(row['d_choice'], self.decision_buttons.card_model(row['c_options']))['kind']
        except (ValueError, TypeError):
            return 'unknown'
        if kind != 'approve':
            return 'rejected'
        if row['d_actor'] not in _loads(row['approvers'], []):
            return 'approver_not_in_rules'
        if row['recipient_slack'] and row['d_actor'] == row['recipient_slack']:
            return 'self_approval_refused'
        return 'approved_with_condition' if row['d_note'] else 'approved'

    def _week_start(self, ts):
        local = dt.datetime.fromtimestamp(ts, self.tz)
        return (local - dt.timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def rules_overview(self, path, now=None):
        """规则 page: aggregates and ids only. Never plans, emails, card text or message text."""
        now = time.time() if now is None else now
        result = {'generated_at': now, 'spec': None, 'spec_error': None, 'config': None, 'weeks': [], 'recent': [],
                  'proposals': [], 'backtest': None, 'totals': {}, 'changelog': []}
        try:
            spec, info = self.rule_engine.load_active('access')
            levels = {}
            for rule in spec['rules']:
                levels[rule['level']] = levels.get(rule['level'], 0) + 1
            result['spec'] = {'category': 'access', 'version': info['version'], 'file': info['file'], 'digest': info['digest'],
                              'rules': len(spec['rules']), 'levels': levels,
                              'never_auto': {k: v['level'] for k, v in spec['never_auto'].items()},
                              'claims': {r['id']: r['claims'] for r in spec['rules'] if r.get('claims')}}
            result['changelog'] = [{k: c.get(k) for k in ('at', 'kind', 'from', 'to', 'proposal', 'from_version', 'to_version',
                                                          'who', 'title_zh')} for c in self.rule_engine.read_changelog(info['dir'], 10)]
        except (ValueError, OSError) as exc:
            result['spec_error'] = str(exc)[:200]
        try:
            result['config'] = self.worker_config.load()[0]['rules']
        except (ValueError, KeyError, OSError) as exc:
            result['config_error'] = str(exc)[:200]
        result['registry'] = self.decision_buttons.rule_registry.overview()  # P47: the rule list (Chinese), live or seed
        if not Path(path).is_file():
            result['notice'] = 'state_database_missing'
            return result
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            tables = _tables(db)
            if 'rule_checks' not in tables:
                result['notice'] = 'rules_tables_missing'
                return result
            rows = db.execute('SELECT r.*, d.choice AS d_choice, d.actor AS d_actor, d.decided_at AS d_decided, d.note AS d_note, '
                              'c.options AS c_options FROM rule_checks r LEFT JOIN decisions d ON d.event_id=r.card_event '
                              'LEFT JOIN escalation_cards c ON c.event_id=r.card_event ORDER BY r.created_at DESC').fetchall()
            first_week = self._week_start(now) - (RULES_WEEKS - 1) * 7 * 86400
            weeks = {first_week + n * 7 * 86400: {'checks': 0, 'outcomes': {o: 0 for o in self.rule_engine.OUTCOMES}, 'latency': [],
                                                  'overrides': 0, 'executed': 0} for n in range(RULES_WEEKS)}
            totals = {'checks': 0, 'outcomes': {o: 0 for o in self.rule_engine.OUTCOMES}, 'approvals': {}, 'overrides': 0}
            for row in rows:
                approval = self._approval(row)
                if not row['smoke']:
                    totals['checks'] += 1
                    totals['outcomes'][row['outcome']] = totals['outcomes'].get(row['outcome'], 0) + 1
                    totals['approvals'][approval] = totals['approvals'].get(approval, 0) + 1
                    overridden = approval == 'rejected' or bool(row['correction'])
                    totals['overrides'] += int(overridden)
                    week = weeks.get(self._week_start(row['created_at']))
                    if week is not None:
                        week['checks'] += 1
                        week['outcomes'][row['outcome']] = week['outcomes'].get(row['outcome'], 0) + 1
                        week['overrides'] += int(overridden)
                        week['executed'] += int(row['result'] == 'executed')
                        if row['d_decided'] is not None:
                            week['latency'].append(row['d_decided'] - (row['card_posted_at'] or row['created_at']))
                if len(result['recent']) < RULES_RECENT:
                    rules = [r for r in _loads(row['matched_rules'], []) if r not in ('AR-01', 'AR-02', 'AR-03', 'AR-04', 'AR-05')]
                    result['recent'].append({'id': row['id'], 'created_at': row['created_at'], 'outcome': row['outcome'],
                                             'rules': rules, 'system': row['system'], 'action': row['action'],
                                             'kind': row['kind'], 'task_id': row['task_id'], 'approval': approval,
                                             'result': row['result'], 'corrected': bool(row['correction']),
                                             'never_auto': [x.get('shape') for x in _loads(row['never_auto'], [])],
                                             'smoke': bool(row['smoke'])})
            for start, week in sorted(weeks.items()):
                n, o = week['checks'], week['outcomes']
                tier_auto = o.get('auto', 0) + o.get('clarify', 0) + o.get('route', 0)
                result['weeks'].append({'start': start, 'checks': n, 'outcomes': o, 'overrides': week['overrides'],
                                        'executed': week['executed'],
                                        'shares': {'auto': round(o.get('auto', 0) / n, 4) if n else None,
                                                   'auto_tier': round(tier_auto / n, 4) if n else None,
                                                   'auto_or_one_tap': round((tier_auto + o.get('one_tap', 0)) / n, 4) if n else None},
                                        'approval_latency': {'median': _pct(week['latency'], 0.5), 'p90': _pct(week['latency'], 0.9),
                                                             'n': len(week['latency'])}})
            result['totals'] = totals
            if 'rule_proposals' in tables:
                result['proposals'] = [{k: r[k] for k in ('id', 'kind', 'category', 'title_zh', 'status', 'base_version',
                                                          'applied_version', 'created_at', 'decided_at', 'applied_at', 'error',
                                                          'suppress_until')} | {'smoke': bool(r['smoke'])}
                                       for r in db.execute('SELECT * FROM rule_proposals ORDER BY created_at DESC LIMIT 30')]
            meta = db.execute('SELECT value FROM runtime_meta WHERE key=?', (self.rule_engine.BACKTEST_META_KEY,)).fetchone()
            result['backtest'] = _loads(meta[0] if meta else None, {}) or None
        return result

    def _active_spec(self):
        try:
            return self.rule_engine.load_active('access')[0]
        except (ValueError, OSError):
            return None

    def rule_check_detail(self, path, check_id, now=None):
        """/rules?check=<id> (P3.1): the technical record behind a decision card — the agent's sentence, matched rules,
        failed checks, never-auto shapes, facts, approvers, the plan, approval and outcome. Cards in Slack carry only a
        plain sentence and link here. Raises KeyError for an unknown or malformed id."""
        now = time.time() if now is None else now
        if not self.rule_engine.CHECK_ID_RE.fullmatch(str(check_id)) or not Path(path).is_file():
            raise KeyError('check_not_found')
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            if 'rule_checks' not in _tables(db):
                raise KeyError('check_not_found')
            row = db.execute('SELECT r.*, d.choice AS d_choice, d.actor AS d_actor, d.decided_at AS d_decided, d.note AS d_note, '
                             'd.provenance AS d_provenance, c.options AS c_options FROM rule_checks r LEFT JOIN decisions d ON '
                             'd.event_id=r.card_event LEFT JOIN escalation_cards c ON c.event_id=r.card_event WHERE r.id=?',
                             (check_id,)).fetchone()
            if row is None:
                raise KeyError('check_not_found')
            approval = {'status': self._approval(row), 'actor': row['d_actor'], 'decided_at': row['d_decided'],
                        'note': row['d_note'], 'provenance': row['d_provenance']}
            check = self.rule_engine.check_record(db, check_id, approval, self._active_spec())
        if check['card']:
            check['card']['slack_url'] = slack_link(check['card']['channel'], check['card']['ts'])
        return {'generated_at': now, 'check': check}

    def rule_proposal_detail(self, path, proposal_id, now=None):
        """/rules?proposal=<id> (P3.1): a rule proposal's title, reason, exact text or patch lines and evidence."""
        now = time.time() if now is None else now
        if not self.rule_engine.PROPOSAL_ID_RE.fullmatch(str(proposal_id)) or not Path(path).is_file():
            raise KeyError('proposal_not_found')
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            if 'rule_proposals' not in _tables(db):
                raise KeyError('proposal_not_found')
            try:
                proposal = self.rule_engine.proposal_record(db, proposal_id, self._active_spec())
            except ValueError:
                raise KeyError('proposal_not_found') from None
        return {'generated_at': now, 'proposal': proposal}

    def routines_overview(self, path, now=None):
        """定时 page: routine definitions (schedule, scope, short prompt), next / last runs, provenance, the owner's
        confirmations of hand-written definitions, proposals."""
        now = time.time() if now is None else now
        result = {'generated_at': now, 'config': None, 'routines': [], 'errors': [], 'runs': [], 'proposals': [],
                  'confirmations': [], 'counts': {}}
        try:
            result['config'] = self.worker_config.load()[0]['routines']
        except (ValueError, KeyError, OSError) as exc:
            result['config_error'] = str(exc)[:200]
        if not Path(path).is_file():
            result['notice'] = 'state_database_missing'
            return result
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            tables = _tables(db)
            if 'routine_state' not in tables:
                result['notice'] = 'routines_tables_missing'
                return result
            per_task = {r[0]: r[1] for r in db.execute("SELECT task_id, count(*) FROM routine_runs WHERE state='fired' AND "
                                                       'fired_at>? GROUP BY task_id', (now - 86400,))}
            confirmations = {(c['task_id'], c['routine_id'], c['digest']): c for c in db.execute(
                'SELECT * FROM routine_confirmations')} if 'routine_confirmations' in tables else {}
            for r in db.execute('SELECT * FROM routine_state ORDER BY task_id, routine_id'):
                if r['routine_id'] == '*':
                    result['errors'].append({'task_id': r['task_id'], 'file': r['file'], 'error': r['error'],
                                             'updated_at': r['updated_at']})
                    continue
                entry = _loads(r['definition'], {})
                try:
                    when = self.routines.human(entry)
                except (ValueError, KeyError):
                    when = entry.get('schedule') or entry.get('every')
                result['counts'][r['state']] = result['counts'].get(r['state'], 0) + 1
                prompt = str(entry.get('prompt') or '')
                c = confirmations.get((r['task_id'], r['routine_id'], r['digest'])) if r['provenance'] == f'{self.key}_authored' else None
                result['routines'].append({
                    'task_id': r['task_id'], 'routine_id': r['routine_id'], 'state': r['state'], 'provenance': r['provenance'],
                    'when': when, 'schedule': entry.get('schedule'), 'every': entry.get('every'), 'scope': entry.get('scope'),
                    'prompt': prompt[:160] + ('…' if len(prompt) > 160 else ''), 'enabled': entry.get('enabled', True),
                    'until': entry.get('until'), 'provider': entry.get('provider'), 'next_at': r['next_at'],
                    'last_fired_at': r['last_fired_at'], 'error': r['error'], 'runs_24h_task': per_task.get(r['task_id'], 0),
                    'approved_by': entry.get('approved_by'), 'smoke': self.task_registry.is_smoke_task(r['task_id']),
                    'confirmation': {'status': c['status'], 'via': c['via'], 'decided_at': c['decided_at'],
                                     'card': bool(c['card_event']), 'error': c['error']} if c else None})
            for r in db.execute('SELECT x.*, e.status AS event_status, d.state AS dispatch_state FROM routine_runs x '
                                'LEFT JOIN events e ON e.id=x.event_id LEFT JOIN dispatches d ON d.dispatch_id=x.dispatch_id '
                                'ORDER BY x.fired_at DESC LIMIT 100'):
                result['runs'].append({k: r[k] for k in ('id', 'task_id', 'routine_id', 'slot', 'fired_at', 'trigger', 'state',
                                                         'delivery', 'event_id', 'dispatch_id', 'event_status', 'dispatch_state',
                                                         'error')} | {'smoke': bool(r['smoke'])})
            if 'routine_proposals' in tables:
                result['proposals'] = [{k: r[k] for k in ('id', 'task_id', 'routine_id', 'status', 'proposer', 'decided_by',
                                                          'created_at', 'decided_at', 'error')} | {'smoke': bool(r['smoke'])}
                                       for r in db.execute('SELECT * FROM routine_proposals ORDER BY created_at DESC LIMIT 30')]
            result['confirmations'] = [{k: r[k] for k in ('task_id', 'routine_id', 'digest', 'status', 'via', 'decided_by',
                                                          'created_at', 'decided_at', 'error')} |
                                       {'card': bool(r['card_event']), 'smoke': bool(r['smoke'])}
                                       for r in db.execute('SELECT * FROM routine_confirmations ORDER BY updated_at DESC '
                                                           'LIMIT 30')] if confirmations else []
        return result

    def gaps_overview(self, path, now=None):
        """缺口 page: capability blockers and the automation gap ledger. System / operation / type / status and counts
        only; never `tried` / `unblock` / notes (those are on the detail view) and never codes (not stored anywhere)."""
        now = time.time() if now is None else now
        result = {'generated_at': now, 'config': None, 'counts': {}, 'by_system': [], 'by_type': {}, 'by_resolution': {},
                  'weeks': [], 'top_paths': [], 'open': [], 'recent': [], 'proposals': []}
        try:
            result['config'] = self.worker_config.load()[0]['blockers']
        except (ValueError, KeyError, OSError) as exc:
            result['config_error'] = str(exc)[:200]
        if not Path(path).is_file():
            result['notice'] = 'state_database_missing'
            return result
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            if 'automation_gaps' not in _tables(db):
                result['notice'] = 'gaps_tables_missing'
                return result
            result.update(self.blockers.dashboard_overview(db, now))
        return result

    def learning_overview(self, path, now=None):
        """学习 page (P35): what the system learned from the owner's card feedback. Aggregates only — counts by shape, override
        rate, promote cards and their outcomes, adoption. NEVER raw feedback, the distillation text, or card text; shapes
        are shown as safe labels (card type + system / category)."""
        now = time.time() if now is None else now
        result = {'generated_at': now, 'config': None, 'counts': {}, 'weeks': [], 'shapes': [], 'promotes': []}
        try:
            result['config'] = self.worker_config.load()[0]['feedback_learning']
        except (ValueError, KeyError, OSError) as exc:
            result['config_error'] = str(exc)[:200]
        if not Path(path).is_file():
            result['notice'] = 'state_database_missing'
            return result
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            if 'interaction_records' not in _tables(db):
                result['notice'] = 'learning_tables_missing'
                return result
            result.update(self.feedback_learning.aggregate(db, now))
        return result

    def cache_overview(self, path, now=None):
        """健康 page cache panel (P40): `cache_report` over the last 24 h, summary rows only (no per-run list, never message
        bodies), memoized per database for CACHE_REPORT_SECONDS."""
        now = time.time() if now is None else now
        key = str(Path(path).resolve())
        hit = self._cache_memo.get(key)
        if hit and now - hit[0] < CACHE_REPORT_SECONDS:
            return hit[1]
        if not Path(path).is_file():
            return {'notice': 'state_database_missing', 'summary': [], 'overall': None}
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            result = self.cache_report.report(db, since_hours=24, limit=0, now=now)
        # P40.1: the keep-warm refreshes as their own summary rows (never counted as runs; no per-refresh list here).
        result = {**{k: result[k] for k in ('generated_at', 'since_hours', 'runs_total', 'summary', 'overall')},
                  'keep_warm': {k: result['keep_warm'][k] for k in ('refreshes_total', 'summary')}}
        self._cache_memo[key] = (now, result)
        return result

    def health_overview(self, path, now=None):
        """健康 page (P38): per-provider status with the rolling-window counts (runs / adverse / overload / failed / slow /
        retried, per model), recent transitions and notices, tasks displaced from their preferred provider, stranded
        deliveries, stuck claims and (F49) refused the owner-facing posts. Counts, ids and states only — never message bodies or
        run logs."""
        now = time.time() if now is None else now
        result = {'generated_at': now, 'config': None, 'providers': None, 'stranded': None, 'stuck_claims': None,
                  'card_destination': None, 'provider_wait': None}
        try:
            config = self.worker_config.load()[0]
        except (ValueError, OSError) as exc:
            config = self.worker_config.DEFAULTS
            result['config_error'] = str(exc)[:200]
        result['config'] = {key: config['dispatch'][key] for key in (
            'provider_health', 'long_lived_fresh', 'long_lived_tasks', 'max_session_context_mb', 'stranded_revive',
            'stuck_claim_minutes', 'provider_wait_hours', 'probe_enabled', 'probe_min_seconds', 'probe_max_seconds')}
        if not Path(path).is_file():
            result['notice'] = 'state_database_missing'
            return result
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            tables = _tables(db)
            if 'runtime_meta' in tables:  # P38 (F49): card-destination refusals need no health table
                result['card_destination'] = self.decision_buttons.destination_overview(db, now)
            if {'runtime_meta', 'dispatches'} <= tables:  # P44: probes + dispatches waiting for a provider
                result['provider_wait'] = self.dispatcher.provider_wait_overview(db, now)
            if not {'provider_health', 'stuck_claims', 'stranded_deliveries'} <= tables:
                result['notice'] = 'health_tables_missing'
                return result
            result['providers'] = self.provider_health.overview(db, self.worker_config, config, now)
            result['stranded'] = self.dispatcher.stranded_overview(db, now)
            result['stuck_claims'] = self.stuck_claims.overview(db, self.claude_delivery, config['dispatch']['stuck_claim_minutes'],
                                                           now)
        return result

    def gap_detail(self, path, gap_id, now=None):
        """/gaps/<id>: one blocker with its `tried` / `unblock` text (agent-written, secret-checked), card refs and
        timeline. Raises KeyError for an unknown or malformed id."""
        now = time.time() if now is None else now
        if not self.blockers.GAP_ID_RE.fullmatch(str(gap_id)) or not Path(path).is_file():
            raise KeyError('gap_not_found')
        with contextlib.closing(read_db(path)) as db:
            db.execute('BEGIN')
            if 'automation_gaps' not in _tables(db):
                raise KeyError('gap_not_found')
            try:
                gap = self.blockers.gap_detail(db, gap_id, now)['gap']
            except ValueError:
                raise KeyError('gap_not_found') from None
        gap['cards'] = [{**c, 'slack_url': slack_link(c['channel'], c['ts'])} for c in gap['cards']]
        return {'generated_at': now, 'gap': gap}

    def design_refresh_command(self, path):
        """The exact shell command that (re)generates the design document at `path` from the task workspace's Markdown
        with the locally inlined mermaid bundle (never `--cdn`: the iframe's CSP blocks every network fetch)."""
        target = Path(path)
        return '\n'.join((
            f'W={self.design_workspace}',
            f'mkdir -p -m 700 {shlex.quote(str(target.parent))}',
            'python3 -I "$W/scripts/design-doc/build_review_html.py" "$W/runbooks/architecture-design.md" \\',
            f'  "$W/scripts/design-doc/mermaid-11.17.2.min.js" {shlex.quote(str(target))}'))

    def design_status(self, path, now=None):
        """/api/design: whether the generated design document is there to serve (`exists`; otherwise `error` says why),
        its size, mtime (= generated time), the first 12 hex of its sha256 and how many src / href attributes point at
        http(s) URLs (`external_refs`; `external_src_refs` = the src ones, i.e. resources the CSP will block)."""
        now = time.time() if now is None else now
        result = {'generated_at': now, 'path': str(path), 'exists': False, 'error': None, 'size': None, 'mtime': None,
                  'sha256': None, 'external_refs': None, 'external_src_refs': None, 'max_bytes': DESIGN_MAX_BYTES,
                  'doc_url': '/design/doc', 'refresh_command': self.design_refresh_command(path)}
        try:
            st, data = read_design_doc(path)
        except DesignDocError as exc:
            result['error'] = exc.code
            return result
        refs = [m[1].lower() for m in DESIGN_REF_RE.finditer(data)]
        result.update(exists=True, size=len(data), mtime=st.st_mtime, sha256=hashlib.sha256(data).hexdigest()[:12],
                      external_refs=len(refs), external_src_refs=refs.count(b'src'))
        return result

    def settings_state(self, token):
        path = self.worker_config.config_path()
        result = {'path': str(path), 'log_path': str(self.worker_config.log_path(path)), 'defaults': self.worker_config.DEFAULTS,
                  'editable': list(EDITABLE_KEYS), 'page_token': token,
                  'enums': {'efforts': list(self.worker_config.EFFORTS), 'providers': list(self.worker_config.PROVIDERS)},
                  'log': self.worker_config.change_log(path, limit=60)}
        try:
            config, source = self.worker_config.load(path)
        except ValueError as exc:
            result.update(ok=False, error=str(exc), config=None, source=None, digest=None)
            return result
        result.update(ok=True, config=config, source=source, digest=self.worker_config.digest(config))
        return result

    def save_settings(self, payload, now=None):
        """Validate + write through worker_config.set_many (actor "dashboard"); raises ValueError codes."""
        changes, digest, dry_run = _settings_changes(payload)
        return self.worker_config.set_many(changes, 'dashboard', now=now, dry_run=dry_run, expect_digest=digest)

    # ---- Phase 2 C1: the control entry (kage.core.control) -------------------------------------------------------------
    def _control(self):
        return _control.Control(self.language, owner_key=self.key, owner_name=self.name)

    def _control_config(self):
        try:
            return self.worker_config.load()[0], None
        except ValueError as exc:
            return {}, str(exc)

    def control_state(self, path, conversation=None, token=None, words=None):
        """/api/control: the switch, model and limits, the conversations, one conversation's messages (text, state,
        request facts, relays). Read-only. `words` (C3): a Slack conversation's words re-read from Slack, memory only."""
        config, error = self._control_config()
        db = read_db(path) if Path(path).is_file() else None
        try:
            out = self._control().overview(db, config, conversation, words=words)
        finally:
            if db is not None:
                db.close()
        out.update(config_error=error, digest=self.worker_config.digest(config) if config else None,
                   enums={'providers': list(self.worker_config.PROVIDERS), 'efforts': list(self.worker_config.EFFORTS)})
        slack = (out.get('conversation') or {}).get('slack')
        if slack:  # C2: where the conversation lives (its Slack text is never stored here)
            slack['link'] = slack_link(slack['channel'], slack['root'])
        if token:
            out['page_token'] = token
        return out

    def _control_db(self, path):
        db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def control_submit(self, path, payload, facts, now=None):
        """POST /api/control/messages {text, client_id, conversation?}: one owner message (kage.core.control.submit)."""
        if set(payload) - {'text', 'client_id', 'conversation'} or 'text' not in payload or 'client_id' not in payload:
            raise ValueError('invalid_request_body')
        config, error = self._control_config()
        if error:
            raise ValueError('control_off')
        with contextlib.closing(self._control_db(path)) as db:
            message = self._control().submit(db, config, payload['text'], client_id=payload['client_id'],
                                             conversation=payload.get('conversation'), facts=facts, now=now)
        return {'ok': True, 'message': message}

    def control_retry(self, path, payload, now=None):
        """POST /api/control/retry {message}: a failed owner message queued again."""
        if set(payload) != {'message'}:
            raise ValueError('invalid_request_body')
        config, error = self._control_config()
        if error:
            raise ValueError('control_off')
        with contextlib.closing(self._control_db(path)) as db:
            return {'ok': True, 'message': self._control().retry(db, config, payload['message'], now=now)}

    def control_save(self, payload, now=None):
        """POST /api/control/settings {changes: {enabled?, provider?, model?, effort?}, digest}: control.* through
        worker_config.set_many (actor "dashboard"), like the settings page."""
        changes, digest = payload.get('changes'), payload.get('digest')
        if set(payload) != {'changes', 'digest'} or not isinstance(changes, dict) or not changes or \
                set(changes) - set(CONTROL_EDITABLE) or not isinstance(digest, str) or \
                not re.fullmatch(r'[0-9a-f]{16}', digest):
            raise ValueError('invalid_request_body')
        result = self.worker_config.set_many([(f'control.{k}', v) for k, v in sorted(changes.items())], 'dashboard',
                                             now=now, expect_digest=digest)
        config, _ = self._control_config()
        return {**result, 'digest': self.worker_config.digest(config) if config else None}

    def stop_watch(self, path, watch_id):
        # Existing-file mode prevents creating a new database on an incorrect path.
        db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, timeout=3)
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM watches WHERE id=?', (watch_id,)).fetchone()
            if row is None:
                raise KeyError('watch_not_found')
            w = json.loads(row[0])
            if w.get('state') != 'stopped' or w.get('stop_reason') in ('kill_switch', 'retired'):
                # P38: the owner's stop of a watch the approvals kill switch stopped makes it his (sticky) stop, never resumed.
                # P39: so does his stop of a thread the reconcile retired (a new card would re-arm it, the router feed it).
                w.update(state='stopped', stop_reason='explicit_stop', stopped_at=time.time(), stopped_via='local_dashboard')
                db.execute('UPDATE watches SET data=? WHERE id=?', (json.dumps(w, ensure_ascii=False), watch_id))
            if w.get('provider') == 'claude' and db.execute("SELECT 1 FROM sqlite_master WHERE name='claude_wakes'").fetchone():
                self.claude_delivery.resolved(db, w['session'])
            db.commit()
            return {'ok': True, 'watch_id': watch_id, 'state': 'stopped'}
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def stop_session(self, path, provider, session_id):
        """Stop the exact provider/session's currently running watches atomically."""
        db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, timeout=3)
        try:
            db.execute('BEGIN IMMEDIATE')
            matched, stopped = False, []
            now = time.time()
            # Scan the database, not the dashboard's capped or filtered snapshot.
            for wid, raw in db.execute('SELECT id,data FROM watches').fetchall():
                w = json.loads(raw)
                if str(w.get('provider') or 'codex') != provider or w.get('session') != session_id:
                    continue
                matched = True
                if w.get('state') != 'running' or w.get('kind') == 'approvals':
                    # P38: an approvals thread belongs to its task (it follows the task's newest session): stopping one
                    # session must not stop the task's threads for good — stop such a thread on its own (stop_watch).
                    continue
                w.update(state='stopped', stop_reason='explicit_stop', stopped_at=now,
                         stopped_via='local_dashboard_session')
                db.execute('UPDATE watches SET data=? WHERE id=?', (json.dumps(w, ensure_ascii=False), wid))
                stopped.append(wid)
            if not matched:
                raise KeyError('session_not_found')
            if provider == 'claude' and db.execute("SELECT 1 FROM sqlite_master WHERE name='claude_wakes'").fetchone():
                self.claude_delivery.resolved(db, session_id)
            db.commit()
            return {'ok': True, 'provider': provider, 'session_id': session_id,
                    'stopped_count': len(stopped), 'watch_ids': stopped}
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def cli_main(argv=None, *, description, default_db, default_design_html, default_port, server_class):
    """The dashboard's command line: `--db`, `--port`, `--no-enrichment`, `--design-html` (the deployment's defaults);
    serves on the loopback interface until interrupted. `server_class`: the deployment's DashboardServer."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--db', type=Path, default=default_db)
    parser.add_argument('--port', type=int, default=default_port)
    parser.add_argument('--no-enrichment', action='store_true', help='Disable bounded Slack profile/participant cache worker')
    parser.add_argument('--design-html', type=Path, default=Path(default_design_html),
                        help='Generated architecture design HTML served read-only on /design (default: %(default)s)')
    args = parser.parse_args(argv)
    os.umask(0o077)
    server = server_class(('127.0.0.1', args.port), args.db.expanduser(), enrich_profiles=not args.no_enrichment,
                          design_html=args.design_html.expanduser())
    print(f'Kage console: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
