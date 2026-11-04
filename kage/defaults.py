"""Neutral wiring for a new deployment; explicit assembly values replace each default as a whole.

Paths are scoped to the validated owner key. Policy and playbook references still need pinned deployment packs:
defaults never substitute an empty policy for a missing pack. The initial worker configuration does not dispatch
agents or read the owner's conversations; onboarding enables those capabilities in a separate, verified step.
"""
from __future__ import annotations

import json
from pathlib import Path


def assembly_values(deployment):
    """A fresh value tree for this owner, with no shared mutable configuration between assemblies."""
    owner = deployment.owner
    key, name = owner.key, owner.short_name
    config = f'.config/kage/{key}'
    state = f'.local/share/kage/state-{key}'
    worker = json.loads(Path(__file__).with_name('worker_defaults.json').read_text(encoding='utf-8'))
    worker['intake']['inbox_task'] = f'{key}-inbox'
    worker['intake']['oncall_task'] = f'{key}-oncall'
    values = {
        'avatar_cache.guard_env': 'KAGE_NO_REAL_SLACK',
        'blockers.internal_names': 'kage',
        'blockers.run_id_env': 'KAGE_DISPATCH_RUN_ID',
        'blockers.worker_slug': f'kage/{key}',
        'card_gate.help': f'The concrete decision only {name} can make.',
        'card_threads.ROUTED_INSTRUCTION': (
            f'{name} wrote in a card thread about this task. Read context.routed_card for the card and the verified '
            'decision status. Answer in that card thread as the assistant and act within the task authorization. '
            'Use propose-decision for a typed decision not recorded by code; handoff --event ID --task T when it '
            'belongs to another task.'),
        'decision_buttons.CONFIG_ENV': 'KAGE_DECISIONS_CONFIG',
        'decision_buttons.DEFAULT_CONFIG': f'{config}/decisions.json',
        'decision_buttons.ASK_STANDARD': (
            'Lead with the concrete decision: action, object, link and how to answer. Keep implementation details '
            'on the dashboard or in task files. Answer a question in its own thread.'),
        'decision_buttons.COPY_STANDARD': (
            'Write one plain sentence naming the action, its subject and scope. Add at most two short lines '
            'explaining why the owner must decide and whether it can be undone. Use names; keep internal '
            'identifiers and hashes in task files.'),
        'dispatcher.hops_env': 'KAGE_DISPATCH_HOPS',
        'dispatcher.probe_dir_prefix': 'kage-probe-',
        'dispatcher.run_context_env': 'KAGE_RUN_CONTEXT',
        'dispatcher.run_id_env': 'KAGE_DISPATCH_RUN_ID',
        'dispatcher.task_env': 'KAGE_DISPATCH_TASK',
        'event_envelope.DEFAULT_PEOPLE': f'{config}/people.json',
        'event_envelope.PEOPLE_ENV': 'KAGE_PEOPLE',
        'feedback_learning.CORPUS_DIR_ENV': 'KAGE_FEEDBACK_CORPUS_DIR',
        'feedback_learning.DEFAULT_CORPUS_DIR': f'~/{state}/feedback-corpus',
        'feedback_learning.smoke_scope': 'onboarding',
        'hub.DEFAULT_DB': f'{state}/state.sqlite',
        'hub.ITERM_SCRIPT': 'kage-task-workspace',
        'hub.RECEIPT_EMOJI.claude': 'eyes',
        'hub.RECEIPT_EMOJI.codex': 'eyes',
        'hub.TAIL_LINK': '.local/bin/kage-tail',
        'hub.TAIL_WRAPPER': 'kage-tail',
        'hub.__doc__': 'Kage: a durable, owner-addressed agent runtime. Stored events contain references only.',
        'hub.alias.run_tail': 'kage_tail',
        'hub.hops_env': 'KAGE_DISPATCH_HOPS',
        'hub.issue_tracker': deployment.org.tracker,
        'hub.no_autostart_env': 'KAGE_NO_AUTOSTART',
        'hub.no_iterm_env': 'KAGE_NO_ITERM',
        'hub.run_context_env': 'KAGE_RUN_CONTEXT',
        'hub.run_id_env': 'KAGE_DISPATCH_RUN_ID',
        'hub.tail_command': 'kage-tail',
        'hub.tasks_root_env': 'KAGE_TASKS_ROOT',
        'hub.worker_config_path': f'~/{config}/worker.json',
        'intake.BOOTSTRAP': 'kage-task-workspace',
        'intake.run_id_env': 'KAGE_DISPATCH_RUN_ID',
        'intake.ticket_ref_reason': 'tracker_ref',
        'poll_schedule.DEFAULT_DB': f'{state}/state.sqlite',
        'routines.run_id_env': 'KAGE_DISPATCH_RUN_ID',
        'rule_engine._p': 'default.access',
        'rule_engine.rules_dir_env': 'KAGE_RULES_DIR',
        'rule_engine.run_id_env': 'KAGE_DISPATCH_RUN_ID',
        'rule_engine.smoke_dir_prefix': 'kage-rules-smoke-',
        'rule_engine.worker_slug': f'kage/{key}',
        'rule_registry.0': f'{key}_facing_\\w+_breaks_the_card_copy_standard',
        'rule_registry.root': 'KAGE_RULES_DIR',
        'rule_registry.root_2': f'{config}/rules',
        # O1c2: Kage's own Slack transport (a deployment without its own module); tokens under owner-scoped names, and
        # no bot is trusted until onboarding records the verified bot user
        'slack_transport.guard_env': 'KAGE_NO_REAL_SLACK',
        'slack_transport.expected_bot_user': '',
        'slack_transport.user_token_env': f'KAGE_{key.upper()}_SLACK_USER_TOKEN',
        'slack_transport.user_token_file_env': f'KAGE_{key.upper()}_SLACK_USER_TOKEN_FILE',
        'slack_transport.user_token_file': f'~/{config}/slack-user-token',
        'slack_transport.bot_token_env': f'KAGE_{key.upper()}_SLACK_BOT_TOKEN',
        'slack_transport.bot_token_file_env': f'KAGE_{key.upper()}_SLACK_BOT_TOKEN_FILE',
        'slack_transport.bot_token_file': f'~/{config}/slack-bot-token',
        'task_lifecycle.tracker_helper': 'kage-tracker/scripts/tracker.py',
        'task_registry.DEFAULT_SMOKE_ROOT': f'/tmp/kage-{key}-dispatch-smoke',
        'task_registry.ROOT_ENV': 'KAGE_TASKS_ROOT',
        'task_registry.SMOKE_ROOT_ENV': 'KAGE_DISPATCH_SMOKE_ROOT',
        'worker_config.CONFIG_ENV': 'KAGE_WORKER_CONFIG',
        'worker_config.DEFAULTS': worker,
        'worker_config.DEFAULT_PATH': f'{config}/worker.json',
    }
    report_next = (
        f'Posts to a refused destination are blocked. Configure a private destination for {name} in decisions.json '
        'and invite the assistant. Check the conversation read scopes, then refresh the destination check.')
    values.update({key: report_next for key in ('decision_buttons.report_next', 'decision_buttons.REPORT_NEXT')})
    return values
