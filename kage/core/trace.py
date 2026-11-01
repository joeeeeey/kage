"""P48 收件轨迹: the whole processing chain of one inbox item, read from the runtime's SQLite (read-only, metadata only).

An inbox item is an intake burst (one or more Slack messages merged), a message intake saw but did not burst (filtered,
or covered by a task's own watch), the owner's reply in a card thread, or a local agent message. `build` turns one into the
eight stages the owner reads on the 收件 page — 收到 · 过滤 / 合并 · 分诊 · 路由 · 派发 · 卡片 · 回复 · 结清 — each with a
status (done / active / <owner key> / failed / skip), its first time, plain-Chinese facts and links, plus the chronological
steps behind them (a stage can happen twice: a burst routed to <owner key>-inbox and then handed off is routed and dispatched
twice). A stage nothing reached says why when the data says why (held behind a failed dispatch, logged as chit-chat,
filtered as our own agent's post).

It joins what the trace CLI joined — intake_seen / intake_bursts / triage_records, escalations / escalation_cards /
decisions / rule_checks / automation_gaps, card_reply_routes / card_reply_targets, events → dispatch_routes → dispatches
→ consumer_runs, intake_sends / agent_sends — never calls Slack and never reads a message body. The only free text is
the agent's own triage basis / note and the last summary lines of its local run log, both masked like the trace CLI
(secret-looking values, long token-like strings, Slack mention syntax) and cut short.

  python3 trace_chain.py [--db PATH] [--json] REF
  REF = Slack link | CHANNEL/TS | TS | burst id | event id | dispatch id | run id | task id
"""
import argparse
import contextlib
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

from kage import locale

_OWNER_DEFAULT = object()  # a parameter whose default is the owner's value, filled in at call time


SESSION_CHARS = 13


STUCK_SECONDS = 900          # waiting this long before any run picked it up = stuck


OPEN_CARD_DAYS = 7           # an undecided card older than this no longer counts as open (card_replies.OPEN_CARD_DAYS)


SUMMARY_LINES = 3


LINE_LIMIT = 260


NOTE_LIMIT = 160


LOG_TAIL_BYTES = 65536


LIST_DAYS = 3


LIST_MAX_DAYS = 14


SECRETISH = re.compile(r'[A-Za-z0-9+/_=-]{40,}')


TS_RE = re.compile(r'\d{9,10}\.\d{1,6}')


BURST_RE = re.compile(r'in[0-9a-f]{14}')


EVENT_RE = re.compile(r'[0-9a-f]{24}')


DISPATCH_RE = re.compile(r'd[0-9a-f]{15}')


RUN_RE = re.compile(r'r\d{8}T\d{6}Z-[0-9a-f]{6}')


CHANNEL_RE = re.compile(r'[CDG][A-Z0-9]{2,}')


TASK_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{1,159}')


LINK_RE = re.compile(r'archives/([CDG][A-Z0-9]+)/p(\d{10})(\d{6})')


def tsf(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sid(session):
    return str(session or '')[:SESSION_CHARS] or None


def slack_url(channel, ts=None, thread=None):
    if not CHANNEL_RE.fullmatch(str(channel or '')):
        return None
    if not ts or not TS_RE.fullmatch(str(ts)):
        return None  # a link is always to one message; a bare channel link says nothing
    url = f'https://app.slack.com/archives/{channel}/p' + str(ts).replace('.', '')
    if thread and str(thread) != str(ts) and TS_RE.fullmatch(str(thread)):
        url += f'?thread_ts={thread}&cid={channel}'
    return url


def link(label, href, kind='page'):
    return {'label': label, 'href': href, 'kind': kind} if href else None


def _loads(text, default):
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


class Store:
    def __init__(self, db, now=None, runs_root=None, logs=True, inbox_tasks=_OWNER_DEFAULT, *, core):
        self.core = core
        inbox_tasks = self.core.inbox_tasks if inbox_tasks is _OWNER_DEFAULT else inbox_tasks
        self.db, self.now, self.runs_root, self.logs = db, time.time() if now is None else now, runs_root, logs
        self.inbox_tasks = tuple(inbox_tasks)
        self.tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self._cols, self._memo, self._envelopes = {}, {}, None

    def has(self, table, column=None):
        if table not in self.tables:
            return False
        if column is None:
            return True
        if table not in self._cols:
            self._cols[table] = {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}
        return column in self._cols[table]

    def all(self, sql, args=(), table=None):
        if table and not self.has(table):
            return []
        return [dict(r) for r in self.db.execute(sql, args)]

    def one(self, sql, args=(), table=None):
        rows = self.all(sql, args, table)
        return rows[0] if rows else None

    def memo(self, key, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def event(self, eid):
        return self.memo(('event', eid), lambda: self.one('SELECT * FROM events WHERE id=?', (eid,), 'events'))

    def watch(self, wid):
        def load():
            row = self.one('SELECT data FROM watches WHERE id=?', (wid,), 'watches')
            return _loads(row['data'], {}) if row else {}
        return self.memo(('watch', wid), load)

    def task_paths(self):
        return self.memo('task_paths', lambda: {str(r['path']).rstrip('/'): r['task_id'] for r in self.all(
            'SELECT task_id, path FROM tasks', (), 'tasks')})

    def task_ids(self):
        return self.memo('task_ids', lambda: set(self.task_paths().values()))

    def task_of_watch(self, w):
        if not w:
            return None
        if w.get('task_id'):
            return w['task_id']
        cwd = str(w.get('cwd') or '').rstrip('/')
        if cwd in self.task_paths():
            return self.task_paths()[cwd]
        name = cwd.rsplit('/', 1)[-1]
        return name if name in self.task_ids() else None

    def task_of_event(self, e):
        if e is None:
            return None
        local = self.one('SELECT task_id FROM local_messages WHERE event_id=?', (e['id'],), 'local_messages')
        return (local or {}).get('task_id') or self.task_of_watch(self.watch(e['watch_id']))

    def task_of_session(self, session):
        if not session:
            return None
        def load():
            row = self.one('SELECT task FROM consumer_runs WHERE session=? ORDER BY started_at DESC LIMIT 1', (session,),
                           'consumer_runs')
            if row:
                return row['task']
            row = self.one('SELECT task_id FROM tasks WHERE current_session=?', (session,), 'tasks')
            return row['task_id'] if row else None
        return self.memo(('session_task', session), load)

    def dispatch(self, did):
        return self.memo(('dispatch', did), lambda: self.one('SELECT * FROM dispatches WHERE dispatch_id=?', (did,),
                                                             'dispatches'))

    def runs(self, did):
        return self.memo(('runs', did), lambda: self.all('SELECT * FROM consumer_runs WHERE dispatch_id=? ORDER BY started_at',
                                                         (did,), 'consumer_runs'))

    def pause(self, task_key):
        if not self.has('dispatch_pauses'):
            return None
        return self.memo(('pause', task_key), lambda: self.one(
            'SELECT * FROM dispatch_pauses WHERE task_key=? AND resumed_at IS NULL', (task_key,)))

    def profile(self, uid):
        if not uid or not self.has('dashboard_profiles'):
            return None
        return self.memo(('profile', uid), lambda: self.one(
            'SELECT name, avatar IS NOT NULL AS has_avatar FROM dashboard_profiles WHERE user_id=?', (uid,)))

    def channel_name(self, channel):
        if not channel or not self.has('dashboard_channels'):
            return None
        row = self.memo(('channel', channel), lambda: self.one('SELECT name FROM dashboard_channels WHERE channel=?',
                                                                (channel,)))
        return (row or {}).get('name')

    def burst_events(self, burst_id):
        """Every event this burst was delivered as (a handoff re-delivers the same burst), oldest first."""
        if self._envelopes is not None:
            return self._envelopes.get(burst_id, [])
        return [r['event_id'] for r in self.all('SELECT event_id FROM event_envelopes WHERE burst_id=? ORDER BY created_at',
                                                (burst_id,), 'event_envelopes')]

    def prefetch_envelopes(self, since):
        self._envelopes = {}
        if self.has('event_envelopes', 'burst_id'):
            for r in self.db.execute('SELECT burst_id, event_id FROM event_envelopes WHERE burst_id IS NOT NULL AND '
                                     'created_at>=? ORDER BY created_at', (since,)):
                self._envelopes.setdefault(r[0], []).append(r[1])

    def run_lines(self, run):
        """The agent's last 说明 / 结果 lines of its local readable log (masked, cut short); only runs/<run-id> is read."""
        if not self.logs or not self.runs_root or not run.get('run_id') or not RUN_RE.fullmatch(run['run_id']):
            return []
        root = Path(self.runs_root)
        path = root / run['run_id']
        try:
            if run.get('log_dir') and Path(run['log_dir']).expanduser().resolve() != path.resolve():
                return []
            if path.is_symlink() or not path.is_dir():
                return []
            with (path / 'readable.log').open('rb') as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - LOG_TAIL_BYTES))
                text = fh.read().decode('utf-8', errors='replace')
        except OSError:
            return []
        said = [l for l in text.splitlines() if self.core.msg('trace.Store.run_lines.1') in l or self.core.msg('trace.Store.run_lines.2') in l]
        return [self.core.masked(l, LINE_LIMIT) for l in said[-SUMMARY_LINES:]]


class Trace:
    def __init__(self, store, subject, *, core):
        self.core = core
        self.s, self.subject = store, subject
        self.steps, self.stage_note, self.tasks, self.flags = [], {}, [], set()
        self.meta = {'kind': subject['kind'], 'ref': subject.get('ref')}
        self.stuck_since = None
        self.seen_runs, self.seen_cards, self.seen_sends = set(), set(), set()
        self.intent = self.disposition = None

    def add(self, stage, status, at, title, facts=(), links=(), lines=(), **extra):
        step = {'stage': stage, 'status': status, 'at': tsf(at), 'title': title,
                'facts': [list(f) for f in facts if f and f[1] not in (None, '', [], ())],
                'links': [l for l in links if l], 'lines': [l for l in lines if l]}
        step.update({k: v for k, v in extra.items() if v is not None})
        self.steps.append(step)
        return step

    def note(self, stage, reason, status=_OWNER_DEFAULT):
        """Why a stage has no step (not reached / not applicable); ACTIVE = it is coming (still being processed)."""
        status = self.core.skip if status is _OWNER_DEFAULT else status
        self.stage_note.setdefault(stage, (status, reason))

    def task(self, task_id):
        if task_id and task_id not in self.tasks:
            self.tasks.append(task_id)

    def stuck(self, since):
        since = tsf(since)
        if since is not None and (self.stuck_since is None or since < self.stuck_since):
            self.stuck_since = since

    def finish(self):
        now = self.s.now
        for i, step in enumerate(self.steps):
            step['_sort'] = (step['at'] if step['at'] is not None else float('inf'), i)
        self.steps.sort(key=lambda x: x.pop('_sort'))
        stages = []
        for key, label in self.core.stages:
            mine = [x for x in self.steps if x['stage'] == key]
            statuses = {x['status'] for x in mine}
            status = (self.core.failed if self.core.failed in statuses else self.core.owner_wait if self.core.owner_wait in statuses else self.core.active if self.core.active in statuses
                      else self.core.done if self.core.done in statuses else None)
            note = self.stage_note.get(key)
            if status is None:
                status = note[0] if note else self.core.skip
            times = [x['at'] for x in mine if x['at'] is not None]
            head = next((x for x in reversed(mine) if x['status'] == status), mine[-1] if mine else None)
            stages.append({'key': key, 'label': label, 'status': status, 'at': min(times) if times else None,
                           'summary': head['title'] if head else (note[1] if note else self.core.msg('trace.Trace.finish.1')),
                           'count': len(mine), 'reason': None if mine else (note[1] if note else None)})
        statuses = [x['status'] for x in stages]
        overall = ('failed' if self.core.failed in statuses else self.core.key if self.core.owner_wait in statuses else 'waiting' if self.core.active in statuses
                   else 'done')
        pending = [x for x in stages if x['status'] in (self.core.failed, self.core.owner_wait, self.core.active)]
        reached = [x for x in stages if x['status'] != self.core.skip]
        current = (pending[0] if pending else reached[-1] if reached else stages[0])['key']
        if overall == 'failed':
            self.flags.add('failed')
        if overall == self.core.key:
            self.flags.add(f'needs_{self.core.key}')
        outcome = next((o for o in self.core.outcome_order if o in self.flags), 'pending' if overall == 'waiting' else 'delivered')
        stuck = overall == 'waiting' and self.stuck_since is not None and now - self.stuck_since > STUCK_SECONDS
        times = [x['at'] for x in self.steps if x['at'] is not None]
        trace = dict(self.meta)
        trace.update(status=overall, stuck=stuck, outcome=outcome, flags=sorted(self.flags), current=current,
                     current_label=dict(self.core.stages)[current], stages=stages, steps=self.steps, tasks=self.tasks,
                     intent=self.intent, disposition=self.disposition,
                     started_at=min(times) if times else None, updated_at=max(times) if times else None,
                     generated_at=now)
        trace['live'] = any(x['status'] == self.core.active for x in stages)
        trace['headline'] = self.core.headline(trace)
        return trace


