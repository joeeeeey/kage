"""Who may decide, and where an owner card may go.

The decisions config is a small deployment file: `decision_channel` (the private place for owner cards; absent: the
owner's own DM), `approvers` (default: the owner), and an out-of-office window (`ooo`: active, start, end, delegates,
delegate_scope, channel). Authority comes only from the approvers and, inside an active window, the delegates (never for
never-auto cards); names, mentions and message text never authorize. An owner card may only go to a configured
destination or the owner's DM; a malformed config fails closed (every card refused).

Identifier formats are the channel's (passed in as regexes); the owner's id is the DM fallback. Error codes keep the
`invalid_decision_config_*` names; the not-private refusal is named by the owner key (`<key>_card_destination_…`).
"""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path

SCOPES = ('all_except_never_auto', 'none')


def ooo_in_window(config, now):
    ooo = config['ooo']
    if not ooo['active']:
        return False
    return ooo['start'] <= dt.datetime.fromtimestamp(now, dt.timezone.utc) <= ooo['end']


def delegates_now(config, now, never_auto=False):
    ooo = config['ooo']
    if never_auto or ooo['delegate_scope'] != 'all_except_never_auto' or not ooo_in_window(config, now):
        return []
    return list(ooo['delegates'])


def card_route(config, now, never_auto=False):
    """Cards go to the decision channel; only an in-window out-of-office card uses ooo.channel."""
    ooo = config['ooo']
    if not never_auto and ooo['channel'] and ooo_in_window(config, now):
        return {'channel': ooo['channel'], 'delegates': delegates_now(config, now), 'ooo': True,
                'window': [ooo['start'].timestamp(), ooo['end'].timestamp()]}
    return {'channel': config['decision_channel'], 'delegates': [], 'ooo': False, 'window': None}


def owner_like_users(worker_config, buttons, _cache={}):
    """0.77.0: the people who count as the owner besides the owner: the approvers, when the deployment speaks as its
    bot (`intake.identity: bot`) and says so (`intake.owner_like: approvers`); empty otherwise, and on any config error
    (fails closed). Read at most every 30 s."""
    import time
    now = time.monotonic()
    key = (id(worker_config), id(buttons))
    hit = _cache.get(key)
    if hit and now - hit[0] < 30:
        return hit[1]
    users = frozenset()
    try:
        intake = worker_config.load()[0]['intake']
        if intake.get('identity') == 'bot' and intake.get('owner_like') == 'approvers':
            config = buttons.load_config()  # 0.78.10 (F22): the owners when the file names them apart from the approvers
            users = frozenset(str(u) for u in config.get('owners') or config.get('approvers') or [])
    except Exception:  # noqa: BLE001 - a broken config grants nothing
        users = frozenset()
    _cache[key] = (now, users)
    return users


def card_mentions(config, owner_user):
    """0.78.10 (F22): who a card mentions — the active approvers when the decisions config names the owners apart
    (not every owner decides), else the owner."""
    if (config or {}).get('owners') and config.get('approvers'):
        return ' '.join(f'<@{u}>' for u in config['approvers'])
    return f'<@{owner_user}>'


def card_mentioned(config, owner_user, text):
    """Whether a read-back card text carries its mention (the owner's, or an active approver's)."""
    return any(f'<@{u}>' in str(text or '') for u in [owner_user, *((config or {}).get('approvers') or [])])


def decision_channels(config):
    return {config['decision_channel'], *([config['ooo']['channel']] if config['ooo']['channel'] else [])}


def authorize(config, actor, now, never_auto=False):
    """The actor's role, or None. Names, mentions and message text never authorize."""
    if actor in config['approvers']:
        return 'approver'
    if actor in delegates_now(config, now, never_auto):
        return 'ooo_delegate'
    return None


