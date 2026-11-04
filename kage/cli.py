"""The command line (K14f): the argument tree of every command and what each one runs, the program name and the
owner-keyed command names from the profile. Every name it runs is reached through the runtime (`rt`); the
description is the deployment's (the hub's docstring).
"""
from __future__ import annotations
import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from kage import locale
from kage.core import control as _control




class CliCore:
    """The owner-bound half of the command line: the owner's Slack user, the bot, the program's description
    (the deployment's), the env and tool names, the words in the owner's language, and the runtime (`rt`)
    every command runs through."""

    def __init__(self, language, *, owner_key, owner_name, owner_slack_user, bot_user, dashboard_url, deployment, description, tail_command, tasks_root_env, worker_config_path, issue_tracker, rt):
        self.language = language
        self.owner_slack_user = owner_slack_user
        self.bot_user = bot_user
        self.dashboard_url = dashboard_url
        self.deployment = deployment
        self.description = description
        self.tail_command = tail_command
        self.tasks_root_env = tasks_root_env
        self.worker_config_path = worker_config_path
        self.issue_tracker = issue_tracker
        self.rt = rt
        self.key, self.name = owner_key, owner_name
        self.team = self.deployment.owner.slack_workspace
        self.app_id = self.deployment.owner.assistant_slack_app
        self.assistant = self.deployment.owner.assistant_name
        self.agent_event_type = self.deployment.instance.agent_marker

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def control_command(self, args, db, db_path):
        """Phase 2 C1: the control-* commands (kage.core.control, bound to the owner here)."""
        control = _control.Control(self.language, owner_key=self.key, owner_name=self.name)
        rt = self.rt
        if args.command == 'control-context':
            return {'ok': True, **control.context(rt, db, hours=args.hours)}
        if args.command == 'control-status':
            return control.status(rt, db)
        if args.command == 'control-bootstrap':
            return control.bootstrap(rt, db, db_path, launch=not args.no_launch, rewrite_goal=args.rewrite_goal)
        if args.command == 'control-push':  # C2: the Socket Mode receiver runs it without an agent identity
            if rt._agent_session():
                return rt._fail('agent_session_cannot_push_control_messages')
            return control.slack_push(rt, db, args.channel, args.ts, args.user)
        if args.command == 'control-poll':
            if rt._agent_session():
                return rt._fail('agent_session_cannot_poll_control_messages')
            return {'ok': True, **(control.slack_poll(rt, db, force=True) or {'skipped': 'control_off'})}
        if args.command == 'control-show':
            return control.show(rt, db, args.message)
        if args.command == 'control-reply':
            text = Path(args.file).read_text(encoding='utf-8') if args.file else args.text
            return control.reply(rt, db, db_path, args.message, text, needs_answer=args.needs_answer,
                                 followup=args.followup)
        if args.command == 'control-relay':
            return control.relay(rt, db, db_path, args.message, args.task, note=args.note)
        return control.launch(rt, db, db_path, args.message, args.title, linear=args.linear)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def main(self):
        os.umask(0o077)
        parser = argparse.ArgumentParser(description=self.description)
        parser.add_argument('--db', type=Path, default=self.rt.DEFAULT_DB)
        sub = parser.add_subparsers(dest='command', required=True)
        reg = sub.add_parser('register')
        reg.add_argument('--session-id')
        reg.add_argument('--provider', choices=self.rt.claude.PROVIDERS, default=None)
        reg.add_argument('--cwd', required=True)
        reg.add_argument('--channel', required=True)
        reg.add_argument('--thread-ts')
        reg.add_argument('--peer', action='append', default=[])
        reg.add_argument('--dm-channel')
        reg.add_argument('--mode', choices=self.rt.MODES, default='waiting')
        reg.add_argument('--since', required=True)
        reg.add_argument('--owner-pid', type=int)
        reg.add_argument('--duration-seconds', type=int,
                         help='Explicit fixed observation duration; resolve does not refresh it')
        reg.add_argument('--start', action='store_true')
        reg.add_argument('--scope', choices=['thread', 'channel'], default='thread')
        reg.add_argument('--lifecycle', choices=['idle', 'until_stop', 'sliding'], default='idle')
        reg.add_argument('--context-all', action='store_true')
        ctx = sub.add_parser('context')
        ctx.add_argument('--event', required=True)
        cont = sub.add_parser('continue-until-stop')
        cont.add_argument('--watch', required=True)
        cont.add_argument('--expected-session', required=True)
        cont.add_argument('--context-all', action='store_true')
        for command in ['status', 'poll', 'stop']:
            p = sub.add_parser(command)
            p.add_argument('--watch', required=command != 'status')
        sub.add_parser('start')
        sub.add_parser('run')
        res = sub.add_parser('resolve')
        res.add_argument('--event', required=True)
        res.add_argument('--disposition', choices=[*self.rt.MODES, 'irrelevant'], required=True)
        res.add_argument('--action-ts')
        esc = sub.add_parser('escalate')
        esc.add_argument('--event', required=True)
        esc.add_argument('--question-file', required=True)
        esc.add_argument('--holding-message-file', required=True,
                         help='Short holding reply in the source conversation language')
        esc.add_argument('--option', action='append', default=[], metavar='ID=LABEL[:KIND]',
                         help='Scenario option (repeatable, 2-4; KIND approve|reject|other). Replaces approve/reject')
        esc.add_argument('--recommend', metavar='ID', help='Mark one option as recommended')
        esc.add_argument('--never-auto', action='store_true',
                         help='Only configured approvers may decide; out-of-office delegates may not')
        dec = sub.add_parser('decide', help='Record a Slack button decision; only the Socket Mode receiver calls this')
        dec.add_argument('--event-id', required=True, help='Escalation event ID (the button value)')
        dec.add_argument('--choice', required=True, help='approve, reject or opt:<option id>')
        dec.add_argument('--actor', required=True, help='Slack user ID from the interactive payload')
        dec.add_argument('--card-channel', required=True)
        dec.add_argument('--card-ts', required=True)
        dec.add_argument('--response-url', help='Accepted from the receiver; never stored or printed')
        dec.add_argument('--note', help='Optional condition shown on the card status (max 300 chars)')
        # P39: the owner's card-thread replies — the Socket Mode receiver's realtime push, its gate, and the reconcile sweep.
        crc = sub.add_parser('card-reply-config', help='Receiver only: is the realtime card-reply push on, which channels '
                                                       'and authors to forward (no bodies)')
        crp = sub.add_parser('card-reply-push', help=f"Receiver only: route {self.name}'s card-thread reply (refs only; re-read and re-verified here) to the task the card is about")
        crp.add_argument('--channel', required=True)
        crp.add_argument('--ts', required=True, help="The reply's ts")
        crp.add_argument('--thread-ts', required=True, help='The card thread root')
        crp.add_argument('--user', required=True, help='The author from the event (re-checked against Slack)')
        cts = sub.add_parser('card-thread-sweep', help='Run the card-thread reconcile now (cards.reconcile_days); '
                                                       '--dry-run lists what it would route')
        cts.add_argument('--dry-run', action='store_true')
        cts.add_argument('--backfill-hours', type=float, help='Also replies typed this many hours before P39 first ran '
                                                              '(one-off catch-up; default: none)')
        ho = sub.add_parser('handoff', help='--event E --task T: hand ONE received message to the task it belongs to (P39); '
                                            '--watch W --session-id S --cwd C: move a watch to another session')
        ho.add_argument('--event', help='The event id you received (context --event)')
        ho.add_argument('--task', help='Target task id or workspace')
        ho.add_argument('--note', help='Optional one line for the receiving task (max 300 chars)')
        ho.add_argument('--watch')
        ho.add_argument('--session-id')
        ho.add_argument('--cwd')
        ho.add_argument('--owner-pid', type=int)
        ho.add_argument('--provider', choices=self.rt.claude.PROVIDERS)
        dbm = sub.add_parser('dm-bindings-migrate', help='P39 install: whole-DM bindings -> request-thread bindings '
                                                         '(dry run unless --apply)')
        dbm.add_argument('--apply', action='store_true')
        prop = sub.add_parser('propose-decision', help="Owner only: ask for one-tap confirmation of a typed decision")
        prop.add_argument('--event-id', required=True, help='Escalation event ID of the card')
        prop.add_argument('--choice', required=True, help="The card's option id (approve/reject on default cards)")
        prop.add_argument('--note', help='Condition/detail from the message, max 300 chars')
        prop.add_argument('--meaning', help=self.msg('cli.main.1', owner=self.name))
        prop.add_argument('--source-ts', required=True, help=f"{self.name}'s (or an OOO delegate's) message ts in the card thread")
        conf = sub.add_parser('confirm-decision', help='Record a confirmation click; only the Socket Mode receiver calls this')
        conf.add_argument('--proposal-id', required=True)
        conf.add_argument('--answer', choices=['yes', 'no'], required=True)
        conf.add_argument('--actor', required=True)
        conf.add_argument('--card-channel', required=True)
        conf.add_argument('--message-ts', required=True, help='Confirmation message ts')
        conf.add_argument('--response-url', help='Accepted from the receiver; never stored or printed')
        smoke = sub.add_parser('smoke-escalation', help='Synthesize a labeled TEST card with its own decision watch (dry)')
        smoke.add_argument('--session', required=True)
        smoke.add_argument('--provider', choices=self.rt.claude.PROVIDERS, required=True)
        smoke.add_argument('--cwd', required=True)
        smoke.add_argument('--question', required=True)
        smoke.add_argument('--option', action='append', default=[], metavar='ID=LABEL[:KIND]')
        smoke.add_argument('--recommend', metavar='ID')
        smoke.add_argument('--live-card', action='store_true',
                           help='Post a REAL TEST card to the PRIVATE decision channel (receiver E2E); default dry (no Slack post)')
        clean = sub.add_parser('smoke-cleanup', help='Stop a smoke-escalation watch; refuses real escalations')
        clean.add_argument('--event-id', required=True)
        asker = sub.add_parser('ask', help=f"Owner only: ask colleagues in {self.name}'s voice in a followed thread/DM")
        asker.add_argument('--channel')
        asker.add_argument('--thread')
        asker.add_argument('--dm', metavar='USER')
        asker.add_argument('--responder', action='append', default=[], required=True)
        asker.add_argument('--text', required=True)
        asker.add_argument('--option', action='append', default=[], metavar='ID=LABEL[:KIND]', required=True)
        asker.add_argument('--confirm-text', choices=self.rt.asks.CONFIRM_MODES, default='auto')
        asker.add_argument('--remind-after', default=self.rt.asks.DEFAULT_REMIND)
        asker.add_argument('--expire-after', default=self.rt.asks.DEFAULT_EXPIRE)
        asker.add_argument('--reminder-text')
        asker.add_argument('--lang', choices=self.rt.asks.LANGS, default='zh',
                           help='Language of the card\'s own words (hint, reminder, result); the responders\' latest '
                                'message here decides when there is one')
        asker.add_argument('--no-hint', action='store_true')
        answer = sub.add_parser('ask-answer', help='Record an ask-card click; only the Socket Mode receiver calls this')
        answer.add_argument('--card-id', required=True)
        answer.add_argument('--option', required=True)
        answer.add_argument('--actor', required=True)
        answer.add_argument('--channel', required=True)
        answer.add_argument('--message-ts', required=True)
        answer.add_argument('--actor-team')
        answer.add_argument('--response-url', help='Accepted from the receiver; never stored or printed')
        ask_prop = sub.add_parser('ask-propose', help='Owner only: map a typed answer to an ask-card option')
        ask_prop.add_argument('--card-id', required=True)
        ask_prop.add_argument('--option', required=True)
        ask_prop.add_argument('--note')
        ask_prop.add_argument('--source-ts', required=True)
        ask_conf = sub.add_parser('ask-confirm', help='Record an ask confirmation click; receiver only')
        ask_conf.add_argument('--proposal-id', required=True)
        ask_conf.add_argument('--answer', choices=['yes', 'no'], required=True)
        ask_conf.add_argument('--actor', required=True)
        ask_conf.add_argument('--channel', required=True)
        ask_conf.add_argument('--message-ts', required=True)
        ask_conf.add_argument('--actor-team')
        ask_conf.add_argument('--response-url', help='Accepted from the receiver; never stored or printed')
        smoke_ask_p = sub.add_parser('smoke-ask', help=f'Synthesize a TEST ask card ({self.name} as responder) with its own watch (dry)')
        smoke_ask_p.add_argument('--session', required=True)
        smoke_ask_p.add_argument('--provider', choices=self.rt.claude.PROVIDERS, required=True)
        smoke_ask_p.add_argument('--cwd', required=True)
        smoke_ask_p.add_argument('--text', required=True)
        smoke_ask_p.add_argument('--option', action='append', default=[], metavar='ID=LABEL[:KIND]')
        smoke_ask_p.add_argument('--confirm-text', choices=self.rt.asks.CONFIRM_MODES, default='auto')
        smoke_ask_p.add_argument('--remind-after', default=self.rt.asks.DEFAULT_REMIND, help='Smoke allows short values, e.g. 2m')
        smoke_ask_p.add_argument('--expire-after', default=self.rt.asks.DEFAULT_EXPIRE, help='Smoke allows short values, e.g. 5m')
        smoke_ask_p.add_argument('--lang', choices=self.rt.asks.LANGS, default='zh')
        smoke_ask_p.add_argument('--live-card', action='store_true',
                                 help='Post a REAL TEST ask card to the PRIVATE decision channel (receiver E2E); default dry')
        smoke_ask_clean = sub.add_parser('smoke-ask-cleanup', help='Stop a smoke-ask watch; refuses real ask cards')
        smoke_ask_clean.add_argument('--card-id', required=True)
        rec = sub.add_parser('recover')
        rec.add_argument('--watch', required=True)
        rec.add_argument('--new-session', action='store_true')
        rec.add_argument('--unrecoverable-reason')
        again = sub.add_parser('rearm')
        again.add_argument('--watch', required=True)
        again.add_argument('--mode', choices=self.rt.MODES, required=True)
        again.add_argument('--since', required=True)
        again.add_argument('--provider', choices=self.rt.claude.PROVIDERS)
        again.add_argument('--duration-seconds', type=int,
                           help='Explicit new fixed duration, required for rearming a fixed window')
        bind = sub.add_parser('bind-source')
        bind.add_argument('--watch', required=True)
        bind.add_argument('--source-ts', required=True,
                          help='Exact original message timestamp in the registered channel')
        waiter = sub.add_parser('claude-wait', help='Claim one SQLite batch; no Slack polling/model process')
        waiter.add_argument('--session-id', required=True)
        waiter.add_argument('--timeout', type=float, default=900)
        unclaim = sub.add_parser('claude-unclaim', help='Explicitly release a reconciled interrupted batch; never automatic')
        unclaim.add_argument('--session-id', required=True)
        unclaim.add_argument('--batch-id', required=True)
        unclaim.add_argument('--reason', required=True)
        hook = sub.add_parser('claude-hook')
        hook.add_argument('--bind', action='store_true')
        sub.add_parser('claude-hook-config', help='Print session-only native hook settings; does not install them')
        settle = sub.add_parser('claude-release-settled', help='Idempotently close claims whose events are already handled')
        settle.add_argument('--dry-run', action='store_true', help='Report counts without writing')
        owner = sub.add_parser(f'ask-{self.key}', help=f'Owner only: post an {self.assistant} decision card to {self.name} without an inbound event')
        owner.add_argument('--session', required=True)
        owner.add_argument('--provider', choices=self.rt.claude.PROVIDERS, required=True)
        owner.add_argument('--cwd', required=True)
        question_source = owner.add_mutually_exclusive_group(required=True)
        question_source.add_argument('--question', help=f'Chinese by default ({self.name}-facing): 1-3 short lines under the lines code renders from the P47 fields below (background, what each option means); no rule / check ids, hashes or "never-auto" (refused)')
        question_source.add_argument('--question-file')
        owner.add_argument('--option', action='append', default=[], metavar='ID=LABEL[:KIND]')
        owner.add_argument('--recommend', metavar='ID')
        owner.add_argument('--never-auto', action='store_true')
        owner.add_argument('--lang', choices=self.rt.asks.LANGS, default='zh', help=f'zh (default) for everything addressed to {self.name}')
        owner.add_argument('--about', help='P41: the card this one replaces or follows (its event id, ts or Slack link: the new '
                                          'card threads under it and also shows in the channel), or a GitHub PR link')
        self.rt.card_gate.add_args(owner)
        owner_close = sub.add_parser(f'ask-{self.key}-close', help=f'Owner only: stop an ask-{self.key} decision watch')
        owner_close.add_argument('--event-id', required=True)
        tasks_p = sub.add_parser('tasks', help='Task registry (task = workspace); routing unchanged in P0')
        tasks_sub = tasks_p.add_subparsers(dest='tasks_command', required=True)
        tasks_sub.add_parser('list')
        t_show = tasks_sub.add_parser('show')
        t_show.add_argument('--task', required=True)
        t_link = tasks_sub.add_parser('link-parent', help='Audited: record parent/child in SQLite (authoritative)')
        t_link.add_argument('--task', required=True)
        t_link.add_argument('--parent', required=True)
        t_link.add_argument('--reason')
        t_cur = tasks_sub.add_parser('set-current', help='Audited: override the current session of a task')
        t_cur.add_argument('--task', required=True)
        t_cur.add_argument('--provider', choices=self.rt.claude.PROVIDERS, required=True)
        t_cur.add_argument('--session', required=True)
        t_cur.add_argument('--reason')
        t_sync = tasks_sub.add_parser('sync-parent-lines', help='Compare goal.md parent: mirrors with SQLite')
        t_sync.add_argument('--dry-run', action='store_true', required=True)
        t_back = tasks_sub.add_parser('backfill', help='Fill missing task rows/current sessions from watches')
        t_back.add_argument('--dry-run', action='store_true')
        shadow = sub.add_parser('routing-shadow-report', help='Task-addressed vs actual delivery targets')
        shadow.add_argument('--since', help='Epoch, ISO-8601 with timezone, or a duration such as 3d')
        cfg = sub.add_parser('config', help=f'Digital-worker config ({self.worker_config_path})')
        cfg_sub = cfg.add_subparsers(dest='config_command', required=True)
        cfg_sub.add_parser('show')
        cfg_validate = cfg_sub.add_parser('validate')
        cfg_validate.add_argument('--file')
        cfg_set = cfg_sub.add_parser('set', help='Validate, write atomically and append to the change log')
        cfg_set.add_argument('key')
        cfg_set.add_argument('value', help='JSON value, or a bare string')
        cfg_set.add_argument('--who', help='Recorded in the change log; defaults to the agent session or local user')
        # ---- P1a consumer guarantee ----
        live_p = sub.add_parser('liveness', help='Read-only: is a session live_idle|live_busy|dead|unknown?')
        live_p.add_argument('--provider', choices=self.rt.claude.PROVIDERS, required=True)
        live_p.add_argument('--session', required=True)
        launch_p = sub.add_parser('launch-task', help='Start the FIRST session of a task headless (P2 intake)')
        launch_p.add_argument('--task', required=True, help='Task workspace directory')
        launch_p.add_argument('--provider', choices=['auto', *self.rt.claude.PROVIDERS], default='auto')
        launch_p.add_argument('--category', help='Routing category for --provider auto (infra, docs, ui, ...)')
        launch_p.add_argument('--important', action='store_true', help='Claude uses model_important')
        launch_p.add_argument('--prompt-file', required=True)
        launch_p.add_argument('--parent', help='Parent task id (audited link, like tasks link-parent)')
        launch_p.add_argument('--from-card', help='Escalation event of the card this task is launched from; the task joins '
                                                  "that card's approvals thread (P3.2)")
        launch_p.add_argument('--allow-existing', action='store_true', help='Start even if the task has a current session')
        launch_p.add_argument('--wait-seconds', type=float, default=90.0)
        dstat = sub.add_parser('dispatch-status', help='Dispatches, consumer runs, leases and pauses')
        dstat.add_argument('--task')
        dstat.add_argument('--limit', type=int, default=20)
        for name, choices in (('dispatch-control', ['stop', 'resume', 'iterm', 'view']),
                              ('dispatch-retry', ['auto', 'codex', 'claude'])):
            p = sub.add_parser(name, help='Receiver only: run/failure card buttons')
            p.add_argument('--dispatch-id', required=True)
            p.add_argument('--action' if name == 'dispatch-control' else '--mode', required=True, choices=choices)
            p.add_argument('--actor', required=True)
            p.add_argument('--channel', required=True)
            p.add_argument('--message-ts', required=True)
            p.add_argument('--response-url', help='Accepted from the receiver; never stored or printed')
        take_p = sub.add_parser('takeover', help=f"P43, {self.name}'s terminal only (fsess take): stop + hold a background session")
        back_p = sub.add_parser('handback', help=f"P43, {self.name}'s terminal only (fsess back): release it; held events resume")
        for p in (take_p, back_p):
            p.add_argument('--task', required=True, help='Task id, task workspace directory or session id (unique prefix)')
        take_p.add_argument('--wait', type=float, default=20.0, help='Seconds to wait for the run to end (default 20)')
        take_p.add_argument('--force', action='store_true', help='SIGKILL a run still alive after --wait')
        brk = sub.add_parser('breaker-decide', help='Receiver only: agent-pair circuit breaker card')
        brk.add_argument('--breaker-id', required=True)
        brk.add_argument('--choice', choices=['continue', 'stop'], required=True)
        brk.add_argument('--actor', required=True)
        brk.add_argument('--channel', required=True)
        brk.add_argument('--message-ts', required=True)
        brk.add_argument('--response-url', help='Accepted from the receiver; never stored or printed')
        dhook = sub.add_parser('dispatch-hook', help='PreToolUse hook of dispatched Claude runs (reads hook JSON on stdin)')
        dhook.add_argument('--run-id', required=True)
        note_p = sub.add_parser('note', help='Leave a note for a task; wakes its current session unless you are it')
        note_p.add_argument('--task', required=True, help='Task id or workspace directory')
        note_src = note_p.add_mutually_exclusive_group(required=True)
        note_src.add_argument('--text')
        note_src.add_argument('--file')
        send_p = sub.add_parser('send-to-task', help='Agent-to-agent message (never authority); revival + breaker apply')
        send_p.add_argument('--task', required=True, help='Target task id or workspace directory')
        send_src = send_p.add_mutually_exclusive_group(required=True)
        send_src.add_argument('--text')
        send_src.add_argument('--file')
        send_p.add_argument('--from-task', help='Sender task id; defaults to the task of the current directory')
        guard = sub.add_parser('memory-guard', help='status.md rev header: check before editing, bump after')
        guard.add_argument('action', choices=['check', 'bump'])
        guard.add_argument('--task', required=True, help='Task id or workspace directory')
        guard.add_argument('--expect-rev', type=int)
        crep = sub.add_parser('cache-report', help='P40 read-only prompt-cache report of dispatched runs: first-call input, '
                                                   'cache read / write, cold-start rate, durations (numbers only)')
        crep.add_argument('--task', help='Only this task id')
        crep.add_argument('--since', type=float, default=24.0, help='Hours back (default 24)')
        crep.add_argument('--provider', choices=self.rt.claude.PROVIDERS)
        crep.add_argument('--limit', type=int, default=50, help='Newest runs listed one by one (default 50; 0 = summary only)')
        tail_p = sub.add_parser('tail', help=f'{self.tail_command}: print/follow the readable log of a dispatched run')
        tail_p.add_argument('target', help='Run id, dispatch id, task id or task workspace directory')
        tail_p.add_argument('--raw', action='store_true', help='Masked raw provider stream instead of the readable log')
        tail_p.add_argument('--no-follow', action='store_true', help='Print what exists now and exit')
        tail_p.add_argument('--poll', type=float, default=1.0, help=argparse.SUPPRESS)
        smoke_d = sub.add_parser('smoke-dispatch', help='TEST task under /tmp + dead current session + synthetic event')
        smoke_d.add_argument('--provider', choices=self.rt.claude.PROVIDERS, required=True)
        smoke_d.add_argument('--scenario', choices=['revive', 'fallback'], default='revive')
        smoke_d.add_argument('--no-wait', action='store_true')
        smoke_d.add_argument('--timeout', type=float, default=900.0)
        # ---- P2 intake (收件处 <owner key>-inbox / <owner key>-oncall) ----
        sub.add_parser('intake-status', help='Intake cadence, API budget, backoff, bursts and filters (local only)')
        probe = sub.add_parser('intake-probe', help="Read-only: run each discovery search once; counts by conversation type")
        probe.add_argument('--query', action='append', default=[], help='Extra/alternative query (repeatable, max 6)')
        ictx = sub.add_parser('intake-context', help='Burst messages read live from Slack + handling guards (burst task only)')
        ictx.add_argument('--burst', required=True, help='Burst id or its intake event id')
        irec = sub.add_parser('intake-reconcile',
                              help=f'P3.3: re-read the conversation since the trigger ({self.name} / recipient later messages) before replying as {self.name} or carding {self.name}')
        irec.add_argument('--burst', required=True, help='Burst id or its intake event id')
        tri = sub.add_parser('triage-record', help='Structured triage record for a burst (inbox / oncall session)')
        tri.add_argument('--burst', required=True)
        tri.add_argument('--intent', required=True, choices=self.rt.intake.INTENTS)
        tri.add_argument('--clarity', required=True, choices=self.rt.intake.CLARITIES)
        tri.add_argument('--disposition', required=True, choices=self.rt.intake.DISPOSITIONS)
        tri.add_argument('--target-task')
        tri.add_argument('--confidence', type=float)
        tri.add_argument('--basis', help='Rule id or person field the decision relied on (one line, <=200 chars)')
        tri.add_argument(f'--needs-{self.key}', action='store_true')
        tri.add_argument('--note', help='One line, <=300 chars, never a quote of the message')
        tcor = sub.add_parser('triage-correct', help=f"{self.name}'s correction of a triage record (never an agent session)")
        tcor.add_argument('--record', required=True)
        tcor.add_argument('--disposition', choices=self.rt.intake.DISPOSITIONS)
        tcor.add_argument('--note')
        il = sub.add_parser('intake-launch', help='New task from a burst: bootstrap --no-launch, bind source, launch, ack')
        il.add_argument('--from-burst', required=True)
        il.add_argument('--title', required=True)
        il.add_argument('--brief-file', required=True)
        il.add_argument('--provider', choices=['auto', *self.rt.claude.PROVIDERS], default='auto')
        il.add_argument('--category', help='infra / debug / dev -> Codex, docs / design / ui -> Claude (routing.categories)')
        il.add_argument('--important', action='store_true')
        il.add_argument('--linear', action='store_true', help=f'Also create a {self.issue_tracker} issue (English title and brief)')
        il.add_argument('--no-ack', action='store_true', help=self.msg('cli.main.2'))
        il.add_argument('--ack-text', help=self.msg('cli.main.3'))
        il.add_argument('--again', action='store_true',
                        help=f'P38 (F29): ack even though this burst was already answered as {self.name} (else the ack is skipped)')
        il.add_argument('--decision-event', help=self.msg('cli.main.4', owner=self.name))
        il.add_argument('--parent', help='Parent task id (audited link)')
        il.add_argument('--slug', help='ASCII folder slug (required for titles without ASCII words)')
        il.add_argument('--wait-seconds', type=float, default=90.0)
        fw = sub.add_parser('intake-forward', help='Hand a burst to an existing task (agent message, never authority)')
        fw.add_argument('--from-burst', required=True)
        fw.add_argument('--task', required=True)
        fw_src = fw.add_mutually_exclusive_group()
        fw_src.add_argument('--text')
        fw_src.add_argument('--file')
        fw.add_argument('--no-bind', action='store_true', help='Do not route later messages of this conversation there')
        fw.add_argument('--decision-event')
        for name, helptext in (('intake-brief', f'Strategic/vague directive: brief card to {self.name} only, never an external post'),
                               ('intake-draft', f'Show {self.name} a draft reply on a card (approve before intake-reply)'),
                               ('intake-reply', f'Answer as {self.name} in the burst conversation (strategic: approved draft only)')):
            x = sub.add_parser(name, help=helptext)
            x.add_argument('--from-burst', required=True)
            x.add_argument('--file', required=True)
            if name == 'intake-reply':
                x.add_argument('--decision-event', help=self.msg('cli.main.5', owner=self.name))
                x.add_argument('--again', action='store_true',
                               help=f'P38 (F29): a deliberate further message to a burst that was already answered as {self.name}')
                x.add_argument('--relevant', action='store_true',
                               help=f'P46.3 (R-0013): a message {self.key}-inbox handed you does answer your ask (checked)')
        follow = sub.add_parser('intake-follow', help='Watch the burst conversation from this inbox/oncall session')
        follow.add_argument('--from-burst', required=True)
        inc = sub.add_parser('oncall-incident', help=f'Big incident: incident task + high-priority ask-{self.key} card')
        inc.add_argument('--from-burst', required=True)
        inc.add_argument('--title', required=True)
        inc.add_argument('--brief-file', required=True)
        inc.add_argument('--summary', required=True, help='Chinese, <=800 chars, shown on the card')
        inc.add_argument('--provider', choices=['auto', *self.rt.claude.PROVIDERS], default='auto')
        inc.add_argument('--category', default='infra')
        inc.add_argument('--slug')
        inc.add_argument('--wait-seconds', type=float, default=90.0)
        boot = sub.add_parser('intake-bootstrap', help=f'Idempotent: create/register {self.key}-inbox + {self.key}-oncall and bind them')
        boot.add_argument('--no-launch', action='store_true', help='Do not start a first session for a task without one')
        boot.add_argument('--rewrite-policy', action='store_true', help='Rewrite goal.md from the current policy text')
        boot.add_argument('--wait-seconds', type=float, default=90.0)
        smoke_i = sub.add_parser('smoke-intake', help='TEST: synthetic burst through the live intake pipeline (no Slack read)')
        smoke_i.add_argument('--scenario', choices=['request', 'strategic', 'alert'], default='request')
        smoke_i.add_argument('--no-wait', action='store_true')
        smoke_i.add_argument('--timeout', type=float, default=900.0)
        people_p = sub.add_parser('people-set-field', help='Set a neutral handling field in a COPY of the people registry')
        people_p.add_argument('--file', required=True, help='The people.json copy to edit (never defaults to the real file)')
        people_p.add_argument('--person', required=True, help='Person id or Slack user id')
        people_p.add_argument('--field', required=True, choices=self.rt.intake.HANDLING_FIELDS)
        people_p.add_argument('--value', required=True)
        # ---- P3 rule engine, weekly rule learning, routines ----
        rc = sub.add_parser('rule-check', help='Check an access / account plan against the active rules (recorded)')
        rc.add_argument('--plan', required=True, help='Plan JSON file (references/rules.md)')
        rc.add_argument('--card', action='store_true', help=f'Post the one_tap / {self.key} decision card (agent session only)')
        rc.add_argument('--summary', help=self.msg('cli.main.6', owner=self.name))
        rc.add_argument('--category', default='access', choices=self.rt.rules.CATEGORIES)
        rc.add_argument('--approved-by', help=f"P46, with --card: Slack link or CHANNEL/TS of {self.name}'s own typed message in a card thread of this task that already states this action: it approves this plan (no second card) unless the plan cannot be undone or is escalated for more than an unknown role (then the card goes into that thread)")
        rc.add_argument('--about', help='P46, with --card: the card this one continues (event id, ts or Slack link): the '
                        'card threads under it and also shows in the channel')
        self.rt.card_gate.add_args(rc, decide=False)
        rstat = sub.add_parser('rule-status', help='Approval of a check: verified card decision, bound to the plan hash')
        rstat.add_argument('--check', required=True)
        rstat.add_argument('--plan', help='The plan you are about to execute; must hash to the checked plan')
        rout = sub.add_parser('rule-outcome', help='Record executed | failed | abandoned for a check')
        rout.add_argument('--check', required=True)
        rout.add_argument('--result', required=True, choices=self.rt.rules.RESULTS)
        rout.add_argument('--note', help='One line, <=300 chars, no quotes of messages')
        rcor = sub.add_parser('rule-correct', help=f"{self.name}'s verdict on what a check should have said (never an agent)")
        rcor.add_argument('--check', required=True)
        rcor.add_argument('--outcome', required=True, choices=self.rt.rules.OUTCOMES)
        rcor.add_argument('--note')
        rls = sub.add_parser('rules', help='Rules spec: show | validate | diff | backtest | install | inbox-sync | proposals')
        rls_sub = rls.add_subparsers(dest='rules_command', required=True)
        r_show = rls_sub.add_parser('show')
        r_show.add_argument('--category', default='access', choices=self.rt.rules.CATEGORIES)
        r_show.add_argument('--rule', help='One rule id, e.g. AR-13')
        r_show.add_argument('--registry', action='store_true', help='P47: the rule registry as a Chinese list')
        rls_sub.add_parser('lint', help='P47: check the rule registry (the live one, else the seed)').add_argument(
            '--dir', help='A registry directory instead')
        rls_sub.add_parser('validate').add_argument('--file', help='A spec file instead of the active rules dir')
        r_diff = rls_sub.add_parser('diff', help='vN or a file; default: previous version -> active')
        r_diff.add_argument('--category', default='access', choices=self.rt.rules.CATEGORIES)
        r_diff.add_argument('--from', dest='from_ref')
        r_diff.add_argument('--to', dest='to_ref')
        r_bt = rls_sub.add_parser('backtest', help='Cases with recorded facts through the engine; aggregates by default')
        r_bt.add_argument('--cases', required=True)
        r_bt.add_argument('--spec', help='Spec file (default: the active version)')
        r_bt.add_argument('--summary-out', help='Write the aggregate JSON here')
        r_bt.add_argument('--details-out', help='PRIVATE per-case results (0600)')
        r_bt.add_argument('--record', action='store_true', help='Keep the aggregate for the /rules dashboard page')
        r_inst = rls_sub.add_parser('install', help=f'{self.name} only (no agent identity, never a dispatched run): install a validated spec version and make it active; P47: or one registry rule file (R-NNNN.json), linted with the whole registry')
        r_inst.add_argument('--file', required=True)
        r_inst.add_argument('--who', help='Recorded in the changelog')
        r_inst.add_argument('--rollback', action='store_true', help='Allow activating an older version')
        rls_sub.add_parser('inbox-sync', help=f'Add the code-managed P3 policy / addenda blocks to the {self.key}-inbox goal.md')
        rls_sub.add_parser('proposals').add_argument('--status')
        rrd = sub.add_parser('rules-review-data', help='Weekly review input: aggregates and ids only, never message text')
        rrd.add_argument('--since', default='7d')
        rprop = sub.add_parser('rule-propose', help='Weekly proposals -> one card each; code applies adopted ones')
        rprop.add_argument('--file', required=True)
        rtn = sub.add_parser('routines', help='routines.yaml per task: list | validate | run-now | disable | enable | proposals')
        rtn_sub = rtn.add_subparsers(dest='routines_command', required=True)
        rtn_sub.add_parser('list').add_argument('--task')
        rtn_val = rtn_sub.add_parser('validate').add_mutually_exclusive_group()
        rtn_val.add_argument('--task')
        rtn_val.add_argument('--file')
        for name in ('run-now', 'disable', 'enable'):
            x = rtn_sub.add_parser(name, help=f'{self.name} only (never an agent session)')
            x.add_argument('--task', help='Task id or workspace (default: the current directory)')
            x.add_argument('--id', required=True)
        rtn_sub.add_parser('proposals')
        rpp = sub.add_parser('routine-propose', help=self.msg('cli.main.7'))
        rpp.add_argument('--task', help='Task id or workspace (default: the current directory)')
        rpp.add_argument('--id', required=True)
        rpp_when = rpp.add_mutually_exclusive_group(required=True)
        rpp_when.add_argument('--schedule', help=f'5-field cron, local time ({self.deployment.owner.timezone})')
        rpp_when.add_argument('--every', help='15m .. 30d')
        rpp_prompt = rpp.add_mutually_exclusive_group(required=True)
        rpp_prompt.add_argument('--prompt')
        rpp_prompt.add_argument('--prompt-file')
        rpp.add_argument('--scope', required=True, help='What the routine may do (its authority boundary)')
        rpp.add_argument('--until', help='YYYY-MM-DD or ISO with offset')
        rpp.add_argument('--provider', choices=self.rt.claude.PROVIDERS, help='Provider for a fresh launch when the task has no session')
        smr = sub.add_parser('smoke-rules', help='TEST: synthetic plans per outcome, or a TEST proposal in a temp rules dir')
        smr.add_argument('--scenario', choices=['outcomes', 'proposal'], default='outcomes')
        smr.add_argument('--card', action='store_true', help='Post 🧪 TEST cards (owner: --session/--provider or yours)')
        smr.add_argument('--session')
        smr.add_argument('--provider', choices=self.rt.claude.PROVIDERS)
        smr.add_argument('--timeout', type=float, default=900.0)
        smrt = sub.add_parser('smoke-routines', help='TEST routine on a temp smoke task; fires within two supervisor cycles')
        smrt.add_argument('--provider', choices=self.rt.claude.PROVIDERS, required=True)
        smrt.add_argument('--once', action='store_true', help='Only the first fire (fresh launch), no second fire (revival)')
        smrt.add_argument('--no-wait', action='store_true')
        smrt.add_argument('--timeout', type=float, default=900.0)
        # ---- P3.1 capability blockers and the automation gap ledger ----
        rb = sub.add_parser('report-blocker', help=f'Agent: a capability blocker -> one {self.assistant} card to {self.name} (deduplicated)')
        rb.add_argument('--task', help='Task id or workspace (default: the current directory)')
        rb.add_argument('--system', required=True, help='Short slug, e.g. authentik, aws-console, google-workspace')
        rb.add_argument('--operation', required=True, help='What could not be done (<=160 chars; Chinese is fine)')
        rb.add_argument('--type', required=True, choices=self.rt.blockers.TYPES + self.rt.blockers.REMOVED_TYPES)
        rb.add_argument('--tried', '--checked', dest='tried', action='append', required=True,
                        help='What you tried / looked at (Chinese, no secrets), once per check (P47 R-0003: 2+ for '
                             'auth_missing / auth_expired / permission or an Unauthorized / 403 / timeout); <=500 in all')
        rb.add_argument('--unblock', required=True, help='What would unblock it (Chinese, <=500 chars)')
        rb.add_argument('--option', action='append', default=[], metavar='ID=LABEL',
                        help='Replace the default options (2-4); ID: ' + '|'.join(self.rt.blockers.OPTION_RESOLUTION))
        rb.add_argument('--recommend', metavar='ID')
        self.rt.card_gate.add_args(rb, checked=False)
        rv = sub.add_parser('resolve-blocker', help=f'After read-back verification (agent), or by {self.name}')
        rv.add_argument('--id', required=True)
        rv.add_argument('--how', required=True, choices=self.rt.blockers.RESOLUTIONS)
        rv.add_argument('--note', help='One line, <=300 chars; never a code or a secret')
        gaps_p = sub.add_parser('gaps', help='Automation gap ledger: list | show (tried / unblock text only in show)')
        gaps_sub = gaps_p.add_subparsers(dest='gaps_command', required=True)
        gl = gaps_sub.add_parser('list')
        gl.add_argument('--status', default='all', choices=['all', 'unresolved', 'open', 'in_progress', 'resolved', 'superseded'])
        gl.add_argument('--task')
        gl.add_argument('--system')
        gl.add_argument('--limit', type=int, default=100)
        gaps_sub.add_parser('show').add_argument('--id', required=True)
        gprop = sub.add_parser('gap-propose', help='Weekly review: skill proposals for frequent gaps -> one card each')
        gprop.add_argument('--file', required=True)
        bcode = sub.add_parser('blocker-code', help=f'Run CMD with the verification code {self.name} replied in a blocker card thread; the code is never printed or stored')
        bcode.add_argument('--id', required=True)
        bcode.add_argument('--event', help=f"The event of {self.name}'s reply (default: his newest reply, at most 30 minutes old)")
        bcode.add_argument('--stdin', action='store_true', help="Code on the command's stdin")
        bcode.add_argument('--env', metavar='NAME', help='Code in this environment variable of the command')
        bcode.add_argument('--timeout', type=float, default=120.0)
        bcode.add_argument('cmd', nargs=argparse.REMAINDER, help='-- CMD [ARGS...]; an argument {code} is replaced')
        smb = sub.add_parser('smoke-blocker', help='TEST: blocker card on a TEST task, dedupe, synthetic click / reply, '
                                                   'resolve, gap row, /gaps')
        smb.add_argument('--provider', choices=self.rt.claude.PROVIDERS, default='claude', help='Synthetic TEST owner session')
        smb.add_argument('--dashboard', default=self.rt.blockers.DASHBOARD_URL, help='Local dashboard to check /api/gaps on')
        smb.add_argument('--live-card', action='store_true',
                         help='Post the two REAL TEST cards to the PRIVATE decision channel (receiver E2E); default dry')
        td = sub.add_parser('task-deadline', help='Register / list a task deadline; the watchdog cards before it is due '
                                                  '(P42: a --wake deadline wakes the task at due instead)')
        td_sub = td.add_subparsers(dest='deadline_command', required=True)
        td_reg = td_sub.add_parser('register', help='Record a due date + one-line note for a task')
        td_reg.add_argument('--task', help='Task id or workspace (default: the current directory)')
        td_reg.add_argument('--due', required=True, help='+Nh | +Nd | +Nw, or YYYY-MM-DD[THH:MM] (Shanghai)')
        td_reg.add_argument('--note', required=True, help='Your own one-line note, <=160 chars, no secrets (dashboard only, '
                                                          'never on the card; with --wake it is the woken run\'s brief)')
        td_kind = td_reg.add_mutually_exclusive_group(required=True)
        td_kind.add_argument('--ask', help=self.msg('cli.main.8', owner=self.name))
        td_kind.add_argument('--wake', action='store_true',
                             help=f'P42: your OWN checkpoint (verify an output, check a status, re-run later): at due the task is woken with --note as its brief (no card to {self.name}, no new authority); >= 5 min ahead; tasks.max_wakes per tasks.wake_window_hours, then it is a card to {self.name}')
        td_reg.add_argument('--about', help=f'P41: the card the deadline is about (event id, ts or Slack link: while it still waits for {self.name} the reminder goes into its thread, no new buttons), or a GitHub PR link')
        td_reg.add_argument('--lead-hours', type=int, help='Card this many hours before due (default tasks.deadline_lead_hours)')
        td_list = td_sub.add_parser('list')
        td_list.add_argument('--task')
        td_list.add_argument('--status', default='all',
                             choices=['all', 'pending', 'carded', 'reminded', 'woken', 'lapsed', 'handled', 'handed',
                                      'deferred', 'cancelled'])
        td_sub.add_parser('cancel').add_argument('--id', required=True)
        sub.add_parser('task-sweep', help='Run the orphan sweep + deadline watchdog once (the supervisor does this on a timer)')
        hlth = sub.add_parser('health', help=f'P38 daemon health: provider status and window counts, stranded deliveries, stuck claims, refused {self.name}-facing posts (the supervisor checks every 30 s)')
        hlth.add_argument('--sweep', action='store_true', help='Run one stranded / stuck-claim / provider-health / '
                                                               'refused-card re-post pass first')
        # P38 (F49): one-off install check of every owner-facing destination (posts nothing; the assistant's conversations.info).
        cdc = sub.add_parser('card-destination-check', help=f'Install check: are the configured and every stored {self.name}-facing card channel private / an IM per Slack? Exit 1 if a configured one or one used in the last 30 days is not (older ones are listed as legacy)')
        cdc.add_argument('--refresh', action='store_true', help='Ignore cached verdicts and ask Slack again')
        # ---- O1a: read-only installation checks ----
        doc = sub.add_parser('doctor', help='Onboarding: read-only installation checks (tokens and identities, the private card '
                                            'destination, state, services, agent CLIs, clock, secrets in the deployment); '
                                            'posts nothing, changes nothing')
        doc.add_argument('--offline', action='store_true', help='No Slack call (skips the two auth.test reads)')
        doc.add_argument('--json', action='store_true', help='The report as JSON')
        man = sub.add_parser('manifest', help='Onboarding: print the Slack app manifest for this deployment (paste it at '
                                              'api.slack.com/apps) and the steps after it; reads nothing, posts nothing')
        man.add_argument('--as-owner', action='store_true', help='Add the user-token scopes (speak as the owner, read the '
                                                                  "owner's DMs, search)")
        man.add_argument('--name', help="The app's name (default: the assistant's display name)")
        # ---- P35 learn rules from the owner's card feedback ----
        idis = sub.add_parser('interaction-distill', help=f"Handling session: fill the one-line distillation of {self.name}'s intent for an interaction (raw text stays in the private store)")
        idis.add_argument('--event', required=True, help='The resolved card event (escalation event id)')
        idis.add_argument('--intent', required=True, help=f'<=200 chars: the handling {self.name} wants for THIS shape (no secrets)')
        idis_dir = idis.add_mutually_exclusive_group()
        idis_dir.add_argument('--directive', dest='directive', action='store_true', default=None,
                              help=self.msg('cli.main.9'))
        idis_dir.add_argument('--no-directive', dest='directive', action='store_false')
        idis.add_argument('--feedback-file', help=f"A file with {self.name}'s raw reply; redacted + secret-scanned into the private store (never the DB / argv)")
        idis.add_argument('--scenario', help=self.msg('cli.main.10'))
        # P37: the re-dispatched <owner key>-inbox session's output for a drafting rule card.
        rdft = sub.add_parser('rule-draft', help='Drafting session: post a rich rule-distillation card from a concrete '
                                                 'candidate rule, or --no-rule to log and post no card')
        rdft.add_argument('--promote', required=True, help='The drafting promote-card id (pc…) from the dispatch brief')
        for fld, hlp in (('title', self.msg('cli.main.11')), ('scenario', self.msg('cli.main.12')),
                         ('default', self.msg('cli.main.13')), ('override', self.msg('cli.main.14')),
                         ('rule', self.msg('cli.main.15')), ('impact', self.msg('cli.main.16'))):
            rdft.add_argument('--' + fld, help=hlp)
        rdft.add_argument('--basis-count', type=int, help=self.msg('cli.main.17'))
        rdft.add_argument('--no-rule', action='store_true', help='Cannot articulate a concrete rule: log + no card')
        rdft.add_argument('--reason', help='With --no-rule: one line on why no rule could be drafted')
        lrev = sub.add_parser('learning-review', help='Read-only feedback-learning aggregates and ids (no raw text): '
                                                      'overall, or same-shape history for a distillation')
        lrev.add_argument('--shape', help='Only this shape (the 12-hex shape key); default: the weekly review bundle')
        # P38 (F60): the redraft brief carries only the card id; the owner's guidance is read here from the private corpus.
        lrev.add_argument('--promote', help=f"One rule card (pc…): the draft it redrafts + {self.name}'s redraft guidance from the private corpus (for its drafting session)")
        lrev.add_argument('--since', default='7d', help='Window for the weekly bundle (default 7d)')
        # P39: rename long-lived tasks (task id == workspace folder). Dry run by default; --apply needs the supervisor down.
        trn = sub.add_parser('task-rename', help='Rename a task id + its workspace folder in ONE transaction (dry run unless '
                                                 '--apply; backup, old-name symlink, reversible with --map NEW=OLD)')
        trn.add_argument('--map', action='append', required=True, metavar='OLD=NEW', help='Repeatable')
        trn.add_argument('--apply', action='store_true', help='Write (default: report what would change)')
        tasks_root = self.deployment.instance.workspace_root  # the deployment's (K15d: no folder name in Kage)
        trn.add_argument('--root', help=f'Tasks root (default: {self.tasks_root_env} or the '
                                         f'{tasks_root.name if tasks_root else "tasks"} root)')
        trn.add_argument('--backup-dir', help='Default: <db dir>/backups')
        trn.add_argument('--update-config', action='store_true',
                         help='Set intake.inbox_task / intake.oncall_task (and dispatch.long_lived_tasks) still naming OLD')
        trn.add_argument('--no-symlink', action='store_true', help='Do not leave <root>/OLD -> NEW')
        smfl = sub.add_parser('smoke-feedback-learning', help='TEST: synthetic strong signal -> drafting -> rich TEST card '
                                                              '-> redraft -> TEST card -> a TEST rule proposal (temp rules)')
        smfl.add_argument('--card', action='store_true', help='Post REAL TEST rule-distillation cards to the private channel')
        smfl.add_argument('--session')
        smfl.add_argument('--provider', choices=self.rt.claude.PROVIDERS)
        # ---- Phase 2 C1: the owner's control entry (kage.core.control); the run commands refuse outside the control
        # task's run that holds the message ----
        cctx = sub.add_parser('control-context', help='Control run: one digest of the work (tasks, runs, cards, blockers, intake)')
        cctx.add_argument('--hours', type=int, default=24, choices=range(1, 169), metavar='1-168')
        cshow = sub.add_parser('control-show', help='Control run: the held owner message and its conversation')
        crep = sub.add_parser('control-reply', help=f'Control run: answer {self.name} (once; --followup after a relay)')
        crel = sub.add_parser('control-relay', help=f"Control run: pass {self.name}'s message to a task (the runtime "
                                                    'attaches the stored words and their source)')
        clau = sub.add_parser('control-launch', help=f"Control run: new work as a new task, {self.name}'s words as its brief")
        for p in (cshow, crep, crel, clau):
            p.add_argument('--message', required=True, help='The control message id (cm…)')
        crep_src = crep.add_mutually_exclusive_group(required=True)
        crep_src.add_argument('--text')
        crep_src.add_argument('--file')
        crep.add_argument('--needs-answer', action='store_true', help=f'The reply asks {self.name} something')
        crep.add_argument('--followup', action='store_true', help='A later reply (a relay\'s outcome), at most 3')
        crel.add_argument('--task', required=True, help='Target task id or workspace directory')
        crel.add_argument('--note', help='One line of your own (no authority): why this task')
        clau.add_argument('--title', required=True)
        clau.add_argument('--linear', action='store_true', help='Create a tracker ticket for the new task')
        sub.add_parser('control-status', help='The control entry: switch, task, messages by state')
        cpush = sub.add_parser('control-push', help=f"Receiver only: {self.name}'s new top-level message in the card "
                                                    'channel (refs only; read back and verified here)')
        cpush.add_argument('--channel', required=True)
        cpush.add_argument('--ts', required=True)
        cpush.add_argument('--user', required=True, help='The author from the event (re-checked against Slack)')
        sub.add_parser('control-poll', help='Run the card-channel fallback poll now (control.channel_poll_seconds)')
        cboot = sub.add_parser('control-bootstrap', help='Idempotent: create/register the control task and bind it')
        cboot.add_argument('--no-launch', action='store_true')
        cboot.add_argument('--rewrite-goal', action='store_true')
        args = parser.parse_args()
        if args.command == 'people-set-field':
            # Never opens the state DB; prints only the changed field, never <owner key>_view.
            print(json.dumps(self.rt.intake.people_set_field(args.file, args.person, args.field, args.value), ensure_ascii=False,
                             indent=2))
            return
        if args.command == 'intake-probe':
            print(json.dumps(self.rt.intake.intake_probe(self.rt, args.query), ensure_ascii=False, indent=2))
            return
        if args.command == 'config':
            if args.config_command == 'show':
                result = self.rt.worker_config.show()
            elif args.config_command == 'validate':
                result = self.rt.worker_config.check(args.file)
            else:
                result = self.rt.worker_config.set_value(args.key, args.value, args.who or self.rt.registry.actor_label(self.rt.claude.actor()))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get('ok'):
                sys.exit(1)
            return
        if args.command == 'liveness':
            print(json.dumps(self.rt.liveness.Probe().check(args.provider, args.session), indent=2))
            return
        if args.command == 'rules' and args.rules_command == 'show' and args.registry:
            print(self.rt.rule_registry.show_text())  # P47: the Chinese list, for the owner
            return
        if args.command == 'rules' and args.rules_command in ('show', 'validate', 'diff', 'install', 'lint'):
            # Rules-dir only: these never open the state database.
            if args.rules_command == 'lint':
                result = self.rt.rule_registry.lint(args.dir)
            elif args.rules_command == 'show':
                result = self.rt.rules.rules_show(args.category, args.rule)
            elif args.rules_command == 'validate':
                result = self.rt.rules.rules_validate(args.file)
            elif args.rules_command == 'diff':
                result = self.rt.rules.rules_diff(args.category, args.from_ref, args.to_ref)
            else:
                result = self.rt.rules.install_cli(self.rt, args.file, args.who, args.rollback)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get('ok'):
                sys.exit(1)
            return
        db_path = args.db.expanduser().resolve()
        if args.command == 'task-rename':
            # P39: opens the database itself — read-only for the dry run, after taking the supervisor lock for --apply.
            result = self.rt.task_rename.run(self.rt, db_path, args.map, apply=args.apply, root=args.root, backup_dir=args.backup_dir,
                                     update_config=args.update_config, symlink=not args.no_symlink)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get('ok'):
                sys.exit(1)
            return
        if args.command == 'cache-report':
            # P40, read-only like <tail_command>: never creates or migrates a database; only numbers leave the run logs.
            if not db_path.is_file():
                print(json.dumps({'ok': False, 'error': 'state_database_missing'}), file=sys.stderr)
                sys.exit(2)
            if not 0 < args.since <= 24 * 90 or args.limit < 0:
                print(json.dumps({'ok': False, 'error': 'since_must_be_0_to_2160_hours_and_limit_0_or_more'}), file=sys.stderr)
                sys.exit(2)
            db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                result = self.rt.cache_report.report(db, args.since, args.task, args.provider, args.limit)
            finally:
                db.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == 'tail':
            # Read-only: <tail_command> never creates or migrates a database (a wrong --db fails instead).
            if not db_path.is_file():
                print(f'{self.tail_command}: state_database_missing', file=sys.stderr)
                sys.exit(2)
            db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                code = self.rt.run_tail(db, db_path, args.target, raw=args.raw, follow=not args.no_follow, poll=max(0.2, args.poll))
            except KeyboardInterrupt:
                code = 130
            except BrokenPipeError:
                code = 0
            except ValueError as exc:
                print(f'{self.tail_command}: {exc}', file=sys.stderr)
                code = 2
            sys.exit(code)
        if args.command == 'manifest':
            from kage.onboard import manifest
            print(manifest.render(args.name or self.assistant, as_owner=args.as_owner))
            return
        if args.command == 'doctor':
            # O1a, read-only like cache-report: never creates or migrates a database; two auth.test reads at most.
            from kage.onboard import doctor
            db = None
            if db_path.is_file():
                db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=10)
                db.row_factory = sqlite3.Row
            try:
                try:
                    health = self.rt.health_report(db) if db is not None else None
                except Exception:  # noqa: BLE001 - the runtime checks are then reported as skipped
                    health = None
                tokens = {'bot': self.rt.slack.default_bot_token, 'user': self.rt.slack.default_user_token}
                report = doctor.run(self.deployment.owner, probe=doctor.Probe(transport=self.rt.slack), tokens=tokens,
                                    db_path=db_path, db=db, health=health, profile_dir=self.deployment.directory,
                                    offline=args.offline)
            finally:
                if db is not None:
                    db.close()
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else doctor.text(report))
            sys.exit(0 if report['ok'] else 1)
        db = self.rt.connect(db_path)
        if args.command.startswith('control-'):
            result = self.control_command(args, db, db_path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get('ok', True):
                sys.exit(1)
            return
        if args.command == 'dispatch-hook':
            self.rt.dispatch_hook(db, db_path, args.run_id)
            return
        if args.command == 'claude-hook-config':
            print(json.dumps(self.rt.claude.hook_config(db_path, Path(self.rt.__file__).resolve(), sys.executable), indent=2))
            return
        if args.command == 'claude-hook':
            self.rt.claude_hook(db, db_path, args.bind)
            return
        if args.command == 'claude-wait':
            batch = self.rt.claude.wait(db, args.session_id, args.timeout)
            if not batch:
                sys.exit(124)
            print(self.rt.claude_batch_prompt(db, batch, db_path), flush=True)
            return
        if args.command == 'claude-unclaim':
            result = self.rt.claude.release(db, args.session_id, args.batch_id, args.reason)
            self.rt.claude.publish_wakes(db, db_path)
        elif args.command == 'register':
            provider = args.provider or self.rt.claude.actor()[0]
            session = args.session_id or (self.rt.claude.actor()[1] if self.rt.claude.actor()[0] == provider else None)
            if not session:
                raise ValueError('explicit_session_id_required')
            owner_pid = args.owner_pid or (self.rt.discover_owner(session) if provider == 'codex' else None)
            result = self.rt.register(db, session=session, provider=provider, cwd=args.cwd, channel=args.channel,
                              thread=args.thread_ts, peers=args.peer, mode=args.mode, since=args.since,
                              dm=args.dm_channel, owner_pid=owner_pid, owner_birth=self.rt.pid_birth(owner_pid),
                              duration_seconds=args.duration_seconds, lifecycle=args.lifecycle,
                              scope=args.scope, context_all=args.context_all)
            if args.start:
                result['started_pid'] = self.rt.start(db_path)
        elif args.command == 'context':  # O1c2: a local event (a note, an agent message) needs no Slack identity
            local = self.rt.local_message(db, args.event) is not None
            result = self.rt.event_context(db, args.event, None if local else self.rt.api_identity())
        elif args.command == 'continue-until-stop':
            result = self.rt.continue_until_stop(db, args.watch, args.expected_session, args.context_all)
        elif args.command == 'run':
            self.rt.run_loop(db_path)
            return
        elif args.command == 'start':
            result = {'pid': self.rt.start(db_path)}
        elif args.command == 'poll':
            self.rt.tick(db, args.watch, str(db_path))
            result = self.rt.load(db, args.watch)
        elif args.command == 'resolve':
            result = self.rt.resolve(db, args.event, args.disposition, args.action_ts)
            if result['state'] == 'running':
                self.rt.start(db_path)
        elif args.command == 'escalate':
            question = Path(args.question_file).read_text().strip()
            if not question or len(question) > 1500:
                raise ValueError('question_must_be_1_to_1500_characters_and_secret_free')
            holding = Path(args.holding_message_file).read_text().strip()
            result = self.rt.escalation(db, args.event, question, db_path, holding, args.option, args.never_auto, args.recommend)
        elif args.command in ('decide', 'confirm-decision'):
            if args.command == 'decide':
                result = self.rt.decide(db, args.event_id, args.choice, args.actor, args.card_channel, args.card_ts, note=args.note)
            else:
                result = self.rt.confirm_decision(db, args.proposal_id, args.answer, args.actor, args.card_channel, args.message_ts)
            if result.get('ok') and not result.get('duplicate'):
                try:
                    self.rt.start(db_path)  # usually the launchd poller is already alive
                except Exception:
                    result['poller_start'] = 'failed'
        elif args.command in ('card-reply-config', 'card-reply-push'):
            if self.rt._agent_session():
                result = self.rt._fail('agent_session_cannot_push_card_replies')
            elif args.command == 'card-reply-config':
                result = self.rt.card_threads.receiver_config(self.rt, db)
            else:
                result = self.rt.card_threads.push(self.rt, db, args.channel, args.ts, args.thread_ts, args.user)
        elif args.command == 'card-thread-sweep':
            result = self.rt.card_threads.sweep(self.rt, db, force=True, dry_run=args.dry_run,
                                        backfill_seconds=args.backfill_hours * 3600 if args.backfill_hours else None)
            result = {'ok': not (result or {}).get('error'), **(result or {})}
        elif args.command == 'handoff' and args.event:
            if not args.task or args.watch:
                raise ValueError('handoff_takes_--event_and_--task_or_--watch_--session-id_--cwd')
            result = self.rt.intake.handoff(self.rt, db, db_path, args.event, args.task, args.note)
        elif args.command == 'dm-bindings-migrate':
            result = self.rt.intake.dm_bindings_migrate(self.rt, db, apply=args.apply)
        elif args.command == 'propose-decision':
            result = self.rt.propose_decision(db, args.event_id, args.choice, args.source_ts, args.note, meaning=args.meaning)
        elif args.command == 'smoke-escalation':
            result = self.rt.smoke_escalation(db, db_path, args.session, args.provider, args.cwd, args.question,
                                      options=args.option, recommend=args.recommend, live_card=args.live_card)
        elif args.command == 'smoke-cleanup':
            result = self.rt.smoke_cleanup(db, args.event_id)
        elif args.command == 'claude-release-settled':
            result = self.rt.release_settled_claims(db, dry_run=args.dry_run)
        elif args.command == f'ask-{self.key}':
            question = Path(args.question_file).read_text(encoding='utf-8') if args.question_file else args.question
            result = getattr(self.rt, f'ask_{self.key}')(db, db_path, args.session, args.provider, args.cwd, question, args.option, args.recommend,
                              args.never_auto, args.lang, poster_cwd=os.getcwd(), about=args.about,
                              gate=self.rt.card_gate.fields(args))
        elif args.command == f'ask-{self.key}-close':
            result = getattr(self.rt, f'ask_{self.key}_close')(db, args.event_id)
        elif args.command == 'tasks':
            actor = self.rt.claude.actor()
            if args.tasks_command == 'list':
                result = self.rt.registry.list_tasks(db)
            elif args.tasks_command == 'show':
                result = self.rt.registry.show_task(db, args.task)
            elif args.tasks_command == 'sync-parent-lines':
                result = self.rt.registry.sync_parent_lines(db, dry_run=args.dry_run)
            elif args.tasks_command == 'backfill':
                if args.dry_run:
                    result = self.rt.registry.backfill(db, dry_run=True)
                else:
                    with self.rt.transaction(db):
                        result = self.rt.registry.backfill(db)
            else:
                with self.rt.transaction(db):
                    if args.tasks_command == 'link-parent':
                        result = self.rt.registry.link_parent(db, args.task, args.parent, self.rt.registry.actor_label(actor),
                                                      session=actor[1], reason=args.reason)
                    else:
                        result = self.rt.registry.set_current(db, args.task, args.provider, args.session,
                                                      self.rt.registry.actor_label(actor), args.reason)
        elif args.command == 'routing-shadow-report':
            result = self.rt.registry.shadow_report(db, args.since)
        elif args.command == 'launch-task':
            result = self.rt.launch_task(db, db_path, args.task, args.provider,
                                 Path(args.prompt_file).read_text(encoding='utf-8'), important=args.important,
                                 category=args.category, parent=args.parent, allow_existing=args.allow_existing,
                                 wait_seconds=args.wait_seconds, from_card=args.from_card, persist=True)
        elif args.command == 'dispatch-status':
            result = self.rt.make_dispatcher(db, db_path).status(args.task, args.limit)
        elif args.command in ('dispatch-control', 'dispatch-retry', 'breaker-decide'):
            if args.command == 'breaker-decide':
                result = self.rt.dispatch_action(db, db_path, 'breaker', args.breaker_id, args.choice, args.actor, args.channel,
                                         args.message_ts)
            else:
                choice = args.action if args.command == 'dispatch-control' else {
                    'auto': 'retry', 'codex': 'retry_codex', 'claude': 'retry_claude'}[args.mode]
                result = self.rt.dispatch_action(db, db_path, 'dispatch', args.dispatch_id, choice, args.actor, args.channel,
                                         args.message_ts)
            if result.get('ok') and not result.get('duplicate') and result.get('action') not in ('iterm', 'view'):
                try:
                    self.rt.start(db_path)
                except Exception:
                    result['poller_start'] = 'failed'
        elif args.command in ('takeover', 'handback'):
            try:
                result = (self.rt.takeover(db, db_path, args.task, args.wait, args.force) if args.command == 'takeover' else
                          self.rt.handback(db, db_path, args.task))
            except ValueError as exc:
                print(f'{args.command}: {exc}', file=sys.stderr)
                sys.exit(2)
            with contextlib.suppress(Exception):
                self.rt.start(db_path)  # the supervisor finalizes / restarts what changed
            if result.get('still_running'):
                print(json.dumps(result, ensure_ascii=False, indent=2))
                sys.exit(3)
        elif args.command in ('note', 'send-to-task'):
            text = Path(args.file).read_text(encoding='utf-8') if args.file else args.text
            result = (self.rt.note(db, db_path, args.task, text) if args.command == 'note' else
                      self.rt.send_to_task(db, db_path, args.task, text, args.from_task))
        elif args.command == 'memory-guard':
            result = self.rt.memory_guard(db, args.action, args.task, args.expect_rev)
            if not result.get('ok'):
                print(json.dumps(result, ensure_ascii=False, indent=2))
                sys.exit(1)
        elif args.command == 'smoke-dispatch':
            result = self.rt.smoke_dispatch(db, db_path, args.provider, args.scenario, wait=not args.no_wait, timeout=args.timeout)
        elif args.command == 'intake-status':
            result = self.rt.intake.intake_status(self.rt, db)
        elif args.command == 'intake-context':
            result = self.rt.intake.intake_context(self.rt, db, args.burst)
        elif args.command == 'intake-reconcile':
            result = self.rt.intake.reconcile(self.rt, db, args.burst)
        elif args.command == 'triage-record':
            result = self.rt.intake.triage_record_cli(self.rt, db, args.burst, intent=args.intent, clarity=args.clarity, disposition=args.disposition, target_task=args.target_task, confidence=args.confidence, basis=args.basis, **{f'needs_{self.key}': getattr(args, f'needs_{self.key}')}, note=args.note)
        elif args.command == 'triage-correct':
            result = self.rt.intake.triage_correct(self.rt, db, args.record, args.disposition, args.note)
        elif args.command == 'intake-launch':
            result = self.rt.intake.intake_launch(self.rt, db, db_path, args.from_burst, args.title,
                                          Path(args.brief_file).read_text(encoding='utf-8'), provider=args.provider,
                                          category=args.category, important=args.important, linear=args.linear,
                                          ack=not args.no_ack, ack_text=args.ack_text, decision_event=args.decision_event,
                                          parent=args.parent, slug=args.slug, again=args.again,
                                          wait_seconds=args.wait_seconds)
        elif args.command == 'intake-forward':
            text = Path(args.file).read_text(encoding='utf-8') if args.file else args.text
            result = self.rt.intake.intake_forward(self.rt, db, db_path, args.from_burst, args.task, text, bind=not args.no_bind,
                                           decision_event=args.decision_event)
        elif args.command in ('intake-brief', 'intake-draft', 'intake-reply'):
            text = Path(args.file).read_text(encoding='utf-8')
            if args.command == 'intake-brief':
                result = self.rt.intake.intake_brief(self.rt, db, db_path, args.from_burst, text)
            elif args.command == 'intake-draft':
                result = self.rt.intake.intake_draft(self.rt, db, db_path, args.from_burst, text)
            else:
                result = self.rt.intake.intake_reply(self.rt, db, db_path, args.from_burst, text, args.decision_event,
                                             again=args.again, relevant=args.relevant)
        elif args.command == 'intake-follow':
            result = self.rt.intake.intake_follow(self.rt, db, db_path, args.from_burst)
        elif args.command == 'oncall-incident':
            result = self.rt.intake.oncall_incident(self.rt, db, db_path, args.from_burst, args.title,
                                            Path(args.brief_file).read_text(encoding='utf-8'), args.summary,
                                            provider=args.provider, category=args.category, slug=args.slug,
                                            wait_seconds=args.wait_seconds)
        elif args.command == 'intake-bootstrap':
            result = self.rt.intake.intake_bootstrap(self.rt, db, db_path, launch=not args.no_launch,
                                             rewrite_policy=args.rewrite_policy, wait_seconds=args.wait_seconds)
        elif args.command == 'smoke-intake':
            result = self.rt.intake.smoke_intake(self.rt, db, db_path, args.scenario, wait=not args.no_wait, timeout=args.timeout)
        elif args.command == 'rule-check':
            result = self.rt.rules.rule_check(self.rt, db, db_path, Path(args.plan).read_text(encoding='utf-8'), card=args.card,
                                      category=args.category, summary=args.summary, approved_by=args.approved_by,
                                      about=args.about,
                                      gate=self.rt.card_gate.fields(args, decide=args.summary) if args.card else None)
            if result.get('card'):
                self.rt.start(db_path)
        elif args.command == 'rule-status':
            result = self.rt.rules.rule_status(self.rt, db, args.check,
                                       Path(args.plan).read_text(encoding='utf-8') if args.plan else None)
        elif args.command == 'rule-outcome':
            result = self.rt.rules.rule_outcome(self.rt, db, args.check, args.result, args.note)
        elif args.command == 'rule-correct':
            result = self.rt.rules.rule_correct(self.rt, db, args.check, args.outcome, args.note)
        elif args.command == 'rules':
            if args.rules_command == 'backtest':
                result = self.rt.rules.backtest_cli(self.rt, db, args.cases, args.spec, args.summary_out, args.details_out, args.record)
            elif args.rules_command == 'inbox-sync':
                result = self.rt.rules.inbox_sync(self.rt, db_path)
            else:
                result = self.rt.rules.list_proposals(db, args.status)
        elif args.command == 'rules-review-data':
            result = self.rt.rules.review_data(self.rt, db, args.since)
        elif args.command == 'rule-propose':
            result = self.rt.rules.propose(self.rt, db, db_path, Path(args.file).read_text(encoding='utf-8'))
            self.rt.start(db_path)
        elif args.command == 'routines':
            if args.routines_command == 'list':
                result = self.rt.routines.list_routines(self.rt, db, db_path, args.task)
            elif args.routines_command == 'validate':
                path = Path(args.file) if args.file else Path(self.rt._resolve_task_arg(db, args.task or os.getcwd())['path']) / self.rt.routines.FILE
                result = self.rt.routines.check_file(self.rt, path)
            elif args.routines_command == 'proposals':
                result = {'ok': True, 'proposals': self.rt.routines.list_proposals(db)}
            elif args.routines_command == 'run-now':
                result = self.rt.routines.run_now(self.rt, db, db_path, args.task, args.id)
            else:
                result = self.rt.routines.set_enabled(self.rt, db, db_path, args.task, args.id, args.routines_command == 'enable')
            if not result.get('ok'):
                print(json.dumps(result, ensure_ascii=False, indent=2))
                sys.exit(1)
        elif args.command == 'routine-propose':
            prompt = Path(args.prompt_file).read_text(encoding='utf-8') if args.prompt_file else args.prompt
            result = self.rt.routines.propose(self.rt, db, db_path, args.task, {
                'id': args.id, 'schedule': args.schedule, 'every': args.every, 'prompt': prompt, 'scope': args.scope,
                'until': args.until, 'provider': args.provider})
            self.rt.start(db_path)
        elif args.command == 'smoke-rules':
            provider, session = args.provider or self.rt.claude.actor()[0], args.session or self.rt.claude.actor()[1]
            if args.scenario == 'outcomes':
                result = self.rt.rules.smoke_outcomes(self.rt, db, db_path, card=args.card, session=session, provider=provider)
            else:
                result = self.rt.rules.smoke_proposal(self.rt, db, db_path, card=args.card, session=session, provider=provider,
                                              timeout=args.timeout)
            if args.card:
                self.rt.start(db_path)
        elif args.command == 'smoke-routines':
            result = self.rt.routines.smoke(self.rt, db, db_path, args.provider, wait=not args.no_wait, timeout=args.timeout,
                                    second=not args.once)
        elif args.command == 'report-blocker':
            result = self.rt.blockers.report_blocker(self.rt, db, db_path, system=args.system, operation=args.operation,
                                             type=args.type, tried=self.msg('cli.main.18').join(args.tried), unblock=args.unblock,
                                             task=args.task, options=args.option, recommend=args.recommend,
                                             gate=self.rt.card_gate.fields(args, checked=args.tried))
            self.rt.start(db_path)
        elif args.command == 'resolve-blocker':
            result = self.rt.blockers.resolve_blocker(self.rt, db, args.id, args.how, args.note)
        elif args.command == 'gaps':
            result = (self.rt.blockers.gaps_list(db, args.status, args.task, args.system, args.limit)
                      if args.gaps_command == 'list' else self.rt.blockers.gap_detail(db, args.id))
        elif args.command == 'gap-propose':
            result = self.rt.blockers.propose_skills(self.rt, db, db_path, Path(args.file).read_text(encoding='utf-8'))
            self.rt.start(db_path)
        elif args.command == 'blocker-code':
            result = self.rt.blockers.blocker_code(self.rt, db, args.id, args.cmd, event_id=args.event, stdin=args.stdin,
                                           env_name=args.env, timeout=args.timeout)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0 if result['ok'] else 1)
        elif args.command == 'smoke-blocker':
            result = self.rt.blockers.smoke(self.rt, db, db_path, provider=args.provider, dashboard=args.dashboard,
                                    live_card=args.live_card)
        elif args.command == 'task-deadline':
            if args.deadline_command == 'register':
                result = self.rt.task_lifecycle.deadline_register(self.rt, db, args.task, args.due, args.note,
                                                          lead_hours=args.lead_hours, ask=args.ask, about=args.about,
                                                          wake=args.wake)
                self.rt.start(db_path)
            elif args.deadline_command == 'cancel':
                result = self.rt.task_lifecycle.deadline_cancel(self.rt, db, args.id)
            else:
                result = self.rt.task_lifecycle.deadline_list(self.rt, db, args.task, args.status)
        elif args.command == 'task-sweep':
            result = self.rt.task_lifecycle.sweep(self.rt, db, db_path)
        elif args.command == 'health':
            if args.sweep:
                live = db.execute('SELECT pid, birth FROM runtime WHERE id=1').fetchone()
                if live and live['birth'] and self.rt.pid_birth(live['pid']) == live['birth']:
                    # One sweeper at a time: the card / notice dedupe assumes the supervisor is the only writer.
                    raise ValueError('supervisor_running_it_sweeps_every_30_seconds_use_health_without_--sweep')
            swept = self.rt.daemon_health(db, self.rt.make_dispatcher(db, db_path)) if args.sweep else None
            result = {'ok': True, **self.rt.health_report(db), **({'sweep': swept} if swept is not None else {})}
        elif args.command == 'card-destination-check':
            result = self.rt.buttons.destination_report(self.rt, db, refresh=args.refresh)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0 if result['ok'] else 1)
        elif args.command == 'interaction-distill':
            result = self.rt.learning.distill(self.rt, db, db_path, args.event, args.intent, directive=args.directive,
                                      feedback_file=args.feedback_file, semantic=args.scenario)
            self.rt.start(db_path)
        elif args.command == 'rule-draft':
            fields = {k: getattr(args, k) for k in ('title', 'scenario', 'default', 'override', 'rule', 'impact')}
            result = self.rt.learning.rule_draft(self.rt, db, db_path, args.promote, fields=fields, no_rule=args.no_rule,
                                         reason=args.reason, basis_count=args.basis_count)
            if result.get('carded'):
                self.rt.start(db_path)
        elif args.command == 'learning-review':
            if args.promote:
                result = self.rt.learning.promote_brief(self.rt, db, args.promote)
            elif args.shape:
                result = self.rt.learning.shape_history(self.rt, db, args.shape)
            else:
                now = time.time()
                start_ts = self.rt.registry.parse_since(args.since, now) or (now - 7 * 86400)
                result = {'ok': True, 'since': self.rt.rules.iso(start_ts), 'until': self.rt.rules.iso(now),
                          'interactions': self.rt.learning.review_interactions(self.rt, db, start_ts, now),
                          'counts': self.rt.learning.aggregate(db, now)['counts']}
        elif args.command == 'smoke-feedback-learning':
            provider, session = args.provider or self.rt.claude.actor()[0], args.session or self.rt.claude.actor()[1]
            result = self.rt.learning.smoke(self.rt, db, db_path, provider=provider, session=session, card=args.card)
            if args.card:
                self.rt.start(db_path)
        elif args.command == 'ask':
            result = self.rt.ask(db, responders=args.responder, text=args.text, options=args.option, channel=args.channel,
                         thread=args.thread, dm=args.dm, confirm_mode=args.confirm_text, remind_after=args.remind_after,
                         expire_after=args.expire_after, reminder_text=args.reminder_text, lang=args.lang,
                         hint=not args.no_hint)
            self.rt.start(db_path)
        elif args.command in ('ask-answer', 'ask-confirm'):
            if args.command == 'ask-answer':
                result = self.rt.ask_answer(db, args.card_id, args.option, args.actor, args.channel, args.message_ts, args.actor_team)
            else:
                result = self.rt.ask_confirm(db, args.proposal_id, args.answer, args.actor, args.channel, args.message_ts,
                                     args.actor_team)
            if result.get('ok') and not result.get('duplicate'):
                try:
                    self.rt.start(db_path)
                except Exception:
                    result['poller_start'] = 'failed'
        elif args.command == 'ask-propose':
            result = self.rt.ask_propose(db, args.card_id, args.option, args.source_ts, args.note)
            self.rt.start(db_path)
        elif args.command == 'smoke-ask':
            result = self.rt.smoke_ask(db, db_path, args.session, args.provider, args.cwd, args.text, args.option,
                               args.confirm_text, args.remind_after, args.expire_after, args.lang,
                               live_card=args.live_card)
        elif args.command == 'smoke-ask-cleanup':
            result = self.rt.smoke_ask_cleanup(db, args.card_id)
        elif args.command == 'handoff':
            if not (args.watch and args.session_id and args.cwd):
                raise ValueError('handoff_takes_--event_and_--task_or_--watch_--session-id_--cwd')
            result = self.rt.handoff(db, args.watch, args.session_id, args.cwd, args.provider, args.owner_pid)
        elif args.command == 'recover':
            result = self.rt.recover(db, args.watch, str(db_path), new_session=args.new_session,
                             reason=args.unrecoverable_reason)
        elif args.command == 'rearm':
            result = self.rt.rearm(db, args.watch, args.mode, args.since, duration_seconds=args.duration_seconds, provider=args.provider)
            result['started_pid'] = self.rt.start(db_path)
        elif args.command == 'bind-source':
            result = self.rt.bind_source(db, args.watch, args.source_ts)
        elif args.command == 'stop':
            with self.rt.transaction(db):
                w = self.rt.load(db, args.watch)
                w['state'] = 'stopped'
                w['stop_reason'] = 'explicit_stop'
                w['stopped_at'] = time.time()
                self.rt.save(db, w)
                if self.rt.claude.provider(w) == 'claude':
                    self.rt.claude.resolved(db, w['session'])
            result = w
        else:
            watches = [self.rt.load(db, args.watch)] if args.watch else [json.loads(r[0]) for r in db.execute('SELECT data FROM watches')]
            runtime = db.execute('SELECT * FROM runtime WHERE id=1').fetchone()
            daemon = dict(runtime) if runtime else None
            if daemon:
                daemon['heartbeat_age_seconds'] = time.time() - daemon['heartbeat']
                daemon['alive'] = bool(daemon['birth'] and self.rt.pid_birth(daemon['pid']) == daemon['birth'])
            runtime_version = db.execute("SELECT value FROM runtime_meta WHERE key='version'").fetchone()
            result = {'version': self.rt.VERSION, 'running_version': runtime_version[0] if runtime_version else None, 'supervisor': daemon, 'watches': [
                {'watch': {**w, 'provider': self.rt.claude.provider(w)}, 'task_id': self.rt.registry.task_id_for_cwd(w.get('cwd')),
                 'events': [dict(r) for r in db.execute('SELECT * FROM events WHERE watch_id=?', (w['id'],))],
                 'escalations': [dict(r) for r in db.execute('SELECT * FROM escalations WHERE event_id IN (SELECT id FROM events WHERE watch_id=?)', (w['id'],))],
                 'decisions': [dict(r) for r in db.execute('SELECT * FROM decisions WHERE event_id IN (SELECT event_id FROM escalations WHERE watch_id=?)', (w['id'],))],
                 'proposals': [dict(r) for r in db.execute('SELECT * FROM decision_proposals WHERE event_id IN (SELECT event_id FROM escalations WHERE watch_id=?)', (w['id'],))],
                 'ask_cards': [{k: r[k] for k in r.keys() if k not in ('question', 'reminder_text')} for r in db.execute('SELECT * FROM ask_cards WHERE watch_id=?', (w['id'],))]}
                for w in watches]}
        if args.command == 'status':
            sessions = {w['session'] for w in watches if self.rt.claude.provider(w) == 'claude'}
            result['claude_delivery'] = {row['session_id']: {**dict(row), 'wake_path': str(self.rt.claude.wake_path(db_path,row['session_id']))} for row in db.execute('SELECT * FROM claude_wakes') if row['session_id'] in sessions}
            result['claude_claims'] = [dict(row) for row in db.execute("SELECT c.* FROM claude_claims c JOIN events e ON e.id=c.event_id WHERE e.status='claimed' AND c.released_at IS NULL") if row['session_id'] in sessions]
        print(json.dumps(result, ensure_ascii=False, indent=2))