def _recorder(s, recorded_by, task=True):
    """`claude:c511…` → `<owner key>-inbox · claude c511713b-f569` (the task its session ran for, when known)."""
    if not recorded_by:
        return None
    provider, _, session = str(recorded_by).partition(':')
    if not session:
        return provider
    owner = s.task_of_session(session) if task else None
    return ' · '.join(x for x in (owner, f'{provider} {sid(session)}') if x)


def parse_ref(text):
    """Slack link / CHANNEL/TS / TS / burst / event / dispatch / run / task id, optionally prefixed `burst:` … `task:`."""
    raw = str(text or '').strip()
    if not raw or len(raw) > 600 or any(ord(c) < 32 for c in raw):
        return None
    m = LINK_RE.search(raw)
    if m:
        thread = re.search(r'thread_ts=(\d+\.\d+)', raw)
        return {'kind': 'msg', 'channel': m[1], 'ts': f'{m[2]}.{m[3]}', 'thread': thread[1] if thread else None}
    prefix, sep, rest = raw.partition(':')
    if sep and prefix in ('burst', 'event', 'reply', 'seen', 'dispatch', 'run', 'task', 'card', 'msg'):
        parsed = parse_ref(rest)
        if parsed is None:
            return None
        if prefix in ('reply', 'seen', 'msg') and parsed['kind'] == 'msg':
            return {**parsed, 'kind': prefix}
        if prefix == 'card' and parsed['kind'] == 'event':
            return {'kind': 'card', 'id': parsed['id']}
        if prefix == 'task':
            return {'kind': 'task', 'id': rest.strip()} if TASK_RE.fullmatch(rest.strip()) else None
        return parsed if parsed['kind'] == prefix else None
    m = re.fullmatch(r'([CDG][A-Z0-9]{2,})[/: ](\d{9,10}\.\d{1,6})', raw)
    if m:
        return {'kind': 'msg', 'channel': m[1], 'ts': m[2], 'thread': None}
    if TS_RE.fullmatch(raw):
        return {'kind': 'ts', 'ts': raw}
    if BURST_RE.fullmatch(raw):
        return {'kind': 'burst', 'id': raw}
    if EVENT_RE.fullmatch(raw):
        return {'kind': 'event', 'id': raw}
    if DISPATCH_RE.fullmatch(raw):
        return {'kind': 'dispatch', 'id': raw}
    if RUN_RE.fullmatch(raw):
        return {'kind': 'run', 'id': raw}
    if TASK_RE.fullmatch(raw):
        return {'kind': 'task', 'id': raw}
    return None


def _subject_for_event(s, eid, focus=None, depth=0):
    if s.has('intake_bursts'):
        b = s.one('SELECT burst_id FROM intake_bursts WHERE event_id=?', (eid,))
        env = s.one('SELECT burst_id FROM event_envelopes WHERE event_id=?', (eid,), 'event_envelopes') if not b else None
        burst = (b or env or {}).get('burst_id')
        if burst and s.one('SELECT 1 AS x FROM intake_bursts WHERE burst_id=?', (burst,)):
            return {'kind': 'burst', 'id': burst, 'focus': focus}
    tg = s.one('SELECT channel, reply_ts FROM card_reply_targets WHERE event_id=?', (eid,), 'card_reply_targets')
    if tg and s.one('SELECT 1 AS x FROM card_reply_routes WHERE channel=? AND reply_ts=?', (tg['channel'], tg['reply_ts']),
                    'card_reply_routes'):
        return {'kind': 'card_reply', 'channel': tg['channel'], 'ts': tg['reply_ts'], 'focus': focus}
    if s.one("SELECT 1 AS x FROM local_messages WHERE event_id=? AND kind!='intake'", (eid,), 'local_messages'):
        return {'kind': 'local', 'id': eid, 'focus': focus}
    if depth == 0:
        d = s.one('SELECT event_id FROM decisions WHERE delivered_event=?', (eid,), 'decisions')
        if d:
            return _subject_for_card(s, d['event_id'], 'cards')
        if s.one('SELECT 1 AS x FROM escalation_cards WHERE event_id=?', (eid,), 'escalation_cards'):
            return _subject_for_card(s, eid, 'cards')
    e = s.event(eid)
    if e is None:
        return None
    seen = s.one('SELECT burst_id FROM intake_seen WHERE channel=? AND ts=?', (e['channel'], e['ts']), 'intake_seen')
    if seen:
        return ({'kind': 'burst', 'id': seen['burst_id'], 'focus': focus} if seen.get('burst_id') else
                {'kind': 'seen', 'channel': e['channel'], 'ts': e['ts'], 'focus': focus})
    return {'kind': 'event', 'id': eid, 'focus': focus}


def _subject_for_card(s, card_event, focus='cards'):
    br = s.one('SELECT burst_id FROM intake_briefs WHERE card_event=?', (card_event,), 'intake_briefs')
    if br:
        return {'kind': 'burst', 'id': br['burst_id'], 'focus': focus}
    c = s.one('SELECT c.created, w.data AS wdata FROM escalation_cards c JOIN escalations x ON x.event_id=c.event_id '
              'LEFT JOIN watches w ON w.id=x.watch_id WHERE c.event_id=?', (card_event,), 'escalation_cards')
    if c is None:
        return None
    wd = _loads(c.get('wdata'), {})
    session = (wd.get('posted_by') or {}).get('session') or wd.get('session')
    run = s.one('SELECT event_ids, dispatch_id FROM consumer_runs WHERE session=? AND started_at<=? AND '
                '(ended_at IS NULL OR ended_at>=?) ORDER BY started_at DESC LIMIT 1', (session, c['created'] + 2, c['created'] - 5),
                'consumer_runs') if session else None
    for eid in _loads((run or {}).get('event_ids'), []):
        found = _subject_for_event(s, eid, focus, depth=1)
        if found:
            return found
    if run and run.get('dispatch_id'):  # the first run of a task intake launched: the card belongs to that burst
        found = [x for x in _subjects_for_dispatch(s, run['dispatch_id'], depth=1) if x['kind'] == 'burst']
        if found:
            return {**found[0], 'focus': focus}
    return {'kind': 'card', 'id': card_event, 'focus': focus}


def _subjects_for_dispatch(s, did, depth=0):
    out = []
    eids = [r['event_id'] for r in s.all('SELECT DISTINCT event_id FROM dispatch_routes WHERE dispatch_id=?', (did,),
                                         'dispatch_routes') if r.get('event_id')]
    eids += [r['id'] for r in s.all('SELECT id FROM events WHERE receipt=?', ('dispatch:' + did,), 'events')]
    for run in s.runs(did):
        eids += _loads(run.get('event_ids'), [])
    for eid in dict.fromkeys(eids):
        found = _subject_for_event(s, eid, 'dispatch', depth)
        if found:
            out.append(found)
    d = s.dispatch(did)
    if not out and d and d.get('trigger') == 'launch' and d.get('task_id'):
        rec = s.one("SELECT burst_id FROM triage_records WHERE disposition='launch' AND target_task=? ORDER BY created_at LIMIT 1",
                    (d['task_id'],), 'triage_records')
        if rec and rec.get('burst_id'):
            out.append({'kind': 'burst', 'id': rec['burst_id'], 'focus': 'dispatch'})
    return out


def _subject_for_msg(s, channel, ts, kind='msg'):
    if kind in ('msg', 'reply') and s.one('SELECT 1 AS x FROM card_reply_routes WHERE channel=? AND reply_ts=?',
                                          (channel, ts), 'card_reply_routes') and (kind == 'reply' or not s.one(
                                              'SELECT 1 AS x FROM intake_seen WHERE channel=? AND ts=? AND burst_id IS NOT NULL',
                                              (channel, ts), 'intake_seen')):
        return [{'kind': 'card_reply', 'channel': channel, 'ts': ts}]
    if kind == 'reply':
        return []
    seen = s.one('SELECT burst_id FROM intake_seen WHERE channel=? AND ts=?', (channel, ts), 'intake_seen')
    if seen:
        return [{'kind': 'burst', 'id': seen['burst_id']} if seen.get('burst_id') else
                {'kind': 'seen', 'channel': channel, 'ts': ts}]
    if kind == 'seen':
        return []
    out = []
    for b in s.all("SELECT burst_id FROM intake_bursts WHERE channel=? AND (first_ts=? OR refs LIKE ?)",
                   (channel, ts, f'%"{ts}"%'), 'intake_bursts'):
        out.append({'kind': 'burst', 'id': b['burst_id']})
    if out:
        return out
    for e in s.all('SELECT id FROM events WHERE channel=? AND ts=? ORDER BY received', (channel, ts), 'events'):
        found = _subject_for_event(s, e['id'])
        if found and found not in out:
            out.append(found)
    if out:
        return out
    sent = s.one('SELECT * FROM agent_sends WHERE channel=? AND ts=?', (channel, ts), 'agent_sends')
    if sent and sent.get('ref'):
        ref = parse_ref(sent['ref'])
        if ref and ref['kind'] == 'burst':
            return [{'kind': 'burst', 'id': ref['id'], 'focus': 'replies'}]
        if ref and ref['kind'] == 'event':
            found = _subject_for_event(s, ref['id'], 'cards')
            return [found] if found else []
        if ref and ref['kind'] == 'dispatch':
            return _subjects_for_dispatch(s, ref['id'])
    card = s.one('SELECT event_id FROM escalations WHERE thread_ts=?', (ts,), 'escalations')
    if card:
        return [_subject_for_card(s, card['event_id'])]
    d = s.one('SELECT dispatch_id FROM dispatches WHERE card_ts=? OR fail_card_ts=?', (ts, ts), 'dispatches')
    return _subjects_for_dispatch(s, d['dispatch_id']) if d else []


def resolve(store, parsed):
    """Inbox items (subjects) a parsed reference points at, most specific first. A task is resolved by the caller."""
    s, k = store, (parsed or {}).get('kind')
    if k == 'burst':
        return [{'kind': 'burst', 'id': parsed['id']}] if s.one(
            'SELECT 1 AS x FROM intake_bursts WHERE burst_id=?', (parsed['id'],), 'intake_bursts') else []
    if k == 'event':
        found = _subject_for_event(s, parsed['id'])
        return [found] if found else []
    if k == 'card':
        found = _subject_for_card(s, parsed['id'])
        return [found] if found else []
    if k == 'dispatch':
        return _subjects_for_dispatch(s, parsed['id'])
    if k == 'run':
        run = s.one('SELECT dispatch_id, event_ids FROM consumer_runs WHERE run_id=?', (parsed['id'],), 'consumer_runs')
        if not run:
            return []
        if run.get('dispatch_id'):
            return _subjects_for_dispatch(s, run['dispatch_id'])
        return [x for x in (_subject_for_event(s, e) for e in _loads(run.get('event_ids'), [])) if x]
    if k in ('msg', 'reply', 'seen'):
        return _subject_for_msg(s, parsed['channel'], parsed['ts'], k)
    if k == 'ts':
        out = []
        for table, col, chan in (('intake_seen', 'ts', 'channel'), ('card_reply_routes', 'reply_ts', 'channel'),
                                 ('events', 'ts', 'channel'), ('agent_sends', 'ts', 'channel')):
            for r in s.all(f'SELECT DISTINCT {chan} AS c FROM {table} WHERE {col}=?', (parsed['ts'],), table):
                for x in _subject_for_msg(s, r['c'], parsed['ts']):
                    if x not in out:
                        out.append(x)
        return out
    return []


