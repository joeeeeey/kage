"""Routines (standing instructions per task), fired by the supervisor. No model runs here.

`<workspace>/routines.yaml`, a strict YAML subset parsed with the standard library:

    # comment
    routines:
      - id: weekly-rule-review
        schedule: "7 10 * * 1"     # 5-field cron in the owner's local time; or `every: 15m` .. `30d`
        prompt: "…"
        scope: "…"
        enabled: true               # optional: until (YYYY-MM-DD or ISO with offset), provider (codex | claude)

One top-level `routines:` list of flat mappings; scalars are plain, 'single' or "double" quoted strings, integers,
true / false, null. Anything else (nesting, flow collections, block scalars, anchors, tabs) is an error with its line.

Authority: an entry without `proposed_by` (hand-written) is the owner's intent within its `scope` once the owner
confirmed exactly that definition: the supervisor posts one never-auto card (enable / do not enable) per new or edited
definition (or the owner runs `routines enable`); until then it is `awaiting_confirmation`, after "do not enable"
`rejected`, and it never fires. An entry that code wrote after an approval on a `routine-propose` card carries
proposed_by / approved_by / approved_at / proposal and is honored only while SQLite holds that approval for exactly that
definition. `enabled` may change without a new confirmation. Agents never edit the file.

Firing (supervisor step after intake): a due routine becomes a `routine` event (envelope kind routine, authority
<owner key>_intent) on the task-inbox watch of the task's current session -- normal delivery when that session is live,
headless revival when it is dead -- or, for a task without a session, a fresh headless launch. Missed slots fire at
most once (catch-up); a routine whose previous run is still in flight is skipped; ≥ 15 min between runs, ≤ 20
routines and ≤ 48 runs per task per day; kill switch `routines.enabled`.

`Routines` binds the owner (key, name, chat user, the ask hook), the owner's time zone and language (the card and file
words come from the catalog, `routines.*`) and the copy-standard helpers; `Scheduler` runs on one `Routines`.
"""
from __future__ import annotations
import datetime as dt
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from kage import locale

FILE = 'routines.yaml'
KEYS = ('id', 'schedule', 'every', 'prompt', 'scope', 'enabled', 'until', 'provider', 'created_by', 'proposed_by',
        'approved_by', 'approved_at', 'proposal')
APPROVAL_KEYS = ('proposed_by', 'approved_by', 'approved_at', 'proposal')
DIGEST_KEYS = ('id', 'schedule', 'every', 'prompt', 'scope', 'until', 'provider')
ID_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,47}')
PROPOSAL_RE = re.compile(r'rq[0-9a-f]{14}')
EVERY_RE = re.compile(r'(\d+)\s*([mhd])')
UNIT = {'m': 60, 'h': 3600, 'd': 86400}
MIN_EVERY, MAX_EVERY = 15 * 60, 30 * 86400
CRON_FIELDS = (('minute', 0, 59), ('hour', 0, 23), ('dom', 1, 31), ('month', 1, 12), ('dow', 0, 7))
NAMES = {'month': {m: n for n, m in enumerate(('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct',
                                               'nov', 'dec'), 1)},
         'dow': {d: n for n, d in enumerate(('sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'))}}
TRIGGERS = ('schedule', 'catch_up', 'run_now', 'smoke')
GRACE_SECONDS = 600
SMOKE_ROUTINE = 'smoke-routine'
CONFIRM_RETRY_SECONDS, CONFIRM_ATTEMPTS, CONFIRM_PER_STEP = 900, 3, 5  # a card that could not be posted
DDL = '''
  CREATE TABLE IF NOT EXISTS routine_state (
    task_id TEXT NOT NULL, routine_id TEXT NOT NULL, digest TEXT NOT NULL, definition TEXT NOT NULL, file TEXT NOT NULL,
    provenance TEXT NOT NULL, state TEXT NOT NULL, error TEXT, first_seen REAL NOT NULL, updated_at REAL NOT NULL,
    last_slot REAL, next_at REAL, last_run_id TEXT, last_fired_at REAL, PRIMARY KEY(task_id, routine_id));
  CREATE INDEX IF NOT EXISTS routine_state_due ON routine_state(state, next_at);
  CREATE TABLE IF NOT EXISTS routine_runs (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, routine_id TEXT NOT NULL, slot REAL, fired_at REAL NOT NULL,
    trigger TEXT NOT NULL, state TEXT NOT NULL, delivery TEXT, event_id TEXT, dispatch_id TEXT, file TEXT, error TEXT,
    smoke INTEGER NOT NULL DEFAULT 0);
  CREATE INDEX IF NOT EXISTS routine_runs_task ON routine_runs(task_id, fired_at);
  CREATE INDEX IF NOT EXISTS routine_runs_routine ON routine_runs(task_id, routine_id, fired_at);
  CREATE TABLE IF NOT EXISTS routine_proposals (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, routine_id TEXT NOT NULL, entry TEXT NOT NULL, digest TEXT NOT NULL,
    proposer TEXT NOT NULL, card_event TEXT, status TEXT NOT NULL, created_at REAL NOT NULL, decided_by TEXT,
    decided_at REAL, written_at REAL, error TEXT, smoke INTEGER NOT NULL DEFAULT 0);
  CREATE INDEX IF NOT EXISTS routine_proposals_status ON routine_proposals(status, created_at);
  CREATE TABLE IF NOT EXISTS routine_confirmations (
    task_id TEXT NOT NULL, routine_id TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, via TEXT,
    card_event TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    decided_by TEXT, decided_at REAL, card_settled_at REAL, error TEXT, smoke INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(task_id, routine_id, digest));
  CREATE INDEX IF NOT EXISTS routine_confirmations_card ON routine_confirmations(card_event);
'''


def initialize(db):
    db.executescript(DDL)


def _err(line, why):
    return ValueError(f'routines_yaml_line_{line}: {why}')


def _strip_comment(raw):
    quote = None
    for n, ch in enumerate(raw):
        if quote:
            if ch == quote and not (quote == '"' and raw[n - 1] == '\\'):
                quote = None
        elif ch in '"\'':
            quote = ch
        elif ch == '#' and (n == 0 or raw[n - 1] in ' \t'):
            return raw[:n].rstrip()
    return raw.rstrip()


def _scalar(text, line):
    if text == '':
        raise _err(line, 'empty value (nested mappings and lists are not supported)')
    if text[0] == '"':
        if len(text) < 2 or text[-1] != '"':
            raise _err(line, 'unterminated double-quoted string')
        try:
            return json.loads(text)
        except ValueError:
            raise _err(line, 'invalid escape in double-quoted string') from None
    if text[0] == "'":
        if len(text) < 2 or text[-1] != "'" or re.search(r"(?<!')'(?!')", text[1:-1]):
            raise _err(line, 'unterminated single-quoted string')
        return text[1:-1].replace("''", "'")
    if text[0] in '|>':
        raise _err(line, 'block scalars (| >) are not supported; use a double-quoted string with \\n')
    if text[0] in '[{':
        raise _err(line, 'flow collections ([ ] { }) are not supported')
    if text[0] in '&*!%@`':
        raise _err(line, f'{text[0]!r} (anchors, aliases, tags, reserved) is not supported')
    if text in ('true', 'false'):
        return text == 'true'
    if text in ('null', '~'):
        return None
    if re.fullmatch(r'-?\d{1,12}', text):
        return int(text)
    if ': ' in text or text.endswith(':'):
        raise _err(line, 'plain value contains ": " (quote it)')
    return text


