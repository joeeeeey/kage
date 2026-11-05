"""Deployment profile: who the owner is, which organisation conventions apply, where the instance lives.

A deployment is a directory with three small JSON documents. They are data, never code, and the engine ships
no values for any of them:

  owner.json     kage.owner/v1     the one person this instance serves: name, short name, key, language,
                                   timezone, the owner's identity on each channel, and the assistant (bot) identity
  org.json       kage.org/v1       organisation conventions: the tracker, the ticket reference pattern, and
                                   the names of shared channels
  instance.json  kage.instance/v1  where this instance's files are: the task workspace root and other named
                                   paths, kept exactly as written (a "~/..." path stays "~/..." in text); the
                                   marker this instance puts on the messages its agents send

Loading is strict. Any problem raises ProfileError and nothing is half-loaded, so a caller can fall back as a
whole. A profile file that is group- or world-writable is refused, like any other file that decides who may
approve things.
"""
from __future__ import annotations
import dataclasses
import datetime as dt
import json
from pathlib import Path
import re
import types

OWNER_SCHEMA = 'kage.owner/v1'
ORG_SCHEMA = 'kage.org/v1'
INSTANCE_SCHEMA = 'kage.instance/v1'
FILES = {'owner': 'owner.json', 'org': 'org.json', 'instance': 'instance.json'}
NO_TICKET = '(?!)'  # Org.ticket_pattern() without a tracker: matches nothing