def summary(trace):
    return {k: trace.get(k) for k in ('ref', 'kind', 'at', 'title', 'where', 'who', 'channel', 'conv_type', 'msg_count',
                                      'slack_url', 'status', 'stuck', 'outcome', 'flags', 'current', 'current_label',
                                      'headline', 'tasks', 'intent', 'disposition', 'live', 'smoke', 'updated_at')} | {
        'pipeline': [x['status'] for x in trace['stages']]}


def list_subjects(s, since):
    subjects = []
    for r in s.all('SELECT burst_id FROM intake_bursts WHERE created_at>=? ORDER BY created_at DESC', (since,), 'intake_bursts'):
        subjects.append({'kind': 'burst', 'id': r['burst_id']})
    replies = {(r['channel'], r['reply_ts']) for r in s.all('SELECT channel, reply_ts FROM card_reply_routes WHERE at>=?',
                                                            (since,), 'card_reply_routes')}
    for r in s.all('SELECT channel, ts FROM intake_seen WHERE burst_id IS NULL AND seen_at>=? ORDER BY seen_at DESC',
                   (since,), 'intake_seen'):
        if (r['channel'], r['ts']) not in replies:
            subjects.append({'kind': 'seen', 'channel': r['channel'], 'ts': r['ts']})
    for channel, ts in sorted(replies, key=lambda x: x[1], reverse=True):
        subjects.append({'kind': 'card_reply', 'channel': channel, 'ts': ts})
    for r in s.all("SELECT event_id FROM local_messages WHERE kind!='intake' AND created_at>=? ORDER BY created_at DESC",
                   (since,), 'local_messages'):
        subjects.append({'kind': 'local', 'id': r['event_id']})
    return subjects


