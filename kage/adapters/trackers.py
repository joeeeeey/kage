"""Issue trackers (N2): what the lifecycle, intake and control need from a deployment's tracker, so core names no
product. The org profile's `tickets.tracker` picks one (`for_name`).

- `display`: the tracker's name in text ("set {tracker} to Done"); empty for `none`, whose copy has no tracker step.
- `read_state(script, ticket_id, timeout)`: the ticket's workflow state, {'state': name, 'type': category}, through the
  deployment's helper script, or None when it cannot be read (the caller stays conservative).
- `title_language_rule`: the error code when a ticket's title / brief must be English and is not (None: no rule).
- `ticket_flag`: the CLI flag that asks for a ticket (`--ticket`; Linear keeps its historic `--linear`).
- `pattern_word`: the tracker's word in the locale patterns (`<tracker>`; `ticket` without a tracker).

`use(tracker)` sets the deployment's tracker for the process (`current()`); text names it as `{tracker}` / `{ticket_flag}`
(kage.locale) and a message key's `.no_tracker` variant replaces it when there is no tracker.

Terminal states are generic workflow categories (Done / Canceled), so `is_terminal` lives here for every tracker.
"""
from __future__ import annotations

import json
import subprocess

TERMINAL_TYPES = ('completed', 'canceled', 'cancelled')
TERMINAL_STATES = ('done', 'canceled', 'cancelled', 'completed', 'duplicate')


def is_terminal(state):
    """A Done / Canceled ticket state ({'state', 'type'} or None)."""
    if not state:
        return False
    if str(state.get('type') or '').lower() in TERMINAL_TYPES:
        return True
    return str(state.get('state') or '').strip().lower() in TERMINAL_STATES


class Tracker:
    """No tracker: no tickets, no state to read, no tracker words in text."""
    name = 'none'
    display = ''
    pattern_word = 'ticket'  # the tracker's word in the locale patterns (`<tracker>`)
    ticket_flag = 'ticket'
    title_language_rule = None

    @property
    def tickets(self):
        return bool(self.display)

    def read_state(self, script, ticket_id, timeout):
        return None

    def __repr__(self):
        return f'<tracker {self.name}>'


class Linear(Tracker):
    """Linear through the deployment's helper script, which prints the issue node of `issue get --json <ID>`."""
    name = 'linear'
    display = 'Linear'
    pattern_word = 'linear'  # the tracker's word in the locale patterns (`<tracker>`)
    ticket_flag = 'linear'
    title_language_rule = 'linear_title_and_brief_must_be_english'

    def read_state(self, script, ticket_id, timeout):
        if script is None or not script.is_file():
            return None
        out = subprocess.run(['python3', str(script), '--json', 'issue', 'get', ticket_id], capture_output=True,
                             text=True, timeout=timeout, check=False)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        state = json.loads(out.stdout).get('state') or {}
        return {'state': state.get('name'), 'type': state.get('type')} if state.get('name') else None


class Jira(Tracker):
    """A name only (as before N2): tickets match the org's pattern; no state reader yet."""
    name = 'jira'
    display = 'Jira'
    pattern_word = 'jira'  # the tracker's word in the locale patterns (`<tracker>`)
    title_language_rule = 'ticket_title_and_brief_must_be_english'


TRACKERS = {t.name: t for t in (Tracker(), Linear(), Jira())}


def for_name(name):
    """The tracker of an org profile's `tickets.tracker` ('none' when unset)."""
    try:
        return TRACKERS[name or 'none']
    except KeyError:
        raise ValueError(f'unknown tracker {name!r} (one of {", ".join(TRACKERS)})') from None


NONE = TRACKERS['none']
_CURRENT = [NONE]


def use(tracker):
    """The deployment's tracker for this process (kage.app and the deployment bridge set it when they load the
    deployment, like its message overrides); text and the owner-bound classes read it."""
    _CURRENT[0] = tracker or NONE


def current():
    return _CURRENT[0]