def parse(text):
    """{'routines': [...], 'items': [{'line', 'keys': {key: line}}], 'item_indent': n} or ValueError."""
    routines, items, current, item_indent, key_indent = None, [], None, None, None
    for n, raw in enumerate(str(text).splitlines(), 1):
        lead = raw[:len(raw) - len(raw.lstrip(' \t'))]
        if '\t' in lead:
            raise _err(n, 'tabs are not allowed for indentation')
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent, body = len(line) - len(line.lstrip(' ')), line.strip()
        if routines is None:
            match = re.fullmatch(r'routines:\s*(\[\s*\])?', body)
            if indent or not match:
                raise _err(n, 'the file must start with the top-level key `routines:`')
            routines = []
            if match[1]:
                item_indent = -1  # `routines: []`: nothing may follow
            continue
        if item_indent == -1:
            raise _err(n, 'content after `routines: []`')
        if body == '-' or body.startswith('- '):
            if item_indent is None:
                item_indent = indent
            if indent != item_indent:
                raise _err(n, f'list items must be indented by {item_indent} spaces')
            current = {}
            routines.append(current)
            items.append({'line': n, 'keys': {}})
            key_indent = indent + 2
            body = body[1:].strip()
            if not body:
                continue
        elif current is None or indent != key_indent:
            raise _err(n, 'unexpected indentation (each routine is a flat `key: value` mapping under `- `)')
        match = re.fullmatch(r'([a-z_]+):(?:\s+(.*))?', body)
        if not match:
            raise _err(n, 'expected `key: value`')
        key, value = match[1], (match[2] or '').strip()
        if key in current:
            raise _err(n, f'duplicate key {key}')
        current[key] = _scalar(value, n)
        items[-1]['keys'][key] = n
    if routines is None:
        raise _err(1, 'the file must start with the top-level key `routines:`')
    return {'routines': routines, 'items': items, 'item_indent': 2 if item_indent in (None, -1) else item_indent}


