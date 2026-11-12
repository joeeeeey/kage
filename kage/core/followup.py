"""Follow-up core (K14a): the main program's constants, pure helpers and read models — watches, Slack error
codes, who may write into a watch, delegates and envelopes, the decision / ask annotations of an event, the
identity checks, the approvals-thread, decision and ask read models, `--about` references, the
ask-owner card text, local events, the run / takeover / tail read models and the terminal-only check.
Everything it does not hold it reaches through the runtime (`rt`).
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.error

from kage import locale
from kage.core import authority as _authority
from kage.core.dispatch import watchdog_overview as _watchdog_overview

IDLE_SECONDS = 2 * 86400


def load(db, wid):
    row = db.execute('SELECT data FROM watches WHERE id=?', (wid,)).fetchone()
    if not row:
        raise ValueError('unknown_watch')
    return json.loads(row['data'])


def clamp_poll(value, deadline):
    return min(value, deadline) if deadline is not None else value


def expired(w, now):
    return w.get('deadline') is not None and now >= w['deadline']


def safe_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP_{exc.code}'
    match = re.fullmatch(r'Slack API error: ([a-z_]+)', str(exc))
    return match[1] if match else type(exc).__name__


LOCAL_CHANNEL = 'LOCAL'


def local_message(db, eid):
    if db is None:
        return None
    try:
        row = db.execute('SELECT * FROM local_messages WHERE event_id=?', (eid,)).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


APPROVALS_KIND = 'approvals'  # long-lived task-level watch over the whole approvals thread (kind in watch.data)


IN_FLIGHT_SQL = "'queued','claimed','sending','delivery_unknown'"  # delivered to a session, not yet resolved by it


def _approvals_root(db, task_id):
    """The task's inherited HOME approvals root {channel, root_ts} (set only when the task was launched from a card), or
    None. P36: this is NOT a per-task catch-all — a plain task has no home root, so each of its independent cards is its
    own topic root. Only a launched-from-card task carries a home root, under which its cards about that request thread."""
    if not task_id:
        return None
    row = db.execute('SELECT channel, root_ts FROM approvals_roots WHERE task_id=? AND home=1 ORDER BY created LIMIT 1',
                     (str(task_id),)).fetchone()
    return {'channel': row['channel'], 'root_ts': row['root_ts']} if row else None


def _card_root(db, event_id):
    """The {channel, root_ts} of an existing card's thread (its own ts when it is itself a root): the root a CONTINUATION
    of that card (a blocker stage-2, a reminder, a confirmation) threads under. P36 links continuations by parent-card
    id, never by task — so distinct requests from one long-lived task never share a thread."""
    if not event_id:
        return None
    card = db.execute('SELECT c.channel, c.thread_root, x.thread_ts FROM escalation_cards c JOIN escalations x '
                      'ON x.event_id=c.event_id WHERE c.event_id=?', (str(event_id),)).fetchone()
    if card is None or not card['channel']:
        return None
    root = card['thread_root'] or card['thread_ts']
    return {'channel': card['channel'], 'root_ts': root} if root else None


def _approvals_wid(channel, root_ts):
    """One approvals watch per thread root (not per task): a task launched from a card shares the parent's root."""
    return hashlib.sha256(f'approvals/{channel}/{root_ts}'.encode()).hexdigest()[:20]


def _card_reply_thread(db, card, esc):
    """The thread root to match the owner's replies against: a threaded card records its root (its replies carry thread_ts =
    root); otherwise the card's own ts. Chat.update still targets the card's own ts (esc.thread_ts)."""
    root = card['thread_root'] if 'thread_root' in card.keys() else None
    return root or esc['thread_ts']


def _secret_thread(db, channel, root):
    """P38: a card in this Slack thread waits for a verification code (its decision watch is secret_input)."""
    if not channel or not root:
        return False
    return db.execute("SELECT 1 FROM escalation_cards c JOIN escalations x ON x.event_id=c.event_id JOIN watches w ON "
                      "w.id=x.watch_id WHERE c.channel=? AND coalesce(c.thread_root,x.thread_ts)=? AND "
                      "json_extract(w.data,'$.secret_input') LIMIT 1", (channel, str(root))).fetchone() is not None


def _in_flight(db, wid):
    return db.execute('SELECT 1 FROM events WHERE watch_id=? AND status IN (%s) LIMIT 1' % IN_FLIGHT_SQL,
                      (wid,)).fetchone() is not None


SUPERVISOR_STAGE = 'supervisor'  # escalations.stage of a code-owned card (supervisor_card): no watch, no session event


