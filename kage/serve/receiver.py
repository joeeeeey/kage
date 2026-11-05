"""`serve receiver` (O1c5d): a generated deployment's Slack Socket Mode receiver, Kage's own.

The loop is kage.adapters.slack.socket_mode (`SocketModeRunner`: N connections, ack at once, ping / pong, capped
backoff, each envelope processed once); what it does with an envelope is kage.adapters.slack.receiver's: a card or
confirmation button -> the deployment program's `decide` / `confirm-decision`, a card-thread reply -> `card-reply-push`,
the owner's top-level message in a decision channel -> `control-push`. Nothing else (a deployment's own shortcuts are
its own receiver's).

- Tokens: the app-level token (`connections:write`) and the bot token only from the owner's secrets store
  (kage.ports.secrets: `slack-app-token`, `slack-bot-token`), never a file, an argument or the environment.
- Identity: the expected workspace and owner come only from the verified profile (owner.json, written by `kage init`
  from the verified records). The preflight refuses a bot token of another workspace or of another bot user.
- Slack calls go through the deployment's transport leaf (its real-Slack guard included).
- Every log line is secret-free JSON (stdout; errors on stderr), as the deployment's receiver writes them.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import types

from kage.adapters.slack import receiver as _r
from kage.adapters.slack import socket_mode as _sm
from kage.ports import secrets as _secrets


class ReceiverRefused(RuntimeError):
    """The receiver must not start (said why; no token in the text)."""


class Client:
    """`call(method, params=None, json_body=None)` -> (data, headers) over the transport leaf, for one token."""

    def __init__(self, transport, token):
        self._transport, self._token = transport, token

    def __repr__(self):
        return 'Client(<token withheld>)'

    def call(self, method, params=None, json_body=None):
        return self._transport.api_call(method, self._token, params=params, json_body=json_body), {}


def _diagnostic(event, payload, exc):
    container = payload.get('container') or {}
    return {'event': event, 'error': type(exc).__name__,  # the type only: an error text may quote a payload
            'channel_id': _r._safe(container.get('channel_id')), 'message_ts': _r._safe(container.get('message_ts'))}


def namespace(assembly, *, poster=None, runner=None):
    """The names the Socket Mode loop reads (`ns`): the Slack-side receiver functions, the loop's tunables, the
    deployment's program as the followup CLI, and the owner's click answers."""
    owner = assembly.deployment.owner
    answers = _r.ClickAnswers(owner.language, owner_name=owner.short_name,
                              guard_env=assembly.value('slack_transport.guard_env'))
    ns = types.SimpleNamespace(**{name: getattr(_sm, name) for name in dir(_sm) if not name.startswith('__')})
    ns.__dict__.update({name: getattr(_r, name) for name in dir(_r) if not name.startswith('__')})
    program = Path(assembly.program)

    def handle_decision_action(payload, *, expected_team_id, runner=runner, poster=poster, python=None, script=program,
                               db=None, timeout=_r.DECISION_TIMEOUT_SECONDS):
        return answers.handle_decision_action(payload, expected_team_id=expected_team_id, runner=runner, poster=poster,
                                              python=python or sys.executable, script=script, db=db, timeout=timeout)

    ns.FOLLOWUP_SCRIPT = program
    ns.ANSWERS = answers
    ns.handle_decision_action = handle_decision_action
    ns.decision_error_diagnostic = lambda payload, exc: _diagnostic('decision_error', payload, exc)
    return ns


def build_parser():
    p = argparse.ArgumentParser(prog='serve receiver', description="the deployment's Slack Socket Mode receiver")
    p.add_argument('--followup-db', default=None, help='the store the program uses (default: its own default)')
    p.add_argument('--idle-timeout-seconds', type=int, default=0, help='0 = run until stopped')
    p.add_argument('--liveness-interval-seconds', type=float, default=10.0)
    p.add_argument('--pong-timeout-seconds', type=float, default=5.0)
    p.add_argument('--decision-timeout-seconds', type=float, default=_r.DECISION_TIMEOUT_SECONDS)
    p.add_argument('--socket-connections', type=int, default=_sm.SOCKET_CONNECTIONS,
                   help=f'concurrent connections (1-{_sm.MAX_SOCKET_CONNECTIONS})')
    p.add_argument('--once', action='store_true', help='stop after the first interactive payload it handles')
    return p


class Receiver(_sm.SocketModeRunner):
    def __init__(self, assembly, args, *, store=None, transport=None, poster=None, runner=None):
        """`store`, `transport`, `poster` (response_url answers) and `runner` (the program's command runs) are
        injectable for tests; by default the owner's secrets store, the transport leaf, Slack and subprocess."""
        owner = assembly.deployment.owner
        if not owner.slack_workspace or not owner.slack_user or not owner.assistant_slack_bot_user:
            raise ReceiverRefused('profile_has_no_verified_slack_identity')
        self.assembly, self.args = assembly, args
        self.args.expected_team_id = owner.slack_workspace
        self.args.followup_script = str(assembly.program)
        self.ns = namespace(assembly, poster=poster, runner=runner)
        self.followup_runner = runner
        store = store or _secrets.for_owner(owner.key)
        app_token, bot_token = store.get(_secrets.SLACK_APP), store.get(_secrets.SLACK_BOT)
        if not app_token or not bot_token:
            raise ReceiverRefused('tokens_not_in_the_secrets_store: '
                                  + ', '.join(n for n, v in ((_secrets.SLACK_APP, app_token),
                                                             (_secrets.SLACK_BOT, bot_token)) if not v))
        transport = transport or assembly.leaf('slack_transport')
        self.app_client, self.bot_client = Client(transport, app_token), Client(transport, bot_token)

    def preflight(self):
        owner = self.assembly.deployment.owner
        bot, _ = self.bot_client.call('auth.test')
        if bot.get('team_id') != owner.slack_workspace:
            raise ReceiverRefused('bot_token_of_another_workspace')
        if bot.get('user_id') != owner.assistant_slack_bot_user:
            raise ReceiverRefused('bot_token_of_another_bot_user')


def main(assembly, runtime, argv, **inject):
    args = build_parser().parse_args(argv)
    try:
        return Receiver(assembly, args, **inject).run()
    except ReceiverRefused as exc:  # fail closed; launchd retries after its throttle interval
        print(json.dumps({'error': 'ReceiverRefused', 'detail': str(exc)}), file=sys.stderr, flush=True)
        return 78