def yaml_scalar(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _cron_value(text, name, lo, hi):
    names = NAMES.get(name, {})
    value = names.get(text.lower()) if text.lower() in names else int(text) if re.fullmatch(r'\d{1,2}', text) else None
    if value is None or not lo <= value <= hi:
        raise ValueError(f'schedule_{name}_value_{text}_out_of_range_{lo}_{hi}')
    return value


def parse_cron(expr):
    parts = str(expr).split()
    if len(parts) != 5:
        raise ValueError('schedule_must_have_5_fields_minute_hour_day_month_weekday')
    out = {}
    for text, (name, lo, hi) in zip(parts, CRON_FIELDS):
        values = set()
        for piece in text.split(','):
            step = 1
            if '/' in piece:
                piece, step_text = piece.split('/', 1)
                if not re.fullmatch(r'\d{1,2}', step_text) or int(step_text) < 1:
                    raise ValueError(f'schedule_{name}_step_invalid')
                step = int(step_text)
            if piece == '*':
                start, end = lo, hi
            elif '-' in piece:
                a, b = piece.split('-', 1)
                start, end = _cron_value(a, name, lo, hi), _cron_value(b, name, lo, hi)
                if start > end:
                    raise ValueError(f'schedule_{name}_range_reversed')
            else:
                start = _cron_value(piece, name, lo, hi)
                end = hi if step > 1 else start
            values.update(range(start, end + 1, step))
        if name == 'dow':
            values = {0 if v == 7 else v for v in values}
        out[name] = sorted(values)
    out['dom_any'], out['dow_any'] = parts[2].startswith('*'), parts[4].startswith('*')
    return out


def _day_ok(spec, day):
    if day.month not in spec['month']:
        return False
    dom_ok, dow_ok = day.day in spec['dom'], (day.weekday() + 1) % 7 in spec['dow']
    if spec['dom_any'] and spec['dow_any']:
        return True
    if spec['dom_any']:
        return dow_ok
    if spec['dow_any']:
        return dom_ok
    return dom_ok or dow_ok


def parse_every(text):
    match = EVERY_RE.fullmatch(str(text).strip().lower())
    if not match:
        raise ValueError('every_must_look_like_15m_6h_or_7d')
    seconds = int(match[1]) * UNIT[match[2]]
    if not MIN_EVERY <= seconds <= MAX_EVERY:
        raise ValueError('every_must_be_15m_to_30d')
    return seconds


def definition_digest(entry):
    return hashlib.sha256(json.dumps({k: entry.get(k) for k in DIGEST_KEYS}, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()[:16]


def limits_of(rt):
    try:
        return rt.worker_config.load()[0]['routines']
    except Exception:  # noqa: BLE001 - invalid config: nothing fires (see Scheduler.step)
        return dict(rt.worker_config.DEFAULTS['routines'], enabled=False)


def read_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('routines_yaml_missing_or_not_a_regular_file')
    if path.stat().st_size > 65536:
        raise ValueError('routines_yaml_too_large')
    return path.read_text(encoding='utf-8')


def _task(rt, db, value):
    if value is None:
        tid = rt.registry.task_id_for_cwd(os.getcwd())
        if not tid:
            raise ValueError('run_inside_a_task_workspace_or_pass_--task')
        value = tid
    return rt._resolve_task_arg(db, value)


def set_enabled_text(text, routine_id, enabled):
    doc = parse(text)
    for item, entry in zip(doc['items'], doc['routines']):
        if entry.get('id') != routine_id:
            continue
        lines = text.splitlines()
        if 'enabled' in item['keys']:
            n = item['keys']['enabled'] - 1
            lines[n] = re.sub(r'(enabled:\s*)(true|false)', r'\g<1>' + ('true' if enabled else 'false'), lines[n], count=1)
        else:
            n = item['keys']['id'] - 1
            indent = doc['item_indent'] + 2
            lines.insert(n + 1, ' ' * indent + f'enabled: {"true" if enabled else "false"}')
        return '\n'.join(lines) + '\n'
    raise ValueError('unknown_routine_id')


def _brief(text, cap):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= cap else text[:cap - 1] + '…'


class Routines:
    """The owner-bound half of routines: cron in the owner's time zone, validation, the owner's confirmation and
    proposal cards, the CLIs and the smoke."""

    def __init__(self, language, *, tz, owner_key, owner_name, owner_user, ask_owner, buttons, run_id_env):
        self.language, self.local, self.key, self.name = language, tz, owner_key, owner_name
        self.run_id_env = run_id_env  # the environment variable a dispatched (headless) run carries
        self.owner_user, self.ask_owner, self.buttons = owner_user, ask_owner, buttons  # hooks: rt -> value
        self.reference = dt.datetime(2026, 1, 5, tzinfo=tz).timestamp()  # a Monday; schedule statistics use a fixed window
        self.dow = locale.words(language, 'routines.dow')  # day names, Sunday first
        self.confirm_options = [f'enable={self.msg("routines.option_enable")}:approve',
                                f'skip={self.msg("routines.option_skip")}:reject']
        self.confirm_via = {via: self.msg(f'routines.via_{via}') for via in ('card', 'cli', 'smoke')}
        self.instruction = (f"Routine fired: one of {owner_name}'s standing instructions for this task (authority "
                            f'{owner_key}_intent ONLY within the scope in the file). Do what the prompt says inside that '
                            'scope, update status.md when the task state changes, then resolve this event (observe). '
                            f'Anything outside the scope needs ask-{owner_key}. Never edit routines.yaml; new routines go '
                            'through routine-propose.')
        self.header = self.msg('routines.header', owner=owner_name)
        self.confirm_reply = {
            'confirmed': self.msg('routines.confirm_reply_confirmed', owner=owner_name),
            'rejected': self.msg('routines.confirm_reply_rejected'),
            'needs_revision': self.msg('routines.confirm_reply_needs_revision'),
            f'not_{owner_key}': self.msg('routines.confirm_reply_not_owner', owner=owner_name),
            'stale': self.msg('routines.confirm_reply_stale')}
        self.smoke_prompt = self.msg('routines.smoke_prompt')
        self.Scheduler = functools.partial(Scheduler, self)

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def cron_next(self, spec, after):
        """First slot strictly after `after` (epoch) or None within 5 years."""
        t = dt.datetime.fromtimestamp(after, self.local).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        day = t.date()
        for _ in range(366 * 5):
            if _day_ok(spec, day):
                for hour in spec['hour']:
                    if day == t.date() and hour < t.hour:
                        continue
                    for minute in spec['minute']:
                        if day == t.date() and hour == t.hour and minute < t.minute:
                            continue
                        return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.local).timestamp()
            day += dt.timedelta(days=1)
        return None

    def cron_prev(self, spec, before):
        """Latest slot at or before `before` (epoch) or None within 5 years."""
        t = dt.datetime.fromtimestamp(before, self.local).replace(second=0, microsecond=0)
        day = t.date()
        for _ in range(366 * 5):
            if _day_ok(spec, day):
                for hour in reversed(spec['hour']):
                    if day == t.date() and hour > t.hour:
                        continue
                    for minute in reversed(spec['minute']):
                        if day == t.date() and hour == t.hour and minute > t.minute:
                            continue
                        return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.local).timestamp()
            day -= dt.timedelta(days=1)
        return None

    def cron_stats(self, spec):
        """(min gap seconds between consecutive slots, max slots on one day) over a fixed reference window."""
        slots, t = [], self.reference - 60
        while len(slots) < 3000:
            nxt = self.cron_next(spec, t)
            if nxt is None or (nxt - self.reference > 14 * 86400 and len(slots) >= 2):
                break
            slots.append(nxt)
            t = nxt
        gaps = [b - a for a, b in zip(slots, slots[1:])]
        per_day = {}
        for s in slots:
            if s - self.reference < 14 * 86400:
                day = dt.datetime.fromtimestamp(s, self.local).date()
                per_day[day] = per_day.get(day, 0) + 1
        return (min(gaps) if gaps else 366 * 86400), (max(per_day.values()) if per_day else 1)

    def human(self, definition):
        if definition.get('every'):
            return self.msg('routines.every', every=definition['every'])
        spec = parse_cron(definition['schedule'])
        if len(spec['hour']) == 1 and len(spec['minute']) == 1:
            clock = f'{spec["hour"][0]:02d}:{spec["minute"][0]:02d}'
            if spec['dom_any'] and not spec['dow_any'] and len(spec['month']) == 12:
                return self.msg('routines.weekly') + self.msg('routines.dow_joiner').join(self.dow[d] for d in spec['dow']) + ' ' + clock
            if spec['dom_any'] and spec['dow_any'] and len(spec['month']) == 12:
                return self.msg('routines.daily') + clock
        return f'cron {definition["schedule"]}'

    def parse_until(self, value):
        text = str(value)
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
            day = dt.date.fromisoformat(text)
            return dt.datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=self.local).timestamp()
        try:
            parsed = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('until_must_be_YYYY-MM-DD_or_ISO_with_offset') from None
        if parsed.tzinfo is None:
            raise ValueError('until_needs_a_timezone_offset')
        return parsed.timestamp()

    def validate_entry(self, entry, limits, secret_check=None):
        """Problems of one routine (list of codes)."""
        problems = []
        unknown = sorted(set(entry) - set(KEYS))
        if unknown:
            problems.append('unknown_key_' + unknown[0])
        if not isinstance(entry.get('id'), str) or not ID_RE.fullmatch(entry['id']):
            problems.append('id_must_be_lowercase_letters_digits_and_dashes_1_to_48')
        if bool(entry.get('schedule')) == bool(entry.get('every')):
            problems.append('exactly_one_of_schedule_or_every')
        gap = None
        try:
            if entry.get('schedule'):
                gap, _ = self.cron_stats(parse_cron(entry['schedule']))
            elif entry.get('every'):
                gap = parse_every(entry['every'])
        except ValueError as exc:
            problems.append(str(exc))
        if gap is not None and gap < limits['min_interval_minutes'] * 60:
            problems.append(f'runs_must_be_at_least_{limits["min_interval_minutes"]}_minutes_apart')
        for key, cap in (('prompt', 2000), ('scope', 300)):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > cap or \
                    any(ord(c) < 32 and c not in '\n\t' for c in value):
                problems.append(f'{key}_must_be_text_of_1_to_{cap}_characters')
            elif secret_check:
                try:
                    secret_check(value)
                except ValueError:
                    problems.append(f'{key}_looks_like_it_contains_a_credential')
        if 'enabled' in entry and not isinstance(entry['enabled'], bool):
            problems.append('enabled_must_be_true_or_false')
        if entry.get('provider') not in (None, 'codex', 'claude'):
            problems.append('provider_must_be_codex_or_claude')
        if entry.get('until') is not None:
            try:
                self.parse_until(entry['until'])
            except ValueError as exc:
                problems.append(str(exc))
        if entry.get('created_by') is not None and (not isinstance(entry['created_by'], str) or len(entry['created_by']) > 60):
            problems.append('created_by_must_be_short_text')
        present = [k for k in APPROVAL_KEYS if entry.get(k) is not None]
        if present and len(present) != len(APPROVAL_KEYS):
            problems.append('proposed_by_approved_by_approved_at_and_proposal_go_together')
        if entry.get('approved_by') is not None and not re.fullmatch(r'U[A-Z0-9]{6,15}', str(entry['approved_by'])):
            problems.append('approved_by_must_be_a_slack_user_id')
        if entry.get('proposal') is not None and not PROPOSAL_RE.fullmatch(str(entry['proposal'])):
            problems.append('proposal_must_be_a_routine_proposal_id')
        return problems

    def runs_per_day(self, entry):
        if entry.get('every'):
            return -(-86400 // parse_every(entry['every']))
        return self.cron_stats(parse_cron(entry['schedule']))[1]

    def validate(self, doc, limits, secret_check=None):
        """(routines, problems) for a parsed file. Problems are '<id or #n>: code' strings."""
        problems, seen = [], set()
        routines = doc['routines']
        for n, entry in enumerate(routines):
            name = entry.get('id') if isinstance(entry.get('id'), str) else f'#{n + 1}'
            problems += [f'{name}: {p}' for p in self.validate_entry(entry, limits, secret_check)]
            if name in seen:
                problems.append(f'{name}: duplicate_id')
            seen.add(name)
        if len(routines) > limits['max_per_task']:
            problems.append(f'at_most_{limits["max_per_task"]}_routines_per_task')
        if not problems:
            daily = sum(self.runs_per_day(e) for e in routines if e.get('enabled', True))
            if daily > limits['max_runs_per_day']:
                problems.append(f'enabled_routines_would_run_{daily}_times_a_day_limit_{limits["max_runs_per_day"]}')
        return routines, problems

    def check_file(self, rt, path):
        """CLI `routines validate`: parse + validate + next fire times; never writes."""
        limits = limits_of(rt)
        try:
            doc = parse(read_file(path))
        except ValueError as exc:
            return {'ok': False, 'file': str(path), 'problems': [str(exc)]}
        routines, problems = self.validate(doc, limits, rt.asks.check_secret_free)
        now = time.time()
        out = []
        if not problems:
            for entry in routines:
                nxt = self.cron_next(parse_cron(entry['schedule']), now) if entry.get('schedule') else now + parse_every(entry['every'])
                out.append({'id': entry['id'], 'when': self.human(entry), 'next_if_new': self.iso(nxt), 'enabled': entry.get('enabled', True),
                            'provenance': f'{self.key}_approved' if entry.get('proposal') else f'{self.key}_authored',
                            'confirmation': None if entry.get('proposal') else f'{self.key}_confirms_each_definition_once',
                            'runs_per_day': self.runs_per_day(entry)})
        return {'ok': not problems, 'file': str(path), 'problems': problems, 'routines': out}

    def iso(self, ts):
        return dt.datetime.fromtimestamp(ts, self.local).isoformat(timespec='seconds') if ts else None

    def _owner_only(self, rt):
        if rt._agent_session() or os.environ.get(self.run_id_env):
            raise ValueError(f'only_{self.key}_changes_or_runs_routines_agents_use_routine-propose')

    def list_routines(self, rt, db, db_path, task=None, now=None):
        now = time.time() if now is None else now
        scheduler = self.Scheduler(db, db_path, rt)
        limits = limits_of(rt)
        scheduler.scan(now, limits)
        where, args = ('WHERE task_id=?', (task,)) if task else ('', ())
        rows = db.execute(f'SELECT * FROM routine_state {where} ORDER BY task_id, routine_id', args).fetchall()
        out = []
        for r in rows:
            entry = json.loads(r['definition'])
            out.append({'task_id': r['task_id'], 'routine_id': r['routine_id'], 'state': r['state'], 'error': r['error'],
                        'provenance': r['provenance'], 'when': self.human(entry) if entry.get('id') else None,
                        'confirmation': scheduler.confirmation(r['task_id'], r['routine_id'], r['digest'])
                        if r['provenance'] == f'{self.key}_authored' else None,
                        'next_at': self.iso(r['next_at']), 'last_fired_at': self.iso(r['last_fired_at']), 'file': r['file']})
        return {'ok': True, 'enabled': bool(limits.get('enabled')), 'limits': limits, 'routines': out}

    def _edit_file(self, path, mutate, limits, rt, now):
        """Backup -> mutate text -> parse + validate -> atomic write (mode kept). Returns the new parsed doc."""
        path = Path(path)
        text = read_file(path) if path.exists() else ''
        new = mutate(text)
        doc = parse(new)
        _, problems = self.validate(doc, limits, rt.asks.check_secret_free)
        if problems:
            raise ValueError('routines_yaml_would_be_invalid: ' + '; '.join(problems[:3]))
        if path.exists():
            backup = path.parent / 'history' / 'routines-backups' / \
                f'{dt.datetime.fromtimestamp(now, dt.timezone.utc):%Y%m%dT%H%M%SZ}-{FILE}'
            backup.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(text)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        fd, tmp = tempfile.mkstemp(prefix='.routines.', dir=str(path.parent))
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(new)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return doc

    def append_entry(self, text, entry):
        """Append one rendered entry, keeping the owner's formatting and comments."""
        if not text.strip():
            text = self.header
        elif re.search(r'^routines:\s*\[\s*\]\s*(#.*)?$', text, re.M):
            text = re.sub(r'^routines:\s*\[\s*\]\s*(#.*)?$', 'routines:', text, count=1, flags=re.M)
        current = parse(text)
        pad = ' ' * (current['item_indent'] if current['items'] else 2)
        lines = [f'{pad}- id: {entry["id"]}']
        for key in ('schedule', 'every', 'prompt', 'scope', 'until', 'provider', 'enabled', 'proposed_by', 'approved_by',
                    'approved_at', 'proposal'):
            if entry.get(key) is not None:
                lines.append(f'{pad}  {key}: {yaml_scalar(entry[key])}')
        return text.rstrip('\n') + '\n' + '\n'.join(lines) + '\n'

    def confirm(self, rt, db, task_id, entry, via, actor, now, smoke=False):
        """Record the owner's confirmation of exactly this hand-written definition (terminal `routines enable`, TEST smoke).
        A decided card of the same definition is still settled afterwards: the later action wins."""
        digest = definition_digest(entry)
        with rt.transaction(db):
            db.execute('INSERT INTO routine_confirmations (task_id,routine_id,digest,status,via,created_at,updated_at,'
                       "decided_by,decided_at,smoke) VALUES (?,?,?,'confirmed',?,?,?,?,?,?) ON CONFLICT(task_id,routine_id,"
                       "digest) DO UPDATE SET status='confirmed',via=excluded.via,updated_at=excluded.updated_at,"
                       'decided_by=excluded.decided_by,decided_at=excluded.decided_at,error=NULL',
                       (task_id, entry['id'], digest, via, now, now, actor, now, int(bool(smoke))))
        return digest

    def set_enabled(self, rt, db, db_path, task, routine_id, enabled, now=None):
        now = time.time() if now is None else now
        self._owner_only(rt)
        row = _task(rt, db, task)
        path = Path(row['path']) / FILE
        doc = self._edit_file(path, lambda text: set_enabled_text(text, routine_id, enabled), limits_of(rt), rt, now)
        entry = next(e for e in doc['routines'] if e.get('id') == routine_id)
        # the owner's own `routines enable` is their confirmation of exactly this hand-written definition (no card needed).
        confirmed = self.confirm(rt, db, row['task_id'], entry, 'cli', self.owner_user(rt), now) \
            if enabled and not entry.get('proposal') else None
        self.Scheduler(db, db_path, rt).scan(now, limits_of(rt))
        state = db.execute('SELECT state, next_at FROM routine_state WHERE task_id=? AND routine_id=?',
                           (row['task_id'], routine_id)).fetchone()
        return {'ok': True, 'task_id': row['task_id'], 'routine_id': routine_id, 'enabled': enabled,
                'confirmed_digest': confirmed, 'state': state['state'] if state else None,
                'next_at': self.iso(state['next_at']) if state else None}

    def run_now(self, rt, db, db_path, task, routine_id, now=None, _smoke=False):
        now = time.time() if now is None else now
        if not _smoke:
            self._owner_only(rt)
        row = _task(rt, db, task)
        limits = limits_of(rt)
        if not limits['enabled']:
            raise ValueError('routines_disabled_routines.enabled_false')
        scheduler = self.Scheduler(db, db_path, rt)
        scheduler.scan(now, limits)
        state = db.execute('SELECT * FROM routine_state WHERE task_id=? AND routine_id=?', (row['task_id'], routine_id)).fetchone()
        if state is None or state['routine_id'] == '*':
            raise ValueError('unknown_routine_id')
        if state['state'] != 'active':
            raise ValueError(f'routine_is_{state["state"]}_not_active')
        result = scheduler.fire(state, now, limits, trigger='smoke' if _smoke else 'run_now', force_slot=now)
        rt.start(db_path)
        return {'ok': result['state'] == 'fired', **result}

    def proposal_card_text(self, task_id, entry, proposer=None, smoke=False):
        """the owner-facing (P3.1 copy standard): what, when and how far in plain words; the full prompt, schedule and the
        proposer are on the dashboard (/routines), linked from the card footer."""
        until = self.msg('routines.propose_card_until', date=str(entry['until'])[:10]) if entry.get('until') else ''
        head = self.msg('routines.propose_card_head', task=task_id, when=self.human(entry))
        room = max(10, self.buttons.COPY_FIRST_LINE_MAX - len(head) - 20)  # 20: the question tail and the TEST marker
        return '\n'.join([('🧪 TEST · ' if smoke else '') + self.msg('routines.propose_card_first_line', head=head, prompt=_brief(entry['prompt'], min(50, room))),
                          self.msg('routines.propose_card_scope', scope=_brief(entry['scope'], 100), until=until),
                          self.msg('routines.propose_card_hint')])

    def propose(self, rt, db, db_path, task, entry, now=None, smoke=False):
        """routine-propose: validate against the task's file and limits, then one decision card [批准] [拒绝]."""
        now = time.time() if now is None else now
        provider, session = rt.claude.actor()
        if not session:
            raise ValueError('routine_propose_requires_an_agent_session')
        row = _task(rt, db, task)
        entry = {k: v for k, v in entry.items() if v is not None}
        entry.setdefault('enabled', True)
        limits = limits_of(rt)
        if any(k in entry for k in APPROVAL_KEYS):
            raise ValueError('invalid_routine: approval_fields_are_written_by_code')
        for field in ('prompt', 'scope'):  # P3.1 copy standard: the owner reads both on the card
            if entry.get(field):
                rt.buttons.check_copy(entry[field], 'routine_' + field, first_line_max=None)
        problems = self.validate_entry(entry, limits, rt.asks.check_secret_free)
        if problems:
            raise ValueError('invalid_routine: ' + '; '.join(problems))
        path = Path(row['path']) / FILE
        existing = read_file(path) if path.exists() else ''
        doc = parse(self.append_entry(existing, entry))
        _, problems = self.validate(doc, limits, rt.asks.check_secret_free)
        if problems:
            raise ValueError('routine_does_not_fit_the_task_file: ' + '; '.join(problems[:3]))
        if db.execute("SELECT 1 FROM routine_proposals WHERE task_id=? AND routine_id=? AND status='pending'",
                      (row['task_id'], entry['id'])).fetchone():
            raise ValueError('a_proposal_for_this_routine_id_is_already_pending')
        pid = 'rq' + uuid.uuid4().hex[:14]
        proposer = f'{provider}:{session}'
        with rt.transaction(db):
            db.execute('INSERT INTO routine_proposals (id,task_id,routine_id,entry,digest,proposer,status,created_at,smoke) '
                       'VALUES (?,?,?,?,?,?,?,?,?)', (pid, row['task_id'], entry['id'], json.dumps(entry, ensure_ascii=False),
                                                      definition_digest(entry), proposer, 'posting', now, int(bool(smoke))))
        text = self.proposal_card_text(row['task_id'], entry, proposer, smoke)
        try:
            card = (rt.smoke_escalation(db, db_path, session, provider, row['path'], text[:480], now=now) if smoke else
                    self.ask_owner(rt)(db, db_path, session, provider, row['path'], text, never_auto=True, now=now,
                                footer=self.msg('routines.propose_card_footer', dashboard_url=rt.DASHBOARD_URL)))
        except Exception as exc:
            with rt.transaction(db):
                db.execute("UPDATE routine_proposals SET status='failed',error=? WHERE id=?", (str(exc)[:200], pid))
            raise
        with rt.transaction(db):
            db.execute("UPDATE routine_proposals SET status='pending',card_event=? WHERE id=?", (card['event_id'], pid))
        return {'ok': True, 'proposal_id': pid, 'task_id': row['task_id'], 'routine_id': entry['id'],
                'card_event': card['event_id'], 'card_ts': card['thread_ts'],
                'next': self.msg('routines.propose_next')}

    def write_approved(self, rt, db, prop, actor, now):
        entry = json.loads(prop['entry'])
        if definition_digest(entry) != prop['digest']:
            raise ValueError('proposal_entry_digest_mismatch')
        entry.update(proposed_by=prop['proposer'], approved_by=actor,
                     approved_at=dt.datetime.fromtimestamp(now, self.local).isoformat(timespec='seconds'), proposal=prop['id'])
        path = rt.registry.task_dir(prop['task_id']) / FILE
        self._edit_file(path, lambda text: self.append_entry(text, entry), limits_of(rt), rt, now)
        return path

    def settle_proposals(self, rt, db, now=None):
        """Supervisor step: [批准] -> code writes the entry (backup + validation); [拒绝] -> rejected; a condition -> revise."""
        now = time.time() if now is None else now
        done = []
        for prop in db.execute("SELECT * FROM routine_proposals WHERE status='pending' AND card_event IS NOT NULL").fetchall():
            d = db.execute('SELECT d.*, c.options FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                           'WHERE d.event_id=?', (prop['card_event'],)).fetchone()
            if d is None:
                continue
            option = rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))
            status, error = None, None
            if option['kind'] != 'approve':
                status = 'rejected'
            elif d['note']:
                status = 'needs_revision'
            else:
                try:
                    with rt.transaction(db):  # provenance needs status=written + decided_by before the next scan
                        db.execute("UPDATE routine_proposals SET status='written',decided_by=?,decided_at=?,written_at=? "
                                   "WHERE id=? AND status='pending'", (d['actor'], d['decided_at'], now, prop['id']))
                    self.write_approved(rt, db, prop, d['actor'], now)
                    status = 'written'
                except ValueError as exc:
                    status, error = 'conflict', str(exc)[:300]
            with rt.transaction(db):
                db.execute('UPDATE routine_proposals SET status=?,decided_by=?,decided_at=?,written_at=?,error=? WHERE id=?',
                           (status, d['actor'], d['decided_at'], now if status == 'written' else None, error, prop['id']))
            line = {'written': self.msg('routines.proposal_written'),
                    'rejected': self.msg('routines.proposal_rejected'), 'needs_revision': self.msg('routines.proposal_needs_revision'),
                    'conflict': self.msg('routines.proposal_conflict') + (error or '')}[status]
            rt.rules._card_reply(rt, db, prop['card_event'], ('🧪 TEST · ' if prop['smoke'] else '') + line, prop['id'])
            done.append({'proposal_id': prop['id'], 'status': status, 'error': error})
        return done

    def list_proposals(self, db, limit=50):
        return [{k: r[k] for k in ('id', 'task_id', 'routine_id', 'status', 'proposer', 'decided_by', 'error', 'smoke')} |
                {'created_at': self.iso(r['created_at'])}
                for r in db.execute('SELECT * FROM routine_proposals ORDER BY created_at DESC LIMIT ?', (limit,))]

    def scrub(self, text):
        """Card text built from a hand-written routines.yaml: drop ids / hashes (decision_buttons.scrub_copy)."""
        return self.buttons.scrub_copy(text)

    def confirmation_card_text(self, task_id, entry, edited=False, smoke=False):
        """Short Chinese card (P3.1 copy standard). Prompt and scope were written by hand and passed the file's secret
        check; ids or hashes in them are dropped from the card (scrub), never refused: the file stays the owner's."""
        what = (self.msg('routines.confirm_card_edited', task=task_id, routine=entry['id'], when=self.human(entry)) if edited else
                self.msg('routines.confirm_card_new', task=task_id, routine=entry['id'], when=self.human(entry)))
        ask = self.msg('routines.confirm_card_ask', new_content=self.msg('routines.confirm_card_new_content') if edited else '')
        room = max(10, self.buttons.COPY_FIRST_LINE_MAX - len(what) - len(ask) - 12)  # 12: TEST marker and colon
        first = self.scrub(self.msg('routines.confirm_card_first_line', what=what, prompt=_brief(entry['prompt'], min(50, room)), ask=ask))
        return '\n'.join([('🧪 TEST · ' if smoke else '') + first,
                          self.scrub(self.msg('routines.confirm_card_scope', scope=_brief(entry['scope'], 100))),
                          self.msg('routines.confirm_card_hint')])

    def settle_confirmations(self, rt, db, now=None):
        """Supervisor step: the owner's click on a confirmation card -> confirmed / rejected / needs_revision for exactly that
        definition (no file write). Only the owner's own click counts; a click older than a later `routines enable` is void."""
        now = time.time() if now is None else now
        done = []
        for conf in db.execute('SELECT * FROM routine_confirmations WHERE card_event IS NOT NULL AND card_settled_at IS NULL'
                               ).fetchall():
            d = db.execute('SELECT d.*, c.options FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                           'WHERE d.event_id=?', (conf['card_event'],)).fetchone()
            if d is None:
                continue
            option = rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))
            if d['actor'] != self.owner_user(rt):
                outcome = f'not_{self.key}'
            elif conf['decided_at'] and d['decided_at'] < conf['decided_at']:
                outcome = 'stale'
            elif option['kind'] != 'approve':
                outcome = 'rejected'
            else:
                outcome = 'needs_revision' if d['note'] else 'confirmed'
            key = (conf['task_id'], conf['routine_id'], conf['digest'])
            with rt.transaction(db):
                if outcome == 'stale':
                    db.execute('UPDATE routine_confirmations SET card_settled_at=?,updated_at=? WHERE task_id=? AND '
                               'routine_id=? AND digest=?', (now, now, *key))
                elif outcome == f'not_{self.key}':  # an earlier terminal confirmation stands; otherwise the owner must use the CLI
                    db.execute(f"UPDATE routine_confirmations SET status=CASE WHEN status IN ('confirmed','rejected') THEN status ELSE 'not_{self.key}' END,card_settled_at=?,updated_at=? WHERE task_id=? AND routine_id=? AND digest=?", (now, now, *key))
                else:
                    db.execute("UPDATE routine_confirmations SET status=?,via='card',decided_by=?,decided_at=?,"
                               'card_settled_at=?,updated_at=? WHERE task_id=? AND routine_id=? AND digest=?',
                               (outcome, d['actor'], d['decided_at'], now, now, *key))
            rt.rules._card_reply(rt, db, conf['card_event'], ('🧪 TEST · ' if conf['smoke'] else '') + self.confirm_reply[outcome],
                                 f'{conf["task_id"]}/{conf["routine_id"]}', kind='routine_confirmation_result')
            done.append({'task_id': conf['task_id'], 'routine_id': conf['routine_id'], 'digest': conf['digest'],
                         'outcome': outcome})
        return done

    def disable_smoke_routine(self, rt, db, db_path, task_id, now=None):
        """End of a smoke: the TEST routine never recurs (P3.1 fix). Only code-written TEST workspaces are touched."""
        now = time.time() if now is None else now
        if not rt.registry.is_smoke_task(task_id):
            raise ValueError('only_smoke_tasks')
        path = rt.registry.task_dir(task_id) / FILE
        self._edit_file(path, lambda text: set_enabled_text(text, SMOKE_ROUTINE, False), limits_of(rt), rt, now)
        self.Scheduler(db, db_path, rt).scan(now, limits_of(rt))
        state = db.execute('SELECT state FROM routine_state WHERE task_id=? AND routine_id=?',
                           (task_id, SMOKE_ROUTINE)).fetchone()
        return state['state'] if state else None

    def smoke(self, rt, db, db_path, provider, wait=True, timeout=900.0, poll=3.0, second=True):
        """TEST workspace under the smoke root with one TEST routine, made due now; the installed supervisor fires it
        (no current session -> fresh headless launch), then optionally fires it again (current session -> revival).
        The TEST routine never outlives the smoke: it carries an `until` just past the smoke's timeout (covers --no-wait
        and an interrupted smoke) and is disabled when the smoke finishes."""
        if provider not in ('codex', 'claude'):
            raise ValueError('provider_must_be_codex_or_claude')
        limits = limits_of(rt)
        if not limits['enabled']:
            raise ValueError('routines_disabled_routines.enabled_false')
        path = rt.smoke_dispatch_workspace(provider)
        task_id = rt.registry.task_id_for_cwd(str(path))
        until = dt.datetime.fromtimestamp(time.time() + timeout + 3600, self.local).isoformat(timespec='seconds')
        text = (self.header + self.msg('routines.smoke_yaml', routine=SMOKE_ROUTINE, prompt=yaml_scalar(self.smoke_prompt), until=until, provider=provider))
        (path / FILE).write_text(text, encoding='utf-8')
        now = time.time()
        # Code wrote this TEST routine itself in a TEST workspace: confirmed at once, no card (hand-written ones ask the owner).
        self.confirm(rt, db, task_id, parse(text)['routines'][0], 'smoke', 'smoke', now, smoke=True)
        self.Scheduler(db, db_path, rt).scan(now, limits)
        with rt.transaction(db):
            db.execute('UPDATE routine_state SET next_at=? WHERE task_id=? AND routine_id=?', (now, task_id, SMOKE_ROUTINE))
        rt.start(db_path)
        report = {'ok': False, 'test': True, 'task_id': task_id, 'workspace': str(path), 'provider': provider, 'runs': [],
                  'until': until}
        if not wait:
            return {**report, 'next': f'slack_followup.py routines list --task {task_id} (the TEST routine expires at '
                                      f'{until})'}
        end = time.time() + timeout

        def wait_for(count, predicate):
            while time.time() < end:
                runs = db.execute('SELECT * FROM routine_runs WHERE task_id=? ORDER BY fired_at', (task_id,)).fetchall()
                if len([r for r in runs if r['state'] == 'fired']) >= count and predicate(runs):
                    return runs
                time.sleep(poll)
            return db.execute('SELECT * FROM routine_runs WHERE task_id=? ORDER BY fired_at', (task_id,)).fetchall()

        def settled(runs):
            last = [r for r in runs if r['state'] == 'fired'][-1]
            if last['event_id']:
                e = db.execute('SELECT status FROM events WHERE id=?', (last['event_id'],)).fetchone()
                busy = db.execute("SELECT count(*) FROM dispatches WHERE task_key=? AND state IN ('waiting','running')",
                                  (task_id,)).fetchone()[0]
                return bool(e and e['status'] == 'handled' and not busy)
            d = db.execute('SELECT state FROM dispatches WHERE dispatch_id=?', (last['dispatch_id'],)).fetchone()
            return bool(d and d['state'] not in ('waiting', 'running'))
        started = time.time()
        runs = wait_for(1, settled)
        report['first_fire_seconds'] = round(min((r['fired_at'] for r in runs), default=time.time()) - now, 1)
        if second and runs:
            self.run_now(rt, db, db_path, task_id, SMOKE_ROUTINE, _smoke=True)
            runs = wait_for(2, settled)
        status_md = (path / 'status.md').read_text(encoding='utf-8')
        report['runs'] = [{k: r[k] for k in ('id', 'trigger', 'state', 'delivery', 'event_id', 'dispatch_id')} for r in runs]
        report['status_md_lines'] = status_md.count('- smoke-routine ok')
        report['elapsed_seconds'] = round(time.time() - started, 1)
        try:
            report['routine_state_after'] = self.disable_smoke_routine(rt, db, db_path, task_id)
        except Exception as exc:  # noqa: BLE001 - `until` still ends it
            report['routine_state_after'] = 'disable_failed:' + rt.safe_error(exc)
        fired = [r for r in runs if r['state'] == 'fired']
        report['ok'] = bool(fired and fired[0]['delivery'] == 'launch' and report['first_fire_seconds'] <= 10 and
                            report['status_md_lines'] >= (2 if second else 1) and
                            (not second or (len(fired) >= 2 and fired[1]['delivery'] == 'event')))
        return report


