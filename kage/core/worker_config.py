"""Digital-worker configuration: stdlib JSON, schema-versioned, validated, fail-closed.

Missing file = defaults. A malformed, unknown-key, symlinked or group/world-writable
file is rejected (never silently replaced by defaults). Every `set` validates the
whole result first, writes atomically (0600) and appends one JSONL change record
(who/when/key/old/new). Model IDs live ONLY in DEFAULTS below; launches pass them
per run (`-m` / `--model`), so ~/.codex/config.toml is never modified.
P1a: the dispatcher reloads it for every dispatch decision and every launched run.
P1b: the local dashboard edits it through `set_many` (same validation, one atomic write, one change
record per key, optimistic `expect_digest` check under a lock, actor "dashboard").
P2: the `intake` section gains optional keys (enabled, channel IDs, oncall channels, poll/burst/dedupe
windows, search queries, API budget, the <owner key>-inbox / <owner key>-oncall task ids and their provider/model/effort);
`model_for_task` gives those two tasks their own model.
P3: optional `rules` (rule engine + weekly learning kill switches) and `routines` (kill switch and limits; the hard
limits are ≥ 15 minutes between runs, ≤ 20 routines and ≤ 48 runs per task per day, so config can only tighten).
P3.1: optional `blockers` (capability-blocker cards and weekly skill proposals: kill switches,
reminder delay, suppression and per-run limits) and `cards` (the owner's thread replies on decision cards: retire the
buttons on his first reply; record a clear 批准 / 不行 / option label as the decision; a provider emoji on every human
message that enters a task; the fast poll of open card threads).
P38: optional `dispatch` keys for daemon health — long-lived tasks run every event fresh (`long_lived_fresh`,
`long_lived_tasks`), the per-session context cap (`max_session_context_mb`), the stranded-delivery revive
(`stranded_revive`), the stuck-claim card (`stuck_claim_minutes`) and the nested `provider_health` monitor.
P39: `dispatch.reserved_intake_slots` (run slots only the <owner key>-inbox / <owner key>-oncall tasks may use, on top of
`concurrency`), and two `provider_health` keys: `oncall_slow_run_minutes` (the <owner key>-oncall task's own "slow" threshold —
its code RCA runs are long by design; 0 = never slow) and `min_runs` (a provider is degraded only once its window holds
at least this many runs). `slow_run_seconds` / `slow_limits` give each long-lived task its threshold.
P40: `dispatch.lean_context` — per task class (`task_class`: inbox / oncall / long_lived / task) what a dispatched Claude
run loads besides its prompt (skill listing, git context, the dispatcher rules in the system prompt, the prompt-cache
TTL); `lean_profile` resolves it (the kill switch `enabled` false = every class as before P40).
P40.1: per class `first_message` (constant = the same -p message for every fresh run, the Run context in a file),
`auto_memory`, `mcp` and `drop_tools`; `dispatch.lean_context.keep_warm` (`keep_warm_spec`) — the supervisor's cache
refresh of the inbox / oncall prefix.
P44: `dispatch.provider_wait_hours` / `probe_min_seconds` / `probe_max_seconds` / `probe_enabled` / `unresolved_retries`
— a dispatch whose provider is down waits (probed, no card) instead of failing (min ≤ max is checked across keys).
P45: `RETIRED_KEYS` — keys of removed features (`blockers.guard_enabled`). A file that still holds one loads: the
value must keep its old type (a bool) and is then dropped, so nothing reads it and the next `set` writes the file
without it. Any other unknown key still fails closed.
"""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile
import time

from kage import locale

SCHEMA_VERSION = 1


PROVIDERS = ('codex', 'claude')


EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


MODEL_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,79}')


CATEGORY_RE = re.compile(r'[a-z][a-z0-9_-]{0,31}')


CHANNEL_RE = re.compile(r'#[a-z0-9][a-z0-9._-]{0,79}|[CG][A-Z0-9]{2,}')


DURATION_RE = re.compile(r'(\d+)\s*([smhd])')


UNIT = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}


OPTIONAL_SECTIONS = ('dispatch', 'rules', 'routines', 'blockers', 'cards', 'tasks', 'feedback_learning')


RETIRED_KEYS = {'blockers': ('guard_enabled',)}  # the P3.1 run-end guard


# Phase 2 C1: sections the config may carry that are validated when present and never filled in from the defaults
# (a config without one reads and prints exactly as before); readers merge their own defaults (`control_settings`).
SPARSE_SECTIONS = ('control',)


# The control entry (kage.core.control): off unless the config turns it on. `task` None = `<owner key>-control`;
# `model` / `effort` None = the provider's defaults (`model_for`).
CONTROL_DEFAULTS = {'enabled': False, 'task': None, 'provider': 'claude', 'model': None, 'effort': None,
                    'retention_days': 30, 'answer_timeout_minutes': 15, 'allow_launch': True, 'messages_per_hour': 30,
                    'relays_per_hour': 20, 'launches_per_day': 10, 'history_turns': 8}
