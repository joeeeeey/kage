"""Loop (K14c): the supervisor's own loop — watches (register, rearm, migrations, the due set), Slack reading
(identity, paged and incremental reads with the read cache and rate limits, the exact source of a thread, an
event's bounded context), ingest into events with envelopes and read receipts, delivery (the Claude outbox, the
Codex queue, the dispatcher's route), resolve, recovery of a dead Codex owner, the daemon's health step,
run_loop and its start. Everything else is reached through the runtime (`rt`).
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
import urllib.error

from kage import locale
from kage.core import control as _control
from kage.core import mode as _mode
from kage.core import system_status as _system

_OWNER_DEFAULT = object()  # a parameter whose default is the owner's value, filled in at call time


MAX_TICKS_PER_CYCLE = 5


def _meta_note(db, key, text):
    """Record a short, secret-free status code with its time (same rules as _meta_error)."""
    if db.in_transaction:
        return
    try:
        db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (key, f'{time.time():.0f} {text}'))
        db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _meta_clear(db, key):
    """Drop a recorded failure once the step it describes succeeded again (same rules as _meta_note)."""
    if db.in_transaction:
        return
    try:
        if db.execute('SELECT 1 FROM runtime_meta WHERE key=?', (key,)).fetchone():
            db.execute('DELETE FROM runtime_meta WHERE key=?', (key,))
            db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def save(db, w):
    db.execute('INSERT OR REPLACE INTO watches VALUES (?,?)',
               (w['id'], json.dumps(w, ensure_ascii=False)))


ROUTED_CONTEXT_LIMIT = 20   # P38: thread messages from the routed card up to the owner's reply (event_context)


def due_watches(db, now, limit=MAX_TICKS_PER_CYCLE):
    """Only due I/O or unsent outbox work enters tick; idle watches incur no writes."""
    rows = db.execute("""SELECT data FROM watches WHERE
        (json_extract(data,'$.state')='running' AND json_extract(data,'$.next_poll')<=?)
        OR (json_extract(data,'$.state')!='stopped' AND id IN
            (SELECT watch_id FROM events WHERE status='pending'))
        ORDER BY json_extract(data,'$.next_poll'),id LIMIT ?""", (now,limit)).fetchall()
    return [json.loads(r[0]) for r in rows]


CONTEXTUAL_EMOJI = {'approve': 'white_check_mark', 'reject': 'x'}


SEEN_EMOJI = 'eyes'                 # control plane: "I read your message" (the owner's card-thread / decision-channel message)


REPLIED_EMOJI = 'white_check_mark'  # conversation plane: "handled / replied" (placed when the agent answers as the owner)


def pid_birth(pid):
    if not pid or pid <= 1:
        return None
    r = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True)
    return r.stdout.strip() or None


def discover_owner(session):
    if os.environ.get('CODEX_THREAD_ID') != session:
        return None
    pid = os.getppid()
    for _ in range(10):
        r = subprocess.run(['ps', '-p', str(pid), '-o', 'ppid=,comm='], capture_output=True, text=True)
        if not r.stdout.strip():
            return None
        parent, comm = r.stdout.strip().split(None, 1)
        if Path(comm).name == 'codex':
            # Shared app-server / remote frontend liveness isn't thread liveness.
            args = subprocess.run(['ps', '-p', str(pid), '-o', 'args='], capture_output=True, text=True).stdout
            try:
                words = shlex.split(args)
            except ValueError:  # B1: ps joins argv with spaces, so a quote in the prompt can be left unbalanced
                words = args.split()
            prefix = words[:8]
            if any(a in ['app-server', 'exec-server', '--remote'] for a in prefix):
                return None
            return pid
        pid = int(parent)
        if pid <= 1:
            return None
    return None


HEALTH_SECONDS = 30  # P38: stranded-delivery sweep, stuck-claim scan and provider-health check cadence


KEEP_WARM_CHECK_SECONDS = 60  # P40.1: how often the supervisor asks whether an intake prompt prefix needs a refresh


def _ledger(db, channel, ts, kind, ref=None, session=None, mentions=None):
    """Inside a transaction: remember a runtime post so the poller never ingests it. P46.3: `mentions` = whom a post as
    the owner to people @-mentions and the task it speaks for (schedule.mentions; ids only, never the text)."""
    if channel and ts:
        db.execute('INSERT OR IGNORE INTO agent_sends (channel,ts,kind,ref,session,created,mentions) VALUES '
                   '(?,?,?,?,?,?,?)', (channel, ts, kind, ref, session, time.time(), mentions))


class LoopCore:
    """The owner-bound half of the loop: the owner's Slack user, bot and workspace, the deployment's read cache,
    rate limits, identity cache and provider emoji (its objects), the autostart guard's env name, and the
    runtime (`rt`) every name it does not hold is reached through — the names the tests replace, too."""

    def __init__(self, language, *, owner_key, owner_name, workspace, owner_slack_user, bot_user, read_cache, rate_limits, identity_cache, receipt_emoji, no_autostart_env, rt):
        self.language = language
        self.workspace = workspace
        self.owner_slack_user = owner_slack_user
        self.bot_user = bot_user
        self.read_cache = read_cache
        self.rate_limits = rate_limits
        self.identity_cache = identity_cache
        self.receipt_emoji = receipt_emoji
        self.no_autostart_env = no_autostart_env
        self.rt = rt
        self.key, self.name = owner_key, owner_name
        self.control = _control.Control(language, owner_key=owner_key, owner_name=owner_name)  # Phase 2 C1
        self.start_cards = _mode.StartCards(language, owner_key=owner_key, owner_name=owner_name)  # O2d
        self.system = _system.Monitor(language)  # 0.78.6 (F18)

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def migrate_p0(self, db, now=None):
        """P0 schema: new tables, two nullable ask columns, one-time task backfill.

        Never rewrites watches, events or any existing row. The backfill is guarded by a
        runtime_meta marker and its failure is recorded, never raised: connect() must
        keep working for delivery even if the registry cannot be built.
        """
        self.rt.registry.initialize(db)
        self.rt.envelopes.initialize(db)
        # Which identity posted an ask card / typed-answer confirmation, so later
        # chat.update calls use the same token. NULL = legacy row = the owner's user token.
        self.rt._add_column(db, 'ask_cards', 'post_identity', 'TEXT')
        self.rt._add_column(db, 'ask_proposals', 'post_identity', 'TEXT')
        db.commit()
        if db.execute('SELECT 1 FROM runtime_meta WHERE key=?', (self.rt.registry.BACKFILL_KEY,)).fetchone():
            return None
        try:
            with self.rt.transaction(db):
                if db.execute('SELECT 1 FROM runtime_meta WHERE key=?', (self.rt.registry.BACKFILL_KEY,)).fetchone():
                    return None
                report = self.rt.registry.backfill(db, now)
                db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (self.rt.registry.BACKFILL_KEY, json.dumps(
                    {k: report[k] for k in ('tasks_created', 'current_filled', 'watches_bound',
                                            'watches_unbound_per_session', 'multi_session_tasks')} | {'at': time.time()})))
            return report
        except Exception as exc:  # noqa: BLE001 - registry is shadow-only in P0
            self._meta_error(db, 'tasks_backfill_error', exc)
            return None

    def _meta_error(self, db, key, exc):
        """Record a shadow-feature failure; never touches a caller's open transaction."""
        _meta_note(db, key, self.rt.safe_error(exc))

    def _registry_hook(self, db, fn, *args):
        """Shadow-only registry update after the main operation committed; never raises."""
        try:
            with self.rt.transaction(db):
                return fn(db, *args)
        except Exception as exc:  # noqa: BLE001
            self._meta_error(db, 'task_registry_error', exc)
            return None

    def register(self, db, *, session, cwd, channel, thread, peers, mode='waiting',
                 since=None, dm=None, owner_pid=None, owner_birth=None, now=None,
                 duration_seconds=None, lifecycle='idle', scope='thread', context_all=False, provider='codex',
                 registry_note=True):
        uuid.UUID(session)
        self.rt.claude.provider({'provider': provider})
        cwd = str(Path(cwd).expanduser().absolute())
        if not Path(cwd).is_dir():
            raise ValueError('task_cwd_missing')
        if mode not in self.rt.MODES or not re.fullmatch(r'[CDG][A-Z0-9]+', channel):
            raise ValueError('invalid_mode_or_channel')
        if thread and not re.fullmatch(r'\d+\.\d+', thread):
            raise ValueError('invalid_thread')
        if scope not in ('thread', 'channel') or lifecycle not in ('idle', 'until_stop', 'sliding'):
            raise ValueError('invalid_scope_or_lifecycle')
        if scope == 'channel' and (thread or dm or mode == 'decision' or channel.startswith('D')):
            raise ValueError('channel_scope_requires_task_channel_without_thread_or_dm')
        if not thread and not channel.startswith('D') and scope != 'channel':
            raise ValueError('channel_watch_requires_exact_thread')
        if mode == 'decision' and (channel not in self.rt.buttons.decision_channels(self.rt.buttons.load_config()) or peers != [self.owner_slack_user] or dm or not thread):
            raise ValueError(f'decision_watch_must_target_{self.key}_only_in_escalation_thread')
        if not peers and mode != 'observe':
            raise ValueError('waiting_watch_requires_exact_peer_ids')
        if dm and not re.fullmatch(r'D[A-Z0-9]+', dm):
            raise ValueError('invalid_dm')
        if any(not re.fullmatch(r'U[A-Z0-9]+', p) for p in peers):
            raise ValueError('invalid_peer')
        if duration_seconds is not None and (not isinstance(duration_seconds, int) or duration_seconds <= 0):
            raise ValueError('duration_seconds_must_be_a_positive_integer')
        now = time.time() if now is None else now
        # Same source+owner has one watcher. Repeated registration is not activity.
        source_key = f'{session}/{channel}/{thread or ("channel" if scope == "channel" else "dm")}'
        # Keep legacy Codex IDs; Claude occupies a distinct namespace. Handoff retains IDs.
        wid = hashlib.sha256((source_key if provider == 'codex' else provider + '/' + source_key).encode()).hexdigest()[:20]
        with self.rt.transaction(db):
            existing = db.execute("SELECT data FROM watches WHERE coalesce(json_extract(data,'$.provider'),'codex')=? AND json_extract(data,'$.session')=? AND json_extract(data,'$.channel')=? AND json_extract(data,'$.thread') IS ? AND coalesce(json_extract(data,'$.scope'),'thread')=?", (provider,session,channel,thread,scope)).fetchone()
            if existing is None:
                existing = db.execute('SELECT data FROM watches WHERE id=?', (wid,)).fetchone()
            if existing:
                old = json.loads(existing['data'])
                if self.rt.claude.provider(old) != provider or old['session'] != session or (old['peers'], old['dm'], old['cwd']) != (peers, dm, cwd):
                    raise ValueError('existing_watch_binding_differs_use_explicit_handoff')
                if duration_seconds is not None and old.get('duration_seconds') != duration_seconds:
                    raise ValueError('existing_watch_duration_differs_use_explicit_rearm')
                return old
            deadline = (now + duration_seconds if duration_seconds is not None else
                        now + self.rt.IDLE_SECONDS if lifecycle == 'idle' else
                        now + self.rt.MODES[mode][0] if lifecycle == 'sliding' else None)
            w = dict(id=wid, session=session, provider=provider, cwd=cwd, channel=channel, thread=thread,
                     peers=peers, dm=dm, mode=mode, lifecycle='fixed' if duration_seconds else lifecycle,
                     scope=scope, context_all=context_all or scope == 'channel', kind='decision' if mode == 'decision' else 'peer', state='running', started=now,
                     since=str(since if since is not None else now), deadline=deadline,
                     next_poll=self.rt.clamp_poll(now+self.rt.MODES[mode][1][0], deadline), step=0, polls=0,
                     owner_pid=owner_pid, owner_birth=owner_birth, last_error=None,
                     cadence_activity_ts=now, cadence_version=self.rt.schedule.VERSION)
            if lifecycle == 'idle' and duration_seconds is None:
                w.update(inactivity_seconds=self.rt.IDLE_SECONDS, last_activity=now)
            if duration_seconds is not None:
                w.update(duration_seconds=duration_seconds, fixed_deadline=deadline)
            save(db, w)
        if registry_note:
            # Task = workspace derived from cwd (never stored in the watch); root/outside cwd stays per-session.
            self._registry_hook(db, self.rt.registry.note_success, provider, session, cwd, now, 'register')
        return w

    def api_identity(self, bot=False):
        """The Slack token for a call: the owner's user token, or the bot's (`bot`). 0.77.0: a bot-only deployment
        (`intake.identity: bot`) has no user token; every call then uses the bot token."""
        bot = bot or self.bot_only()
        cached = self.identity_cache.get(bot)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        token = self.rt.slack.default_bot_token() if bot else self.rt.slack.default_user_token()
        result = self.rt.slack.api_call('auth.test', token)
        if result.get('team_id') != self.workspace or result.get('user_id') != (self.bot_user if bot else self.owner_slack_user):
            raise RuntimeError('unexpected_slack_identity')
        if not bot and result.get('bot_id'):
            raise RuntimeError('expected_human_user_token')
        self.identity_cache[bot] = (time.time(), token)
        return token

    def bot_only(self):
        cached = getattr(self, '_bot_only', None)
        if cached and time.time() - cached[0] < 30:
            return cached[1]
        try:
            value = self.rt.worker_config.load()[0]['intake'].get('identity') == 'bot'
        except Exception:  # noqa: BLE001 - no worker config (yet): the owner's token, as before
            value = False
        self._bot_only = (time.time(), value)
        return value

    def pages(self, token, endpoint, params):
        cursor = None
        for _ in range(50):
            response = self.rt.slack.api_call(endpoint, token, params={**params, **({'cursor': cursor} if cursor else {})})
            yield from response.get('messages', [])
            cursor = response.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                if response.get('has_more'):
                    raise RuntimeError('incomplete_slack_pagination')
                return
        raise RuntimeError('slack_pagination_limit')

    def read_api(self, endpoint, token, params):
        until = self.rate_limits.get(endpoint, 0)
        if time.time() < until:
            raise urllib.error.HTTPError('slack', 429, 'rate limited',
                                         {'Retry-After': str(until-time.time())}, None)
        key = (endpoint, json.dumps(params, sort_keys=True))
        cached = self.read_cache.get(key)
        if cached and time.time() - cached[0] < 30:
            return cached[1]
        try:
            result = self.rt.slack.api_call(endpoint, token, params=params)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    delay = max(1, float(exc.headers.get('Retry-After', 60)))
                except (TypeError, ValueError):
                    delay = 60
                self.rate_limits[endpoint] = time.time() + delay
            raise
        if len(self.read_cache) >= 128:
            self.read_cache.clear()
        self.read_cache[key] = (time.time(), result)
        return result

    def incremental(self, w, token, endpoint, channel, root=None):
        """Two pages per stream; persist the frozen window until pagination completes."""
        states = w.setdefault('fetch_state', {}).setdefault('streams', {})
        key = f'{channel}/{root or "history"}'
        state = states.setdefault(key, {'oldest': w['since']})
        messages = []
        for _ in range(2):
            # include_all_metadata: our hidden agent-sent marker must be visible to
            # the envelope (agent-before-the owner classification and self-wake suppression).
            params = {'channel': channel, 'oldest': state['oldest'], 'limit': 100, 'include_all_metadata': 'true'}
            if w.get('lifecycle') == 'idle':
                params['latest'] = str(w['deadline'])
            if root:
                params['ts'] = root
            if state.get('cursor'):
                params['cursor'] = state['cursor']
            response = self.rt.read_api(endpoint, token, params)
            batch = response.get('messages', [])
            messages.extend((channel, m) for m in batch)
            high = max([state.get('high', state['oldest'])] + [m['ts'] for m in batch], key=float)
            cursor = response.get('response_metadata', {}).get('next_cursor')
            if response.get('has_more') and not cursor:
                raise RuntimeError('incomplete_slack_pagination')
            if cursor:
                state.update(cursor=cursor, high=high)
            else:
                state.update(oldest=high)
                state.pop('cursor', None)
                state.pop('high', None)
                break
        return messages

    def fetch(self, w, token):
        messages = []
        if w.get('dry'):
            return messages  # a dry TEST watch (smoke card synthesized, never posted) has no real thread to poll
        if w.get('scope') == 'channel':
            messages.extend(self.incremental(w, token, 'conversations.history', w['channel']))
            state = w.setdefault('fetch_state', {})
            roots = state.setdefault('roots', [])
            root_deadlines = state.setdefault('root_deadlines', {})
            for root in roots:
                root_deadlines.setdefault(root, time.time()+self.rt.IDLE_SECONDS)
            excluded = set(w.get('covered_threads', [])) | set(w.get('_covered_threads', []))
            roots[:] = [root for root in roots if root not in excluded]
            for _, m in messages:
                root = m.get('thread_ts') or m.get('ts')
                # All ordinary humans can supply context, independently of expected peers.
                if (root and float(root) > float(w.get('since', 0)) and root not in roots and root not in excluded and
                        not m.get('bot_id') and m.get('subtype') in (None, 'file_share')):
                    if len(roots) >= 500:
                        raise ValueError('channel_thread_capacity_500_requires_scope_review')
                    roots.append(root)
                    root_deadlines[root] = time.time()+self.rt.IDLE_SECONDS
            offset = state.get('offset', 0) % max(1, len(roots))
            selected = (roots[offset:] + roots[:offset])[:5]
            for root in selected:
                replies = self.incremental(w, token, 'conversations.replies', w['channel'], root)
                deadline = root_deadlines[root]
                for channel, message in sorted(replies, key=lambda item: float(item[1].get('ts', 0))):
                    sent = float(message.get('ts', 0))
                    if w.get('lifecycle') == 'idle' and sent > deadline:
                        continue
                    if self.rt.avatar_cache.valid_human(message) and sent <= deadline:
                        deadline = max(deadline, sent+self.rt.IDLE_SECONDS)
                    messages.append((channel, message))
                root_deadlines[root] = deadline
                stream = state.get('streams', {}).get(f"{w['channel']}/{root}", {})
                if w.get('lifecycle') == 'idle' and time.time() >= deadline and not stream.get('cursor'):
                    roots.remove(root)
                    root_deadlines.pop(root, None)
                    state.get('streams', {}).pop(f"{w['channel']}/{root}", None)
            state['offset'] = (offset + len(selected)) % max(1, len(roots))
        else:
            if w['thread']:
                messages.extend(self.incremental(w, token, 'conversations.replies', w['channel'], w['thread']))
            for channel in set(filter(None, [w['dm'], w['channel'] if not w['thread'] else None])):
                messages.extend(self.incremental(w, token, 'conversations.history', channel))
        return messages

    def continue_until_stop(self, db, wid, expected_session, context_all=False):
        """Explicit scoped migration; preserve owner, source cursor and every event."""
        with self.rt.transaction(db):
            w = self.rt.load(db, wid)
            if w['session'] != expected_session:
                raise ValueError('unexpected_watch_owner')
            if w['state'] == 'stopped':
                raise ValueError('explicit_stop_is_sticky_use_authorized_rearm')
            w.update(lifecycle='until_stop', deadline=None, state='running',
                     next_poll=min(w['next_poll'], time.time()+60))
            w.pop('fixed_deadline', None)
            w.pop('duration_seconds', None)
            w.pop('stop_reason', None)
            if context_all and w.get('kind') != 'decision':
                w['context_all'] = True
            save(db, w)
        return w

    def event_context(self, db, eid, token, now=None):
        """Age-sensitive bounded reads, returned transiently with source attribution."""
        e = db.execute('SELECT * FROM events WHERE id=?', (eid,)).fetchone()
        if e is None:
            raise ValueError('unknown_event')
        w = self.rt.load(db, e['watch_id'])
        self.rt.require_event_owner(db, e, w)
        now = time.time() if now is None else now
        local = self.rt.local_message(db, eid)
        if local is not None:
            return self.rt.local_event_context(db, e, w, local, now)
        age = max(0, now-float(e['ts']))
        after = 5 if age <= 300 else 15 if age <= 3600 else 30
        before = 3 if age <= 300 else 5 if age <= 3600 else 10
        # history can resolve channel roots and broadcasts; exact-thread events have
        # their registered source and do not depend on history returning replies.
        source_ref = db.execute('SELECT root FROM event_sources WHERE event_id=?', (eid,)).fetchone()
        root = source_ref[0] if source_ref else w['thread'] if e['channel'] == w['channel'] else None
        source_result = self.rt.read_api('conversations.replies' if root else 'conversations.history', token,
                                {'channel': e['channel'], **({'ts': root} if root else {}),
                                 'oldest': e['ts'], 'latest': e['ts'], 'inclusive': True, 'limit': 1})
        source = next((m for m in source_result.get('messages', []) if m.get('ts') == e['ts']), None)
        fallback = None
        if source is None and root:
            source, fallback = self._source_under_card_root(db, e, root, token)
        if source is None:
            raise ValueError('exact_context_source_not_found')
        root = source.get('thread_ts') or source['ts']
        output = []
        def add(messages, origin):
            for m in messages:
                if not m.get('ts'):
                    continue
                output.append({'channel': e['channel'], 'root': m.get('thread_ts') or m['ts'],
                               'origin': origin, 'ts': m['ts'], 'author': m.get('user'),
                               'bot': bool(m.get('bot_id')), 'text': m.get('text', ''),
                               'files': [x.get('id') for x in m.get('files', [])]})
        add([source], 'source')
        root_response = self.rt.read_api('conversations.replies', token,
                                {'channel': e['channel'], 'ts': root, 'oldest': root,
                                 'latest': root, 'inclusive': True, 'limit': 1})
        add(root_response.get('messages', [])[:1], 'root')
        prior = self.rt.read_api('conversations.history', token,
                         {'channel': e['channel'], 'latest': root, 'limit': before})
        add(prior.get('messages', [])[:before], 'channel_before')
        # Slack history is newest-first: bounded time window and cap are explicit;
        # the response may omit intervening messages in unusually busy channels.
        following = self.rt.read_api('conversations.history', token,
                            {'channel': e['channel'], 'oldest': root,
                             'latest': str(min(now, float(root) + (300 if age <= 300 else 3600))),
                             'limit': 100})
        subsequent = sorted(following.get('messages', []), key=lambda m: float(m['ts']))
        add(subsequent[:after], 'channel_after')
        replies = self.rt.read_api('conversations.replies', token,
                           {'channel': e['channel'], 'ts': root,
                            'oldest': e['ts'], 'inclusive': True, 'limit': after+1})
        add(replies.get('messages', [])[:after+1], 'thread_after')
        # P38 (F25): the owner's reply in an approvals thread reaches a session that has no memory of the thread (a long-lived
        # task runs every event fresh): say which card it was routed to, and read the thread from that card (else the root)
        # up to the reply — one bounded extra read.
        routed = self.rt.card_replies.routed(db, e['channel'], e['ts'])
        if routed:
            earlier = self.rt.read_api('conversations.replies', token,
                               {'channel': e['channel'], 'ts': routed['root'], 'oldest': routed['card_ts'] or routed['root'],
                                'latest': e['ts'], 'inclusive': True, 'limit': ROUTED_CONTEXT_LIMIT})
            add(earlier.get('messages', [])[:ROUTED_CONTEXT_LIMIT], 'thread_before')
        unique = {}
        for item in output:
            unique.setdefault((item['channel'], item['ts']), item)
        secret_thread = not w.get('secret_input') and self.rt._secret_thread(db, e['channel'], root)
        person = re.fullmatch(r'U[A-Z0-9]+', str(e['author'])) and e['author'] not in (self.owner_slack_user, self.bot_user)
        notes = self.rt.intake.notes_line(self.rt, e['author']) if person else None  # P47 (R-0011): background, never quoted
        # P46.3 (R-0013): a person's typed reply in the thread of this watch's ask card: the ask it may answer, and the check
        card = self.rt.intake.ask_thread_card(db, e['channel'], root, w['id']) if person and not self.rt.ask_answer_context(db, eid) \
            else None
        try:
            why = card and self.rt.intake.why_you_got_it(self.rt, db, None, self.rt.registry.task_id_for_cwd(w['cwd']),
                                                 self.rt.worker_config.load()[0]['intake'], now, card)
        except ValueError:  # an unreadable config: no hint, the context still reads
            why = None
        if w.get('secret_input') or secret_thread:
            # P3.1 blocker card waiting for a verification code: humans' text is masked here (the code reaches a command
            # only through `blocker-code`, never this output, a log or a file). P38: also for a reply that reached the task
            # another way (an approvals thread, a hand-off) in a thread hosting such a card.
            for item in unique.values():
                if not item['bot']:
                    item['text'] = self.rt.blockers.mask_codes(item['text'])
        return {'event': eid, 'source_root': root, 'age_seconds': age,
                # Code-derived envelope (null for events ingested before P0) and task binding.
                'envelope': self.rt.envelopes.lookup(db, eid), 'task_id': self.rt.registry.task_id_for_cwd(w['cwd']),
                'decision': self.rt.button_decision(db, eid), 'proposal': self.rt.rejected_proposal(db, eid),
                'ask_answer': self.rt.ask_answer_context(db, eid), 'ask_expired': self.rt.ask_expired_context(db, eid),
                'ask_proposal': self.rt.ask_proposal_context(db, eid), 'blocker': self.rt.blockers.event_context(self.rt, db, eid, w),
                'thread_reply_decision': self.rt.thread_reply_decision_of(db, eid), 'routed_card': routed,
                # P46.2: the language to reply to the author in (their messages read above; else registry / Slack tz)
                **({'reply_language': self.rt.intake.reply_line(self.rt, db, e['author'], [
                    m['text'] for m in sorted(unique.values(), key=lambda m: -float(m['ts'])) if m['author'] == e['author']])}
                   if person else {}),
                **({f'{self.key}_notes': notes} if notes else {}),
                **({'why_you_got_it': why} if why else {}),
                # P39: a card-thread reply card_threads delivered to this task's inbox: what it is and how to answer it.
                **({'instruction': self.rt.card_threads.ROUTED_INSTRUCTION} if routed and w.get('kind') == self.rt.INBOX_KIND else {}),
                **({'secret_thread': True} if secret_thread else {}),
                **({'source_fallback': fallback} if fallback else {}),
                'limits': {'before': before, 'after': after,
                           'api_calls_max': (6 if routed else 5) + (fallback['api_calls'] if fallback else 0),
                           'messages_max': 72 + (ROUTED_CONTEXT_LIMIT if routed else 0)},
                'truncated': bool(following.get('has_more') or replies.get('has_more') or (routed and earlier.get('has_more'))),
                'messages': sorted(unique.values(), key=lambda m: float(m['ts']))[:72 + (ROUTED_CONTEXT_LIMIT if routed else 0)]}

    def _source_under_card_root(self, db, e, stored_root, token):
        """B3: an event stored under a threaded card's own ts (a receipt or confirmation recorded before B3), while Slack
        threads every reply to that card under the card's thread root. Look once under the card's recorded thread root,
        else under the thread Slack reports for the stored root message. Returns (source, fallback) or (None, None); the
        stored row is not rewritten (a read path)."""
        calls, tried = 0, {stored_root}
        def look(alt):
            nonlocal calls
            calls += 1
            found = self.rt.read_api('conversations.replies', token, {
                'channel': e['channel'], 'ts': alt, 'oldest': e['ts'], 'latest': e['ts'], 'inclusive': True, 'limit': 1})
            return next((m for m in found.get('messages', []) if m.get('ts') == e['ts']), None)
        try:
            card = db.execute('SELECT c.thread_root FROM escalation_cards c JOIN escalations x ON x.event_id=c.event_id '
                              'WHERE x.thread_ts=? AND c.channel=? AND c.thread_root IS NOT NULL', (stored_root, e['channel'])).fetchone()
        except sqlite3.OperationalError:  # a store without the card tables
            card = None
        if card and card[0] not in tried:
            tried.add(card[0])
            source = look(card[0])
            if source is not None:
                return source, {'stored_root': stored_root, 'root': card[0], 'via': 'card_thread_root', 'api_calls': calls}
        calls += 1
        head = self.rt.read_api('conversations.replies', token, {'channel': e['channel'], 'ts': stored_root, 'limit': 1})
        message = next((m for m in head.get('messages', []) if m.get('ts') == stored_root), None)
        alt = message and message.get('thread_ts')
        if alt and alt not in tried:
            source = look(alt)
            if source is not None:
                return source, {'stored_root': stored_root, 'root': alt, 'via': 'slack_thread', 'api_calls': calls}
        return None, None

    def migrate_idle(self, db, now=None):
        """One explicit local policy migration, not an implicit schema upgrade."""
        now = time.time() if now is None else now
        changed = []
        with self.rt.transaction(db):
            watches = [json.loads(r[0]) for r in db.execute('SELECT data FROM watches')]
            for w in watches:
                if w['state'] != 'running' or w.get('fixed_deadline') is not None or w.get('lifecycle') == 'idle':
                    continue
                w.update(lifecycle='idle', inactivity_seconds=self.rt.IDLE_SECONDS, last_activity=now,
                         deadline=now+self.rt.IDLE_SECONDS, policy_changed_at=now)
                w.pop('stop_reason', None)
                save(db,w);changed.append(w['id'])
        return changed

    def migrate_cadence(self, db, now=None):
        """Retain lifetime/cursors; map prior activity age to the adaptive schedule."""
        now = time.time() if now is None else now
        elapsed_boundaries = []
        total = 0
        for delay in self.rt.schedule.DELAYS:
            total += delay
            elapsed_boundaries.append(total)
        changed = []
        with self.rt.transaction(db):
            for row in db.execute('SELECT data FROM watches').fetchall():
                w=json.loads(row[0])
                if w['state'] != 'running' or w.get('cadence_version') == self.rt.schedule.VERSION:
                    continue
                last = max(float(w['since']), w.get('last_activity',w['started']))
                # This migration does not invent a new message or extend expiry.
                age = max(0,now-last)
                step = min(sum(age >= boundary for boundary in elapsed_boundaries),len(self.rt.schedule.DELAYS)-1)
                w.update(step=step,cadence_activity_ts=last,cadence_version=self.rt.schedule.VERSION,
                         next_poll=min(w['next_poll'],self.rt.clamp_poll(now+self.rt.schedule.DELAYS[step],w.get('deadline'))))
                save(db,w);changed.append(w['id'])
        return changed

    def ingest(self, db, wid, messages, now, fetch_state=None):
        """New eligible messages -> pending events. Returns [(channel, ts)] of human messages that became events."""
        with self.rt.transaction(db):
            w = self.rt.load(db, wid)
            if w['state'] == 'stopped':
                return []
            if fetch_state is not None:
                w['fetch_state'] = fetch_state
            previous_activity = w.get('cadence_activity_ts', w['started'])
            scope = {}  # lazily loaded envelope inputs (decisions config, people registry)
            created = []
            responders = set()
            for row in db.execute("SELECT responders FROM ask_cards WHERE watch_id=? AND state IN ('open','answered')", (wid,)):
                responders.update(json.loads(row[0]))
            # P3.2: when a task-level approvals watch covers this decision card's thread, it is the sole ingester of the owner's
            # replies (one event, one delivery, a deterministic contextual read receipt); this per-card watch skips them.
            covered, card_root = False, None
            if w.get('kind') == 'decision':
                cr = db.execute('SELECT c.channel, c.thread_root FROM escalations x JOIN escalation_cards c '
                                'ON c.event_id=x.event_id WHERE x.watch_id=?', (wid,)).fetchone()
                covered = bool(cr and self.rt._approvals_watch_active(db, cr['channel'], cr['thread_root']))
                card_root = cr['thread_root'] if cr else None
            messages = sorted(messages, key=lambda item: float(item[1].get('ts', 0)))
            dm_top_level = w.get('kind', 'peer') == 'peer' and any(str(c).startswith('D') or self.rt.schedule.dm_like(db, c)
                                                                   for c, _ in messages) and \
                self.rt.dm_routing_active()  # P39: evaluated once per poll
            # P38: a card thread that (from before P38) also hosts a card waiting for a verification code hashes no reply
            # text — whichever watch reads it (the approvals watch, another card's own watch) — like that card's own would.
            secret = bool(w.get('secret_input') or
                          (w.get('kind') == self.rt.APPROVALS_KIND and self.rt._secret_thread(db, w['channel'], w['thread'])) or
                          (card_root and self.rt._secret_thread(db, w['channel'], card_root)))
            for channel, m in messages:
                if not self.rt.observation_in_window(w, channel, m, now, dm_top_level):
                    continue
                if channel in [w['channel'], w['dm']]:
                    self.rt.avatar_cache.observe(db, channel, [m], w['thread'] if channel == w['channel'] else None)
                extra = responders
                if w.get('kind') == self.rt.APPROVALS_KIND:  # P39 (F21): in-window OOO delegates, from the decisions config
                    extra = responders | self.rt._delegates_at(w, m.get('ts', 0), self.rt._envelope_inputs(scope))
                if channel not in [w['channel'], w['dm']] or not self.rt.eligible(w, m, extra):
                    continue
                if dm_top_level and self.rt.schedule.dm_like(db, channel) and (m.get('thread_ts') or m.get('ts')) == m.get('ts'):
                    continue  # P39 rules 2/6: intake routes a new top-level DM message (refs first, one task, else inbox)
                if covered:  # the task-level approvals watch ingests this thread's replies; the per-card watch stays idle
                    continue
                ts = m.get('ts', '')
                revision = m.get('edited', {}).get('ts', ts)
                if not re.fullmatch(r'\d+\.\d+', ts) or float(revision) <= float(w['since']):
                    continue
                # A button receipt was already delivered by `decide`; never ingest it twice.
                if db.execute('SELECT 1 FROM decisions WHERE card_channel=? AND receipt_ts=?', (channel, ts)).fetchone():
                    continue
                # Anything this runtime posted (cards, reminders, echoes) is not conversation.
                if db.execute('SELECT 1 FROM agent_sends WHERE channel=? AND ts=?', (channel, ts)).fetchone():
                    continue
                root = m.get('thread_ts') or (w['thread'] if channel == w['channel'] else None) or ts
                env = self.rt.message_envelope(w, m, root, scope)
                # Self-wake suppression: a message our agent sent (hidden metadata) never
                # becomes a deliverable event for the session or task that sent it.
                if self.rt.envelopes.self_wake(env, w, self.rt.claude.provider(w), self.rt.registry.task_id_for_cwd):
                    continue
                # Only hashes and exact references leave the transient API response. A secret-input watch (a P3.1
                # blocker card waiting for a verification code) hashes no text at all: a short code would be
                # recoverable from its digest.
                if secret:
                    digest = hashlib.sha256(json.dumps({'secret_input': True, 'ts': ts, 'revision': revision},
                                                       sort_keys=True).encode()).hexdigest()
                else:
                    digest = hashlib.sha256(json.dumps(
                        {'text': m.get('text'), 'files': [f.get('id') for f in m.get('files', [])]},
                        sort_keys=True).encode()).hexdigest()
                eid = hashlib.sha256(f'{wid}/{channel}/{ts}/{digest}'.encode()).hexdigest()[:24]
                if w.get('kind') in ('decision', self.rt.APPROVALS_KIND) and db.execute(
                        'SELECT 1 FROM events WHERE channel=? AND ts=? AND watch_id!=? LIMIT 1', (channel, ts, wid)).fetchone():
                    # P38: the owner's reply in a card thread is ONE event, whichever card-thread watch took it first (a per-card
                    # watch no longer covered once the approvals watch stopped, an approvals watch resuming from its cursor,
                    # a reply already handed off to the task's inbox) — never a second delivery to another session.
                    continue
                duplicate = db.execute('SELECT 1 FROM events e JOIN watches x ON x.id=e.watch_id '
                                       "WHERE e.channel=? AND e.ts=? AND e.digest=? AND json_extract(x.data,'$.session')=? AND coalesce(json_extract(x.data,'$.provider'),'codex')=?",
                                       (channel, ts, digest, w['session'], self.rt.claude.provider(w))).fetchone()
                if duplicate:
                    continue
                inserted = db.execute('INSERT OR IGNORE INTO events '
                                      '(id,watch_id,channel,ts,author,digest,received,status) VALUES (?,?,?,?,?,?,?,?)',
                                      (eid, wid, channel, ts, m['user'], digest, now, 'pending')).rowcount
                db.execute('INSERT OR IGNORE INTO event_sources VALUES (?,?)', (eid, root))
                self.rt.envelopes.store(db, eid, env, now)
                if inserted and env and env.get('author_kind') == 'human':
                    created.append((channel, ts))  # P3.1 read receipt: this human message entered the task's context
            # Replies fetched by dedicated thread watches also count as activity for
            # their channel watch, without a duplicate Slack request or outbox event.
            if w.get('thread') and messages:
                siblings = db.execute("SELECT data FROM watches WHERE json_extract(data,'$.session')=? AND json_extract(data,'$.channel')=? AND json_extract(data,'$.scope')='channel' AND json_extract(data,'$.state')='running' AND coalesce(json_extract(data,'$.provider'),'codex')=? ", (w['session'],w['channel'],self.rt.claude.provider(w))).fetchall()
                for row in siblings:
                    parent = json.loads(row[0]); previous = (parent['deadline'],parent.get('cadence_activity_ts'))
                    for channel, message in messages:
                        self.rt.observation_in_window(parent,channel,message,now)
                    if (parent['deadline'],parent.get('cadence_activity_ts')) != previous:
                        save(db,parent)
            w['polls'] += 1
            w['last_poll'] = now
            # An outbound hook may have reset cadence while this network poll was in flight.
            if w.get('cadence_reset_at', 0) <= now:
                if w.get('cadence_activity_ts', w['started']) == previous_activity:
                    w['step'] = min(w['step']+1, len(self.rt.schedule.DELAYS)-1)
                else:
                    w['step'] = 0
                intervals = self.rt.MODES[w['mode']][1]
                interval = intervals[min(w['step'], len(intervals)-1)]
                # P3.1: a card thread still waiting for the owner keeps a short fixed cadence instead of the ladder.
                fast = self.rt.card_replies.open_poll_seconds(self.rt, db, w)
                w['next_poll'] = self.rt.clamp_poll(now+(min(interval, fast) if fast else interval), w['deadline'])
            w['error_count'] = 0
            w['last_error'] = None
            w.pop('expiry_pending_check', None)
            save(db, w)
        return created

    def _react(self, db, refs, provider, contextual, now=None):
        """P3.3 shared core of the two read-receipt planes. For each (channel, ts) in `refs`, add two reactions on Slack —
        `contextual(ts)` first (a plain emoji name) then the consuming provider's emoji — best effort and idempotent
        (already_reacted = done). The bot reacts where it can (the decision channel, channels it is in) with a plain
        reactions.add: its scopes are not known to include the reactions:read a read-back needs. the owner's user token reacts
        through the Slack transport's add_reactions (as the escalation() provenance reaction does) in DMs, and wherever Slack
        refused the bot or the bot token is unusable. A network error after a bot add is not retried as the owner (the reaction
        may have landed), so a message never gets the emoji twice. Never raises; a 429 backs reactions.add off runtime-wide."""
        out = []
        try:
            if not refs or not self.rt.card_replies.config(self.rt)['read_receipts']:
                return out
            provider_emoji, tokens = self.receipt_emoji[self.rt.claude.provider({'provider': provider})], {}
            for channel, ts in dict.fromkeys(refs):
                if time.time() < self.rate_limits.get('reactions.add', 0):
                    out.append((channel, ts, 'rate_limited'))
                    continue
                names = [contextual(ts), provider_emoji]  # contextual first, then provider
                result = 'failed'
                for bot in ((False,) if str(channel).startswith('D') else (True, False)):  # the bot is in no DM of the owner's
                    try:
                        if bot not in tokens:
                            tokens[bot] = self.rt.api_identity(bot=bot)
                        if bot:
                            for nm in names:  # the bot has no read-back scope: add each, tolerating already_reacted
                                try:
                                    self.rt.slack.api_call('reactions.add', tokens[bot],
                                                   json_body={'channel': channel, 'timestamp': ts, 'name': nm})
                                except Exception as exc:  # noqa: BLE001
                                    if self.rt.safe_error(exc) != 'already_reacted':
                                        raise
                            result = 'bot'
                        else:
                            self.rt.slack.add_reactions(tokens[bot], channel, ts, names)  # read back, idempotent, both at once
                            result = 'user'
                    except Exception as exc:  # noqa: BLE001
                        code = self.rt.safe_error(exc)
                        if code == 'already_reacted':
                            result = 'already'
                        elif str(exc).startswith('Slack reaction readback'):
                            result = 'unverified'  # the owner's add went through; the read-back has not caught up yet
                        elif code in ('ratelimited', 'HTTP_429'):
                            self.rate_limits['reactions.add'] = time.time() + 60
                            result = 'rate_limited'
                        else:
                            result = 'failed:' + code
                            if bot and (bot not in tokens or str(exc).startswith('Slack API error:')):
                                continue  # no usable bot token, or Slack refused the bot (no reaction landed): as the owner
                    break
                if result.startswith('failed'):
                    _meta_note(db, 'read_receipt_error', result)
                out.append((channel, ts, result))
        except Exception as exc:  # noqa: BLE001 - a receipt never blocks ingest, routing or a reply
            self._meta_error(db, 'read_receipt_error', exc)
        return out

    def ingest_receipt(self, db, refs, provider, now=None, decided=None):
        """P3.3 CONTROL plane only (the owner's messages in the decision channel / card threads — a `decision` or `approvals`
        watch). At ingest, each of the owner's messages that became an event gets 👀 (seen), or ✅ / ❌ when code recorded his
        thread reply as an approve / reject card decision (`decided` = {ts: 'approve'|'reject'}), then the provider emoji —
        the receipt the owner wants ("did my message get in"). The conversation plane does NOT react at ingest (see
        reply_receipt): an incoming message from someone else is reacted to only if the agent actually replies as the owner."""
        decided = decided or {}
        return self._react(db, refs, provider, lambda ts: CONTEXTUAL_EMOJI.get(decided.get(ts), SEEN_EMOJI), now)

    def reply_receipt(self, db, refs, provider, now=None):
        """P3.3 CONVERSATION plane. Placed only when the agent actually posts a reply / ack AS THE OWNER to someone, on the
        message(s) it is replying to: ✅ (handled) then the provider emoji. If the agent merely cards the owner, logs, or the owner
        skips, the incoming message gets NO reaction — so there is never a misleading emoji to retract."""
        return self._react(db, refs, provider, lambda ts: REPLIED_EMOJI, now)

    def shadow_route(self, db, wid, event_ids, delivery):
        """Record the task-addressed target next to the actual one. Routing is unchanged; never raises."""
        if not event_ids:
            return
        try:
            self.rt.registry.record_shadow(db, self.rt.load(db, wid), event_ids, delivery)
        except Exception as exc:  # noqa: BLE001
            self._meta_error(db, 'routing_shadow_error', exc)

    def deliver(self, db, wid, db_path, runner=subprocess.run, dispatcher=None):
        # P1a: only the supervisor passes a dispatcher. A live owner (or unknown liveness) keeps
        # exactly today's delivery below; a proven-dead owner is rerouted or revived headless.
        if dispatcher is not None:
            try:
                decision = dispatcher.route(self.rt.load(db, wid))
            except Exception as exc:  # noqa: BLE001 - routing failure falls back to today's delivery
                self._meta_error(db, 'dispatch_route_error', exc)
                decision = {'action': 'as_today'}
            if decision.get('action') == 'held':
                return
        if self.rt.claude.provider(self.rt.load(db, wid)) == 'claude':
            pending = [r[0] for r in db.execute("SELECT id FROM events WHERE watch_id=? AND status='pending'", (wid,))]
            self.rt.claude.queue_watch(db, wid)
            self.rt.claude.publish_wakes(db, db_path)
            if pending:
                queued = [r[0] for r in db.execute("SELECT id FROM events WHERE watch_id=? AND status!='pending' AND id IN (%s)"
                                                    % ','.join('?' * len(pending)), (wid, *pending))]
                self.shadow_route(db, wid, queued, 'claude_outbox')
            return
        with self.rt.transaction(db):
            w = self.rt.load(db, wid)
            if w['state'] == 'stopped':
                return
            events = list(db.execute("SELECT * FROM events WHERE watch_id=? AND status='pending' ORDER BY received,id LIMIT 30", (wid,)))
            if not events:
                return
            for e in events:
                db.execute("UPDATE events SET status='sending' WHERE id=?", (e['id'],))
        # Sending state survives crashes. Never silently retry an ambiguous delivery.
        try:
            result = runner(['codex', 'queue', '--thread', w['session'], '--message', self.rt.event_prompt(w, events, db_path, db)],
                            cwd=w['cwd'], capture_output=True, text=True, timeout=25)
            receipt = re.search(r'Queued message ([0-9a-f-]+) for thread ([0-9a-f-]+)', result.stdout)
            success = result.returncode == 0 and receipt and receipt[2] == w['session']
            status = 'queued' if success else 'delivery_unknown'
            receipt_id = receipt[1] if success else None
        except (OSError, subprocess.TimeoutExpired):
            status, receipt_id = 'delivery_unknown', None
        with self.rt.transaction(db):
            for e in events:
                db.execute('UPDATE events SET status=?, receipt=? WHERE id=? AND status=\'sending\'',
                           (status, receipt_id, e['id']))
            w = self.rt.load(db, wid)
            w['last_delivery'] = status
            save(db, w)
        self.shadow_route(db, wid, [e['id'] for e in events], 'codex_' + status)

    def resolve(self, db, eid, disposition, action_ts=None, now=None):
        now = time.time() if now is None else now
        with self.rt.transaction(db):
            e = db.execute('SELECT * FROM events WHERE id=?', (eid,)).fetchone()
            if not e:
                raise ValueError('unknown_event')
            w = self.rt.load(db, e['watch_id'])
            actor_provider, actor_session = self.rt.claude.actor()
            # P38 G3: the running run of the dispatch holding this event may resolve it even after the watch moved on.
            consumer = self.rt.dispatch_consumer(db, e) if actor_session else None
            dispatched = bool(consumer and (consumer['provider'], consumer['session']) == (actor_provider, actor_session))
            if not dispatched and ((self.rt.claude.provider(w) == 'claude' and not actor_session) or (actor_session and (actor_provider, actor_session) != (self.rt.claude.provider(w), w['session']))):
                raise ValueError('watch_transferred_to_another_session_do_not_execute')
            if e['status'] == 'handled':
                return w
            if self.rt.claude.provider(w) == 'claude' and w['state'] == 'stopped' and disposition != 'irrelevant' and not dispatched:
                raise ValueError('watch_stopped_only_irrelevant_reconciliation_allowed')
            if disposition not in [*self.rt.MODES, 'irrelevant']:
                raise ValueError('invalid_disposition')
            db.execute("UPDATE events SET status='handled', disposition=?, action_ts=? WHERE id=?",
                       (disposition, action_ts, eid))
            succeeded = not w.get('smoke')
            self.rt.claude.settle_claims(db, [eid], 'resolved', now)
            if self.rt.claude.provider(w) == 'claude':
                self.rt.claude.resolved(db, w['session'])
            if disposition != 'irrelevant' and w['state'] != 'stopped' and w.get('kind') != self.rt.INBOX_KIND:
                # A decision thread always retains its owner-only author filter, even
                # when its timing downgrades to a 15-minute observation window.
                # (A local task inbox has no Slack lifecycle: it stays `inbox`, never `running`.)
                w['mode'] = ('observe' if disposition == 'observe' else
                             'decision' if w.get('kind') == 'decision' else 'waiting')
                deadline = w['deadline'] if w.get('lifecycle') == 'idle' else w.get('fixed_deadline')
                if deadline is None and w.get('lifecycle') not in ('idle', 'until_stop'):
                    deadline = now+self.rt.MODES[w['mode']][0]
                w.update(state=w['state'] if w.get('lifecycle') == 'idle' else 'expired' if deadline is not None and now >= deadline else 'running', deadline=deadline,
                         next_poll=self.rt.clamp_poll(w['next_poll'], deadline), last_relevant=now)
                save(db, w)
        if succeeded:
            # The owner just handled an event: it is the task's most recent successful session (G3: the caller — the
            # dispatch's run — in its task, not whoever the watch is bound to now).
            if dispatched:
                self._registry_hook(db, self.rt.registry.note_success, actor_provider, actor_session, consumer['task_path'], now, 'resolve')
            else:
                self._registry_hook(db, self.rt.registry.note_success, self.rt.claude.provider(w), w['session'], w['cwd'], now, 'resolve')
        return w

    def tick(self, db, wid, db_path, now=None, fetcher=_OWNER_DEFAULT, runner=subprocess.run, token=None, dispatcher=None):
        fetcher = self.fetch if fetcher is _OWNER_DEFAULT else fetcher
        now = time.time() if now is None else now
        w = self.rt.load(db, wid)
        if w.get('kind') == self.rt.APPROVALS_KIND and w.get('task_id') and w['state'] != 'stopped' and self.rt._task_thread_enabled(db):
            # P3.2: before polling, bind the approvals thread to the task's consumer (fallback / revival included). P38: the
            # same resolver as the reconcile and the dispatcher's reroute (G1); never with the kill switch off (F19).
            try:
                self.rt._ensure_approvals_watch(db, w['channel'], w['thread'], self.rt._approvals_task(w, db), now, dispatcher=dispatcher)
                w = self.rt.load(db, wid)
            except Exception as exc:  # noqa: BLE001
                self._meta_error(db, 'approvals_watch_error', exc)
        if w['state'] == 'stopped':
            return
        paired = []  # P38: the card watches of decisions the owner just typed in this approvals thread
        if w.get('kind') == self.rt.APPROVALS_KIND and w['state'] == 'running' and now >= w['next_poll'] and \
                self.rt._task_thread_switch(db) is False:
            # P38 (F19): kill switch on, stop deferred while a session still holds work here: no new polls meanwhile.
            with self.rt.transaction(db):
                current = self.rt.load(db, wid)
                current['next_poll'] = now + 60
                save(db, current)
            w = self.rt.load(db, wid)
        if w['state'] == 'running' and now >= w['next_poll']:
            try:
                if w.get('scope') == 'channel':
                    w['_covered_threads'] = [r[0] for r in db.execute("SELECT json_extract(data,'$.thread') FROM watches WHERE json_extract(data,'$.channel')=? AND json_extract(data,'$.session')=? AND json_extract(data,'$.thread') IS NOT NULL AND coalesce(json_extract(data,'$.provider'),'codex')=?", (w['channel'], w['session'], self.rt.claude.provider(w)))]
                messages = fetcher(w, token if token is not None else self.rt.api_identity())
                # A delayed final poll may see newer data beyond the watch deadline;
                # only messages sent/edited within the observation window qualify.
                messages = [(c, m) for c, m in messages
                            if w.get('lifecycle') == 'idle' or w['deadline'] is None or float(m.get('edited', {}).get('ts', m.get('ts', 0))) <= w['deadline']]
                created = self.ingest(db, wid, messages, now, w.get('fetch_state'))
                decided = {}
                try:
                    # P3.1: the owner's own reply in a decision card's thread retires its buttons; a clear 批准 / 不行 decides.
                    # P41: with cards.retire_on plan_change only a decision or a change of plan retires them.
                    # P3.2: a task-level approvals watch routes replies across every card in its thread (newest open card).
                    # Run this before the read receipt so a reply recorded as a decision gets the ✅ / ❌ contextual emoji.
                    current = self.rt.load(db, wid)
                    if current.get('kind') == self.rt.APPROVALS_KIND:
                        results = self.rt.card_replies.process_thread(self.rt, db, current, messages, now)
                    else:
                        results = self.rt.card_replies.process_watch(self.rt, db, current, messages, now)
                    decided = {r['ts']: r['kind'] for r in (results or [])
                               if r.get('action') == 'decided' and r.get('kind')}
                    if current.get('kind') == self.rt.APPROVALS_KIND:
                        paired = [x[0] for x in (db.execute('SELECT watch_id FROM escalations WHERE event_id=?',
                                                            (r['event_id'],)).fetchone() for r in results or []
                                                 if r.get('action') == 'decided' and r.get('event_id')) if x and x[0]]
                        self._settle_code_decided(db, wid, results, now)
                except Exception as exc:  # noqa: BLE001 - never blocks delivery of the reply itself
                    self._meta_error(db, 'card_replies_error', exc)
                # P3.3 CONTROL plane only: the owner's messages on a decision / approvals card thread get the ingest receipt
                # (👀 seen, or ✅ / ❌ for a reply recorded as a decision). Conversation-plane watches (DMs / channels where
                # the agent acts as the owner) do NOT react at ingest — the agent reacts (reply_receipt) only when it actually
                # answers as the owner, so an incoming message that is only carded / logged / skipped gets no misleading emoji.
                if w.get('kind') in ('decision', self.rt.APPROVALS_KIND):
                    self.ingest_receipt(db, created, self.rt.claude.provider(w), now, decided)
            except Exception as exc:
                with self.rt.transaction(db):
                    w = self.rt.load(db, wid)
                    w['last_error'] = self.rt.safe_error(exc)
                    w['error_count'] = w.get('error_count', 0) + 1
                    delay = min(1800, 60 * 2 ** min(w['error_count']-1, 5))
                    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                        try:
                            delay = max(delay, float(exc.headers.get('Retry-After', 60)))
                        except (TypeError, ValueError):
                            pass
                    w['next_poll'] = now + delay
                    save(db, w)
        current = self.rt.load(db, wid)
        outstanding = [r[0] for r in db.execute("SELECT id FROM events WHERE watch_id=? AND status!='handled' ORDER BY id", (wid,))]
        owner_gone = bool(current.get('owner_pid') and current.get('owner_birth') and
                          self.rt.pid_birth(current['owner_pid']) != current['owner_birth'])
        recovery_active = bool(current.get('recovery_pid') and current.get('recovery_birth') and
                               self.rt.pid_birth(current['recovery_pid']) == current['recovery_birth'])
        if dispatcher is None and self.rt.claude.provider(current) == 'codex' and owner_gone and outstanding and not recovery_active:
            # Legacy path (no dispatcher): only resume when process identity was proven at
            # registration. A failed attempt with the same event set requires inspection.
            # With the P1a dispatcher, rollout-file liveness replaces owner_pid (a gone TUI pid
            # proved unreliable: the shared app-server can still be the thread's active writer).
            if outstanding != current.get('recovery_events'):
                self.rt.recover(db, wid, db_path)
        else:
            self.rt.deliver(db, wid, db_path, runner, dispatcher)
            for card_wid in dict.fromkeys(paired if dispatcher is not None else ()):
                # P38 (F25): a typed decision is TWO events — the owner's reply here and the decision on the card's own watch.
                # Route the card's watch in the same pass, so a fresh-per-event task handles both in ONE dispatch.
                try:
                    self.rt.deliver(db, card_wid, db_path, runner, dispatcher)
                except Exception as exc:  # noqa: BLE001 - its own tick delivers it otherwise
                    self._meta_error(db, 'paired_decision_delivery_error', exc)
        with self.rt.transaction(db):
            w = self.rt.load(db, wid)
            catchup = any(s.get('cursor') for s in w.get('fetch_state', {}).get('streams', {}).values())
            # Failed/incomplete final reads are not proof of inactivity. Retry with backoff.
            if w.get('lifecycle') == 'idle' and (w.get('last_error') or catchup):
                w['expiry_pending_check'] = self.rt.expired(w, now)
                if catchup and self.rt.expired(w, now):
                    w['next_poll'] = max(w['next_poll'], now+60)
                save(db, w)
            elif w['state'] != 'stopped' and self.rt.expired(w, now) and (w['state'] != 'expired' or not w.get('expired_at')):
                # Stamped once, on the transition (P38: route / the stranded sweep tell later work from backlog by it).
                w['state'] = 'expired'
                w['stop_reason'] = 'fixed_deadline' if w.get('fixed_deadline') else 'inactivity_48h' if w.get('lifecycle') == 'idle' else 'inactivity_timeout'
                w['expired_at'] = now
                save(db, w)

    def _settle_code_decided(self, db, wid, results, now):
        """P39: the owner's typed decision on a CODE-owned card in an approvals thread (a routine confirmation, an orphan /
        deadline card ...) is applied by code from `decisions`: the raw reply ingested for the thread wakes no session."""
        ts = [r['ts'] for r in results or [] if r.get('action') == 'decided' and r.get('code_owned') and r.get('ts') and
              not r.get('guidance')]  # the owner wrote more than the choice: the task reads the rest
        if not ts:
            return
        with self.rt.transaction(db):
            for reply_ts in ts:
                db.execute("UPDATE events SET status='handled',disposition='decided_by_code' WHERE watch_id=? AND ts=? AND "
                           "status='pending'", (wid, reply_ts))

    def recover(self, db, wid, db_path, *, new_session=False, reason=None):
        """Explicit recovery only: uncertain delivery/liveness must not fork work."""
        w = self.rt.load(db, wid)
        if self.rt.claude.provider(w) != 'codex':
            raise ValueError('claude_auto_resume_not_supported_use_existing_session_outbox')
        if not w.get('owner_pid') or not w.get('owner_birth'):
            raise ValueError('owner_liveness_unproven_bind_verified_owner_pid_first')
        if self.rt.pid_birth(w['owner_pid']) == w['owner_birth']:
            raise ValueError('owner_still_alive_use_queue_not_resume')
        task_key = self.rt.registry.task_id_for_cwd(w['cwd']) or f'session:codex:{w["session"]}'
        lease = db.execute("SELECT * FROM consumer_leases WHERE task_key=? AND holder_kind='headless'", (task_key,)).fetchone()
        if lease and lease['pid'] and self.rt.pid_birth(lease['pid']):
            raise ValueError('dispatcher_consumer_running_for_this_task_never_two_consumers')
        if new_session and not reason:
            raise ValueError('new_session_requires_verified_history_unrecoverable_reason')
        events = list(db.execute("SELECT * FROM events WHERE watch_id=? AND status!='handled' ORDER BY id", (wid,)))
        if not events:
            raise ValueError('no_pending_events_to_recover')
        prompt = self.rt.event_prompt(w, events, db_path, db)
        if new_session:
            prompt += f'\nRecovery handoff: prior session {w["session"]} was verified unrecoverable: {reason}. Register your actual CODEX_THREAD_ID with handoff before executing task work. Preserve task cwd and approvals; do not infer authority from Slack content.\n'
            command = ['codex', 'exec', '--skip-git-repo-check', '-']
        else:
            command = ['codex', 'exec', 'resume', '--skip-git-repo-check', w['session'], '-']
        log_path = Path(db_path).parent / f'recovery-{wid}-{int(time.time())}.log'
        with self.rt.transaction(db):
            current = self.rt.load(db, wid)
            if current.get('recovery_pid') and self.rt.pid_birth(current['recovery_pid']) == current.get('recovery_birth'):
                raise ValueError('recovery_already_running')
            with log_path.open('w') as log:
                child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                                         cwd=w['cwd'], text=True, start_new_session=True)
                child.stdin.write(prompt)
                child.stdin.close()
            current.update(recovery_pid=child.pid, recovery_birth=self.rt.pid_birth(child.pid),
                           recovery_log=str(log_path), recovery_kind='new' if new_session else 'resume',
                           recovery_events=[e['id'] for e in events])
            save(db, current)
        return {'pid': child.pid, 'kind': current['recovery_kind'], 'log': str(log_path), 'cwd': w['cwd']}

    def daemon_health(self, db, dispatcher, now=None):
        """P38 supervisor step, each part isolated: C1 revive deliveries a dead session never consumed, C2 card stuck
        claims, B provider health (degrade / recover / notices), F49 re-post breaker / failure cards the destination guard
        refused once the check passes. Also the `health --sweep` CLI."""
        out = {}
        for name, step in (('stranded', lambda: dispatcher.sweep_stranded(now)),
                           ('stuck_claims', lambda: self.rt.stuck_claims.scan(self.rt, db, now)),
                           ('provider_health', lambda: self.rt.provider_health.evaluate(self.rt, db, now)),
                           ('refused_cards', lambda: dispatcher.repost_refused_cards(now))):
            try:
                out[name] = step()
            except Exception as exc:  # noqa: BLE001 - a health step never stops the loop
                self._meta_error(db, f'{name}_error', exc)
                out[name] = {'error': self.rt.safe_error(exc)}
        return out

    def run_loop(self, path):
        db = self.rt.connect(path)
        lock = Path(str(Path(path).expanduser())+'.lock').open('w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        db.execute('INSERT OR REPLACE INTO runtime VALUES (1,?,?,?)',
                   (os.getpid(), self.rt.pid_birth(os.getpid()), time.time()))
        db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', ('version', self.rt.VERSION))
        db.commit()
        next_sweep = next_settle = next_task_sweep = next_health = next_keep_warm = 0.0
        dispatcher = self.rt.make_dispatcher(db, path)
        inbox = self.rt.make_intake(db, path)
        planner = self.rt.make_routines(db, path)
        while True:
            self.rt.claude.publish_wakes(db, path)
            try:
                dispatcher.cycle()  # reap finished runs, then start waiting dispatches (cap + lease)
            except Exception as exc:  # noqa: BLE001 - the poller keeps running
                self._meta_error(db, 'dispatch_cycle_error', exc)
            try:
                inbox.step()  # P2 intake: bounded Slack reads at its own cadence, then bursts -> routes -> task events
            except Exception as exc:  # noqa: BLE001 - intake never stops delivery
                self._meta_error(db, 'intake_step_error', exc)
            try:
                # P3 routines: due routines -> routine events / fresh launches; one confirmation card per new or edited
                # hand-written definition (no model).
                planner.step()
            except Exception as exc:  # noqa: BLE001 - routines never stop delivery
                self._meta_error(db, 'routines_step_error', exc)
            try:
                # P3.2: keep each task's approvals-thread watch alive and bound to the task's consumer (P38 G1: the
                # dispatcher's resolver, so a fresh run handling a reply keeps the thread while it works).
                self.rt._reconcile_approvals_watches(db, time.time(), dispatcher=dispatcher)
            except Exception as exc:  # noqa: BLE001 - never stops delivery
                self._meta_error(db, 'approvals_watch_error', exc)
            try:
                # Phase 2 C2: the card channel's fallback poll (self-gated by control.enabled / card_channel /
                # channel_poll_seconds; no Slack call while off), then C1: the owner's control messages -> events on the
                # control task (nothing at all while the entry has never been used: its tables do not exist).
                self.control.slack_poll(self.rt, db)
                self.control.pump(self.rt, db)
            except Exception as exc:  # noqa: BLE001 - never stops delivery
                self._meta_error(db, 'control_error', exc)
            if time.time() >= next_settle:
                next_settle = time.time() + 10
                # P3: decided rule / routine proposal and routine confirmation cards are applied by code here, never by
                # the agent. P3.1: blocker choices, stage-2 [已完成] cards, 24 h reminders, card watches of resolved
                # blockers, and decided skill proposals (bootstrap + launch); the owner's thread replies on code-owned cards
                # (routine confirmations) first, so a typed 启用 settles in the same cycle. Each step fails on its own.
                # P34: task_lifecycle.settle routes the owner's click on an orphan / deadline card back (re-dispatch or record).
                # P35/P37: learning.capture records every new the owner card decision; on a strong signal it re-dispatches
                # <owner key>-inbox to DRAFT a concrete rule first (draft-before-ask). learning.settle_promote routes the owner's click
                # on a rule-distillation card (就按这条固化 -> gated rule-propose; 我改一下 -> await guidance; 只记/先不用).
                # learning.settle_redraft turns a free-text thread reply on a rule card into a redraft re-dispatch.
                # P38: stuck_claims.settle applies the owner's click on a stuck-claim card (re-queue / settle / leave).
                for settle in (self.rt.card_replies.settle, self.rt.rules.settle_proposals, self.rt.routines.settle_proposals,
                               self.rt.routines.settle_confirmations, self.rt.blockers.settle, self.rt.blockers.settle_skill_proposals,
                               self.rt.task_lifecycle.settle, self.rt.learning.capture, self.rt.learning.settle_promote, self.rt.learning.settle_redraft,
                               self.rt.stuck_claims.settle):
                    try:
                        settle(self.rt, db)
                    except Exception as exc:  # noqa: BLE001
                        self._meta_error(db, 'proposals_settle_error', exc)
                try:
                    self.control.sweep(self.rt, db)  # Phase 2 C1: working / no answer / timeout / retention
                    self.control.settle(self.rt, db)  # C1b: the owner's click on a confirm card runs the relay / launch
                except Exception as exc:  # noqa: BLE001
                    self._meta_error(db, 'control_error', exc)
                try:
                    self.start_cards.step(self.rt, db)  # O2d: observe-only mode's daily start card (one read while active)
                except Exception as exc:  # noqa: BLE001
                    self._meta_error(db, 'mode_card_error', exc)
            running = self.rt.due_watches(db, time.time())
            self.read_cache.clear()
            for w in running:
                db.execute('UPDATE runtime SET heartbeat=? WHERE id=1', (time.time(),))
                db.commit()
                try:
                    self.tick(db, w['id'], str(path), dispatcher=dispatcher)
                except Exception as exc:
                    with self.rt.transaction(db):
                        current = self.rt.load(db, w['id'])
                        current['last_error'] = self.rt.safe_error(exc)
                        save(db, current)
            if time.time() >= next_sweep:
                try:
                    self.rt.ask_sweep(db)  # one indexed query when nothing is due
                except Exception as exc:
                    # e.g. token/auth failure: back off instead of calling Slack every loop.
                    next_sweep = time.time() + 60
                    db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', ('ask_sweep_error', self.rt.safe_error(exc)))
                else:
                    _meta_clear(db, 'ask_sweep_error')  # B1: a stale error does not outlive the next good sweep
            if time.time() >= next_task_sweep:
                # P34 no-orphan sweep (may read the tracker) + deadline watchdog: off the 10 s settle cadence.
                try:
                    interval = self.rt.worker_config.load()[0]['tasks']['sweep_interval_minutes']
                except Exception:  # noqa: BLE001 - a malformed config just uses the default cadence
                    interval = self.rt.worker_config.DEFAULTS['tasks']['sweep_interval_minutes']
                next_task_sweep = time.time() + max(300, interval * 60)
                try:
                    self.rt.task_lifecycle.sweep(self.rt, db, str(path))
                except Exception as exc:  # noqa: BLE001 - the sweep never stops the loop
                    self._meta_error(db, 'task_sweep_error', exc)
            if time.time() >= next_health:
                next_health = time.time() + HEALTH_SECONDS
                self.rt.daemon_health(db, dispatcher)
                try:
                    # 0.78.6 (F18): disk and memory against system.* (self-gated by system.enabled / check_minutes; the
                    # directory sizes in a background thread), one owner notice per breach episode.
                    self.system.evaluate(self.rt, db, path, supervisor_pid=os.getpid())
                except Exception as exc:  # noqa: BLE001 - never stops the loop
                    self._meta_error(db, 'system_status_error', exc)
                try:
                    # P39: the low-frequency reconcile of card threads no live poller covers (self-gated by
                    # cards.reconcile_minutes): the owner's late replies on decided / closed / expired / code-owned cards.
                    self.rt.card_threads.sweep(self.rt, db)
                except Exception as exc:  # noqa: BLE001 - never stops the loop
                    self._meta_error(db, 'card_thread_sweep_error', exc)
                else:
                    _meta_clear(db, 'card_thread_sweep_error')  # B1
            if time.time() >= next_keep_warm:
                next_keep_warm = time.time() + KEEP_WARM_CHECK_SECONDS
                try:
                    # P40.1: refresh the cached prompt prefix of the intake tasks on Claude before the cache TTL runs out
                    # (self-gated by dispatch.lean_context.keep_warm); never an event, dispatch, card or Slack post.
                    dispatcher.keep_warm()
                except Exception as exc:  # noqa: BLE001 - never stops the loop
                    self._meta_error(db, 'keep_warm_error', exc)
                try:
                    # P44: probe a provider that dispatches wait for (one probe per provider, backoff), release them once it
                    # answers; never an event, dispatch, card or Slack post of its own.
                    dispatcher.probe_providers()
                except Exception as exc:  # noqa: BLE001 - never stops the loop
                    self._meta_error(db, 'provider_probe_error', exc)
            db.execute('UPDATE runtime SET heartbeat=? WHERE id=1', (time.time(),))
            db.commit()
            time.sleep(2)

    def start(self, path):
        # Test harnesses set this so CLI subprocesses never leave a background supervisor behind
        # (with P1a a stray supervisor would run a real dispatcher). Never set in production.
        if os.environ.get(self.no_autostart_env) == '1':
            return None
        # fcntl in the child enforces a single supervisor per database.
        db = self.rt.connect(path)
        existing = db.execute('SELECT * FROM runtime WHERE id=1').fetchone()
        if existing and existing['birth'] and self.rt.pid_birth(existing['pid']) == existing['birth']:
            db.close()
            return existing['pid']
        db.close()
        root = Path(path).expanduser().resolve().parent
        with (root/'watcher.log').open('a') as log:
            p = subprocess.Popen([sys.executable, str(Path(self.rt.__file__).resolve()), '--db', str(path), 'run'],
                                 cwd=str(self.rt.SKILL), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        return p.pid

    def rearm(self, db, wid, mode, since, now=None, duration_seconds=None, provider=None):
        now = time.time() if now is None else now
        if duration_seconds is not None and (not isinstance(duration_seconds, int) or duration_seconds <= 0):
            raise ValueError('duration_seconds_must_be_a_positive_integer')
        with self.rt.transaction(db):
            w = self.rt.load(db, wid)
            if provider is not None:
                self.rt.claude.provider({'provider': provider})
            renewing = w['state'] == 'running'
            if renewing and (w.get('fixed_deadline') is None or duration_seconds is None):
                raise ValueError('watch_already_running_resolve_new_event_instead')
            if db.execute("SELECT 1 FROM events WHERE watch_id=? AND status!='handled'", (wid,)).fetchone():
                raise ValueError('reconcile_pending_events_before_rearm')
            if renewing and str(since) != w['since']:
                raise ValueError('running_renewal_must_preserve_since')
            if not renewing and float(since) <= float(w['since']):
                raise ValueError('rearm_requires_newer_source_timestamp')
            if w.get('fixed_deadline') is not None and duration_seconds is None:
                raise ValueError('fixed_duration_rearm_requires_explicit_duration')
            if w['kind'] == 'decision' and mode == 'waiting':
                mode = 'decision'
            deadline = (now + duration_seconds if duration_seconds is not None else
                        None if w.get('lifecycle') == 'until_stop' else
                        now + self.rt.IDLE_SECONDS if w.get('lifecycle') == 'idle' else now + self.rt.MODES[mode][0])
            if renewing and deadline <= w['fixed_deadline']:
                raise ValueError('running_renewal_must_extend_deadline')
            if provider is not None:
                w['provider'] = provider
            w.update(mode=mode, state='running', since=since, step=0,
                     deadline=deadline, next_poll=self.rt.clamp_poll(now+self.rt.MODES[mode][1][0], deadline))
            w.pop('stop_reason', None)
            w.pop('fetch_state', None)
            if w.get('lifecycle') == 'idle':
                w['last_activity'] = now
            w['cadence_activity_ts'] = now
            w['cadence_version'] = self.rt.schedule.VERSION
            if duration_seconds is not None:
                w.update(lifecycle='fixed', duration_seconds=duration_seconds, fixed_deadline=deadline, rearmed_at=now)
            save(db, w)
        return w

    def verified_root(self, token, channel, source_ts):
        """Resolve the exact source through Slack, never guess from the latest DM."""
        result = self.rt.slack.api_call('conversations.replies', token,
                                params={'channel': channel, 'ts': source_ts, 'limit': 100})
        messages = result.get('messages', [])
        source = next((m for m in messages if m.get('ts') == source_ts), None)
        if source is None:
            raise ValueError('exact_source_message_not_found')
        root = source.get('thread_ts') or source['ts']
        if not re.fullmatch(r'\d+\.\d+', root):
            raise ValueError('invalid_source_thread')
        if root != source_ts:
            check = self.rt.slack.api_call('conversations.replies', token,
                                   params={'channel': channel, 'ts': root, 'limit': 1})
            if not any(m.get('ts') == root for m in check.get('messages', [])):
                raise ValueError('source_root_not_found')
        return root

    def bind_source(self, db, wid, source_ts):
        w = self.rt.load(db, wid)
        self.rt.require_owner(w)
        if w.get('scope') == 'channel':
            raise ValueError('channel_watch_sources_are_per_event')
        root = self.rt.verified_root(self.rt.api_identity(), w['channel'], source_ts)
        with self.rt.transaction(db):
            w = self.rt.load(db, wid)
            self.rt.require_owner(w)
            if w['thread'] and w['thread'] != root:
                raise ValueError('existing_source_thread_differs')
            w['thread'] = root
            save(db, w)
        return w

    def record_send(self, db, channel, ts, kind, ref=None, session=None, mentions=None):
        with self.rt.transaction(db):
            _ledger(db, channel, ts, kind, ref, session, mentions)
