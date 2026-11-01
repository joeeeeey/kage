"""Escalation (K14d): the owner's cards and decisions — the per-topic approvals threads (roots, their watches,
reconcile, retire, hand-off, inheritance), posting a card, escalation, decisions by button, by typed reply and by
confirmed proposal, receipts and the decision event, the ask-owner card, code-owned (supervisor) cards, closing,
the smokes and claim release. Everything else is reached through the runtime (`rt`).
"""
from __future__ import annotations
import functools
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from kage import locale
from kage.core import control as _control

_OWNER_DEFAULT = object()  # a parameter whose default is the owner's value, filled in at call time


def _owner_kwargs(fn):
    """A method whose keyword names carry the owner's key (`needs_<key>`) takes them under the generic name
    (`needs_owner`)."""
    @functools.wraps(fn)
    def call(self, *args, **kw):
        pattern = re.compile(rf'(?<![a-z0-9]){re.escape(self.key)}(?![a-z0-9])')
        return fn(self, *args, **{pattern.sub('owner', k): v for k, v in kw.items()})
    return call


APPROVALS_KILL_SWITCH = 'kill_switch'   # stop_reason of an approvals watch stopped by cards.task_thread=false


THREAD_RETIRE_CHECK_SECONDS = 600


CJK_RE = re.compile('[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')