CONTROL_RANGES = {'retention_days': (1, 365), 'answer_timeout_minutes': (2, 240), 'messages_per_hour': (1, 600),
                  'relays_per_hour': (1, 200), 'launches_per_day': (0, 100), 'history_turns': (0, 40)}


def control_settings(config, owner_key):
    """The control section of a loaded config with its defaults (the task named by the owner key when not set)."""
    raw = (config or {}).get('control') if isinstance(config, dict) else None
    out = {**CONTROL_DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    out['task'] = out['task'] or f'{owner_key}-control'
    return out


def _validate_control(spec):
    _keys(spec, CONTROL_DEFAULTS, 'control')
    for key, value in spec.items():
        field = f'control_{key}'
        if key in ('enabled', 'allow_launch'):
            _bool(value, field)
        elif key == 'task':
            if value is not None and (not isinstance(value, str) or not TASK_ID_RE.fullmatch(value)):
                _fail(field, 'must_be_a_task_id')
        elif key == 'provider':
            if value not in PROVIDERS:
                _fail(field, 'must_be_one_of_' + '_'.join(PROVIDERS))
        elif key == 'model':
            if value is not None and (not isinstance(value, str) or not MODEL_RE.fullmatch(value)):
                _fail(field, 'must_be_a_model_id')
        elif key == 'effort':
            if value is not None and value not in EFFORTS:
                _fail(field, 'must_be_one_of_' + '_'.join(EFFORTS))
        else:
            _int(value, field, *CONTROL_RANGES[key])


def _open_sparse(config, key):
    """Before a `control.*` key is set on a config without that section: an empty section to set it in."""
    head = str(key).split('.', 1)[0]
    if head in SPARSE_SECTIONS and head not in config:
        config[head] = {}


OPEN_POLL_MIN = 15


UNBOUND_POLICIES = ('revive', 'legacy')


ENV_NAME_RE = re.compile(r'[A-Z][A-Z0-9_]{1,63}')


PROVIDER_KEYS = {'claude': ('model_default', 'model_important', 'effort', 'yolo_args'),
                 'codex': ('model', 'effort', 'yolo_args')}


def _fail(field, why='invalid'):
    raise ValueError(f'invalid_worker_config_{field}_{why}')


def _keys(value, allowed, field):
    if not isinstance(value, dict):
        _fail(field, 'must_be_an_object')
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _fail(field, 'unknown_key_' + re.sub(r'[^a-z0-9_]', '_', str(unknown[0]).lower())[:40])


def _int(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail(field, f'must_be_an_integer_{low}_to_{high}')


def _duration(value, field):
    match = DURATION_RE.fullmatch(str(value).strip().lower()) if isinstance(value, str) else None
    if not match:
        _fail(field, 'must_look_like_10h')
    seconds = int(match[1]) * UNIT[match[2]]
    if not 3600 <= seconds <= 7 * 86400:
        _fail(field, 'must_be_1h_to_7d')
    return seconds


def _args(value, field):
    if not isinstance(value, list) or len(value) > 10 or any(
            not isinstance(a, str) or not a or len(a) > 80 or any(c.isspace() or ord(c) < 32 for c in a) for a in value):
        _fail(field, 'must_be_a_list_of_1_to_10_plain_arguments')


def _bool(value, field):
    if not isinstance(value, bool):
        _fail(field, 'must_be_true_or_false')


LEAN_CLASSES = ('inbox', 'oncall', 'long_lived', 'task')  # P40, see task_class


LEAN_SKILLS = ('full', 'names', 'keep', 'none')


LEAN_TTLS = ('default', '5m', '1h')


LEAN_FIRST_MESSAGES = ('constant', 'per_run')  # P40.1


LEAN_MCP = ('inherit', 'none')  # P40.1


SKILL_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,79}')


TOOL_NAME_RE = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,79}')  # P40.1 drop_tools: Claude Code tool names (MCP: mcp__x__y)


PROVIDER_WAIT_RANGES = {'provider_wait_hours': (0, 168), 'probe_min_seconds': (30, 3600),  # P44
                        'probe_max_seconds': (60, 21600), 'unresolved_retries': (0, 5)}


KEEP_WARM_RANGES = {'interval_minutes': (1, 55), 'max_idle_hours': (0, 168)}  # P40.1; the TTL bounds the interval


PROVIDER_HEALTH_RANGES = {'enabled': None, 'window_minutes': (5, 1440), 'degrade_threshold': (1, 100),
                          'recover_checks': (1, 60), 'slow_run_minutes': (1, 1440),
                          'oncall_slow_run_minutes': (0, 1440), 'min_runs': (1, 100)}  # P39: the last two


