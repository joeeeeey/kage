"""Language data: the user-facing text (messages) and the patterns guards read, one JSON catalog per language tag.

`kage/locale/<tag>.json` = {"language", "patterns": {key: regex source}, "messages": {key: str.format template},
"words": {key: [phrase, ...]}}.
The owner's language (owner.json) picks the catalog. Every catalog carries the same keys; a missing catalog or key is a
LocaleError, never a silent fallback, so a guard cannot quietly run with the wrong words.
"""
from __future__ import annotations
import functools
import json
from pathlib import Path
import re
import types

HERE = Path(__file__).resolve().parent


class LocaleError(KeyError):
    """No catalog for the language, or no such key in it."""


def languages():
    return sorted(p.stem for p in HERE.glob('*.json'))


@functools.lru_cache(maxsize=None)
def catalog(language):
    """The read-only catalog of one language."""
    path = HERE / f'{language}.json'
    if not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', str(language)) or not path.is_file():
        raise LocaleError(f'no catalog for language {language!r} (have {", ".join(languages())})')
    raw = json.loads(path.read_text(encoding='utf-8'))
    return types.MappingProxyType({'language': raw['language'],
                                   'patterns': types.MappingProxyType(dict(raw['patterns'])),
                                   'messages': types.MappingProxyType(dict(raw['messages'])),
                                   'words': types.MappingProxyType({k: tuple(v) for k, v in raw.get('words', {}).items()})})


def pattern(language, key):
    """The regex source for `key`."""
    try:
        return catalog(language)['patterns'][key]
    except KeyError:
        raise LocaleError(f'{language}: no pattern {key!r}') from None


def words(language, key):
    """The phrase list for `key`, as a tuple in catalog order."""
    try:
        return catalog(language)['words'][key]
    except KeyError:
        raise LocaleError(f'{language}: no words {key!r}') from None


def message(language, key, **values):
    """The text for `key`, formatted with `values`."""
    try:
        template = catalog(language)['messages'][key]
    except KeyError:
        raise LocaleError(f'{language}: no message {key!r}') from None
    return template.format(**values) if values else template
