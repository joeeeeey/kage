"""Neutral pack templates (O1c2): what a new deployment starts with before it writes its own packs.

A template is never loaded from here. Onboarding copies it into the deployment (`deploy/packs/...`) and pins its
sha256 in kage.lock `packs`, exactly like an organization's or an owner's own pack; `Bridge.pack` refuses a missing or
altered pin either way. `template(name)` returns a template's bytes.
"""
from __future__ import annotations

from pathlib import Path

TEMPLATES = {'default.access': 'default_access.py', 'default.playbook': 'default_playbook.py'}


def template(name):
    """The bytes of a neutral pack template (`default.access`, `default.playbook`)."""
    try:
        return Path(__file__).with_name(TEMPLATES[name]).read_bytes()
    except KeyError:
        raise ValueError(f'unknown_pack_template: {name}') from None