def open_db(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    return db


class TraceCore:
    """The owner-bound half of the inbox trace: the stage, route, card and reply words in the owner's language,
    the owner's name, key (status, view and stored values), Slack user and time zone, the inbox task
    names, the org's ticket words and the deployment's run-log module; Store and Trace hold one."""

    def __init__(self, language, *, tz, owner_key, owner_name, owner_slack_user, ticket_ref_reason, ticket_team, run_log):
        self.language = language
        self.tz = tz
        self.owner_slack_user = owner_slack_user
        self.ticket_ref_reason = ticket_ref_reason
        self.ticket_team = ticket_team
        self.run_log = run_log
        self.key, self.name = owner_key, owner_name
        self.inbox_tasks = (f'{self.key}-inbox', f'{self.key}-oncall')
        self.stages = (('received', self.msg('trace.stages.1')), ('filtered', self.msg('trace.stages.2')), ('triage', self.msg('trace.stages.3')), ('route', self.msg('trace.stages.4')), ('dispatch', self.msg('trace.stages.5')),
                       ('cards', self.msg('trace.stages.6')), ('replies', self.msg('trace.stages.7')), ('resolved', self.msg('trace.stages.8')))
        self.done, self.active, self.owner_wait, self.failed, self.skip = 'done', 'active', self.key, 'failed', 'skip'
        self.conv = {'im': self.msg('trace.conv.im'), 'mpim': self.msg('trace.conv.mpim'), 'channel': self.msg('trace.conv.channel')}
        self.sender_kind = {'human': self.msg('trace.sender_kind.human'), 'bot': 'bot', 'agent': self.msg('trace.sender_kind.agent'), 'system': self.msg('trace.sender_kind.system')}
        self.source = {'dm_history': self.msg('trace.source.dm_history'), 'dm_search': self.msg('trace.source.dm_search'), 'mention_search': self.msg('trace.source.mention_search', owner=self.name), 'channel_history': self.msg('trace.source.channel_history'),
                       'smoke': self.msg('trace.source.smoke')}
        self.seen_reason = {
                 'burst': self.msg('trace.seen_reason.burst'), 'alert_duplicate': self.msg('trace.seen_reason.alert_duplicate'),
                 f'{self.key}_authored': self.msg('trace.seen_reason.owner_authored', owner=self.name), 'our_agent': self.msg('trace.seen_reason.our_agent'),
                 'system_noise': self.msg('trace.seen_reason.system_noise'), 'bot_outside_oncall': self.msg('trace.seen_reason.bot_outside_oncall'),
                 'covered_by_watch': self.msg('trace.seen_reason.covered_by_watch'), 'pending': self.msg('trace.seen_reason.pending')}
        self.route = {'inbox': self.msg('trace.route.inbox'), 'oncall': 'oncall', 'bound': self.msg('trace.route.bound'), 'ref': self.msg('trace.route.ref'), 'handoff': self.msg('trace.route.handoff')}
        self.route_reason = {
                 'default_inbox': self.msg('trace.route_reason.default_inbox'), 'oncall_alert': self.msg('trace.route_reason.oncall_alert'),
                 'intake_binding': self.msg('trace.route_reason.intake_binding'), self.ticket_ref_reason: self.msg('trace.route_reason.ticket_ref_reason', ticket_team=self.ticket_team),
                 'pr_link': self.msg('trace.route_reason.pr_link'), 'dm_new_topic': self.msg('trace.route_reason.dm_new_topic'),
                 'dm_conflict': self.msg('trace.route_reason.dm_conflict'), 'dm_unresolved_ref': self.msg('trace.route_reason.dm_unresolved_ref'),
                 'dm_conversation': self.msg('trace.route_reason.dm_conversation'), 'ask_card': self.msg('trace.route_reason.ask_card')}
        self.route_prefix = (('watch:', self.msg('trace.route_prefix.1')), ('ask_thread:', self.msg('trace.route_prefix.2')),
                             ('dm_inflight:ask_card:', self.msg('trace.route_prefix.3')),
                             ('dm_inflight:watch:', self.msg('trace.route_prefix.4')), ('handoff_from:', self.msg('trace.route_prefix.5')),
                             ('handoff_of_event:', self.msg('trace.route_prefix.6')))
        self.intent = {'request': self.msg('trace.intent.request'), 'question': self.msg('trace.intent.question'), 'directive': self.msg('trace.intent.directive'), 'follow_up': self.msg('trace.intent.follow_up'), 'fyi': self.msg('trace.intent.fyi'), 'chit_chat': self.msg('trace.intent.chit_chat'),
                       'alert': self.msg('trace.intent.alert'), 'other': self.msg('trace.intent.other')}
        self.disposition = {'forward': self.msg('trace.disposition.forward'), 'launch': self.msg('trace.disposition.launch'), 'answer': self.msg('trace.disposition.answer'), 'ask_requester': self.msg('trace.disposition.ask_requester'),
                            'brief': self.msg('trace.disposition.brief', owner=self.name), f'card_{self.key}': self.msg('trace.disposition.card_owner', owner=self.name), 'log': self.msg('trace.disposition.log'), 'ignore': self.msg('trace.disposition.ignore'), 'investigate': self.msg('trace.disposition.investigate'),
                            'escalate': self.msg('trace.disposition.escalate')}
        self.clarity = {'clear': self.msg('trace.clarity.clear'), 'partial': self.msg('trace.clarity.partial'), 'vague': self.msg('trace.clarity.vague')}
        self.dispatch_state = {'waiting': self.msg('trace.dispatch_state.waiting'), 'running': self.msg('trace.dispatch_state.running'), 'paused': self.msg('trace.dispatch_state.paused'), 'failed': self.msg('trace.dispatch_state.failed'), f'needs_{self.key}': self.msg('trace.dispatch_state.needs_owner', owner=self.name),
                               'done': self.msg('trace.dispatch_state.done'), 'superseded': self.msg('trace.dispatch_state.superseded'), 'stopped': self.msg('trace.dispatch_state.stopped')}
        self.hold_reason = {
                 'revive': self.msg('trace.hold_reason.revive'), 'task_run_active': self.msg('trace.hold_reason.task_run_active'),
                 f'task_failed_waiting_for_{self.key}': self.msg('trace.hold_reason.task_failed_waiting_for_owner', owner=self.name),
                 'task_paused': self.msg('trace.hold_reason.task_paused'), 'dispatch_waiting': self.msg('trace.hold_reason.dispatch_waiting'),
                 'stranded_revive': self.msg('trace.hold_reason.stranded_revive'), 'stranded_task_run_active': self.msg('trace.hold_reason.stranded_task_run_active'),
                 'launch': self.msg('trace.hold_reason.launch')}
        self.switch_reason = {'long_lived': self.msg('trace.switch_reason.long_lived'), 'context_cap': self.msg('trace.switch_reason.context_cap'), 'degraded_reroute': self.msg('trace.switch_reason.degraded_reroute'),
                              'auto_return': self.msg('trace.switch_reason.auto_return'), 'stuck_on_degraded': self.msg('trace.switch_reason.stuck_on_degraded'),
                              'rerouted_to_live_current': self.msg('trace.switch_reason.rerouted_to_live_current'), 'rerouted_to_live_task_session': self.msg('trace.switch_reason.rerouted_to_live_task_session')}
        self.direct_reason = {'watch_session_live': self.msg('trace.direct_reason.watch_session_live'), 'watch_liveness_unknown': self.msg('trace.direct_reason.watch_liveness_unknown'),
                              'current_liveness_unknown': self.msg('trace.direct_reason.current_liveness_unknown')}
        self.outcome = {'success': self.msg('trace.outcome.success'), 'failed': self.msg('trace.outcome.failed'), 'partial': self.msg('trace.outcome.partial'), 'unresolved': self.msg('trace.outcome.unresolved'), 'stopped': self.msg('trace.outcome.stopped')}
        self.error = {'transient': self.msg('trace.error.transient'), 'rate_limited': self.msg('trace.error.rate_limited'), 'quota': self.msg('trace.error.quota'), 'auth': self.msg('trace.error.auth'),
                      'model_unsupported': self.msg('trace.error.model_unsupported'), 'session_missing': self.msg('trace.error.session_missing'), 'session_live': self.msg('trace.error.session_live'),
                      'unknown': self.msg('trace.error.unknown'), 'task_unresolved': self.msg('trace.error.task_unresolved'), 'run_timeout': self.msg('trace.error.run_timeout'), 'spawn_failed': self.msg('trace.error.spawn_failed'),
                      'stopped': self.msg('trace.error.stopped'), 'crashed': self.msg('trace.error.crashed'), 'provider_degraded': self.msg('trace.error.provider_degraded')}
        self.trigger = {'slack_message': self.msg('trace.trigger.slack_message'), 'decision_answer': self.msg('trace.trigger.decision_answer'), 'ask_answer': self.msg('trace.trigger.ask_answer'), 'agent_message': self.msg('trace.trigger.agent_message'),
                        'note': self.msg('trace.trigger.note'), 'routine': self.msg('trace.trigger.routine'), 'launch': self.msg('trace.trigger.launch'), 'event': self.msg('trace.trigger.event')}
        self.event_status = {'pending': self.msg('trace.event_status.pending'), 'queued': self.msg('trace.event_status.queued'), 'claimed': self.msg('trace.event_status.claimed'),
                             'sending': self.msg('trace.event_status.sending'), 'delivery_unknown': self.msg('trace.event_status.delivery_unknown'), 'dispatched': self.msg('trace.event_status.dispatched'), 'handled': self.msg('trace.event_status.handled')}
        self.resolved = {'waiting': self.msg('trace.resolved.waiting'), 'observe': self.msg('trace.resolved.observe'), 'decision': self.msg('trace.resolved.decision'),
                         'irrelevant': self.msg('trace.resolved.irrelevant'), 'handed_off': self.msg('trace.resolved.handed_off'), f'ask_{self.key}': self.msg('trace.resolved.ask_owner'), 'smoke_test': self.msg('trace.resolved.smoke_test')}
        self.reply_kind = {'intake_reply': self.msg('trace.reply_kind.intake_reply', owner=self.name), 'intake_ack': self.msg('trace.reply_kind.intake_ack', owner=self.name), 'ask_card': self.msg('trace.reply_kind.ask_card', owner=self.name),
                           'ask_reminder': self.msg('trace.reply_kind.ask_reminder'), 'ask_echo': self.msg('trace.reply_kind.ask_echo')}
        self.send_state = {'sent': self.done, 'sending': self.active, 'delivery_unknown': self.failed}
        self.card_kind = {'brief': self.msg('trace.card_kind.brief'), 'draft': self.msg('trace.card_kind.draft'), f'ask_{self.key}': self.msg('trace.card_kind.ask_owner', owner=self.name), 'blocker': self.msg('trace.card_kind.blocker'), 'card': self.msg('trace.card_kind.card')}
        self.provenance = {'slack_button': self.msg('trace.provenance.slack_button'), 'text_confirmed': self.msg('trace.provenance.text_confirmed'), 'thread_reply': self.msg('trace.provenance.thread_reply'), 'smoke_synthetic': self.msg('trace.provenance.smoke_synthetic')}
        self.rule_outcome = {'auto': self.msg('trace.rule_outcome.auto'), 'one_tap': self.msg('trace.rule_outcome.one_tap'), 'clarify': self.msg('trace.rule_outcome.clarify'), 'route': self.msg('trace.rule_outcome.route'), self.key: self.msg('trace.rule_outcome.owner', owner=self.name)}
        self.reply_outcome = {'decided': self.msg('trace.reply_outcome.decided'), 'retired': self.msg('trace.reply_outcome.retired'), 'kept': self.msg('trace.reply_outcome.kept'),
                              'guidance_no_open_card': self.msg('trace.reply_outcome.guidance_no_open_card'), 'already_retired': self.msg('trace.reply_outcome.already_retired'),
                              'already_decided': self.msg('trace.reply_outcome.already_decided')}
        self.reply_class = {'decided': self.msg('trace.reply_class.decided'), 'plan_change': self.msg('trace.reply_class.plan_change'), 'kept': self.msg('trace.reply_class.kept')}
        self.reply_target = {'approvals_root': self.msg('trace.reply_target.approvals_root'), 'card': self.msg('trace.reply_target.card'), 'handoff': self.msg('trace.reply_target.handoff'),
                             'card_task_unknown': self.msg('trace.reply_target.card_task_unknown'), 'run_card': self.msg('trace.reply_target.run_card'),
                             'card_task_closed': self.msg('trace.reply_target.card_task_closed'), 'stuck_claim_card': self.msg('trace.reply_target.stuck_claim_card')}
        self.reply_source = {'push': self.msg('trace.reply_source.push'), 'sweep': self.msg('trace.reply_source.sweep'), 'code_card_poll': self.msg('trace.reply_source.code_card_poll')}
        self.watch_kind = {'peer': self.msg('trace.watch_kind.peer'), 'decision': self.msg('trace.watch_kind.decision'), 'approvals': self.msg('trace.watch_kind.approvals'), 'task_inbox': self.msg('trace.watch_kind.task_inbox')}
        self.local_kind = {'message': self.msg('trace.local_kind.message'), 'note': self.msg('trace.local_kind.note'), 'routine': self.msg('trace.local_kind.routine'), 'intake': self.msg('trace.local_kind.intake')}
        self.outcome_order = ('filtered', 'failed', f'needs_{self.key}', 'launched', 'carded', 'replied', 'handed_off', 'logged', 'delivered',
                              'pending')
        self.views = ('all', self.key, 'failed', 'logged', 'filtered', 'card_reply', 'agent')
        self.builders = {'burst': self._burst, 'seen': self._seen, 'card_reply': self._card_reply, 'local': self._local, 'event': self._event_item,
                         'card': self._card_item}
        self.icon = {self.done: '✓', self.active: '…', self.owner_wait: '!', self.failed: '✗', self.skip: '·'}

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def clock(self, ts):
        try:
            return dt.datetime.fromtimestamp(float(ts), self.tz).strftime('%m-%d %H:%M:%S')
        except (TypeError, ValueError, OverflowError, OSError):
            return '-'

    def dur(self, seconds):
        s = max(0, int(round(float(seconds or 0))))
        if s < 60:
            return self.msg('trace.dur.1', s=s)
        m, s = divmod(s, 60)
        if m < 60:
            return self.msg('trace.dur.2', m=m, s=s) if s else self.msg('trace.dur.3', m=m)
        h, m = divmod(m, 60)
        return self.msg('trace.dur.4', h=h, m=m) if h < 48 else self.msg('trace.dur.5', value=h // 24, value2=h % 24)

    def masked(self, text, limit=NOTE_LIMIT):
        """One masked line of agent-written text: run_log.short (secrets, Slack mentions) plus the trace CLI's long-token mask."""
        return self.run_log.short(SECRETISH.sub('<masked>', self.run_log.mask(str(text or ''))), limit) or None

    def route_reason_text(self, reason):
        reason = str(reason or '')
        if reason in self.route_reason:
            return self.route_reason[reason]
        for prefix, text in self.route_prefix:
            if reason.startswith(prefix):
                return text.format(reason[len(prefix):]) if '{}' in text else text
        return reason or None

    def headline(self, t):
        """One plain-Chinese sentence: where it went, what was decided, and what it is waiting on now."""
        stages = {x['key']: x for x in t['stages']}
        if 'filtered' in t['flags']:
            return self.msg('trace.headline.1') + stages['filtered']['summary']
        parts = []
        route = stages['route']
        if route['status'] != self.skip:
            parts.append(route['summary'])
        if t.get('intent') or t.get('disposition'):
            parts.append(self.msg('trace.headline.2') + '·'.join(x for x in (self.intent.get(t.get('intent'), t.get('intent')),
                                                      self.disposition.get(t.get('disposition'), t.get('disposition'))) if x))
        replies = [x for x in t['steps'] if x['stage'] == 'replies']
        cards = [x for x in t['steps'] if x['stage'] == 'cards' and x.get('card')]
        if 'logged' in t['flags'] and not replies and not cards:
            parts.append(self.msg('trace.headline.3'))
        else:
            if cards:
                parts.append(self.msg('trace.headline.4', len=len(cards)))
            if replies:
                parts.append(self.msg('trace.headline.5', len=len(replies)))
        if t['status'] in ('failed', self.key, 'waiting'):
            stage = stages[t['current']]
            parts.append((self.msg('trace.headline.6') if t.get('stuck') else '') + stage['label'] + self.msg('trace.headline.7') + stage['summary'])
        elif stages['resolved']['status'] == self.done and 'logged' not in t['flags']:
            summary = stages['resolved']['summary']
            task, sep, rest = summary.partition(self.msg('trace.headline.8'))
            parts.append(rest if sep and any(task in p for p in parts) else summary)  # the task is already named above
        return self.msg('trace.headline.9').join(dict.fromkeys(p for p in parts if p)) or stages[t['current']]['summary']

    def _session_facts(self, provider, session, task):
        short = sid(session)
        if not short:
            return []
        facts = [(self.msg('trace._session_facts.1'), f'{provider or "?"} {short}', 'mono'), (self.msg('trace._session_facts.2'), f'fsess show {short}', 'copy')]
        if task:
            facts.append((self.msg('trace._session_facts.3'), f'fsess take {task}', 'copy'))
        return facts

    def _run_step(self, t, run, task):
        s = t.s
        t.seen_runs.add(run['run_id'])
        events = _loads(run.get('event_ids'), [])
        state = run.get('state')
        if state == 'running':
            status, label = self.active, self.msg('trace._run_step.1')
        elif run.get('outcome') in ('failed', 'unresolved'):
            status, label = self.failed, self.outcome.get(run['outcome'])
        else:
            status, label = self.done, self.outcome.get(run.get('outcome'), run.get('outcome') or self.msg('trace._run_step.2'))
        took = (run.get('ended_at') or s.now) - (run.get('started_at') or s.now)
        facts = [(self.msg('trace._run_step.3'), ' · '.join(x for x in (run.get('provider'), run.get('model'), run.get('effort')) if x)),
                 (self.msg('trace._run_step.4'), {'fresh': self.msg('trace._run_step.fresh'), 'resume': self.msg('trace._run_step.resume')}.get(run.get('mode'), run.get('mode'))),
                 (self.msg('trace._run_step.5'), self.switch_reason.get(run.get('route_reason'), run.get('route_reason'))),
                 (self.msg('trace._run_step.6'), self.dur(took) + (self.msg('trace._run_step.7') if state == 'running' else '')),
                 (self.msg('trace._run_step.8'), self.error.get(run.get('error_class'), run.get('error_class'))),
                 (self.msg('trace._run_step.9'), self.msg('trace._run_step.10', len=len(events)) if len(events) > 1 else None),
                 *self._session_facts(run.get('provider'), run.get('session'), task)]
        # the dispatch's state is the item's state: a failed run of a dispatch that went on (retried, waiting, paused) is history
        d = s.dispatch(run.get('dispatch_id')) if run.get('dispatch_id') else None
        if status == self.failed and d and d.get('state') not in ('failed', f'needs_{self.key}'):
            status, label = self.skip, self.msg('trace._run_step.11', label=label, get=self.dispatch_state.get(d.get('state'), d.get('state')))
        t.add('dispatch', status, run.get('started_at'), self.msg('trace._run_step.12', value=run['run_id'][-6:], label=label), facts,
              [link(self.msg('trace._run_step.13'), f'/runs/{run["run_id"]}', 'run')], s.run_lines(run), run={
                  'run_id': run['run_id'], 'provider': run.get('provider'), 'session': sid(run.get('session')),
                  'state': state, 'outcome': run.get('outcome'), 'started_at': run.get('started_at'),
                  'ended_at': run.get('ended_at'), 'href': f'/runs/{run["run_id"]}'})

    def _dispatch_step(self, t, d, routes, task, joined=None):
        s = t.s
        state = d.get('state')
        key = d.get('task_key') or task
        held = [r for r in routes if r.get('dispatch_id') == d['dispatch_id'] and r.get('decision') == 'held']
        switched = [r for r in routes if r.get('decision') in ('switched', 'rerouted')]
        reason = held[-1]['reason'] if held else d.get('reason')
        facts = [(self.msg('trace._dispatch_step.1'), d.get('task_id') or key, 'mono'), (self.msg('trace._dispatch_step.2'), self.hold_reason.get(reason, reason)),
                 (self.msg('trace._dispatch_step.3'), self.trigger.get(d.get('trigger'), d.get('trigger'))),
                 (self.msg('trace._dispatch_step.4'), self.msg('trace._dispatch_step.5').join(dict.fromkeys(self.switch_reason.get(r['reason'], r['reason']) for r in switched)) or None),
                 (self.msg('trace._dispatch_step.6'), self.msg('trace._dispatch_step.7', retries=d['retries']) if d.get('retries') else None),
                 (self.msg('trace._dispatch_step.8'), self.error.get(d.get('error_class'), d.get('error_class')))]
        links = [link(self.msg('trace._dispatch_step.9'), slack_url(d.get('card_channel'), d.get('card_ts')), 'slack'),
                 link(self.msg('trace._dispatch_step.10'), slack_url(d.get('card_channel'), d.get('fail_card_ts')), 'slack'),
                 link(self.msg('trace._dispatch_step.11'), f'/runs?task={d.get("task_id") or key}', 'run')]
        pause = s.pause(key) if state == 'paused' else None
        if state == 'done':
            status, title = self.done, self.msg('trace._dispatch_step.12', value=d.get('task_id') or key)
        elif state == 'running':
            status, title = self.active, self.msg('trace._dispatch_step.13', value=d.get('task_id') or key)
        elif state == 'waiting' and d.get('wait_reason') == 'provider_wait':
            status = self.active
            title = self.msg('trace._dispatch_step.14', dur=self.dur(s.now - (d.get('wait_since') or d.get('updated_at') or s.now)))
            facts.append((self.msg('trace._dispatch_step.15'), self.msg('trace._dispatch_step.16')))
            links += [link(self.msg('trace._dispatch_step.17'), '/health', 'page'), link(self.msg('trace._dispatch_step.18'), '/settings', 'settings')]
        elif state == 'waiting':
            status, title = self.active, self.msg('trace._dispatch_step.19', value=d.get('task_id') or key)
            t.stuck(d.get('created_at'))
        elif state == 'paused' and (pause or {}).get('reason') == f'{self.key}_takeover':
            status, title = self.owner_wait, self.msg('trace._dispatch_step.20')
            facts.append((self.msg('trace._dispatch_step.21'), self.clock(pause.get('paused_at'))))
            facts.append((self.msg('trace._dispatch_step.22'), f'fsess back {d.get("task_id") or key}', 'copy'))
        elif state == 'paused':
            status, title = self.owner_wait, self.msg('trace._dispatch_step.23')
        elif state == 'failed':
            status = self.failed
            title = (self.msg('trace._dispatch_step.24')
                     if reason == f'task_failed_waiting_for_{self.key}' else self.msg('trace._dispatch_step.25'))
        elif state == f'needs_{self.key}':
            status, title = self.owner_wait, self.msg('trace._dispatch_step.26')
        elif state == 'superseded':
            status, title = self.done, self.msg('trace._dispatch_step.27')
        elif state == 'stopped':
            status, title = self.done, self.msg('trace._dispatch_step.28')
        else:
            status, title = self.active, self.dispatch_state.get(state, state or '?')
        t.add('dispatch', status, max(x for x in (d.get('created_at'), joined) if x is not None) if (d.get('created_at') or joined)
              else None, title, facts, links, dispatch={
            'dispatch_id': d['dispatch_id'], 'state': state, 'wait_reason': d.get('wait_reason'), 'reason': reason})
        return status

    def _event_chain(self, t, e, task, depth=0):
        """Dispatch / runs / artifacts / resolution of one delivered event. Returns its runs."""
        s = t.s
        t.task(task)
        routes = s.all('SELECT * FROM dispatch_routes WHERE event_id=? ORDER BY at, id', (e['id'],), 'dispatch_routes')
        receipt = str(e.get('receipt') or '')
        dids = list(dict.fromkeys([r['dispatch_id'] for r in routes if r.get('dispatch_id')] +
                                  ([receipt[9:]] if receipt.startswith('dispatch:') else [])))
        runs, statuses = [], []
        received = tsf(e.get('received')) or 0
        for did in dids:
            d = s.dispatch(did)
            if d is None:
                continue
            joined = min((r['at'] for r in routes if r.get('dispatch_id') == did and r.get('at')), default=received)
            statuses.append(self._dispatch_step(t, d, routes, task, joined))
            for run in s.runs(did):
                mine = e['id'] in _loads(run.get('event_ids'), []) or (tsf(run.get('started_at')) or 0) >= received - 1
                if run['run_id'] in t.seen_runs or not mine:
                    continue
                self._run_step(t, run, d.get('task_id') or task)
                runs.append(run)
        if not dids:
            w = s.watch(e.get('watch_id'))
            provider = w.get('provider') or ('claude' if receipt.startswith('claude-outbox') else 'codex' if receipt else None)
            direct = [r for r in routes if r.get('decision') == 'as_today']
            why = self.direct_reason.get(direct[-1]['reason'], direct[-1]['reason']) if direct else None
            facts = [(self.msg('trace._event_chain.1'), task, 'mono'), (self.msg('trace._event_chain.2'), why), *self._session_facts(provider, w.get('session'), task)]
            st = e.get('status')
            if st == 'pending':
                t.add('dispatch', self.active, e.get('received'), self.event_status['pending'], facts)
                t.stuck(e.get('received'))
            elif st in ('queued', 'claimed', 'sending'):
                t.add('dispatch', self.active, e.get('received'), self.msg('trace._event_chain.3') + self.event_status[st], facts)
                if st == 'queued':
                    t.stuck(e.get('received'))
            elif st == 'delivery_unknown':
                t.add('dispatch', self.failed, e.get('received'), self.msg('trace._event_chain.4'), facts)
            elif receipt:
                t.add('dispatch', self.done, e.get('received'), self.msg('trace._event_chain.6', value=task or self.msg('trace._event_chain.5')), facts)
            else:
                t.note('dispatch', self.msg('trace._event_chain.7'))
        if depth == 0:
            self._artifacts(t, runs, task)
        if e.get('status') == 'handled':
            ended = [r.get('ended_at') for r in runs if r.get('ended_at')]
            disp = e.get('disposition')
            if disp in ('irrelevant',):
                t.flags.add('resolved_irrelevant')
            if disp == 'handed_off':
                t.flags.add('handed_off')
            t.add('resolved', self.done, max(ended) if ended else None,
                  f"{(task + self.msg('trace._event_chain.8') if task else '')}{self.resolved.get(disp, disp or self.msg('trace._event_chain.9'))}", [(self.msg('trace._event_chain.10'), disp, 'mono')])
        elif e.get('status') == 'dispatched' and any(x == self.failed for x in statuses):
            t.note('resolved', self.msg('trace._event_chain.11'))
        else:
            t.note('resolved', self.msg('trace._event_chain.12') + self.event_status.get(e.get('status'), e.get('status') or '?'))
        return runs

    def _card_step(self, t, c, kind=None):
        s = t.s
        if c['event_id'] in t.seen_cards:
            return
        t.seen_cards.add(c['event_id'])
        t.flags.add('carded')
        d = s.one('SELECT * FROM decisions WHERE event_id=?', (c['event_id'],), 'decisions')
        x = s.one('SELECT * FROM escalations WHERE event_id=?', (c['event_id'],), 'escalations') or {}
        w = s.watch(x.get('watch_id')) if x.get('watch_id') else {}
        kind = kind or (w.get('origin') if w.get('origin') == f'ask_{self.key}' else 'blocker' if w.get('blocker_id') else 'card')
        label = self.card_kind.get(kind, self.msg('trace._card_step.1'))
        created = tsf(c.get('created'))
        age = s.now - (created or s.now)
        if d:
            status, title = self.done, self.msg('trace._card_step.2', label=label, owner=self.name, choice=d['choice'])
        elif w.get('state') in ('stopped', 'expired') or w.get('retire_reason'):
            status, title = self.done, self.msg('trace._card_step.3', label=label, value=w.get('retire_reason') or w.get('stop_reason') or w.get('state'))
        elif age > OPEN_CARD_DAYS * 86400:
            status, title = self.done, self.msg('trace._card_step.4', label=label, open_card_days=OPEN_CARD_DAYS)
        else:
            status, title = self.owner_wait, self.msg('trace._card_step.5', label=label)
        facts = [(self.msg('trace._card_step.6'), c.get('task_id'), 'mono'), (self.msg('trace._card_step.7'), c['event_id'], 'mono'),
                 (self.msg('trace._card_step.8'), self.msg('trace._card_step.9') if c.get('never_auto') else None),
                 (self.msg('trace._card_step.10'), self.msg('trace._card_step.11') if c.get('smoke') or c.get('dry') else None)]
        links = [link(self.msg('trace._card_step.12'), slack_url(c.get('channel'), x.get('thread_ts'), c.get('thread_root')), 'slack')]
        for rc in s.all('SELECT id, outcome, result FROM rule_checks WHERE card_event=?', (c['event_id'],), 'rule_checks'):
            facts.append((self.msg('trace._card_step.13'), f"{self.rule_outcome.get(rc['outcome'], rc['outcome'])}" + (f' · {rc["result"]}' if rc.get('result') else '')))
            links.append(link(self.msg('trace._card_step.14') + rc['id'][:8], f'/rules?check={rc["id"]}', 'page'))
        for g in s.all('SELECT id, system, type, status, resolution FROM automation_gaps WHERE card_event=? OR stage2_event=?',
                       (c['event_id'], c['event_id']), 'automation_gaps'):
            facts.append((self.msg('trace._card_step.15'), ' · '.join(x for x in (g['system'], g['type'], g['status'], g.get('resolution')) if x)))
            links.append(link(self.msg('trace._card_step.16') + g['id'][:8], f'/gaps/{g["id"]}', 'page'))
        t.add('cards', status, created, title, facts, links, card={'event_id': c['event_id'], 'kind': kind,
                                                                   'decided': bool(d)})
        if d:
            who = self.name if d.get('actor') == self.owner_slack_user else d.get('actor')
            t.add('cards', self.done, d.get('decided_at'), self.msg('trace._card_step.17', choice=d['choice'], who=who, get=self.provenance.get(d.get('provenance'), d.get('provenance'))),
                  [(self.msg('trace._card_step.18'), who, 'mono'), (self.msg('trace._card_step.19'), self.provenance.get(d.get('provenance'), d.get('provenance'))),
                   (self.msg('trace._card_step.20'), d.get('delivered_event'), 'mono')],
                  [link(self.msg('trace._card_step.21'), slack_url(d.get('card_channel'), d.get('receipt_ts'), d.get('card_ts')), 'slack')])
            delivered = s.event(d['delivered_event']) if d.get('delivered_event') else None
            if delivered is not None:
                self._event_chain(t, delivered, s.task_of_event(delivered) or c.get('task_id'), depth=1)

    def _artifacts(self, t, runs, task):
        """What the runs that handled this item produced: cards (by the session that opened them), blockers, rule checks,
        ask cards and posts as the owner. A run that handled several events shares them (the step says so)."""
        s = t.s
        for run in runs:
            session, start = run.get('session'), tsf(run.get('started_at'))
            if not session or start is None:
                continue
            lo, hi = start - 2, (tsf(run.get('ended_at')) or s.now) + 5
            if s.has('escalation_cards'):
                for c in s.all('SELECT c.*, w.data AS wdata FROM escalation_cards c JOIN escalations x ON x.event_id=c.event_id '
                               'LEFT JOIN watches w ON w.id=x.watch_id WHERE c.created BETWEEN ? AND ? ORDER BY c.created',
                               (lo, hi)):
                    wd = _loads(c.pop('wdata', None), {})
                    if session in (wd.get('session'), (wd.get('posted_by') or {}).get('session')):
                        self._card_step(t, c)
            for g in s.all('SELECT * FROM automation_gaps WHERE run_id=? AND card_event IS NULL', (run['run_id'],),
                           'automation_gaps'):
                t.add('cards', self.done if g['status'] == 'resolved' else self.owner_wait, g['created_at'],
                      self.msg('trace._artifacts.1', system=g['system'], type=g['type']), [(self.msg('trace._artifacts.2'), g['status'])],
                      [link(self.msg('trace._artifacts.3') + g['id'][:8], f'/gaps/{g["id"]}', 'page')])
            for rc in s.all('SELECT id, outcome, created_at, card_event, result FROM rule_checks WHERE task_id=? AND created_at '
                            'BETWEEN ? AND ? AND card_event IS NULL', (run.get('task'), lo, hi), 'rule_checks'):
                t.add('cards', self.done, rc['created_at'], self.msg('trace._artifacts.4', get=self.rule_outcome.get(rc['outcome'], rc['outcome'])),
                      [(self.msg('trace._artifacts.5'), rc.get('result'))], [link(self.msg('trace._artifacts.6') + rc['id'][:8], f'/rules?check={rc["id"]}', 'page')])
            for a in s.all('SELECT id, channel, ts, thread, state, responders, created_at FROM ask_cards WHERE owner_session=? '
                           'AND created_at BETWEEN ? AND ?', (session, lo, hi), 'ask_cards'):
                if (a['channel'], a['ts']) in t.seen_sends:
                    continue
                t.seen_sends.add((a['channel'], a['ts']))
                t.flags.add('replied')
                t.add('replies', self.done if a['state'] not in ('sending',) else self.active, a['created_at'],
                      self.msg('trace._artifacts.8', owner=self.name, join=self.msg('trace._artifacts.7').join(_loads(a['responders'], []))),
                      [(self.msg('trace._artifacts.9'), a['state']), (self.msg('trace._artifacts.10'), f'{run.get("task")} · {run.get("provider")} {sid(session)}', 'mono')],
                      [link(self.msg('trace._artifacts.11'), slack_url(a['channel'], a['ts'], a.get('thread')), 'slack')])
            kinds = tuple(self.reply_kind)
            for r in s.all(f'SELECT * FROM agent_sends WHERE session=? AND created BETWEEN ? AND ? AND kind IN '
                           f'({",".join("?" * len(kinds))})', (session, lo, hi, *kinds), 'agent_sends'):
                self._send_step(t, r['channel'], r['ts'], r['kind'], 'sent', r['created'], run.get('task'),
                           f'{run.get("provider")} {sid(session)}', None)

    def _send_step(self, t, channel, ts, kind, state, at, task, actor, thread):
        if (channel, ts) in t.seen_sends:
            return
        t.seen_sends.add((channel, ts))
        t.flags.add('replied')
        status = self.send_state.get(state, self.done)
        title = self.reply_kind.get(kind, kind) + ('' if status == self.done else self.msg('trace._send_step.1') + {'sending': self.msg('trace._send_step.sending'), 'delivery_unknown': self.msg('trace._send_step.delivery_unknown')}.get(state, state))
        t.add('replies', status, at, title, [(self.msg('trace._send_step.2'), ' · '.join(x for x in (task, actor) if x), 'mono')],
              [link('Slack', slack_url(channel, ts, thread), 'slack')])

    def _who(self, s, uid, kind=None):
        p = s.profile(uid) if uid else None
        return {'id': uid, 'name': (p or {}).get('name') or (self.name if uid == self.owner_slack_user else None),
                'avatar': bool((p or {}).get('has_avatar')), 'kind': kind}

    def _conv_label(self, s, conv_type, channel):
        name = s.channel_name(channel)
        return f"{self.conv.get(conv_type, conv_type or self.msg('trace._conv_label.1'))} {('#' + name if name and conv_type == 'channel' else channel or '')}".strip()

    def _burst(self, t):
        s = t.s
        b = s.one('SELECT * FROM intake_bursts WHERE burst_id=?', (t.subject['id'],), 'intake_bursts')
        if b is None:
            raise KeyError('not_found')
        bid, first, root = b['burst_id'], tsf(b['first_ts']), b.get('thread_root')
        seen = s.all('SELECT * FROM intake_seen WHERE burst_id=? ORDER BY ts', (bid,), 'intake_seen')
        smoke = bool(b.get('smoke'))
        who = self._who(s, b.get('sender'), b.get('sender_kind'))
        if not who['name'] and b.get('alert_service'):
            who['name'] = self.msg('trace._burst.1', alert_service=b['alert_service'])
        t.meta.update(ref=bid, at=first, channel=b['channel'], conv_type=b.get('conv_type'), thread=root,
                      where=self._conv_label(s, b.get('conv_type'), b['channel']), who=who,
                      msg_count=b.get('msg_count'), smoke=smoke,
                      slack_url=None if smoke else slack_url(b['channel'], b['first_ts'], root),
                      title=self.msg('trace._burst.3', get=self.conv.get(b.get('conv_type'), self.msg('trace._burst.2')), value=b.get('msg_count') or 1))
        # 收到
        handed = str(b.get('route_reason') or '').startswith('handoff_from:') and not seen
        seen_at = min((x['seen_at'] for x in seen if x.get('seen_at')), default=None)
        sources = self.msg('trace._burst.4').join(dict.fromkeys(self.source.get(x['source'], x['source']) for x in seen))
        style = b.get('person_style')
        facts = [(self.msg('trace._burst.5'), t.meta['where'] + (self.msg('trace._burst.6') if root else self.msg('trace._burst.7')), 'mono'),
                 (self.msg('trace._burst.8'), ' · '.join(x for x in (t.meta['who']['name'], b.get('sender'),
                                                 self.sender_kind.get(b.get('sender_kind'), b.get('sender_kind'))) if x)),
                 (self.msg('trace._burst.9'), sources or None),
                 (self.msg('trace._burst.10'), self.msg('trace._burst.11', dur=self.dur(seen_at - first)) if seen_at and first else None),
                 (self.msg('trace._burst.12'), self.msg('trace._burst.13', value=b.get('msg_count') or 1)),
                 (self.msg('trace._burst.14'), self.msg('trace._burst.15', owner=self.name) if style == 'strategic_directive' else
                  (self.msg('trace._burst.16') if b.get('auto_execute') == 0 else None)),
                 (self.msg('trace._burst.17'), ' · '.join(x for x in (b.get('alert_service'), b.get('alert_state'),
                                               (self.msg('trace._burst.18') + b['alert_fp'][:8]) if b.get('alert_fp') else None) if x) or None),
                 (self.msg('trace._burst.19'), '🧪 TEST smoke-intake' if smoke else None)]
        title = (self.msg('trace._burst.20', value=b['route_reason'][len('handoff_from:'):]) if handed else
                 self.msg('trace._burst.22', value=t.meta['who']['name'] or b.get('sender') or '?', get=self.conv.get(b.get('conv_type'), self.msg('trace._burst.21')), value2=b.get('msg_count') or 1))
        t.add('received', self.done, first, title, facts, [link(self.msg('trace._burst.23'), t.meta['slack_url'], 'slack')])
        # 过滤 / 合并
        state = b.get('state')
        sealed = tsf(b.get('sealed_at')) or tsf(b.get('created_at'))
        skipped = {}
        if s.has('intake_seen'):
            hi = max(tsf(b.get('last_ts')) or 0, sealed or 0)
            for r in s.all('SELECT reason, count(*) AS n FROM intake_seen WHERE channel=? AND burst_id IS NULL AND '
                           'CAST(ts AS REAL) BETWEEN ? AND ? GROUP BY reason', (b['channel'], (first or 0) - 1, hi)):
                skipped[r['reason']] = r['n']
        names = {'our_agent': self.msg('trace._burst.our_agent'), f'{self.key}_authored': self.msg('trace._burst.owner_authored', owner=self.name), 'system_noise': self.msg('trace._burst.system_noise'), 'bot_outside_oncall': 'bot'}
        facts = [(self.msg('trace._burst.24'), self.msg('trace._burst.25', value=b.get('msg_count') or 1, clock=self.clock(b['first_ts']), clock2=self.clock(b['last_ts']))
                  if (b.get('msg_count') or 1) > 1 else self.msg('trace._burst.26')),
                 (self.msg('trace._burst.27'), self.msg('trace._burst.28', dup_count=b['dup_count']) if b.get('dup_count') else None),
                 (self.msg('trace._burst.29'), self.msg('trace._burst.30').join(self.msg('trace._burst.31', get=names.get(k, k), n=n) for k, n in skipped.items() if k in names) or None)]
        if state == 'open':
            t.add('filtered', self.active, b.get('created_at'), self.msg('trace._burst.32'), facts)
            t.stuck(b.get('created_at'))
        elif not handed:
            t.add('filtered', self.done, sealed, self.msg('trace._burst.33') + (self.msg('trace._burst.34', msg_count=b['msg_count']) if (b.get('msg_count') or 1) > 1 else ''),
                  facts)
        else:
            t.note('filtered', self.msg('trace._burst.35'))
        # 分诊 (records first: the route of a launch / forward comes from them)
        records = s.all('SELECT * FROM triage_records WHERE burst_id=? ORDER BY created_at', (bid,), 'triage_records')
        for r in records:
            self._triage_step(t, r)
        # 卡片 the inbox opened for this burst on purpose (brief / reply draft)
        for br in s.all('SELECT * FROM intake_briefs WHERE burst_id=? ORDER BY created_at', (bid,), 'intake_briefs'):
            c = s.one('SELECT * FROM escalation_cards WHERE event_id=?', (br['card_event'],), 'escalation_cards')
            if c is not None:
                self._card_step(t, c, br.get('kind'))
        # 回复 as the owner to this burst (the reservation ledger, then older agent_sends rows); first, so
        # their state wins over the same post found again by session in the run artifacts
        for r in s.all('SELECT * FROM intake_sends WHERE burst_id=? ORDER BY created_at', (bid,), 'intake_sends'):
            self._send_step(t, r.get('channel'), r.get('ts'), r['kind'], r['state'], r['created_at'], r.get('task_id'),
                       _recorder(s, r.get('actor'), task=False), root)
        for r in s.all("SELECT * FROM agent_sends WHERE ref=? AND kind IN ('intake_reply','intake_ack') ORDER BY created",
                       (bid,), 'agent_sends'):
            self._send_step(t, r['channel'], r['ts'], r['kind'], 'sent', r['created'], s.task_of_session(r.get('session')),
                       sid(r.get('session')), root)
        # 路由 → 派发 → … per delivery (a handoff re-delivers the same burst)
        events = [x for x in s.burst_events(bid)]
        if b.get('event_id') and b['event_id'] not in events:
            events.append(b['event_id'])
        events = [e for e in (s.event(x) for x in events) if e is not None]
        if state in ('open', 'sealed') and not events:
            t.note('route', self.msg('trace._burst.36'), self.active)
            if state == 'sealed':
                t.stuck(b.get('sealed_at'))
        elif state == 'waiting':
            why = {'no_current_session': self.msg('trace._burst.no_current_session'), 'task_missing': self.msg('trace._burst.task_missing')}.get(b.get('error'), b.get('error'))
            t.add('route', self.active, b.get('updated_at'), self.msg('trace._burst.37', get=b.get('target_task'), why=why),
                  [(self.msg('trace._burst.38'), self.route_reason_text(b.get('route_reason')))])
            t.stuck(b.get('updated_at'))
            t.task(b.get('target_task'))
        elif state == 'expired':
            t.add('route', self.failed, b.get('updated_at'), self.msg('trace._burst.39', value=b.get('error') or 'too_old'))
        elif state == 'covered':
            t.add('route', self.done, b.get('updated_at'), self.msg('trace._burst.40') + str(b.get('error') or '').replace('bound_to_', ''))
        for i, e in enumerate(events):
            task = s.task_of_event(e)
            last = i == len(events) - 1
            if last:
                reason = self.route_reason_text(b.get('route_reason'))
                label = self.route.get(b.get('route'), b.get('route'))
                title = (self.msg('trace._burst.41', task=task) if b.get('route') == 'handoff' else self.msg('trace._burst.42', task=task, label=label))
            else:
                reason, title = self.msg('trace._burst.43'), self.msg('trace._burst.44', task=task)
            if i > 0 and b.get('route') == 'handoff' and last:
                prev = s.task_of_event(events[i - 1])
                title = self.msg('trace._burst.46', value=prev or self.msg('trace._burst.45'), task=task)
            t.add('route', self.done, e.get('received'), title,
                  [(self.msg('trace._burst.47'), reason), (self.msg('trace._burst.48'), b.get('route_reason') if last else None, 'mono')],
                  [link(self.msg('trace._burst.49', task=task), f'/runs?task={task}', 'run')] if task else ())
            if task and task not in s.inbox_tasks and b.get('route') in ('bound', 'ref', 'handoff') and last:
                t.flags.add('handed_off' if b.get('route') == 'handoff' else 'delivered')
            self._event_chain(t, e, task)
        # launch / forward the inbox decided (new task, or a send-to-task) — their own dispatch chain
        for r in records:
            target = r.get('target_task')
            if r['disposition'] == 'launch' and target:
                t.flags.add('launched')
                d = s.one("SELECT * FROM dispatches WHERE task_id=? AND trigger='launch' AND created_at>=? ORDER BY created_at "
                          'LIMIT 1', (target, (tsf(b.get('created_at')) or 0) - 60), 'dispatches')
                t.add('route', self.done, r['created_at'], self.msg('trace._burst.50', target=target),
                      [(self.msg('trace._burst.51'), _recorder(s, r.get('recorded_by')), 'mono'),
                       (self.msg('trace._burst.52'), self.msg('trace._burst.53', owner=self.name) if str(r.get('basis') or '').startswith(f'{self.key}_decision:') else None)],
                      [link(self.msg('trace._burst.54', target=target), f'/runs?task={target}', 'run')])
                t.task(target)
                if d is not None:
                    self._dispatch_step(t, d, [], target)
                    runs = []
                    for run in s.runs(d['dispatch_id']):
                        if run['run_id'] not in t.seen_runs:
                            self._run_step(t, run, target)
                            runs.append(run)
                    self._artifacts(t, runs, target)
            elif r['disposition'] == 'forward' and target and r.get('basis') != 'handoff':
                t.flags.add('handed_off')
                m = s.one('SELECT * FROM agent_messages WHERE to_key=? AND event_id IS NOT NULL AND created_at BETWEEN ? AND ? '
                          'ORDER BY abs(created_at-?) LIMIT 1',
                          (target, r['created_at'] - 30, r['created_at'] + 5, r['created_at']), 'agent_messages')
                t.add('route', self.done, r['created_at'], self.msg('trace._burst.55', target=target),
                      [(self.msg('trace._burst.56'), _recorder(s, r.get('recorded_by')), 'mono')], [link(self.msg('trace._burst.57', target=target), f'/runs?task={target}', 'run')])
                e = s.event(m['event_id']) if m else None
                if e is not None:
                    self._event_chain(t, e, target)
                else:
                    t.task(target)
        self._close_notes(t, records, b)

    def _triage_step(self, t, r):
        s = t.s
        t.intent, t.disposition = r.get('intent'), r.get('disposition')
        if r.get('disposition') in ('log', 'ignore'):
            t.flags.add('logged')
        corr = _loads(r.get('correction'), {})
        title = f"{self.intent.get(r['intent'], r['intent'])} · {self.disposition.get(r['disposition'], r['disposition'])}"
        if r.get('basis') == 'handoff':
            title = self.msg('trace._triage_step.1', get=r.get('target_task'))
        facts = [(self.msg('trace._triage_step.2'), self.msg('trace._triage_step.3', get=self.intent.get(r['intent'], r['intent']), intent=r['intent'])),
                 (self.msg('trace._triage_step.4'), self.msg('trace._triage_step.5', get=self.disposition.get(r['disposition'], r['disposition']), disposition=r['disposition'])),
                 (self.msg('trace._triage_step.6'), f'{float(r["confidence"]):.2f}' if r.get('confidence') is not None else None),
                 (self.msg('trace._triage_step.7'), self.clarity.get(r.get('clarity'), r.get('clarity'))),
                 (self.msg('trace._triage_step.8'), r.get('target_task'), 'mono'),
                 (self.msg('trace._triage_step.9'), self.masked(r.get('basis'))), (self.msg('trace._triage_step.10'), self.masked(r.get('note'))),
                 (self.msg('trace._triage_step.11', owner=self.name), self.msg('trace._triage_step.12') if r.get(f'needs_{self.key}') else None),
                 (self.msg('trace._triage_step.13'), _recorder(s, r.get('recorded_by')), 'mono'),
                 (self.msg('trace._triage_step.14', owner=self.name), (f"{self.disposition.get(corr.get('disposition'), corr.get('disposition') or '')} {self.masked(corr.get('note')) or ''}").strip() if corr else None)]
        t.add('triage', self.done, r.get('created_at'), title, facts,
              [link(self.msg('trace._triage_step.15'), '/intake#intake-triage', 'page')], triage={'id': r['id'], 'smoke': bool(r.get('smoke'))})

    def _close_notes(self, t, records, b=None):
        """Plain-language reasons for the stages nothing reached."""
        last = records[-1] if records else None
        disp = (last or {}).get('disposition')
        route = (b or {}).get('route')
        if any(x['stage'] == 'dispatch' and x['status'] == self.failed for x in t.steps):
            for stage in ('cards', 'replies', 'resolved'):
                t.note(stage, self.msg('trace._close_notes.1'))
        if not records:
            if b is not None and route in ('bound', 'ref') and (b.get('target_task') not in t.s.inbox_tasks):
                t.note('triage', self.msg('trace._close_notes.2'))
            elif any(x['stage'] == 'dispatch' and x['status'] in (self.active, self.owner_wait, self.failed) for x in t.steps) or \
                    t.stage_note.get('route', (None,))[0] == self.active or any(
                        x['stage'] == 'route' and x['status'] == self.active for x in t.steps):
                t.note('triage', self.msg('trace._close_notes.3'))
            else:
                t.note('triage', self.msg('trace._close_notes.4'))
        if disp in ('log', 'ignore'):
            why = self.msg('trace._close_notes.5', value=self.disposition[disp])
            t.note('cards', why + self.msg('trace._close_notes.6'))
            t.note('replies', why + self.msg('trace._close_notes.7'))
        elif 'resolved_irrelevant' in t.flags and not any(x['stage'] in ('cards', 'replies') for x in t.steps):
            t.flags.add('logged')
            t.note('cards', self.msg('trace._close_notes.8'))
            t.note('replies', self.msg('trace._close_notes.9'))
        t.note('cards', self.msg('trace._close_notes.10'))
        t.note('replies', self.msg('trace._close_notes.11', owner=self.name))
        t.note('dispatch', self.msg('trace._close_notes.12'))
        t.note('resolved', self.msg('trace._close_notes.13'))

    def _seen(self, t):
        s = t.s
        channel, ts = t.subject['channel'], t.subject['ts']
        r = s.one('SELECT * FROM intake_seen WHERE channel=? AND ts=?', (channel, ts), 'intake_seen')
        if r is None:
            raise KeyError('not_found')
        t.meta.update(ref=f'{channel}/{ts}', at=tsf(ts), channel=channel, conv_type=r.get('conv_type'), thread=r.get('thread_root'),
                      where=self._conv_label(s, r.get('conv_type'), channel), who=self._who(s, r.get('user'), r.get('sender_kind')),
                      msg_count=1, slack_url=slack_url(channel, ts, r.get('thread_root')),
                      title=self.msg('trace._seen.2', get=self.conv.get(r.get('conv_type'), self.msg('trace._seen.1'))))
        reason = r.get('reason')
        by = self.msg('trace._seen.3', owner=self.name) if reason == 'our_agent' else (t.meta['who']['name'] or r.get('user') or '?')
        t.add('received', self.done, ts, self.msg('trace._seen.5', by=by, get=self.conv.get(r.get('conv_type'), self.msg('trace._seen.4'))),
              [(self.msg('trace._seen.6'), t.meta['where'] + (self.msg('trace._seen.7') if r.get('thread_root') else self.msg('trace._seen.8')), 'mono'),
               (self.msg('trace._seen.9'), ' · '.join(x for x in (t.meta['who']['name'], r.get('user'),
                                               self.sender_kind.get(r.get('sender_kind'), r.get('sender_kind'))) if x)),
               (self.msg('trace._seen.10'), self.source.get(r.get('source'), r.get('source'))),
               (self.msg('trace._seen.11'), self.msg('trace._seen.12', dur=self.dur(r['seen_at'] - float(ts))) if r.get('seen_at') else None)],
              [link(self.msg('trace._seen.13'), t.meta['slack_url'], 'slack')])
        facts = [(self.msg('trace._seen.14'), reason, 'mono')]
        links = []
        sent = s.one('SELECT * FROM agent_sends WHERE channel=? AND ts=?', (channel, ts), 'agent_sends')
        if sent:
            facts += [(self.msg('trace._seen.15'), f'{sent["kind"]}' + (f' · ref {sent["ref"]}' if sent.get('ref') else ''), 'mono'),
                      (self.msg('trace._seen.16'), ' · '.join(x for x in (s.task_of_session(sent.get('session')), sid(sent.get('session'))) if x) or None, 'mono')]
            if sent.get('ref') and (BURST_RE.fullmatch(sent['ref']) or EVENT_RE.fullmatch(sent['ref'])):
                links.append(link(self.msg('trace._seen.17'), f'/intake?trace={sent["ref"]}', 'trace'))
        status = self.active if reason == 'pending' else self.done
        t.add('filtered', status, r.get('seen_at'), self.seen_reason.get(reason, reason), facts, links)
        events = [e for e in s.all('SELECT * FROM events WHERE channel=? AND ts=? ORDER BY received', (channel, ts), 'events')
                  if e.get('disposition') != f'ask_{self.key}'] if reason in ('covered_by_watch', f'{self.key}_authored', 'pending') else []
        if reason == f'{self.key}_authored':
            t.flags.add('own')
        if reason == 'covered_by_watch' or events:
            for e in events:
                task = s.task_of_event(e)
                w = s.watch(e['watch_id'])
                t.add('route', self.done, e.get('received'), self.msg('trace._seen.19', value=task or self.msg('trace._seen.18')),
                      [(self.msg('trace._seen.20'), self.watch_kind.get(w.get('kind'), w.get('kind'))), (self.msg('trace._seen.21'), e['watch_id'], 'mono')],
                      [link(self.msg('trace._seen.22', task=task), f'/runs?task={task}', 'run')] if task else ())
                t.flags.add('delivered')
                self._event_chain(t, e, task)
            if not events:
                fresh = s.now - (r.get('seen_at') or 0) < 3600
                t.note('route', self.msg('trace._seen.23') if fresh else self.msg('trace._seen.24'),
                       self.active if fresh else self.skip)
            records = s.all('SELECT * FROM triage_records WHERE event_id IN (%s)' % ','.join('?' * len(events)),
                            [e['id'] for e in events], 'triage_records') if events else []
            for rec in records:
                self._triage_step(t, rec)
            if not records:
                t.note('triage', self.msg('trace._seen.25'))
            self._close_notes(t, records)
        else:
            if reason == 'our_agent':
                t.flags.add('own')
            if reason != 'pending':
                t.flags.add('filtered')
            why = self.msg('trace._seen.26') + self.seen_reason.get(reason, reason)
            for stage in ('triage', 'route', 'dispatch', 'cards', 'replies', 'resolved'):
                t.note(stage, why, self.active if reason == 'pending' else self.skip)

    def _card_reply(self, t):
        s = t.s
        channel, ts = t.subject['channel'], t.subject['ts']
        r = s.one('SELECT * FROM card_reply_routes WHERE channel=? AND reply_ts=?', (channel, ts), 'card_reply_routes')
        if r is None:
            raise KeyError('not_found')
        tg = s.one('SELECT * FROM card_reply_targets WHERE channel=? AND reply_ts=?', (channel, ts), 'card_reply_targets') or {}
        e = s.event(tg['event_id']) if tg.get('event_id') else None
        if e is None:
            e = s.one('SELECT * FROM events WHERE channel=? AND ts=? ORDER BY received DESC LIMIT 1', (channel, ts), 'events')
        author = tg.get('author') or (str((e or {}).get('author') or '') or None)
        t.meta.update(ref=f'reply:{channel}/{ts}', at=tsf(ts), channel=channel, conv_type='channel', thread=r.get('root'),
                      where=self._conv_label(s, 'channel', channel), who=self._who(s, author, 'human'), msg_count=1,
                      slack_url=slack_url(channel, ts, r.get('root')), title=self.msg('trace._card_reply.1'))
        t.add('received', self.done, ts, self.msg('trace._card_reply.3', value=self.name if author == self.owner_slack_user else t.meta['who']['name'] or author or self.msg('trace._card_reply.2')),
              [(self.msg('trace._card_reply.4'), r.get('root'), 'mono'), (self.msg('trace._card_reply.5'), self.reply_source.get(tg.get('source'), tg.get('source')))],
              [link(self.msg('trace._card_reply.6'), t.meta['slack_url'], 'slack')])
        t.note('filtered', self.msg('trace._card_reply.7'))
        t.add('triage', self.done, r.get('at'), self.msg('trace._card_reply.8', get=self.reply_class.get(r.get('reply_class'), r.get('reply_class') or '—')),
              [(self.msg('trace._card_reply.9'), self.reply_outcome.get(r.get('outcome'), r.get('outcome'))), (self.msg('trace._card_reply.10'), self.msg('trace._card_reply.11'))])
        task = tg.get('task_id') or s.task_of_event(e)
        if task:
            t.add('route', self.done, tg.get('at') or r.get('at'), self.msg('trace._card_reply.12', task=task),
                  [(self.msg('trace._card_reply.13'), self.reply_target.get(tg.get('reason'), tg.get('reason'))), (self.msg('trace._card_reply.14'), tg.get('card_task'), 'mono')],
                  [link(self.msg('trace._card_reply.15', task=task), f'/runs?task={task}', 'run')])
        else:
            t.note('route', self.msg('trace._card_reply.16'), self.active)
            t.stuck(r.get('at'))
        if r.get('card_event'):
            c = s.one('SELECT * FROM escalation_cards WHERE event_id=?', (r['card_event'],), 'escalation_cards')
            if c is not None:
                self._card_step(t, c)
        if e is not None:
            self._event_chain(t, e, task)
        self._close_notes(t, [], None)

    def _local(self, t):
        s = t.s
        eid = t.subject['id']
        m = s.one('SELECT * FROM local_messages WHERE event_id=?', (eid,), 'local_messages')
        e = s.event(eid)
        if m is None or e is None:
            raise KeyError('not_found')
        am = s.one('SELECT * FROM agent_messages WHERE event_id=?', (eid,), 'agent_messages') or {}
        kind = m.get('kind')
        sender = str(m.get('sender') or '')
        provider, _, session = sender.removeprefix('agent:').partition(':')
        src = am.get('from_key') or s.task_of_session(session)
        t.meta.update(ref=eid, at=tsf(m.get('created_at')), channel='LOCAL', conv_type='local', thread=None,
                      where=self.msg('trace._local.1'), who={'id': None, 'name': src or self.local_kind.get(kind, kind), 'avatar': False, 'kind': 'agent'},
                      msg_count=1, slack_url=None, title=self.local_kind.get(kind, kind))
        t.add('received', self.done, m.get('created_at'),
              self.msg('trace._local.2', get=self.local_kind.get(kind, kind), value=src or '?', get2=m.get('task_id')) if kind == 'message' else self.local_kind.get(kind, kind),
              [(self.msg('trace._local.3'), ' · '.join(x for x in (src, f'{provider} {sid(session)}' if session else sender) if x), 'mono'),
               (self.msg('trace._local.4'), am.get('hops') or None)])
        breaker = s.one('SELECT * FROM agent_breakers WHERE id=?', (am['breaker_id'],), 'agent_breakers') if am.get('breaker_id') else None
        if breaker:
            tripped = breaker.get('state') not in ('reset', 'closed')
            t.add('filtered', self.failed if tripped else self.done, breaker.get('tripped_at'),
                  self.msg('trace._local.5') + ('' if tripped else self.msg('trace._local.6')), [(self.msg('trace._local.7'), breaker.get('state'))])
        else:
            t.note('filtered', self.msg('trace._local.8'))
        t.note('triage', self.msg('trace._local.9'))
        t.add('route', self.done, e.get('received'), self.msg('trace._local.10', get=m.get('task_id')), (),
              [link(self.msg('trace._local.11', get=m.get('task_id')), f'/runs?task={m.get("task_id")}', 'run')])
        t.flags.add('delivered')
        self._event_chain(t, e, m.get('task_id'))
        self._close_notes(t, [], None)

    def _event_item(self, t):
        s = t.s
        e = s.event(t.subject['id'])
        if e is None:
            raise KeyError('not_found')
        task = s.task_of_event(e)
        w = s.watch(e['watch_id'])
        root = (s.one('SELECT root FROM event_sources WHERE event_id=?', (e['id'],), 'event_sources') or {}).get('root')
        env = s.one('SELECT * FROM event_envelopes WHERE event_id=?', (e['id'],), 'event_envelopes') or {}
        author = str(e.get('author') or '')
        t.meta.update(ref=e['id'], at=tsf(e['ts']) if TS_RE.fullmatch(str(e['ts'])) else tsf(e.get('received')),
                      channel=e['channel'], conv_type='channel' if str(e['channel']).startswith('C') else 'im', thread=root,
                      where=self._conv_label(s, 'channel' if str(e['channel']).startswith('C') else 'im', e['channel']),
                      who=self._who(s, author if re.fullmatch(r'[UW][A-Z0-9]+', author) else None, env.get('author_kind')),
                      msg_count=1, slack_url=slack_url(e['channel'], e['ts'], root), title=self.trigger.get(env.get('kind'), self.msg('trace._event_item.1')))
        t.add('received', self.done, t.meta['at'], self.msg('trace._event_item.3', get=self.trigger.get(env.get('kind'), self.msg('trace._event_item.2')), value=t.meta['who']['name'] or author or '?'),
              [(self.msg('trace._event_item.4'), t.meta['where'], 'mono'), (self.msg('trace._event_item.5'), ' · '.join(x for x in (env.get('author_kind'), env.get('trust'),
                                                                                   env.get('rule')) if x) or None)],
              [link(self.msg('trace._event_item.6'), t.meta['slack_url'], 'slack')])
        t.note('filtered', self.msg('trace._event_item.7'))
        records = s.all('SELECT * FROM triage_records WHERE event_id=? ORDER BY created_at', (e['id'],), 'triage_records')
        for rec in records:
            self._triage_step(t, rec)
        t.add('route', self.done, e.get('received'), self.msg('trace._event_item.9', value=task or self.msg('trace._event_item.8')),
              [(self.msg('trace._event_item.10'), self.watch_kind.get(w.get('kind'), w.get('kind'))), (self.msg('trace._event_item.11'), e['watch_id'], 'mono')],
              [link(self.msg('trace._event_item.12', task=task), f'/runs?task={task}', 'run')] if task else ())
        t.flags.add('delivered')
        self._event_chain(t, e, task)
        if not records:
            t.note('triage', self.msg('trace._event_item.13'))
        self._close_notes(t, records)

    def _card_item(self, t):
        s = t.s
        c = s.one('SELECT * FROM escalation_cards WHERE event_id=?', (t.subject['id'],), 'escalation_cards')
        if c is None:
            raise KeyError('not_found')
        x = s.one('SELECT * FROM escalations WHERE event_id=?', (c['event_id'],), 'escalations') or {}
        w = s.watch(x.get('watch_id')) if x.get('watch_id') else {}
        session = (w.get('posted_by') or {}).get('session') or w.get('session')
        t.meta.update(ref=c['event_id'], at=tsf(c.get('created')), channel=c.get('channel'), conv_type='channel',
                      thread=c.get('thread_root'), where=self._conv_label(s, 'channel', c.get('channel')),
                      who={'id': None, 'name': c.get('task_id') or self.msg('trace._card_item.1'), 'avatar': False, 'kind': 'agent'}, msg_count=1,
                      slack_url=slack_url(c.get('channel'), x.get('thread_ts'), c.get('thread_root')), title=self.msg('trace._card_item.2'))
        for stage in ('received', 'filtered', 'triage'):
            t.note(stage, self.msg('trace._card_item.3'))
        t.task(c.get('task_id'))
        if c.get('task_id'):
            t.add('route', self.done, c.get('created'), self.msg('trace._card_item.4', task_id=c['task_id']), (),
                  [link(self.msg('trace._card_item.5', task_id=c['task_id']), f'/runs?task={c["task_id"]}', 'run')])
        run = s.one('SELECT * FROM consumer_runs WHERE session=? AND started_at<=? ORDER BY started_at DESC LIMIT 1',
                    (session, (tsf(c.get('created')) or 0) + 5), 'consumer_runs') if session else None
        if run:
            self._run_step(t, run, run.get('task'))
        self._card_step(t, c)
        self._close_notes(t, [], None)

    def build(self, store, subject):
        t = Trace(store, subject, core=self)
        self.builders[subject['kind']](t)
        trace = t.finish()
        trace['focus'] = subject.get('focus')
        trace['ref'] = trace.get('ref') or subject.get('ref')
        return trace

    def in_view(self, item, view):
        flags = set(item.get('flags') or ())
        if view == 'all':
            return item['kind'] in ('burst', 'seen', 'card_reply', 'event') and not flags & {'filtered', 'own'}
        if view == self.key:
            return item['status'] == self.key
        if view == 'failed':
            return item['status'] == 'failed' or bool(item.get('stuck'))
        if view == 'logged':
            return 'logged' in flags or item.get('intent') == 'chit_chat'
        if view == 'filtered':
            return bool(flags & {'filtered', 'own'})
        if view == 'card_reply':
            return item['kind'] == 'card_reply'
        if view == 'agent':
            return item['kind'] == 'local'
        return False

    def inbox(self, store, days=LIST_DAYS):
        """Every inbox item of the last `days` days (summaries, newest message first). Run logs are not read here."""
        s = store
        since = s.now - max(1, min(LIST_MAX_DAYS, int(days))) * 86400
        s.prefetch_envelopes(since - 86400)
        items = []
        for subject in list_subjects(s, since):
            try:
                items.append(summary(self.build(s, subject)))
            except (KeyError, sqlite3.Error, ValueError, TypeError):
                continue
        items.sort(key=lambda x: -(x.get('at') or 0))
        return items

    def trace_ref(self, db_path, ref, now=None, runs_root=None, logs=True, inbox_tasks=_OWNER_DEFAULT, days=7):
        """{'parsed', 'trace', 'matches'} for one reference; KeyError when nothing matches, ValueError when unparsable."""
        inbox_tasks = self.inbox_tasks if inbox_tasks is _OWNER_DEFAULT else inbox_tasks
        parsed = parse_ref(ref)
        if parsed is None:
            raise ValueError('invalid_ref')
        with contextlib.closing(open_db(db_path)) as db:
            db.execute('BEGIN')
            s = Store(db, now, runs_root if runs_root is not None else Path(db_path).resolve().parent / 'runs', logs, inbox_tasks, core=self)
            if parsed['kind'] == 'task':
                light = Store(db, s.now, None, False, inbox_tasks, core=self)
                matches = [x for x in self.inbox(light, days) if parsed['id'] in (x.get('tasks') or [])]
                if not matches:
                    raise KeyError('not_found')
                subjects = resolve(s, parse_ref(matches[0]['ref']))
                if not subjects:
                    raise KeyError('not_found')
                subject = subjects[0]
            else:
                subjects = resolve(s, parsed)
                if not subjects:
                    raise KeyError('not_found')
                subject = subjects[0]
                light = Store(db, s.now, None, False, inbox_tasks, core=self)
                matches = [summary(self.build(light, x)) for x in subjects[:50]]
            trace = self.build(s, subject)
        return {'parsed': parsed, 'trace': trace, 'matches': matches}

    def render_text(self, result):
        t = result['trace']
        who = (t.get('who') or {}).get('name') or (t.get('who') or {}).get('id') or ''
        out = [f"{t.get('title')} · {t.get('where') or ''} · {who} · {self.clock(t.get('at'))} BJT  [{t['status']}]",
               f'  {t["headline"]}']
        if t.get('slack_url'):
            out.append(f'  {t["slack_url"]}')
        out.append('  ' + '  '.join(f"{self.icon[x['status']]} {x['label']}" for x in t['stages']))
        for x in t['stages']:
            if not x['count']:
                out.append(f"\n[{x['label']}] {self.icon[x['status']]} {x['reason'] or self.msg('trace.render_text.1')}")
                continue
            out.append(f'\n[{x["label"]}]')
            for step in [y for y in t['steps'] if y['stage'] == x['key']]:
                out.append(f"  {self.icon[step['status']]} {self.clock(step['at'])}  {step['title']}")
                for fact in step['facts']:
                    out.append(f'      {fact[0]}: {fact[1]}')
                for l in step['links']:
                    out.append(f'      {l["label"]}: {l["href"]}')
                for line in step['lines']:
                    out.append(f'      » {line}')
        if len(result['matches']) > 1:
            out.append(self.msg('trace.render_text.2', value=len(result['matches']) - 1) + ', '.join(m['ref'] for m in result['matches'][1:6]) + ')')
        return '\n'.join(out)


def cli_main(argv=None, *, language, default_db, description, trace_ref, render_text):
    """The trace command line: a reference (a Slack link, CHANNEL/TS, a burst / event / dispatch / run id or a task id),
    `--db` (the deployment's database by default) and `--json`; `trace_ref` / `render_text` are the deployment's (its
    names, looked up at call time). Exit 2: not a reference; 1: no record; 0: printed."""
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('ref')
    parser.add_argument('--db', type=Path, default=default_db)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = trace_ref(args.db.expanduser(), args.ref)
    except ValueError:
        print(locale.message(language, 'trace.cli.unrecognized'), file=sys.stderr)
        return 2
    except KeyError:
        print(locale.message(language, 'trace.cli.not_found'), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1) if args.json else render_text(result))
    return 0
