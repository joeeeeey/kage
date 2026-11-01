"""The runtime's Slack client: the operations a deployment's Slack module exposes over the transport
(kage.adapters.slack.transport), and the post-send hook that tells the follow-up runtime about a successful send.

The deployment builds one `Client` with its guard variable, the owner's name, its bot's user name, the token variables
and file, and two hooks that are its own names, looked up at call time: `call` (its `api_call`, which tests replace, so
every operation reaches the replaced one) and `post_send` (its `notify_followup_activity`). `followup_helper` is the
path of the follow-up runtime's poll-schedule module (absent: the hook does nothing).
"""
from __future__ import annotations
import importlib.util
import re
import sys

from kage.adapters.slack import transport as _transport

_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


def _payload_texts(payload):
    def walk(value):
        if isinstance(value, dict):
            own = [value["text"]] if isinstance(value.get("text"), str) else []
            own += [f"<@{value['user_id']}>"] if value.get("type") == "user" and value.get("user_id") else []
            return own + [t for v in value.values() if not isinstance(v, str) for t in walk(v)]
        return [t for v in value for t in walk(v)] if isinstance(value, list) else []
    return [payload["text"]] if payload.get("text") else walk(payload.get("blocks") or [])


class Client:
    """The transport's operations under the deployment's names, and the post-send hook."""

    def __init__(self, *, guard_env, owner_name, expected_bot_user, user_token_env, user_token_file, bot_token_env,
                 call, post_send, followup_helper):
        self.followup_helper = followup_helper
        self.transport = _transport.Transport(
            guard_env=guard_env, owner_name=owner_name, expected_bot_user=expected_bot_user,
            user_token_env=user_token_env, user_token_file=user_token_file, bot_token_env=bot_token_env,
            post_send=post_send, call=call)

    def require_real_slack(self, what):
        self.transport.require_live(what)

    def api_call(self, endpoint, token, params=None, json_body=None):
        return self.transport.api_call(endpoint, token, params, json_body)

    def api_call_form(self, endpoint, token, fields):
        return self.transport.api_call_form(endpoint, token, fields)

    def validate_bot_identity(self, token):
        return self.transport.validate_bot_identity(token)

    def validate_user_identity(self, token):
        return self.transport.validate_user_identity(token)

    def validate_any_identity(self, token):
        return self.transport.validate_any_identity(token)

    def read_token_file(self, path):
        return self.transport.read_token_file(path)

    def default_user_token(self):
        return self.transport.default_user_token()

    def default_bot_token(self):
        return self.transport.default_bot_token()

    def add_reactions(self, token, channel, ts, names):
        """Add and verify reactions as the authenticated user; retries are idempotent."""
        return self.transport.add_reactions(token, channel, ts, names)

    def upload_file_bytes(self, upload_url, file_path):
        return self.transport.upload_file_bytes(upload_url, file_path)

    def paginated_list(self, endpoint, token, params, key):
        return self.transport.paginated_list(endpoint, token, params, key)

    def notify_followup_activity(self, response, payload):
        """Successful sends reset existing local watches; never retry a Slack send on hook failure."""
        helper = self.followup_helper
        if not helper.is_file():
            return
        try:
            spec = importlib.util.spec_from_file_location('slack_send_poll_schedule', helper)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            message = response.get('message') or {}
            args = (response.get('channel') or payload.get('channel'), response.get('ts'),
                    message.get('thread_ts') or payload.get('thread_ts') or response.get('ts'), message.get('user'))
            try:  # the post's hidden agent metadata says which task sent it (a DM watch extends only on its own task)
                module.record_outbound(*args, metadata=payload.get('metadata'))
            except TypeError:  # an older follow-up runtime without the keyword
                module.record_outbound(*args)
            if hasattr(module, 'record_mentions'):  # whom it @-mentioned (ids only, never the text): open asks
                module.record_mentions(args[0], args[1], _MENTION_RE.findall('\n'.join(_payload_texts(payload))),
                                       metadata=payload.get('metadata'))
        except Exception:
            print('Follow-up cadence update failed locally; Slack send succeeded. Poller will reconcile activity.', file=sys.stderr)
