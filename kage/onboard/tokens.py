"""Token checks before a token is stored (O1c3): onboarding pastes a token, Kage checks it with Slack and only then puts
it into the owner's secrets store. A token of another kind or another workspace is refused before anything is
written. Errors are codes; no token appears in a result, an error or a log line.

- bot (`xoxb-`): `auth.test` names a bot (`bot_id`) — its team and bot user; granted scopes from `x-oauth-scopes`.
- app-level (`xapp-`): `apps.connections.open` answers ok. The returned socket URL is dropped, never connected.
  Slack does not name the workspace of an app-level token, so it is tied to the bot only by the owner's install.
- user (`xoxp-`): `auth.test` names a person, not the bot, in the bot's team.

`call(method, token[, params])` returns (payload, lower-cased headers); `slack_call` is the live one, refused while the
guard variable is "1". Tests pass their own.
"""
from __future__ import annotations
import json
import os
import urllib.parse
import urllib.request

from kage.onboard import manifest

BOT, APP, USER = 'slack-bot-token', 'slack-app-token', 'slack-user-token'  # names in the secrets store
PREFIX = {BOT: 'xoxb-', APP: 'xapp-', USER: 'xoxp-'}
GUARD_ENV = 'KAGE_NO_REAL_SLACK'


class TokenRefused(ValueError):
    """`code` says why; never the token."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def slack_call(method, token, params=None, *, guard_env=GUARD_ENV, timeout=20):
    """A live Web API call (POST, form fields `params`): (payload, lower-cased headers)."""
    if os.environ.get(guard_env) == '1':
        raise TokenRefused('slack_offline_guard_set')
    req = urllib.request.Request(f'https://slack.com/api/{method}', method='POST',
                                 data=urllib.parse.urlencode(params or {}).encode('ascii'),
                                 headers={'Authorization': f'Bearer {token}',
                                          'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8')), {k.lower(): v for k, v in resp.headers.items()}
    except (OSError, ValueError):
        raise TokenRefused(f'slack_unreachable_for_{method}') from None


def _shape(name, token):
    if not isinstance(token, str) or not token.startswith(PREFIX[name]) or len(token) > 2048 or any(
            c.isspace() for c in token):
        raise TokenRefused(f'not_a_{name.replace("-", "_")}')


def _ask(call, method, token):
    payload, headers = call(method, token)
    if not isinstance(payload, dict) or payload.get('ok') is not True:
        error = payload.get('error') if isinstance(payload, dict) else None
        raise TokenRefused(f'slack_{method}_refused_{error or "unknown"}')
    return payload, headers or {}


def _scopes(headers, wanted):
    granted = sorted(s.strip() for s in (headers.get('x-oauth-scopes') or '').split(',') if s.strip())
    return granted, sorted(set(wanted) - set(granted)) if granted else None


def check_bot(token, *, call=slack_call, scopes=manifest.BOT_SCOPES):
    _shape(BOT, token)
    auth, headers = _ask(call, 'auth.test', token)
    if not auth.get('bot_id') or not auth.get('user_id') or not auth.get('team_id'):
        raise TokenRefused('bot_token_is_not_a_bot')
    granted, missing = _scopes(headers, scopes)
    return {'kind': 'bot', 'team_id': auth['team_id'], 'team': auth.get('team'), 'bot_user_id': auth['user_id'],
            'bot_id': auth['bot_id'], 'scopes': granted, 'missing_scopes': missing}


def check_app(token, *, call=slack_call):
    _shape(APP, token)
    _ask(call, 'apps.connections.open', token)  # the socket URL is not kept
    return {'kind': 'app'}


def check_user(token, bot, *, call=slack_call, scopes=manifest.USER_SCOPES):
    """`bot`: check_bot's result for this deployment."""
    _shape(USER, token)
    auth, headers = _ask(call, 'auth.test', token)
    if auth.get('bot_id') or auth.get('user_id') == bot['bot_user_id']:
        raise TokenRefused('user_token_is_the_bot')
    if auth.get('team_id') != bot['team_id']:
        raise TokenRefused('user_token_of_another_workspace')
    if not auth.get('user_id'):
        raise TokenRefused('user_token_names_nobody')
    granted, missing = _scopes(headers, scopes)
    return {'kind': 'user', 'team_id': auth['team_id'], 'user_id': auth['user_id'], 'scopes': granted,
            'missing_scopes': missing}


def admit(store, name, token, *, call=slack_call, bot=None, team_id=None):
    """Check `token` as `name` and only then store it; the check's result (no token). `team_id`: the workspace this
    deployment already belongs to (a bot of another one is refused); `bot`: needed for a user token."""
    if name == BOT:
        result = check_bot(token, call=call)
        if team_id and result['team_id'] != team_id:
            raise TokenRefused('bot_token_of_another_workspace')
    elif name == APP:
        result = check_app(token, call=call)
    elif name == USER:
        if bot is None:
            raise TokenRefused('user_token_needs_the_checked_bot_first')
        result = check_user(token, bot, call=call)
    else:
        raise TokenRefused('unknown_token_name')
    store.put(name, token)
    return {**result, 'ref': store.ref(name)}
