"""The answers of `kage init` (O1c4): `kage.answers/v1`, the owner's choices, never a secret.

The wizard writes this file and `kage init --answers FILE` reads it, so a run can be repeated or diffed. It holds
options and references to secrets (`kage-secret://<key>/<name>`), never a token: any token-shaped string anywhere
refuses the whole file, and so does an unknown field. Identities are not answers: the Slack workspace, the owner's
user and the bot come only from verified records (kage.onboard.identity), never from this file.

  {"schema": "kage.answers/v1",
   "owner": {"name": "Mira Lind", "short_name": "Mira", "key": "mira", "language": "en", "timezone": "Europe/Berlin"},
   "channel": "slack",
   "slack": {"voice": "assistant", "cards": "dm",
             "secrets": {"slack-bot-token": "kage-secret://mira/slack-bot-token",
                         "slack-app-token": "kage-secret://mira/slack-app-token"}},
   "tracker": "none",
   "workspace_root": "~/kage-tasks"}

`voice` is `assistant` (the bot speaks) or `as_owner` (adds the user token); `cards` is `dm` (the owner's DM with the
bot) or a private channel id. Only `slack` and tracker `none` exist in this release: a console-only deployment needs
the console passkey first (onboarding G3), and a tracker needs its adapter.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile

from kage import profile
from kage.ports import secrets as _secrets

SCHEMA = 'kage.answers/v1'
LANGUAGES = ('en', 'zh-CN')
VOICES = ('assistant', 'as_owner')
CHANNELS = ('slack',)
TRACKERS = ('none',)
REQUIRED_SECRETS = {'assistant': (_secrets.SLACK_BOT, _secrets.SLACK_APP),
                    'as_owner': (_secrets.SLACK_BOT, _secrets.SLACK_APP, _secrets.SLACK_USER)}
CARD_CHANNEL_RE = re.compile(r'[CG][A-Z0-9]{4,20}')
TOKEN_RE = re.compile(r'(?:xox[abposr]|xapp)-[0-9A-Za-z-]{6,}|sk-[A-Za-z0-9_-]{20,}')
FIELDS = {'': {'schema', 'owner', 'channel', 'slack', 'tracker', 'workspace_root'},
          'owner': {'name', 'short_name', 'key', 'language', 'timezone'},
          'slack': {'voice', 'cards', 'secrets'}}
DEFAULT_WORKSPACE_ROOT = '~/kage-tasks'
MAX_BYTES = 16384


class AnswersError(ValueError):
    """The answers are invalid (said which field); nothing was generated."""


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _fields(obj, where):
    if not isinstance(obj, dict):
        raise AnswersError(f'{where or "answers"} must be an object')
    unknown = sorted(set(obj) - FIELDS[where])
    if unknown:
        raise AnswersError(f'unknown field {(where + ".") if where else ""}{unknown[0]}')
    return obj


def _text(obj, key, where, *, required=True, limit=80):
    value = obj.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > limit or any(
            ord(c) < 32 for c in value):
        raise AnswersError(f'{where}.{key} must be a short line of text')
    return value


def default_key(name):
    """A key from a name: the first ascii word, lower case ("Mira Lind" -> "mira"); "owner" when nothing fits."""
    word = re.sub(r'[^a-z0-9_]', '', (name or '').split()[0].lower()) if (name or '').split() else ''
    word = word.lstrip('0123456789_')[:32]
    return word if profile.OWNER_KEY_RE.fullmatch(word or '-') else 'owner'


def validate(raw):
    """The answers, normalized (defaults filled in), or AnswersError."""
    if not isinstance(raw, dict):
        raise AnswersError('answers must be an object')
    if any(TOKEN_RE.search(s) for s in _strings(raw)):
        raise AnswersError('answers must not contain a token: store it with the wizard, then reference it')
    _fields(raw, '')
    if raw.get('schema') != SCHEMA:
        raise AnswersError(f'schema must be {SCHEMA}')
    who = _fields(raw.get('owner'), 'owner')
    name = _text(who, 'name', 'owner')
    short_name = _text(who, 'short_name', 'owner', required=False) or name.split()[0]
    key = _text(who, 'key', 'owner', required=False, limit=32) or default_key(name)
    if not profile.OWNER_KEY_RE.fullmatch(key):
        raise AnswersError('owner.key must be a lower-case word such as mira')
    language = who.get('language')
    if language not in LANGUAGES:
        raise AnswersError('owner.language must be one of ' + ', '.join(LANGUAGES))
    timezone = _text(who, 'timezone', 'owner', limit=64)
    try:
        offset = fallback_offset(timezone)
    except profile.ProfileError as exc:
        raise AnswersError(str(exc)) from None
    channel = raw.get('channel', 'slack')
    if channel not in CHANNELS:
        raise AnswersError('channel must be slack (a console-only deployment needs the console passkey first)')
    tracker = raw.get('tracker', 'none')
    if tracker not in TRACKERS:
        raise AnswersError('tracker must be none (a tracker needs its adapter first)')
    root = raw.get('workspace_root', DEFAULT_WORKSPACE_ROOT)
    if not isinstance(root, str) or not (root.startswith('~/') or root.startswith('/')) or len(root) > 200 or any(
            ord(c) < 32 for c in root) or '/../' in f'{root}/':
        raise AnswersError('workspace_root must be an absolute path or start with ~/')
    slack = _fields(raw.get('slack'), 'slack')
    voice = slack.get('voice', 'assistant')
    if voice not in VOICES:
        raise AnswersError('slack.voice must be one of ' + ', '.join(VOICES))
    cards = slack.get('cards', 'dm')
    if cards != 'dm' and not (isinstance(cards, str) and CARD_CHANNEL_RE.fullmatch(cards)):
        raise AnswersError('slack.cards must be dm or a private channel id')
    refs = slack.get('secrets')
    if not isinstance(refs, dict):
        raise AnswersError('slack.secrets must be an object of secret references')
    wanted = REQUIRED_SECRETS[voice]
    if sorted(refs) != sorted(wanted):
        raise AnswersError('slack.secrets must name exactly ' + ', '.join(wanted))
    for secret_name, ref in refs.items():
        if ref != _secrets.ref(key, secret_name):
            raise AnswersError(f'slack.secrets.{secret_name} must be {_secrets.ref(key, secret_name)}')
    return {'schema': SCHEMA,
            'owner': {'name': name, 'short_name': short_name, 'key': key, 'language': language, 'timezone': timezone},
            'channel': channel,
            'slack': {'voice': voice, 'cards': cards, 'secrets': {n: refs[n] for n in wanted}},
            'tracker': tracker, 'workspace_root': root, '_fallback_offset': offset}


def fallback_offset(timezone):
    """The profile's fixed fallback offset for a timezone: its standard-time UTC offset ("+01:00"; the smaller of two
    fixed dates' offsets, so a re-run in summer writes the same file); ProfileError if unknown."""
    tz = profile._tzinfo(timezone, None)
    minutes = int(min(dt.datetime(2025, m, 1, 12, tzinfo=tz).utcoffset() for m in (1, 7)).total_seconds() // 60)
    sign = '+' if minutes >= 0 else '-'
    return f'{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}'


def public(answers):
    """The answers as written to the file (without derived fields)."""
    return {k: v for k, v in answers.items() if not k.startswith('_')}


def load(path):
    path = Path(path).expanduser()
    try:
        if path.stat().st_size > MAX_BYTES:
            raise AnswersError('answers file too large')
        raw = json.loads(path.read_text(encoding='utf-8'))
    except OSError as exc:
        raise AnswersError(f'answers file unreadable ({type(exc).__name__})') from None
    except ValueError:
        raise AnswersError('answers file is not valid JSON') from None
    return validate(raw)


def write(path, answers):
    """Write the answers 0600 (atomically); returns the path."""
    path = Path(path).expanduser()
    data = json.dumps(public(validate(public(answers))), indent=2, ensure_ascii=False) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.answers-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path