SECTION_RANGES = {  # P3: (low, high) for integers; booleans are listed with None
    'rules': {'enabled': None, 'proposals_enabled': None, 'proposal_suppress_days': (1, 365),
              'max_proposals_per_run': (1, 5), 'typed_approval_max_age_hours': (0, 168)},
    'routines': {'enabled': None, 'min_interval_minutes': (15, 1440), 'max_per_task': (1, 20),
                 'max_runs_per_day': (1, 48), 'scan_seconds': (10, 600)},
    'blockers': {'enabled': None, 'skill_proposals_enabled': None, 'reminder_hours': (1, 168),
                 'skill_proposal_suppress_days': (1, 365), 'max_skill_proposals_per_run': (1, 5)},
    'cards': {'retire_buttons_on_reply': None, 'thread_reply_decisions': None, 'read_receipts': None,
              'open_poll_seconds': (0, 900), 'task_thread': None, 'realtime_replies': None, 'reconcile_days': (1, 60),
              'reconcile_minutes': (0, 1440), 'thread_retire_hours': (0, 720),
              'retire_on': ('plan_change', 'any_reply'), 'max_open_per_task': (1, 20)},
    'tasks': {'orphan_sweep_enabled': None, 'deadline_watchdog_enabled': None, 'orphan_idle_hours': (1, 720),
              'deadline_lead_hours': (0, 720), 'sweep_interval_minutes': (5, 1440), 'max_wakes': (0, 50),
              'wake_window_hours': (1, 720), 'stale_card_days': (0, 30)},
    # P35: strong_signal_mode is a string enum (tuple of allowed strings); the rest are bool / int as above.
    'feedback_learning': {'enabled': None, 'strong_signal_mode': ('conservative', 'moderate', 'aggressive'),
                          'max_promote_cards_per_day': (0, 20), 'suppress_days': (1, 365)}}


INTAKE_REQUIRED = ('channels', 'burst_seconds')  # the v1 keys; the P2 keys are optional


TASK_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}')


QUERY_FORBIDDEN_RE = re.compile(r'[\x00-\x1f\x7f]')


APP_PATTERN_RE = re.compile(r'[A-Za-z0-9*_-]{1,60}')  # P42.1 intake.delegate_bot_names


def _channel_list(value, field):
    """0-50 distinct `#name` or channel IDs (an empty list is allowed, as before P2)."""
    if not isinstance(value, list) or len(value) > 50 or any(
            not isinstance(c, str) or not CHANNEL_RE.fullmatch(c) for c in value) or len(set(value)) != len(value):
        _fail(field, 'must_be_distinct_channel_names_or_ids')


def _drop_retired(raw):
    """P45: a retired key still in the file must be a bool (as before: fail closed); it is dropped in place."""
    for section, keys in RETIRED_KEYS.items():
        spec = raw.get(section) if isinstance(raw, dict) else None
        for key in keys:
            if isinstance(spec, dict) and key in spec:
                _bool(spec.pop(key), f'{section}_{key}')
    return raw


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('worker_config_must_be_a_regular_file')
    if path.stat().st_mode & 0o022:
        raise ValueError('worker_config_must_not_be_group_or_world_writable')
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('invalid_worker_config_json') from None
    return raw


def provider_order(config, category):
    """Primary provider for a task category followed by fallbacks (P1 consumer helper)."""
    primary = config['routing']['categories'].get(category, config['routing']['fallback_order'][0])
    return [primary] + [p for p in config['routing']['fallback_order'] if p != primary]


def fallback_after(config, provider):
    """Providers to try after `provider` failed at the infrastructure level, in configured order."""
    order = list(config['routing']['fallback_order'])
    order += [p for p in PROVIDERS if p not in order]
    return [p for p in order if p != provider]


def model_for(config, provider, important=False):
    """(model, effort, yolo_args) for a dispatched run; model IDs come only from the config."""
    spec = config['providers'][provider]
    model = spec['model'] if provider == 'codex' else spec['model_important' if important else 'model_default']
    return model, spec['effort'], list(spec['yolo_args'])


LEAN_OFF = {'skills': 'full', 'git_context': True, 'rules_in_system_prompt': False, 'cache_ttl': 'default',
            'keep_skills': [], 'first_message': 'per_run', 'auto_memory': True, 'mcp': 'inherit', 'drop_tools': []}