LANGUAGE_RE = re.compile(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*')
OFFSET_RE = re.compile(r'([+-])(\d{2}):(\d{2})')
SLACK_ID_RE = {
    'workspace': re.compile(r'T[A-Z0-9]{4,20}'),
    'user': re.compile(r'[UW][A-Z0-9]{4,20}'),
    'bot_user': re.compile(r'[UB][A-Z0-9]{4,20}'),
    'app': re.compile(r'A[A-Z0-9]{4,20}'),
    'channel': re.compile(r'[CG][A-Z0-9]{4,20}'),
}
WORKSPACE_URL_RE = re.compile(r'https://[a-z0-9][a-z0-9-]*\.slack\.com')
CHANNEL_NAME_RE = re.compile(r'#[a-z0-9][a-z0-9._-]{0,79}')
PATH_NAME_RE = re.compile(r'[a-z][a-z0-9_]{0,63}')
OWNER_KEY_RE = re.compile(r'[a-z][a-z0-9_]{0,31}')
MARKER_RE = re.compile(r'[a-z][a-z0-9_]{2,63}')
DEFAULT_AGENT_MARKER = 'kage_agent_sent'
DEFAULT_ASSISTANT = 'assistant'   # how text names the assistant (bot) when owner.json gives no assistant.display
TRACKERS = ('linear', 'jira', 'none')


class ProfileError(ValueError):
    """The deployment profile is missing, unreadable or invalid."""


@dataclasses.dataclass(frozen=True)
class Owner:
    name: str
    short_name: str     # how text names the owner ("waiting for Mira")
    key: str            # the owner's word in identifiers: CLI values, stored states (default "owner")
    language: str
    timezone: str
    timezone_fallback_offset: str | None
    slack_workspace: str | None
    slack_workspace_url: str | None
    slack_user: str | None
    assistant_display: str | None
    assistant_slack_bot_user: str | None
    assistant_slack_app: str | None

    @property
    def assistant_name(self):
        """How text names the assistant: "post an <name> decision card", "<name> cards"."""
        return self.assistant_display or DEFAULT_ASSISTANT

    def tz(self):
        """The owner's tzinfo. Without a tz database, the profile's fixed fallback offset (validated at load)."""
        return _tzinfo(self.timezone, self.timezone_fallback_offset)


@dataclasses.dataclass(frozen=True)
class Org:
    name: str
    tracker: str
    ticket_ref: str | None        # regex source of one ticket reference, without anchors or groups around it
    ticket_ref_ignorecase: bool
    tracker_team: str | None
    channels: types.MappingProxyType   # shared channel name ("#ops") -> channel id

    def ticket_ref_re(self):
        if self.ticket_ref is None:
            return None
        return re.compile(self.ticket_ref, re.I if self.ticket_ref_ignorecase else 0)

    def ticket_pattern(self):
        """The ticket reference's regex source for the units that compile it: the org's, else one that never matches
        (an org without a tracker has no ticket to find)."""
        return NO_TICKET if self.ticket_ref is None else self.ticket_ref


@dataclasses.dataclass(frozen=True)
class Instance:
    paths: types.MappingProxyType      # name -> path text exactly as written in instance.json
    agent_marker: str = DEFAULT_AGENT_MARKER   # hidden marker (event type) on every message this instance's agents send

    @property
    def workspace_root(self):
        text = self.paths.get('workspace_root')
        return Path(text).expanduser() if text is not None else None

    def path(self, name):
        """The named path, expanded; None when the deployment does not name it."""
        text = self.paths.get(name)
        return Path(text).expanduser() if text is not None else None


@dataclasses.dataclass(frozen=True)
class Deployment:
    directory: Path
    owner: Owner
    org: Org
    instance: Instance


def load(directory):
    """Load owner.json, org.json and instance.json from one deployment directory."""
    directory = Path(directory).expanduser()
    docs = {kind: _read(directory / name) for kind, name in FILES.items()}
    return Deployment(directory=directory, owner=parse_owner(docs['owner']), org=parse_org(docs['org']),
                      instance=parse_instance(docs['instance']))


def parse_owner(raw):
    _schema(raw, OWNER_SCHEMA)
    who = _obj(raw, 'owner', required=True)
    name = _text(who, 'owner.name', required=True)
    short_name = _text(who, 'owner.short_name') or name.split()[0]
    key = _text(who, 'owner.key') or 'owner'
    if not OWNER_KEY_RE.fullmatch(key):
        raise ProfileError('owner.key must be a lower-case word such as owner or mira')
    language = _text(who, 'owner.language', required=True)
    if not LANGUAGE_RE.fullmatch(language):
        raise ProfileError('owner.language must be a language tag such as en or zh-CN')
    timezone = _text(who, 'owner.timezone', required=True)
    fallback = _text(who, 'owner.timezone_fallback_offset')
    if fallback is not None and not OFFSET_RE.fullmatch(fallback):
        raise ProfileError('owner.timezone_fallback_offset must look like +08:00')
    _tzinfo(timezone, fallback)
    slack = _obj(_obj(raw, 'identities'), 'identities.slack')
    assistant = _obj(raw, 'assistant')
    assistant_slack = _obj(assistant, 'assistant.slack')
    owner = Owner(
        name=name, short_name=short_name, key=key, language=language, timezone=timezone,
        timezone_fallback_offset=fallback,
        slack_workspace=_slack_id(slack, 'workspace', 'identities.slack.workspace'),
        slack_workspace_url=_text(slack, 'identities.slack.workspace_url'),
        slack_user=_slack_id(slack, 'user', 'identities.slack.user'),
        assistant_display=_text(assistant, 'assistant.display'),
        assistant_slack_bot_user=_slack_id(assistant_slack, 'bot_user', 'assistant.slack.bot_user'),
        assistant_slack_app=_slack_id(assistant_slack, 'app', 'assistant.slack.app'))
    if owner.slack_workspace_url is not None and not WORKSPACE_URL_RE.fullmatch(owner.slack_workspace_url):
        raise ProfileError('identities.slack.workspace_url must look like https://<name>.slack.com')
    if (owner.slack_user is None) != (owner.slack_workspace is None):
        raise ProfileError('identities.slack needs both workspace and user')
    if owner.slack_user is not None and owner.slack_user == owner.assistant_slack_bot_user:
        raise ProfileError('the owner and the assistant must be different Slack users')
    return owner


def parse_org(raw):
    _schema(raw, ORG_SCHEMA)
    name = _text(raw, 'name', required=True)
    tickets = _obj(raw, 'tickets')
    tracker = _text(tickets, 'tickets.tracker') or 'none'
    if tracker not in TRACKERS:
        raise ProfileError('tickets.tracker must be one of ' + ', '.join(TRACKERS))
    ref = _text(tickets, 'tickets.ref')
    ignorecase = tickets.get('ref_ignorecase', False)
    if not isinstance(ignorecase, bool):
        raise ProfileError('tickets.ref_ignorecase must be true or false')
    if ref is not None:
        try:
            compiled = re.compile(ref)
        except re.error as exc:
            raise ProfileError(f'tickets.ref is not a valid regular expression: {exc}') from None
        if compiled.groups:
            raise ProfileError('tickets.ref must not contain capturing groups; use (?:...)')
        if compiled.match(''):
            raise ProfileError('tickets.ref must not match an empty string')
    if tracker != 'none' and ref is None:
        raise ProfileError('a tracker needs tickets.ref')
    channels = {}
    for channel_name, channel_id in _obj(raw, 'channels').items():
        if not CHANNEL_NAME_RE.fullmatch(channel_name):
            raise ProfileError('channels keys must be lower-case channel names such as #ops')
        if not isinstance(channel_id, str) or not SLACK_ID_RE['channel'].fullmatch(channel_id):
            raise ProfileError(f'channels.{channel_name} is not a Slack channel id')
        channels[channel_name] = channel_id
    return Org(name=name, tracker=tracker, ticket_ref=ref, ticket_ref_ignorecase=ignorecase,
               tracker_team=_text(tickets, 'tickets.team'), channels=types.MappingProxyType(channels))


def parse_instance(raw):
    _schema(raw, INSTANCE_SCHEMA)
    paths = {}
    for name in _obj(raw, 'paths'):
        if not PATH_NAME_RE.fullmatch(name):
            raise ProfileError('paths keys must be snake_case names')
        text = _text(raw['paths'], f'paths.{name}', required=True)
        if not (text.startswith('/') or text.startswith('~/')):
            raise ProfileError(f'paths.{name} must be absolute or start with ~/')
        paths[name] = text
    marker = _text(raw, 'agent_marker') or DEFAULT_AGENT_MARKER
    if not MARKER_RE.fullmatch(marker):
        raise ProfileError('agent_marker must be a lower-case identifier such as kage_agent_sent')
    return Instance(paths=types.MappingProxyType(paths), agent_marker=marker)


def _read(path):
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise ProfileError(f'{path.name} is missing') from None
    except OSError as exc:
        raise ProfileError(f'{path.name} cannot be read: {exc.strerror}') from None
    if mode & 0o022:
        raise ProfileError(f'{path.name} must not be group- or world-writable')
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        raise ProfileError(f'{path.name} is not valid JSON') from None
    if not isinstance(raw, dict):
        raise ProfileError(f'{path.name} must hold a JSON object')
    return raw


def _schema(raw, expected):
    if not isinstance(raw, dict) or raw.get('schema') != expected:
        raise ProfileError(f'schema must be {expected}')


def _obj(raw, key, required=False):
    value = raw.get(key.rsplit('.', 1)[-1]) if isinstance(raw, dict) else None
    if value is None:
        if required:
            raise ProfileError(f'{key} is required')
        return {}
    if not isinstance(value, dict):
        raise ProfileError(f'{key} must be an object')
    return value


def _text(raw, key, required=False):
    value = raw.get(key.rsplit('.', 1)[-1])
    if value is None:
        if required:
            raise ProfileError(f'{key} is required')
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ProfileError(f'{key} must be a non-empty string without surrounding spaces')
    return value


def _slack_id(raw, kind, key):
    value = _text(raw, key)
    if value is not None and not SLACK_ID_RE[kind].fullmatch(value):
        raise ProfileError(f'{key} is not a Slack {kind.replace("_", " ")} id')
    return value


def _tzinfo(name, fallback):
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name)
    except Exception:  # an unknown name, or no tz database on this machine
        try:
            known = zoneinfo.available_timezones()
        except Exception:
            known = set()
        if known:
            raise ProfileError(f'owner.timezone {name!r} is not a known IANA timezone') from None
        if fallback is None:
            raise ProfileError('there is no tz database here and owner.timezone_fallback_offset is not set') from None
        sign, hours, minutes = OFFSET_RE.fullmatch(fallback).groups()
        delta = dt.timedelta(hours=int(hours), minutes=int(minutes))
        return dt.timezone(-delta if sign == '-' else delta)