class Scheduler:
    def __init__(self, routines, db, db_path, rt, clock=time.time):
        self.r = routines
        self.db, self.db_path, self.rt, self.clock = db, str(db_path), rt, clock
        self._next_scan = 0.0
        self._files = {}   # path -> (mtime_ns, size, parsed or error)

    def step(self, now=None):
        now = self.clock() if now is None else now
        try:
            limits = self.rt.worker_config.load()[0]['routines']
        except ValueError as exc:
            self.rt._meta_error(self.db, 'routines_config_error', exc)
            return 'config_invalid'
        if not limits['enabled']:
            return 'disabled'
        if now >= self._next_scan:
            self._next_scan = now + limits['scan_seconds']
            self.scan(now, limits)
            self.request_confirmations(now)
        fired = self.fire_due(now, limits)
        return f'fired:{len(fired)}' if fired else 'ok'

    # ---- discovery ----
    def workspaces(self):
        rt = self.rt
        out = []
        for base, smoke in ((rt.registry.root(), False), (rt.registry.smoke_root(), True)):
            try:
                entries = list(os.scandir(base))
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith('.') or (smoke and not entry.name.startswith(rt.registry.SMOKE_PREFIX)):
                    continue
                if entry.is_symlink() and rt.registry.task_id_for_cwd(entry.path) != entry.name:
                    continue  # P39: a renamed task's old-name symlink (task-rename) is not a second task
                path = Path(entry.path) / FILE
                if path.is_file():
                    out.append((entry.name, Path(entry.path), path))
        return out

    def _parsed(self, path):
        try:
            st = path.stat()
        except OSError:
            return None, 'routines_yaml_missing'
        key = (st.st_mtime_ns, st.st_size)
        cached = self._files.get(str(path))
        if cached and cached[0] == key:
            return cached[1], cached[2]
        try:
            doc, error = parse(read_file(path)), None
        except ValueError as exc:
            doc, error = None, str(exc)
        self._files[str(path)] = (key, doc, error)
        return doc, error

    def provenance(self, task_id, entry):
        if not entry.get('proposal'):
            return f'{self.r.key}_authored'
        row = self.db.execute("SELECT * FROM routine_proposals WHERE id=? AND status='written'",
                              (entry['proposal'],)).fetchone()
        ok = row is not None and row['task_id'] == task_id and row['routine_id'] == entry['id'] and \
            row['digest'] == definition_digest(entry) and row['decided_by'] == entry.get('approved_by')
        return f'{self.r.key}_approved' if ok else 'unapproved'

    def confirmation(self, task_id, routine_id, digest):
        """the owner's confirmation of a hand-written definition: confirmed / rejected / pending … or None (never asked)."""
        row = self.db.execute('SELECT status FROM routine_confirmations WHERE task_id=? AND routine_id=? AND digest=?',
                              (task_id, routine_id, digest)).fetchone()
        return row['status'] if row else None

    def first_due(self, entry, now, last_slot=None):
        if entry.get('schedule'):
            return self.r.cron_next(parse_cron(entry['schedule']), last_slot or now)
        return (last_slot or now) + parse_every(entry['every'])

    def scan(self, now, limits):
        """Files -> routine_state. A definition change re-plans the next slot; nothing else is rewritten."""
        db, rt = self.db, self.rt
        seen = set()
        for task_id, task_path, path in self.workspaces():
            doc, error = self._parsed(path)
            routines, problems = (self.r.validate(doc, limits, rt.asks.check_secret_free) if doc else ([], [error]))
            with rt.transaction(db):
                if problems:  # a broken file stops every routine of the task until the owner fixes it
                    db.execute("UPDATE routine_state SET state='invalid',next_at=NULL,error=?,updated_at=? WHERE "
                               "task_id=? AND state!='removed'", ('; '.join(problems)[:500], now, task_id))
                    seen.update((task_id, r[0]) for r in db.execute(
                        "SELECT routine_id FROM routine_state WHERE task_id=? AND state!='removed'", (task_id,)))
                    db.execute('INSERT OR REPLACE INTO routine_state (task_id,routine_id,digest,definition,file,provenance,'
                               'state,error,first_seen,updated_at) VALUES (?,?,?,?,?,?,?,?,coalesce((SELECT first_seen FROM '
                               "routine_state WHERE task_id=? AND routine_id='*'),?),?)",
                               (task_id, '*', '-', '{}', str(path), 'file', 'invalid', '; '.join(problems)[:500], task_id,
                                now, now))
                    seen.add((task_id, '*'))
                    continue
                db.execute("DELETE FROM routine_state WHERE task_id=? AND routine_id='*'", (task_id,))
                for entry in routines:
                    seen.add((task_id, entry['id']))
                    self._upsert(task_id, path, entry, now)
        with rt.transaction(db):
            for row in db.execute("SELECT task_id, routine_id FROM routine_state WHERE state!='removed'").fetchall():
                if (row['task_id'], row['routine_id']) not in seen:
                    if row['routine_id'] == '*':
                        db.execute("DELETE FROM routine_state WHERE task_id=? AND routine_id='*'", (row['task_id'],))
                    else:
                        db.execute("UPDATE routine_state SET state='removed',next_at=NULL,updated_at=? WHERE task_id=? "
                                   'AND routine_id=?', (now, row['task_id'], row['routine_id']))

    def _upsert(self, task_id, path, entry, now):
        db = self.db
        digest = definition_digest(entry)
        row = db.execute('SELECT * FROM routine_state WHERE task_id=? AND routine_id=?', (task_id, entry['id'])).fetchone()
        provenance = self.provenance(task_id, entry)
        confirmed = None if entry.get('proposal') else self.confirmation(task_id, entry['id'], digest)
        until = self.r.parse_until(entry['until']) if entry.get('until') is not None else None
        state = ('unapproved' if provenance == 'unapproved' else 'disabled' if not entry.get('enabled', True) else
                 'expired' if until is not None and until < now else 'rejected' if confirmed == 'rejected' else
                 'awaiting_confirmation' if provenance == f'{self.r.key}_authored' and confirmed != 'confirmed' else 'active')
        definition = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        if row is None:
            db.execute('INSERT INTO routine_state (task_id,routine_id,digest,definition,file,provenance,state,first_seen,'
                       'updated_at,next_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (task_id, entry['id'], digest, definition, str(path), provenance, state, now, now,
                        self.first_due(entry, now) if state == 'active' else None))
            return
        next_at = row['next_at']
        if row['digest'] != digest or (state == 'active' and next_at is None):
            next_at = self.first_due(entry, now, None if row['digest'] != digest else row['last_slot']) \
                if state == 'active' else None
            if next_at is not None and row['digest'] == digest and row['last_slot'] and next_at <= now:
                next_at = self.first_due(entry, now)  # re-enabled after a pause: no burst of old slots
        if state != 'active':
            next_at = None
        db.execute('UPDATE routine_state SET digest=?,definition=?,file=?,provenance=?,state=?,error=NULL,updated_at=?,'
                   'next_at=? WHERE task_id=? AND routine_id=?',
                   (digest, definition, str(path), provenance, state, now, next_at, task_id, entry['id']))

    # ---- the owner's one-time confirmation of hand-written definitions ----
    def request_confirmations(self, now):
        """One never-auto card per unconfirmed definition (supervisor step only, never a CLI scan). No second card for
        the same definition: only a card that could not be posted is retried (CONFIRM_ATTEMPTS, every 15 min)."""
        out = []
        for row in self.db.execute("SELECT * FROM routine_state WHERE state='awaiting_confirmation' "
                                   'ORDER BY first_seen, task_id, routine_id').fetchall():
            if len(out) >= CONFIRM_PER_STEP:
                break
            conf = self.db.execute('SELECT * FROM routine_confirmations WHERE task_id=? AND routine_id=? AND digest=?',
                                   (row['task_id'], row['routine_id'], row['digest'])).fetchone()
            if conf is None or (conf['status'] in ('posting', 'failed') and not conf['card_event'] and
                                conf['attempts'] < CONFIRM_ATTEMPTS and now - conf['updated_at'] >= CONFIRM_RETRY_SECONDS):
                out.append(self.post_confirmation(row, conf, now))
        return out

    def post_confirmation(self, row, conf, now):
        db, rt = self.db, self.rt
        key = (row['task_id'], row['routine_id'], row['digest'])
        smoke = rt.registry.is_smoke_task(row['task_id'])
        edited = db.execute('SELECT 1 FROM routine_confirmations WHERE task_id=? AND routine_id=? AND digest!=? AND '
                            "(status='confirmed' OR card_event IS NOT NULL)", key).fetchone() is not None
        with rt.transaction(db):
            db.execute('INSERT INTO routine_confirmations (task_id,routine_id,digest,status,attempts,created_at,updated_at,'
                       "smoke) VALUES (?,?,?,'posting',?,?,?,?) ON CONFLICT(task_id,routine_id,digest) DO UPDATE SET "
                       "status='posting',attempts=excluded.attempts,updated_at=excluded.updated_at,error=NULL",
                       (*key, (conf['attempts'] if conf else 0) + 1, now, now, int(smoke)))
        try:
            card = rt.supervisor_card(db, self.r.confirmation_card_text(row['task_id'], json.loads(row['definition']), edited,
                                                                 smoke),
                                      self.r.confirm_options, smoke=smoke, kind='routine_confirmation',
                                      refs={'task_id': row['task_id'], 'routine_id': row['routine_id']}, now=now,
                                      footer=self.r.msg('routines.confirm_card_footer', task=row['task_id'], dashboard_url=rt.DASHBOARD_URL),
                                      task_id=None if smoke else row['task_id'])
        except Exception as exc:  # noqa: BLE001 - not posted: retried later; the routine keeps waiting
            with rt.transaction(db):
                db.execute("UPDATE routine_confirmations SET status='failed',error=?,updated_at=? WHERE task_id=? AND "
                           'routine_id=? AND digest=?', (rt.safe_error(exc), now, *key))
            return {'task_id': key[0], 'routine_id': key[1], 'status': 'failed', 'error': rt.safe_error(exc)}
        with rt.transaction(db):
            db.execute("UPDATE routine_confirmations SET status='pending',card_event=?,error=?,updated_at=? WHERE task_id=? "
                       'AND routine_id=? AND digest=?', (card['event_id'], card['warning'], now, *key))
        return {'task_id': key[0], 'routine_id': key[1], 'status': 'pending', 'card_event': card['event_id']}

    # ---- firing ----
    def fire_due(self, now, limits):
        rows = self.db.execute("SELECT * FROM routine_state WHERE state='active' AND next_at IS NOT NULL AND next_at<=? "
                               'ORDER BY next_at LIMIT 20', (now,)).fetchall()
        return [self.fire(row, now, limits) for row in rows]

    def in_flight(self, run, now):
        max_age = 48 * 3600
        try:
            max_age = self.rt.worker_config.load()[0]['dispatch']['max_event_age_hours'] * 3600
        except Exception:  # noqa: BLE001
            pass
        if now - run['fired_at'] >= max_age:  # stale backlog the dispatcher no longer delivers never blocks
            return False
        if run['event_id']:
            e = self.db.execute('SELECT status FROM events WHERE id=?', (run['event_id'],)).fetchone()
            return e is not None and e['status'] != 'handled'
        if run['dispatch_id']:
            d = self.db.execute('SELECT state FROM dispatches WHERE dispatch_id=?', (run['dispatch_id'],)).fetchone()
            return d is not None and d['state'] in ('waiting', 'running')
        return False

    def _advance(self, row, entry, slot, now, run_id=None, fired=False):
        nxt = self.first_due(entry, now, slot if entry.get('schedule') else now)
        with self.rt.transaction(self.db):
            self.db.execute('UPDATE routine_state SET last_slot=?,next_at=?,updated_at=?' +
                            (',last_run_id=?,last_fired_at=?' if fired else '') + ' WHERE task_id=? AND routine_id=?',
                            (slot, nxt, now, *((run_id, now) if fired else ()), row['task_id'], row['routine_id']))

    def _record(self, row, slot, trigger, state, now, **kw):
        run_id = 'rr' + uuid.uuid4().hex[:14]
        with self.rt.transaction(self.db):
            self.db.execute('INSERT INTO routine_runs (id,task_id,routine_id,slot,fired_at,trigger,state,delivery,event_id,'
                            'dispatch_id,file,error,smoke) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (run_id, row['task_id'], row['routine_id'], slot, now, trigger, state, kw.get('delivery'),
                             kw.get('event_id'), kw.get('dispatch_id'), kw.get('file'), kw.get('error'),
                             int(self.rt.registry.is_smoke_task(row['task_id']))))
        return run_id

    def fire(self, row, now, limits, trigger=None, force_slot=None):
        """Deliver one routine run (or record why it was skipped) and plan the next slot."""
        entry = json.loads(row['definition'])
        if force_slot is not None:
            slot = force_slot
        elif entry.get('schedule'):
            slot = self.r.cron_prev(parse_cron(entry['schedule']), now) or row['next_at']
        else:
            slot = row['next_at']
        trigger = trigger or ('catch_up' if now - (row['next_at'] or now) > GRACE_SECONDS else 'schedule')
        last = self.db.execute("SELECT * FROM routine_runs WHERE task_id=? AND routine_id=? AND state='fired' "
                               'ORDER BY fired_at DESC LIMIT 1', (row['task_id'], row['routine_id'])).fetchone()
        if last is not None and self.in_flight(last, now):
            run_id = self._record(row, slot, trigger, 'skipped_in_flight', now)
            self._advance(row, entry, slot, now)
            return {'routine': row['routine_id'], 'task': row['task_id'], 'state': 'skipped_in_flight', 'run': run_id}
        count = self.db.execute("SELECT count(*) FROM routine_runs WHERE task_id=? AND state='fired' AND fired_at>?",
                                (row['task_id'], now - 86400)).fetchone()[0]
        if count >= limits['max_runs_per_day']:
            run_id = self._record(row, slot, trigger, 'skipped_daily_cap', now)
            self._advance(row, entry, slot, now)
            return {'routine': row['routine_id'], 'task': row['task_id'], 'state': 'skipped_daily_cap', 'run': run_id}
        try:
            delivered = self.deliver(row, entry, slot, trigger, now)
        except Exception as exc:  # noqa: BLE001 - one bad routine never stops the loop
            run_id = self._record(row, slot, trigger, 'failed', now, error=self.rt.safe_error(exc))
            self._advance(row, entry, slot, now)
            return {'routine': row['routine_id'], 'task': row['task_id'], 'state': 'failed', 'run': run_id}
        run_id = self._record(row, slot, trigger, 'fired', now, **delivered)
        self._advance(row, entry, slot, now, run_id, fired=True)
        return {'routine': row['routine_id'], 'task': row['task_id'], 'state': 'fired', 'run': run_id, **delivered}

    def routine_text(self, task_id, entry, row, slot, trigger, now, cli):
        approved = row['provenance'] == f'{self.r.key}_approved'
        conf = None if approved else self.db.execute(
            'SELECT via, decided_by, decided_at FROM routine_confirmations WHERE task_id=? AND routine_id=? AND digest=? '
            "AND status='confirmed'", (task_id, entry['id'], row['digest'])).fetchone()
        source = (self.r.msg('routines.source_approved', proposal=entry['proposal'], approved_by=entry['approved_by'], approved_at=entry['approved_at'])
                  if approved else self.r.msg('routines.source_authored_confirmed', owner=self.r.name, via=self.r.confirm_via.get(conf['via'], conf['via']), decided_by=conf['decided_by'], at=self.r.iso(conf['decided_at'])) if conf else self.r.msg('routines.source_authored', owner=self.r.name))
        return '\n'.join([
            self.r.msg('routines.text_title', routine=entry['id']), '',
            f'- task: {task_id}', self.r.msg('routines.text_schedule', when=self.r.human(entry), spec=entry.get('schedule') or 'every ' + entry['every']),
            f'- fired: {self.r.iso(now)} · slot {self.r.iso(slot)} · trigger {trigger}',
            self.r.msg('routines.text_authority', owner=self.r.name, owner_key=self.r.key, source=source),
            f'- file: {row["file"]}', '', '## Scope', '', entry['scope'], '', '## Prompt', '', entry['prompt'], '',
            f'CLI: `{cli}`', '', self.r.instruction, ''])

    def deliver(self, row, entry, slot, trigger, now):
        rt, db = self.rt, self.db
        task_id = row['task_id']
        task_path = rt.registry.task_dir(task_id)
        if not task_path.is_dir():
            raise ValueError('task_workspace_missing')
        with rt.transaction(db):
            rt.registry.ensure_task(db, task_id, now)
        cli = f'python3 {Path(rt.__file__).resolve()} --db {self.db_path}'
        text = self.routine_text(task_id, entry, row, slot, trigger, now, cli)
        directory = task_path / 'history' / 'routines'
        directory.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        path = directory / f'{stamp}-{entry["id"]}.md'
        n = 2
        while path.exists():
            path = directory / f'{stamp}-{entry["id"]}-{n}.md'
            n += 1
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
        task = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        rule = f'routine_{self.r.key}_approved' if row['provenance'] == f'{self.r.key}_approved' else f'routine_{self.r.key}_authored'
        env = rt.envelopes.envelope('routine', 'human', f'{self.r.key}', f'{self.r.key}_intent', rule, None, 0)
        if task['current_session']:
            eid, _ = rt.enqueue_local_event(db, task_id, str(task_path), task['current_provider'], task['current_session'],
                                            'routine', path, 'routine:' + entry['id'], env, now)
            return {'delivery': 'event', 'event_id': eid, 'file': str(path)}
        disp = rt.make_dispatcher(db, self.db_path)
        # P39: a routine run inherits the task's launch preference; the entry's own provider applies to this run only.
        dispatch_id = disp.launch(disp.task_for_key(task_id), entry.get('provider') or 'auto', text,
                                  reason='routine:' + entry['id'], now=now, inherit=True, persist=False)
        return {'delivery': 'launch', 'dispatch_id': dispatch_id, 'file': str(path)}