def _parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _lookup(config, parts):
    node = config
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_mode & 0o022:
        raise ValueError('worker_config_directory_must_not_be_group_or_world_writable')
    fd, tmp = tempfile.mkstemp(prefix='.config.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_log(path, record):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def _parts(key):
    parts = str(key).split('.')
    if not parts or any(not re.fullmatch(r'[a-z0-9_-]{1,40}', p) for p in parts) or parts[0] == 'schema_version':
        raise ValueError('invalid_worker_config_key')
    return parts


def _apply(config, key, new):
    """Set `key` on `config` in place; returns (found, old). Unknown keys raise."""
    parts = _parts(key)
    found, old = _lookup(config, parts)
    parent_found, parent = _lookup(config, parts[:-1])
    # New keys are allowed only inside the open routing.categories map; validate() rejects the rest.
    if not found and not (parent_found and isinstance(parent, dict)):
        raise ValueError('unknown_worker_config_key')
    parent[parts[-1]] = copy.deepcopy(new)
    return found, old


def digest(config):
    """Stable fingerprint of a loaded config (used for optimistic concurrency by the dashboard)."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


@contextlib.contextmanager
def _write_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_name('.' + path.name + '.lock'), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _record(now, who, key, found, old, new):
    return {'at': dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec='seconds'), 'ts': now,
            'who': who, 'key': key, 'old': old if found else None, 'new': new, 'schema_version': SCHEMA_VERSION}


class WorkerConfig:
    """The deployment-bound half of the worker configuration: the defaults (the owner's models, providers,
    task ids and skills), the environment variable naming the config file and the default path."""

    def __init__(self, language, *, defaults, config_env, default_path, owner_key, owner_name):
        self.language = language
        self.defaults = defaults
        self.config_env = config_env
        self.default_path = default_path
        self.key, self.name = owner_key, owner_name

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def config_path(self):
        return Path(os.environ.get(self.config_env) or self.default_path).expanduser()

    def log_path(self, path=None):
        path = Path(path) if path else self.config_path()
        return path.with_name(path.stem + '.changes.jsonl')

    def validate(self, raw):
        """Full validation of a complete config object; returns it unchanged or raises ValueError."""
        _keys(raw, (*self.defaults, *SPARSE_SECTIONS), 'top_level')
        if raw.get('schema_version') != SCHEMA_VERSION:
            _fail('schema_version', f'must_be_{SCHEMA_VERSION}')
        for key in self.defaults:
            if key not in raw and key not in OPTIONAL_SECTIONS:
                _fail(key, 'missing')
        if 'dispatch' in raw:
            self._validate_dispatch(raw['dispatch'])
        if 'rules' in raw:
            self._validate_section(raw['rules'], 'rules')
        if 'routines' in raw:
            self._validate_section(raw['routines'], 'routines')
        if 'blockers' in raw:
            self._validate_section(raw['blockers'], 'blockers')
        if 'cards' in raw:
            self._validate_section(raw['cards'], 'cards')
            if 0 < raw['cards'].get('open_poll_seconds', 45) < OPEN_POLL_MIN:  # 0 = off; never faster than 15 s
                _fail('cards_open_poll_seconds', f'must_be_0_or_{OPEN_POLL_MIN}_to_900')
        if 'tasks' in raw:
            self._validate_section(raw['tasks'], 'tasks')
        if 'feedback_learning' in raw:
            self._validate_section(raw['feedback_learning'], 'feedback_learning')
        if 'control' in raw:
            _validate_control(raw['control'])
        providers = raw['providers']
        _keys(providers, PROVIDERS, 'providers')
        for name in PROVIDERS:
            spec = providers.get(name)
            if spec is None:
                _fail(f'providers_{name}', 'missing')
            _keys(spec, PROVIDER_KEYS[name], f'providers_{name}')
            for key in PROVIDER_KEYS[name]:
                if key not in spec:
                    _fail(f'providers_{name}_{key}', 'missing')
                if key.startswith('model') and (not isinstance(spec[key], str) or not MODEL_RE.fullmatch(spec[key])):
                    _fail(f'providers_{name}_{key}', 'must_be_a_model_id')
            if spec['effort'] not in EFFORTS:
                _fail(f'providers_{name}_effort', 'must_be_one_of_' + '_'.join(EFFORTS))
            _args(spec['yolo_args'], f'providers_{name}_yolo_args')
        routing = raw['routing']
        _keys(routing, ('categories', 'fallback_order'), 'routing')
        categories = routing.get('categories')
        if not isinstance(categories, dict) or not categories or len(categories) > 32:
            _fail('routing_categories', 'must_be_a_non_empty_object')
        for name, provider in categories.items():
            if not CATEGORY_RE.fullmatch(str(name)) or provider not in PROVIDERS:
                _fail('routing_categories', 'must_map_category_to_codex_or_claude')
        order = routing.get('fallback_order')
        if not isinstance(order, list) or not order or len(set(order)) != len(order) or any(p not in PROVIDERS for p in order):
            _fail('routing_fallback_order', 'must_list_distinct_providers')
        breaker = raw['circuit_breaker']
        _keys(breaker, ('round_trips_per_hour',), 'circuit_breaker')
        _int(breaker.get('round_trips_per_hour'), 'circuit_breaker_round_trips_per_hour', 1, 1000)
        self._validate_intake(raw['intake'])
        ask = raw['ask']
        _keys(ask, ('remind_after', 'expire_after'), 'ask')
        if _duration(ask.get('remind_after'), 'ask_remind_after') >= _duration(ask.get('expire_after'), 'ask_expire_after'):
            _fail('ask_remind_after', 'must_be_shorter_than_expire_after')
        return raw

    def _validate_dispatch(self, spec):
        """Present keys are validated; missing keys take DEFAULTS (see `load`)."""
        _keys(spec, self.defaults['dispatch'], 'dispatch')
        for key, value in spec.items():
            field = f'dispatch_{key}'
            if key in ('enabled', 'notify_cards', 'progress_updates', 'long_lived_fresh', 'stranded_revive', 'probe_enabled'):
                _bool(value, field)
            elif key == 'long_lived_tasks':
                if not isinstance(value, list) or len(value) > 20 or len(set(map(str, value))) != len(value) or any(
                        not isinstance(t, str) or not TASK_ID_RE.fullmatch(t) for t in value):
                    _fail(field, 'must_be_up_to_20_distinct_task_workspace_names')
            elif key == 'max_session_context_mb':
                _int(value, field, 0, 4096)
            elif key == 'stuck_claim_minutes':
                if isinstance(value, bool) or not isinstance(value, int) or not (value == 0 or 10 <= value <= 1440):
                    _fail(field, 'must_be_0_or_10_to_1440')
            elif key == 'provider_health':
                _keys(value, self.defaults['dispatch']['provider_health'], field)
                for name, item in value.items():
                    bounds = PROVIDER_HEALTH_RANGES[name]
                    if bounds is None:
                        _bool(item, f'{field}_{name}')
                    else:
                        _int(item, f'{field}_{name}', *bounds)
            elif key == 'lean_context':  # P40
                self._validate_lean_context(value, field)
            elif key == 'concurrency':
                _int(value, field, 1, 20)
            elif key == 'reserved_intake_slots':  # P39
                _int(value, field, 0, 10)
            elif key == 'liveness_cache_seconds':
                _int(value, field, 1, 600)
            elif key == 'partial_retries':
                _int(value, field, 0, 3)
            elif key == 'max_run_seconds':
                _int(value, field, 60, 86400)
            elif key == 'failed_cooldown_seconds':
                _int(value, field, 60, 7 * 86400)
            elif key == 'max_event_age_hours':
                _int(value, field, 1, 720)
            elif key in PROVIDER_WAIT_RANGES:  # P44
                _int(value, field, *PROVIDER_WAIT_RANGES[key])
            elif key == 'claude_env_passthrough':
                if not isinstance(value, list) or len(value) > 20 or any(
                        not isinstance(v, str) or not ENV_NAME_RE.fullmatch(v) for v in value):
                    _fail(field, 'must_be_a_list_of_up_to_20_env_var_names')
            elif key == 'login_shell':
                if not isinstance(value, str) or not value.startswith('/') or not os.path.isfile(value):
                    _fail(field, 'must_be_an_existing_absolute_path')
            elif key == 'unbound_watches':
                if value not in UNBOUND_POLICIES:
                    _fail(field, 'must_be_revive_or_legacy')
            elif key == 'retry_backoff_seconds':
                if not isinstance(value, list) or len(value) > 5 or any(
                        isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 3600 for v in value):
                    _fail(field, 'must_be_a_list_of_0_to_5_integers_1_to_3600')
        merged = {**self.defaults['dispatch'], **spec}
        if merged['probe_min_seconds'] > merged['probe_max_seconds']:  # P44
            _fail('dispatch_probe_min_seconds', 'must_not_exceed_probe_max_seconds')

    def _validate_lean_context(self, spec, field):
        """P40: `enabled` and, per class, the keys of DEFAULTS (missing ones take the defaults, see with_optional_defaults).
        P40.1: the class keys first_message / auto_memory / mcp / drop_tools and the `keep_warm` object."""
        defaults = self.defaults['dispatch']['lean_context']
        _keys(spec, defaults, field)
        for name, item in spec.items():
            if name == 'enabled':
                _bool(item, f'{field}_enabled')
                continue
            sub = f'{field}_{name}'
            _keys(item, defaults[name], sub)
            if name == 'keep_warm':
                for key, value in item.items():
                    if key == 'enabled':
                        _bool(value, f'{sub}_enabled')
                    else:
                        _int(value, f'{sub}_{key}', *KEEP_WARM_RANGES[key])
                continue
            for key, value in item.items():
                if key == 'skills' and value not in LEAN_SKILLS:
                    _fail(f'{sub}_skills', 'must_be_one_of_' + '_'.join(LEAN_SKILLS))
                elif key == 'cache_ttl' and value not in LEAN_TTLS:
                    _fail(f'{sub}_cache_ttl', 'must_be_default_5m_or_1h')
                elif key in ('git_context', 'rules_in_system_prompt', 'auto_memory'):
                    _bool(value, f'{sub}_{key}')
                elif key == 'first_message' and value not in LEAN_FIRST_MESSAGES:
                    _fail(f'{sub}_first_message', 'must_be_constant_or_per_run')
                elif key == 'mcp' and value not in LEAN_MCP:
                    _fail(f'{sub}_mcp', 'must_be_inherit_or_none')
                elif key == 'keep_skills' and (not isinstance(value, list) or len(value) > 50 or len(set(map(str, value)))
                                               != len(value) or any(not isinstance(v, str) or not SKILL_NAME_RE.fullmatch(v)
                                                                    for v in value)):
                    _fail(f'{sub}_keep_skills', 'must_be_up_to_50_distinct_skill_names')
                elif key == 'drop_tools':
                    if (not isinstance(value, list) or len(value) > 40 or len(set(map(str, value))) != len(value)
                            or any(not isinstance(v, str) or not TOOL_NAME_RE.fullmatch(v) for v in value)):
                        _fail(f'{sub}_drop_tools', 'must_be_up_to_40_distinct_tool_names')
                    if 'Bash' in value:  # every runtime step of a dispatched run is a Bash CLI call
                        _fail(f'{sub}_drop_tools', 'must_not_drop_bash')

    def _validate_section(self, spec, section):
        """Present keys are validated; missing keys take DEFAULTS (see `with_optional_defaults`). Bounds are None (bool),
        an (low, high) int range, or a tuple of allowed strings (enum)."""
        _keys(spec, self.defaults[section], section)
        for key, value in spec.items():
            bounds = SECTION_RANGES[section][key]
            if bounds is None:
                _bool(value, f'{section}_{key}')
            elif all(isinstance(b, str) for b in bounds):
                if value not in bounds:
                    _fail(f'{section}_{key}', 'must_be_one_of_' + '_'.join(bounds))
            else:
                _int(value, f'{section}_{key}', *bounds)

    def _validate_intake(self, spec):
        """v1 keys are required (as before P2); present P2 keys are validated, missing ones take DEFAULTS."""
        _keys(spec, self.defaults['intake'], 'intake')
        for key in INTAKE_REQUIRED:
            if key not in spec:
                _fail(f'intake_{key}', 'missing')
        for key, value in spec.items():
            field = f'intake_{key}'
            if key in ('enabled', 'mention_search', 'dm_request_routing'):
                _bool(value, field)
            elif key == 'dm_conversation_minutes':  # P39: 0 = no conversation window
                _int(value, field, 0, 240)
            elif key in ('channels', 'oncall_channels'):
                _channel_list(value, field)
            elif key == 'burst_seconds':
                _int(value, field, 1, 3600)
            elif key == 'dm_settle_seconds':  # P3.3: 0 = off; otherwise the human-DM settle delay before dispatch
                _int(value, field, 0, 3600)
            elif key == 'poll_seconds':
                _int(value, field, 20, 3600)
            elif key == 'alert_dedupe_minutes':
                _int(value, field, 1, 1440)
            elif key == 'search_max_pages':
                _int(value, field, 1, 5)
            elif key == 'max_api_calls_per_cycle':
                _int(value, field, 4, 100)
            elif key == 'dm_queries':
                if not isinstance(value, list) or len(value) > 5 or len(set(map(str, value))) != len(value) or any(
                        not isinstance(q, str) or not q.strip() or len(q) > 200 or QUERY_FORBIDDEN_RE.search(q)
                        for q in value):
                    _fail(field, 'must_be_up_to_5_distinct_single_line_queries')
            elif key == 'delegate_bot_names':  # P42.1
                if not isinstance(value, list) or len(value) > 20 or any(
                        not isinstance(n, str) or not APP_PATTERN_RE.fullmatch(n) for n in value):
                    _fail(field, 'must_be_up_to_20_app_name_patterns')
            elif key in ('inbox_task', 'oncall_task'):
                if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
                    _fail(field, 'must_be_a_task_workspace_name')
            elif key.endswith('_provider'):
                if value not in PROVIDERS:
                    _fail(field, 'must_be_codex_or_claude')
            elif key.endswith('_model'):
                if not isinstance(value, str) or not MODEL_RE.fullmatch(value):
                    _fail(field, 'must_be_a_model_id')
            elif key.endswith('_effort'):
                if value not in EFFORTS:
                    _fail(field, 'must_be_one_of_' + '_'.join(EFFORTS))
        merged = {**self.defaults['intake'], **spec}
        if merged['inbox_task'] == merged['oncall_task']:
            _fail('intake_oncall_task', 'must_differ_from_inbox_task')

    def with_optional_defaults(self, config):
        """Fill optional sections and their missing keys from DEFAULTS (validated input)."""
        out = copy.deepcopy(config)
        for section in OPTIONAL_SECTIONS:
            out[section] = {**copy.deepcopy(self.defaults[section]), **(out.get(section) or {})}
        # P38: the one nested optional object; a partial provider_health keeps the defaults of its missing keys.
        out['dispatch']['provider_health'] = {**self.defaults['dispatch']['provider_health'],
                                              **(out['dispatch'].get('provider_health') or {})}
        # P40: lean_context is nested twice; a partial class keeps the defaults of its missing keys.
        lean = out['dispatch'].get('lean_context') or {}
        out['dispatch']['lean_context'] = {
            'enabled': lean.get('enabled', self.defaults['dispatch']['lean_context']['enabled']),
            **{name: {**copy.deepcopy(self.defaults['dispatch']['lean_context'][name]), **(lean.get(name) or {})}
               for name in (*LEAN_CLASSES, 'keep_warm')}}
        # P2: the intake section is required, its new keys are optional (a v1 file keeps working).
        out['intake'] = {**copy.deepcopy(self.defaults['intake']), **(out.get('intake') or {})}
        return out

    def load(self, path=None):
        """(config, source). Missing file -> deep copy of DEFAULTS; anything malformed raises."""
        path = Path(path).expanduser() if path else self.config_path()
        if not path.exists() and not path.is_symlink():
            return copy.deepcopy(self.defaults), 'default'
        return self.with_optional_defaults(self.validate(_drop_retired(_read(path)))), str(path)

    def intake_role(self, config, task_id):
        """'inbox' / 'oncall' when `task_id` is one of the long-lived intake tasks, 'control' for the control task while
        the control entry is on, else None."""
        intake = {**self.defaults['intake'], **(config.get('intake') or {})}
        for role in ('inbox', 'oncall'):
            if task_id and task_id == intake[f'{role}_task']:
                return role
        if task_id and 'control' in config:
            control = self.control_settings(config)
            if control['enabled'] and task_id == control['task']:
                return 'control'
        return None

    def control_settings(self, config):
        """The control section of a loaded config with its defaults (`control_settings`)."""
        return control_settings(config, self.key)

    def long_lived_ids(self, config):
        """P38: the long-lived tasks — the intake inbox / oncall tasks plus dispatch.long_lived_tasks (a loaded config)."""
        intake = {**self.defaults['intake'], **(config.get('intake') or {})}
        ids = {intake['inbox_task'], intake['oncall_task'], *config['dispatch']['long_lived_tasks']}
        if 'control' in config:
            control = self.control_settings(config)
            if control['enabled']:
                ids.add(control['task'])
        return ids

    def long_lived_task(self, config, task_id):
        return bool(task_id) and task_id in self.long_lived_ids(config)

    def task_class(self, config, task_id):
        """P40: inbox / oncall (the intake tasks), long_lived (dispatch.long_lived_tasks) or task."""
        return self.intake_role(config, task_id) or ('long_lived' if self.long_lived_task(config, task_id) else 'task')

    def lean_profile(self, config, task_id):
        """P40: the lean-context spec of a task's dispatched Claude runs plus its `class`; the kill switch (or a config
        without the section) gives LEAN_OFF, i.e. a run exactly as before P40."""
        name = self.task_class(config, task_id)
        lean = (config.get('dispatch') or {}).get('lean_context') or {}
        spec = lean.get('inbox' if name == 'control' else name) if lean.get('enabled') else None  # control: the inbox's
        return {**copy.deepcopy(LEAN_OFF), **copy.deepcopy(spec or {}), 'class': name, 'enabled': bool(spec)}

    def keep_warm_spec(self, config):
        """P40.1: the `dispatch.lean_context.keep_warm` object of a loaded config, or None when keep-warm is off (its own
        switch, the lean-context kill switch or dispatch.enabled)."""
        dispatch = config.get('dispatch') or {}
        lean = dispatch.get('lean_context') or {}
        spec = {**self.defaults['dispatch']['lean_context']['keep_warm'], **(lean.get('keep_warm') or {})}
        if not (dispatch.get('enabled', True) and lean.get('enabled') and spec['enabled']):
            return None
        return copy.deepcopy(spec)

    def slow_run_seconds(self, config, task_id):
        """P39: how long a run of `task_id` may take before provider health counts it slow (None = never): the <owner key>-oncall
        task uses provider_health.oncall_slow_run_minutes (0 = never — its code RCA runs are long by design), <owner key>-inbox and
        dispatch.long_lived_tasks use slow_run_minutes; a bounded task is never slow."""
        if not self.long_lived_task(config, task_id):
            return None
        ph = {**self.defaults['dispatch']['provider_health'], **(config['dispatch'].get('provider_health') or {})}
        minutes = ph['oncall_slow_run_minutes'] if self.intake_role(config, task_id) == 'oncall' else ph['slow_run_minutes']
        return minutes * 60 or None

    def slow_limits(self, config):
        """P39: {task_id: seconds} for every long-lived task that can be slow (see slow_run_seconds)."""
        limits = {task_id: self.slow_run_seconds(config, task_id) for task_id in self.long_lived_ids(config)}
        return {task_id: seconds for task_id, seconds in limits.items() if seconds}

    def preferred_provider(self, config, task_id, category):
        """P38: the provider a task is configured to run on — its intake role provider (<owner key>-inbox / <owner key>-oncall), else
        the primary provider of its routing category. A task on another provider only because of a fallback or a
        provider-health reroute returns here once it is healthy."""
        role = self.intake_role(config, task_id)
        if role == 'control':
            return self.control_settings(config)['provider']
        if role:
            return {**self.defaults['intake'], **(config.get('intake') or {})}[f'{role}_provider']
        return provider_order(config, category)[0]

    def model_for_task(self, config, provider, important=False, task_id=None):
        """Like model_for, but the <owner key>-inbox / <owner key>-oncall tasks use intake.<role>_model/_effort when they run on
        their configured provider (a fallback to the other provider uses that provider's defaults)."""
        role = self.intake_role(config, task_id)
        intake = {**self.defaults['intake'], **(config.get('intake') or {})}
        if role == 'control':
            control = self.control_settings(config)
            if provider == control['provider']:
                model, effort, yolo = model_for(config, provider, important)
                return control['model'] or model, control['effort'] or effort, yolo
            return model_for(config, provider, important)
        if role and provider == intake[f'{role}_provider']:
            return intake[f'{role}_model'], intake[f'{role}_effort'], list(config['providers'][provider]['yolo_args'])
        return model_for(config, provider, important)

    def set_value(self, key, value_text, who, path=None, now=None):
        """Validate the complete result, then atomically write it and append the change record."""
        path = Path(path).expanduser() if path else self.config_path()
        now = time.time() if now is None else now
        _parts(key)
        with _write_lock(path):
            config, _ = self.load(path)
            new = _parse_value(value_text)
            updated = copy.deepcopy(config)
            _open_sparse(updated, key)
            found, old = _apply(updated, key, new)
            self.validate(updated)
            _atomic_write(path, updated)
            record = _record(now, who, key, found, old, new)
            _append_log(self.log_path(path), record)
        return {'ok': True, 'path': str(path), 'log': str(self.log_path(path)), 'change': record}

    def set_many(self, changes, who, path=None, now=None, dry_run=False, expect_digest=None):
        """Apply several `(key, value)` pairs (values already decoded) as ONE validated atomic write.

        Same rules as `set_value` (key syntax, unknown keys, whole-config validation); unchanged
        values are skipped; each changed key gets its own change-log record. `expect_digest`
        refuses the write when the file changed since the caller read it.
        """
        path = Path(path).expanduser() if path else self.config_path()
        now = time.time() if now is None else now
        changes = list(changes)
        if not changes or len(changes) > 50:
            raise ValueError('invalid_worker_config_changes')
        with _write_lock(path):
            config, source = self.load(path)
            if expect_digest is not None and expect_digest != digest(config):
                raise ValueError('worker_config_changed_since_loaded_reload')
            updated = copy.deepcopy(config)
            records = []
            for key, value in changes:
                _open_sparse(updated, key)
                found, old = _apply(updated, key, value)
                if found and old == value:
                    continue
                records.append(_record(now, who, key, found, old, copy.deepcopy(value)))
            self.validate(updated)
            result = {'ok': True, 'path': str(path), 'log': str(self.log_path(path)), 'dry_run': bool(dry_run),
                      'changes': records, 'digest': digest(config if dry_run or not records else updated)}
            if dry_run or not records:
                return result
            _atomic_write(path, updated)
            for record in records:
                _append_log(self.log_path(path), record)
        return result

    def change_log(self, path=None, limit=50):
        """Newest-first change records (tolerates a missing or partly malformed log)."""
        path = self.log_path(Path(path).expanduser() if path else self.config_path())
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                out.append(record)
            if len(out) >= limit:
                break
        return out

    def show(self, path=None):
        path = Path(path).expanduser() if path else self.config_path()
        config, source = self.load(path)
        return {'ok': True, 'source': source, 'path': str(path), 'log': str(self.log_path(path)), 'config': config}

    def check(self, path=None):
        path = Path(path).expanduser() if path else self.config_path()
        try:
            _, source = self.load(path)
        except ValueError as exc:
            return {'ok': False, 'path': str(path), 'error': str(exc)}
        return {'ok': True, 'path': str(path), 'source': source, 'schema_version': SCHEMA_VERSION}
