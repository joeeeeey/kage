"""P34 no-orphan task lifecycle (任务不孤儿). Deterministic, no model.

A dispatched run must leave its task in exactly ONE closure state, each with an owner + trigger + deadline or terminal:
Done (Linear Done) / Not-actionable (Canceled + reason) / Waiting-on-a-named-person (an @-mention to a specific Slack
id + a watch + a ~1-day reminder; Linear In Review, assignee stays the owner) / Waiting-on-the owner (an open card) /
Agent-continuing (a running or scheduled session) / Blocked (a report-blocker card). A run that ends with none of these
(the case: "suggested next steps" and stopped, ticket left In Progress with no owner) is an orphan.

- `closure_state` is the shared, deterministic detector: it maps a task to one of those states from the live DB (open
  cards, owed-reply watches / ask cards with a named target and an unexpired deadline, routines / scheduled wakes,
  running sessions, a status.md terminal marker) or to `orphan`.
- `closure_guard` (dispatcher, at run end): a `success` run that left the task an orphan AND whose final message merely
  handed off / suggested next steps (no completion, no mechanism) gets ONE assistant card to the owner. Conservative: only
  Linear-ticket, runtime-managed, non-smoke tasks; one card per task per cooldown; misses rather than noise.
- `orphan_sweep` (supervisor, deduped, every `tasks.sweep_interval_minutes`): scans ONLY runtime-managed tasks (≥1
  `dispatches` row) the runtime launched or that were registered after the P0 backfill — a backfilled folder only ever
  revived by an event is never swept (`sweep_tasks`). An orphan that has been idle past `tasks.orphan_idle_hours` and
  that Linear still shows non-terminal (read via the deployment's tracker helper, best effort) gets one card. Each Linear read is
  persisted (a Done / Canceled ticket is not read again until the task is dispatched again) and one sweep's liveness
  probes and reads share a wall-clock budget. Local tasks are exempt (looser; never swept).
- `deadline_watchdog` (supervisor): a registered `task_deadlines` row surfaces one card `tasks.deadline_lead_hours`
  before its due time. P41: its first line is the the owner-facing `--ask` (legacy rows: a code-built question; the task's
  own note never reaches Slack). A deadline `--about` a card that still waits for the owner posts NO card: one reminder in
  that card's thread (reply_broadcast) restating the ask and how to answer there, disposition `reminded` (card_event
  stays NULL, so settle can never read the owner's 批准 there as a deadline option — it decides the original card). About a
  decided / closed card: the deadline card threads under it (broadcast); about a PR, or nothing: top level.
- P42 agent-owned wake-ups: a deadline registered `--wake` (the agent's own checkpoint: verify an output, check a status,
  re-run later) posts NO card at due — it wakes the task exactly like the owner's [让 agent 跟进] (a note event to its live
  current session, else `_redispatch`), with a brief that grants nothing new; disposition `woken`. Loop guard: at most
  `tasks.max_wakes` wake-ups per task in `tasks.wake_window_hours`, then the due wake is the P41 card with a code-built
  first line. A legacy checkpoint card (no --ask / --about) the owner neither clicked nor answered for
  `tasks.stale_card_days` lapses once (`lapse_stale`): it wakes its task (counted against the cap) and its buttons retire.
- `settle` (supervisor): the owner's click on an orphan / deadline card routes back here — 继续查 / 让 agent 跟进 re-dispatches
  the task to a closure state; 我来跟进 / 改状态指派 / 关单 / 改期 record his choice and reply in the card thread.
"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from kage import locale

ID_RE = re.compile(r'(?:or|dl)[0-9a-f]{14}')


SWEEP_LINEAR_TTL = 300.0


SWEEP_LINEAR_BUDGET = 15.0


LINEAR_TIMEOUT, LINEAR_MIN_TIMEOUT = 20.0, 5.0


LIFECYCLE_SINCE_KEY = 'lifecycle_since'   # P38 (F36): runtime_meta; the cut-over when there is no backfill marker


BACKFILL_KEY = 'tasks_backfill_v1'        # = task_registry.BACKFILL_KEY: the P0 backfill's report (+ its time `at`)


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS task_deadlines (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_path TEXT, due REAL NOT NULL, note TEXT NOT NULL,
        lead_hours INTEGER, disposition TEXT NOT NULL DEFAULT 'pending', card_event TEXT, card_channel TEXT,
        card_ts TEXT, carded_at REAL, choice TEXT, decided_by TEXT, decided_at REAL, dispatch_id TEXT,
        created_by TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT);
      CREATE INDEX IF NOT EXISTS task_deadlines_due ON task_deadlines(disposition, due);
      CREATE TABLE IF NOT EXISTS orphan_cards (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL, last_action_at REAL,
        linear_state TEXT, card_event TEXT, card_channel TEXT, card_ts TEXT, choice TEXT, decided_by TEXT,
        decided_at REAL, dispatch_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT);
      CREATE INDEX IF NOT EXISTS orphan_cards_task ON orphan_cards(task_id, state);
      -- P38 (F30): the sweep's last Linear read per task (state NULL = unreadable). A terminal read stands until the
      -- task is dispatched again (`dispatches` = its dispatch count at the read); read_at orders the next sweep.
      CREATE TABLE IF NOT EXISTS task_linear_reads (
        task_id TEXT PRIMARY KEY, state TEXT, type TEXT, terminal INTEGER NOT NULL DEFAULT 0,
        dispatches INTEGER NOT NULL DEFAULT 0, read_at REAL NOT NULL);
    ''')
    try:
        # P38 (F36): the sweep's fallback cut-over (only read without a P0 backfill marker, `_lifecycle_since`), written
        # once. On an upgrade the registry already holds the older rows; a fresh DB has none ('0').
        legacy = db.execute('SELECT 1 FROM tasks LIMIT 1').fetchone() is not None
        db.execute('INSERT OR IGNORE INTO runtime_meta VALUES (?,?)',
                   (LIFECYCLE_SINCE_KEY, f'{time.time():.6f}' if legacy else '0'))
    except sqlite3.OperationalError:  # a bare DB without the registry / meta tables: nothing is pre-lifecycle
        pass
    db.commit()


def _db_path(db):
    row = next((r for r in db.execute('PRAGMA database_list') if r[1] == 'main'), None)
    return row[2] if row else None


def config(rt):
    try:
        return rt.worker_config.load()[0]['tasks']
    except Exception:  # noqa: BLE001 - a malformed config never sweeps or cards by accident
        return {**rt.worker_config.DEFAULTS['tasks'], 'orphan_sweep_enabled': False, 'deadline_watchdog_enabled': False}


def long_lived(rt):
    """P39: the long-lived task ids (intake inbox / oncall + dispatch.long_lived_tasks); a malformed config -> the
    DEFAULT ids, so a resident task is never swept or guarded by accident."""
    try:
        return rt.worker_config.long_lived_ids(rt.worker_config.load()[0])
    except Exception:  # noqa: BLE001
        return rt.worker_config.long_lived_ids(rt.worker_config.DEFAULTS)


def runtime_managed(db, task_id):
    """The runtime has dispatched this task at least once (the run-end guard's scope; the sweep narrows it further,
    `sweep_tasks`). The backfilled folders start with none."""
    return db.execute('SELECT 1 FROM dispatches WHERE task_id=? OR task_key=? LIMIT 1', (task_id, task_id)).fetchone() \
        is not None


def runtime_managed_tasks(db):
    """DISTINCT task_id of every runtime-managed, drivable (Linear-ticket, non-smoke) task."""
    return [r[0] for r in db.execute('SELECT DISTINCT task_id FROM dispatches WHERE task_id IS NOT NULL ORDER BY task_id')]


def _lifecycle_since(db):
    """P38 (F36): the sweep's cut-over. Only the P0-backfilled folders are pre-lifecycle: when the backfill marker
    exists it is the backfill's time (0 when it created no task — nothing to exclude); a DB without a readable marker
    falls back to `lifecycle_since` (the first start of this code on an upgraded DB, 0 on a fresh one)."""
    row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (BACKFILL_KEY,)).fetchone()
    try:
        marker = json.loads(row[0]) if row else None
        if isinstance(marker, dict):
            return float(marker['at']) if marker.get('tasks_created') else 0.0
    except (TypeError, ValueError, KeyError):
        pass
    row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (LIFECYCLE_SINCE_KEY,)).fetchone()
    try:
        return float(row[0]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def sweep_tasks(db):
    """P38 (F36): the runtime-managed tasks the orphan sweep may card — the runtime launched it (a `launch` dispatch:
    launch-task / intake-launch / a routine / a card's re-dispatch), or its registry row is newer than the cut-over
    (`_lifecycle_since`: the P0 backfill). A P0-backfilled folder that was only ever revived by an event — a peer
    replied in its old watched thread — is never swept: one revive must not turn a ticket parked In Progress long ago
    into a card. (The run-end guard still checks the run that revived it.)"""
    return [r[0] for r in db.execute(
        'SELECT DISTINCT d.task_id FROM dispatches d LEFT JOIN tasks t ON t.task_id=d.task_id WHERE d.task_id IS NOT '
        'NULL AND (coalesce(t.created_at,0)>=? OR EXISTS (SELECT 1 FROM dispatches l WHERE (l.task_id=d.task_id OR '
        "l.task_key=d.task_id) AND l.trigger='launch')) ORDER BY d.task_id", (_lifecycle_since(db),))]


def last_action_at(db, task_id):
    """The most recent moment the runtime did anything for this task (last run, last dispatch move)."""
    times = []
    for sql, args in (('SELECT last_run_at FROM tasks WHERE task_id=?', (task_id,)),
                      ('SELECT max(coalesce(ended_at,updated_at)) FROM dispatches WHERE task_id=? OR task_key=?',
                       (task_id, task_id)),
                      ('SELECT max(coalesce(ended_at,started_at)) FROM consumer_runs WHERE task=?', (task_id,))):
        row = db.execute(sql, args).fetchone()
        if row and row[0] is not None:
            times.append(float(row[0]))
    return max(times) if times else None


def _status_text(task_path):
    try:
        path = Path(task_path) / 'status.md'
        return path.read_text(encoding='utf-8') if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024 else ''
    except OSError:
        return ''


def _task_watches(rt, db, task_id):
    """Running watches whose cwd resolves to this task (watches never store the task; it comes from cwd)."""
    out = []
    for row in db.execute("SELECT id FROM watches WHERE json_extract(data,'$.state')='running'").fetchall():
        try:
            w = rt.load(db, row[0])
        except Exception:  # noqa: BLE001 - a malformed watch row never breaks the sweep
            continue
        if rt.registry.task_id_for_cwd(w.get('cwd')) == task_id:
            out.append(w)
    return out


def _running(rt, db, task_id, probe, ended_session=None):
    """A running dispatch / run, or a live interactive session currently owns the task."""
    if db.execute("SELECT 1 FROM dispatches WHERE (task_id=? OR task_key=?) AND state='running' LIMIT 1",
                  (task_id, task_id)).fetchone():
        return True
    if db.execute("SELECT 1 FROM consumer_runs WHERE task=? AND state='running' LIMIT 1", (task_id,)).fetchone():
        return True
    cur = db.execute('SELECT current_provider, current_session FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if cur and ended_session and cur['current_session'] == ended_session:
        # P38 (F34): the run that just ended is usually the task's current session; it is over by definition, and a
        # liveness snapshot cached while its process was still alive must not make it look running.
        return False
    if cur and cur['current_session'] and probe is not None:
        try:
            if probe.check(cur['current_provider'], cur['current_session'])['state'] in rt.liveness.LIVE:
                return True
        except Exception:  # noqa: BLE001 - a probe failure is not a reason to call a task alive
            return False
    return False


def _scheduled(db, task_id, now):
    """A trigger will re-activate the task: a waiting dispatch, an active routine with a next run, or a pending task
    deadline the watchdog will card before (the agent-continuing state 5). P42: a pending wake counts until it has fired
    (the first watchdog pass at or after its due; the orphan sweep runs before the watchdog in a pass)."""
    if db.execute("SELECT 1 FROM dispatches WHERE (task_id=? OR task_key=?) AND state='waiting' LIMIT 1",
                  (task_id, task_id)).fetchone():
        return True
    if db.execute("SELECT 1 FROM routine_state WHERE task_id=? AND state='active' AND next_at IS NOT NULL LIMIT 1",
                  (task_id,)).fetchone():
        return True
    return db.execute("SELECT 1 FROM task_deadlines WHERE task_id=? AND disposition IN ('pending','carded','reminded') "
                      "AND (due>? OR (wake=1 AND disposition='pending')) LIMIT 1", (task_id, now)).fetchone() is not None


def _open_card_to_owner(rt, db, task_id):
    """An undecided decision card to the owner for this task (ask-<owner key> / blocker / escalate / proposal / intake / orphan)."""
    rows = db.execute("SELECT c.event_id, c.task_id, x.watch_id FROM escalation_cards c JOIN escalations x "
                      "ON x.event_id=c.event_id WHERE x.stage IN ('watching', ?) "
                      'AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.event_id=c.event_id) '
                      "AND NOT EXISTS (SELECT 1 FROM task_deadlines t WHERE t.card_event=c.event_id AND "
                      "t.disposition='lapsed')",  # P42: a lapsed card waits for nobody
                      (rt.SUPERVISOR_STAGE,)).fetchall()
    for r in rows:
        tid = r['task_id']
        if tid is None and r['watch_id']:
            try:
                tid = rt.registry.task_id_for_cwd(rt.load(db, r['watch_id']).get('cwd'))
            except Exception:  # noqa: BLE001
                tid = None
        if tid == task_id:
            return True
    return False


def _waiting_on_person(rt, db, task_id, now, watches):
    """A DELIBERATE @-mention to a specific person with an unexpired deadline: an open ask card (named responders,
    future expire_at), or a peer watch with named peers and an EXPLICIT fixed deadline (`ask` / `register
    --duration-seconds`). A passive inbound binding (sliding / idle peer watch, no fixed_deadline) does NOT count —
    watching a thread for replies is not the same as owing a named person an answer by a deadline."""
    for r in db.execute("SELECT watch_id, responders, expire_at FROM ask_cards WHERE state IN ('sending','open')").fetchall():
        if not json.loads(r['responders'] or '[]') or not r['expire_at'] or r['expire_at'] <= now:
            continue
        if r['watch_id']:
            try:
                if rt.registry.task_id_for_cwd(rt.load(db, r['watch_id']).get('cwd')) == task_id:
                    return True
            except Exception:  # noqa: BLE001
                continue
    for w in watches:
        if w.get('kind') == 'peer' and w.get('peers') and w.get('fixed_deadline') and float(w['fixed_deadline']) > now:
            return True
    return False


_BULLET_RE = re.compile(r'^\s*(?:[-*+]\s+|\d+[.)]\s+)(?:\[[ xX]\]\s+)?(.*)')


def _recent_orphan_card(db, task_id, now, cfg):
    """An orphan card is open (posting / open), or one settled within the idle cooldown — never two in a row."""
    cooldown = cfg['orphan_idle_hours'] * 3600
    return db.execute("SELECT 1 FROM orphan_cards WHERE task_id=? AND (state IN ('posting','open') OR updated_at>?) "
                      'LIMIT 1', (task_id, now - cooldown)).fetchone() is not None


def _linear_script(rt, helper=None):
    """The deployment's tracker helper (`helper`: its path under the skills root, the folder of the runtime's skill
    folder); None without one, then every ticket state is unknown."""
    return Path(rt.__file__).resolve().parents[2] / helper if helper else None


def _linear_ttl(cfg):
    """P38 (F30): an open / unreadable read is reused by the NEXT sweep (the supervisor sweeps every max(300 s,
    interval)) instead of re-running the subprocess; ×1.5 absorbs loop jitter, so never by the one after."""
    return 1.5 * max(SWEEP_LINEAR_TTL, cfg['sweep_interval_minutes'] * 60.0)


def _dispatch_count(db, task_id):
    return db.execute('SELECT count(*) FROM dispatches WHERE task_id=? OR task_key=?', (task_id, task_id)).fetchone()[0]


def _known_linear(db, task_id, now, ttl):
    """P38 (F30): (known, state) from the task's last persisted read. A Done / Canceled ticket stays known until the
    task is dispatched again (then it is read once more); an open or unreadable read is known for `ttl`."""
    row = db.execute('SELECT * FROM task_linear_reads WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return False, None
    state = {'state': row['state'], 'type': row['type']} if row['state'] else None
    if row['terminal']:
        return row['dispatches'] == _dispatch_count(db, task_id), state
    return now - row['read_at'] < ttl, state


def _note_linear(rt, db, task_id, state, now):
    with rt.transaction(db):
        db.execute('INSERT OR REPLACE INTO task_linear_reads (task_id,state,type,terminal,dispatches,read_at) '
                   'VALUES (?,?,?,?,?,?)', (task_id, (state or {}).get('state'), (state or {}).get('type'),
                                            int(_is_terminal_linear(state)), _dispatch_count(db, task_id), now))


def _is_terminal_linear(state):
    if not state:
        return False
    if str(state.get('type') or '').lower() in ('completed', 'canceled', 'cancelled'):
        return True
    return str(state.get('state') or '').strip().lower() in ('done', 'canceled', 'cancelled', 'completed', 'duplicate')


def _budget_runner(deadline, clock):
    """P38 (F30): the subprocess runner of the sweep's own liveness probe (claude agents / lsof / ps): each call gets at
    most the budget left, none starts past it. A probe cut short reads as unknown, and the deadline check after
    closure_state then defers that candidate — nothing is carded on it."""
    import subprocess

    def run(cmd, timeout=None, **kw):
        left = deadline - clock()
        if left <= 0:
            raise subprocess.TimeoutExpired(cmd, 0)
        return subprocess.run(cmd, timeout=left if timeout is None else min(timeout, left), **kw)
    return run


DUE_RE = re.compile(r'^\+(\d{1,5})([hdw])$')


WAKE_MIN_AHEAD = 300  # P42: a wake is a LATER checkpoint of the agent's own (keep working now instead)


def deadline_cancel(rt, db, deadline_id, now=None):
    now = time.time() if now is None else now
    if not ID_RE.fullmatch(str(deadline_id or '')):
        raise ValueError('deadline_id_must_look_like_dl + 14 hex')
    with rt.transaction(db):
        changed = db.execute("UPDATE task_deadlines SET disposition='cancelled',updated_at=? WHERE id=? AND "
                             "disposition IN ('pending','carded')", (now, deadline_id)).rowcount
    return {'ok': bool(changed), 'id': deadline_id}


WAKE_ACTOR = 'wake'  # P42: decided_by of a wake-up — the runtime on the agent's own checkpoint, never the owner


def _wakes(db, task_id, hours, now):
    """P42 (B): the task's wake-ups (woken / lapsed rows the runtime woke) in the last `hours`."""
    return db.execute("SELECT count(*) FROM task_deadlines WHERE task_id=? AND disposition IN ('woken','lapsed') AND "
                      'decided_by=? AND decided_at>?', (task_id, WAKE_ACTOR, now - hours * 3600)).fetchone()[0]


