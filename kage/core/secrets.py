"""Secrets never land: text the engine sends or stores is refused when it looks like it carries a credential.

The shapes are obvious credentials only (chat, cloud and code-host tokens, private keys, JWTs, `password=` style
assignments). Every language catalog adds its own labelled form (kage.locale `secret.labelled`), and all of them apply
whatever the owner's language is. Links to one-time secret shares are the intended path and pass.
"""
import re

from kage import locale

ERROR = 'text_looks_like_it_contains_a_credential_use_a_one_time_share_link'
SHAPES = (r'xox[abposr]-[A-Za-z0-9-]{8,}', r'xapp-[A-Za-z0-9-]{8,}', r'\bsk-[A-Za-z0-9_-]{16,}',
          r'\bAKIA[0-9A-Z]{16}\b', r'\bASIA[0-9A-Z]{16}\b', r'\bgh[pousr]_[A-Za-z0-9]{20,}',
          r'\bgithub_pat_[A-Za-z0-9_]{20,}', r'\bAIza[0-9A-Za-z_-]{30,}', r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
          r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.')


def patterns():
    """The compiled patterns: the shapes, then each catalog's labelled form (each source once, catalogs in order)."""
    sources = list(SHAPES)
    for language in ('en', *(x for x in locale.languages() if x != 'en')):
        source = locale.pattern(language, 'secret.labelled')
        if source not in sources:
            sources.append(source)
    return tuple(re.compile(s) for s in sources)


PATTERNS = patterns()


def check_secret_free(*texts):
    """Raise ValueError(ERROR) when any text looks like it contains a credential."""
    for text in texts:
        if text and any(p.search(str(text)) for p in PATTERNS):
            raise ValueError(ERROR)
