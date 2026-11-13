"""The owner's verified identity (O1c3): which Slack user in which workspace this deployment belongs to, proven, never
typed. The proof is recorded as `kage.identity/v1` in the private state directory (0600) and onboarding writes the
profile's `identities.slack` only from it.

Two proofs:
- `user_token`: the owner's checked user token (kage.onboard.tokens.check_user) names them.
- `dm_code`, for the default assistant mode without a user token: `new_challenge` makes a one-time code that is shown
  only in the owner's terminal and stored only as a hash; the owner sends it to the bot as a direct message;
  `prove_by_dm` reads the bot's own DMs with the bot token and accepts exactly one sender — a person of the bot's
  workspace, not the bot — within CODE_TTL. The code is used once. Slackbot's DM with the bot, a deleted person's DM
  and a DM whose history Slack does not show the bot (`channel_not_found` / `not_in_channel`) are skipped, not fatal.
A bot token's auth.test names the bot, not the installer, so it is never a proof of the owner.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import secrets as _random
import stat
import tempfile
import time

from kage.onboard.tokens import TokenRefused, slack_call

SCHEMA = 'kage.identity/v1'
RECORD, CHALLENGE = 'owner-identity.json', 'identity-challenge.json'
BOT_SCHEMA, BOT_RECORD = 'kage.bot/v1', 'bot-identity.json'  # O1c4: the checked bot (no token)
URL_RE, APP_RE, BOT_ID_RE = re.compile(r'https://[a-z0-9][a-z0-9-]*\.slack\.com'), re.compile(r'A[A-Z0-9]{4,20}'), \
    re.compile(r'B[A-Z0-9]{4,20}')
CODE_TTL = 600
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no 0 / O / 1 / I
TEAM_RE, USER_RE = re.compile(r'T[A-Z0-9]{4,20}'), re.compile(r'[UW][A-Z0-9]{4,20}')
MAX_PAGES = 10
SKIPPED_IM_USERS = frozenset({'USLACKBOT'})  # the bot's auto-created DM with Slackbot: its history is never readable
SKIPPED_IM_ERRORS = frozenset({'channel_not_found', 'not_in_channel'})  # one DM the bot cannot read: skip it


def _private_dir(state_dir):
    d = Path(state_dir)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise TokenRefused('state_directory_not_private')
    return d


def _write(path, data):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write('\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _digest(code):
    return hashlib.sha256(code.strip().upper().encode('utf-8')).hexdigest()


def new_challenge(state_dir, *, clock=time.time):
    """A fresh one-time code (replacing any earlier one); only its hash is stored."""
    d = _private_dir(state_dir)
    code = 'KAGE-' + ''.join(_random.choice(ALPHABET) for _ in range(5)) + '-' + ''.join(
        _random.choice(ALPHABET) for _ in range(5))
    _write(d / CHALLENGE, {'schema': 'kage.challenge/v1', 'sha256': _digest(code), 'created': clock()})
    return code


def _record(method, team_id, user_id, evidence, clock):
    if not TEAM_RE.fullmatch(team_id or '') or not USER_RE.fullmatch(user_id or ''):
        raise TokenRefused('identity_ids_malformed')
    return {'schema': SCHEMA, 'method': method, 'team_id': team_id, 'user_id': user_id,
            'verified_at': round(clock(), 3), 'evidence': evidence}


def prove_by_user_token(state_dir, user, *, clock=time.time):
    """`user`: tokens.check_user's result (it already compared the workspace with the bot's)."""
    d = _private_dir(state_dir)
    record = _record('user_token', user['team_id'], user['user_id'], {'token': 'slack-user-token'}, clock)
    _write(d / RECORD, record)
    return record


def _pages(call, token, method, params, key, skip=frozenset()):
    """The items under `key` over Slack's cursor pages; an error in `skip` ends the listing instead of raising."""
    cursor = None
    for _ in range(MAX_PAGES):
        payload, _headers = call(method, token, {**params, **({'cursor': cursor} if cursor else {})})
        if not isinstance(payload, dict) or payload.get('ok') is not True:
            error = payload.get('error') if isinstance(payload, dict) else None
            if error in skip:
                return
            raise TokenRefused(f'slack_{method}_refused_{error or "unknown"}')
        yield from payload.get(key) or []
        cursor = (payload.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            return
    raise TokenRefused(f'slack_{method}_too_many_pages')


def prove_by_dm(state_dir, bot_token, bot, *, call=slack_call, clock=time.time, expect_user=None):
    """Find the code in the bot's DMs; `bot`: tokens.check_bot's result for `bot_token`. Records and returns the
    identity, or raises TokenRefused (the challenge stays for a retry unless it expired). `expect_user` (0.79.3, the
    owner transfer): only that person may send the code; anyone else ends the challenge and nothing is recorded."""
    d = _private_dir(state_dir)
    try:
        challenge = json.loads((d / CHALLENGE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise TokenRefused('no_identity_challenge') from None
    created, now = float(challenge['created']), clock()
    if now - created > CODE_TTL:
        (d / CHALLENGE).unlink()
        raise TokenRefused('identity_challenge_expired')
    senders = {}
    for im in _pages(call, bot_token, 'conversations.list', {'types': 'im', 'limit': 200}, 'channels'):
        if not im.get('id') or im.get('user') in SKIPPED_IM_USERS or im.get('is_user_deleted'):
            continue
        history = _pages(call, bot_token, 'conversations.history',
                         {'channel': im['id'], 'oldest': f'{created:.6f}', 'limit': 200}, 'messages',
                         skip=SKIPPED_IM_ERRORS)
        for m in history:
            if m.get('subtype') or m.get('bot_id') or m.get('user') in (None, bot['bot_user_id']):
                continue
            if _digest(str(m.get('text') or '')) == challenge['sha256']:
                senders.setdefault(m['user'], (im['id'], m.get('ts'), m.get('team')))
    if not senders:
        raise TokenRefused('identity_code_not_found_in_the_bots_direct_messages')
    if len(senders) > 1:
        (d / CHALLENGE).unlink()
        raise TokenRefused('identity_code_sent_by_more_than_one_person')
    (user_id, (channel, ts, team)), = senders.items()
    if expect_user is not None and user_id != expect_user:
        (d / CHALLENGE).unlink()
        raise TokenRefused('identity_code_sent_by_another_person')
    if team is None:
        payload, _headers = call('users.info', bot_token, {'user': user_id})
        info = payload.get('user') if isinstance(payload, dict) and payload.get('ok') is True else None
        if not info or info.get('is_bot'):
            raise TokenRefused('identity_sender_not_a_person')
        team = info.get('team_id')
    if team != bot['team_id']:
        (d / CHALLENGE).unlink()
        raise TokenRefused('identity_sender_of_another_workspace')
    record = _record('dm_code', bot['team_id'], user_id, {'channel': channel, 'ts': ts}, clock)
    _write(d / RECORD, record)
    (d / CHALLENGE).unlink()
    return record


def record_bot(state_dir, bot, app_id, *, clock=time.time):
    """O1c4: keep the checked bot's facts for the profile (`bot`: tokens.check_bot's result; `app_id`: tokens.bot_app's):
    workspace, workspace URL, bot user, bot id, app id. No token, no scope list."""
    d = _private_dir(state_dir)
    url = (bot.get('url') or '').rstrip('/')
    record = {'schema': BOT_SCHEMA, 'team_id': bot['team_id'], 'url': url, 'bot_user_id': bot['bot_user_id'],
              'bot_id': bot['bot_id'], 'app_id': app_id, 'verified_at': round(clock(), 3)}
    _check_bot(record)
    _write(d / BOT_RECORD, record)
    return record


def _check_bot(record):
    if record.get('schema') != BOT_SCHEMA or not TEAM_RE.fullmatch(record.get('team_id') or '') or not URL_RE.fullmatch(
            record.get('url') or '') or not re.fullmatch(r'[UB][A-Z0-9]{4,20}', record.get('bot_user_id') or '') \
            or not BOT_ID_RE.fullmatch(record.get('bot_id') or '') or not APP_RE.fullmatch(record.get('app_id') or ''):
        raise TokenRefused('bot_record_malformed')


def _private_file(path):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077:
        raise TokenRefused('identity_record_not_private')


def load_bot(state_dir):
    """The recorded bot, or None; a malformed record raises TokenRefused."""
    path = Path(state_dir) / BOT_RECORD
    if not path.exists():
        return None
    _private_file(path)
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        raise TokenRefused('bot_record_malformed') from None
    _check_bot(record)
    return record


def load(state_dir):
    """The recorded identity, or None; a malformed record raises TokenRefused."""
    path = Path(state_dir) / RECORD
    if not path.exists():
        return None
    _private_file(path)
    record = json.loads(path.read_text(encoding='utf-8'))
    if record.get('schema') != SCHEMA or record.get('method') not in ('user_token', 'dm_code') or not TEAM_RE.fullmatch(
            record.get('team_id') or '') or not USER_RE.fullmatch(record.get('user_id') or ''):
        raise TokenRefused('identity_record_malformed')
    return record


def status(owner, state_dir):
    """(ok, detail) for doctor: the profile's Slack identity against the record. ok None = adopted (a profile whose
    identity predates the record; nothing to compare)."""
    record = load(state_dir)
    if record is None:
        if owner.slack_user is None:
            return False, 'owner identity not verified'
        return None, 'adopted: the profile identity predates verification'
    if (owner.slack_workspace, owner.slack_user) != (record['team_id'], record['user_id']):
        return False, 'profile identity differs from the verified one'
    return True, f"verified ({record['method']})"
