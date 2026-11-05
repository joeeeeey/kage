"""Language data: the user-facing text (messages) and the patterns guards read, one JSON catalog per language tag.

`kage/locale/<tag>.json` = {"language", "patterns": {key: regex source}, "messages": {key: str.format template},
"words": {key: [phrase, ...]}}.
The owner's language (owner.json) picks the catalog. Every catalog carries the same keys; a missing catalog or key is a
LocaleError, never a silent fallback, so a guard cannot quietly run with the wrong words.

A deployment may word some messages its own way (`override`, from instance.json `assembly.messages`: an example that
names one of its projects, say); the catalog itself stays free of deployment words.
"""
from __future__ import annotations
import functools
import json
from pathlib import Path
import re
import string
import types

HERE = Path(__file__).resolve().parent
HAN_RE = re.compile('[\u3400-\u4dbf\u4e00-\u9fff]')


class LocaleError(KeyError):
    """No catalog for the language, or no such key in it."""


def chinese(language):
    """Whether an owner of this language tag reads Chinese (zh, zh-CN, zh-Hant, …)."""
    return str(language or '').split('-')[0].lower() == 'zh'


def card_lang(language):
    """The language code of Kage's own card words (`zh` / `en`, kage.core.asks) for an owner of this language tag."""
    return 'zh' if chinese(language) else 'en'


def written_for(language, text):
    """Whether `text` is written for an owner of `language`: Han in it for a Chinese owner, a letter in it for anyone
    else. The gate on the agent's owner-facing sentences (card summaries, proposal texts)."""
    text = str(text or '')
    return bool(HAN_RE.search(text)) if chinese(language) else any(c.isalpha() for c in text)


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


_OVERRIDES = {}  # language -> {key: the deployment's template}


def fields(template):
    """The placeholder names of a message template, sorted."""
    return sorted(f for _, f, _, _ in string.Formatter().parse(template) if f is not None)


def override(language, messages):
    """The deployment's own wording for some messages of `language` (replaces any earlier override). Each key must be a
    message of the catalog, with the same placeholders."""
    base = catalog(language)['messages']
    for key, text in messages.items():
        if key not in base:
            raise LocaleError(f'{language}: an override for no message {key!r}')
        if not isinstance(text, str) or fields(text) != fields(base[key]):
            raise LocaleError(f'{language}: the override of {key!r} has other placeholders than the catalog')
    _OVERRIDES[language] = dict(messages)


def message(language, key, **values):
    """The text for `key` (the deployment's override if it has one), formatted with `values`."""
    template = _OVERRIDES.get(language, {}).get(key)
    if template is None:
        try:
            template = catalog(language)['messages'][key]
        except KeyError:
            raise LocaleError(f'{language}: no message {key!r}') from None
    return template.format(**values) if values else template
