"""Kage's Slack transport leaf (O1c2), for a deployment that declares no `slack_transport` module: the operations of
kage.adapters.slack.client under the names the runtime calls (`api_call`, `default_bot_token`, `add_reactions`, …).
`api_call` and `notify_followup_activity` are looked up at call time, so a patched `api_call` reaches every
operation. After a successful send the hook records the activity through Kage's own poll-schedule unit, never
through another skill's file. Run by kage.app with `_app` in its namespace."""
from __future__ import annotations
kage_bridge = _app.bridge
_k = kage_bridge.module('slack_client', 'kage.adapters.slack.client')
_owner = kage_bridge.deployment().owner
CLIENT = _k.Client(
    guard_env=_app.value('slack_transport.guard_env'), owner_name=_owner.short_name,
    expected_bot_user=_app.value('slack_transport.expected_bot_user'),
    user_token_env=_app.value('slack_transport.user_token_env'),
    user_token_file=_app.value('slack_transport.user_token_file'),
    bot_token_env=_app.value('slack_transport.bot_token_env'),
    bot_token_file=_app.value('slack_transport.bot_token_file'),
    user_token_file_env=_app.value('slack_transport.user_token_file_env'),
    bot_token_file_env=_app.value('slack_transport.bot_token_file_env'),
    call=lambda *args, **kwargs: api_call(*args, **kwargs),
    post_send=lambda response, payload: notify_followup_activity(response, payload),
    followup_helper=lambda: _app.leaf('poll_schedule'))
(TRANSPORT, require_real_slack, api_call) = (CLIENT.transport, CLIENT.require_real_slack, CLIENT.api_call)
(api_call_form, validate_bot_identity) = (CLIENT.api_call_form, CLIENT.validate_bot_identity)
(validate_user_identity, validate_any_identity) = (CLIENT.validate_user_identity, CLIENT.validate_any_identity)
(read_token_file, default_user_token) = (CLIENT.read_token_file, CLIENT.default_user_token)
(default_bot_token, add_reactions) = (CLIENT.default_bot_token, CLIENT.add_reactions)
(upload_file_bytes, paginated_list) = (CLIENT.upload_file_bytes, CLIENT.paginated_list)
notify_followup_activity = CLIENT.notify_followup_activity