class Authority:
    def __init__(self, *, owner_user, owner_key, user_re, channel_re, dm_re):
        self.fallback, self.key = owner_user, owner_key
        self.user_re, self.channel_re, self.dm_re = user_re, channel_re, dm_re

    def _users(self, value, field, allow_empty=False):
        if not isinstance(value, list) or (not value and not allow_empty) or any(
                not isinstance(u, str) or not self.user_re.fullmatch(u) for u in value):
            raise ValueError(f'invalid_decision_config_{field}')
        return list(dict.fromkeys(value))

    def _channel(self, value, field):
        if not isinstance(value, str) or not self.channel_re.fullmatch(value):
            raise ValueError(f'invalid_decision_config_{field}')
        return value

    @staticmethod
    def _instant(value, field):
        if not isinstance(value, str):
            raise ValueError(f'invalid_decision_config_ooo_{field}')
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            raise ValueError(f'invalid_decision_config_ooo_{field}') from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f'invalid_decision_config_ooo_{field}_requires_timezone')
        return parsed

    def load_config(self, path):
        """A missing file means owner-only in the owner's DM (never a public channel); a malformed one fails closed."""
        path = Path(path).expanduser()
        if not path.exists():
            return {'decision_channel': self.fallback, 'approvers': [self.fallback], 'owners': None,
                    'ooo': {'active': False, 'start': None, 'end': None, 'delegates': [],
                            'delegate_scope': 'none', 'channel': None},
                    'source': 'default'}
        if path.stat().st_mode & 0o022:
            raise ValueError('decision_config_must_not_be_group_or_world_writable')
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('invalid_decision_config_json') from None
        if not isinstance(raw, dict):
            raise ValueError('invalid_decision_config_json')
        # `escalation_channel` is the legacy alias of `decision_channel`.
        if 'decision_channel' in raw and 'escalation_channel' in raw and raw['decision_channel'] != raw['escalation_channel']:
            raise ValueError('invalid_decision_config_conflicting_decision_channel_alias')
        # A configured channel is validated as a real channel id; an absent field falls back to the owner's DM, never a
        # public channel. The owner's id is a trusted internal constant, so it skips the channel validation.
        raw_channel = raw.get('decision_channel', raw.get('escalation_channel'))
        channel = self._channel(raw_channel, 'decision_channel') if raw_channel is not None else self.fallback
        approvers = self._users(raw.get('approvers', [self.fallback]), 'approvers')
        ooo = raw.get('ooo') or {}
        if not isinstance(ooo, dict) or not isinstance(ooo.get('active', False), bool):
            raise ValueError('invalid_decision_config_ooo')
        scope = ooo.get('delegate_scope', 'none')
        if scope not in SCOPES:
            raise ValueError('invalid_decision_config_ooo_delegate_scope')
        start = self._instant(ooo['start'], 'start') if ooo.get('start') is not None else None
        end = self._instant(ooo['end'], 'end') if ooo.get('end') is not None else None
        if ooo.get('active') and (start is None or end is None):
            raise ValueError('invalid_decision_config_active_ooo_requires_start_and_end')
        if start and end and end <= start:
            raise ValueError('invalid_decision_config_ooo_end_must_follow_start')
        ooo_channel = self._channel(ooo['channel'], 'ooo_channel') if ooo.get('channel') is not None else None
        if ooo_channel == channel:
            raise ValueError('invalid_decision_config_ooo_channel_must_differ_from_decision_channel')
        delegates = self._users(ooo.get('delegates', []), 'ooo_delegates', allow_empty=True)
        # 0.78.10 (F22): the owners (owner-like, may switch the approvers) apart from the active approvers; the approvers
        # must be owners (the deployment owner always is one)
        owners = self._users(raw['owners'], 'owners') if raw.get('owners') is not None else None
        if owners is not None and set(approvers) - set(owners) - {self.fallback}:
            raise ValueError('invalid_decision_config_approvers_must_be_owners')
        if ooo.get('active') and scope != 'none' and delegates and not ooo_channel:
            # Delegates are not in the owner-only decision channel and could not see cards.
            raise ValueError('invalid_decision_config_active_ooo_delegates_require_ooo_channel')
        return {'decision_channel': channel, 'approvers': approvers, 'owners': owners,
                'ooo': {'active': ooo.get('active', False), 'start': start, 'end': end,
                        'delegates': delegates, 'delegate_scope': scope, 'channel': ooo_channel},
                'source': str(path)}

    def private_card_destination(self, config, channel):
        """An owner card may only go to a private destination: the configured decision channel (or its out-of-office
        channel), or the owner's DM. The owner's id is the fallback; while the decision channel is that fallback a
        posted card resolves to a DM id, which is trusted only in that unconfigured state. A set membership check, never
        a channel call. Anything else (e.g. a public channel set by mistake) is refused."""
        channel = str(channel)
        if channel in decision_channels(config) or channel == self.fallback:
            return True
        return config.get('decision_channel') == self.fallback and bool(self.dm_re.fullmatch(channel))

    def assert_private_card_destination(self, config, channel):
        """Raise before an owner card is posted to anything but a private destination."""
        if not self.private_card_destination(config, channel):
            raise ValueError(f'{self.key}_card_destination_not_private:' + str(channel))
        return channel
