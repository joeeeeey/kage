"""Slack Web API transport: every call to slack.com goes through here, behind the offline guard.

The guard: when the environment variable named `guard_env` is "1", no token is loaded and every HTTP call (Web API,
form call, file upload) raises before a request is built. Test harnesses and sandboxes set it.

Tokens come from an environment variable, else a file (both configurable), or only from the owner's secrets store when
one is given (O1c3, kage.ports.secrets). Identity checks make sure a bot token is the expected bot and a user token is a
person, not the bot.

Callers and their tests replace module-level functions of the adapter that wraps this transport (api_call,
add_reactions). So that a replaced `api_call` is the one every other operation uses, the transport calls through
`self.call` (the adapter passes a function that looks its own `api_call` up at call time) and announces successful
posts through `self.post_send` (the adapter's post-send hook).
"""
from __future__ import annotations
import json
import mimetypes
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request
import uuid

from kage.ports.secrets import SLACK_BOT, SLACK_USER

API_BASE = 'https://slack.com/api'
BOT_TOKEN_PREFIX = 'xoxb-'


class Transport:
    def __init__(self, *, guard_env, owner_name, expected_bot_user, user_token_env='SLACK_USER_TOKEN',
                 user_token_file_env='SLACK_USER_TOKEN_FILE', user_token_file='~/.config/slack/token',
                 bot_token_env='SLACK_BOT_TOKEN', bot_token_file_env='SLACK_BOT_TOKEN_FILE',
                 bot_token_file='~/.config/slack/bot-token', post_send=None, call=None, emoji_example='eyes',
                 secret_store=None, write_guard=None):
        self.guard_env, self.owner_name, self.expected_bot_user = guard_env, owner_name, expected_bot_user
        self.user_token_env, self.user_token_file_env, self.user_token_file = (user_token_env, user_token_file_env,
                                                                               user_token_file)
        self.bot_token_env, self.bot_token_file_env, self.bot_token_file = bot_token_env, bot_token_file_env, bot_token_file
        self.post_send = post_send or (lambda response, payload: None)
        self.call = call or self.api_call
        self.emoji_example = emoji_example  # the second name in the add_reactions usage error (a deployment's own)
        self.secret_store = secret_store  # O1c3: tokens from the owner's secrets port instead of env / files
        self.write_guard = write_guard  # O2b: (endpoint, payload) -> None, or raises (kage.core.mode.WriteGuard)

    def offline(self):
        return os.environ.get(self.guard_env) == '1'

    def require_live(self, what):
        if self.offline():
            raise RuntimeError(f'real_slack_disabled: {self.guard_env}=1 refuses {what} (tests never reach Slack)')

    def api_call(self, endpoint, token, params=None, json_body=None):
        self.require_live(endpoint)
        if self.write_guard is not None:
            self.write_guard(endpoint, json_body if json_body is not None else params)
        url = f'{API_BASE}/{endpoint}'
        headers = {'Authorization': f'Bearer {token}'}
        if json_body is not None:
            data = json.dumps(json_body).encode('utf-8')
            headers['Content-Type'] = 'application/json; charset=utf-8'
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        else:
            if params:
                url = f'{url}?{urllib.parse.urlencode(params)}'
            req = urllib.request.Request(url, headers=headers, method='GET')
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode('utf-8')
        data = json.loads(payload)
        if not data.get('ok'):
            raise RuntimeError(f"Slack API error: {data.get('error')}")
        if endpoint == 'chat.postMessage':
            self.post_send(data, json_body or {})
        return data

    def api_call_form(self, endpoint, token, fields):
        self.require_live(endpoint)
        if self.write_guard is not None:
            self.write_guard(endpoint, fields)
        url = f'{API_BASE}/{endpoint}'
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/x-www-form-urlencoded'}
        payload = {k: v for k, v in fields.items() if v is not None and v != ''}
        encoded = urllib.parse.urlencode(payload).encode('utf-8')
        req = urllib.request.Request(url, data=encoded, headers=headers, method='POST')
        with urllib.request.urlopen(req) as resp:
            payload = resp.read().decode('utf-8')
        data = json.loads(payload)
        if not data.get('ok'):
            raise RuntimeError(f"Slack API error: {data.get('error')}")
        return data

    @staticmethod
    def _identity(auth, user=None):
        return {'team': auth.get('team'), 'team_id': auth.get('team_id'), 'user': user if user is not None else
                auth.get('user'), 'user_id': auth.get('user_id'), 'bot_id': auth.get('bot_id')}

    def validate_bot_identity(self, token):
        if not token.startswith(BOT_TOKEN_PREFIX):
            raise RuntimeError(f"Expected Slack bot token starting with '{BOT_TOKEN_PREFIX}', got prefix '{token[:5]}...'.")
        auth = self.call('auth.test', token)
        actual_user = auth.get('user')
        if actual_user != self.expected_bot_user:
            detail = self._identity(auth, actual_user)
            raise RuntimeError(f"Expected Slack bot user '{self.expected_bot_user}', "
                               f'auth.test returned {json.dumps(detail, ensure_ascii=False, sort_keys=True)}.')

    def validate_user_identity(self, token):
        if token.startswith(BOT_TOKEN_PREFIX):
            raise RuntimeError(f"Expected {self.owner_name}'s Slack user token for this operation, but got a bot-token "
                               f'prefix. Set {self.user_token_env} or pass --token-mode bot intentionally.')
        auth = self.call('auth.test', token)
        if auth.get('bot_id') or auth.get('user') == self.expected_bot_user:
            detail = self._identity(auth)
            raise RuntimeError(f"Expected {self.owner_name}'s Slack user token for this operation, "
                               f'auth.test returned bot-like identity {json.dumps(detail, ensure_ascii=False, sort_keys=True)}.')
        return auth

    def validate_any_identity(self, token):
        return self.call('auth.test', token)

    @staticmethod
    def read_token_file(path):
        p = Path(path).expanduser()
        if not p.is_file():
            return ''
        return p.read_text(encoding='utf-8').strip()

    def default_user_token(self):
        if self.offline():
            return ''
        if self.secret_store is not None:
            return self.secret_store.get(SLACK_USER) or ''
        return (os.environ.get(self.user_token_env, '').strip()
                or self.read_token_file(os.environ.get(self.user_token_file_env, self.user_token_file)))

    def default_bot_token(self):
        if self.offline():
            return ''
        if self.secret_store is not None:
            return self.secret_store.get(SLACK_BOT) or ''
        return (os.environ.get(self.bot_token_env, '').strip()
                or self.read_token_file(os.environ.get(self.bot_token_file_env, self.bot_token_file)))

    def add_reactions(self, token, channel, ts, names):
        """Add and verify reactions as the authenticated user; retries are idempotent."""
        identity = self.call('auth.test', token)
        normalized = list(dict.fromkeys(name.strip(':') for name in names))
        if not normalized or any(not re.fullmatch(r'[a-z0-9_+-]+', n) for n in normalized):
            raise ValueError(f'Use Slack emoji API names, for example thinking_face or {self.emoji_example}.')
        for name in normalized:
            try:
                self.call('reactions.add', token, json_body={'channel': channel, 'timestamp': ts, 'name': name})
            except RuntimeError as exc:
                if str(exc) != 'Slack API error: already_reacted':
                    raise
        result = self.call('reactions.get', token, params={'channel': channel, 'timestamp': ts, 'full': 'true'})
        reactions = result.get('message', {}).get('reactions', [])
        verified = {name: any(r.get('name') == name and identity['user_id'] in r.get('users', [])
                             for r in reactions) for name in normalized}
        if not all(verified.values()):
            raise RuntimeError('Slack reaction readback did not confirm every requested reaction.')
        return {'channel': channel, 'ts': ts, 'actor': identity['user_id'], 'verified': verified}

    def upload_file_bytes(self, upload_url, file_path):
        self.require_live('a file upload')
        file_path = Path(file_path)
        filename = file_path.name
        content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        boundary = f'----codex-slack-upload-{uuid.uuid4().hex}'
        body = bytearray()
        body.extend(f'--{boundary}\r\n'.encode('utf-8'))
        body.extend(f'Content-Disposition: form-data; name="filename"; filename="{filename}"\r\n'.encode('utf-8'))
        body.extend(f'Content-Type: {content_type}\r\n\r\n'.encode('utf-8'))
        body.extend(file_path.read_bytes())
        body.extend(b'\r\n')
        body.extend(f'--{boundary}--\r\n'.encode('utf-8'))
        headers = {'Content-Type': f'multipart/form-data; boundary={boundary}'}
        req = urllib.request.Request(upload_url, data=bytes(body), headers=headers, method='POST')
        with urllib.request.urlopen(req) as resp:
            payload = resp.read().decode('utf-8')
        if resp.status != 200:
            raise RuntimeError(f'Slack upload transport error: HTTP {resp.status}')
        return payload

    def paginated_list(self, endpoint, token, params, key):
        items = []
        cursor = None
        while True:
            if cursor:
                params['cursor'] = cursor
            data = self.call(endpoint, token, params=params)
            items.extend(data.get(key, []))
            cursor = data.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                break
        return items