class EscalationCore:
    """The owner-bound half of the owner's cards: the owner's Slack user and the bot, the deployment profile (the
    owner's time zone name), the agent marker, the card words in the owner's language, the deployment's retire
    throttle (its object) and the runtime (`rt`) every name it does not hold is reached through."""

    def __init__(self, language, *, owner_key, owner_name, owner_slack_user, bot_user, deployment, agent_event_type, retire_checked, rt):
        self.language = language
        self.owner_slack_user = owner_slack_user
        self.bot_user = bot_user
        self.deployment = deployment
        self.agent_event_type = agent_event_type
        self.retire_checked = retire_checked
        self.rt = rt
        self.key, self.name = owner_key, owner_name
        self.team = self.deployment.owner.slack_workspace
        self.app_id = self.deployment.owner.assistant_slack_app
        self.ask_owner_closed_line = self.msg('escalation.ask_owner_closed_line.1') + self.deployment.owner.timezone + ')'
        self.superseded_line = self.msg('escalation.superseded_line.1')  # P46: a waiting card a rule-check continuation of its task took over
        self.ask_owner_proposal_closed = self.msg('escalation.ask_owner_proposal_closed.1')

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def _register_root(self, db, channel, root_ts, task_id, now, home=False):
        """Record a managed approvals root (one per topic): the reconcile gives it a task-level watch bound to `task_id`'s
        current session. `home=True` marks a task's inherited launch-from-card root (what `_approvals_root` returns)."""
        with self.rt.transaction(db):
            if task_id:
                self.rt.registry.ensure_task(db, str(task_id), now)
            db.execute('INSERT INTO approvals_roots (channel,root_ts,task_id,home,created) VALUES (?,?,?,?,?) '
                       'ON CONFLICT(channel,root_ts) DO UPDATE SET task_id=excluded.task_id,'
                       'home=max(approvals_roots.home,excluded.home)',
                       (channel, root_ts, str(task_id) if task_id else None, int(bool(home)), now))

    def _ensure_approvals_watch(self, db, channel, root, task_id, now=None, since=None, dispatcher=None):
        """Create, or rebind to the owning task's consumer, the long-lived approvals watch of ONE topic thread (keyed by
        {channel, root}). P36: one watch per topic root, not per task — a task with several independent requests has one
        watch per request, and a task launched from a card shares the originating card's root (one watch).

        Returns the watch id, or None when the task has no consumer yet (a later reconcile creates it). The watch follows the
        task's consumer (P38 G1 `registry.task_consumer`, the same answer the dispatcher's reroute uses: the running headless
        run, else a live interactive lease — only with the supervisor's `dispatcher` to prove it live — else current_session),
        so after any single card's decision watch stops, the owner's later replies in the thread still reach whichever session now
        owns the task. P38 G2: a rebind moves OWNERSHIP only — it never changes an event's status or receipt. While the bound
        session still has in-flight events on the watch (queued / claimed / sending / delivery_unknown) the rebind waits:
        that work stays with the session it was given, and a proven-dead session's work moves only through the dispatcher
        (route / the stranded sweep, which settle claims first). Dispatched events are never touched (G7). G6: an explicitly
        stopped watch stays stopped (re-arm only through `rearm`); a rebind wakes nobody (deliver bumps for real work)."""
        now = time.time() if now is None else now
        if not channel or not root or not task_id:
            return None
        row = db.execute('SELECT path FROM tasks WHERE task_id=?', (str(task_id),)).fetchone()
        if row is None:
            return None
        cwd = row['path']
        consumer = (dispatcher.task_consumer(str(task_id)) if dispatcher is not None else
                    self.rt.registry.task_consumer(db, str(task_id)))
        if not consumer or not cwd or not Path(cwd).is_dir():
            return None
        provider, session = self.rt.claude.provider({'provider': consumer[0] or 'codex'}), consumer[1]
        wid = self.rt._approvals_wid(channel, root)
        existing = db.execute('SELECT data FROM watches WHERE id=?', (wid,)).fetchone()
        if existing:
            w = json.loads(existing['data'])
            if w.get('state') == 'stopped':
                return wid
            target = (provider, session, str(cwd), str(task_id))
            if (self.rt.claude.provider(w), w['session'], w['cwd'], w.get('task_id')) == target and not w.get('rebind_waiting'):
                return wid  # the common case every loop: nothing to write, no transaction
            if (self.rt.claude.provider(w), w['session']) != (provider, session) and self.rt._in_flight(db, wid) and \
                    self._owner_dead_unrescued(db, w, dispatcher):
                # Nothing else will ever move work a dead session was given (the stranded sweep is off): settle its claims
                # and hand it to the new consumer the dispatcher's way.
                dispatcher.rebind_watch(wid, provider, session, 'approvals_rebind_dead_owner')
            with self.rt.transaction(db):
                w = self.rt.load(db, wid)
                if w.get('state') == 'stopped':
                    return wid
                if (self.rt.claude.provider(w), w['session'], w['cwd'], w.get('task_id')) == target:
                    changed = w.pop('rebind_waiting', None) is not None
                elif self.rt._in_flight(db, wid):
                    # The bound session still holds work on this thread: ownership (session, cwd AND task) waits with it.
                    waiting = {'provider': provider, 'session': session, 'task_id': str(task_id)}
                    changed = w.get('rebind_waiting') != waiting
                    w['rebind_waiting'] = waiting  # shown by `status`; the reconcile keeps aiming at this task
                else:
                    # Rebind (fallback / revival / fresh run / launch-from-card handoff): pending and future replies go to
                    # the new consumer; nothing already delivered moves.
                    if (self.rt.claude.provider(w), w['session'], w['cwd']) != (provider, session, str(cwd)):
                        history = (list(w.get('owner_history') or [])[-9:] +
                                   [{'provider': self.rt.claude.provider(w), 'session': w['session'], 'until': now,
                                     'reason': 'approvals_rebind'}])
                        w.update(session=session, provider=provider, cwd=str(cwd), owner_history=history,
                                 routed_by='reconcile', routed_at=now, route_reason='approvals_rebind')
                    w['task_id'] = str(task_id)
                    w.pop('rebind_waiting', None)
                    changed = True
                if changed:
                    self.rt.save(db, w)
            return wid
        with self.rt.transaction(db):
            if db.execute('SELECT 1 FROM watches WHERE id=?', (wid,)).fetchone():
                return wid
            w = dict(id=wid, session=session, provider=provider, cwd=str(cwd), channel=channel, thread=root,
                     peers=[self.owner_slack_user], dm=None, mode='waiting', lifecycle='until_stop', scope='thread', context_all=False,
                     kind=self.rt.APPROVALS_KIND, state='running', started=now, since=str(since if since is not None else root),
                     deadline=None, next_poll=now + 5, step=0, polls=0, owner_pid=None, owner_birth=None, last_error=None,
                     cadence_activity_ts=now, cadence_version=self.rt.schedule.VERSION, label=self.msg('escalation._ensure_approvals_watch.1'),
                     task_id=str(task_id))
            self.rt.save(db, w)
        return wid

    def _rearm_retired_approvals(self, db, channel, root, now):
        """P39 (F03 c): a new card (a continuation, a launch-from-card child's card) under a thread whose approvals watch
        the reconcile retired re-arms it, so the card threads there and the thread is polled again. the owner's own stops and the
        kill switch stay as they are."""
        if not channel or not root:
            return False
        wid = self.rt._approvals_wid(channel, root)
        with self.rt.transaction(db):
            row = db.execute('SELECT data FROM watches WHERE id=?', (wid,)).fetchone()
            w = json.loads(row[0]) if row else None
            if not w or w.get('state') != 'stopped' or w.get('stop_reason') != self.rt.card_threads.RETIRED:
                return False
            w.update(state='running', next_poll=now + 5, resumed_at=now, step=0)
            for key in ('stop_reason', 'stopped_at', 'retired_at'):
                w.pop(key, None)
            self.rt.save(db, w)
        return True

    def _maybe_retire_approvals(self, db, w, now):
        """P39 (F03 c): an approvals watch retires (stop_reason `retired`) once its thread has no open card and no
        unhandled event and has been quiet for cards.thread_retire_hours, or at once when its task is closed. Replies typed
        later are not lost: the realtime push / the reconcile sweep (card_threads) deliver them to the card's task, and a
        new card under the thread re-arms the watch. Checked at most every THREAD_RETIRE_CHECK_SECONDS per watch."""
        if w.get('state') != 'running' or now - self.retire_checked.get(w['id'], 0) < THREAD_RETIRE_CHECK_SECONDS:
            return False
        self.retire_checked[w['id']] = now
        cfg = self.rt.card_threads.config(self.rt)
        hours = cfg.get('thread_retire_hours') or 0
        # Only with the realtime push on: it reaches a thread of ANY age, the sweep only the last reconcile_days — without
        # the push a retired old thread's late reply would reach nobody (pre-P39 its poller ran forever).
        if not hours or not cfg.get('realtime_replies') or self.rt.card_replies.open_cards(self.rt, db, w['channel'], w['thread'],
                                                                                      now=now):
            return False
        if db.execute("SELECT 1 FROM events WHERE watch_id=? AND status!='handled' LIMIT 1", (w['id'],)).fetchone():
            return False
        task = self.rt._approvals_task(w, db)
        closed = not task or self.rt.card_threads.task_closed(self.rt, db, task)
        last = max([float(w.get(k) or 0) for k in ('started', 'resumed_at', 'cadence_activity_ts')] + [
            float(db.execute('SELECT coalesce(max(c.created),0) FROM escalation_cards c JOIN escalations x ON '
                             'x.event_id=c.event_id WHERE c.channel=? AND coalesce(c.thread_root,x.thread_ts)=?',
                             (w['channel'], w['thread'])).fetchone()[0]),
            float(db.execute('SELECT coalesce(max(received),0) FROM events WHERE watch_id=?', (w['id'],)).fetchone()[0])])
        if not closed and now - last < hours * 3600:
            return False
        if self._stop_approvals_watch(db, w['id'], self.rt.card_threads.RETIRED, now):
            with self.rt.transaction(db):
                current = self.rt.load(db, w['id'])
                current['retired_at'] = now
                current['retire_reason'] = 'task_closed' if closed else 'idle'
                self.rt.save(db, current)
            return True
        return False

    def _owner_dead_unrescued(self, db, w, dispatcher):
        """P38 G2: the bound session is proven dead and the stranded sweep will not rescue its work (dispatch or
        stranded_revive off) — the only case a rebind may move in-flight events itself (pre-P38 moved them always)."""
        if dispatcher is None:
            return False
        try:
            config = dispatcher.config()['dispatch']
        except ValueError:
            return False
        if config['enabled'] and config['stranded_revive']:
            return False
        return dispatcher.probe.check(self.rt.claude.provider(w), w['session'])['state'] == dispatcher.rt.liveness.DEAD

    def _stop_approvals_watch(self, db, wid, reason, now):
        """Stop one approvals watch (P38 F19 kill switch) — the same sticky stop as an explicit one."""
        with self.rt.transaction(db):
            w = self.rt.load(db, wid)
            if w.get('state') == 'stopped':
                return False
            w.update(state='stopped', stop_reason=reason, stopped_at=now)
            self.rt.save(db, w)
            if self.rt.claude.provider(w) == 'claude':
                self.rt.claude.resolved(db, w['session'])
        return True

    def _hand_off_stopped_approvals(self, db, w, now, dispatcher=None, in_flight=False):
        """P38 F03(d): a stopped approvals watch never polls or delivers again, so the owner's replies it ingested but no session
        consumed yet (pending, or queued to a Claude outbox — a stopped watch is never claimed) move ONCE to the task
        consumer's local inbox watch, where ordinary delivery / routing applies (the event keeps its Slack source:
        event_sources has its root). Anything a session already has (claimed, a Codex queue, sending / delivery_unknown) or a
        dispatch holds (dispatched) stays where it is: never re-sent (G2) — unless `in_flight`: the stranded sweep proved
        that session dead on two sweeps, so its claims are settled and its undelivered work moves too. Returns the moved ids."""
        task_id = w.get('task_id')
        claude_watch = self.rt.claude.provider(w) == 'claude'
        statuses = (('pending', 'queued', 'claimed', 'sending', 'delivery_unknown') if in_flight else
                    ('pending', 'queued') if claude_watch else ('pending',))
        ids = [r[0] for r in db.execute('SELECT id FROM events WHERE watch_id=? AND status IN (%s) ORDER BY received,id'
                                        % ','.join('?' * len(statuses)), (w['id'], *statuses))]
        if not ids or not task_id:
            return []
        row = db.execute('SELECT path FROM tasks WHERE task_id=?', (str(task_id),)).fetchone()
        consumer = (dispatcher.task_consumer(str(task_id)) if dispatcher is not None else
                    self.rt.registry.task_consumer(db, str(task_id)))
        if row is None or not consumer or not row['path'] or not Path(row['path']).is_dir():
            return []
        moved = []
        with self.rt.transaction(db):
            inbox = self.rt.ensure_inbox_watch(db, str(task_id), row['path'], consumer[0] or 'codex', consumer[1], now)
            self.rt.claude.settle_claims(db, [r[0] for r in db.execute("SELECT id FROM events WHERE watch_id=? AND status='claimed'",
                                                                (w['id'],))] if in_flight else [], 'owner_dead_handed_off', now)
            for eid in ids:
                if db.execute('UPDATE events SET watch_id=?,status=\'pending\',receipt=NULL WHERE id=? AND watch_id=? AND '
                              'status IN (%s)' % ','.join('?' * len(statuses)),
                              (inbox['id'], eid, w['id'], *statuses)).rowcount:
                    moved.append(eid)
            if moved:
                current = self.rt.load(db, w['id'])
                current['handed_off'] = (list(current.get('handed_off') or []) + [
                    {'to': inbox['id'], 'events': len(moved), 'at': now}])[-10:]
                self.rt.save(db, current)
        return moved

    def _reconcile_approvals_watches(self, db, now=None, dispatcher=None):
        """Supervisor step: rebind every running approvals watch to its owning task's consumer, and create any missing watch
        for a registered topic root whose task has a consumer. Best effort; keyed by {channel, root} so a root shared by a
        parent and its launched child is never duplicated. P38: a stopped watch stays stopped (G6) and hands its unconsumed
        replies off once (F03 d). `cards.task_thread=false` (explicitly — an unreadable config changes nothing) stops every
        approvals watch once no session holds in-flight work on it (F19: sticky like an explicit stop, handed off the same
        way); switching it back on resumes only those, from their own cursor (ingest's card-thread dedupe skips what the
        per-card watches took meanwhile)."""
        now = time.time() if now is None else now
        out = []
        switch = self.rt._task_thread_switch(db)
        if switch is None:
            return out
        seen = set()
        for row in db.execute("SELECT data FROM watches WHERE json_extract(data,'$.kind')=?", (self.rt.APPROVALS_KIND,)).fetchall():
            w = json.loads(row[0])
            seen.add((w['channel'], w['thread']))
            try:
                if not switch and w.get('state') != 'stopped':
                    if not self.rt._in_flight(db, w['id']):
                        self._stop_approvals_watch(db, w['id'], APPROVALS_KILL_SWITCH, now)
                    w = self.rt.load(db, w['id'])
                elif switch and w.get('state') == 'stopped' and w.get('stop_reason') == APPROVALS_KILL_SWITCH:
                    with self.rt.transaction(db):
                        w = self.rt.load(db, w['id'])
                        if w.get('state') == 'stopped' and w.get('stop_reason') == APPROVALS_KILL_SWITCH:
                            w.update(state='running', next_poll=now + 5, resumed_at=now)
                            for key in ('stop_reason', 'stopped_at'):
                                w.pop(key, None)
                            self.rt.save(db, w)
                if w.get('state') == 'stopped':
                    self._hand_off_stopped_approvals(db, w, now, dispatcher)
                elif switch and w.get('task_id'):
                    if self.rt._approvals_task(w) != w['task_id'] and not self.rt.registry._row(db, self.rt._approvals_task(w)):
                        with self.rt.transaction(db):  # the task a waiting handoff aimed at is gone: aim at its own task again
                            w = self.rt.load(db, w['id'])
                            w.pop('rebind_waiting', None)
                            self.rt.save(db, w)
                    self._ensure_approvals_watch(db, w['channel'], w['thread'], self.rt._approvals_task(w, db), now, dispatcher=dispatcher)
                    out.append(w['id'])
                    self._maybe_retire_approvals(db, self.rt.load(db, w['id']), now)
            except Exception as exc:  # noqa: BLE001
                self.rt._meta_error(db, 'approvals_watch_error', exc)
        if not switch:
            return out
        for r in db.execute('SELECT a.channel,a.root_ts,a.task_id FROM approvals_roots a JOIN tasks t ON t.task_id=a.task_id '
                            'WHERE t.current_session IS NOT NULL').fetchall():
            if (r['channel'], r['root_ts']) in seen:
                continue
            try:
                wid = self._ensure_approvals_watch(db, r['channel'], r['root_ts'], r['task_id'], now, dispatcher=dispatcher)
                if wid:
                    out.append(wid)
            except Exception as exc:  # noqa: BLE001
                self.rt._meta_error(db, 'approvals_watch_error', exc)
        return out

    def inherit_approvals_thread(self, db, child_task_id, from_card_event, now=None):
        """A task launched from a card joins that card's approvals thread: the child's cards thread under the same root, and
        the thread rebinds to the child so replies between the owner's click and the takeover reach it (reuse the handoff path)."""
        now = time.time() if now is None else now
        if not from_card_event or not self.rt._task_thread_enabled(db):
            return None
        card = db.execute('SELECT channel, thread_root, thread_ts FROM escalation_cards c JOIN escalations x '
                          'ON x.event_id=c.event_id WHERE c.event_id=?', (str(from_card_event),)).fetchone()
        if card is None:
            return None
        channel, root = card['channel'], (card['thread_root'] or card['thread_ts'])
        if not channel or not root or self.rt._secret_thread(db, channel, root):
            return None  # P38: a thread hosting a card that waits for a verification code never becomes a task thread
        # Never overwrite a child that already owns a thread (it launched and established its own root first): keep it.
        owned = db.execute('SELECT channel, root_ts FROM approvals_roots WHERE task_id=? ORDER BY home DESC, created LIMIT 1',
                           (str(child_task_id),)).fetchone()
        if owned:
            return {'channel': owned['channel'], 'root_ts': owned['root_ts']}
        self._register_root(db, channel, root, str(child_task_id), now, home=True)
        self._rearm_retired_approvals(db, channel, root, now)  # P39 (F03 c): the child's thread is polled again
        self._ensure_approvals_watch(db, channel, root, child_task_id, now)  # create, or transfer this root's thread to the child
        return {'channel': channel, 'root_ts': root}

    def _post_card(self, db, eid, channel, text, options, client_id, user_token, bot_token, metadata=None,
                   kind='escalation_card', footer=None, hint=None, task_id=None, task_thread=True, broadcast=False,
                   establish_root=True, parent_event=None, now=None, config=None, smoke=False, live_card=False,
                   body_blocks=None, verdict=None):
        # `options`: stored option list ([] = default approve/reject pair). `footer`: P3.1 context line under the
        # buttons (task name, dashboard link), kept on the card after a decision. P36 rooting (threading by REQUEST/TOPIC,
        # not task lifetime): `parent_event` set = this card CONTINUES that card's request (blocker stage-2, a reminder,
        # a confirmation) and threads under its root; otherwise an independent card threads under the task's inherited
        # launch-from-card HOME root if it has one, else is its OWN fresh top-level root (so two unrelated asks from one
        # long-lived task never share a thread). `task_thread=False` keeps the card top-level but still records its own ts
        # as a root a child can inherit (intake briefs); `establish_root=False` joins an existing thread but never starts
        # one (code-owned routine cards); `broadcast` adds reply_broadcast so a threaded never-auto / high-priority card
        # also surfaces at the channel level. cardsafe: `config` drives the private-destination guard (loaded if not
        # supplied); a smoke card is DRY unless `live_card` — synthesized in the DB with no Slack post. P38 (F49):
        # `verdict` is the caller's own Slack destination check of this channel, taken before its first write — passed on
        # so this post does not look again (an expiring cache must not strand a holding message / row without a card).
        # P41: the default hint follows cards.retire_on (a question keeps the buttons; any_reply = the P3.1 hint).
        """Post the card, persist its ts before readback, and verify the owner mention. A dry smoke card touches no Slack."""
        now = time.time() if now is None else now
        if config is None:
            config = self.rt.buttons.load_config()
        if hint is None:
            hint = self.rt.buttons.HINT if self.rt._cards_config(db).get('retire_on') == 'any_reply' else self.rt.buttons.KEEP_HINT
        # Defence in depth: this card mentions the owner; it must never reach a public / arbitrary channel even via
        # misconfiguration. Refuse (and record) rather than post to anything but the private decision channel or the owner's DM.
        if not self.rt.buttons.private_card_destination(config, channel):
            self.rt._meta_note(db, 'card_destination_blocked', f'{kind} {channel}')
            raise ValueError(f'{self.key}_card_destination_not_private:' + str(channel))
        if smoke and not live_card:
            # Dry smoke: a TEST card synthesized exactly as a real one in the DB (so decide / settle / cleanup all work on
            # the record), but with a fabricated ts and NO network post — never a real Slack message, never any cleanup.
            ts = f'{int(now)}.{int(hashlib.sha256(("dry" + eid).encode()).hexdigest()[:8], 16) % 1000000:06d}'
            with self.rt.transaction(db):
                db.execute('UPDATE escalations SET stage=?,thread_ts=? WHERE event_id=?', ('sent', ts, eid))
                db.execute('UPDATE escalation_cards SET task_id=?,thread_root=?,dry=1 WHERE event_id=?',
                           (str(task_id) if task_id else None, ts, eid))
                self.rt._ledger(db, channel, ts, kind, eid, (metadata or {}).get('event_payload', {}).get('session'))
            return ts
        # P38 (F49): before a REAL post Slack itself must report the destination private / an IM (cached per channel; a
        # public channel or a failed check refuses, recorded) — the membership check above can't see a misconfiguration.
        if verdict not in self.rt.buttons.PRIVATE_VERDICTS:
            self.rt.buttons.require_private_destination(self.rt, db, channel, kind, config=config)
        threading_on = bool(task_thread and task_id and self.rt._task_thread_enabled(db))
        # A continuation threads under its parent card's root; an independent card under the task's inherited home root (if
        # any). An independent card with no such root becomes its own fresh topic root below.
        root = (self.rt._card_root(db, parent_event) if parent_event else self.rt._approvals_root(db, task_id)) if threading_on else None
        if root and root['channel'] == channel:
            self._rearm_retired_approvals(db, channel, root['root_ts'], now)  # P39 (F03 c): a new card re-arms a retired thread
        # Thread a card only under a root that a live task-level approvals watch is actually polling (created by the root
        # card once its task has a current session, by inheritance at launch-from-card, or by the reconcile step). Otherwise
        # post at the top level, so a thread's replies can never reach no one.
        threaded = bool(root and root['channel'] == channel and self.rt._approvals_watch_active(db, channel, root['root_ts']))
        body = {'channel': channel, 'text': text, 'client_msg_id': client_id,
                'blocks': self.rt.buttons.card_blocks(text, eid, self.rt.buttons.card_model(options), footer, hint, body_blocks=body_blocks),
                'metadata': metadata or self.rt.agent_metadata(kind, event_id=eid),
                'unfurl_links': False, 'unfurl_media': False}
        if threaded:
            body['thread_ts'] = root['root_ts']
            if broadcast:
                body['reply_broadcast'] = True
        sent = self.rt.slack.api_call('chat.postMessage', bot_token, json_body=body)
        ts = sent['ts']
        # A post addressed to the owner's user id (the unconfigured DM fallback) resolves to his bot↔the owner D-channel; use that
        # resolved conversation id for the read-back, ledger and stored card channel. A normal channel resolves to itself.
        channel = sent.get('channel') or channel
        thread_root = root['root_ts'] if threaded else ts
        # Persist before verification so an API/read failure cannot duplicate posts.
        with self.rt.transaction(db):
            db.execute('UPDATE escalations SET stage=?,thread_ts=? WHERE event_id=?', ('sent', ts, eid))
            db.execute('UPDATE escalation_cards SET channel=?,task_id=?,thread_root=? WHERE event_id=?',
                       (channel, str(task_id) if task_id else None, thread_root, eid))
            self.rt._ledger(db, channel, ts, kind, eid, (metadata or {}).get('event_payload', {}).get('session'))
        if threading_on and not threaded and establish_root and not parent_event:
            # An independent card with no active root to join: register it as its OWN topic root (the reconcile step starts
            # the task-level approvals watch for it once the task has a current session). A continuation (parent_event) never
            # starts a new root; a code-owned card (establish_root=False) only joins an existing thread.
            self._register_root(db, channel, ts, task_id, now)
        read = self.rt.slack.api_call('conversations.replies', user_token,
                              params={'channel': channel, 'ts': ts, 'limit': 1})['messages'][0]
        if read.get('user') != self.bot_user or f'<@{self.owner_slack_user}>' not in read.get('text', ''):
            raise RuntimeError('escalation_readback_failed')
        return ts

    def escalation(self, db, eid, question, db_path, holding_message, options=None, never_auto=False, recommend=None):
        if not holding_message.strip() or len(holding_message) > 500:
            raise ValueError('holding_message_required_in_peer_language_max_500_chars')
        self.rt.buttons.check_copy(question)  # P3.1 copy standard: the owner reads this first
        options = self.rt.buttons.validate_options(options, recommend)  # 2-4 scenario options replace approve/reject
        config = self.rt.buttons.load_config()  # malformed config fails before any Slack write
        e = db.execute('SELECT * FROM events WHERE id=?', (eid,)).fetchone()
        if not e:
            raise ValueError('unknown_event')
        w = self.rt.load(db, e['watch_id'])
        self.rt.require_event_owner(db, e, w)
        owner = w
        if self.rt.by_dispatch(db, e):
            # P38 G3: the dispatch's run escalates its event: the card, its watch and the holding message are the run's.
            provider, session = self.rt.claude.actor()
            owner = {**w, 'provider': provider, 'session': session, 'owner_pid': None, 'owner_birth': None}
        original_thread = w['thread']
        if w.get('scope') == 'channel':
            source_ref = db.execute('SELECT root FROM event_sources WHERE event_id=?', (eid,)).fetchone()
            original_thread = source_ref[0] if source_ref else None
        if not original_thread:
            raise ValueError('bind_canonical_source_thread_before_escalation')
        if w['mode'] == 'decision' or w.get('kind') == 'decision':
            raise ValueError('clarify_in_existing_decision_thread')
        row = db.execute('SELECT * FROM escalations WHERE event_id=?', (eid,)).fetchone()
        if row:
            return dict(row)
        if e['status'] == 'handled':
            raise ValueError('event_already_handled')
        user_token, bot_token = self.rt.api_identity(), self.rt.api_identity(bot=True)
        original_thread = self.rt.verified_root(user_token, w['channel'], original_thread)
        # P46.2: the holding message is the owner's voice to the colleague: in the language they wrote in, before any write
        self.rt.intake.require_language(self.rt, db, e['author'], [holding_message], what='holding message',
                                words=self.rt.intake.own_words(self.rt, w['channel'], original_thread, e['author']))
        # Daily cards: the owner-only decision channel. In-window OOO: ooo.channel + delegates.
        route = self.rt.buttons.card_route(config, time.time(), never_auto)
        card_channel = route['channel']
        # P38 (F49): verify the card destination before ANY write, so a refused card never leaves the colleague a holding
        # message with no card behind it; _post_card takes this verdict and does not look again.
        verdict = self.rt.buttons.require_private_destination(self.rt, db, card_channel, 'escalation_card', config=config)
        with self.rt.transaction(db):
            self.rt.require_event_owner(db, db.execute('SELECT * FROM events WHERE id=?', (eid,)).fetchone(), self.rt.load(db, e['watch_id']))
            row = db.execute('SELECT * FROM escalations WHERE event_id=?', (eid,)).fetchone()
            if row:
                return dict(row)
            client_id = str(uuid.uuid4())
            db.execute('INSERT INTO escalations VALUES (?,?,?,?,?)', (eid, 'sending', None, None, client_id))
            db.execute('INSERT INTO escalation_cards (event_id,channel,options,never_auto,created) VALUES (?,?,?,?,?)',
                       (eid, card_channel, json.dumps(options, ensure_ascii=False), int(bool(never_auto)), time.time()))
        held = self.rt.slack.api_call('chat.postMessage', user_token, json_body={
            'channel': w['channel'], 'thread_ts': original_thread, 'text': holding_message,
            'metadata': self.rt.agent_metadata('holding_message', owner, event_id=eid)})
        self.rt.record_send(db, w['channel'], (held or {}).get('ts'), 'holding_message', eid, owner['session'])
        if (held or {}).get('ts'):  # P39: a holding message in a DM opens the task's conversation window there
            with self.rt.transaction(db):
                self.rt.schedule.note_window(db, w['channel'], held['ts'], self.rt.registry.task_id_for_cwd(w['cwd']), 'holding_message',
                                     e['author'] if re.fullmatch(r'U[A-Z0-9]+', str(e['author'])) else None)
        self.rt.slack.add_reactions(user_token, e['channel'], e['ts'], ['thinking_face', self.rt.RECEIPT_EMOJI['claude' if self.rt.claude.provider(owner) == 'claude' else 'codex']])
        link = f"{self.deployment.owner.slack_workspace_url}/archives/{e['channel']}/p{e['ts'].replace('.', '')}"
        # Escape supplied plain text so it cannot insert mentions or Slack links. P3.1: the question leads; the source
        # message link and the task move to the footer.
        text = f'<@{self.owner_slack_user}> {self.rt.buttons.escape(question)}'
        if route['delegates']:
            text += self.msg('escalation.escalation.1', owner=self.name) + ' '.join(f'<@{d}>' for d in route['delegates']) + self.msg('escalation.escalation.2')
        footer = (self.msg('escalation.escalation.4', escape=self.rt.buttons.escape(self.rt.registry.task_id_for_cwd(w['cwd']) or self.msg('escalation.escalation.3') + owner['session'][:8]), link=link))
        tag = self.rt.session_footer(db, self.rt.registry.task_id_for_cwd(w['cwd']), self.rt.claude.provider(owner), owner['session'])  # P43
        footer += f' · {tag}' if tag else ''
        ts = self._post_card(db, eid, card_channel, text, options, client_id, user_token, bot_token,
                        self.rt.agent_metadata('escalation_card', owner, event_id=eid), footer=footer,
                        task_id=self.rt.registry.task_id_for_cwd(w['cwd']), broadcast=bool(never_auto), config=config,
                        verdict=verdict)
        child = self.rt.register(db, session=owner['session'], provider=self.rt.claude.provider(owner), cwd=w['cwd'], channel=card_channel,
                         thread=ts, peers=[self.owner_slack_user], mode='decision', since=ts,
                         owner_pid=owner['owner_pid'], owner_birth=owner['owner_birth'])
        if route['delegates']:
            with self.rt.transaction(db):
                child = self.rt.load(db, child['id'])
                child.update(delegates=route['delegates'], delegate_window=route['window'])
                self.rt.save(db, child)
        self.rt.resolve(db, eid, 'decision', ts)
        with self.rt.transaction(db):
            db.execute('UPDATE escalations SET stage=?,watch_id=? WHERE event_id=?', ('watching', child['id'], eid))
        self.rt.start(db_path)
        return {'thread_ts': ts, 'channel': card_channel, 'watch_id': child['id'], 'stage': 'watching',
                'options': options, 'never_auto': bool(never_auto), 'ooo': route['ooo'], 'delegates': route['delegates'],
                # the owner-facing text is Chinese by default; flagged (not refused) on this existing path.
                'warnings': [] if CJK_RE.search(question) else [f'{self.key}_facing_question_not_chinese']}

    def _find_receipt(self, user_token, channel, card_ts, eid):
        """Resolve an ambiguous earlier receipt post from bot-authored message metadata."""
        result = self.rt.slack.api_call('conversations.replies', user_token, params={
            'channel': channel, 'ts': card_ts, 'oldest': card_ts, 'include_all_metadata': 'true', 'limit': 200})
        for m in result.get('messages', []):
            meta = m.get('metadata') or {}
            payload = meta.get('event_payload') or {}
            receipt = meta.get('event_type') == self.rt.buttons.METADATA_TYPE or (
                meta.get('event_type') == self.agent_event_type and payload.get('kind') == 'decision_receipt')
            if m.get('user') == self.bot_user and m.get('bot_id') and receipt and payload.get('event_id') == eid:
                return m['ts']
        return None

    def _queue_owner_event(self, db, w, channel, ts, root, author, payload, now, envelope=None):
        """Inside a transaction: pending outbox event on watch `w`, counted as human activity."""
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        event = hashlib.sha256(f'{w["id"]}/{channel}/{ts}/{digest}'.encode()).hexdigest()[:24]
        db.execute('INSERT OR IGNORE INTO events (id,watch_id,channel,ts,author,digest,received,status) '
                   'VALUES (?,?,?,?,?,?,?,?)', (event, w['id'], channel, ts, author, digest, now, 'pending'))
        db.execute('INSERT OR IGNORE INTO event_sources VALUES (?,?)', (event, root))
        self.rt.envelopes.store(db, event, envelope, now)
        sent = float(ts)
        if w['state'] == 'running' and w.get('lifecycle') == 'idle' and sent <= w['deadline'] and \
                sent > w.get('last_activity', w['started']):
            w.update(last_activity=sent, deadline=sent+w.get('inactivity_seconds', self.rt.IDLE_SECONDS))
        self.rt.schedule.activity(w, sent, now)
        self.rt.save(db, w)
        return event

    def _deliver_decision(self, db, eid, receipt_ts, now):
        """The normal outbox delivers the decision; it also closes pending proposals."""
        with self.rt.transaction(db):
            d = db.execute('SELECT * FROM decisions WHERE event_id=?', (eid,)).fetchone()
            if d['delivered_event']:
                return []
            watch_id = db.execute('SELECT watch_id FROM escalations WHERE event_id=?', (eid,)).fetchone()[0]
            if not watch_id:  # supervisor_card: code settles it from `decisions`; no session is woken
                db.execute("UPDATE decisions SET receipt_ts=?,delivered_event=?,stage='delivered',last_error=NULL "
                           'WHERE event_id=?', (receipt_ts, self.rt.SUPERVISOR_STAGE, eid))
                return []
            w = self.rt.load(db, watch_id)
            event = self._queue_owner_event(db, w, d['card_channel'], receipt_ts, d['card_ts'], d['actor'], {
                'sf_decision': eid, 'choice': d['choice'], 'actor': d['actor'], 'receipt_ts': receipt_ts,
                'note': d['note'], 'provenance': d['provenance']}, now,
                self.rt.interaction_envelope(w, 'decision_answer', d['actor'], d['card_ts'], now,
                                     verified_decision=True, role=d['role']))
            closed = [dict(r) for r in db.execute(
                "SELECT * FROM decision_proposals WHERE event_id=? AND status='pending'", (eid,))]
            db.execute("UPDATE decision_proposals SET status='superseded',closed_reason='card_decision' "
                       "WHERE event_id=? AND status='pending'", (eid,))
            db.execute("UPDATE decisions SET receipt_ts=?,delivered_event=?,stage='delivered',last_error=NULL "
                       'WHERE event_id=?', (receipt_ts, event, eid))
        return closed

    def _update_card(self, db, eid, model, user_token, bot_token, excerpt=None):
        d = db.execute('SELECT * FROM decisions WHERE event_id=?', (eid,)).fetchone()
        try:
            card = self.rt.slack.api_call('conversations.replies', user_token, params={
                'channel': d['card_channel'], 'ts': d['card_ts'], 'limit': 1})['messages'][0]
            if card.get('ts') != d['card_ts'] or card.get('user') != self.bot_user:
                raise RuntimeError('card_readback_failed')
            outcome = self.rt.buttons.outcome_block(self.rt.buttons.resolve_choice(d['choice'], model),
                                            d['actor'], d['decided_at'], d['note'], d['provenance'], excerpt)
            self.rt.slack.api_call('chat.update', bot_token, json_body={
                'channel': d['card_channel'], 'ts': d['card_ts'], 'text': card.get('text', ''),
                'blocks': self.rt.buttons.decided_blocks(card.get('blocks'), outcome)})
            stage, error = 'card_updated', None
        except Exception as exc:
            # The recorded and delivered decision stands; only the card is stale.
            stage, error = 'card_update_failed', self.rt.safe_error(exc)
        with self.rt.transaction(db):
            db.execute('UPDATE decisions SET stage=?,last_error=? WHERE event_id=?', (stage, error, eid))

    def _update_proposal_message(self, db, bot_token, channel, p, model, status_line=None, text=None):
        """Best effort: replace confirmation buttons; record the stage, never raise."""
        if not p['message_ts']:
            return
        meaning = p['meaning'] if 'meaning' in p.keys() else None
        body = text or self.rt.buttons.proposal_text(self.rt.buttons.resolve_choice(p['choice'], model), p['note'], meaning)
        blocks = ([{'type': 'section', 'text': {'type': 'mrkdwn', 'text': body}}] if status_line is None
                  else self.rt.buttons.proposal_closed_blocks(body, status_line))
        try:
            self.rt.slack.api_call('chat.update', bot_token, json_body={
                'channel': channel, 'ts': p['message_ts'], 'text': body, 'blocks': blocks})
            stage, error = 'message_updated', None
        except Exception as exc:
            stage, error = 'message_update_failed', self.rt.safe_error(exc)
        with self.rt.transaction(db):
            db.execute('UPDATE decision_proposals SET message_stage=?,last_error=? WHERE id=?', (stage, error, p['id']))

    def _complete_decision(self, db, eid, model, duplicate, user_token, bot_token, now, excerpt=None, guidance=False):
        """Shared by card buttons, confirmed text and (P3.1) the owner's typed thread reply: receipt, owner event, card,
        proposal messages. `excerpt` / `guidance` only shape the thread-reply receipt and outcome line."""
        d = db.execute('SELECT * FROM decisions WHERE event_id=?', (eid,)).fetchone()
        closed = []
        if not d['delivered_event']:
            receipt_ts = self._find_receipt(user_token, d['card_channel'], d['card_ts'], eid) if duplicate else None
            if receipt_ts is None:
                option = self.rt.buttons.resolve_choice(d['choice'], model)
                owner = self.rt._escalation_watch(db, eid)
                fields = self.rt.buttons.receipt_metadata(eid, d['choice'], d['actor'], d['provenance'],
                                                  d['proposal_id'], option)['event_payload']
                try:
                    sent = self.rt.slack.api_call('chat.postMessage', bot_token, json_body={
                        'channel': d['card_channel'], 'thread_ts': d['card_ts'],
                        'text': self.rt.buttons.receipt_text(d['actor'], option, d['note'], d['provenance'], guidance),
                        'metadata': self.rt.agent_metadata('decision_receipt', owner, **fields),
                        'unfurl_links': False, 'unfurl_media': False})
                    receipt_ts = sent['ts']
                    self.rt.record_send(db, d['card_channel'], receipt_ts, 'decision_receipt', eid, owner and owner['session'])
                except Exception as exc:
                    # Slack's explicit ok:false proves nothing was posted: forget the click.
                    definite = isinstance(exc, RuntimeError) and str(exc).startswith('Slack API error:')
                    with self.rt.transaction(db):
                        if definite:
                            db.execute('DELETE FROM decisions WHERE event_id=? AND delivered_event IS NULL', (eid,))
                            if d['proposal_id']:
                                db.execute("UPDATE decision_proposals SET status='pending',decided_by=NULL,decided_at=NULL "
                                           "WHERE id=? AND status='confirmed'", (d['proposal_id'],))
                        else:
                            db.execute("UPDATE decisions SET stage='receipt_failed',last_error=? WHERE event_id=?",
                                       (self.rt.safe_error(exc), eid))
                    return {'ok': False, 'reason': 'receipt_post_failed', 'recorded': not definite,
                            'event_id': eid, 'error': self.rt.safe_error(exc)}
            closed = self._deliver_decision(db, eid, receipt_ts, now)
        if d['stage'] != 'card_updated':
            self._update_card(db, eid, model, user_token, bot_token, excerpt)
        for p in closed:
            self._update_proposal_message(db, bot_token, d['card_channel'], p, model,
                                     status_line=self.rt.buttons.SUPERSEDED['card_decision'])
        if d['proposal_id']:
            p = self.rt._proposal(db, d['proposal_id'])
            if p and p['message_stage'] != 'message_updated':
                self._update_proposal_message(db, bot_token, d['card_channel'], p, model,
                                         status_line=self.rt.buttons.confirmed_line(d['actor'], d['decided_at']))
        return self.rt._decision_result(db.execute('SELECT * FROM decisions WHERE event_id=?', (eid,)).fetchone(),
                                model, duplicate)

    def decide(self, db, eid, choice, actor, card_channel, card_ts, now=None, config=None, note=None):
        """Record one Slack card-button decision; every rejection happens before any write."""
        now = time.time() if now is None else now
        if self.rt._agent_session():
            return self.rt._fail('agent_session_cannot_decide')
        if not (re.fullmatch(r'[0-9a-f]{24}', str(eid)) and self.rt._ids_valid(actor, card_channel, card_ts)):
            return self.rt._fail('invalid_request')
        try:
            note = self.rt.buttons.validate_note(note)
        except ValueError:
            return self.rt._fail('invalid_note')
        esc = db.execute('SELECT * FROM escalations WHERE event_id=?', (eid,)).fetchone()
        card = db.execute('SELECT * FROM escalation_cards WHERE event_id=?', (eid,)).fetchone()
        if esc is None or card is None:
            return self.rt._fail('unknown_escalation')
        if card['channel'] != card_channel or esc['thread_ts'] != card_ts:
            return self.rt._fail('card_mismatch')
        model = self.rt.buttons.card_model(card['options'])
        try:
            self.rt.buttons.resolve_choice(choice, model)  # exact button choice; scenario cards drop approve/reject
        except ValueError:
            return self.rt._fail('invalid_choice')
        try:
            config = config or self.rt.buttons.load_config()
        except ValueError:
            return self.rt._fail('invalid_decision_config')
        role = self.rt.buttons.authorize(config, actor, now, bool(card['never_auto']))
        if role is None:
            return self.rt._fail('not_authorized')
        existing = db.execute('SELECT * FROM decisions WHERE event_id=?', (eid,)).fetchone()
        if existing is not None and existing['stage'] == 'card_updated':
            return self.rt._decision_result(existing, model, True)
        if existing is None and esc['stage'] != self.rt.SUPERVISOR_STAGE:  # code-owned cards have no watch (supervisor_card)
            if esc['stage'] != 'watching' or not esc['watch_id']:
                return self.rt._fail('escalation_not_ready')
            if self.rt.load(db, esc['watch_id'])['state'] == 'stopped':
                return self.rt._fail('decision_watch_stopped')
        # Verify both identities before recording, so auth failures leave no decision behind.
        user_token, bot_token = self.rt.api_identity(), self.rt.api_identity(bot=True)
        if existing is None:
            with self.rt.transaction(db):
                existing = db.execute('SELECT * FROM decisions WHERE event_id=?', (eid,)).fetchone()
                if existing is None:
                    db.execute('INSERT INTO decisions (event_id,choice,actor,role,decided_at,card_channel,card_ts,stage,note,provenance) '
                               'VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (eid, choice, actor, role, now, card_channel, card_ts, 'recorded', note, 'slack_button'))
        # Later authorized clicks resume a partial pipeline with the first recorded
        # choice; they never post a second receipt or create a second event.
        return self._complete_decision(db, eid, model, existing is not None, user_token, bot_token, now)

    def thread_reply_decision(self, db, eid, reply_ts, now=None, config=None):
        """P3.1 (supervisor only, via card_replies): the owner's typed reply in a card thread becomes the card decision.

        Nothing is taken from the caller but the reply's ts: the message is read back from Slack here, must be the owner's own
        hand-typed reply in this card's thread (envelope trust=<owner key>, authority=<owner key>_intent), and its leading phrase or
        option label must map to exactly one option (decision_buttons.parse_reply). Never-auto cards too: the owner's own typed
        approval is as authenticated as his click; delegates' text never counts. No note is stored, so an approval is
        never conditional: the rest of the text is guidance the session reads in the thread."""
        now = time.time() if now is None else now
        if not (re.fullmatch(r'[0-9a-f]{24}', str(eid)) and re.fullmatch(r'\d+\.\d+', str(reply_ts))):
            return self.rt._fail('invalid_request')
        esc = db.execute('SELECT * FROM escalations WHERE event_id=?', (eid,)).fetchone()
        card = db.execute('SELECT * FROM escalation_cards WHERE event_id=?', (eid,)).fetchone()
        if esc is None or card is None or not esc['thread_ts']:
            return self.rt._fail('unknown_escalation')
        if float(reply_ts) <= float(esc['thread_ts']):
            # P38 (F01): the owner typed this before the card existed (a shared approvals thread re-reads older replies): it can
            # never be his answer to this card, whatever it says.
            return self.rt._fail('reply_is_older_than_the_card')
        if db.execute('SELECT 1 FROM decisions WHERE event_id=?', (eid,)).fetchone():
            return self.rt._fail('already_decided')
        if _control.button_only(db, eid):
            return self.rt._fail('only_a_button_click_decides_this_card')  # Phase 2 C1b: a control confirm card
        w = None
        if esc['stage'] != self.rt.SUPERVISOR_STAGE:
            if esc['stage'] != 'watching' or not esc['watch_id']:
                return self.rt._fail('escalation_not_ready')
            w = self.rt.load(db, esc['watch_id'])
            if w['state'] == 'stopped':
                return self.rt._fail('decision_watch_stopped')
        try:
            config = config or self.rt.buttons.load_config()
        except ValueError:
            return self.rt._fail('invalid_decision_config')
        role = self.rt.buttons.authorize(config, self.owner_slack_user, now, bool(card['never_auto']))
        if role is None:
            return self.rt._fail('not_authorized')
        model = self.rt.buttons.card_model(card['options'])
        user_token, bot_token = self.rt.api_identity(), self.rt.api_identity(bot=True)
        # P3.2: a threaded card is a reply under the task's approvals root, so the owner's reply carries thread_ts = root, not the
        # card's own ts. Match (and read back) against the thread root; the card's own ts stays the chat.update target.
        reply_thread = self.rt._card_reply_thread(db, card, esc)
        found = self.rt.slack.api_call('conversations.replies', user_token, params={
            'channel': card['channel'], 'ts': reply_thread, 'oldest': reply_ts, 'latest': reply_ts, 'inclusive': 'true',
            'limit': 2, 'include_all_metadata': 'true'})
        m = next((x for x in found.get('messages', []) if x.get('ts') == reply_ts), None)
        if m is None or not getattr(self.rt.card_replies, f'{self.key}_typed')(self.rt, db, card['channel'], m, reply_thread):
            return self.rt._fail(f'reply_is_not_{self.key}s_own_typed_reply_in_this_card_thread')
        parsed = self.rt.buttons.parse_reply(m.get('text'), model, strict=self.rt.card_replies.strict(db, eid))
        if parsed['option'] is None:
            return self.rt._fail('no_unambiguous_choice', why=parsed['reason'])
        with self.rt.transaction(db):
            if db.execute('SELECT 1 FROM decisions WHERE event_id=?', (eid,)).fetchone():
                return self.rt._fail('already_decided')
            db.execute('INSERT INTO decisions (event_id,choice,actor,role,decided_at,card_channel,card_ts,stage,note,'
                       'provenance,source_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (eid, parsed['option']['choice'], self.owner_slack_user, role, now, card['channel'], esc['thread_ts'], 'recorded',
                        None, 'thread_reply', reply_ts))
        secret = bool(w and w.get('secret_input'))  # a card waiting for a verification code never quotes a reply
        result = self._complete_decision(db, eid, model, False, user_token, bot_token, now,
                                    excerpt=None if secret else self.rt.buttons.excerpt(m.get('text')), guidance=parsed['guidance'])
        return {**result, 'phrase': parsed['phrase'], 'via': parsed['via'], 'guidance': parsed['guidance']}

    def propose_decision(self, db, eid, choice, source_ts, note=None, now=None, meaning=None):
        """Owner turns a typed decision into a one-tap confirmation; never a decision by itself. `meaning`: the owner's
        one plain sentence of what the owner meant ("我理解你的意思是：<meaning>。" [对，就这样] [不是])."""
        now = time.time() if now is None else now
        note = self.rt.buttons.validate_note(note)
        if meaning is not None:
            meaning = ' '.join(str(meaning).split()).rstrip('。.')
            if not meaning or len(meaning) > 120:
                raise ValueError('meaning_must_be_one_sentence_of_1_to_120_characters')
            self.rt.buttons.check_copy(meaning, 'meaning')
        if not re.fullmatch(r'[0-9a-f]{24}', str(eid)) or not re.fullmatch(r'\d+\.\d+', str(source_ts)):
            raise ValueError('invalid_event_id_or_source_ts')
        esc = db.execute('SELECT * FROM escalations WHERE event_id=?', (eid,)).fetchone()
        card = db.execute('SELECT * FROM escalation_cards WHERE event_id=?', (eid,)).fetchone()
        if esc is None or card is None:
            raise ValueError('unknown_escalation_card')
        if esc['stage'] != 'watching' or not esc['watch_id']:
            raise ValueError('escalation_not_ready')
        w = self.rt.load(db, esc['watch_id'])
        routed = self.rt._routed_reply_consumer(db, card, eid, source_ts)
        if routed is None:
            self.rt.require_owner(w)
        elif w['state'] == 'stopped':
            raise ValueError('watch_stopped')
        if w.get('secret_input') and note and re.search(r'(?<!\d)\d{5,8}(?!\d)', note):
            raise ValueError('note_must_not_contain_a_verification_code')  # P3.1: codes never reach SQLite or Slack
        model = self.rt.buttons.card_model(card['options'])
        choice = self.rt.buttons.normalize_choice(choice, model)  # option id, opt:<id>, or approve/reject on default cards
        option = self.rt.buttons.resolve_choice(choice, model)
        if db.execute('SELECT 1 FROM decisions WHERE event_id=?', (eid,)).fetchone():
            raise ValueError('already_decided')
        card_ts = esc['thread_ts']
        if source_ts == card_ts:
            raise ValueError('source_must_be_a_reply_in_the_card_thread')
        if float(source_ts) < float(card_ts):
            raise ValueError('reply_is_older_than_the_card')  # P38 (F01): typed before this card existed
        config = self.rt.buttons.load_config()
        user_token, bot_token = self.rt.api_identity(), self.rt.api_identity(bot=True)
        # The author comes from Slack, never from the owner's reading of the text. P3.2: a threaded card's replies carry the
        # thread root, not the card's own ts.
        reply_thread = self.rt._card_reply_thread(db, card, esc)
        found = self.rt.slack.api_call('conversations.replies', user_token, params={
            'channel': card['channel'], 'ts': reply_thread, 'oldest': source_ts, 'latest': source_ts,
            'inclusive': 'true', 'limit': 2})
        source = next((m for m in found.get('messages', []) if m.get('ts') == source_ts), None)
        if source is None or source.get('thread_ts') != reply_thread:
            raise ValueError('source_not_in_card_thread')
        author = str(source.get('user') or '')
        if source.get('bot_id') or source.get('subtype') not in (None, 'file_share') or not re.fullmatch(r'U[A-Z0-9]+', author):
            raise ValueError('source_must_be_a_human_message')
        role = self.rt.buttons.authorize(config, author, now, bool(card['never_auto']))
        if role is None:
            raise ValueError('source_author_not_authorized')
        pid = uuid.uuid4().hex
        with self.rt.transaction(db):
            if db.execute('SELECT 1 FROM decisions WHERE event_id=?', (eid,)).fetchone():
                raise ValueError('already_decided')
            db.execute('INSERT INTO decision_proposals (id,event_id,choice,note,source_ts,proposer_user,owner_session,'
                       'proposed_at,status,meaning) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (pid, eid, choice, note, source_ts, author, routed[1] if routed else w['session'], now, 'pending',
                        meaning))
        text = self.rt.buttons.proposal_text(option, note, meaning)
        try:
            sent = self.rt.slack.api_call('chat.postMessage', bot_token, json_body={
                'channel': card['channel'], 'thread_ts': card_ts, 'text': text,
                'blocks': self.rt.buttons.proposal_blocks(pid, text), 'unfurl_links': False, 'unfurl_media': False,
                'metadata': self.rt.agent_metadata('proposal', w, event_id=eid, proposal_id=pid)})
        except Exception as exc:
            with self.rt.transaction(db):
                db.execute("UPDATE decision_proposals SET status='failed',closed_reason='post_failed',last_error=? "
                           'WHERE id=?', (self.rt.safe_error(exc), pid))
            raise ValueError('confirmation_post_failed_' + self.rt.safe_error(exc)) from None
        with self.rt.transaction(db):
            db.execute('UPDATE decision_proposals SET message_ts=? WHERE id=?', (sent['ts'], pid))
            self.rt._ledger(db, card['channel'], sent['ts'], 'proposal', pid, routed[1] if routed else w['session'])
            older = [dict(r) for r in db.execute(
                "SELECT * FROM decision_proposals WHERE event_id=? AND status='pending' AND id!=?", (eid, pid))]
            db.execute("UPDATE decision_proposals SET status='superseded',closed_reason='newer_proposal' "
                       "WHERE event_id=? AND status='pending' AND id!=?", (eid, pid))
        for p in older:
            self._update_proposal_message(db, bot_token, card['channel'], p, model,
                                     status_line=self.rt.buttons.SUPERSEDED['newer_proposal'])
        return {'ok': True, 'proposal_id': pid, 'status': 'pending', 'message_ts': sent['ts'], 'choice': choice,
                'option_id': option['id'], 'label': option['label'], 'kind': option['kind'], 'note': note, 'meaning': meaning,
                'source_author': author, 'source_role': role,
                'superseded': [p['id'] for p in older],
                'next': 'Wait for the confirmation click. Do not execute until context shows a non-null decision.'}

    def confirm_decision(self, db, pid, answer, actor, card_channel, message_ts, now=None, config=None):
        """Handle "✅ 对，就这样" / "✏️ 不是" on a proposal; every rejection happens before any write."""
        now = time.time() if now is None else now
        if self.rt._agent_session():
            return self.rt._fail('agent_session_cannot_decide')
        if not (re.fullmatch(r'[0-9a-f]{32}', str(pid)) and answer in ('yes', 'no')
                and self.rt._ids_valid(actor, card_channel, message_ts)):
            return self.rt._fail('invalid_request')
        p = self.rt._proposal(db, pid)
        if p is None:
            return self.rt._fail('unknown_proposal')
        esc = db.execute('SELECT * FROM escalations WHERE event_id=?', (p['event_id'],)).fetchone()
        card = db.execute('SELECT * FROM escalation_cards WHERE event_id=?', (p['event_id'],)).fetchone()
        if esc is None or card is None:
            return self.rt._fail('unknown_escalation')
        if card['channel'] != card_channel or p['message_ts'] != message_ts:
            return self.rt._fail('proposal_mismatch')
        model = self.rt.buttons.card_model(card['options'])
        try:
            config = config or self.rt.buttons.load_config()
        except ValueError:
            return self.rt._fail('invalid_decision_config')
        role = self.rt.buttons.authorize(config, actor, now, bool(card['never_auto']))
        if role is None:
            return self.rt._fail('not_authorized')
        if actor not in (p['proposer_user'], self.owner_slack_user):
            return self.rt._fail('not_proposal_author')
        status = p['status']
        if status in ('superseded', 'failed'):
            return self.rt._fail('proposal_superseded', closed_reason=p['closed_reason'])
        if (status, answer) == ('confirmed', 'no'):
            return self.rt._fail('proposal_already_confirmed')
        if (status, answer) == ('rejected', 'yes'):
            return self.rt._fail('proposal_already_rejected')
        decision = db.execute('SELECT * FROM decisions WHERE event_id=?', (p['event_id'],)).fetchone()
        if status == 'pending' and decision is not None:
            with self.rt.transaction(db):
                db.execute("UPDATE decision_proposals SET status='superseded',closed_reason='card_decision' "
                           "WHERE id=? AND status='pending'", (pid,))
            return self.rt._fail('already_decided')
        if status == 'confirmed' and decision is not None and decision['stage'] == 'card_updated' \
                and p['message_stage'] == 'message_updated':
            return {**self.rt._decision_result(decision, model, True), 'answer': 'yes'}
        if status == 'rejected' and p['message_stage'] == 'message_updated':
            return {'ok': True, 'answer': 'no', 'duplicate': True, 'proposal_id': pid, 'status': 'rejected',
                    'event_id': p['event_id'], 'actor': p['decided_by'], 'rejected_event': p['rejected_event']}
        if status == 'pending' and self.rt.load(db, esc['watch_id'])['state'] == 'stopped':
            return self.rt._fail('decision_watch_stopped')
        user_token, bot_token = self.rt.api_identity(), self.rt.api_identity(bot=True)
        duplicate = status != 'pending'
        if answer == 'yes':
            if not duplicate:
                with self.rt.transaction(db):
                    current = self.rt._proposal(db, pid)
                    if current['status'] != 'pending' or db.execute(
                            'SELECT 1 FROM decisions WHERE event_id=?', (p['event_id'],)).fetchone():
                        return self.rt._fail('proposal_superseded')
                    db.execute('INSERT INTO decisions (event_id,choice,actor,role,decided_at,card_channel,card_ts,stage,'
                               'note,provenance,proposal_id,source_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                               (p['event_id'], p['choice'], actor, role, now, card_channel, esc['thread_ts'], 'recorded',
                                p['note'], 'text_confirmed', pid, p['source_ts']))
                    db.execute("UPDATE decision_proposals SET status='confirmed',decided_by=?,decided_at=? WHERE id=?",
                               (actor, now, pid))
            result = self._complete_decision(db, p['event_id'], model, duplicate, user_token, bot_token, now)
            return {**result, 'answer': 'yes', 'proposal_id': pid}
        if not duplicate:
            with self.rt.transaction(db):
                current = self.rt._proposal(db, pid)
                if current['status'] != 'pending':
                    return self.rt._fail('proposal_superseded')
                w = self.rt.load(db, esc['watch_id'])
                # The owner gets an event so it can ask again; the confirmation message is its source.
                event = self._queue_owner_event(db, w, card_channel, message_ts, esc['thread_ts'], actor,
                                           {'sf_proposal_rejected': pid, 'actor': actor}, now,
                                           self.rt.interaction_envelope(w, 'decision_answer', actor, esc['thread_ts'], now,
                                                                role=role))
                db.execute("UPDATE decision_proposals SET status='rejected',decided_by=?,decided_at=?,rejected_event=? "
                           'WHERE id=?', (actor, now, event, pid))
        current = self.rt._proposal(db, pid)
        if current['message_stage'] != 'message_updated':
            self._update_proposal_message(db, bot_token, card_channel, current, model, text=self.rt.buttons.CANCELLED)
        current = self.rt._proposal(db, pid)
        return {'ok': True, 'answer': 'no', 'duplicate': duplicate, 'proposal_id': pid, 'status': 'rejected',
                'event_id': p['event_id'], 'actor': current['decided_by'], 'rejected_event': current['rejected_event'],
                'message_stage': current['message_stage']}

    def smoke_escalation(self, db, db_path, session, provider, cwd, question, now=None, options=None, recommend=None, footer=None,
                         live_card=False):
        """TEST card in the decision channel with its own watch; no source message, no other watch. `footer`: a detail
        (e.g. the dashboard link of a TEST rule check) after the TEST note. Dry by default (no real Slack card / cleanup);
        `live_card` posts a real TEST card to the PRIVATE decision channel for receiver E2E."""
        now = time.time() if now is None else now
        uuid.UUID(session)
        provider = self.rt.claude.provider({'provider': provider})
        question = ' '.join(str(question).split())
        if not question or len(question) > 500:
            raise ValueError('question_must_be_1_to_500_characters')
        options = self.rt.buttons.validate_options(options, recommend)
        if not Path(cwd).expanduser().is_dir():
            raise ValueError('task_cwd_missing')
        config = self.rt.buttons.load_config()
        channel = config['decision_channel']  # never ooo.channel, never delegates
        # P38 (F49): a live TEST card meets the destination guard before any row, so a refused one leaves no 'sending'
        # orphan; _post_card takes this verdict. A dry card asks no Slack at all.
        verdict = (self.rt.buttons.require_private_destination(self.rt, db, channel, 'smoke_escalation', config=config)
                   if live_card else None)
        user_token, bot_token = self.rt.api_identity(), self.rt.api_identity(bot=True)
        eid = hashlib.sha256(f'smoke/{uuid.uuid4()}'.encode()).hexdigest()[:24]
        client_id = str(uuid.uuid4())
        with self.rt.transaction(db):
            db.execute('INSERT INTO escalations VALUES (?,?,?,?,?)', (eid, 'sending', None, None, client_id))
            db.execute('INSERT INTO escalation_cards (event_id,channel,options,never_auto,created,smoke) '
                       'VALUES (?,?,?,?,?,1)', (eid, channel, json.dumps(options, ensure_ascii=False), 0, now))
        text = f'🧪 TEST · <@{self.owner_slack_user}> {self.rt.buttons.escape(question)}'
        ts = self._post_card(db, eid, channel, text, options, client_id, user_token, bot_token,
                        self.rt.agent_metadata('escalation_card', {'session': session, 'provider': provider, 'cwd': cwd},
                                       event_id=eid, smoke='true'),
                        footer=self.msg('escalation.smoke_escalation.1')
                               + (f' · {footer}' if footer else ''), config=config, smoke=True, live_card=live_card,
                        verdict=verdict)
        child = self.rt.register(db, session=session, provider=provider, cwd=cwd, channel=channel, thread=ts,
                         peers=[self.owner_slack_user], mode='decision', since=ts, now=now, registry_note=False)
        with self.rt.transaction(db):
            child = self.rt.load(db, child['id'])
            child.update(smoke=True, label='TEST smoke-escalation')
            if not live_card:
                child['dry'] = True  # a dry TEST watch is never polled against Slack (fetch short-circuits)
            self.rt.save(db, child)
            # Synthetic, already-handled source event: it is never delivered to anyone.
            db.execute('INSERT OR IGNORE INTO events (id,watch_id,channel,ts,author,digest,received,status,disposition) '
                       'VALUES (?,?,?,?,?,?,?,?,?)', (eid, child['id'], channel, ts, self.bot_user,
                                                       hashlib.sha256(b'smoke-escalation').hexdigest(), now, 'handled', 'smoke_test'))
            db.execute('UPDATE escalations SET stage=?,watch_id=? WHERE event_id=?', ('watching', child['id'], eid))
        self.rt.start(db_path)
        return {'ok': True, 'test': True, 'dry': not live_card, 'event_id': eid, 'watch_id': child['id'], 'channel': channel,
                'thread_ts': ts, 'session': session, 'provider': provider,
                'cleanup': None if not live_card else f'slack_followup.py smoke-cleanup --event-id {eid}'}

    def release_settled_claims(self, db, now=None, dry_run=False):
        """Idempotent reconciliation: close only open claims whose events are already handled.

        Safe at install: one local transaction, no Slack/agent calls, never touches events
        or claims of unhandled (claimed/queued) events; a second run releases 0.
        """
        with self.rt.transaction(db):
            rows = db.execute('SELECT c.event_id, c.released_at, e.status FROM claude_claims c '
                              'LEFT JOIN events e ON e.id=c.event_id').fetchall()
            open_rows = [r for r in rows if r['released_at'] is None]
            ids = [r['event_id'] for r in open_rows if r['status'] == 'handled']
            still_open = {}
            for r in open_rows:
                if r['status'] != 'handled':
                    key = r['status'] or 'event_missing'
                    still_open[key] = still_open.get(key, 0) + 1
            if not dry_run:
                self.rt.claude.settle_claims(db, ids, 'settled_backfill', now)
        return {'ok': True, 'dry_run': dry_run, 'claims_total': len(rows),
                'already_released': len(rows) - len(open_rows), 'open_before': len(open_rows),
                'released': 0 if dry_run else len(ids), 'would_release': len(ids),
                'open_after': len(open_rows) - (0 if dry_run else len(ids)), 'left_open_by_event_status': still_open}

    def _demote_foreign_current(self, db, card_task, provider, session, now):
        """P39: a foreign poster that is the task's current session (made so by a pre-P39 ask-<owner key> from the orchestrator)
        hands `current` back to the task's own latest run session, or — the task never ran on its own — clears it (the
        next dispatch of the task starts it fresh). Audited. Returns the new current (None when cleared / unchanged)."""
        row = db.execute('SELECT current_provider, current_session FROM tasks WHERE task_id=?', (card_task,)).fetchone()
        if not row or (row['current_provider'], row['current_session']) != (provider, session):
            return None
        own = db.execute('SELECT provider, session FROM consumer_runs WHERE task=? AND session IS NOT NULL AND NOT '
                         '(provider=? AND session=?) ORDER BY started_at DESC LIMIT 1', (card_task, provider, session)).fetchone()
        new = (own['provider'], own['session']) if own else (None, None)
        with self.rt.transaction(db):
            db.execute("UPDATE tasks SET current_provider=?,current_session=?,current_source='p39_foreign_poster',"
                       'updated_at=? WHERE task_id=?', (*new, now, card_task))
            self.rt.registry.audit(db, card_task, 'p39_foreign_poster_demoted', {'provider': provider, 'session': session},
                           {'provider': new[0], 'session': new[1]}, f'ask-{self.key}', None, now)
        return new if own else None

    @_owner_kwargs
    def ask_owner(self, db, db_path, session, provider, cwd, question, options=None, recommend=None, never_auto=False,
                 lang='zh', now=None, trusted_owner=False, priority='normal', owner_only=False, kind=_OWNER_DEFAULT,
                 watch_extra=None, smoke=False, registry_note=True, footer=None, lint=True, task_thread=True,
                 parent_event=None, live_card=False, poster_cwd=None, about=None, replaces=False, gate=None):
        """Agent-initiated decision card with no inbound Slack event (owner session only).

        Like smoke-escalation but real: an assistant card mentioning the owner (the owner-only audience),
        its own decision watch owned by the calling session and a synthetic, already-handled
        source event, so decide / propose-decision / confirm-decision and normal provider-aware
        delivery work unchanged. `trusted_owner` is only for the dispatcher (end-of-run questions)
        and the PreToolUse hook, after they verified the dispatched run's session; never the CLI.
        P3.1 blocker cards (code that verified the reporting session first): `<owner key>_only` keeps the card in the decision
        channel without making it never-auto, `kind` changes the metadata kind, `watch_extra` marks the decision watch
        (blocker id, secret input), `smoke` makes a 🧪 TEST card that smoke-cleanup accepts, `registry_note=False` leaves
        the task's current session alone (supervisor posts). P3.1 cards: the question must meet the copy standard
        (decision_buttons.check_copy; `lint=False` only for code relaying text it already scrubbed), `footer` adds a detail
        (e.g. the dashboard link) to the task line under the buttons.
        P39: `poster_cwd` (the CLI's own cwd) detects a session posting about ANOTHER task (`_foreign_poster`): the
        decision watch is then owned by that task's consumer (never made current by this post), so the decision and
        the owner's thread replies reach the task the card is about; the poster may still close the card.
        P41 `about` (--about: a card event id / card ts / Slack link in the decision channel, or a PR link): a card about
        another card threads under that card's root (parent_event) and also shows in the channel (reply_broadcast); an agent
        card still waiting for the owner is refused (answer in its thread, or ask-<owner key>-close it first: never two live button sets
        for one question). The footer links the card or PR.
        P46: a card whose buttons are already retired (a plan_change reply) no longer counts as waiting; `replaces`
        (rule-check --about) takes over a waiting agent card of the same task: its buttons retire with SUPERSEDED_LINE.
        P47 `gate` (the agent's card fields, from the ask-<owner key> CLI; None for code-built cards): card_gate checks them before
        any write and renders the decision, situation and checked lines (and a PR's / infra apply's parts) above the
        question; the task's open-card limit (R-0008) applies.
        """
        kind = f'ask_{self.key}' if kind is _OWNER_DEFAULT else kind
        now = time.time() if now is None else now
        uuid.UUID(session)
        provider = self.rt.claude.provider({'provider': provider})
        if not trusted_owner and self.rt.claude.actor() != (provider, session):
            raise ValueError(f'ask_{self.key}_requires_the_calling_owner_session_identity')
        cwd = str(Path(cwd).expanduser().absolute())
        if not Path(cwd).is_dir():
            raise ValueError('task_cwd_missing')
        if lang not in self.rt.asks.LANGS:
            raise ValueError('lang_must_be_zh_or_en')
        question = str(question or '').strip()
        decide = None
        if gate is not None:
            shown = self.rt.card_gate.check(self.rt, gate, text=f'{question} {about or ""}')
            question, decide = self.rt.card_gate.compose(shown, question), shown['decide']
        if not question or len(question) > 1500:
            raise ValueError('question_must_be_1_to_1500_characters')
        self.rt.asks.check_secret_free(question)
        if lang == 'zh' and not CJK_RE.search(decide or question):
            raise ValueError(f'{self.key}_facing_question_must_be_written_in_chinese_or_pass_lang_en_explicitly')
        if lint:
            self.rt.buttons.check_copy(question)
        options = self.rt.buttons.validate_options(options, recommend)
        warnings = [f'option_label_not_chinese:{o["id"]}' for o in options if lang == 'zh' and not CJK_RE.search(o['label'])]
        if len(question.split('\n')) > 3 and gate is None:
            warnings.append(f'{self.key}_facing_question_more_than_3_lines')
        config = self.rt.buttons.load_config()  # malformed config fails before any Slack write
        ref, waiting = self.rt.continuation(db, about, self.rt.registry.task_id_for_cwd(cwd), replaces, config)
        if gate is not None:
            self.rt.card_gate.open_limit(self.rt, db, self.rt.registry.task_id_for_cwd(cwd), waiting and waiting['event_id'], now)
        parent_event = ref.get('event_id') or parent_event
        if ref:
            footer = ' · '.join(x for x in (footer, self.msg('escalation.ask_owner.1', link=ref['link']) if ref.get('event_id') else
                                            f'<{ref["pr"]}|PR #{ref["number"]}>') if x)
        route = self.rt.buttons.card_route(config, now, never_auto or owner_only)
        verdict = None
        if not smoke or live_card:  # P38 (F49): a refused card writes no row (_post_card takes this verdict, no 2nd look)
            verdict = self.rt.buttons.require_private_destination(self.rt, db, route['channel'], kind, config=config)
        user_token, bot_token = self.rt.api_identity(), self.rt.api_identity(bot=True)
        eid = hashlib.sha256(f'ask_{self.key}/{uuid.uuid4()}'.encode()).hexdigest()[:24]
        client_id = str(uuid.uuid4())
        with self.rt.transaction(db):
            db.execute('INSERT INTO escalations VALUES (?,?,?,?,?)', (eid, 'sending', None, None, client_id))
            db.execute('INSERT INTO escalation_cards (event_id,channel,options,never_auto,created,smoke,about) '
                       'VALUES (?,?,?,?,?,?,?)', (eid, route['channel'], json.dumps(options, ensure_ascii=False),
                                                  int(bool(never_auto)), now, int(bool(smoke)),
                                                  ref.get('event_id') or ref.get('pr')))
        task_id = self.rt.registry.task_id_for_cwd(cwd)
        if priority not in ('normal', 'high'):
            raise ValueError('priority_must_be_normal_or_high')
        text = getattr(self.rt, f'ask_{self.key}_text')(provider, session, task_id, question, lang, route['delegates'], priority)
        owner = {'session': session, 'provider': provider, 'cwd': cwd}
        watch_owner, foreign = (provider, session), False
        if not trusted_owner and not smoke and self.rt._foreign_poster(db, provider, session, task_id, poster_cwd):
            foreign = True
            self._demote_foreign_current(db, task_id, provider, session, now)
            consumer = self.rt.registry.task_consumer(db, task_id)
            if not consumer:  # the task never ran on its own: <owner key>-inbox owns the card (card_threads routes it there too)
                try:
                    consumer = self.rt.registry.task_consumer(db, self.rt.worker_config.load()[0]['intake']['inbox_task'])
                except Exception:  # noqa: BLE001
                    consumer = None
            if consumer:  # nothing at all (no inbox either): the poster keeps it, as before P39
                watch_owner = (self.rt.claude.provider({'provider': consumer[0] or 'codex'}), consumer[1])
        # P36: an independent ask is its own topic root; `parent_event` (a blocker stage-2) threads under the originating
        # card. A threaded never-auto / high-priority card also broadcasts to the channel. Smoke cards never thread (they
        # are standalone TEST cards, cleaned up on their own).
        ts = self._post_card(db, eid, route['channel'], text, options, client_id, user_token, bot_token,
                        self.rt.agent_metadata(kind, owner, event_id=eid, task_id=task_id, smoke='true' if smoke else None),
                        kind=kind, footer=getattr(self.rt, f'ask_{self.key}_footer')(provider, session, task_id, lang, footer,
                                                          self.rt.session_footer(db, task_id, provider, session, footer)),
                        task_id=task_id,
                        # P38: a card waiting for a verification code (secret_input) never joins or opens an approvals
                        # thread: its own secret watch keeps the P3.1 code path (no text hashed, context masked).
                        task_thread=task_thread and not smoke and not (watch_extra or {}).get('secret_input'),
                        broadcast=bool(never_auto) or priority == 'high' or bool(ref.get('event_id')),
                        hint=self.rt.buttons.HINT if (watch_extra or {}).get('secret_input') else None,
                        parent_event=parent_event, now=now, config=config, smoke=smoke, live_card=live_card,
                        verdict=verdict)
        if waiting:  # P46: the old card's buttons go once the new card stands (best effort, like a lapsed card)
            self.rt.card_replies.retire(self.rt, db, waiting, {'ts': None}, now, line=self.superseded_line)
        child = self.rt.register(db, session=watch_owner[1], provider=watch_owner[0], cwd=cwd, channel=route['channel'], thread=ts,
                         peers=[self.owner_slack_user], mode='decision', since=ts, now=now,
                         registry_note=registry_note and not smoke and not foreign)
        with self.rt.transaction(db):
            child = self.rt.load(db, child['id'])
            child.update(origin=f'ask_{self.key}', label=f'ask-{self.key}')
            if foreign:
                child.update(posted_by={'provider': provider, 'session': session}, card_task=task_id)
            if smoke and not live_card:
                child['dry'] = True  # a dry TEST watch is never polled against Slack (fetch short-circuits)
            child.update(watch_extra or {})
            if smoke:
                child['smoke'] = True
            if route['delegates']:
                child.update(delegates=route['delegates'], delegate_window=route['window'])
            self.rt.save(db, child)
            # Synthetic, already-handled source event (the card itself): never delivered.
            db.execute('INSERT OR IGNORE INTO events (id,watch_id,channel,ts,author,digest,received,status,disposition) '
                       'VALUES (?,?,?,?,?,?,?,?,?)', (eid, child['id'], route['channel'], ts, self.bot_user,
                                                       hashlib.sha256(f'ask-{self.key}'.encode()).hexdigest(), now, 'handled', f'ask_{self.key}'))
            db.execute('INSERT OR IGNORE INTO event_sources VALUES (?,?)', (eid, ts))
            self.rt.envelopes.store(db, eid, self.rt.envelopes.envelope('agent_message', 'agent', 'agent', 'none', f'ask_{self.key}_source', ts,
                                                        0, {'provider': provider, 'session': session, 'task_cwd': cwd}), now)
            db.execute('UPDATE escalations SET stage=?,watch_id=? WHERE event_id=?', ('watching', child['id'], eid))
        self.rt.start(db_path)
        return {'ok': True, 'event_id': eid, 'watch_id': child['id'], 'channel': route['channel'], 'thread_ts': ts,
                'session': session, 'provider': provider, 'task_id': task_id, 'options': options,
                **({'routed_to': {'task_id': task_id, 'provider': watch_owner[0], 'session': watch_owner[1]},
                    'note': 'card about another task: its decision and replies go to that task, not to this session'}
                   if foreign else {}),
                'never_auto': bool(never_auto), 'ooo': route['ooo'], 'delegates': route['delegates'], 'lang': lang,
                'warnings': warnings, **({'about': ref} if ref else {}),
                'next': (self.msg('escalation.ask_owner.next')),
                'close': f'slack_followup.py ask-{self.key}-close --event-id {eid}'}

    def supervisor_card(self, db, question, options, *, never_auto=True, smoke=False, kind='supervisor_card', refs=None, now=None,
                        footer=None, task_id=None, live_card=False, body_blocks=None, hint=None, parent_event=None,
                        broadcast=False, first_line_max=_OWNER_DEFAULT):
        """Code-owned decision card (P3 routine confirmations): an assistant card to the owner with no owner session and no
        watch. The receiver records the click (`decide`, receipt, card update) but queues no session event; the
        supervisor settles the decision from SQLite. A card that was posted but failed readback is still returned
        (with `warning`) so it is never posted twice. P3.1: the owner's typed reply in its thread is polled by
        card_replies.settle (a clear 启用 / 不启用 decides; other text keeps the buttons). P37: `body_blocks` carries a rich
        Block Kit body (rule-distillation card); `hint` overrides the default code-owned hint; `question` stays the
        notification / readback fallback and must carry the the owner mention. P41: `parent_event` threads the card under that
        card's root (deadline card about a decided / closed card), `broadcast` also shows it in the channel; `first_line_max`
        None keeps a first line that carries a link whole."""
        first_line_max = self.rt.buttons.COPY_FIRST_LINE_MAX if first_line_max is _OWNER_DEFAULT else first_line_max
        now = time.time() if now is None else now
        question = str(question or '').strip()
        if not question or len(question) > 1500:
            raise ValueError('question_must_be_1_to_1500_characters')
        self.rt.asks.check_secret_free(question)
        question = self.rt.buttons.scrub_copy(question, first_line_max)  # code-built text (routines.yaml): drop ids, never refuse
        options = self.rt.buttons.validate_options(options)
        config = self.rt.buttons.load_config()  # malformed config fails before any Slack write
        route = self.rt.buttons.card_route(config, now, never_auto)
        verdict = None
        if not smoke or live_card:  # P38 (F49): a refused card writes no row, so a retrying caller leaves no orphans
            verdict = self.rt.buttons.require_private_destination(self.rt, db, route['channel'], kind, config=config)
        user_token, bot_token = self.rt.api_identity(), self.rt.api_identity(bot=True)
        eid = hashlib.sha256(f'{kind}/{uuid.uuid4()}'.encode()).hexdigest()[:24]
        client_id = str(uuid.uuid4())
        with self.rt.transaction(db):
            db.execute('INSERT INTO escalations VALUES (?,?,?,?,?)', (eid, 'sending', None, None, client_id))
            db.execute('INSERT INTO escalation_cards (event_id,channel,options,never_auto,created,smoke) VALUES (?,?,?,?,?,?)',
                       (eid, route['channel'], json.dumps(options, ensure_ascii=False), int(bool(never_auto)), now,
                        int(bool(smoke))))
        warning = None
        try:
            ts = self._post_card(db, eid, route['channel'], f'<@{self.owner_slack_user}> {self.rt.buttons.escape(question)}', options, client_id,
                            user_token, bot_token, self.rt.agent_metadata(kind, event_id=eid, **(refs or {})), kind=kind,
                            footer=footer, hint=hint or self.rt.buttons.code_owned_hint(self.rt.buttons.card_model(options)), task_id=task_id,
                            broadcast=broadcast, establish_root=False, parent_event=parent_event, now=now, config=config,
                            smoke=smoke, live_card=live_card,
                            body_blocks=body_blocks, verdict=verdict)
        except Exception as exc:
            row = db.execute('SELECT thread_ts FROM escalations WHERE event_id=?', (eid,)).fetchone()
            if not row['thread_ts']:
                raise
            ts, warning = row['thread_ts'], 'card_readback_failed:' + self.rt.safe_error(exc)
        with self.rt.transaction(db):
            db.execute('UPDATE escalations SET stage=? WHERE event_id=?', (self.rt.SUPERVISOR_STAGE, eid))
        return {'event_id': eid, 'thread_ts': ts, 'channel': route['channel'], 'warning': warning}

    def ask_owner_close(self, db, eid, now=None):
        """Owner stops its ask-<owner key> decision watch; refuses other cards and unresolved events."""
        now = time.time() if now is None else now
        card = db.execute('SELECT * FROM escalation_cards WHERE event_id=?', (eid,)).fetchone()
        esc = db.execute('SELECT * FROM escalations WHERE event_id=?', (eid,)).fetchone()
        if card is None or esc is None or not esc['watch_id']:
            raise ValueError(f'unknown_ask_{self.key}_card')
        w = self.rt.load(db, esc['watch_id'])
        if w.get('origin') != f'ask_{self.key}':
            raise ValueError(f'not_an_ask_{self.key}_card')
        if w.get('blocker_id'):  # P3.1: the supervisor closes blocker cards once resolve-blocker recorded the outcome
            raise ValueError('blocker_card_use_resolve-blocker')
        poster = w.get('posted_by') or {}
        # P41: also the running run of the task the card is about (a fresh session re-asking after a plan change, rule 3).
        if self.rt.claude.actor() not in ((self.rt.claude.provider(w), w['session']), (poster.get('provider'), poster.get('session'))) and \
                not (card['task_id'] and self.rt.registry.running_session(db, card['task_id']) == self.rt.claude.actor()):
            raise ValueError('current_session_must_match_watch_owner')
        decided = db.execute('SELECT 1 FROM decisions WHERE event_id=?', (eid,)).fetchone() is not None
        if w['state'] == 'stopped':
            return {'ok': True, 'duplicate': True, 'event_id': eid, 'watch_id': w['id'], 'decided': decided}
        if db.execute("SELECT 1 FROM events WHERE watch_id=? AND status!='handled'", (w['id'],)).fetchone():
            raise ValueError('resolve_pending_events_before_close')
        with self.rt.transaction(db):
            w = self.rt.load(db, esc['watch_id'])
            w.update(state='stopped', stop_reason=f'ask_{self.key}_closed', stopped_at=now)
            self.rt.save(db, w)
            closed = [dict(r) for r in db.execute("SELECT * FROM decision_proposals WHERE event_id=? AND status='pending'",
                                                  (eid,))]
            db.execute("UPDATE decision_proposals SET status='superseded',closed_reason='card_closed' "
                       "WHERE event_id=? AND status='pending'", (eid,))
            if self.rt.claude.provider(w) == 'claude':
                self.rt.claude.resolved(db, w['session'])
        card_stage = 'decided' if decided else None
        if not decided or closed:
            # Best effort, bot identity (the card is the owner-only): drop stale buttons.
            try:
                user_token, bot_token = self.rt.api_identity(), self.rt.api_identity(bot=True)
                model = self.rt.buttons.card_model(card['options'])
                if not decided:
                    message = self.rt.slack.api_call('conversations.replies', user_token, params={
                        'channel': card['channel'], 'ts': esc['thread_ts'], 'limit': 1})['messages'][0]
                    if message.get('ts') != esc['thread_ts'] or message.get('user') != self.bot_user:
                        raise RuntimeError('card_readback_failed')
                    line = {'type': 'context', 'block_id': self.rt.buttons.OUTCOME_BLOCK_ID, 'elements': [
                        {'type': 'mrkdwn', 'text': self.ask_owner_closed_line.format(clock=self.rt.asks.clock(now))}]}
                    self.rt.slack.api_call('chat.update', bot_token, json_body={
                        'channel': card['channel'], 'ts': esc['thread_ts'], 'text': message.get('text', ''),
                        'blocks': self.rt.buttons.decided_blocks(message.get('blocks'), line)})
                    card_stage = 'card_closed'
                for p in closed:
                    self._update_proposal_message(db, bot_token, card['channel'], p, model, status_line=self.ask_owner_proposal_closed)
            except Exception as exc:  # noqa: BLE001 - the watch is closed either way
                card_stage = 'card_update_failed:' + self.rt.safe_error(exc)
        return {'ok': True, 'duplicate': False, 'event_id': eid, 'watch_id': w['id'], 'decided': decided,
                'closed_proposals': [p['id'] for p in closed], 'card_stage': card_stage}

    def smoke_cleanup(self, db, eid, now=None):
        """Stop only a smoke-escalation watch and settle its leftovers; real cards are refused."""
        now = time.time() if now is None else now
        card = db.execute('SELECT * FROM escalation_cards WHERE event_id=?', (eid,)).fetchone()
        esc = db.execute('SELECT * FROM escalations WHERE event_id=?', (eid,)).fetchone()
        if card is None or esc is None or not card['smoke']:
            raise ValueError('not_a_smoke_escalation')
        with self.rt.transaction(db):
            w = self.rt.load(db, esc['watch_id']) if esc['watch_id'] else None
            if w is not None and not w.get('smoke'):
                raise ValueError('not_a_smoke_watch')
            settled = []
            if w is not None:
                settled = [r[0] for r in db.execute("SELECT id FROM events WHERE watch_id=? AND status!='handled'", (w['id'],))]
                db.execute("UPDATE events SET status='handled',disposition='irrelevant' WHERE watch_id=? AND status!='handled'",
                           (w['id'],))
                self.rt.claude.settle_claims(db, settled, 'smoke_cleanup', now)
                w.update(state='stopped', stop_reason='smoke_cleanup', stopped_at=now)
                self.rt.save(db, w)
                if self.rt.claude.provider(w) == 'claude':
                    self.rt.claude.resolved(db, w['session'])
            db.execute("UPDATE decision_proposals SET status='superseded',closed_reason='smoke_cleanup' "
                       "WHERE event_id=? AND status='pending'", (eid,))
        return {'ok': True, 'event_id': eid, 'watch_id': w['id'] if w else None, 'stopped': w is not None,
                'settled_events': settled, 'slack_card': f'{card["channel"]}/{esc["thread_ts"]} (left in Slack, labeled TEST)'}
