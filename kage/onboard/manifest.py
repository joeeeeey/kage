"""The Slack app manifest for a new deployment (onboarding O1b): paste it at api.slack.com/apps → Create New App → From
a manifest. Two variants: `assistant` (the bot speaks; colleagues message the bot) and `as_owner` (adds the user-token
scopes for speaking as the owner, reading the owner's DMs and search). The scopes follow the Web API methods the
runtime calls; the app-level token (`connections:write`, for Socket Mode) is created by hand after install."""
from __future__ import annotations

import json
from typing import Any

BOT_SCOPES = ('chat:write', 'im:write', 'im:history', 'im:read', 'groups:history', 'groups:read', 'channels:history',
              'channels:read', 'reactions:read', 'reactions:write', 'users:read', 'users:read.email',
              'app_mentions:read')  # 0.77.0: @mentions of the bot (`app_mention`) for a bot-identity intake
USER_SCOPES = ('chat:write', 'reactions:write', 'search:read', 'im:history', 'mpim:history', 'channels:history',
               'groups:history', 'users:read')
BOT_EVENTS = ('message.im', 'message.groups', 'app_mention')
STEPS = (
    'api.slack.com/apps → Create New App → From a manifest → choose the workspace → paste this JSON',
    'Install to Workspace (if the workspace needs admin approval, Slack asks an admin first)',
    'Basic Information → App-Level Tokens → generate one with connections:write (Socket Mode)',
    'store the bot token, the app-level token{user} where the deployment reads them, then run `doctor`',
)


def manifest(name: str, *, as_owner: bool = False, description: str | None = None) -> dict[str, Any]:
    name = (name or '').strip()[:35] or 'kage'
    scopes: dict[str, list[str]] = {'bot': list(BOT_SCOPES)}
    if as_owner:
        scopes['user'] = list(USER_SCOPES)
    return {
        'display_information': {'name': name, 'description': (description or f"{name}: a personal agent runtime (Kage)")[:140]},
        # the Messages tab with sending allowed: colleagues and the owner reach the kage by direct message, and the
        # owner's one-time identity code arrives that way (O1c3)
        'features': {'bot_user': {'display_name': name, 'always_online': True},
                     'app_home': {'home_tab_enabled': False, 'messages_tab_enabled': True,
                                  'messages_tab_read_only_enabled': False}},
        'oauth_config': {'scopes': scopes},
        'settings': {'event_subscriptions': {'bot_events': list(BOT_EVENTS)}, 'interactivity': {'is_enabled': True},
                     'org_deploy_enabled': False, 'socket_mode_enabled': True, 'token_rotation_enabled': False},
    }


def steps(as_owner: bool = False) -> list[str]:
    return [s.format(user=' and the user token' if as_owner else '') for s in STEPS]


def render(name: str, *, as_owner: bool = False) -> str:
    """The manifest as JSON, then the steps (stderr-free text for a terminal)."""
    lines = [json.dumps(manifest(name, as_owner=as_owner), indent=2), '']
    lines += [f'{i}. {s}' for i, s in enumerate(steps(as_owner), 1)]
    return '\n'.join(lines)