def _escalation_watch(db, eid):
    row = db.execute('SELECT watch_id FROM escalations WHERE event_id=?', (eid,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        return load(db, row[0])
    except ValueError:
        return None


def _proposal(db, pid):
    return db.execute('SELECT * FROM decision_proposals WHERE id=?', (pid,)).fetchone()


def _fail(reason, **extra):
    return {'ok': False, 'reason': reason, **extra}


def _ids_valid(actor, channel, ts):
    return bool(re.fullmatch(r'U[A-Z0-9]+', str(actor)) and re.fullmatch(r'[CG][A-Z0-9]+', str(channel))
                and re.fullmatch(r'\d+\.\d+', str(ts)))


ABOUT_EVENT_RE = re.compile(r'[0-9a-f]{24}')


ABOUT_TS_RE = re.compile(r'\d{1,10}\.\d{6}')


ABOUT_LINK_RE = re.compile(r'https://[a-z0-9-]+\.slack\.com/archives/([CGD][A-Z0-9]{2,})/p(\d{1,10})(\d{6})(?:\?[\w=&.%-]*)?')


ABOUT_PR_RE = re.compile(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/(\d{1,7})/?')


def _ask_card(db, card_id):
    return db.execute('SELECT * FROM ask_cards WHERE id=?', (card_id,)).fetchone()


def _ask_root(card):
    return card['thread'] or card['ts']


def _ask_owner(db, card):
    if card['watch_id']:
        try:
            return load(db, card['watch_id'])
        except ValueError:
            pass
    return {'session': card['owner_session'], 'provider': card['provider'], 'cwd': ''}


def _covering_watch(db, provider, session, channel=None, thread=None, dm=None):
    """Asks only go where this session already follows the conversation."""
    rows = [json.loads(r[0]) for r in db.execute(
        "SELECT data FROM watches WHERE coalesce(json_extract(data,'$.provider'),'codex')=? AND "
        "json_extract(data,'$.session')=? AND json_extract(data,'$.state')='running'", (provider, session))]
    rows = [w for w in rows if w.get('kind') != 'decision']
    if dm:
        for w in rows:
            if dm in w['peers'] and w['channel'].startswith('D'):
                return w, w['channel'], w['thread']
            if dm in w['peers'] and w.get('dm'):
                return w, w['dm'], None
        raise ValueError('dm_not_covered_by_a_running_watch_of_this_session')
    for w in rows:
        if w['channel'] == channel and w.get('scope', 'thread') == 'thread' and w['thread'] == thread:
            return w, channel, thread
    for w in rows:
        if w['channel'] == channel and w.get('scope') == 'channel' and \
                thread in (w.get('fetch_state') or {}).get('roots', []):
            return w, channel, thread
    raise ValueError('thread_not_covered_by_a_running_watch_of_this_session')


def _proposal_row(db, pid):
    return db.execute('SELECT * FROM ask_proposals WHERE id=?', (pid,)).fetchone()


def intake_burst_ref(db, eid):
    try:
        row = db.execute('SELECT burst_id,route,route_reason,target_task,conv_type,channel,thread_root,sender,msg_count,'
                         'person_style,alert_fp,dup_count,smoke FROM intake_bursts WHERE event_id=?', (eid,)).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def _task_row(db, task_id):
    row = db.execute('SELECT * FROM tasks WHERE task_id=?', (str(task_id),)).fetchone()
    if row is None:
        raise ValueError('unknown_task')
    return dict(row)


DISPATCH_ID_RE = re.compile(r'd[0-9a-f]{15}')


RUN_ID_RE = re.compile(r'r\d{8}T\d{6}Z-[0-9a-f]{6}')


def _latest_run(db, dispatch_id=None, task=None):
    where, arg = ('dispatch_id=?', dispatch_id) if dispatch_id else ('task=?', task)
    row = db.execute(f'SELECT * FROM consumer_runs WHERE {where} ORDER BY started_at DESC, rowid DESC LIMIT 1',
                     (arg,)).fetchone()
    return dict(row) if row else None


def _run_counts(db, run):
    events = json.loads(run.get('event_ids') or '[]')
    if not events:
        return 0, 0
    handled = db.execute("SELECT count(*) FROM events WHERE status='handled' AND id IN (%s)" % ','.join('?' * len(events)),
                         events).fetchone()[0]
    return handled, len(events)


def _run_elapsed(run, now):
    return max(0.0, (run.get('ended_at') or now) - (run.get('started_at') or now))


def dispatch_held(db, dispatch_id):
    return [r[0] for r in db.execute("SELECT id FROM events WHERE status='dispatched' AND receipt=?",
                                     ('dispatch:' + dispatch_id,))]


def _takeover_session(db, disp, key):
    """(provider, session) the owner resumes: the running run's, else the task's current one, else its latest run's."""
    run = db.execute("SELECT provider, session, state FROM consumer_runs WHERE task=? AND session IS NOT NULL ORDER BY "
                     "state='running' DESC, started_at DESC, rowid DESC LIMIT 1", (key,)).fetchone()
    current = (disp.task_for_key(key) or {}).get('current')
    return (run[0], run[1]) if run and (run[2] == 'running' or not current) else current or (None, None)


def _held_count(db, disp, key):
    rows = db.execute("SELECT dispatch_id FROM dispatches WHERE task_key=? AND state NOT IN ('done','superseded')",
                      (key,)).fetchall()
    return sum(len(disp._held_events(r[0])) for r in rows)


def _tail_latest(db, kind, ref):
    if kind == 'run':
        return _latest_run_by_id(db, ref['run_id'])
    return _latest_run(db, dispatch_id=ref['dispatch_id']) if kind == 'dispatch' else _latest_run(db, task=ref)


def _latest_run_by_id(db, run_id):
    row = db.execute('SELECT * FROM consumer_runs WHERE run_id=?', (run_id,)).fetchone()
    return dict(row) if row else None


def _run_exit_info(run_dir):
    try:
        info = json.loads((Path(run_dir) / 'exit.json').read_text())
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def _tail_print(out, text=''):
    out.write(text + '\n')
    out.flush()


class FollowupCore:
    """The owner-bound half of the follow-up core: the owner's Slack user, bot, app and workspace, the
    deployment's agent marker, dashboard and tool names, the texts in the owner's words, and the runtime
    (`rt`) every name it does not hold is reached through."""

    def __init__(self, language, *, owner_key, owner_name, workspace, owner_slack_user, bot_user, app_id, agent_event_type, dashboard_url, run_context_env, run_id_env, tail_command, rt):
        self.language = language
        self.workspace = workspace
        self.owner_slack_user = owner_slack_user
        self.bot_user = bot_user
        self.app_id = app_id
        self.agent_event_type = agent_event_type
        self.dashboard_url = dashboard_url
        self.run_context_env = run_context_env
        self.run_id_env = run_id_env
        self.tail_command = tail_command
        self.rt = rt
        self.key, self.name = owner_key, owner_name
        self.thread_reply_guidance = (f'{self.name} typed this decision in the card thread. Read his reply (source_ts) in context.messages: any text after the leading phrase is guidance on how to carry the decision out, not a condition and not a different decision. If it asks for something the chosen option cannot do, do not execute; ask in the card thread first.')
        self.about_help = (f"--about takes a card's event id (ask-{self.key} printed it as event_id), the ts or Slack link of a card in {self.name}'s decision channel, or a GitHub PR link (https://github.com/OWNER/REPO/pull/N)")
        self.outcome_label = {'success': self.msg('followup.outcome_label.success'), 'failed': self.msg('followup.outcome_label.failed'), 'partial': self.msg('followup.outcome_label.partial'), 'unresolved': self.msg('followup.outcome_label.unresolved'),
                              'stopped': self.msg('followup.outcome_label.stopped'), None: '…'}
        self.terminal_only_env = ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', self.run_context_env,
                                  self.run_id_env)

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def eligible(self, w, m, extra_peers=()):
        # Names, mentions and forwarded claims never confer owner authority.
        # extra_peers: responders of this watch's ask cards (conversation, never authority).
        if m.get('bot_id') or m.get('app_id') == self.app_id or m.get('subtype') not in [None, 'file_share']:
            return False
        author = m.get('user', '')
        if not re.fullmatch(r'U[A-Z0-9]+', author) or author == self.bot_user:
            return False
        if w.get('kind') in ('decision', APPROVALS_KIND):
            # P3.2 approvals watch: the owner's own replies anywhere in the task's approvals thread are its input (the per-card
            # decision watches cover only their own card's ts); in-window OOO delegates' text is conversation, as on cards.
            # P39 (F21): an approvals watch takes the delegates in window from the decisions config (`extra_peers`, see
            # ingest) — it never had its own list, so a delegate's text in a covered card thread reached nobody.
            if m.get('subtype'):
                return False
            start, end = w.get('delegate_window') or (0, 0)
            return author == self.owner_slack_user or (author in w.get('delegates', []) and start <= float(m.get('ts', 0)) <= end) or \
                (w.get('kind') == APPROVALS_KIND and author in extra_peers)
        return author != self.owner_slack_user and (w.get('context_all') or not w['peers'] or author in w['peers'] or author in extra_peers)

    def dm_routing_active(self, now=None):
        """P39: intake routes new top-level DM messages (intake enabled + bootstrapped + intake.dm_request_routing), so
        watches no longer capture them. An unreadable config keeps the pre-P39 capture (fail safe: nothing is dropped)."""
        try:
            cfg = self.rt.worker_config.load()[0]['intake']
            return bool(self.rt.intake.dm_routing(cfg) and self.rt.registry.task_dir(cfg['inbox_task']).is_dir())
        except Exception:  # noqa: BLE001
            return False

    def observation_in_window(self, w, channel, m, now=None, dm_routing=None):
        now = time.time() if now is None else now
        if channel not in (w['channel'], w.get('dm')):
            return False
        ts = m.get('ts', '')
        if not re.fullmatch(r'\d+\.\d+', str(ts)):
            return False
        if w.get('thread') and channel == w['channel']:
            if (m.get('thread_ts') or w['thread']) != w['thread']:
                return False
        if w.get('lifecycle') != 'idle':
            if self.rt.avatar_cache.valid_human(m):
                self.rt.schedule.activity(w, float(ts), now)
            return True
        # Only new posts refresh activity; edits, reactions, duplicate reads and resolves do not.
        sent = float(ts)
        if sent > w['deadline']:
            return False
        dm = str(channel).startswith('D') and w.get('kind', 'peer') == 'peer' and (
            self.dm_routing_active(now) if dm_routing is None else dm_routing)
        if self.rt.avatar_cache.valid_human(m) and sent > max(float(w['since']), w.get('last_activity', w['started'])) and not dm:
            # P39 rule 5: a DM watch's 48 h are extended only by its task's own sends (poll_schedule.record_outbound) —
            # while intake routes DMs (dm_request_routing); with that off, the pre-P39 extension applies again.
            w.update(last_activity=sent, deadline=sent+w.get('inactivity_seconds', IDLE_SECONDS))
        if self.rt.avatar_cache.valid_human(m):
            self.rt.schedule.activity(w,sent,now)
        return True

    def _envelope_inputs(self, scope):
        if 'people' not in scope:
            try:
                scope['config'] = self.rt.buttons.load_config()
            except Exception:  # noqa: BLE001 - malformed decisions config: no delegates, ingest unaffected
                scope['config'] = None
            scope['people'] = self.rt.envelopes.load_people()
        return scope

    def _delegates_at(self, w, ts, scope):
        delegates = set(self.rt.envelopes.active_delegates(scope.get('config'), ts, self.rt.buttons))
        start, end = w.get('delegate_window') or (0, 0)
        if start <= float(ts) <= end:
            delegates.update(w.get('delegates', []))
        return delegates

    def message_envelope(self, w, m, root, scope=None):
        """Code-derived envelope for an ingested message; None (never an exception) on failure."""
        try:
            scope = self._envelope_inputs({} if scope is None else scope)
            return self.rt.envelopes.classify_message(m, thread_root=root, delegates=self._delegates_at(w, m.get('ts', 0), scope),
                                              people=scope['people'])
        except Exception:  # noqa: BLE001
            return None

    def interaction_envelope(self, w, kind, actor, root, ts, **kw):
        try:
            scope = self._envelope_inputs({})
            return self.rt.envelopes.interaction(kind, actor, thread_root=root, delegates=self._delegates_at(w, ts, scope),
                                         people=scope['people'], **kw)
        except Exception:  # noqa: BLE001
            return None

    def decision_annotation(self, db, eid):
        """Owner-authored option metadata for button/confirmation events; never Slack message text."""
        if db is None:
            return ''
        decision = self.button_decision(db, eid)
        blocker = self.rt.blockers.annotation(self.rt, db, eid)  # P3.1: ` blocker=<id>:stage1|stage2|proposal|reply`
        typed = self.thread_reply_decision_of(db, eid)  # P3.1: the owner's reply that code recorded as the card decision
        if typed:
            blocker += f" recorded_as_decision={typed['kind']}:{typed['option_id']} (decision event follows; rest is guidance)"
        if decision:
            return (f" decision={decision['kind']}:{decision['option_id']} label={json.dumps(decision['label'], ensure_ascii=False)}"
                    f" via={decision['provenance']}{blocker}")
        proposal = self.rejected_proposal(db, eid)
        if proposal:
            return f" proposal_rejected={proposal['kind']}:{proposal['option_id']} (ask again; not a decision){blocker}"
        if blocker:
            return blocker
        answer = self.ask_answer_context(db, eid)
        if answer:
            return f" ask_answer={answer['option']['kind']}:{answer['option']['option_id']} via={answer['via']} authority=none"
        if self.ask_expired_context(db, eid):
            return ' ask_expired (no reply) authority=none'
        if self.ask_proposal_context(db, eid):
            return ' ask_proposal_rejected (ask again) authority=none'
        return ''

    def envelope_annotation(self, db, eid):
        """Code-derived trust/authority tags; informational, they change no routing."""
        return self.rt.envelopes.prompt_suffix(self.rt.envelopes.lookup(db, eid)) if db is not None else ''

    def health_report(self, db, now=None):
        """P38 read model shared with the dashboard's /health: providers, stranded deliveries, stuck claims, (F49)
        the owner-facing posts the private-destination guard refused, and (P44) the provider probes and the dispatches waiting
        for a provider to come back."""
        now = time.time() if now is None else now
        config = self.rt.worker_config.load()[0]
        return {'providers': self.rt.provider_health.overview(db, self.rt.worker_config, config, now),
                'stranded': self.rt.dispatch.stranded_overview(db, now),
                'stuck_claims': self.rt.stuck_claims.overview(db, self.rt.claude, config['dispatch']['stuck_claim_minutes'], now),
                'card_destination': self.rt.buttons.destination_overview(db, now),
                'provider_wait': self.rt.dispatch.provider_wait_overview(db, now),  # P44
                'watchdog': _watchdog_overview(db, config, now)}  # 0.79.2 (F26)

    def dispatch_consumer(self, db, e):
        """P38 G3: {provider, session, task_path} of the RUNNING run of the dispatch that holds event `e` (status
        `dispatched`, receipt `dispatch:<id>`), else None. A fresh Codex run the supervisor has not bound yet is read from
        its own run log (`thread.started`), exactly the id `capture_session` will bind."""
        if e['status'] != 'dispatched' or not str(e['receipt'] or '').startswith('dispatch:'):
            return None
        run = db.execute("SELECT r.provider, r.session, r.log_dir, r.task_path FROM dispatches d JOIN consumer_runs r ON "
                         "r.run_id=d.run_id WHERE d.dispatch_id=? AND d.state='running' AND r.state='running'",
                         (e['receipt'][len('dispatch:'):],)).fetchone()
        if run is None:
            return None
        session = run['session']
        if not session and run['provider'] == 'codex' and run['log_dir']:
            session = self.rt.dispatch.parse_run_log('codex', self.rt.dispatch._read_head(Path(run['log_dir'], 'events.jsonl')))['session']
            session = session if session and self.rt.liveness.UUID_RE.fullmatch(session) else None
        return {'provider': run['provider'], 'session': session, 'task_path': run['task_path']} if session else None

    def by_dispatch(self, db, e):
        """True when the caller is the running run of the dispatch holding `e` (G3): a dispatched event belongs to its
        dispatch, so the run can read and resolve it whatever later happened to the watch's binding (a reconcile, a
        reroute to another live session, an explicit stop)."""
        actor = self.rt.claude.actor()
        consumer = self.dispatch_consumer(db, e) if actor[1] else None
        return bool(consumer and (consumer['provider'], consumer['session']) == actor)

    def require_event_owner(self, db, e, w):
        """`require_owner` for one event, plus P38 G3 (`by_dispatch`)."""
        if not self.by_dispatch(db, e):
            self.require_owner(w)

    def require_owner(self, w):
        if self.rt.claude.actor() != (self.rt.claude.provider(w), w['session']):
            if self.rt.claude.provider(w) == 'claude':
                raise ValueError('current_session_must_match_watch_owner: set CLAUDE_CODE_SESSION_ID to the actual owning Claude UUID; CODEX_THREAD_ID is not a Claude identity')
            raise ValueError('current_session_must_match_watch_owner')
        if w['state'] == 'stopped':
            raise ValueError('watch_stopped')

    def agent_metadata(self, kind, w=None, **refs):
        """Hidden Slack metadata on every runtime post: agent-sent vs hand-typed is machine-readable."""
        payload = {'kind': kind}
        if w is not None:
            payload.update(session=w['session'], provider=self.rt.claude.provider(w), task_cwd=w['cwd'])
        payload.update({k: str(v) for k, v in refs.items() if v is not None})
        return {'event_type': self.agent_event_type, 'event_payload': payload}

    def _cards_config(self, db):
        return self.rt.card_replies.config(self.rt)

    def _task_thread_enabled(self, db):
        """P3.2 kill switch `cards.task_thread` (false = the pre-P3.2 top-level behaviour; a malformed config is off)."""
        try:
            return bool(self._cards_config(db)['task_thread'])
        except Exception:  # noqa: BLE001 - fail closed to the reversible top-level behaviour
            return False

    def _task_thread_switch(self, db):
        """P38 (F19): True / False as configured, None when the config cannot be read right now. Only an explicit False
        stops the approvals watches: a config glitch (mid-write, bad mode) must never mass-stop and resume them."""
        try:
            config = self.rt.worker_config.load()[0]['cards']
        except Exception:  # noqa: BLE001
            return None
        return bool(config['task_thread'])

    def _approvals_watch_active(self, db, channel, root_ts):
        """True when a running task-level approvals watch covers this thread (so it, not the per-card path, routes replies)."""
        if not root_ts or not channel or not self._task_thread_enabled(db):
            return False
        row = db.execute('SELECT data FROM watches WHERE id=?', (_approvals_wid(channel, root_ts),)).fetchone()
        return bool(row and json.loads(row[0]).get('state') != 'stopped')

    def _approvals_task(self, w, db=None):
        """The task an approvals watch is bound to — or, while a rebind waits, the task it waits to move to (a
        launch-from-card handoff the tick and the reconcile must both keep aiming at, never undo). P39 (F20): the root's
        registered owner (`approvals_roots.task_id`, which a launch-from-card child takes over at inherit time, before it
        has a session) wins over the task the watch was created for, so the child takes the thread over once it can."""
        waiting = (w.get('rebind_waiting') or {}).get('task_id')
        if waiting or db is None:
            return waiting or w['task_id']
        owner = db.execute('SELECT task_id FROM approvals_roots WHERE channel=? AND root_ts=? AND task_id IS NOT NULL',
                           (w['channel'], w['thread'])).fetchone()
        return owner[0] if owner and self.rt.registry._row(db, owner[0]) else w['task_id']

    def button_decision(self, db, eid):
        """Verified decision for an outbox event, from SQLite only (never message text)."""
        row = db.execute('SELECT d.*, c.options FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                         'WHERE d.delivered_event=?', (eid,)).fetchone()
        if row is None:
            return None
        model = self.rt.buttons.card_model(row['options'])
        option = self.rt.buttons.resolve_choice(row['choice'], model)
        result = {'provenance': row['provenance'], 'verified': True,
                  'verified_by': ('Socket Mode block_actions user.id on the card; stored card channel/ts matched; '
                                  'actor authorized by decisions.json') if row['provenance'] == 'slack_button' else
                                 ('TEST card only: synthetic decision recorded by smoke-blocker (no Slack click)')
                                 if row['provenance'] == 'smoke_synthetic' else
                                 (f"{self.name}'s own hand-typed reply in the card thread (source_ts), read back via the Slack API (no app id, agent metadata or send-ledger hit); its leading phrase or option label mapped by code to exactly this option; {self.name} authorized by decisions.json")
                                 if row['provenance'] == 'thread_reply' else
                                 (f'source message author read via Slack API at propose time; confirmation click user.id is the source author or {self.name}; both authorized by decisions.json'),
                  'escalation_event': row['event_id'], 'choice': row['choice'],
                  'option_id': option['id'], 'label': option['label'], 'kind': option['kind'],
                  'recommended': option['recommended'], 'note': row['note'],
                  'card_options': [self.rt.buttons.public_option(o) for o in model],
                  'actor': row['actor'], 'actor_role': row['role'], 'decided_at': self.rt.buttons.iso(row['decided_at']),
                  'card': {'channel': row['card_channel'], 'ts': row['card_ts']}, 'receipt_ts': row['receipt_ts']}
        if row['proposal_id']:
            p = _proposal(db, row['proposal_id'])
            result.update(proposal_id=row['proposal_id'], source_ts=row['source_ts'],
                          source_author=p['proposer_user'] if p else None)
        if row['provenance'] == 'thread_reply':
            result.update(source_ts=row['source_ts'], guidance=self.thread_reply_guidance)
            api = db.execute('SELECT app FROM owner_api_replies WHERE channel=? AND ts=?',
                             (row['card_channel'], row['source_ts'])).fetchone() if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_api_replies'").fetchone() else None
            if api:  # 0.78.8 (F20): typed through an allowed app with the owner's own user token
                result.update(via='api', via_app=api['app'], verified_by=result['verified_by'].replace(
                    'hand-typed reply', 'reply posted through the allowed app ' + repr(api['app']) + ' with their own user '
                    'token (users.info: a person; bot-identity deployment)').replace(
                    ' (no app id, agent metadata or send-ledger hit)', ' (not our app, no agent metadata or send-ledger hit)'))
        return result

    def thread_reply_decision_of(self, db, eid):
        """A Slack message event that code recorded as a card decision (P3.1 thread_reply), or None."""
        row = db.execute("SELECT d.*, c.options FROM events e JOIN decisions d ON d.card_channel=e.channel AND "
                         "d.source_ts=e.ts AND d.provenance='thread_reply' JOIN escalation_cards c ON c.event_id=d.event_id "
                         'WHERE e.id=? AND e.channel!=?', (eid, LOCAL_CHANNEL)).fetchone()
        if row is None:
            return None
        option = self.rt.buttons.resolve_choice(row['choice'], self.rt.buttons.card_model(row['options']))
        return {'escalation_event': row['event_id'], 'option_id': option['id'], 'label': option['label'],
                'kind': option['kind'], 'decision_event': row['delivered_event'],
                'instruction': ('Code recorded this reply as the card decision (thread_reply); it arrives as its own '
                                'decision event. Do not propose-decision again; the rest of the text is guidance.')}

    def rejected_proposal(self, db, eid):
        """A "✏️ 不对" click: not a decision; the owner must ask again."""
        p = db.execute('SELECT p.*, c.options FROM decision_proposals p JOIN escalation_cards c ON c.event_id=p.event_id '
                       'WHERE p.rejected_event=?', (eid,)).fetchone()
        if p is None:
            return None
        option = self.rt.buttons.resolve_choice(p['choice'], self.rt.buttons.card_model(p['options']))
        return {'proposal_id': p['id'], 'status': p['status'], 'provenance': 'confirmation_button',
                'escalation_event': p['event_id'], 'choice': p['choice'], 'option_id': option['id'],
                'label': option['label'], 'kind': option['kind'], 'note': p['note'],
                'source_ts': p['source_ts'], 'source_author': p['proposer_user'], 'rejected_by': p['decided_by'],
                'rejected_at': self.rt.buttons.iso(p['decided_at']),
                'instruction': 'Not a decision. Ask again in the card thread; do not execute.'}

    def _decision_result(self, row, model, duplicate):
        option = self.rt.buttons.resolve_choice(row['choice'], model)
        return {'ok': True, 'duplicate': duplicate, 'event_id': row['event_id'], 'choice': row['choice'],
                'option_id': option['id'], 'label': option['label'], 'kind': option['kind'],
                'note': row['note'], 'actor': row['actor'],
                'role': row['role'], 'provenance': row['provenance'], 'proposal_id': row['proposal_id'],
                'stage': row['stage'], 'receipt_ts': row['receipt_ts'], 'delivered_event': row['delivered_event']}

    def _agent_session(self):
        # Interactive clicks arrive through the Socket Mode receiver, never an agent session.
        return any(os.environ.get(k) for k in self.rt.buttons.AGENT_ENV)

    def _routed_reply_consumer(self, db, card, eid, source_ts):
        """P38 (G3 / F25): the caller's identity when it is the running run of the dispatch holding the owner's reply `source_ts`
        and the approvals watch routed that reply to card `eid` (or `eid` was open then) — the fresh session handling an
        ambiguous typed decision may turn it into a confirmation, as the card's owner could. Else None."""
        route = self.rt.card_replies.routed(db, card['channel'], source_ts)
        if not route or (route['card_event'] != eid and eid not in route['open_cards']):
            return None
        for e in db.execute("SELECT * FROM events WHERE channel=? AND ts=? AND status='dispatched'",
                            (card['channel'], source_ts)).fetchall():
            if self.by_dispatch(db, e):
                return self.rt.claude.actor()
        return None

    def card_link(self, channel, ts, root=None):
        """A card's Slack link; a card threaded under a root needs the thread part to open from the channel."""
        return self.rt.intake.permalink(channel, ts, root)

    def resolve_about(self, db, value, config=None):
        """{'event_id', 'channel', 'ts', 'link'} for a posted card in the owner's private decision channel, {'pr', 'number'} for a
        GitHub PR link. Anything else is refused (fail closed: a reminder never points at something the owner cannot find)."""
        value = str(value or '').strip()
        pr = ABOUT_PR_RE.fullmatch(value)
        if pr:
            return {'pr': value.rstrip('/'), 'number': int(pr[1])}
        link = ABOUT_LINK_RE.fullmatch(value)
        sql = ('SELECT c.event_id, c.channel, c.thread_root, x.thread_ts FROM escalation_cards c JOIN escalations x ON '
               'x.event_id=c.event_id WHERE c.dry=0 AND x.thread_ts IS NOT NULL AND ')
        if ABOUT_EVENT_RE.fullmatch(value):
            row = db.execute(sql + 'c.event_id=?', (value,)).fetchone()
        elif link or ABOUT_TS_RE.fullmatch(value):
            ts = f'{link[2]}.{link[3]}' if link else value
            row = db.execute(sql + 'x.thread_ts=? AND c.channel=coalesce(?,c.channel) ORDER BY c.created DESC',
                             (ts, link[1] if link else None)).fetchone()
        else:
            raise ValueError('about_not_recognized: ' + self.about_help)
        config = self.rt.buttons.load_config() if config is None else config
        if row is None or not self.rt.buttons.private_card_destination(config, row['channel']):
            raise ValueError('about_card_not_found_in_the_decision_channel: ' + self.about_help)
        return {'event_id': row['event_id'], 'channel': row['channel'], 'ts': row['thread_ts'],
                'link': self.card_link(row['channel'], row['thread_ts'], row['thread_root'])}

    def continuation(self, db, about, task_id, replaces=False, config=None):
        """(--about ref, the waiting card this card takes over or None). P41 / P46, registered as R-0007
        (card.about_thread): an agent card that still waits for the owner is refused unless the task's own session continues it
        (`replaces`, rule-check --about); a card whose buttons a plan_change reply retired no longer waits."""
        ref = self.resolve_about(db, about, config) if about else {}
        waiting = self.card_waiting(db, ref['event_id']) if ref.get('event_id') else None
        if waiting and (waiting['stage'] != 'watching' or db.execute(
                "SELECT 1 FROM card_replies WHERE event_id=? AND stage='retired'", (waiting['event_id'],)).fetchone()):
            waiting = None
        if waiting and not (replaces and waiting['task_id'] and waiting['task_id'] == task_id):
            if self.rt.rule_registry.active('card.about_thread'):
                raise ValueError(f"about_card_still_waits_for_{self.key}: answer him in its thread (its buttons stay); if the plan changed, close it first (ask-{self.key}-close --event-id {ref['event_id']}) and post again")
            waiting = None  # R-0007 off: that card keeps its buttons
        return ref, waiting

    def card_waiting(self, db, eid):
        """P41: a card still waiting for the owner — undecided, an agent or code-owned card whose decision watch (if any) is not
        stopped (ask-<owner key>-close, a resolved blocker). Its card_replies.card row, else None."""
        row = self.rt.card_replies.card(db, eid)
        if row is None or row['stage'] not in ('watching', SUPERVISOR_STAGE) or \
                db.execute('SELECT 1 FROM decisions WHERE event_id=?', (eid,)).fetchone() or \
                self.rt.card_replies.continued(self.rt, db, eid):  # P46: a newer card took it over
            return None
        try:
            return None if row['watch_id'] and load(db, row['watch_id']).get('state') == 'stopped' else row
        except ValueError:
            return None

    def ask_owner_text(self, provider, session, task_id, question, lang=None, delegates=(), priority='normal'):
        """the owner-facing card text (P3.1 copy standard): the mention and the question's first line (one plain sentence:
        what the agent wants to do, to whom, how far) lead; at most two short lines follow. Who asks (task, provider)
        is in the footer (`ask_<owner key>_footer`). In the owner's language unless the caller names another."""
        lang = lang or locale.card_lang(self.language)
        urgent = (self.msg('followup.ask_owner_text.1') if lang == 'zh' else '🚨 *HIGH PRIORITY* · ') if priority == 'high' else ''
        try:  # 0.78.10 (F22): the active approvers when the owners are named apart
            mention = _authority.card_mentions(self.rt.buttons.load_config(), self.owner_slack_user)
        except (ValueError, AttributeError):
            mention = f'<@{self.owner_slack_user}>'
        text = f'{mention} {urgent}{self.rt.buttons.escape(question)}'
        if delegates:
            text += self.msg('followup.ask_owner_text.2', owner=self.name) + ' '.join(f'<@{d}>' for d in delegates) + self.msg('followup.ask_owner_text.3')
        return text

    def ask_owner_footer(self, provider, session, task_id, lang=None, extra=None, tag=None):
        """Context line under the buttons: task name (or session), provider, an optional detail (dashboard link) and, P43,
        the session tag of a background session (`session_footer`)."""
        name = {'claude': 'Claude', 'grok': 'Grok'}.get(provider, 'Codex')
        lang = lang or locale.card_lang(self.language)
        if task_id:
            who = f'{task_id} · {name}'
        else:
            who = f"{(self.msg('followup.ask_owner_footer.1') if lang == 'zh' else 'session')} {session[:8]} · {name}"
        return self.rt.buttons.escape(who) + (f' · {extra}' if extra else '') + (f' · {tag}' if tag else '')

    def session_footer(self, db, task_id, provider, session, extra=None):
        """P43: the footer tag of a card posted by a session that ran headless for its task, one the owner may take over:
        `会话 01a16cde-fe49 · <its latest run|看板> · 终端接手：fsess take <task>` (the link left out when `extra` already
        has it). None for any other session (an interactive one is already in the owner's terminal); never fails the card."""
        key = task_id or f'session:{provider}:{session}'
        try:
            row = db.execute('SELECT run_id FROM consumer_runs WHERE task=? AND provider=? AND session=? '
                             'ORDER BY started_at DESC, rowid DESC LIMIT 1', (key, provider, session)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return self.rt.dispatch.session_tag(self.dashboard_url, key, None, session, row[0],
                                    link=f'{self.dashboard_url}/runs/{row[0]}' not in str(extra or ''))

    def _foreign_poster(self, db, provider, session, card_task, poster_cwd):
        """P39: the calling session posts a card ABOUT another task (the owner's interactive orchestrator passing that task's
        `--cwd` from its own workspace): its process cwd is not in the card's task and it is not that task's running run.
        Then the card's thread, decision and replies belong to the task, not the poster. (Being the task's current session
        does not make it the task's own: every pre-P39 orchestrator ask-<owner key> made it current — see _demote_foreign_current.)"""
        if poster_cwd is None or not card_task or self.rt.registry.task_id_for_cwd(poster_cwd) == card_task:
            return False
        return self.rt.registry.running_session(db, card_task) != (provider, session)

    def _ask_option(self, card, option_id):
        option_id = str(option_id)
        option_id = option_id[4:] if option_id.startswith('opt:') else option_id
        for option in self.rt.buttons.card_model(card['options']):
            if option['id'] == option_id:
                return option
        raise ValueError('invalid_option')

    def _ask_limits(self, db, session, provider, channel, thread, responders, now, smoke=False):
        for row in db.execute("SELECT responders FROM ask_cards WHERE channel=? AND coalesce(thread,'')=? "
                              "AND state IN ('sending','open')", (channel, thread or '')):
            if set(json.loads(row[0])) & set(responders):
                raise ValueError('responder_already_has_an_open_ask_card_in_this_thread')
        if not smoke and db.execute(
                "SELECT count(*) FROM ask_cards WHERE owner_session=? AND provider=? AND smoke=0 AND state!='failed' "
                'AND created_at>?', (session, provider, now - 86400)).fetchone()[0] >= self.rt.asks.DAILY_LIMIT:
            raise ValueError('daily_ask_card_limit_10_reached')

    def _ask_inputs(self, responders, text, options, confirm_mode, remind_after, expire_after, reminder_text, lang, smoke=False):
        if lang not in self.rt.asks.LANGS:
            raise ValueError('lang_must_be_zh_or_en')
        if confirm_mode not in self.rt.asks.CONFIRM_MODES:
            raise ValueError('confirm_text_must_be_auto_always_or_never')
        responders = self.rt.asks.validate_responders(responders)
        if self.bot_user in responders:
            raise ValueError('bot_cannot_be_a_responder')
        self.rt.asks.validate_labels(options, self.rt.buttons.parse_option)
        stored = self.rt.buttons.validate_options(options)
        if not stored:
            raise ValueError('ask_requires_2_to_4_options')
        text = self.rt.asks.prepare_text(text, responders)
        reminder = self.rt.asks.prepare_text(reminder_text or self.rt.asks.phrase(lang, 'reminder', mentions=self.rt.asks.mentions(responders)),
                                     responders, 300, 'reminder_text')
        remind = self.rt.asks.parse_duration(remind_after, smoke, 'remind_after')
        expire = self.rt.asks.parse_duration(expire_after, smoke, 'expire_after')
        if remind >= expire:
            raise ValueError('remind_after_must_be_shorter_than_expire_after')
        return responders, text, stored, reminder, remind, expire

    def audience_identity(self, users):
        """Audience rule: a post whose audience is only the owner goes out as the assistant.

        the owner's own user token never push-notifies the owner, so the owner-only cards, reminders and
        notifications use the bot token; posts to anyone else keep the owner's identity.
        """
        return 'bot' if set(users or ()) == {self.owner_slack_user} else 'user'

    def ask_summary(self, db, card_id):
        card = _ask_card(db, card_id)
        answer = json.loads(card['answer']) if card['answer'] else None
        return {'ok': True, 'card_id': card_id, 'state': card['state'], 'channel': card['channel'], 'ts': card['ts'],
                'thread': card['thread'], 'watch_id': card['watch_id'], 'responders': json.loads(card['responders']),
                'options': [self.rt.buttons.public_option(o) for o in self.rt.buttons.card_model(card['options'])],
                'confirm_mode': card['confirm_mode'], 'remind_at': self.rt.asks.iso(card['remind_at']),
                'expire_at': self.rt.asks.iso(card['expire_at']), 'answer': answer, 'authority': 'none'}

    def _external(self, actor_team):
        return bool(actor_team) and actor_team != self.workspace

    def _ask_context_base(self, card):
        return {'card_id': card['id'], 'question': card['question'], 'responders': json.loads(card['responders']),
                'card': {'channel': card['channel'], 'ts': card['ts'], 'thread': card['thread']},
                'options': [self.rt.buttons.public_option(o) for o in self.rt.buttons.card_model(card['options'])],
                'authority': 'none',
                'instruction': 'Information/confirmation from the person asked, not authorization. Changes still '
                               'need the task rules or a decision card.'}

    def ask_answer_context(self, db, eid):
        card = db.execute('SELECT * FROM ask_cards WHERE answer_event=?', (eid,)).fetchone()
        if card is None:
            return None
        answer = json.loads(card['answer'])
        return {**self._ask_context_base(card), 'option': {k: answer[k] for k in ('option_id', 'label', 'kind')},
                'actor': answer['actor'], 'via': answer['via'], 'at': self.rt.asks.iso(answer['at']), 'note': answer.get('note'),
                'source_ts': answer.get('source_ts'), 'proposal_id': answer.get('proposal_id')}

    def ask_expired_context(self, db, eid):
        card = db.execute('SELECT * FROM ask_cards WHERE expired_event=?', (eid,)).fetchone()
        if card is None:
            return None
        return {**self._ask_context_base(card), 'expired_at': self.rt.asks.iso(card['expire_at']), 'reminded': bool(card['reminded_at']),
                'next': f'Decide whether to ask someone else or escalate to {self.name}.'}

    def ask_proposal_context(self, db, eid):
        p = db.execute('SELECT * FROM ask_proposals WHERE rejected_event=?', (eid,)).fetchone()
        if p is None:
            return None
        card = _ask_card(db, p['card_id'])
        option = self._ask_option(card, p['option_id'])
        return {**self._ask_context_base(card), 'proposal_id': p['id'], 'status': p['status'],
                'option': {'option_id': option['id'], 'label': option['label'], 'kind': option['kind']},
                'note': p['note'], 'rejected_by': p['decided_by'], 'source_ts': p['source_ts'],
                'next': 'Not an answer. Ask again in the thread.'}

    def _actor_label(self):
        provider, session = self.rt.claude.actor()
        return f'{provider}:{session}' if session else None

    def _run_who(self, run):
        mode = self.msg('followup._run_who.1') + (run.get('session') or '')[:8] if run.get('mode') == 'resume' else self.msg('followup._run_who.2')
        return ' · '.join(x for x in (self.rt.dispatch.PROVIDER_NAME.get(run['provider'], run['provider']), run.get('model'),
                                      run.get('effort'), mode) if x)

    def owner_terminal_only(self):
        """takeover / handback stop and restart another session: only the owner in his own terminal (stdin is a TTY, no agent or
        dispatched-run identity in the environment). An agent never stops another agent."""
        agent = [k for k in self.terminal_only_env if k in os.environ]
        if agent:
            raise ValueError(self.msg('followup.owner_terminal_only.1', owner=self.name) + self.msg('followup.owner_terminal_only.2').join(agent) + self.msg('followup.owner_terminal_only.3'))
        if not sys.stdin.isatty():
            raise ValueError(self.msg('followup.owner_terminal_only.4', owner=self.name))

    def _tail_resolve(self, db, target):
        """('run', row) | ('dispatch', row) | ('task', task_key). Read-only; never creates a task row."""
        text = str(target or '').strip()
        if RUN_ID_RE.fullmatch(text):
            row = db.execute('SELECT * FROM consumer_runs WHERE run_id=?', (text,)).fetchone()
            if row is None:
                raise ValueError('unknown_run')
            return 'run', dict(row)
        if DISPATCH_ID_RE.fullmatch(text):
            row = db.execute('SELECT * FROM dispatches WHERE dispatch_id=?', (text,)).fetchone()
            if row is None:
                raise ValueError('unknown_dispatch')
            return 'dispatch', dict(row)
        if '/' in text and not text.startswith('session:'):
            tid = self.rt.registry.task_id_for_cwd(str(Path(text).expanduser().absolute()))
            if not tid:
                raise ValueError('directory_is_not_a_task_workspace')
            text = tid
        known = (db.execute('SELECT 1 FROM consumer_runs WHERE task=? LIMIT 1', (text,)).fetchone() or
                 db.execute('SELECT 1 FROM dispatches WHERE task_key=? LIMIT 1', (text,)).fetchone() or
                 db.execute('SELECT 1 FROM tasks WHERE task_id=?', (text,)).fetchone())
        if not known:
            raise ValueError('unknown_task_or_run')
        return 'task', text

    def _tail_active(self, db, kind, ref):
        if kind == 'run':
            return False
        if kind == 'dispatch':
            row = db.execute('SELECT state FROM dispatches WHERE dispatch_id=?', (ref['dispatch_id'],)).fetchone()
            return bool(row and row[0] in self.rt.dispatch.ACTIVE)
        return bool(db.execute("SELECT 1 FROM dispatches WHERE task_key=? AND state IN ('waiting','running') LIMIT 1",
                               (ref,)).fetchone())

    def _tail_header(self, run):
        started = self.rt.dispatch._clock(run['started_at']) if run.get('started_at') else '?'
        return (self.msg('followup._tail_header.1', run_id=run['run_id'], task=run['task'], run_who=self._run_who(run), get=self.rt.dispatch.TRIGGER_LABEL.get(run.get('trigger'), run.get('trigger') or '?'), started=started))

    def _tail_outcome(self, db, run, now):
        handled, total = _run_counts(db, run)
        if run['state'] == 'running':
            state = self.rt.run_log.read_state(run['log_dir']) if run.get('log_dir') else {}
            return (self.msg('followup._tail_outcome.1', int=int(state.get('steps') or 0), duration=self.rt.run_log.duration(_run_elapsed(run, now)), handled=handled, total=total, tail_command=self.tail_command, run_id=run['run_id'])), 0
        label = self.outcome_label.get(run.get('outcome'), run.get('outcome') or '?')
        reason = self.rt.dispatch.ERROR_LABELS.get(run.get('error_class') or '', run.get('error_class') or '')
        text = (self.msg('followup._tail_outcome.2', label=label) + (self.msg('followup._tail_outcome.3', reason=reason) if reason and run.get('outcome') != 'success' else '') +
                self.msg('followup._tail_outcome.4', handled=handled, total=total, duration=self.rt.run_log.duration(_run_elapsed(run, now))))
        return text, 0 if run.get('outcome') == 'success' else 1