def _woken(db, r):
    """P42: the (dispatch id | note event id, how) an earlier pass already made for this row, else None."""
    ref = 'deadline_wake:' + r['id']
    return (db.execute("SELECT dispatch_id, 'dispatch' FROM dispatches WHERE reason=?", (ref,)).fetchone() or
            db.execute("SELECT event_id, 'note' FROM local_messages WHERE sender=?", (ref,)).fetchone())


def _decision(rt, db, card_event):
    d = db.execute('SELECT d.*, c.options FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                   'WHERE d.event_id=?', (card_event,)).fetchone()
    if d is None:
        return None
    return d, rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))


def _redispatch(rt, db, db_path, task_id, reason, prompt, now):
    """Re-activate a task headless (the owner chose 让 agent 继续查 / 关单 / 让 agent 跟进). P39: a system re-dispatch inherits
    the task's launch preference (its pinned provider, important and category; else its preferred provider) and never
    rewrites it — an Opus security task launched `--provider claude --important` stays on Claude with model_important."""
    disp = rt.make_dispatcher(db, db_path)
    return disp.launch(disp.task_for_key(task_id), 'auto', prompt, reason=reason, now=now, inherit=True, persist=False)


DEADLINE_DISPOSITION = {'self': 'handled', 'agent': 'handed', 'defer': 'deferred'}


