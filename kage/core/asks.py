"""Ask cards, channel-neutral part: the agent asks other people something in the owner's voice, with buttons.

Limits and durations, the confirmation policy, the card copy in the asker's language (kage.locale `ask.*`), and the
owner's clock. The language of an ask is a short code (`zh`, `en`) mapped to a catalog; an unknown code falls back to
the owner's language. The answer the owner gives on someone's behalf is recorded as `<key>_override`.
"""
from __future__ import annotations
import datetime as dt
import re

from kage import locale

CONFIRM_MODES = ('auto', 'always', 'never')
LANG_CATALOGS = {'zh': 'zh-CN', 'en': 'en'}
LANGS = tuple(LANG_CATALOGS)
DEFAULT_REMIND, DEFAULT_EXPIRE = '10h', '24h'
DAILY_LIMIT = 10
MAX_RESPONDERS = 5
MAX_TEXT = 1500
CARD_ID_RE = re.compile(r'[0-9a-f]{20}')
DURATION_RE = re.compile(r'(\d+)\s*([smhd])')
UNIT = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
LABEL_FORBIDDEN_RE = re.compile(r'[<>@]|https?://|www\.', re.I)
KIND_ICON = {'approve': '✅', 'reject': '❌', 'other': '🔹'}
PHRASE_KEYS = ('hint', 'reminder', 'not_responder', 'handled', 'external', 'chose', 'override', 'expired', 'confirm',
               'confirm_yes', 'confirm_no', 'confirmed', 'confirm_cancelled', 'echo', 'closed')


def parse_duration(value, smoke=False, field='duration'):
    match = DURATION_RE.fullmatch(str(value).strip().lower())
    if not match:
        raise ValueError(f'{field}_must_look_like_30m_10h_or_1d')
    seconds = int(match[1]) * UNIT[match[2]]
    low = 60 if smoke else 3600
    if not low <= seconds <= 7 * 86400:
        raise ValueError(f'{field}_must_be_{"1m" if smoke else "1h"}_to_7d')
    return seconds


def needs_confirmation(mode, option):
    return mode == 'always' or (mode == 'auto' and option['kind'] == 'approve')


class Asks:
    def __init__(self, *, owner_language, owner_key, owner_name, tz):
        default = next((code for code, catalog in LANG_CATALOGS.items() if catalog == owner_language), None)
        if default is None:
            raise ValueError(f'no ask language for the owner language {owner_language!r}')
        self.default_lang, self.owner_name, self.tz = default, owner_name, tz
        self.vias = ('button', 'text_confirmed', 'text_echo', f'{owner_key}_override')
        self.override_via = self.vias[-1]

    def phrase(self, lang, key, **values):
        catalog = LANG_CATALOGS[lang if lang in LANG_CATALOGS else self.default_lang]
        return locale.message(catalog, f'ask.{key}', **{'owner': self.owner_name, **values})

    def phrases(self):
        """{lang: {key: template}} with the owner's name filled in (the shape of the pre-Kage PHRASES table)."""
        return {lang: {key: locale.message(catalog, f'ask.{key}').replace('{owner}', self.owner_name)
                       for key in PHRASE_KEYS} for lang, catalog in LANG_CATALOGS.items()}

    def clock(self, timestamp):
        return dt.datetime.fromtimestamp(timestamp, self.tz).strftime('%H:%M')

    def iso(self, timestamp):
        return dt.datetime.fromtimestamp(timestamp, self.tz).isoformat(timespec='seconds') if timestamp else None