class Lifecycle:
    """The owner-bound half of the task lifecycle: closure states, the orphan sweep and its cards, deadlines
    and wake-ups in the owner's words and time zone, and the owner's clicks."""

    def __init__(self, language, *, tz, ticket_task_re, ticket_id_re, owner_key, owner_name, owner_user,
                 tracker_helper=None):
        self.language = language
        self.tracker_helper = tracker_helper
        self.tz = tz
        self.ticket_task_re = ticket_task_re
        self.ticket_id_re = ticket_id_re
        self.owner_user = owner_user
        self.key, self.name = owner_key, owner_name
        self.orphan_options = [self.msg('lifecycle.orphan_options.1'), self.msg('lifecycle.orphan_options.2'),
                               self.msg('lifecycle.orphan_options.3'), self.msg('lifecycle.orphan_options.4')]
        self.deadline_options = [self.msg('lifecycle.deadline_options.1'), self.msg('lifecycle.deadline_options.2'), self.msg('lifecycle.deadline_options.3')]
        self.terminal_re = re.compile(self.pattern('lifecycle.terminal_re.1'))
        self.handoff_re = re.compile(self.pattern('lifecycle.handoff_re.1'))
        self.completion_re = re.compile(self.pattern('lifecycle.completion_re.1'))
        self._next_heading_re = re.compile(self.pattern('lifecycle._next_heading_re.1'))
        self.wake_brief = (self.msg('lifecycle.wake_brief.1', owner=self.name))
        self.stale_line = self.msg('lifecycle.stale_line.1')
        self.orphan_reply = {
                 'followup': self.msg('lifecycle.orphan_reply.followup'),
                 'assign': self.msg('lifecycle.orphan_reply.assign'),
                 'agent': self.msg('lifecycle.orphan_reply.agent'),
                 'close': self.msg('lifecycle.orphan_reply.close'),
             }
        self.continue_prompt = (self.msg('lifecycle.continue_prompt.1', owner=self.name))
        self.close_prompt = (self.msg('lifecycle.close_prompt.1', owner=self.name))
        self.deadline_prompt = (self.msg('lifecycle.deadline_prompt.1', owner=self.name))
        self.deadline_reply = {
                 'self': self.msg('lifecycle.deadline_reply.self'),
                 'agent': self.msg('lifecycle.deadline_reply.agent'),
                 'defer': self.msg('lifecycle.deadline_reply.defer'),
             }

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def is_linear_task(self, task_id):
        """A Linear-ticket task (<TEAM>-N, a team of the org's ticket pattern) — driven by the lifecycle machinery. local-task-… and the plain-named
        long-lived tasks (<owner key>-inbox / <owner key>-oncall, P39) are not."""
        return bool(task_id) and bool(self.ticket_task_re.match(str(task_id)))

    def drivable(self, rt, task_id, resident=None):
        """Swept by the orphan sweep / checked by the run-end guard: a Linear-ticket, non-smoke, non-session task that is
        not long-lived (P39: explicit, even for a long-lived task named like a ticket). `resident`: a precomputed
        long_lived(rt) for loops."""
        return (self.is_linear_task(task_id) and not str(task_id or '').startswith('session:')
                and not rt.registry.is_smoke_task(task_id)
                and task_id not in (long_lived(rt) if resident is None else resident))

    def closure_state(self, rt, db, task_id, task_path, now=None, probe=None, ended_session=None):
        """One of: running | scheduled | waiting_on_<owner key> | waiting_on_person | terminal | orphan. `orphan` means the task
        is non-terminal and active but has no owner, no trigger and no deadline — nobody and nothing will move it.
        `ended_session` (run-end guard): the session of the run being finalized, never counted as a live owner."""
        now = time.time() if now is None else now
        if _running(rt, db, task_id, probe, ended_session):
            return 'running'
        if _scheduled(db, task_id, now):
            return 'scheduled'
        if _open_card_to_owner(rt, db, task_id):
            return f'waiting_on_{self.key}'
        if _waiting_on_person(rt, db, task_id, now, _task_watches(rt, db, task_id)):
            return 'waiting_on_person'
        if self.terminal_re.search(_status_text(task_path)):
            return 'terminal'
        return 'orphan'

    def handoff_without_closure(self, final):
        """The run's final message suggested next steps / handed off (HANDOFF) without declaring completion (COMPLETION):
        the forbidden "suggest and stop" shape. Empty / completion-declaring / non-handoff finals do not count."""
        text = str(final or '')
        return bool(self.handoff_re.search(text)) and not self.completion_re.search(text)

    def _when(self, ts, now):
        if not ts:
            return self.msg('lifecycle._when.1')
        delta = max(0.0, now - ts)
        if delta < 90:
            return self.msg('lifecycle._when.2')
        if delta < 90 * 60:
            return self.msg('lifecycle._when.3', round=round(delta / 60))
        if delta < 36 * 3600:
            return self.msg('lifecycle._when.4', round=round(delta / 3600))
        return self.msg('lifecycle._when.5', round=round(delta / 86400))

    def _until(self, ts, now):
        delta = ts - now
        if delta <= 0:
            return self.msg('lifecycle._until.1')
        if delta < 36 * 3600:
            return self.msg('lifecycle._until.2', max=max(1, round(delta / 3600)))
        return self.msg('lifecycle._until.3', round=round(delta / 86400))

    def _next_hint(self, task_path):
        """One short plain line for 下一步 <…>: the first bullet under a Next-steps heading, else a short fallback."""
        text = _status_text(task_path)
        lines = text.splitlines()
        for i, raw in enumerate(lines):
            if raw.lstrip().startswith('#') and self._next_heading_re.search(raw):
                for nxt in lines[i + 1:i + 8]:
                    m = _BULLET_RE.match(nxt)
                    if m and m.group(1).strip():
                        return self._trim(m.group(1))
                break
        return self.msg('lifecycle._next_hint.1')

    def _trim(self, text, limit=40):
        text = ' '.join(str(text or '').split())
        text = re.sub(r'<[@!#][^>]*>', '', text).strip()
        return (text[:limit - 1] + '…') if len(text) > limit else (text or self.msg('lifecycle._trim.1'))

    def post_orphan_card(self, rt, db, task_id, task_path, now, reason, linear_state=None):
        cfg = config(rt)
        if _recent_orphan_card(db, task_id, now, cfg):
            return {'carded': False, 'reason': 'deduped'}
        last = last_action_at(db, task_id)
        question = (self.msg('lifecycle.post_orphan_card.1', task_id=task_id, when=self._when(last, now), next_hint=self._next_hint(task_path)))
        footer = self.msg('lifecycle.post_orphan_card.2', reason=reason, dashboard_url=rt.DASHBOARD_URL, task_id=task_id)
        oid = 'or' + uuid.uuid4().hex[:14]
        with rt.transaction(db):
            db.execute('INSERT INTO orphan_cards (id,task_id,reason,state,last_action_at,linear_state,created_at,updated_at) '
                       "VALUES (?,?,?,'posting',?,?,?,?)", (oid, task_id, reason, last,
                                                            json.dumps(linear_state, ensure_ascii=False) if linear_state else None,
                                                            now, now))
        try:
            card = rt.supervisor_card(db, question, self.orphan_options, kind='orphan_card', footer=footer, task_id=task_id, now=now)
        except Exception as exc:  # noqa: BLE001 - recorded; the sweep retries next cycle
            with rt.transaction(db):
                db.execute("UPDATE orphan_cards SET state='failed',error=?,updated_at=? WHERE id=?",
                           (rt.safe_error(exc), now, oid))
            raise
        with rt.transaction(db):
            db.execute("UPDATE orphan_cards SET state='open',card_event=?,card_channel=?,card_ts=?,updated_at=? WHERE id=?",
                       (card['event_id'], card['channel'], card['thread_ts'], now, oid))
        return {'carded': True, 'orphan_id': oid, 'card': card, 'reason': reason}

    def closure_guard(self, rt, db, run, final, now=None, probe=None):
        """A dispatched `success` run that left its Linear-ticket task an orphan AND merely suggested next steps / handed
        off → ONE assistant card to the owner. Conservative: non-Linear / local / smoke / non-runtime-managed tasks, a task
        already in a closure state, and a final that declared completion or made no suggestion are all left alone."""
        now = time.time() if now is None else now
        if not config(rt)['orphan_sweep_enabled']:
            return 'disabled'
        task_id = run['task']
        if not self.drivable(rt, task_id):
            return 'not_a_linear_task'
        if not runtime_managed(db, task_id):
            return 'not_runtime_managed'
        task_path = run['task_path'] or str(rt.registry.task_dir(task_id))
        if not Path(task_path).is_dir():
            return 'workspace_missing'
        if not self.handoff_without_closure(final):
            return 'no_handoff_signal'
        # P38 (F34): the finalizing run's own session is not a live owner, and any other session is probed fresh — not
        # through the dispatcher's liveness cache (a snapshot up to 20 s old). The shared cache is cleared only when another
        # session is about to be probed.
        cur = db.execute('SELECT current_session FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        clear = getattr(probe, 'clear', None)
        if cur and cur[0] and cur[0] != run['session'] and callable(clear):
            clear()
        if self.closure_state(rt, db, task_id, task_path, now, probe, ended_session=run['session']) != 'orphan':
            return 'closed'
        return self.post_orphan_card(rt, db, task_id, task_path, now, reason='run_end')['reason']

    def _linear_state(self, rt, task_id, timeout=LINEAR_TIMEOUT):
        """Best effort: the Linear workflow state of a <TEAM>-N ticket via the deployment's tracker helper (a script under
        the skills root that prints `issue get --json`; `tracker_helper`).
        Returns {'state': name, 'type': category} or None when it cannot be read (then the sweep stays conservative and
        does NOT card). Not cached here: the sweep persists every read (`task_linear_reads`); tests inject their own
        reader or fake the subprocess."""
        state = None
        try:
            m = self.ticket_id_re.match(str(task_id))
            script = _linear_script(rt, self.tracker_helper)
            if m and script is not None and script.is_file():
                import subprocess
                out = subprocess.run(['python3', str(script), '--json', 'issue', 'get', m.group(0)],
                                     capture_output=True, text=True, timeout=timeout, check=False)
                if out.returncode == 0 and out.stdout.strip():
                    st = (json.loads(out.stdout).get('state') or {})  # `issue get --json` prints the issue node
                    if st.get('name'):
                        state = {'state': st.get('name'), 'type': st.get('type')}
        except Exception:  # noqa: BLE001 - unreadable Linear = unknown = conservative skip
            state = None
        return state

    def orphan_sweep(self, rt, db, db_path, cfg, now, probe, linear_state, result, clock=time.monotonic):
        idle = cfg['orphan_idle_hours'] * 3600
        ttl = _linear_ttl(cfg)
        # P38 (F30): a bounded wall-clock per sweep; what does not fit waits for the next one, least recently read first
        # (a deferred task leads the next sweep, so every candidate is reached). The budget covers the liveness probe too:
        # the sweep's own probe runs inside it, and no Linear read or card starts once it is spent.
        deadline = clock() + SWEEP_LINEAR_BUDGET
        probe = probe or rt.liveness.Probe(ttl=20, runner=_budget_runner(deadline, clock))
        reader = linear_state or (lambda tid: self._linear_state(
            rt, tid, timeout=max(LINEAR_MIN_TIMEOUT, min(LINEAR_TIMEOUT, deadline - clock()))))
        read_at = {r[0]: r[1] for r in db.execute('SELECT task_id, read_at FROM task_linear_reads')}
        tasks = sorted(sweep_tasks(db), key=lambda tid: (read_at.get(tid, 0.0), tid))
        resident = long_lived(rt)  # P39: the long-lived set is never swept (one config read per sweep)
        for i, task_id in enumerate(tasks):
            try:
                if clock() >= deadline:
                    result['deferred'] = [t for t in tasks[i:] if self.drivable(rt, t, resident)]
                    break
                if not self.drivable(rt, task_id, resident):
                    continue
                task_dir = rt.registry.task_dir(task_id)
                if not task_dir.is_dir():
                    continue
                last = last_action_at(db, task_id)
                if last is None or now - last < idle:
                    continue
                if _recent_orphan_card(db, task_id, now, cfg):
                    continue
                known, ls = _known_linear(db, task_id, now, ttl)
                if known and _is_terminal_linear(ls):
                    continue  # P38 (F30): Done / Canceled at the last read, not dispatched since — never re-read each sweep
                if self.closure_state(rt, db, task_id, str(task_dir), now, probe) != 'orphan':
                    continue
                if clock() >= deadline:  # P38 (F30): its liveness probe spent the budget: no read / card past it
                    result['deferred'] = [t for t in tasks[i:] if self.drivable(rt, t, resident)]
                    break
                if not known:
                    ls = reader(task_id)
                    _note_linear(rt, db, task_id, ls, now)
                if ls is None:  # cannot confirm the ticket is still open: do not nag
                    result['skipped_unknown_linear'].append(task_id)
                    continue
                if _is_terminal_linear(ls):
                    continue
                try:
                    posted = self.post_orphan_card(rt, db, task_id, str(task_dir), now, 'sweep', linear_state=ls)
                    if posted['carded']:
                        result['orphans'].append({'task_id': task_id, 'linear': ls.get('state')})
                except Exception as exc:  # noqa: BLE001 - one bad task never stops the sweep
                    rt._meta_error(db, 'orphan_sweep_card_error', exc)
            except Exception as exc:  # noqa: BLE001 - P38: nor does a candidate's DB lock (e.g. its read bookkeeping)
                rt._meta_error(db, 'orphan_sweep_error', exc)

    def parse_due(self, value, now):
        """`+<n>h|d|w`, an ISO date (YYYY-MM-DD → 18:00 Shanghai), or YYYY-MM-DDTHH:MM (Shanghai) → epoch seconds."""
        text = str(value or '').strip()
        rel = DUE_RE.fullmatch(text)
        if rel:
            n, unit = int(rel.group(1)), rel.group(2)
            return now + n * {'h': 3600, 'd': 86400, 'w': 604800}[unit]
        for fmt, add in (('%Y-%m-%dT%H:%M', 0), ('%Y-%m-%d %H:%M', 0), ('%Y-%m-%d', 18 * 3600)):
            try:
                base = dt.datetime.strptime(text, fmt).replace(tzinfo=self.tz)
                return base.timestamp() + add
            except ValueError:
                continue
        raise ValueError('due_must_be_+Nh|+Nd|+Nw_or_YYYY-MM-DD[THH:MM]')

    def deadline_register(self, rt, db, task, due, note, lead_hours=None, now=None, ask=None, about=None, wake=False):
        """P41: `ask` is the reminder's the owner-facing first line (the CLI requires it; verb first, copy standard, no pointer to
        another card without its link) and `about` the card / PR it is about (resolve_about: fail closed). P42 `wake`: the
        agent's own checkpoint — at due it wakes the task (no card); the note is the woken run's brief; no ask, no lead,
        at least WAKE_MIN_AHEAD ahead; `about` is optional context."""
        now = time.time() if now is None else now
        if wake and (ask is not None or lead_hours is not None):
            raise ValueError(f'wake_is_your_own_checkpoint_without_--ask_or_--lead-hours: what {self.name} must do or decide is a deadline with --ask (no --wake)')
        row = rt._resolve_task_arg(db, task or os.getcwd())
        task_id, task_path = row['task_id'], row['path']
        note = ' '.join(str(note or '').split())
        if not note or len(note) > 160:
            raise ValueError('note_must_be_1_to_160_characters')
        rt.asks.check_secret_free(note)
        rt.buttons.check_copy(note, 'deadline_note', first_line_max=None, refs=False)  # the task's own note: dashboard only
        if ask is not None or (about and not wake):
            ask = '\n'.join(' '.join(line.split()) for line in str(ask or '').strip().splitlines() if line.strip())
            if not ask or len(ask) > 400 or ask.count('\n') > 2:
                raise ValueError('ask_must_be_1_to_3_lines_at_most_400_characters: ' + rt.buttons.ASK_STANDARD)
            rt.asks.check_secret_free(ask)
            rt.buttons.check_copy(re.sub(r'https?://[!-~]+', 'https://…', ask), 'ask', ask=True)  # a link is not length
        ref = rt.resolve_about(db, about) if about else {}
        due_ts = self.parse_due(due, now)
        if due_ts <= now:
            raise ValueError('due_must_be_in_the_future')
        if wake and due_ts < now + WAKE_MIN_AHEAD:
            raise ValueError('wake_must_be_at_least_5_minutes_ahead_keep_working_now_instead')
        if lead_hours is not None and (not isinstance(lead_hours, int) or not 0 <= lead_hours <= 720):
            raise ValueError('lead_hours_must_be_0_to_720')
        did = 'dl' + uuid.uuid4().hex[:14]
        actor = '%s:%s' % rt.claude.actor() if rt.claude.actor()[1] else self.key
        with rt.transaction(db):
            db.execute('INSERT INTO task_deadlines (id,task_id,task_path,due,note,lead_hours,disposition,created_by,'
                       "created_at,updated_at,ask,about_event,about_pr,wake) VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
                       (did, task_id, task_path, due_ts, note, 0 if wake else lead_hours, actor, now, now, ask,
                        ref.get('event_id'), ref.get('pr'), 1 if wake else None))  # lead 0: a rollback cards it at due
        return {'ok': True, 'id': did, 'task_id': task_id, 'due': dt.datetime.fromtimestamp(due_ts, self.tz).isoformat(),
                **({'about': ref} if ref else {}), **({'wake': True} if wake else {})}

    def deadline_list(self, rt, db, task=None, status='all'):
        where, args = [], []
        if task:
            where.append('task_id=?')
            args.append(rt._resolve_task_arg(db, task)['task_id'])
        if status and status != 'all':
            where.append('disposition=?')
            args.append(status)
        sql = 'SELECT * FROM task_deadlines' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY due'
        out = []
        for r in db.execute(sql, args).fetchall():
            out.append({'id': r['id'], 'task_id': r['task_id'], 'due': dt.datetime.fromtimestamp(r['due'], self.tz).isoformat(),
                        'note': r['note'], 'ask': r['ask'], 'about': r['about_event'] or r['about_pr'],
                        'disposition': r['disposition'], 'wake': bool(r['wake']), 'result': r['wake_result']})
        return {'deadlines': out}

    def _deadline_question(self, db, r, now, link=None, guard_line=None):
        """P41: the deadline card's text — the agent's `--ask` first (plus the related card / PR link when the card is not
        in that card's thread), else (a legacy row) a code-built question; never the task's own note. P42: `guard_line` is
        the loop guard's code-built first line of a wake that became a card."""
        due = self.msg('lifecycle._deadline_question.1', fromtimestamp=dt.datetime.fromtimestamp(r['due'], self.tz), until=self._until(r['due'], now))
        if r['ask']:
            first, *rest = r['ask'].split('\n')
            return '\n'.join([first + (self.msg('lifecycle._deadline_question.2', link=link) if link and link not in first else ''), *rest, self.msg('lifecycle._deadline_question.3', due=due)])
        title = (db.execute('SELECT title FROM tasks WHERE task_id=?', (r['task_id'],)).fetchone() or {'title': None})['title']
        head = guard_line or self.msg('lifecycle._deadline_question.6', task_id=r['task_id'], value=self.msg('lifecycle._deadline_question.4') if r['due'] <= now else self.msg('lifecycle._deadline_question.5'), due=due)
        return '\n'.join([head + (self.msg('lifecycle._deadline_question.7', link=link) if link else ''), *([self.msg('lifecycle._deadline_question.8', trim=self._trim(title, 60))] if title else []),
                          self.msg('lifecycle._deadline_question.9')])

    def _remind_in_thread(self, rt, db, r, card, now):
        """P41: the --about card still waits for the owner: ONE bot reminder in its thread, also shown in the channel — no new
        card, no second set of buttons. Its first line is the ask; the second says how to answer there (a typed 批准 decides
        that card when it is the only open card of the thread). None when nobody would read the thread (the P36 guard): the
        caller falls back to a top-level card."""
        channel, root = card['channel'], card['thread_root'] or card['thread_ts']
        sent = db.execute("SELECT ts FROM agent_sends WHERE kind='deadline_reminder' AND ref=?", (r['id'],)).fetchone()
        if sent:  # posted, but the row update after it failed: never a second @the owner
            return {'event_id': None, 'channel': channel, 'thread_ts': sent[0]}
        rt._rearm_retired_approvals(db, channel, root, now)
        cfg = rt.card_threads.config(rt)
        stopped = db.execute('SELECT data FROM watches WHERE id=?', (rt._approvals_wid(channel, root),)).fetchone()
        stopped = json.loads(stopped[0]).get('stop_reason') not in (None, rt.card_threads.RETIRED) if stopped else False
        if not (rt.card_threads._covered(rt, db, channel, root, now) or
                (cfg['task_thread'] and cfg.get('realtime_replies') and not stopped)):  # the push skips a stopped thread
            return None
        labels = [o['label'] for o in rt.buttons.card_model(card['options'])]
        typed = cfg['thread_reply_decisions'] and \
            [c['event_id'] for c in rt.card_replies.open_cards(rt, db, channel, root, now=now)] == [card['event_id']]
        first, *rest = r['ask'].split('\n')
        how = (self.msg('lifecycle._remind_in_thread.1', fromtimestamp=dt.datetime.fromtimestamp(r['due'], self.tz), until=self._until(r['due'], now), card_link=rt.card_link(channel, card['thread_ts'], card['thread_root'])) +
               (self.msg('lifecycle._remind_in_thread.2') + self.msg('lifecycle._remind_in_thread.3').join(f'“{rt.buttons.escape(x)}”' for x in labels) if typed else '') + self.msg('lifecycle._remind_in_thread.4'))
        rt.buttons.require_private_destination(rt, db, channel, 'deadline_reminder')
        sent = rt.slack.api_call('chat.postMessage', rt.api_identity(bot=True), json_body={
            'channel': channel, 'thread_ts': root, 'reply_broadcast': True, 'unfurl_links': False, 'unfurl_media': False,
            'text': '\n'.join([f'<@{self.owner_user(rt)}> {rt.buttons.escape(first)}', *map(rt.buttons.escape, rest), how]),
            'metadata': rt.agent_metadata('deadline_reminder', deadline_id=r['id'], event_id=card['event_id'])})
        rt.record_send(db, channel, sent['ts'], 'deadline_reminder', r['id'])
        return {'event_id': None, 'channel': channel, 'thread_ts': sent['ts']}

    def _wake_guard(self, db, r, cfg, now):
        """P42 (B): None while the task may still wake itself, else (why, the card's first line): the due wake becomes the
        P41 card to the owner — never an agent waking itself forever."""
        cap, hours = cfg['max_wakes'], cfg['wake_window_hours']
        n = _wakes(db, r['task_id'], hours, now)
        if cap and n < cap:
            return None
        if not cap:
            return 'max_wakes_0', self.msg('lifecycle._wake_guard.1', task_id=r['task_id'])
        span = self.msg('lifecycle._wake_guard.2', value=hours // 24) if hours % 24 == 0 else self.msg('lifecycle._wake_guard.3', hours=hours)
        return (f'cap_{n}_in_{hours}h', self.msg('lifecycle._wake_guard.4', task_id=r['task_id'], span=span, n=n))

    def _wake(self, rt, db, db_path, r, now, probe, disposition, result):
        """P42: wake the task for a checkpoint exactly like the owner's [让 agent 跟进] would — a note event to its live current
        session (agent_message; author and trust system, authority none), else a fresh run (`_redispatch`: the task's
        provider / important / category) — with WAKE_BRIEF. Never a card and never the owner's authority. Once per row: a
        dispatch or note already made for it is reused (a lost row update never wakes twice). False when it failed (error
        recorded; the next pass retries)."""
        ref = 'deadline_wake:' + r['id']
        try:
            done = _woken(db, r)
            if done is None:
                card = r['about_event'] and rt.card_replies.card(db, r['about_event'])
                link = r['about_pr'] or (card and rt.card_link(card['channel'], card['thread_ts'], card['thread_root']))
                brief = self.wake_brief.format(task=r['task_id'], note=r['note'], about=self.msg('lifecycle._wake.1', link=link) if link else '')
                cur = db.execute('SELECT path, current_provider AS p, current_session AS s FROM tasks WHERE task_id=?',
                                 (r['task_id'],)).fetchone()
                probe = probe or (rt.liveness.Probe(ttl=20) if cur and cur['s'] else None)
                if cur and cur['s'] and probe.check(cur['p'], cur['s'])['state'] in rt.liveness.LIVE:
                    path = rt.task_memory.write_note(cur['path'], r['task_id'], ref, brief, now, f'{cur["p"]}:{cur["s"]}')
                    env = rt.envelopes.envelope('agent_message', 'system', 'system', 'none', 'deadline_wake')
                    done = (rt.enqueue_local_event(db, r['task_id'], cur['path'], cur['p'], cur['s'], 'note', path, ref, env,
                                                   now)[0], 'note')
                else:
                    done = (_redispatch(rt, db, db_path or _db_path(db), r['task_id'], ref, brief, now), 'dispatch')
        except Exception as exc:  # noqa: BLE001 - recorded; retried next cycle
            with rt.transaction(db):
                db.execute('UPDATE task_deadlines SET error=?,updated_at=? WHERE id=?', (rt.safe_error(exc), now, r['id']))
            rt._meta_error(db, 'deadline_wake_error', exc)
            return False
        with rt.transaction(db):
            db.execute('UPDATE task_deadlines SET disposition=?,decided_by=?,decided_at=?,dispatch_id=?,wake_result=?,'
                       'error=NULL,updated_at=? WHERE id=?', (disposition, WAKE_ACTOR, now, done[0], done[1], now, r['id']))
        result['deadlines'].append({'id': r['id'], 'task_id': r['task_id'], disposition: done[1]})
        return True

    def deadline_watchdog(self, rt, db, cfg, now, result, db_path=None, probe=None):
        lead = cfg['deadline_lead_hours'] * 3600
        for r in db.execute("SELECT * FROM task_deadlines WHERE disposition='pending' ORDER BY due").fetchall():
            window = 0 if r['wake'] else (r['lead_hours'] * 3600) if r['lead_hours'] is not None else lead
            if r['due'] - now > window:
                continue
            guard = self._wake_guard(db, r, cfg, now) if r['wake'] and not _woken(db, r) else None
            if r['wake'] and guard is None:  # P42: the agent's own checkpoint wakes its task; no card, no Slack post
                self._wake(rt, db, db_path, r, now, probe, 'woken', result)
                continue
            footer = self.msg('lifecycle.deadline_watchdog.1', dashboard_url=rt.DASHBOARD_URL, task_id=r['task_id'])
            about = rt.card_replies.card(db, r['about_event']) if r['about_event'] else None
            try:
                card = self._remind_in_thread(rt, db, r, about, now) if about and r['ask'] and \
                    rt.card_waiting(db, about['event_id']) else None
                if card is None:  # the link stays even when threaded: it may land top level (no reader, another channel)
                    link = r['about_pr'] or (about and rt.card_link(about['channel'], about['thread_ts'], about['thread_root']))
                    card = rt.supervisor_card(db, self._deadline_question(db, r, now, link, guard and guard[1]), self.deadline_options,
                                              kind='deadline_card', footer=footer, task_id=r['task_id'], now=now,
                                              parent_event=about and about['event_id'], broadcast=bool(about),
                                              first_line_max=None)
            except Exception as exc:  # noqa: BLE001 - recorded; retried next cycle
                with rt.transaction(db):
                    db.execute('UPDATE task_deadlines SET error=?,updated_at=? WHERE id=?', (rt.safe_error(exc), now, r['id']))
                rt._meta_error(db, 'deadline_card_error', exc)
                continue
            with rt.transaction(db):  # P41: a thread reminder is `reminded` with no card_event (settle never reads it)
                db.execute('UPDATE task_deadlines SET disposition=?,card_event=?,card_channel=?,card_ts=?,carded_at=?,'
                           'wake_result=?,error=NULL,updated_at=? WHERE id=?',
                           ('carded' if card['event_id'] else 'reminded', card['event_id'], card['channel'],
                            card['thread_ts'], now, guard and guard[0], now, r['id']))
            result['deadlines'].append({'id': r['id'], 'task_id': r['task_id'], 'reminded': not card['event_id']})

    def lapse_stale(self, rt, db, db_path, cfg, now, probe, result):
        """P42 (D): a legacy checkpoint card (no --ask / --about / --wake), past its due, that the owner neither clicked nor
        answered in its thread for tasks.stale_card_days lapses once: its buttons retire with STALE_LINE (the P3.1 helper),
        then its task is woken (counted against the cap; at the cap the card stays). Buttons first, so a lapsed row always
        has a retired card (a failed retirement is retried, bounded by the helper). A typed choice there later is still
        honoured (settle reads lapsed rows)."""
        if not cfg['stale_card_days']:
            return
        for r in db.execute("SELECT * FROM task_deadlines WHERE disposition='carded' AND card_event IS NOT NULL AND ask IS "
                            'NULL AND about_event IS NULL AND about_pr IS NULL AND wake IS NULL AND carded_at<? AND due<?',
                            (now - cfg['stale_card_days'] * 86400, now)).fetchall():
            try:
                card = rt.card_replies.card(db, r['card_event'])
                if card is None or _decision(rt, db, r['card_event']) or self._wake_guard(db, r, cfg, now) or db.execute(
                        'SELECT 1 FROM card_reply_routes WHERE channel=? AND root=? UNION SELECT 1 FROM card_replies WHERE '
                        'event_id=? AND reply_ts IS NOT NULL', (card['channel'], card['thread_ts'], card['event_id'])
                ).fetchone():
                    continue  # the owner decided or answered there, or the task is at its wake cap: the card stays
                if rt.card_replies.retire(rt, db, card, {'ts': None}, now, line=self.stale_line)['action'] in ('retired',
                                                                                                         'already_retired'):
                    self._wake(rt, db, db_path, r, now, probe, 'lapsed', result)
            except Exception as exc:  # noqa: BLE001 - one card never stops the pass
                rt._meta_error(db, 'deadline_lapse_error', exc)

    def sweep(self, rt, db, db_path, now=None, probe=None, linear_state=None, clock=None):
        """Supervisor step (every tasks.sweep_interval_minutes): orphan sweep (Linear-aware) + deadline watchdog.
        `deferred`: orphan candidates left for the next sweep because this one's budget ran out (`clock` = the wall clock
        that budget is measured on; tests inject one)."""
        now = time.time() if now is None else now
        cfg = config(rt)
        result = {'orphans': [], 'deadlines': [], 'skipped_unknown_linear': [], 'deferred': []}
        if cfg['orphan_sweep_enabled']:
            try:
                self.orphan_sweep(rt, db, db_path, cfg, now, probe, linear_state, result, clock or time.monotonic)
            except Exception as exc:  # noqa: BLE001 - P38: a failed orphan pass never skips this cycle's deadline watchdog
                rt._meta_error(db, 'orphan_sweep_error', exc)
        if cfg['deadline_watchdog_enabled']:
            self.deadline_watchdog(rt, db, cfg, now, result, db_path, probe)
            self.lapse_stale(rt, db, db_path, cfg, now, probe, result)
        return result

    def settle(self, rt, db, db_path=None, now=None):
        """Supervisor step: the owner's click on an orphan / deadline card routes back here (a decision event). Only the owner's own
        click counts. 继续查 / 让 agent 跟进 re-dispatch the task; the rest record his choice and reply in the thread. P42: a
        lapsed card's later decision (a typed label) is honoured the same way."""
        now = time.time() if now is None else now
        db_path = db_path or _db_path(db)
        done = []
        for row in db.execute("SELECT * FROM orphan_cards WHERE state='open' AND card_event IS NOT NULL").fetchall():
            found = _decision(rt, db, row['card_event'])
            if found is None:
                continue
            d, option = found
            if d['actor'] != self.owner_user(rt):
                continue
            choice, dispatch_id, error = option['id'], None, None
            if choice == 'agent':
                try:
                    dispatch_id = _redispatch(rt, db, db_path, row['task_id'], 'orphan_continue:' + row['id'],
                                              self.continue_prompt.format(task=row['task_id']), now)
                except Exception as exc:  # noqa: BLE001 - recorded; the owner can launch by hand
                    error, choice = (str(exc) if isinstance(exc, ValueError) else rt.safe_error(exc))[:300], 'agent_failed'
            elif choice == 'close':
                try:
                    dispatch_id = _redispatch(rt, db, db_path, row['task_id'], 'orphan_close:' + row['id'],
                                              self.close_prompt.format(task=row['task_id']), now)
                except Exception as exc:  # noqa: BLE001
                    error, choice = (str(exc) if isinstance(exc, ValueError) else rt.safe_error(exc))[:300], 'close_failed'
            with rt.transaction(db):
                db.execute("UPDATE orphan_cards SET state='resolved',choice=?,decided_by=?,decided_at=?,dispatch_id=?,"
                           'error=?,updated_at=? WHERE id=?',
                           (choice, d['actor'], d['decided_at'], dispatch_id, error, now, row['id']))
            reply = self.orphan_reply.get(choice, self.msg('lifecycle.settle.1') + (error or '') + self.msg('lifecycle.settle.2'))
            rt.rules._card_reply(rt, db, row['card_event'], reply, row['id'], kind='orphan_card_result')
            done.append({'orphan_id': row['id'], 'task_id': row['task_id'], 'choice': choice, 'dispatch_id': dispatch_id})
        for row in db.execute("SELECT * FROM task_deadlines WHERE disposition IN ('carded','lapsed') AND card_event IS NOT "
                              'NULL').fetchall():
            found = _decision(rt, db, row['card_event'])
            if found is None:
                continue
            d, option = found
            if d['actor'] != self.owner_user(rt):
                continue
            choice, dispatch_id, error = option['id'], None, None
            if choice == 'agent':
                try:
                    dispatch_id = _redispatch(rt, db, db_path, row['task_id'], 'deadline:' + row['id'],
                                              self.deadline_prompt.format(task=row['task_id'], ask=row['ask'] or '—',
                                                                     note=row['note']), now)
                except Exception as exc:  # noqa: BLE001
                    error = (str(exc) if isinstance(exc, ValueError) else rt.safe_error(exc))[:300]
            with rt.transaction(db):
                db.execute('UPDATE task_deadlines SET disposition=?,choice=?,decided_by=?,decided_at=?,dispatch_id=?,error=?,'
                           'updated_at=? WHERE id=?', (DEADLINE_DISPOSITION.get(choice, 'handled'), choice, d['actor'],
                                                       d['decided_at'], dispatch_id, error, now, row['id']))
            rt.rules._card_reply(rt, db, row['card_event'], self.deadline_reply.get(choice, self.msg('lifecycle.settle.3')), row['id'],
                                 kind='deadline_card_result')
            done.append({'deadline_id': row['id'], 'task_id': row['task_id'], 'choice': choice})
        return done
