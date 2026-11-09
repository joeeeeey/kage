"""P3 rule engine ("经验法则"): the owner's approved experience rules as code-checked preconditions and bounds.

Access / account category first. No model runs here; `rt` is the loaded slack_followup module (Slack transport,
SQLite helpers, ask-<owner key>), exactly like intake.py.

Spec (private, `~/.config/<worker_slug>/rules/`): `active.json` names the current version file of each
category (`access-v1.json`, ...). A rule has id, name, kind (gate | domain), level (auto | one_tap | <owner key>), trigger,
checks (named checks implemented below + params), bounds, limits (text for the agent), fallback (clarify | <owner key> |
route | stop) and optional never-auto claims. The skill ships only this engine, the schemas and synthetic examples
(`references/rules-example.json`); rule files with people IDs and thresholds never enter the skill.

rule-check: a plan (agent-written intent, PLAN_SCHEMA) -> facts from trusted sources only (Slack users.lookupByEmail
with the bot token; the people registry, never `<owner key>_view`; the request author from the event envelope, the intake
burst or an exact-ts Slack read; approvals only from verified card decisions by clicker ID; what a requester says
about themselves is never a fact) -> never-auto classifier -> matched rules -> checks -> ONE outcome, strictest
wins (auto < one_tap < clarify < route < <owner key>), with matched rules, failed checks, bounds, limits and a Chinese card
summary. A domain rule must match, otherwise the plan is uncovered (<owner key>). one_tap / <owner key> checks may post a decision
card; the click is bound to the plan hash and the approver must not be the recipient.

Learning: `rules-review-data` (aggregates and ids, never message text) -> `rule-propose` (each proposal is a
machine-applicable patch of the active spec, or an inbox-policy addendum) -> one card each [采纳] [不采纳] -> the
supervisor (code, never the agent) applies an approved patch: new version file, changelog, validation, then switch
`active.json`; a rejected proposal's fingerprint is suppressed for 28 days.
"""
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from kage import locale
from kage.adapters import trackers as _trackers

CATEGORIES = ('access',)


KINDS = ('person', 'group', 'workload', 'external', 'agent_ops')


ACTIONS = ('grant', 'change', 'tighten', 'revoke', 'delete', 'rotate', 'resend', 'repair', 'create_account',
           'create_credential', 'verify', 'inventory', 'offboard', 'notify', 'onboard_bundle', 'agent_op')


ROLE_CLASSES = ('read', 'port_forward', 'member', 'write', 'act_as', 'data_admin', 'exec', 'secret_read', 'admin',
                'owner')


CLASS_RANK = {name: n for n, name in enumerate(ROLE_CLASSES)}


ENVIRONMENTS = ('prod', 'staging', 'dev', 'none')


RESOURCE_LEVELS = ('resource', 'table', 'dataset', 'repository', 'namespace', 'project', 'cluster', 'account',
                   'subscription', 'org')


OFFBOARD_OPS = ('disable', 'suspend', 'remove_member', 'remove_binding', 'remove_assignment', 'stop_instance',
                'disable_key', 'downgrade', 'manual', 'delete', 'rotate', 'revoke_oauth', 'remove_ssh_key',
                'drop_role', 'migrate_dependency')


MESSAGES = ('recipient_dm', 'requester_reply', 'hr_report', 'owner_handoff', 'broadcast', 'departing_person',
            'external_first_contact', 'external_followup', 'usage_anomaly', 'new_commitment')


AGENT_OPS = ('exec_readonly', 'exec', 'secret_read', 'db_read', 'db_write', 'db_admin')


TASK_TYPES = ('debug', 'incident', 'skill', 'grant', 'onboarding', 'offboarding', 'other')


SHAPES = ('unknown_role', 'admin_or_owner', 'prod_destructive', 'spend_over_threshold', 'group_wide_expansion',
          'personal_prod_access', 'workload_admin_identity', 'auth_entry_change', 'account_wide_secret_read',
          'unregistered_principal')


TYPED_APPROVAL_SHAPES = ('unknown_role',)


RESULTS = ('executed', 'failed', 'abandoned')


PROPOSAL_KINDS = ('rule_patch', 'inbox_addendum')


CHECK_ID_RE = re.compile(r'rc[0-9a-f]{14}')


PROPOSAL_ID_RE = re.compile(r'rp[0-9a-f]{14}')


EMAIL_RE = r'^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$'


USER_PAT = r'^U[A-Z0-9]{6,15}$'


DATE_PAT = r'^\d{4}-\d{2}-\d{2}$'


TASK_PAT = r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$'


MAX_PLAN_BYTES = 16384


def _is_type(value, name):
    return {'object': isinstance(value, dict), 'array': isinstance(value, list), 'string': isinstance(value, str),
            'boolean': isinstance(value, bool), 'null': value is None,
            'integer': isinstance(value, int) and not isinstance(value, bool),
            'number': isinstance(value, (int, float)) and not isinstance(value, bool)}[name]


def schema_errors(value, schema, path='$', root=None, limit=20):
    """Human-readable violations of `schema` (first `limit`)."""
    root = root or schema
    out = []
    if '$ref' in schema:
        schema = root['$defs'][schema['$ref'].rsplit('/', 1)[1]]
    types = schema.get('type')
    if types:
        types = types if isinstance(types, list) else [types]
        if not any(_is_type(value, t) for t in types):
            return [f'{path}: must be {"/".join(types)}']
    if 'const' in schema and value != schema['const']:
        out.append(f'{path}: must be {json.dumps(schema["const"], ensure_ascii=False)}')
    if 'enum' in schema and value not in schema['enum']:
        out.append(f'{path}: must be one of {", ".join(map(str, schema["enum"]))[:300]}')
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 10 ** 9):
            out.append(f'{path}: length must be {schema.get("minLength", 0)}..{schema.get("maxLength", "∞")}')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            out.append(f'{path}: must match {schema["pattern"]}')
    if _is_type(value, 'number'):
        if 'minimum' in schema and value < schema['minimum'] or 'maximum' in schema and value > schema['maximum']:
            out.append(f'{path}: must be {schema.get("minimum", "-∞")}..{schema.get("maximum", "∞")}')
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', 10 ** 9):
            out.append(f'{path}: must have {schema.get("minItems", 0)}..{schema.get("maxItems", "∞")} items')
        if schema.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            out.append(f'{path}: items must be unique')
        if 'items' in schema:
            for n, item in enumerate(value):
                out += schema_errors(item, schema['items'], f'{path}[{n}]', root, limit)
    if isinstance(value, dict):
        if len(value) < schema.get('minProperties', 0) or len(value) > schema.get('maxProperties', 10 ** 9):
            out.append(f'{path}: must have {schema.get("minProperties", 0)}..{schema.get("maxProperties", "∞")} keys')
        for key in schema.get('required', ()):
            if key not in value:
                out.append(f'{path}: missing {key}')
        props = schema.get('properties', {})
        extra = schema.get('additionalProperties', True)
        for key, item in value.items():
            if 'propertyNames' in schema:
                out += [e.replace(f'{path}.{key}:', f'{path}: key {key!r}') for e in
                        schema_errors(key, schema['propertyNames'], f'{path}.{key}', root, limit)]
            if key in props:
                out += schema_errors(item, props[key], f'{path}.{key}', root, limit)
            elif extra is False:
                out.append(f'{path}: unknown key {key!r}')
            elif isinstance(extra, dict):
                out += schema_errors(item, extra, f'{path}.{key}', root, limit)
    return out[:limit]


def _str(n=200, pattern=None):
    s = {'type': 'string', 'minLength': 1, 'maxLength': n}
    if pattern:
        s['pattern'] = pattern
    return s


def _list(items, n=50, unique=True, min_items=0):
    return {'type': 'array', 'items': items, 'maxItems': n, 'minItems': min_items, 'uniqueItems': unique}


def _enum(values):
    return {'enum': list(values)}


_NUM = {'type': 'number', 'minimum': 0, 'maximum': 1000000}


_INT = {'type': 'integer', 'minimum': 0, 'maximum': 100000}


_BOOL = {'type': 'boolean'}


EMPLOYMENT = ('active', 'work_trial', 'left', 'external', 'unknown')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value, n=16):
    return hashlib.sha256(canonical(value).encode()).hexdigest()[:n]


def _check_private(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'rules_file_missing_or_not_regular:{path.name}')
    st = path.stat()
    if st.st_mode & 0o022:
        raise ValueError(f'rules_file_must_not_be_group_or_world_writable:{path.name}')
    return st


def _read_private_json(path):
    path = Path(path)
    _check_private(path)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError(f'rules_file_invalid_json:{path.name}') from None


def write_private_json(path, data):
    """Atomic 0600 write inside a 0700 directory (created when missing)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_mode & 0o022:
        raise ValueError('rules_directory_must_not_be_group_or_world_writable')
    fd, tmp = tempfile.mkstemp(prefix='.rules.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=False)
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
    return path


def append_changelog(directory, record):
    path = Path(directory) / 'changelog.jsonl'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return path


def read_changelog(directory, limit=50):
    try:
        lines = (Path(directory) / 'changelog.jsonl').read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


_SPEC_CACHE = {}


def plan_hash(plan):
    return hashlib.sha256(canonical(plan).encode()).hexdigest()


def empty_facts():
    return {'recipient': {'email': None, 'domain': None, 'company_domain': False,
                          'slack': {'status': 'skipped', 'id': None},
                          'registry': {'found': False, 'status': 'unknown', 'contract': 'unknown', 'function': 'unknown',
                                       'disciplines': [], 'flags': [], 'team_lead': None, 'principals': [],
                                       'privileged': False}},
            'requester': {'source': 'none', 'id': None, 'author_kind': None, 'trust': None, 'authority': None,
                          'relations': ['unknown']},
            'principal': {'value': None, 'kind': 'none', 'registered': False}}


def facts_for(spec, plan, injected):
    """Injected (backtest / smoke) facts over empty facts; what the plan itself states (recipient email, domain kind,
    principal) is derived the same way as in live checks unless the injection sets it."""
    facts = merge_facts(injected)
    rec, given = facts['recipient'], (injected or {}).get('recipient') or {}
    email = (plan.get('recipient') or {}).get('email')
    if email and not rec['email']:
        rec.update(email=email, domain=email.rsplit('@', 1)[1])
    if rec['domain'] and 'company_domain' not in given:
        rec['company_domain'] = rec['domain'] in spec['defaults']['company_domains']
    if 'principal' not in (injected or {}):
        facts['principal'] = principal_facts(spec, plan, rec)
    return facts


def merge_facts(injected):
    """Backtest / smoke facts layered over empty facts (unknown keys are refused)."""
    out = empty_facts()

    def layer(base, extra, path):
        for key, value in (extra or {}).items():
            if key not in base:
                raise ValueError(f'unknown_fact_{path}{key}')
            if isinstance(base[key], dict) and isinstance(value, dict):
                layer(base[key], value, f'{path}{key}.')
            else:
                base[key] = value
    layer(out, injected, '')
    return out


_PEOPLE_CACHE = {}


def slack_identity(rt, email):
    """users.lookupByEmail with the bot token (users:read.email): exact email, active, human, not a guest."""
    try:
        user = rt.slack.api_call('users.lookupByEmail', rt.api_identity(bot=True), params={'email': email}).get('user') or {}
    except Exception as exc:  # noqa: BLE001
        code = re.fullmatch(r'Slack API error: ([a-z_]+)', str(exc))
        if code and code[1] == 'users_not_found':
            return {'status': 'not_found', 'id': None}
        return {'status': 'error:' + (code[1] if code else type(exc).__name__), 'id': None}
    uid = str(user.get('id') or '')
    profile = (user.get('profile') or {}).get('email') or ''
    status = ('deleted' if user.get('deleted') else 'bot' if user.get('is_bot') else
              'restricted' if user.get('is_restricted') or user.get('is_ultra_restricted') else
              'email_mismatch' if profile.lower() != email.lower() else 'ok')
    return {'status': status, 'id': uid if re.fullmatch(r'[UW][A-Z0-9]+', uid) else None}


def _classify_author(rt, db, message):
    ledger = db.execute('SELECT 1 FROM agent_sends WHERE channel=? AND ts=?',
                        (message.get('channel') or '', message.get('ts') or '')).fetchone() is not None
    env = rt.envelopes.classify_message(message, ledger_hit=ledger, people=rt.envelopes.load_people())
    return env


def request_origin(rt, db, source):
    """Author of the request from code-held records: event envelope, intake burst, or an exact-ts Slack read."""
    source = source or {}
    out = {'source': 'none', 'id': None, 'author_kind': None, 'trust': None, 'authority': None}
    if source.get('burst_id') or source.get('event_id'):
        row = None
        if source.get('burst_id'):
            row = db.execute('SELECT sender,sender_kind,envelope FROM intake_bursts WHERE burst_id=?',
                             (source['burst_id'],)).fetchone()
        elif source.get('event_id'):
            row = db.execute('SELECT sender,sender_kind,envelope FROM intake_bursts WHERE event_id=?',
                             (source['event_id'],)).fetchone()
        if row is not None:
            env = json.loads(row['envelope']) if row['envelope'] else {}
            return {**out, 'source': 'burst', 'id': row['sender'], 'author_kind': env.get('author_kind') or row['sender_kind'],
                    'trust': env.get('trust'), 'authority': env.get('authority')}
        if source.get('event_id'):
            e = db.execute('SELECT author FROM events WHERE id=?', (source['event_id'],)).fetchone()
            if e is not None:
                env = rt.envelopes.lookup(db, source['event_id']) or {}
                author = str(e['author'] or '')
                return {**out, 'source': 'event', 'id': author if re.fullmatch(r'[UW][A-Z0-9]+', author) else None,
                        'author_kind': env.get('author_kind') or ('human' if re.fullmatch(r'[UW][A-Z0-9]+', author)
                                                                   else 'agent'),
                        'trust': env.get('trust'), 'authority': env.get('authority')}
        return {**out, 'source': 'unresolved'}
    if source.get('channel') and source.get('ts'):
        token = rt.api_identity()
        found = None
        for endpoint, params in (('conversations.history', {'channel': source['channel'], 'oldest': source['ts'],
                                                            'latest': source['ts'], 'inclusive': 'true', 'limit': 1,
                                                            'include_all_metadata': 'true'}),
                                 ('conversations.replies', {'channel': source['channel'], 'ts': source['ts'], 'limit': 1,
                                                            'include_all_metadata': 'true'})):
            try:
                messages = rt.slack.api_call(endpoint, token, params=params).get('messages') or []
            except Exception:  # noqa: BLE001 - e.g. thread_not_found for a top-level ts
                continue
            found = next((m for m in messages if m.get('ts') == source['ts']), None)
            if found:
                break
        if not found:
            return {**out, 'source': 'unresolved'}
        env = _classify_author(rt, db, {**found, 'channel': source['channel']})
        user = str(found.get('user') or '')
        return {**out, 'source': 'slack', 'id': user if re.fullmatch(r'[UW][A-Z0-9]+', user) else None,
                'author_kind': env['author_kind'], 'trust': env['trust'], 'authority': env['authority']}
    return out


def principal_facts(spec, plan, recipient):
    value = (plan.get('recipient') or {}).get('principal') or recipient.get('email')
    if not value:
        return {'value': None, 'kind': 'none', 'registered': False}
    text = str(value).strip()
    lower = text.lower()
    email = lower.split(':', 1)[1] if ':' in lower and '@' in lower.split(':', 1)[1] else lower
    domain = email.rsplit('@', 1)[1] if '@' in email else ''
    if lower.startswith('serviceaccount:') or domain.endswith('gserviceaccount.com'):
        kind = 'service_account'
    elif lower.startswith('group:'):
        kind = 'group'
    elif domain in spec['defaults']['company_domains'] or domain in spec['defaults'].get('managed_domains', []):
        kind = 'company'
    elif domain:
        kind = 'personal'
    else:
        kind = 'other'
    registry = recipient.get('registry') or {}
    return {'value': text, 'kind': kind, 'registered': kind == 'personal' and email in (registry.get('principals') or [])}


def classify_roles(spec, plan):
    catalog = spec['role_catalog'].get(plan['system'], {})
    lower = {k.lower(): v for k, v in catalog.items()}
    scope = plan.get('scope') or {}
    classes, unknown = set(), []
    for role in scope.get('roles') or []:
        cls = catalog.get(role) or lower.get(role.lower())
        if cls is None:
            unknown.append(role)
        else:
            classes.add(cls)
    if scope.get('level'):
        classes.add(scope['level'])
    return classes, unknown


GRANTING = ('grant', 'change', 'create_account', 'create_credential', 'resend', 'onboard_bundle')


def never_auto_shapes(spec, plan, facts, classes, unknown):
    na, scope, action = spec['never_auto'], plan.get('scope') or {}, plan['action']
    env = scope.get('environment')
    ops = {i['op'] for i in scope.get('items') or []}
    out = []

    def hit(shape, why):
        out.append({'shape': shape, 'level': na[shape]['level'], 'why': why})
    if plan['kind'] == 'agent_ops':
        if scope.get('irreversible') or action in ('delete', 'rotate'):
            hit('prod_destructive', 'agent_ops_irreversible')
        return out
    if unknown:
        hit('unknown_role', ', '.join(unknown)[:200])
    admin = classes & set(na['admin_or_owner'].get('classes') or ('admin', 'owner'))
    if admin:
        hit('admin_or_owner', ', '.join(sorted(admin)))
    pd = na['prod_destructive']
    if env in (pd.get('environments') or ['prod']) and (action in (pd.get('actions') or ()) or
                                                        ops & set(pd.get('ops') or ()) or scope.get('irreversible')):
        hit('prod_destructive', action)
    spend, limits = plan.get('spend') or {}, na['spend_over_threshold']
    over = [k for k in ('recurring_usd_month', 'one_time_usd')
            if (spend.get(k) or 0) > limits.get(k, spec['defaults']['spend'][k])]
    if over:
        hit('spend_over_threshold', ', '.join(over))
    if (scope.get('group_wide') or plan['kind'] == 'group') and action in ('grant', 'change'):
        hit('group_wide_expansion', plan.get('recipient', {}).get('group') or 'group')
    pp = na['personal_prod_access']
    if plan['kind'] == 'person' and plan['system'] in (pp.get('systems') or ()) and env in (pp.get('environments') or ['prod']) \
            and classes & set(pp.get('classes') or ()):
        hit('personal_prod_access', ', '.join(sorted(classes & set(pp['classes']))))
    if plan['kind'] == 'workload' and classes & set(na['workload_admin_identity'].get('classes') or ('admin', 'owner')):
        hit('workload_admin_identity', 'workload')
    if plan['system'] in (na['auth_entry_change'].get('systems') or ('auth_entry',)):
        hit('auth_entry_change', plan['system'])
    ws = na['account_wide_secret_read']
    if 'secret_read' in classes and (scope.get('resource_level') in (ws.get('resource_levels') or ()) or
                                     any('*' in r for r in scope.get('resources') or [])):
        hit('account_wide_secret_read', scope.get('resource_level') or '*')
    principal = facts['principal']
    if plan['kind'] == 'person' and action in GRANTING and principal['kind'] == 'personal' and not principal['registered']:
        hit('unregistered_principal', 'personal')
    return out


class Ctx:
    def __init__(self, spec, plan, facts, classes, unknown, now, rate=None):
        self.spec, self.plan, self.facts, self.classes, self.unknown, self.now = spec, plan, facts, classes, unknown, now
        self.scope = plan.get('scope') or {}
        self.reg = facts['recipient']['registry']
        self.rels = set(facts['requester'].get('relations') or ['unknown'])
        self.rate = rate or (lambda rule_id: 0)
        self.shapes = []

    @property
    def max_class(self):
        return max(self.classes, key=CLASS_RANK.get) if self.classes else None


def _r(status, reason, zh, outcome=None, who=None):
    return {'status': status, 'reason': reason, 'zh': zh, 'outcome': outcome, 'who': who}


def missing(reason, zh, who):
    return _r('missing', reason, zh, 'clarify', who)


def fail(reason, zh, outcome=None, who=None):
    return _r('fail', reason, zh, outcome, who)


def escalate(level, reason, zh):
    return _r('escalate', reason, zh, level)


def _trigger_one(t, ctx):
    plan, scope = ctx.plan, ctx.scope
    if 'category' in t and t['category'] != plan['category']:
        return False
    for field, value in (('kinds', plan['kind']), ('actions', plan['action']), ('systems', plan['system']),
                         ('bundles', scope.get('bundle')), ('messages', scope.get('message'))):
        if field in t and value not in t[field]:
            return False
    if 'levels' in t and not ctx.classes & set(t['levels']):
        return False
    if 'max_level' in t and any(CLASS_RANK[c] > CLASS_RANK[t['max_level']] for c in ctx.classes):
        return False
    if 'ops' in t and not {i['op'] for i in scope.get('items') or []} & set(t['ops']):
        return False
    if 'credential' in t and ((plan.get('credential') or {}).get('type', 'none') != 'none') != t['credential']:
        return False
    if 'never_auto' in t and bool(ctx.shapes) != t['never_auto']:
        return False
    if 'group_wide' in t and (bool(scope.get('group_wide')) or plan['kind'] == 'group') != t['group_wide']:
        return False
    return True


def trigger_matches(trigger, ctx):
    base = {k: v for k, v in trigger.items() if k != 'any'}
    return _trigger_one(base, ctx) and (not trigger.get('any') or any(_trigger_one(t, ctx) for t in trigger['any']))


UNDOABLE_OPS = ('disable', 'suspend', 'remove_member', 'remove_binding', 'remove_assignment', 'stop_instance',
                'disable_key', 'downgrade', 'revoke_oauth', 'remove_ssh_key', 'drop_role')


SUMMARY_MAX = 110  # the card adds 。 (and 🧪 TEST · on smokes); its first line stays within 120


TYPED_REF_RE = re.compile(r'([CGD][A-Z0-9]{2,})/(\d{1,10}\.\d{6})')


def irreversible(plan, result):
    scope = plan.get('scope') or {}
    ops = {i.get('op') for i in scope.get('items') or []}
    return bool(scope.get('irreversible') or plan['action'] in ('delete', 'rotate') or ops & {'delete', 'rotate'} or
                any(s['shape'] == 'prod_destructive' for s in result['never_auto']))


DDL = '''
  CREATE TABLE IF NOT EXISTS rule_checks (
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, category TEXT NOT NULL, spec_version INTEGER, spec_digest TEXT,
    plan_hash TEXT NOT NULL, plan TEXT NOT NULL, kind TEXT, action TEXT, system TEXT, task_id TEXT, caller TEXT,
    outcome TEXT NOT NULL, matched_rules TEXT NOT NULL, failed_checks TEXT, never_auto TEXT, facts TEXT,
    approvers TEXT NOT NULL DEFAULT '[]', recipient_slack TEXT, requester TEXT, card_event TEXT, card_posted_at REAL,
    result TEXT, result_at REAL, result_by TEXT, result_note TEXT, correction TEXT, corrected_by TEXT,
    corrected_at REAL, smoke INTEGER NOT NULL DEFAULT 0);
  CREATE INDEX IF NOT EXISTS rule_checks_time ON rule_checks(created_at);
  CREATE INDEX IF NOT EXISTS rule_checks_card ON rule_checks(card_event);
  CREATE TABLE IF NOT EXISTS rule_proposals (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, category TEXT, fingerprint TEXT NOT NULL, title_zh TEXT NOT NULL,
    proposal TEXT NOT NULL, base_version INTEGER, target TEXT NOT NULL, proposer TEXT NOT NULL, card_event TEXT,
    status TEXT NOT NULL, created_at REAL NOT NULL, decided_by TEXT, decided_at REAL, applied_at REAL,
    applied_version INTEGER, suppress_until REAL, error TEXT, smoke INTEGER NOT NULL DEFAULT 0);
  CREATE INDEX IF NOT EXISTS rule_proposals_status ON rule_proposals(status, created_at);
  CREATE INDEX IF NOT EXISTS rule_proposals_fingerprint ON rule_proposals(fingerprint, status);
  CREATE INDEX IF NOT EXISTS rule_proposals_card ON rule_proposals(card_event);
'''


def initialize(db):
    db.executescript(DDL)


def rules_config(rt):
    try:
        return rt.worker_config.load()[0]['rules']
    except Exception:  # noqa: BLE001 - a malformed config never enables automation by accident
        return {**rt.worker_config.DEFAULTS['rules'], 'enabled': False, 'proposals_enabled': False,
                'typed_approval_max_age_hours': 0}


def rate_counter(db, now):
    def count(rule_id):
        return db.execute("SELECT count(*) FROM rule_checks WHERE result='executed' AND result_at>? AND smoke=0 AND "
                          'EXISTS (SELECT 1 FROM json_each(rule_checks.matched_rules) WHERE value=?)',
                          (now - 86400, rule_id)).fetchone()[0]
    return count


def compact_facts(facts):
    rec, req = facts['recipient'], facts['requester']
    return {'recipient': {'company_domain': rec['company_domain'], 'slack': rec['slack']['status'],
                          'registry': {k: rec['registry'][k] for k in ('found', 'status', 'function')}},
            'requester': {'source': req.get('source'), 'author_kind': req.get('author_kind'),
                          'relations': req.get('relations')},
            'principal': {k: facts['principal'][k] for k in ('kind', 'registered')}}


def _task_cwd(rt, task_id):
    if task_id:
        path = rt.registry.task_dir(task_id)
        if path.is_dir():
            return str(path)
    return os.getcwd()


def check_url(rt, check_id):
    return f'{rt.DASHBOARD_URL}/rules?check={check_id}'


def _check_row(db, check_id):
    if not CHECK_ID_RE.fullmatch(str(check_id or '')):
        raise ValueError('check_id_must_look_like_rc + 14 hex')
    row = db.execute('SELECT * FROM rule_checks WHERE id=?', (check_id,)).fetchone()
    if row is None:
        raise ValueError('unknown_rule_check')
    return row


def executable(row, approval):
    return row['outcome'] in ('auto', 'route') or approval.get('status') == 'approved'


def _trigger_text(trigger):
    parts = [f'{k}={",".join(map(str, v)) if isinstance(v, list) else v}' for k, v in trigger.items() if k != 'any']
    if trigger.get('any'):
        parts.append('any(' + ' | '.join(_trigger_text(t) for t in trigger['any']) + ')')
    return '; '.join(parts)


def flatten(value, path=''):
    """{id-keyed path: leaf}. Lists of rules / checks / proposals are keyed by id or name, other lists are leaves."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out.update(flatten(item, f'{path}/{key.replace("~", "~0").replace("/", "~1")}'))
        return out or {path: {}}
    if isinstance(value, list) and value and all(isinstance(x, dict) and (x.get('id') or x.get('name')) for x in value):
        out = {}
        for item in value:
            key = str(item.get('id') or item.get('name'))
            out.update(flatten(item, f'{path}/{key.replace("~", "~0").replace("/", "~1")}'))
        return out
    return {path: value}


def spec_diff(old, new):
    a, b = flatten(old), flatten(new)
    out = []
    for path in sorted(set(a) | set(b)):
        if path not in a:
            out.append({'op': 'added', 'path': path, 'new': b[path]})
        elif path not in b:
            out.append({'op': 'removed', 'path': path, 'old': a[path]})
        elif a[path] != b[path]:
            out.append({'op': 'changed', 'path': path, 'old': a[path], 'new': b[path]})
    return out


def _share(n, total):
    return round(n / total, 4) if total else None


BACKTEST_META_KEY = 'rules_backtest'


PATCH_ROOTS = ('defaults', 'never_auto', 'role_catalog', 'rules')


MAX_PENDING = 10


def _pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 1)


def _decision(rt, db, card_event):
    if not card_event:
        return None
    d = db.execute('SELECT d.*, c.options FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                   'WHERE d.event_id=?', (card_event,)).fetchone()
    if d is None:
        return None
    option = rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))
    return {'actor': d['actor'], 'kind': option['kind'], 'option': option['id'], 'note': d['note'],
            'decided_at': d['decided_at'], 'provenance': d['provenance']}


def _interactions_review(rt, db, start, now):
    """P35 feedback-learning mining for the weekly review; guarded so an old DB without the table still returns."""
    try:
        return rt.learning.review_interactions(rt, db, start, now)
    except Exception:  # noqa: BLE001 - interaction_records may be absent in a pre-P35 database
        return {'total': 0, 'shapes': 0, 'overrides': 0, 'volume_patterns': [], 'logged_only': []}


def _segments(path):
    return [s.replace('~1', '/').replace('~0', '~') for s in path.split('/')[1:]]


def _child(node, seg):
    if isinstance(node, dict):
        if seg not in node:
            raise ValueError('patch_path_not_found:' + seg)
        return node[seg]
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict) and seg in (item.get('id'), item.get('name')):
                return item
        raise ValueError('patch_path_not_found:' + seg)
    raise ValueError('patch_path_not_found:' + seg)


def fingerprint(proposal):
    if proposal['kind'] == 'rule_patch':
        body = {'kind': 'rule_patch', 'category': proposal.get('category', 'access'), 'patch': proposal['patch']}
    else:
        body = {'kind': 'inbox_addendum', 'text': ' '.join(proposal.get('text_zh', '').split())}
    return digest(body)


def _cjk(text):
    return bool(re.search('[㐀-䶿一-鿿]', text or ''))


def inbox_goal_path(rt):
    cfg = rt.worker_config.load()[0]['intake']
    return rt.registry.task_dir(cfg['inbox_task']) / 'goal.md'


def _short(text, cap):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= cap else text[:cap - 1] + '…'


def _card_reply(rt, db, card_event, text, proposal_id, kind='rule_proposal_result'):
    """Best effort: a bot reply in the card thread saying what code did."""
    try:
        esc = db.execute('SELECT thread_ts FROM escalations WHERE event_id=?', (card_event,)).fetchone()
        card = db.execute('SELECT channel FROM escalation_cards WHERE event_id=?', (card_event,)).fetchone()
        if not esc or not card or not esc['thread_ts']:
            return None
        sent = rt.slack.api_call('chat.postMessage', rt.api_identity(bot=True), json_body={
            'channel': card['channel'], 'thread_ts': esc['thread_ts'], 'text': text, 'unfurl_links': False,
            'metadata': rt.agent_metadata(kind, None, proposal_id=proposal_id)})
        rt.record_send(db, card['channel'], (sent or {}).get('ts'), kind, proposal_id)
        return (sent or {}).get('ts')
    except Exception:  # noqa: BLE001 - the applied change stands; only the receipt is missing
        return None


def _replace_block(text, block, begin, end, after=None):
    """Replace the code-managed block between `begin` and `end`, or add it (right after `after` when present)."""
    block = block.strip('\n')
    if begin in text and end in text and text.index(begin) < text.index(end):
        head, rest = text.split(begin, 1)
        return head + block + rest.split(end, 1)[1]
    if after and after in text:
        head, tail = text.split(after, 1)
        return head + after + '\n\n' + block + tail
    return text.rstrip('\n') + '\n\n' + block + '\n'


def _write_goal(goal_path, text, new, now):
    """Backup (history/goal-md-backups/, 0600), then an atomic write that keeps the file mode."""
    backup = goal_path.parent / 'history' / 'goal-md-backups' / f'{dt.datetime.fromtimestamp(now, dt.timezone.utc):%Y%m%dT%H%M%SZ}-goal.md'
    backup.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)
    mode = goal_path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix='.goal.', dir=str(goal_path.parent))
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(new)
    os.chmod(tmp, mode)
    os.replace(tmp, goal_path)
    return backup


SMOKE_FACTS = {'recipient': {'slack': {'status': 'ok', 'id': 'USMOKERULES'},
                             'registry': {'found': True, 'status': 'active', 'function': 'engineering',
                                          'disciplines': ['backend']}},
               'requester': {'source': 'event', 'id': 'USMOKERULES', 'author_kind': 'human', 'relations': ['self']}}


def _dir_fingerprint(directory):
    directory = Path(directory)
    if not directory.is_dir():
        return None
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in sorted(directory.glob('*.json*'))
            if p.is_file() and not p.is_symlink()}


class AccessPolicy:
    """The access-policy engine bound to one owner and one org pack: schemas, facts, the never-auto classifier,
    checks, evaluation, cards, typed approvals, the rules directory, backtests, weekly proposals and the smoke."""

    def __init__(self, language, *, tz, example_spec, owner_key, owner_name, owner_user, ask_owner, worker_slug, rules_dir_env, smoke_dir_prefix, run_id_env, pack, tracker=None):
        self.language = language
        self.tracker = tracker or _trackers.current()  # N2: the deployment's tracker (`kage.adapters.trackers`)
        self.script = 'chinese' if locale.chinese(language) else 'in_the_owner_language'  # O1d: the gates' error words
        self.tz = tz
        self.example_spec = example_spec
        self.owner_user = owner_user
        self.ask_owner = ask_owner
        self.worker_slug = worker_slug
        self.rules_dir_env = rules_dir_env
        self.smoke_dir_prefix = smoke_dir_prefix
        self.run_id_env = run_id_env
        self.pack = pack
        self.key, self.name = owner_key, owner_name
        self.spec_schema_id = f'{self.worker_slug}.rules/v1'
        self.active_schema_id = f'{self.worker_slug}.rules-active/v1'
        self.plan_schema_id = f'{self.worker_slug}.rules-plan/v1'
        self.backtest_schema_id = f'{self.worker_slug}.rules-backtest/v1'
        self.proposals_schema_id = f'{self.worker_slug}.rules-proposals/v1'
        self.default_rules_dir = Path.home() / f'.config/{self.worker_slug}/rules'
        self.levels = ('auto', 'one_tap', self.key)
        self.outcomes = ('auto', 'one_tap', 'clarify', 'route', self.key)   # strictness order: the last one wins
        self.rank = {name: n for n, name in enumerate(self.outcomes)}
        self.tier = {'auto': 'auto', 'clarify': 'auto', 'route': 'auto', 'one_tap': 'one_tap', self.key: self.key}  # owner touch
        self.fallbacks = ('clarify', self.key, 'route', 'stop')
        self.relation_values = ('self', self.key, 'approver', 'hr', 'manager', 'data_contact', 'third_party', 'agent', 'unknown')
        self.plan_schema = {
                 '$id': self.plan_schema_id, 'title': 'rule-check plan (agent-written intent; facts are resolved by code)',
                 'type': 'object', 'additionalProperties': False, 'required': ['schema', 'category', 'kind', 'action', 'system'],
                 'properties': {
                     'schema': {'const': self.plan_schema_id}, 'category': _enum(CATEGORIES), 'kind': _enum(KINDS),
                     'action': _enum(ACTIONS), 'system': _enum(self.pack.SYSTEMS),
                     'recipient': {'type': 'object', 'additionalProperties': False, 'properties': {
                         'email': _str(254, EMAIL_RE), 'principal': _str(300), 'group': _str(200)}},
                     'source': {'type': 'object', 'additionalProperties': False, 'properties': {
                         'event_id': _str(24, '^[0-9a-f]{24}$'), 'burst_id': _str(16, '^in[0-9a-f]{14}$'),
                         'channel': _str(20, '^[CDG][A-Z0-9]{2,}$'), 'ts': _str(20, r'^\d+\.\d+$'),
                         'linear': _str(20, r'^[A-Z]{2,8}-\d{1,6}$')}},  # the ticket id; N2b renames it with the skill's schema
                     'scope': {'type': 'object', 'additionalProperties': False, 'properties': {
                         'roles': _list(_str(200), 30), 'resources': _list(_str(300), 100),
                         'resource_level': _enum(RESOURCE_LEVELS), 'environment': _enum(ENVIRONMENTS), 'level': _enum(ROLE_CLASSES),
                         'group_wide': _BOOL, **self.pack.PLAN_SCOPE, 'justification': _str(500),
                         'purpose': _str(500), 'items': _list({'type': 'object', 'additionalProperties': False,
                                                                 'required': ['system', 'op'],
                                                                 'properties': {'system': _enum(self.pack.SYSTEMS), 'op': _enum(OFFBOARD_OPS)}},
                                                                60, unique=False),
                         'last_day': _str(10, DATE_PAT), 'inventory_complete': _BOOL, 'dependencies': _list(_str(200), 30),
                         'message': _enum(MESSAGES), 'irreversible': _BOOL}},
                     'spend': {'type': 'object', 'additionalProperties': False,
                               'properties': {'recurring_usd_month': _NUM, 'one_time_usd': _NUM}},
                     'temporary': {'type': 'object', 'additionalProperties': False,
                                   'properties': {'expires_at': _str(10, DATE_PAT), 'days': {'type': 'integer', 'minimum': 1,
                                                                                            'maximum': 3650}}},
                     'credential': {'type': 'object', 'additionalProperties': False,
                                    'properties': {'type': _enum(self.pack.CREDENTIAL_TYPES), 'delivery': _enum(self.pack.DELIVERIES)}},
                     'task': _str(120, TASK_PAT), 'context': {'type': 'object', 'additionalProperties': False,
                                                              'properties': {'task_type': _enum(TASK_TYPES)}},
                     'notes': _str(500)}}
        self._level = _enum(self.levels)
        self._trigger_fields = {
                 'category': _enum(CATEGORIES), 'kinds': _list(_enum(KINDS), 5, min_items=1),
                 'actions': _list(_enum(ACTIONS), 20, min_items=1), 'systems': _list(_enum(self.pack.SYSTEMS), 50, min_items=1),
                 'levels': _list(_enum(ROLE_CLASSES), 10, min_items=1), 'max_level': _enum(ROLE_CLASSES),
                 'bundles': _list(_enum(self.pack.BUNDLES), 2, min_items=1), 'messages': _list(_enum(MESSAGES), 10, min_items=1),
                 'ops': _list(_enum(OFFBOARD_OPS), 20, min_items=1), 'credential': _BOOL, 'never_auto': _BOOL, 'group_wide': _BOOL}
        self.spec_schema = {
                 '$id': self.spec_schema_id, 'title': 'experience rules spec (one category, one version)',
                 'type': 'object', 'additionalProperties': False,
                 'required': ['schema', 'category', 'version', 'defaults', 'never_auto', 'role_catalog', 'rules'],
                 'properties': {
                     'schema': {'const': self.spec_schema_id}, 'category': _enum(CATEGORIES),
                     'version': {'type': 'integer', 'minimum': 1, 'maximum': 100000}, 'title_zh': _str(120),
                     'source': {'type': 'object', 'additionalProperties': _str(500)},
                     'revision': {'type': 'object', 'additionalProperties': False, 'properties': {
                         'from_version': {'type': 'integer', 'minimum': 1}, 'proposal': _str(16, '^rp[0-9a-f]{14}$'),
                         'approved_by': _str(20, USER_PAT), 'at': _str(40)}},
                     'defaults': {'type': 'object', 'additionalProperties': False,
                                  'required': ['company_domains', 'owner', 'approvers', 'hr', 'spend', 'temporary_days'],
                                  'properties': {
                                      'company_domains': _list(_str(100, r'^[a-z0-9.-]+\.[a-z]{2,}$'), 10, min_items=1),
                                      'managed_domains': _list(_str(100, r'^[a-z0-9.-]+\.[a-z]{2,}$'), 10),
                                      'owner': _str(20, USER_PAT), 'approvers': _list(_str(20, USER_PAT), 5, min_items=1),
                                      'approver_names_zh': {'type': 'object', 'propertyNames': _str(20, USER_PAT),
                                                            'additionalProperties': _str(40)},
                                      'hr': _list(_str(20, USER_PAT), 5), 'data_contacts': _list(_str(20, USER_PAT), 20),
                                      **self.pack.SPEC_DEFAULTS, 'temporary_days': {'type': 'integer', 'minimum': 1, 'maximum': 90},
                                      'spend': {'type': 'object', 'additionalProperties': False,
                                                'required': ['recurring_usd_month', 'one_time_usd'],
                                                'properties': {'recurring_usd_month': _NUM, 'one_time_usd': _NUM}}}},
                     'never_auto': {'type': 'object', 'additionalProperties': False, 'required': list(SHAPES),
                                    'properties': {s: {'$ref': '#/$defs/shape'} for s in SHAPES}},
                     'role_catalog': {'type': 'object', 'propertyNames': _enum(self.pack.SYSTEMS),
                                      'additionalProperties': {'type': 'object', 'propertyNames': _str(200),
                                                               'additionalProperties': _enum(ROLE_CLASSES)}},
                     'rules': _list({'$ref': '#/$defs/rule'}, 200, unique=False, min_items=1)},
                 '$defs': {
                     'shape': {'type': 'object', 'additionalProperties': False, 'required': ['level'], 'properties': {
                         'level': self._level, 'classes': _list(_enum(ROLE_CLASSES), 10), 'actions': _list(_enum(ACTIONS), 20),
                         'ops': _list(_enum(OFFBOARD_OPS), 20), 'environments': _list(_enum(ENVIRONMENTS), 4),
                         'systems': _list(_enum(self.pack.SYSTEMS), 50), 'resource_levels': _list(_enum(RESOURCE_LEVELS), 10),
                         'recurring_usd_month': _NUM, 'one_time_usd': _NUM, 'note_zh': _str(300)}},
                     'trigger': {'type': 'object', 'additionalProperties': False, 'properties': {
                         **self._trigger_fields, 'any': _list({'type': 'object', 'additionalProperties': False,
                                                          'properties': self._trigger_fields, 'minProperties': 1}, 8, unique=False,
                                                         min_items=1)}},
                     'rule': {'type': 'object', 'additionalProperties': False,
                              'required': ['id', 'name', 'title_zh', 'kind', 'level', 'trigger', 'checks', 'fallback'],
                              'properties': {
                                  'id': _str(40, self.pack.RULE_ID_PATTERN), 'name': _str(60, '^[a-z][a-z0-9_]{2,59}$'),
                                  'title_zh': _str(80), 'kind': _enum(('gate', 'domain')), 'level': self._level,
                                  'trigger': {'$ref': '#/$defs/trigger'},
                                  'checks': _list({'type': 'object', 'additionalProperties': False, 'required': ['name'],
                                                   'properties': {'name': _str(40, '^[a-z][a-z0-9_]{1,39}$'),
                                                                  'params': {'type': 'object'}}}, 20, unique=False),
                                  'bounds': {'type': 'object'}, 'limits': _list(_str(300), 20, unique=False),
                                  'fallback': _enum(self.fallbacks), 'claims': {'type': 'object', 'propertyNames': _enum(SHAPES),
                                                                           'additionalProperties': self._level},
                                  'note_zh': _str(500)}}}}
        self.active_schema = {
                 '$id': self.active_schema_id, 'type': 'object', 'additionalProperties': False, 'required': ['schema', 'categories'],
                 'properties': {'schema': {'const': self.active_schema_id},
                                'categories': {'type': 'object', 'propertyNames': _enum(CATEGORIES),
                                               'additionalProperties': _str(80, r'^[a-z]+-v\d{1,6}\.json$')},
                                'updated_at': _str(40), 'updated_by': _str(200)}}
        self.proposals_schema = {
                 '$id': self.proposals_schema_id, 'type': 'object', 'additionalProperties': False, 'required': ['schema', 'proposals'],
                 'properties': {
                     'schema': {'const': self.proposals_schema_id},
                     'proposals': _list({'type': 'object', 'additionalProperties': False,
                                         'required': ['kind', 'title_zh', 'rationale_zh'],
                                         'properties': {
                                             'kind': _enum(PROPOSAL_KINDS), 'category': _enum(CATEGORIES),
                                             'title_zh': _str(80), 'rationale_zh': _str(600), 'text_zh': _str(500),
                                             'base_version': {'type': 'integer', 'minimum': 1},
                                             'patch': _list({'type': 'object', 'additionalProperties': False,
                                                             'required': ['op', 'path'],
                                                             'properties': {'op': _enum(('replace', 'add', 'remove')),
                                                                            'path': _str(300, '^/'), 'value': {}}},
                                                            20, unique=False, min_items=1),
                                             'evidence': {'type': 'object', 'additionalProperties': False, 'properties': {
                                                 'check_ids': _list(_str(16, '^rc[0-9a-f]{14}$'), 50),
                                                 'triage_ids': _list(_str(16, '^tr[0-9a-f]{14}$'), 50),
                                                 'counts': {'type': 'object', 'additionalProperties': _INT}}}}},
                                        5, unique=False, min_items=1)}}
        self.check_params = {
                 'recipient_identity': {'hr_attested_actions': _list(_enum(ACTIONS), 20), 'personal_ok_status': _list(_enum(EMPLOYMENT), 5),
                                        'offboarding_actions': _list(_enum(ACTIONS), 20)},
                 'registry_status': {'allowed': _list(_enum(EMPLOYMENT), 5), 'offboarding_actions': _list(_enum(ACTIONS), 20),
                                     'hr_attested_actions': _list(_enum(ACTIONS), 20)},
                 'request_origin': {'hr_actions': _list(_enum(ACTIONS), 20), 'hr_systems': _list(_enum(self.pack.SYSTEMS), 50),
                                    'manager_actions': _list(_enum(ACTIONS), 20), 'manager_max_level': _enum(ROLE_CLASSES),
                                    'data_contact_systems': _list(_enum(self.pack.SYSTEMS), 10), 'self_actions': _list(_enum(ACTIONS), 20),
                                    'third_party_one_tap_systems': _list(_enum(self.pack.SYSTEMS), 10)},
                 'delivery_matrix': {'matrix': {'type': 'object', 'propertyNames': _enum(self.pack.CREDENTIAL_TYPES),
                                                'additionalProperties': _list(_enum(self.pack.DELIVERIES), 6, min_items=1)}},
                 'temporary_expiry': {'max_days': {'type': 'integer', 'minimum': 1, 'maximum': 365}},
                 **self.pack.check_params(self._level, self.relation_values), 'message_audience': {'preview': _list(_enum(MESSAGES), 10)},
                 'resource_ids': {}, 'justification_required': {}, 'offboarding_signal': {},
                 'requester_function': {'functions': _list(_enum(self.pack.FUNCTIONS), 3), 'disciplines': _list(_str(40), 20)},
                 'reversible_revocation': {'reversible_ops': _list(_enum(OFFBOARD_OPS), 15, min_items=1),
                                           'manual_systems': _list(_enum(self.pack.SYSTEMS), 20), 'privileged_flags': _list(_str(40), 10),
                                           'privileged_disciplines': _list(_str(40), 10)},
                 'daily_rate': {'max': {'type': 'integer', 'minimum': 1, 'maximum': 1000}},
             }
        self.checks = {name: self.pack.CHECKS.get(name) or getattr(self, 'check_' + name, None) or globals()['check_' + name] for name in self.check_params}
        self.action_zh = {'grant': self.msg('access.action_zh.grant'), 'change': self.msg('access.action_zh.change'), 'tighten': self.msg('access.action_zh.tighten'), 'revoke': self.msg('access.action_zh.revoke'), 'delete': self.msg('access.action_zh.delete'), 'rotate': self.msg('access.action_zh.rotate'),
                          'resend': self.msg('access.action_zh.resend'), 'repair': self.msg('access.action_zh.repair'), 'create_account': self.msg('access.action_zh.create_account'), 'create_credential': self.msg('access.action_zh.create_credential'), 'verify': self.msg('access.action_zh.verify'),
                          'inventory': self.msg('access.action_zh.inventory'), 'offboard': self.msg('access.action_zh.offboard'), 'notify': self.msg('access.action_zh.notify'), 'onboard_bundle': self.msg('access.action_zh.onboard_bundle'),
                          'agent_op': self.msg('access.action_zh.agent_op')}
        self.kind_zh = {'person': self.msg('access.kind_zh.person'), 'group': self.msg('access.kind_zh.group'), 'workload': self.msg('access.kind_zh.workload'), 'external': self.msg('access.kind_zh.external'), 'agent_ops': self.msg('access.kind_zh.agent_ops')}
        self.outcome_zh = {'auto': self.msg('access.outcome_zh.auto'), 'one_tap': self.msg('access.outcome_zh.one_tap'), 'clarify': self.msg('access.outcome_zh.clarify'), 'route': self.msg('access.outcome_zh.route'),
                           self.key: self.msg('access.outcome_zh.owner', owner=self.name)}
        self.relation_zh = {'self': self.msg('access.relation_zh.self'), self.key: self.name, 'approver': self.msg('access.relation_zh.approver'), 'hr': 'HR', 'manager': self.msg('access.relation_zh.manager'),
                            'data_contact': self.msg('access.relation_zh.data_contact'), 'third_party': self.msg('access.relation_zh.third_party'), 'agent': self.msg('access.relation_zh.agent'), 'unknown': self.msg('access.relation_zh.unknown')}
        self.shape_zh = {'unknown_role': self.msg('access.shape_zh.unknown_role'), 'admin_or_owner': self.msg('access.shape_zh.admin_or_owner'), 'prod_destructive': self.msg('access.shape_zh.prod_destructive'),
                         'spend_over_threshold': self.msg('access.shape_zh.spend_over_threshold'), 'group_wide_expansion': self.msg('access.shape_zh.group_wide_expansion'),
                         'personal_prod_access': self.msg('access.shape_zh.personal_prod_access'), 'workload_admin_identity': self.msg('access.shape_zh.workload_admin_identity'),
                         'auth_entry_change': self.msg('access.shape_zh.auth_entry_change'), 'account_wide_secret_read': self.msg('access.shape_zh.account_wide_secret_read'),
                         'unregistered_principal': self.msg('access.shape_zh.unregistered_principal')}
        self.slack_zh = {'ok': self.msg('access.slack_zh.ok'), 'not_found': self.msg('access.slack_zh.not_found'), 'deleted': self.msg('access.slack_zh.deleted'), 'bot': self.msg('access.slack_zh.bot'),
                         'restricted': self.msg('access.slack_zh.restricted'), 'email_mismatch': self.msg('access.slack_zh.email_mismatch'), 'skipped': self.msg('access.slack_zh.skipped')}
        self.status_zh = {'active': self.msg('access.status_zh.active'), 'work_trial': self.msg('access.status_zh.work_trial'), 'left': self.msg('access.status_zh.left'), 'external': self.msg('access.status_zh.external'), 'unknown': self.msg('access.status_zh.unknown')}
        self.reason_why = {
                 'recipient_not_company_email': self.msg('access.reason_why.recipient_not_company_email'),
                 'recipient_slack_deactivated': self.msg('access.reason_why.recipient_slack_deactivated'), 'recipient_not_in_slack': self.msg('access.reason_why.recipient_not_in_slack'),
                 'recipient_bot': self.msg('access.reason_why.recipient_bot'), 'recipient_restricted': self.msg('access.reason_why.recipient_restricted'),
                 'recipient_email_mismatch': self.msg('access.reason_why.recipient_email_mismatch'),
                 'registry_status_left': self.msg('access.reason_why.registry_status_left'), 'registry_status_external': self.msg('access.reason_why.registry_status_external'),
                 'request_from_agent': self.msg('access.reason_why.request_from_agent'), 'third_party_requester': self.msg('access.reason_why.third_party_requester'),
                 'business_owner_request': self.msg('access.reason_why.business_owner_request'), 'credential_type_without_channel': self.msg('access.reason_why.credential_type_without_channel'),
                 'temporary_too_long': self.msg('access.reason_why.temporary_too_long'), **self.pack.REASON_WHY, 'message_preview': self.msg('access.reason_why.message_preview'),
                 'requester_function_not_allowed': self.msg('access.reason_why.requester_function_not_allowed'),
                 'departure_not_confirmed': self.msg('access.reason_why.departure_not_confirmed'), 'before_last_day': self.msg('access.reason_why.before_last_day'),
                 'privileged_subject': self.msg('access.reason_why.privileged_subject'), 'live_dependency': self.msg('access.reason_why.live_dependency'),
                 'manual_offboarding_step': self.msg('access.reason_why.manual_offboarding_step'), 'daily_rate_exceeded': self.msg('access.reason_why.daily_rate_exceeded')}
        self.shape_why = {'unknown_role': self.msg('access.shape_why.unknown_role'), 'admin_or_owner': self.msg('access.shape_why.admin_or_owner'),
                          'prod_destructive': self.msg('access.shape_why.prod_destructive'), 'spend_over_threshold': self.msg('access.shape_why.spend_over_threshold'),
                          'group_wide_expansion': self.msg('access.shape_why.group_wide_expansion'), 'personal_prod_access': self.msg('access.shape_why.personal_prod_access'),
                          'workload_admin_identity': self.msg('access.shape_why.workload_admin_identity'), 'auth_entry_change': self.msg('access.shape_why.auth_entry_change'),
                          'account_wide_secret_read': self.msg('access.shape_why.account_wide_secret_read'), 'unregistered_principal': self.msg('access.shape_why.unregistered_principal')}
        self.reversible_zh = {'grant': self.msg('access.reversible_zh.grant'), 'change': self.msg('access.reversible_zh.change'), 'tighten': self.msg('access.reversible_zh.tighten'),
                              'revoke': self.msg('access.reversible_zh.revoke'), 'create_account': self.msg('access.reversible_zh.create_account'), 'onboard_bundle': self.msg('access.reversible_zh.onboard_bundle')}
        self.typed_click_zh = self.msg('access.typed_click_zh.1')
        self.typed_sensitive_zh = self.msg('access.typed_sensitive_zh.1')
        self.typed_line_text = self.msg('access.typed_line_text.1')
        self.status_next = {
                 'approved': 'Approved for exactly the stored plan (plan_hash). Execute it, then rule-outcome --result executed.',
                 'approved_with_condition': (f'{self.name} approved with a condition (note). The plan must change: revise it and run rule-check --card again; do not execute this check.'),
                 'rejected': 'Rejected. Do not execute; tell the requester if appropriate and record rule-outcome --result abandoned.',
                 'self_approval_refused': 'The approver is the recipient. Another approver must decide: run rule-check --card again.',
                 'approver_not_in_rules': 'The clicker is not an approver in the rules. Run rule-check --card again.',
                 'pending': 'Waiting for the card decision.', 'not_requested': f'Run rule-check --card to ask {self.name}.',
                 'clarify_first': 'Ask what clarify lists, then check a completed plan.', 'not_required': 'No approval needed.'}
        self.proposal_options = [self.msg('access.proposal_options.1'), self.msg('access.proposal_options.2')]
        self.addenda_heading = self.msg('access.addenda_heading.1', owner=self.name)

    def msg(self, key, **values):
        return locale.tracker_message(self.language, key, self.tracker, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def check_param_schema(self, name):
        return {'type': 'object', 'additionalProperties': False, 'properties': self.check_params[name]}

    def spec_problems(self, spec):
        """Every reason `spec` is not a valid rules spec (schema, then semantics). Empty list = valid."""
        problems = schema_errors(spec, self.spec_schema, limit=40)
        if problems:
            return problems
        ids = [r['id'] for r in spec['rules']]
        problems += [f'$.rules: duplicate id {i}' for i in sorted({i for i in ids if ids.count(i) > 1})]
        if not any(r['kind'] == 'domain' for r in spec['rules']):
            problems.append('$.rules: at least one domain rule is required')
        for n, rule in enumerate(spec['rules']):
            where = f'$.rules[{n}] ({rule["id"]})'
            if rule['kind'] == 'gate' and (rule['level'] != 'auto' or rule.get('claims')):
                problems.append(f'{where}: a gate rule has level auto and no claims')
            trig = rule['trigger']
            for t in [trig, *trig.get('any', [])]:
                if t.get('category', spec['category']) != spec['category']:
                    problems.append(f'{where}: trigger category must be {spec["category"]}')
            for k, check in enumerate(rule['checks']):
                if check['name'] not in self.checks:
                    problems.append(f'{where}.checks[{k}]: unknown check {check["name"]}')
                    continue
                problems += [e.replace('$', f'{where}.checks[{k}].params', 1)
                             for e in schema_errors(check.get('params', {}), self.check_param_schema(check['name']))]
        defaults = spec['defaults']
        if defaults['owner'] not in defaults['approvers']:
            problems.append(f'$.defaults: owner ({self.name}) must be one of the approvers')
        if set(defaults.get('hr', [])) & set(defaults['approvers']):
            problems.append('$.defaults: hr and approvers must differ')
        return problems[:60]

    def validate_spec(self, spec):
        problems = self.spec_problems(spec)
        if problems:
            raise ValueError('invalid_rules_spec: ' + '; '.join(problems[:5]))
        return spec

    def rules_dir(self, path=None):
        return Path(path or os.environ.get(self.rules_dir_env) or self.default_rules_dir).expanduser()

    def load_spec_file(self, path):
        path = Path(path)
        st = _check_private(path)  # every load: a cached spec never outlives a permission change
        key = (str(path), st.st_mtime_ns, st.st_size)
        cached = _SPEC_CACHE.get(str(path))
        if cached and cached[0] == key:
            return copy.deepcopy(cached[1])
        spec = self.validate_spec(_read_private_json(path))
        _SPEC_CACHE[str(path)] = (key, spec)
        return copy.deepcopy(spec)

    def read_active(self, directory=None):
        directory = self.rules_dir(directory)
        active = _read_private_json(directory / 'active.json')
        problems = schema_errors(active, self.active_schema)
        if problems:
            raise ValueError('invalid_rules_active_json: ' + '; '.join(problems[:3]))
        return active

    def load_active(self, category='access', directory=None):
        """(spec, info) of the active version of `category`; raises ValueError when not installed or invalid."""
        directory = self.rules_dir(directory)
        if not (directory / 'active.json').exists():
            raise ValueError('rules_not_installed')
        name = self.read_active(directory)['categories'].get(category)
        if not name:
            raise ValueError(f'rules_category_not_active:{category}')
        spec = self.load_spec_file(directory / name)
        if spec['category'] != category or name != f'{category}-v{spec["version"]}.json':
            raise ValueError('rules_active_file_does_not_match_its_category_and_version')
        return spec, {'dir': str(directory), 'file': name, 'version': spec['version'], 'digest': digest(spec)}

    def install_spec(self, spec, directory=None, *, who, note=None, now=None):
        """Write `<category>-v<N>.json` (validated) and point active.json at it; refuses to overwrite a version."""
        now = time.time() if now is None else now
        self.validate_spec(spec)
        directory = self.rules_dir(directory)
        name = f'{spec["category"]}-v{spec["version"]}.json'
        target = directory / name
        if target.exists() and digest(self.load_spec_file(target)) != digest(spec):
            raise ValueError('rules_version_exists_with_different_content')
        write_private_json(target, spec)
        active = self.read_active(directory) if (directory / 'active.json').exists() else {
            'schema': self.active_schema_id, 'categories': {}}
        previous = active['categories'].get(spec['category'])
        active['categories'][spec['category']] = name
        active.update(updated_at=self.iso(now), updated_by=who)
        write_private_json(directory / 'active.json', active)
        append_changelog(directory, {'at': self.iso(now), 'ts': now, 'kind': 'activate', 'category': spec['category'],
                                     'from': previous, 'to': name, 'digest': digest(spec), 'who': who, 'note': note})
        return {'ok': True, 'dir': str(directory), 'file': name, 'previous': previous, 'digest': digest(spec)}

    def install_file(self, path, who, rollback=False, directory=None, now=None):
        """`rules install`: validate a spec file, write it as <category>-v<N>.json and make it active."""
        try:
            spec = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('spec_file_unreadable_or_not_json') from None
        self.validate_spec(spec)
        directory = self.rules_dir(directory)
        current = None
        if (directory / 'active.json').exists():
            name = self.read_active(directory)['categories'].get(spec['category'])
            if name:
                current = self.load_spec_file(directory / name)['version']
        if current is not None and current > spec['version'] and not rollback:
            raise ValueError(f'active_version_v{current}_is_newer_use_--rollback')
        result = self.install_spec(spec, directory, who=who, note='rollback' if rollback else 'install', now=now)
        return {**result, 'version': spec['version'], 'rules': len(spec['rules']), 'replaced_version': current}

    def install_cli(self, rt, path, who=None, rollback=False):
        """CLI `rules install`: the owner only, like `routines enable` -- refused for any agent identity (CLAUDE_CODE_SESSION_ID,
        CLAUDECODE, CODEX_THREAD_ID) and inside dispatched runs. Rules change otherwise only through adopted proposals."""
        if rt._agent_session() or os.environ.get(self.run_id_env):
            raise ValueError(f'only_{self.key}_installs_rules_never_an_agent_session_or_a_dispatched_run')
        try:
            raw = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict) and re.fullmatch(r'R-\d{4}', str(raw.get('id'))):  # P47: one rule of the registry
            return rt.rule_registry.install(path, who or self.key)
        return self.install_file(path, who or self.key, rollback)

    def iso(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz).isoformat(timespec='seconds')

    def local_date(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz).date()

    def normalize_plan(self, raw):
        """Validated, canonical plan (emails lower-cased) or ValueError with the schema problems."""
        if isinstance(raw, (str, bytes)):
            if len(raw) > MAX_PLAN_BYTES:
                raise ValueError('plan_too_large')
            try:
                raw = json.loads(raw)
            except ValueError:
                raise ValueError('plan_is_not_valid_json') from None
        if len(canonical(raw).encode()) > MAX_PLAN_BYTES:
            raise ValueError('plan_too_large')
        problems = schema_errors(raw, self.plan_schema)
        if problems:
            raise ValueError('invalid_plan: ' + '; '.join(problems[:6]))
        plan = copy.deepcopy(raw)
        recipient = plan.get('recipient') or {}
        if recipient.get('email'):
            recipient['email'] = recipient['email'].lower()
        source = plan.get('source') or {}
        if bool(source.get('channel')) != bool(source.get('ts')):
            raise ValueError('invalid_plan: source.channel and source.ts go together')
        return plan

    def _person_facts(self, person, slack_of):
        """Whitelisted, rule-relevant fields only. <owner key>_view is never read."""
        def get(*keys):
            node = person
            for key in keys:
                node = node.get(key) if isinstance(node, dict) else None
            return node
        flags = [f for f in get('flags') or [] if isinstance(f, str)]
        status = get('employment', 'status') if get('employment', 'status') in EMPLOYMENT else 'unknown'
        registered = []
        for ident in get('identities') or []:
            if not isinstance(ident, dict) or ident.get('type') not in ('email', 'gws', 'other'):
                continue
            value = str(ident.get('value') or '').strip().lower()
            if '@' in value and (status == 'work_trial' or ident.get('source') in ('task_history', f'{self.key}_note', 'policy')):
                registered.append(value)
        lead = get('role', 'team', 'lead_person_id')
        scope = get('authority', 'approver_scope')
        return {'found': True, 'status': status,
                'contract': get('employment', 'contract') if isinstance(get('employment', 'contract'), str) else 'unknown',
                'function': get('role', 'function') if get('role', 'function') in self.pack.FUNCTIONS else 'unknown',
                'disciplines': [d for d in get('role', 'disciplines') or [] if isinstance(d, str)], 'flags': flags,
                'team_lead': slack_of.get(lead) if isinstance(lead, str) else None, 'principals': sorted(set(registered)),
                'privileged': 'privileged_access' in flags or scope in self.pack.PRIVILEGED_SCOPES}

    def people_index(self, path):
        """{'email': {...}, 'slack': {...}} from the people registry (cached by mtime). Missing/malformed = empty."""
        path = Path(path).expanduser()
        try:
            st = path.stat()
        except OSError:
            return {'email': {}, 'slack': {}}
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _PEOPLE_CACHE.get('key') == key:
            return _PEOPLE_CACHE['value']
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raw = {}
        people = [p for p in (raw.get('people') if isinstance(raw, dict) else None) or [] if isinstance(p, dict)]
        slack_of = {}
        for person in people:
            for ident in person.get('identities') or []:
                if isinstance(ident, dict) and ident.get('type') == 'slack' and ident.get('primary'):
                    slack_of[str(person.get('id'))] = str(ident.get('value'))
        index = {'email': {}, 'slack': {}}
        for person in people:
            facts = self._person_facts(person, slack_of)
            for ident in person.get('identities') or []:
                if not isinstance(ident, dict):
                    continue
                value = str(ident.get('value') or '').strip()
                if ident.get('type') in ('email', 'gws', 'entra') and '@' in value:
                    index['email'].setdefault(value.lower(), facts)
                elif ident.get('type') == 'slack' and re.fullmatch(r'[UW][A-Z0-9]+', value):
                    index['slack'].setdefault(value, facts)
        _PEOPLE_CACHE.update(key=key, value=index)
        return index

    def relations(self, spec, requester, recipient_slack, registry):
        """Who the requester is to this plan; names, text and self-description never count."""
        if requester.get('author_kind') in ('agent', 'system'):
            return ['agent']
        uid = requester.get('id')
        if not uid:
            return ['unknown']
        d = spec['defaults']
        rels = []
        if recipient_slack and uid == recipient_slack:
            rels.append('self')
        if uid == d['owner']:
            rels.append(self.key)
        if uid in d['approvers']:
            rels.append('approver')
        if uid in d.get('hr', []):
            rels.append('hr')
        if registry.get('team_lead') and uid == registry['team_lead']:
            rels.append('manager')
        if uid in d.get('data_contacts', []):
            rels.append('data_contact')
        return rels or ['third_party']

    def resolve_facts(self, rt, db, spec, plan):
        facts = empty_facts()
        rec = facts['recipient']
        email = (plan.get('recipient') or {}).get('email')
        if email:
            domain = email.rsplit('@', 1)[1]
            rec.update(email=email, domain=domain, company_domain=domain in spec['defaults']['company_domains'])
            if plan['kind'] in ('person', 'external'):
                rec['slack'] = slack_identity(rt, email)
            index = self.people_index(rt.envelopes.people_path())
            person = index['email'].get(email) or (index['slack'].get(rec['slack']['id']) if rec['slack']['id'] else None)
            if person:
                rec['registry'] = copy.deepcopy(person)
        requester = request_origin(rt, db, plan.get('source'))
        requester['relations'] = self.relations(spec, requester, rec['slack']['id'], rec['registry'])
        facts['requester'] = requester
        facts['principal'] = principal_facts(spec, plan, rec)
        return facts

    def check_recipient_identity(self, ctx, p, rule):
        if ctx.plan['kind'] not in ('person', 'external'):
            return None
        rec, reg, action = ctx.facts['recipient'], ctx.reg, ctx.plan['action']
        if not rec['email']:
            return missing('recipient_email_missing', self.msg('access.check_recipient_identity.1'), 'agent')
        if not rec['company_domain'] and not (reg['found'] and reg['status'] in p.get('personal_ok_status', []) and
                                              rec['email'] in reg['principals']):
            return fail('recipient_not_company_email', self.msg('access.check_recipient_identity.2'), self.key)
        slack = rec['slack']['status']
        if slack == 'ok':
            return None
        if slack in ('not_found', 'deleted'):
            if action in p.get('offboarding_actions', []) and (slack == 'deleted' or reg['status'] == 'left'):
                return None
            if slack == 'not_found' and action in p.get('hr_attested_actions', []) and ctx.rels & {'hr', self.key, 'approver'}:
                return None
            if slack == 'deleted':
                return fail('recipient_slack_deactivated', self.msg('access.check_recipient_identity.3'), self.key)
            return fail('recipient_not_in_slack', self.msg('access.check_recipient_identity.4'), self.key)
        if slack.startswith('error'):
            return missing('slack_lookup_failed', self.msg('access.check_recipient_identity.5') + slack[6:] + self.msg('access.check_recipient_identity.6'), 'agent')
        return fail('recipient_' + slack, {'bot': self.msg('access.check_recipient_identity.bot'), 'restricted': self.msg('access.check_recipient_identity.restricted'),
                                           'email_mismatch': self.msg('access.check_recipient_identity.email_mismatch')}.get(slack, slack), self.key)

    def check_registry_status(self, ctx, p, rule):
        if ctx.plan['kind'] not in ('person', 'external'):
            return None
        reg, action = ctx.reg, ctx.plan['action']
        if not reg['found']:
            if action in p.get('hr_attested_actions', []) and ctx.rels & {'hr', self.key, 'approver'}:
                return None  # a new hire announced by HR (or an approver): the request itself is the employment source
            return missing('registry_entry_missing', self.msg('access.check_registry_status.1'), 'hr')
        if reg['status'] in p.get('allowed', ['active', 'work_trial']):
            return None
        if reg['status'] == 'left' and action in p.get('offboarding_actions', []):
            return None
        if reg['status'] in ('left', 'external'):
            return fail('registry_status_' + reg['status'], self.msg('access.check_registry_status.2'), self.key)
        return missing('registry_status_unknown', self.msg('access.check_registry_status.3'), 'hr')

    def check_request_origin(self, ctx, p, rule):
        rels, action, system = ctx.rels, ctx.plan['action'], ctx.plan['system']
        if 'unknown' in rels:
            return missing('request_source_missing', self.msg('access.check_request_origin.1'),
                           'agent')
        if 'agent' in rels:
            return fail('request_from_agent', self.msg('access.check_request_origin.2'), self.key)
        if rels & {self.key, 'approver'}:
            return None
        if 'self' in rels and action in p.get('self_actions', ACTIONS):
            return None
        if 'hr' in rels and (action in p.get('hr_actions', []) or system in p.get('hr_systems', [])):
            return None
        if 'manager' in rels and action in p.get('manager_actions', []) and (
                ctx.max_class is None or CLASS_RANK[ctx.max_class] <= CLASS_RANK[p.get('manager_max_level', 'write')]):
            return None
        if 'data_contact' in rels and system in p.get('data_contact_systems', []) and action in ('grant', 'change'):
            return None
        if system in p.get('third_party_one_tap_systems', []) and action in ('create_account', 'change'):
            return escalate('one_tap', 'business_owner_request', self.msg('access.check_request_origin.3'))
        return fail('third_party_requester', self.msg('access.check_request_origin.4'), self.key)

    def check_delivery_matrix(self, ctx, p, rule):
        cred = ctx.plan.get('credential') or {}
        kind = cred.get('type') or 'none'
        if kind == 'none':
            return None
        allowed = p.get('matrix', {}).get(kind)
        if not allowed:
            return fail('credential_type_without_channel', self.msg('access.check_delivery_matrix.1', kind=kind), self.key)
        if cred.get('delivery') in allowed:
            return None
        return fail('credential_channel_not_allowed', self.msg('access.check_delivery_matrix.2', kind=kind, join=' / '.join(allowed)), 'clarify', 'agent')

    def check_temporary_expiry(self, ctx, p, rule):
        tmp = ctx.plan.get('temporary') or {}
        if not tmp and not ctx.scope.get('tool_evaluation'):
            return None
        days = tmp.get('days')
        today = self.local_date(ctx.now)
        default = (today + dt.timedelta(days=ctx.spec['defaults']['temporary_days'])).isoformat()
        if tmp.get('expires_at'):
            try:
                days = (dt.date.fromisoformat(tmp['expires_at']) - today).days
            except ValueError:
                return fail('expires_at_invalid', self.msg('access.check_temporary_expiry.1'), 'clarify', 'agent')
            if days < 1:
                return fail('expires_at_not_in_future', self.msg('access.check_temporary_expiry.2'), 'clarify', 'agent')
        if days is None:
            return missing('expiry_missing', self.msg('access.check_temporary_expiry.3', default=default, temporary_days=ctx.spec['defaults']['temporary_days']), 'agent')
        if days > p.get('max_days', ctx.spec['defaults']['temporary_days']):
            return fail('temporary_too_long', self.msg('access.check_temporary_expiry.4', get=p.get('max_days')), self.key)
        return None

    def check_message_audience(self, ctx, p, rule):
        message = ctx.scope.get('message')
        if not message:
            return missing('message_kind_missing', self.msg('access.check_message_audience.1'), 'agent')
        if message in p.get('preview', []):
            return escalate('one_tap', 'message_preview', self.msg('access.check_message_audience.2', owner=self.name))
        return None

    def check_resource_ids(self, ctx, p, rule):
        if not ctx.scope.get('resources'):
            return missing('resource_ids_missing', self.msg('access.check_resource_ids.1'), 'requester')
        if ctx.plan['kind'] == 'workload' and not ctx.scope.get('purpose'):
            return missing('workload_service_missing', self.msg('access.check_resource_ids.2'), 'requester')
        return None

    def check_justification_required(self, ctx, p, rule):
        if ctx.scope.get('justification'):
            return None
        return missing('justification_missing', self.msg('access.check_justification_required.1'), 'requester')

    def check_requester_function(self, ctx, p, rule):
        reg = ctx.reg
        if reg['function'] in p.get('functions', []) or set(reg['disciplines']) & set(p.get('disciplines', [])):
            return None
        if reg['function'] == 'unknown':
            return missing('role_function_unknown', self.msg('access.check_requester_function.1'), 'hr')
        return fail('requester_function_not_allowed', self.msg('access.check_requester_function.2'), self.key)

    def check_offboarding_signal(self, ctx, p, rule):
        reg, slack = ctx.reg, ctx.facts['recipient']['slack']['status']
        if reg['status'] == 'left' or slack == 'deleted' or ctx.rels & {'hr', self.key, 'approver', 'manager'}:
            return None
        return missing('departure_signal_missing', self.msg('access.check_offboarding_signal.1', owner=self.name), 'hr')

    def check_reversible_revocation(self, ctx, p, rule):
        reg, scope = ctx.reg, ctx.scope
        departed = reg['status'] == 'left' or ctx.facts['recipient']['slack']['status'] == 'deleted' or (
            reg['status'] == 'work_trial' and ctx.rels & {self.key, 'approver', 'hr'})
        if not departed:
            return fail('departure_not_confirmed', self.msg('access.check_reversible_revocation.1', owner=self.name), self.key)
        if scope.get('last_day') and dt.date.fromisoformat(scope['last_day']) >= self.local_date(ctx.now):
            return fail('before_last_day', self.msg('access.check_reversible_revocation.2'), self.key)
        if set(reg['flags']) & set(p.get('privileged_flags', [])) or reg['privileged'] or \
                set(reg['disciplines']) & set(p.get('privileged_disciplines', [])):
            return fail('privileged_subject', self.msg('access.check_reversible_revocation.3'), self.key)
        if not scope.get('inventory_complete'):
            return missing('inventory_incomplete', self.msg('access.check_reversible_revocation.4'), 'agent')
        if scope.get('dependencies'):
            return fail('live_dependency', self.msg('access.check_reversible_revocation.5'), self.key)
        manual = sorted({i['system'] for i in scope.get('items') or []
                         if i['op'] == 'manual' or i['system'] in p.get('manual_systems', [])})
        if manual:
            return escalate('one_tap', 'manual_offboarding_step', ' / '.join(manual) + self.msg('access.check_reversible_revocation.6', owner=self.name))
        return None

    def check_daily_rate(self, ctx, p, rule):
        count = ctx.rate(rule['id'])
        if count >= p['max']:
            return fail('daily_rate_exceeded', self.msg('access.check_daily_rate.1', count=count, max=p['max']), self.key)
        return None

    def strictest(self, outcomes):
        return max(outcomes, key=self.rank.get)

    def fallback_outcome(self, fallback):
        return self.key if fallback == 'stop' else fallback

    def evaluate(self, spec, plan, facts, now=None, rate=None):
        """(spec, normalized plan, facts) -> result. Pure: no Slack, no SQLite, no files."""
        now = time.time() if now is None else now
        classes, unknown = classify_roles(spec, plan)
        ctx = Ctx(spec, plan, facts, classes, unknown, now, rate)
        ctx.shapes = never_auto_shapes(spec, plan, facts, classes, unknown)
        matched = [r for r in spec['rules'] if trigger_matches(r['trigger'], ctx)]
        claims = {}
        for rule in matched:
            for shape, level in (rule.get('claims') or {}).items():
                if shape not in claims or self.rank[level] < self.rank[claims[shape][0]]:
                    claims[shape] = (level, rule['id'])
        for shape in ctx.shapes:
            claimed = claims.get(shape['shape'])
            shape['effective'] = claimed[0] if claimed else shape['level']
            shape['claimed_by'] = claimed[1] if claimed else None
        results, failed, escalations, stop = [], [], [], False
        for rule in matched:
            outcome = rule['level']
            if rule['trigger'].get('never_auto'):
                outcome = self.strictest([s['effective'] for s in ctx.shapes] or [self.key])
            for check in rule['checks']:
                got = self.checks[check['name']](ctx, check.get('params') or {}, rule)
                if got is None:
                    continue
                got = {**got, 'rule': rule['id'], 'check': check['name'],
                       'outcome': got['outcome'] or self.fallback_outcome(rule['fallback'])}
                if got['status'] == 'fail' and rule['fallback'] == 'stop' and got['outcome'] == self.key:
                    stop = True
                (escalations if got['status'] == 'escalate' else failed).append(got)
                outcome = self.strictest([outcome, got['outcome']])
            results.append({'id': rule['id'], 'name': rule['name'], 'kind': rule['kind'], 'level': rule['level'],
                            'outcome': outcome})
        covered = any(r['kind'] == 'domain' for r in results)
        final = self.strictest([r['outcome'] for r in results] + ([] if covered else [self.key]))
        bounds = {r['id']: r['bounds'] for r in matched if r.get('bounds')}
        limits = list(dict.fromkeys(x for r in matched for x in r.get('limits') or []))
        return {'outcome': final, 'tier': self.tier[final], 'covered': covered, 'stop': stop,
                'matched_rules': [r['id'] for r in results], 'rules': results, 'failed_checks': failed,
                'escalations': escalations, 'never_auto': ctx.shapes, 'classes': sorted(classes, key=CLASS_RANK.get),
                'unknown_roles': unknown, 'clarify': [{'who': f['who'] or 'requester', 'zh': f['zh'], 'rule': f['rule']}
                                                      for f in failed if f['outcome'] == 'clarify'],
                'route': [{'to': f['who'], 'zh': f['zh'], 'rule': f['rule']} for f in failed if f['outcome'] == 'route'],
                'bounds': bounds, 'limits': limits, 'approval_required': final in ('one_tap', self.key)}

    def summary_zh(self, spec, plan, facts, result, check_id=None, phash=None, smoke=False):
        """Technical Chinese summary of a check for the agent (CLI output) and the record. Since P3.1 the card the owner reads
        is `card_text` (one plain sentence + why + reversibility); this text never goes to Slack."""
        scope, rec = plan.get('scope') or {}, facts['recipient']
        head = ('🧪 TEST · ' if smoke else '') + self.msg('access.summary_zh.1') + (f' {check_id}' if check_id else '') + \
            f' · {spec["category"]} v{spec["version"]}' + (self.msg('access.summary_zh.2', value=phash[:8]) if phash else '')
        what = f"{self.pack.SYSTEM_ZH.get(plan['system'], plan['system'])} · {self.action_zh.get(plan['action'], plan['action'])} · {self.kind_zh.get(plan['kind'], plan['kind'])}"
        detail = []
        if scope.get('roles'):
            detail.append(self.msg('access.summary_zh.3') + ', '.join(scope['roles'][:4]) + (' …' if len(scope['roles']) > 4 else ''))
        if scope.get('resources'):
            detail.append(self.msg('access.summary_zh.4', len=len(scope['resources'])) + (self.msg('access.summary_zh.5', resource_level=scope['resource_level']) if scope.get('resource_level') else ''))
        if scope.get('products'):
            limits = scope.get('daily_limits_usd') or {}
            detail.append(self.msg('access.summary_zh.6') + ', '.join(f'{x}' + (self.msg('access.summary_zh.7', value=limits[x]) if x in limits else '') for x in scope['products']))
        if scope.get('environment'):
            detail.append(self.msg('access.summary_zh.8') + scope['environment'])
        lines = [head, what + (self.msg('access.summary_zh.10', join=self.msg('access.summary_zh.9').join(detail)) if detail else '')]
        who = rec['email'] or (plan.get('recipient') or {}).get('group') or (plan.get('recipient') or {}).get('principal')
        if who:
            reg = rec['registry']
            lines.append(self.msg('access.summary_zh.11', who=who, get=self.slack_zh.get(rec['slack']['status'], rec['slack']['status'])) +
                         (self.msg('access.summary_zh.12', get=self.status_zh.get(reg['status'], reg['status']), get2=self.pack.FUNCTION_ZH.get(reg['function']))
                          if reg['found'] else self.msg('access.summary_zh.13')))
        lines.append(self.msg('access.summary_zh.14') + self.msg('access.summary_zh.15').join(self.relation_zh.get(r, r) for r in facts['requester'].get('relations') or ['unknown']))
        spend = plan.get('spend') or {}
        if spend:
            lines.append(self.msg('access.summary_zh.16') + self.msg('access.summary_zh.17').join(f"{(self.msg('access.summary_zh.18') if k == 'recurring_usd_month' else self.msg('access.summary_zh.19'))} ${v:g}" for k, v in spend.items()))
        if (plan.get('temporary') or {}).get('expires_at'):
            lines.append(self.msg('access.summary_zh.20') + plan['temporary']['expires_at'])
        lines.append(self.msg('access.summary_zh.21', value=self.outcome_zh[result['outcome']]) + (self.msg('access.summary_zh.22', owner=self.name) if not result['covered'] else ''))
        reasons = [f'{x["rule"]} {x["zh"]}' for x in result['escalations'] + result['failed_checks']][:6]
        reasons += [self.msg('access.summary_zh.23', get=self.shape_zh.get(s['shape'], s['shape'])) + (self.msg('access.summary_zh.24', claimed_by=s['claimed_by'])
                                                                            if s['effective'] == 'one_tap' else '')
                    for s in result['never_auto']][:4]
        lines += ['· ' + r for r in reasons]
        lines.append(self.msg('access.summary_zh.25') + (' '.join(result['matched_rules']) or self.msg('access.summary_zh.26')))
        if result['approval_required']:
            names = spec['defaults'].get('approver_names_zh') or {}
            lines.append(self.msg('access.summary_zh.27') + ' / '.join(names.get(u, u) for u in spec['defaults']['approvers']) +
                         self.msg('access.summary_zh.28'))
        return '\n'.join(lines)

    def _plain_reason(self, zh):
        """Fallback for an unmapped reason: the check's own text without parentheticals or ids."""
        text = re.sub(r'[（(][^）)]*[）)]', '', str(zh or ''))
        text = re.sub(self.pack.RULE_REF_PATTERN, '', text)
        text = text.split(self.msg('access._plain_reason.1'))[0].strip(' ，,。；;')
        return text or self.msg('access._plain_reason.2')

    def why_zh(self, result):
        """Why the owner has to decide: only the reasons behind the FINAL outcome (other checks never leak into the card)."""
        if result.get('disabled'):
            return self.msg('access.why_zh.1')
        final = result['outcome']
        reasons = []
        for f in result['failed_checks'] + result['escalations']:
            if f['outcome'] == final and f['status'] in ('fail', 'escalate'):
                reasons.append(self.reason_why.get(f['reason']) or self._plain_reason(f.get('zh')))
        reasons += [self.shape_why.get(s['shape'], self.msg('access.why_zh.2')) for s in result['never_auto'] if s['effective'] == final]
        if not result['covered']:
            reasons.append(self.msg('access.why_zh.3'))
        reasons = list(dict.fromkeys(reasons))[:2]
        if not reasons:
            return self.msg('access.why_zh.4') if final == 'one_tap' else self.msg('access.why_zh.5')
        return self.msg('access.why_zh.6').join(reasons) + self.msg('access.why_zh.7')

    def reversibility_zh(self, plan, result):
        """Only when known: irreversible (flagged, destructive or a delete), or the action is plainly undoable."""
        scope = plan.get('scope') or {}
        ops = {i.get('op') for i in scope.get('items') or []}
        if scope.get('irreversible') or plan['action'] == 'delete' or 'delete' in ops or \
                any(s['shape'] == 'prod_destructive' for s in result['never_auto']):
            return self.msg('access.reversibility_zh.1')
        if plan['action'] == 'offboard':
            return self.msg('access.reversibility_zh.2') if ops and ops <= set(UNDOABLE_OPS) else None
        return self.reversible_zh.get(plan['action'])

    def guard_zh(self, spec, facts, owner):
        """The self-approval guard, only when it applies: the person whose access changes is an approver."""
        subject = facts['recipient']['slack'].get('id')
        approvers = spec['defaults']['approvers']
        if not subject or subject not in approvers:
            return None
        names = spec['defaults'].get('approver_names_zh') or {}
        if subject == owner:  # the owner is the subject: another approver decides
            others = self.msg('access.guard_zh.1').join(names.get(u, self.msg('access.guard_zh.2')) for u in approvers if u != owner) or self.msg('access.guard_zh.3')
            return self.msg('access.guard_zh.4', others=others)
        return self.msg('access.guard_zh.6', get=names.get(subject, self.msg('access.guard_zh.5')))

    def validate_summary(self, rt, summary):
        """The agent's plain sentence for the card (required with --card)."""
        text = ' '.join(str(summary or '').split())
        if not text:
            raise ValueError(self.msg('access.validate_summary.1', owner=self.name))
        if len(text) > SUMMARY_MAX or not locale.written_for(self.language, text):
            raise ValueError(f'summary_must_be_one_{self.script}_sentence_of_at_most_{SUMMARY_MAX}_characters')
        rt.buttons.check_copy(text, 'summary')
        rt.asks.check_secret_free(text)
        return text

    def card_text(self, spec, plan, facts, result, summary, owner, typed=None, shown=None):
        """the owner-facing card: the agent's sentence, why, reversibility (and the self-approval guard when it applies). P46
        `typed` (`typed_click`): the first line says why the owner's typed instruction still needs a click; the why line leaves
        out what it already says (and an irreversible one leaves out the reversibility line). P47 `shown` (card_gate): the
        situation and checked lines follow the sentence."""
        why = self.msg('access.card_text.1').join(x for x in self.why_zh(result).rstrip(self.msg('access.card_text.2')).split(self.msg('access.card_text.3')) if x not in (typed or ''))
        lines = ([typed] if typed else []) + [summary.rstrip('。.') + self.msg('access.card_text.4')] + list((shown or {}).get('lines') or ())
        lines += [x for x in (why and why + self.msg('access.card_text.5'), None if typed == self.typed_click_zh else self.reversibility_zh(plan, result),
                              self.guard_zh(spec, facts, owner)) if x]
        return '\n'.join(lines)

    def typed_click(self, plan, result):
        """P46.1: the card's first line when the owner's typed instruction cannot approve this plan (None: it can). It can only
        when every reason the engine escalated the plan is a missing fact (TYPED_APPROVAL_SHAPES), or the plan is one_tap
        with no never-auto shape. Deny by default: any other shape, a check that says <owner key>, no rule, the engine off."""
        if irreversible(plan, result):
            return self.typed_click_zh
        why = [self.shape_why.get(s['shape'], self.msg('access.typed_click.1')) for s in result['never_auto']
               if s['shape'] not in TYPED_APPROVAL_SHAPES]
        if result['outcome'] == self.key:
            why += [self.reason_why.get(f['reason']) or self._plain_reason(f.get('zh'))
                    for f in result['failed_checks'] + result['escalations'] if f['outcome'] == self.key]
            why += [self.msg('access.typed_click.2')] * bool(result.get('disabled')) + [self.msg('access.typed_click.3')] * (not result['covered'])
        return self.typed_sensitive_zh.format(why=why[0]) if why else None

    def typed_approval(self, rt, db, value, task_id, now, phash):
        """P46 `--approved-by`: the owner's own hand-typed message in the thread of a card of this check's task, newer than that
        card and younger than rules.typed_approval_max_age_hours, read back from Slack now, that did not already decide a
        card another way (a rejection, or the approval of another plan). Fails closed (approved_by_…) before anything is
        recorded; the message text is never stored."""
        hours = rules_config(rt)['typed_approval_max_age_hours']
        if not hours:
            raise ValueError('approved_by_is_off: rules.typed_approval_max_age_hours is 0; post the card instead')
        value = str(value or '').strip()
        link, short = rt.ABOUT_LINK_RE.fullmatch(value), TYPED_REF_RE.fullmatch(value)
        if not (link or short):
            raise ValueError(f"approved_by_not_recognized: pass the Slack link of {self.name}'s message or CHANNEL/TS")
        channel, ts = (link[1], f'{link[2]}.{link[3]}') if link else (short[1], short[2])
        if not rt.buttons.private_card_destination(rt.buttons.load_config(), channel):
            raise ValueError('approved_by_message_is_not_in_the_decision_channel')
        hinted = re.search(r'thread_ts=(\d{1,10}\.\d{6})', value)  # Slack's "copy link" of a reply carries its thread
        root = hinted[1] if hinted else next((r[0] for r in db.execute(  # else: the thread the runtime routed it from
            'SELECT root FROM card_reply_routes WHERE channel=? AND reply_ts=? UNION ALL SELECT s.root FROM events e JOIN '
            'event_sources s ON s.event_id=e.id WHERE e.channel=? AND e.ts=?', (channel, ts, channel, ts))), None)
        cards = sorted(db.execute(rt.card_replies.CARD_SQL + 'WHERE c.channel=? AND coalesce(c.thread_root, x.thread_ts)=? '
                                  'AND c.dry=0 AND x.thread_ts IS NOT NULL', (channel, root)).fetchall() if root else [],
                       key=lambda r: float(r['thread_ts']), reverse=True)
        if not cards:
            raise ValueError('approved_by_message_is_not_in_a_card_thread')
        card = next((r for r in cards if float(r['thread_ts']) < float(ts)), None)  # the card it answers (P38 F01)
        if card is None:
            raise ValueError('approved_by_message_is_older_than_the_card')
        if not task_id or card['task_id'] != task_id:
            raise ValueError('approved_by_card_is_about_another_task')
        if now - float(ts) > hours * 3600:
            raise ValueError(f'approved_by_message_is_older_than_{hours}_hours: post the card instead')
        found = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
            'channel': channel, 'ts': root, 'oldest': ts, 'latest': ts, 'inclusive': 'true', 'limit': 2,
            'include_all_metadata': 'true'})
        m = next((x for x in found.get('messages', []) if x.get('ts') == ts), None)
        if m is None or not getattr(rt.card_replies, f'{self.key}_typed')(rt, db, channel, m, root):
            raise ValueError(f'approved_by_message_is_not_{self.key}s_own_typed_reply_in_that_card_thread')
        for d in db.execute('SELECT d.choice, c.options, r.plan_hash FROM decisions d JOIN escalation_cards c ON c.event_id='
                            'd.event_id LEFT JOIN rule_checks r ON r.card_event=d.event_id WHERE d.card_channel=? AND '
                            'd.source_ts=?', (channel, ts)):
            if rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))['kind'] != 'approve' or \
                    d['plan_hash'] not in (None, phash):
                raise ValueError('approved_by_message_already_decided_a_card_otherwise: post the card instead')
        return {'ref': rt.card_link(channel, ts, root), 'card': card['event_id'], 'channel': channel, 'root': root}

    def typed_line(self, rt, db, typed, summary, check_id):
        """P46: ONE bot line in the thread of the owner's instruction (no buttons, no broadcast) restating the plan."""
        sent = rt.slack.api_call('chat.postMessage', rt.api_identity(bot=True), json_body={
            'channel': typed['channel'], 'thread_ts': typed['root'], 'unfurl_links': False,
            'text': self.typed_line_text.format(summary=rt.buttons.escape(summary.rstrip('。.'))),
            'metadata': rt.agent_metadata('typed_approval', None, check=check_id)})
        rt.record_send(db, typed['channel'], sent['ts'], 'typed_approval', check_id)
        return sent['ts']

    def _caller(self, rt):
        provider, session = rt.claude.actor()
        return (f'{provider}:{session}' if session else self.key), provider, session

    def next_steps(self, result, approval=None):
        outcome = result['outcome']
        if outcome == 'auto':
            return 'Execute exactly this plan, then rule-outcome --check C --result executed|failed|abandoned.'
        if outcome == 'route':
            return 'Do not grant. Reply in the source thread as `route` says (infra / platform admin), then rule-outcome.'
        if outcome == 'clarify':
            return ('Do not act. Ask what `clarify` lists (requester: ask card; hr: HR; agent: fix the plan), then run '
                    'rule-check again with the completed plan.')
        if approval and approval.get('status') == 'pending':
            return 'Card posted. Wait for the decision event, then rule-status --check C; act only when approved.'
        if approval and approval.get('provenance') == f'{self.key}_typed':
            return (self.msg('access.next_steps.1', owner=self.name))
        return ('Needs approval: rule-check --card --summary \'<one plain Chinese sentence>\' posts the decision card; act '
                'only after rule-status shows approved.')

    def rule_check(self, rt, db, db_path, raw_plan, *, card=False, category='access', now=None, directory=None, facts=None,
                   smoke=False, spec=None, info=None, summary=None, approved_by=None, about=None, gate=None):
        """One recorded check. `facts` (injected) is for smokes and tests only; the CLI never passes it. P3.1: `summary`
        (required with `card`) is the agent's one plain sentence that opens the card the owner reads. P46: `approved_by` (the owner's
        typed message, `typed_approval`) approves a one_tap / <owner key> plan with no card and one line in its thread; a plan it
        cannot approve (`typed_click`) gets its card in that thread. `about` threads the card under the card it continues.
        P47 `gate` (the CLI's card fields, `summary` as the decision line): a card to post passes card_gate and the task's
        open-card limit before anything is recorded."""
        now = time.time() if now is None else now
        if (approved_by or about) and not card:
            raise ValueError('approved_by_and_about_need_--card')
        if card or summary is not None:
            summary = self.validate_summary(rt, summary)  # refused before anything is recorded
        plan = self.normalize_plan(raw_plan)
        if plan['category'] != category:
            raise ValueError('plan_category_differs_from_requested_category')
        rt.asks.check_secret_free(canonical(plan))
        if spec is None:
            spec, info = self.load_active(category, directory)
        facts = facts_for(spec, plan, facts) if facts is not None else self.resolve_facts(rt, db, spec, plan)
        result = self.evaluate(spec, plan, facts, now, None if smoke else rate_counter(db, now))
        config = rules_config(rt)
        if not config['enabled']:
            result.update(outcome=self.key, tier=self.key, approval_required=True, disabled=True)
        check_id = 'rc' + uuid.uuid4().hex[:14]
        phash = plan_hash(plan)
        task_id = plan.get('task') or rt.registry.task_id_for_cwd(os.getcwd())
        caller, provider, session = self._caller(rt)
        own = bool(task_id) and (rt.registry.task_id_for_cwd(os.getcwd()) == task_id or  # the task's own session
                                 rt.registry.running_session(db, task_id) == (provider, session))
        if approved_by and not own:
            raise ValueError('approved_by_needs_the_tasks_own_session: run it in the task workspace')
        typed = self.typed_approval(rt, db, approved_by, task_id, now, phash) if approved_by else None
        click = typed and result['approval_required'] and self.typed_click(plan, result)  # P46.1: the card's first line
        approves = bool(typed and result['approval_required'] and not click)
        if approves and (self.owner_user(rt) not in spec['defaults']['approvers'] or
                         not rt.buttons.authorize(rt.buttons.load_config(), self.owner_user(rt), now, result['outcome'] == self.key)):
            raise ValueError(f'approved_by_{self.key}_is_not_an_approver_here: post the card instead')
        if approves and facts['recipient']['slack'].get('id') == self.owner_user(rt):
            raise ValueError(f'approved_by_{self.key}_is_the_person_whose_access_changes: another approver decides on the card')
        shown = None
        if gate is not None and card and result['approval_required'] and not approves:  # P47: a card will be posted
            shown = rt.card_gate.check(rt, gate, text=f'{summary} {about or ""}')
            _, replaced = rt.continuation(db, about or (typed and typed['card']), task_id, own)
            rt.card_gate.open_limit(rt, db, task_id, replaced and replaced['event_id'], now)
        result['summary_zh'] = self.summary_zh(spec, plan, facts, result, check_id, phash, smoke) + \
            (self.msg('access.rule_check.1', owner=self.name) if result.get('disabled') else '')
        result['card_text'] = self.card_text(spec, plan, facts, result, summary, self.owner_user(rt), click, shown) if summary else None
        if approves and not session:
            raise ValueError('rule_check_card_requires_an_agent_session')

        def uses():  # the plan hashes this message already approved: one message approves one plan
            found = {r[0] for r in db.execute('SELECT plan_hash FROM rule_checks WHERE typed_ref=? AND typed_approved_at '
                                              'IS NOT NULL', (typed['ref'],))} if approves else set()
            if found - {phash}:
                raise ValueError('approved_by_message_already_approved_another_plan: post the card instead')
            return found
        if approves and phash not in uses():  # the restating line is the safety net: no line, no approval
            try:
                typed['line_ts'] = self.typed_line(rt, db, typed, summary, check_id)
            except Exception as exc:
                raise ValueError('approved_by_line_not_posted_so_not_approved: ' + rt.safe_error(exc)) from exc
        with rt.transaction(db):
            uses()  # again inside the write lock (a concurrent check of another plan)
            db.execute('INSERT INTO rule_checks (id,created_at,category,spec_version,spec_digest,plan_hash,plan,kind,action,'
                       'system,task_id,caller,outcome,matched_rules,failed_checks,never_auto,facts,approvers,recipient_slack,'
                       'requester,smoke,summary,typed_ref,typed_approved_at) VALUES '
                       '(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (check_id, now, category, spec['version'], (info or {}).get('digest') or digest(spec), phash,
                        canonical(plan), plan['kind'], plan['action'], plan['system'], task_id, caller, result['outcome'],
                        json.dumps(result['matched_rules']),
                        json.dumps([{k: f[k] for k in ('rule', 'check', 'status', 'reason', 'outcome', 'who')}
                                    for f in result['failed_checks'] + result['escalations']], ensure_ascii=False),
                        json.dumps([{k: s[k] for k in ('shape', 'effective', 'claimed_by')} for s in result['never_auto']]),
                        json.dumps(compact_facts(facts), ensure_ascii=False), json.dumps(spec['defaults']['approvers']),
                        facts['recipient']['slack']['id'], facts['requester'].get('id'), int(bool(smoke)), summary,
                        typed and typed['ref'], now if approves else None))
            if approves:
                rt.registry.audit(db, task_id, 'rule_check_typed_approval', None, {'check': check_id, 'plan_hash': phash,
                                  'message': typed['ref']}, caller, f"approved by {self.name}'s typed instruction", now)
        out = {'ok': True, 'check_id': check_id, 'plan_hash': phash, 'spec': {'category': category, 'version': spec['version'],
                                                                               'digest': (info or {}).get('digest')},
               **{k: result[k] for k in ('outcome', 'tier', 'covered', 'stop', 'matched_rules', 'failed_checks',
                                         'escalations', 'never_auto', 'classes', 'unknown_roles', 'clarify', 'route', 'bounds',
                                         'limits', 'approval_required', 'summary_zh', 'card_text')},
               'facts': compact_facts(facts), 'card': None}
        if result.get('disabled'):
            out['disabled'] = True
        if card and result['approval_required'] and not approves:
            out['card'] = self.post_check_card(rt, db, db_path, check_id, result, plan, now, smoke=smoke,
                                          about=about or (typed and typed['card']), replaces=own)
        if typed:
            out['typed'] = {'ref': typed['ref'], 'approves': approves, 'needs_click': bool(click),
                            'line_ts': typed.get('line_ts')}
        out['approval'] = self.approval_state(rt, db, check_id)
        out['next'] = self.next_steps(result, out['approval'])
        return out

    def post_check_card(self, rt, db, db_path, check_id, result, plan, now, smoke=False, session=None, provider=None,
                        live_card=False, about=None, replaces=False):
        """Decision card for one_tap / <owner key>: the assistant, the owner-only channel, default [批准] [拒绝], bound to this check.
        P3.1: the card is `card_text` (the agent's plain sentence + why + reversibility); technical detail stays on the
        dashboard (/rules?check=…), linked from the footer next to the task name. A smoke card is dry unless `live_card`.
        P46: `about` (the card this one continues) threads it there with reply_broadcast, like ask-<owner key> --about; with
        `replaces` (the task's own session) it takes over a card of the same task that still waits."""
        _, actor_provider, actor_session = self._caller(rt)
        session, provider = session or actor_session, provider or actor_provider
        if not session:
            raise ValueError('rule_check_card_requires_an_agent_session')
        row = db.execute('SELECT * FROM rule_checks WHERE id=?', (check_id,)).fetchone()
        cwd = _task_cwd(rt, row['task_id'])
        question = result.get('card_text')
        if not question:
            raise ValueError('rule_check_card_requires_--summary')
        footer = self.msg('access.post_check_card.1', check_url=check_url(rt, check_id))
        if smoke:
            card = rt.smoke_escalation(db, db_path, session, provider, cwd, question[:480], now=now, footer=footer,
                                       live_card=live_card)
        else:
            card = self.ask_owner(rt)(db, db_path, session, provider, cwd, question[:1500], never_auto=result['outcome'] == self.key,
                               now=now, footer=footer, about=about, replaces=replaces)
        with rt.transaction(db):
            db.execute('UPDATE rule_checks SET card_event=?,card_posted_at=? WHERE id=?', (card['event_id'], now, check_id))
        return {'event_id': card['event_id'], 'channel': card['channel'], 'thread_ts': card['thread_ts'],
                'never_auto': result['outcome'] == self.key, 'test': bool(smoke)}

    def approval_state(self, rt, db, check_id):
        """Approval of a check from SQLite only: the verified card decision, by clicker ID, bound to the plan hash."""
        row = _check_row(db, check_id)
        if row['outcome'] in ('auto', 'route'):
            return {'required': False, 'status': 'not_required'}
        if row['outcome'] == 'clarify':
            return {'required': False, 'status': 'clarify_first'}
        typed_at = row['typed_approved_at'] if 'typed_approved_at' in row.keys() else None
        if typed_at:  # P46: the owner's typed instruction, verified and bound to this plan hash when the check ran
            d = {'actor': self.owner_user(rt), 'role': 'approver', 'decided_at': typed_at, 'provenance': f'{self.key}_typed', 'note': None}
            option = {'id': 'approve', 'kind': 'approve'}
        elif not row['card_event']:
            return {'required': True, 'status': 'not_requested'}
        else:
            d = db.execute('SELECT d.*, c.options FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                           'WHERE d.event_id=?', (row['card_event'],)).fetchone()
            if d is None:
                return {'required': True, 'status': 'pending', 'card_event': row['card_event']}
            option = rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))
        out = {'required': True, 'card_event': row['card_event'], 'actor': d['actor'], 'actor_role': d['role'],
               'decided_at': d['decided_at'], 'provenance': d['provenance'], 'option': option['id'], 'kind': option['kind'],
               'note': d['note'], 'plan_hash': row['plan_hash'],
               **({'source': row['typed_ref'], 'audit': f"approved by {self.name}'s typed instruction: {row['typed_ref']}"}
                  if typed_at else {})}
        if option['kind'] != 'approve':
            out['status'] = 'rejected'
        elif d['actor'] not in json.loads(row['approvers'] or '[]'):
            out['status'] = 'approver_not_in_rules'
        elif row['recipient_slack'] and d['actor'] == row['recipient_slack']:
            out['status'] = 'self_approval_refused'
        elif d['note']:
            out['status'] = 'approved_with_condition'
        else:
            out['status'] = 'approved'
        return out

    def check_record(self, db, check_id, approval, spec=None):
        """Dashboard /rules?check=…: the technical record behind a card (P3.1). `approval` from the caller's read-only
        computation; `spec` (the active rules) only adds rule titles and approver names."""
        row = _check_row(db, check_id)
        defaults = (spec or {}).get('defaults') or {}
        titles = {r['id']: r.get('title_zh') for r in (spec or {}).get('rules') or []}
        names = defaults.get('approver_names_zh') or {}
        card = db.execute('SELECT x.thread_ts, c.channel FROM escalations x JOIN escalation_cards c ON '
                          'c.event_id=x.event_id WHERE x.event_id=?', (row['card_event'],)).fetchone() \
            if row['card_event'] else None
        return {'id': row['id'], 'created_at': row['created_at'], 'summary': row['summary'] if 'summary' in row.keys() else None,
                'category': row['category'], 'spec_version': row['spec_version'], 'spec_digest': row['spec_digest'],
                'task_id': row['task_id'], 'caller': row['caller'], 'smoke': bool(row['smoke']),
                'what': {'system': row['system'], 'system_zh': self.pack.SYSTEM_ZH.get(row['system'], row['system']),
                         'action': row['action'], 'action_zh': self.action_zh.get(row['action'], row['action']),
                         'kind': row['kind'], 'kind_zh': self.kind_zh.get(row['kind'], row['kind'])},
                'outcome': row['outcome'], 'outcome_zh': self.outcome_zh.get(row['outcome'], row['outcome']),
                'matched_rules': [{'id': r, 'title_zh': titles.get(r)} for r in json.loads(row['matched_rules'])],
                'failed_checks': [{**f, 'why_zh': self.reason_why.get(f.get('reason'))}
                                  for f in json.loads(row['failed_checks'] or '[]')],
                'never_auto': [{**s, 'zh': self.shape_why.get(s.get('shape'))} for s in json.loads(row['never_auto'] or '[]')],
                'facts': json.loads(row['facts'] or '{}'),
                'approvers': [{'id': u, 'name': names.get(u)} for u in json.loads(row['approvers'] or '[]')],
                'recipient_slack': row['recipient_slack'], 'requester': row['requester'],
                'plan_hash': row['plan_hash'], 'plan': json.loads(row['plan']), 'approval': approval,
                'result': {'result': row['result'], 'at': row['result_at'], 'by': row['result_by'], 'note': row['result_note']}
                if row['result'] else None,
                'correction': json.loads(row['correction']) if row['correction'] else None,
                'card': {'event': row['card_event'], 'channel': card['channel'], 'ts': card['thread_ts'],
                         'posted_at': row['card_posted_at']} if card else None}

    def proposal_record(self, db, proposal_id, spec=None):
        """Dashboard /rules?proposal=…: a rule proposal with its patch lines (against the active rules when given)."""
        if not PROPOSAL_ID_RE.fullmatch(str(proposal_id or '')):
            raise ValueError('proposal_id_must_look_like_rp + 14 hex')
        row = db.execute('SELECT * FROM rule_proposals WHERE id=?', (proposal_id,)).fetchone()
        if row is None:
            raise ValueError('unknown_rule_proposal')
        p = json.loads(row['proposal'])
        lines = None
        if p['kind'] == 'rule_patch' and spec is not None:
            try:
                lines = self.patch_lines(spec, p['patch'])
            except (ValueError, KeyError, TypeError):
                lines = None
        return {**{k: row[k] for k in ('id', 'kind', 'category', 'title_zh', 'status', 'base_version', 'applied_version',
                                       'created_at', 'decided_at', 'decided_by', 'applied_at', 'error', 'suppress_until',
                                       'card_event', 'proposer')},
                'smoke': bool(row['smoke']), 'rationale_zh': p.get('rationale_zh'), 'text_zh': p.get('text_zh'),
                'patch': p.get('patch'), 'lines': lines, 'evidence': p.get('evidence')}

    def rule_status(self, rt, db, check_id, raw_plan=None):
        row = _check_row(db, check_id)
        approval = self.approval_state(rt, db, check_id)
        out = {'ok': True, 'check_id': check_id, 'created_at': self.iso(row['created_at']), 'outcome': row['outcome'],
               'matched_rules': json.loads(row['matched_rules']), 'plan_hash': row['plan_hash'],
               'plan': json.loads(row['plan']), 'approval': approval, 'executable': executable(row, approval),
               'result': row['result'], 'next': self.status_next.get(approval['status'], '') +
               (f' Audit{" / " + self.tracker.display if self.tracker.display else ""}: {approval["audit"]}'
                if approval.get('audit') else '')}
        if raw_plan is not None:
            out['plan_matches'] = plan_hash(self.normalize_plan(raw_plan)) == row['plan_hash']
            if not out['plan_matches']:
                out['executable'] = False
                out['next'] = 'The plan changed since this check: run rule-check again (a new check, a new approval).'
        return out

    def rule_outcome(self, rt, db, check_id, result, note=None, now=None):
        now = time.time() if now is None else now
        if result not in RESULTS:
            raise ValueError('result_must_be_executed_failed_or_abandoned')
        if note is not None and (len(note) > 300 or any(ord(c) < 32 for c in note)):
            raise ValueError('note_must_be_one_line_of_at_most_300_characters')
        rt.asks.check_secret_free(note)
        row = _check_row(db, check_id)
        if row['result']:
            if row['result'] == result:
                return {'ok': True, 'duplicate': True, 'check_id': check_id, 'result': result}
            raise ValueError('outcome_already_recorded_as_' + row['result'])
        approval = self.approval_state(rt, db, check_id)
        if result == 'executed' and not executable(row, approval):
            raise ValueError(f'not_executable_outcome_{row["outcome"]}_approval_{approval["status"]}')
        caller, _, _ = self._caller(rt)
        with rt.transaction(db):
            db.execute('UPDATE rule_checks SET result=?,result_at=?,result_by=?,result_note=? WHERE id=? AND result IS NULL',
                       (result, now, caller, note, check_id))
        return {'ok': True, 'check_id': check_id, 'result': result, 'outcome': row['outcome'], 'approval': approval['status'],
                **({'audit': approval['audit']} if approval.get('audit') else {})}

    def rule_correct(self, rt, db, check_id, outcome, note=None, now=None):
        """the owner's verdict on what the engine should have said (never an agent session): input for weekly learning."""
        now = time.time() if now is None else now
        if rt._agent_session() or os.environ.get(self.run_id_env):
            raise ValueError(f'only_{self.key}_corrects_rule_checks')
        if outcome not in self.outcomes:
            raise ValueError('outcome_must_be_one_of_' + '_'.join(self.outcomes))
        if note is not None and len(note) > 300:
            raise ValueError('note_must_be_at_most_300_characters')
        rt.asks.check_secret_free(note)
        _check_row(db, check_id)
        with rt.transaction(db):
            db.execute(f"UPDATE rule_checks SET correction=?,corrected_by='{self.key}',corrected_at=? WHERE id=?",
                       (json.dumps({'outcome': outcome, 'note': note}, ensure_ascii=False), now, check_id))
        return {'ok': True, 'check_id': check_id, 'correction': {'outcome': outcome, 'note': note}}

    def rules_show(self, category='access', rule_id=None, directory=None):
        spec, info = self.load_active(category, directory)
        if rule_id:
            rule = next((r for r in spec['rules'] if r['id'] == rule_id), None)
            if rule is None:
                raise ValueError('unknown_rule_id')
            return {'ok': True, **info, 'rule': rule}
        levels = {}
        for rule in spec['rules']:
            levels[rule['level']] = levels.get(rule['level'], 0) + 1
        return {'ok': True, **info, 'category': category, 'rules_total': len(spec['rules']), 'levels': levels,
                'approvers': spec['defaults']['approvers'], 'spend': spec['defaults']['spend'],
                'temporary_days': spec['defaults']['temporary_days'],
                'never_auto': {k: v['level'] for k, v in spec['never_auto'].items()},
                'rules': [{'id': r['id'], 'name': r['name'], 'title_zh': r['title_zh'], 'kind': r['kind'], 'level': r['level'],
                           'trigger': _trigger_text(r['trigger']), 'checks': [c['name'] for c in r['checks']],
                           'claims': r.get('claims') or {}, 'fallback': r['fallback']} for r in spec['rules']],
                'changelog': read_changelog(info['dir'], 10)}

    def rules_validate(self, path=None, directory=None):
        if path:
            try:
                spec = _read_private_json(path)
            except ValueError as exc:
                return {'ok': False, 'file': str(path), 'problems': [str(exc)]}
            problems = self.spec_problems(spec)
            return {'ok': not problems, 'file': str(path), 'problems': problems,
                    'version': spec.get('version') if isinstance(spec, dict) else None}
        directory = self.rules_dir(directory)
        out = {'ok': True, 'dir': str(directory), 'categories': {}}
        try:
            active = self.read_active(directory)
        except ValueError as exc:
            return {'ok': False, 'dir': str(directory), 'problems': [str(exc)]}
        for category, name in active['categories'].items():
            try:
                spec, info = self.load_active(category, directory)
                out['categories'][category] = {'ok': True, **info}
            except ValueError as exc:
                out['ok'] = False
                out['categories'][category] = {'ok': False, 'file': name, 'problems': [str(exc)]}
        return out

    def rules_diff(self, category='access', from_ref=None, to_ref=None, directory=None):
        """`vN` refers to <category>-vN.json in the rules dir, anything else is a file path; defaults: previous -> active."""
        directory = self.rules_dir(directory)
        active, info = self.load_active(category, directory)

        def load(ref, fallback):
            if ref is None:
                return fallback
            match = re.fullmatch(r'v(\d{1,6})', str(ref))
            return self.load_spec_file(directory / f'{category}-v{match[1]}.json') if match else self.validate_spec(_read_private_json(ref))
        new = load(to_ref, active)
        if from_ref is None and new['version'] <= 1:
            return {'ok': True, 'from': None, 'to': new['version'], 'changes': []}
        old = load(from_ref if from_ref is not None else f'v{new["version"] - 1}', None)
        return {'ok': True, 'from': old['version'], 'to': new['version'], 'changes': spec_diff(old, new)}

    def backtest(self, spec, cases_doc, now=None, baseline=None, tolerance=0.05):
        """Run every case through the engine with its recorded facts; compare tiers with the case labels.

        Label tiers: auto -> auto, assist -> one_tap, <owner key> / uncovered -> <owner key>. Engine tiers: auto, clarify and route
        need no owner touch (auto tier, as in the v1 estimate where AR-06 replies and HR questions count as auto).
        Returns (aggregate, per-case details); the details are private (they carry case ids and rule hits).
        """
        now = time.time() if now is None else now
        problems = schema_errors(cases_doc, {'type': 'object', 'required': ['schema', 'cases'],
                                             'properties': {'schema': {'const': self.backtest_schema_id},
                                                            'cases': {'type': 'array', 'minItems': 1}}})
        if problems:
            raise ValueError('invalid_backtest_cases: ' + '; '.join(problems[:3]))
        label_tier = {'auto': 'auto', 'assist': 'one_tap', self.key: self.key, 'uncovered': self.key}
        details, outcomes, shapes, rules_hit = [], {}, {}, {}
        confusion = {lt: {et: 0 for et in ('auto', 'one_tap', self.key)} for lt in ('auto', 'one_tap', self.key)}
        labels = {}
        for case in cases_doc['cases']:
            label = case['label']
            labels[label] = labels.get(label, 0) + 1
            evaluated = []
            for item in case['plans']:
                plan = self.normalize_plan(item['plan'])
                result = self.evaluate(spec, plan, facts_for(spec, plan, item.get('facts') or {}), now)
                evaluated.append(result)
            tier = self.strictest([r['outcome'] for r in evaluated])
            engine_tier = self.tier[tier]
            confusion[label_tier[label]][engine_tier] += 1
            outcomes[tier] = outcomes.get(tier, 0) + 1
            for r in evaluated:
                for s in r['never_auto']:
                    shapes[s['shape']] = shapes.get(s['shape'], 0) + 1
                for rid in r['matched_rules']:
                    rules_hit[rid] = rules_hit.get(rid, 0) + 1
            details.append({'id': case['id'], 'domain': case.get('domain'), 'label': label, 'label_tier': label_tier[label],
                            'engine_outcome': tier, 'engine_tier': engine_tier, 'agree': engine_tier == label_tier[label],
                            'plans': [{'outcome': r['outcome'], 'matched_rules': r['matched_rules'],
                                       'failed': [f'{f["rule"]}:{f["check"]}:{f["reason"]}' for f in r['failed_checks']],
                                       'escalations': [f'{f["rule"]}:{f["reason"]}' for f in r['escalations']],
                                       'never_auto': [f'{s["shape"]}->{s["effective"]}' for s in r['never_auto']],
                                       'covered': r['covered']} for r in evaluated]})
        total = len(details)
        auto = sum(1 for d in details if d['engine_tier'] == 'auto')
        one_tap = sum(1 for d in details if d['engine_tier'] == 'one_tap')
        v1 = baseline or {'auto': labels.get('auto', 0), 'assist': labels.get('assist', 0)}
        v1_auto, v1_both = _share(v1['auto'], total), _share(v1['auto'] + v1['assist'], total)
        engine_auto, engine_both = _share(auto, total), _share(auto + one_tap, total)
        by_domain = {}
        for d in details:
            bucket = by_domain.setdefault(d.get('domain') or '-', {'cases': 0, 'auto': 0, 'one_tap': 0, self.key: 0, 'agree': 0})
            bucket['cases'] += 1
            bucket[d['engine_tier']] += 1
            bucket['agree'] += int(d['agree'])
        aggregate = {
            'schema': f'{self.worker_slug}.rules-backtest-summary/v1', 'generated_at': self.iso(now),
            'spec': {'category': spec['category'], 'version': spec['version'], 'digest': digest(spec),
                     'rules': len(spec['rules'])},
            'cases': total, 'labels': labels, 'engine_outcomes': outcomes,
            'engine_tiers': {'auto': auto, 'one_tap': one_tap, self.key: total - auto - one_tap},
            'shares': {'v1_auto': v1_auto, 'v1_auto_or_one_tap': v1_both, 'engine_auto': engine_auto,
                       'engine_auto_or_one_tap': engine_both,
                       'delta_auto_pp': round((engine_auto - v1_auto) * 100, 2),
                       'delta_auto_or_one_tap_pp': round((engine_both - v1_both) * 100, 2)},
            'tolerance_pp': tolerance * 100,
            'pass': {'auto': abs(engine_auto - v1_auto) <= tolerance + 1e-9,
                     'auto_or_one_tap': abs(engine_both - v1_both) <= tolerance + 1e-9},
            'agreement': {'cases': sum(d['agree'] for d in details), 'share': _share(sum(d['agree'] for d in details), total)},
            'confusion_label_by_engine': confusion, 'by_domain': by_domain, 'never_auto_shapes': shapes,
            'rule_hits': dict(sorted(rules_hit.items())),
            'tier_mapping': {'labels': label_tier, 'engine': self.tier}}
        aggregate['pass']['overall'] = aggregate['pass']['auto'] and aggregate['pass']['auto_or_one_tap']
        return aggregate, details

    def backtest_cli(self, rt, db, cases_path, spec_path=None, summary_out=None, details_out=None, record=False, now=None):
        now = time.time() if now is None else now
        if spec_path:
            spec = self.validate_spec(json.loads(Path(spec_path).read_text(encoding='utf-8')))
        else:
            spec, _ = self.load_active('access')
        try:
            cases = json.loads(Path(cases_path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('backtest_cases_unreadable_or_not_json') from None
        aggregate, details = self.backtest(spec, cases, now)
        if summary_out:
            write_private_json(summary_out, aggregate)
        if details_out:
            write_private_json(details_out, {'schema': f'{self.worker_slug}.rules-backtest-details/v1', 'private': True,
                                             'generated_at': aggregate['generated_at'], 'cases': details})
        if record:
            with rt.transaction(db):
                db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (BACKTEST_META_KEY, canonical(aggregate)))
        return {'ok': True, **aggregate, 'summary_out': summary_out, 'details_out': details_out, 'recorded': bool(record)}

    def review_data(self, rt, db, since='7d', now=None, category='access'):
        """Aggregates and ids for the weekly review. Never message text, never free-text notes."""
        now = time.time() if now is None else now
        start = rt.registry.parse_since(since, now) or (now - 7 * 86400)
        checks = [dict(r) for r in db.execute('SELECT * FROM rule_checks WHERE created_at>=? AND smoke=0 ORDER BY created_at',
                                              (start,))]
        by_outcome, by_rule, failed, shapes, results = {}, {}, {}, {}, {'executed': 0, 'failed': 0, 'abandoned': 0, 'open': 0}
        cards, overrides, latencies = {'cards': 0, 'approved': 0, 'rejected': 0, 'with_condition': 0, 'pending': 0,
                                       'self_refused': 0, 'typed_approved': 0}, [], []  # P46: by typed instruction
        for row in checks:
            by_outcome[row['outcome']] = by_outcome.get(row['outcome'], 0) + 1
            for rid in json.loads(row['matched_rules']):
                bucket = by_rule.setdefault(rid, {o: 0 for o in self.outcomes})
                bucket[row['outcome']] += 1
            for f in json.loads(row['failed_checks'] or '[]'):
                key = f'{f["rule"]}:{f["check"]}:{f["reason"]}'
                item = failed.setdefault(key, {'rule': f['rule'], 'check': f['check'], 'reason': f['reason'],
                                               'status': f['status'], 'count': 0, 'check_ids': []})
                item['count'] += 1
                if len(item['check_ids']) < 20:
                    item['check_ids'].append(row['id'])
            for s in json.loads(row['never_auto'] or '[]'):
                shapes[s['shape']] = shapes.get(s['shape'], 0) + 1
            results[row['result'] or 'open'] += 1
            cards['typed_approved'] += bool(row.get('typed_approved_at'))
            if row['card_event']:
                cards['cards'] += 1
                d = _decision(rt, db, row['card_event'])
                if d is None:
                    cards['pending'] += 1
                else:
                    latencies.append(d['decided_at'] - (row['card_posted_at'] or row['created_at']))
                    if d['kind'] != 'approve':
                        cards['rejected'] += 1
                        overrides.append({'check': row['id'], 'rules': json.loads(row['matched_rules']), 'engine': row['outcome'],
                                          self.key: 'rejected', 'at': self.iso(d['decided_at'])})
                    elif row['recipient_slack'] and d['actor'] == row['recipient_slack']:
                        cards['self_refused'] += 1
                    elif d['note']:
                        cards['with_condition'] += 1
                    else:
                        cards['approved'] += 1
            if row['correction']:
                corrected = json.loads(row['correction'])
                overrides.append({'check': row['id'], 'rules': json.loads(row['matched_rules']), 'engine': row['outcome'],
                                  self.key: 'corrected:' + corrected.get('outcome', '?'), 'at': self.iso(row['corrected_at'])})
        triage = {'total': 0, 'by_disposition': {}, 'by_intent': {}, f'needs_{self.key}': 0, 'corrections': [], 'basis_rules': {}}
        for r in db.execute('SELECT * FROM triage_records WHERE (created_at>=? OR corrected_at>=?) AND smoke=0',
                            (start, start)):
            if r['created_at'] >= start:
                triage['total'] += 1
                triage['by_disposition'][r['disposition']] = triage['by_disposition'].get(r['disposition'], 0) + 1
                triage['by_intent'][r['intent']] = triage['by_intent'].get(r['intent'], 0) + 1
                triage[f'needs_{self.key}'] += int(bool(r[f'needs_{self.key}']))
                for rid in self.pack.AR_RE.findall(r['basis'] or ''):
                    triage['basis_rules'][rid] = triage['basis_rules'].get(rid, 0) + 1
            if r['correction']:
                corrected = json.loads(r['correction'])
                triage['corrections'].append({'record': r['id'], 'from': r['disposition'], 'to': corrected.get('disposition'),
                                              'basis_rules': self.pack.AR_RE.findall(r['basis'] or ''), 'at': self.iso(r['corrected_at'])})
        special = {row[0] for row in db.execute('SELECT card_event FROM rule_checks WHERE card_event IS NOT NULL UNION '
                                                'SELECT card_event FROM rule_proposals WHERE card_event IS NOT NULL UNION '
                                                'SELECT card_event FROM intake_briefs')}
        try:
            special |= {row[0] for row in db.execute('SELECT card_event FROM routine_proposals WHERE card_event IS NOT NULL')}
        except Exception:  # noqa: BLE001 - routines table may not exist in an old database
            pass
        try:  # P3.1 blocker cards (both stages) and skill proposal cards are not questions to the owner
            special |= {row[0] for row in db.execute(
                'SELECT card_event FROM automation_gaps WHERE card_event IS NOT NULL UNION SELECT stage2_event FROM '
                'automation_gaps WHERE stage2_event IS NOT NULL UNION SELECT card_event FROM gap_proposals WHERE '
                'card_event IS NOT NULL')}
        except Exception:  # noqa: BLE001
            pass
        asks = {'cards': 0, 'decided': 0, 'by_kind': {}, 'latency_seconds': None}
        ask_latency = []
        for row in db.execute(f"SELECT c.event_id, c.created FROM escalation_cards c JOIN events e ON e.id=c.event_id WHERE e.disposition='ask_{self.key}' AND c.created>=? AND c.smoke=0", (start,)):
            if row['event_id'] in special:
                continue
            asks['cards'] += 1
            d = _decision(rt, db, row['event_id'])
            if d:
                asks['decided'] += 1
                asks['by_kind'][d['kind']] = asks['by_kind'].get(d['kind'], 0) + 1
                ask_latency.append(d['decided_at'] - row['created'])
        asks['latency_seconds'] = {'median': _pct(ask_latency, 0.5), 'p90': _pct(ask_latency, 0.9), 'n': len(ask_latency)}
        proposals = {'pending': [r[0] for r in db.execute("SELECT id FROM rule_proposals WHERE status='pending' AND smoke=0")],
                     'suppressed': [{'fingerprint': r['fingerprint'], 'until': self.iso(r['suppress_until'])} for r in db.execute(
                         "SELECT fingerprint, max(suppress_until) AS suppress_until FROM rule_proposals WHERE status='rejected' "
                         'AND suppress_until>? AND smoke=0 GROUP BY fingerprint', (now,))],
                     'recent': [{k: r[k] for k in ('id', 'kind', 'status', 'applied_version')} | {'created_at': self.iso(r['created_at'])}
                                for r in db.execute('SELECT * FROM rule_proposals WHERE smoke=0 ORDER BY created_at DESC LIMIT 20')]}
        try:
            spec, info = self.load_active(category)
            active = {category: {'version': info['version'], 'digest': info['digest'], 'rules': len(spec['rules'])}}
        except ValueError as exc:
            active = {category: {'error': str(exc)}}
        config = rules_config(rt)
        gap_config = rt.blockers.config(rt)
        return {'ok': True, 'since': self.iso(start), 'until': self.iso(now), 'active': active,
                'rule_checks': {'total': len(checks), 'by_outcome': by_outcome, 'by_rule': by_rule, 'results': results,
                                'cards': cards, 'approval_latency_seconds': {'median': _pct(latencies, 0.5),
                                                                             'p90': _pct(latencies, 0.9), 'n': len(latencies)},
                                'overrides': overrides, 'failed_checks': sorted(failed.values(), key=lambda x: -x['count']),
                                'never_auto': shapes},
                'triage': triage, f'ask_{self.key}': asks, 'proposals': proposals,
                # P3.1: capability blockers / automation gaps (labels and ids only) for the weekly skill proposals.
                'gaps': rt.blockers.review_aggregate(db, start, now),
                # P35: same-shape volume patterns and [只记这次] items mined from the interaction records (aggregates + ids
                # only, never raw feedback) to consolidate into proposals through the same weekly cards.
                'interactions': _interactions_review(rt, db, start, now),
                'limits': {'max_proposals': config['max_proposals_per_run'], 'suppress_days': config['proposal_suppress_days'],
                           'max_skill_proposals': gap_config['max_skill_proposals_per_run'],
                           'skill_suppress_days': gap_config['skill_proposal_suppress_days']}}

    def apply_patch(self, spec, ops):
        """Apply id-keyed patch ops (replace | add | remove) to a copy of the spec; the result is validated."""
        out = copy.deepcopy(spec)
        for op in ops:
            segs = _segments(op['path'])
            if not segs or segs[0] not in PATCH_ROOTS:
                raise ValueError('patch_path_must_start_with_' + '_'.join(PATCH_ROOTS))
            parent = out
            for seg in segs[:-1]:
                parent = _child(parent, seg)
            last = segs[-1]
            if op['op'] in ('replace', 'add') and 'value' not in op:
                raise ValueError('patch_value_missing')
            if isinstance(parent, dict):
                if op['op'] == 'add' and last in parent:
                    raise ValueError('patch_add_target_exists:' + last)
                if op['op'] in ('replace', 'remove') and last not in parent:
                    raise ValueError('patch_path_not_found:' + last)
                if op['op'] == 'remove':
                    del parent[last]
                else:
                    parent[last] = copy.deepcopy(op['value'])
            elif isinstance(parent, list):
                keyed = [n for n, item in enumerate(parent) if isinstance(item, dict) and last in (item.get('id'), item.get('name'))]
                if op['op'] == 'add':
                    if last != '-':
                        raise ValueError('patch_list_add_uses_-')
                    parent.append(copy.deepcopy(op['value']))
                elif op['op'] == 'remove':
                    if keyed:
                        del parent[keyed[0]]
                    elif last in parent:
                        parent.remove(last)
                    else:
                        raise ValueError('patch_path_not_found:' + last)
                else:
                    if not keyed:
                        raise ValueError('patch_path_not_found:' + last)
                    parent[keyed[0]] = copy.deepcopy(op['value'])
            else:
                raise ValueError('patch_parent_is_not_a_container')
        return self.validate_spec(out)

    def patch_lines(self, spec, ops):
        lines = []
        for op in ops:
            segs = _segments(op['path'])
            try:
                node = spec
                for seg in segs:
                    node = _child(node, seg)
                old = canonical(node)
            except ValueError:
                old = self.msg('access.patch_lines.1')
            where = '/'.join(segs[1:]) if segs and segs[0] == 'rules' else '/'.join(segs)
            new = self.msg('access.patch_lines.2') if op['op'] == 'remove' else canonical(op.get('value'))
            lines.append(self.msg('access.patch_lines.3', where=where, value=old[:120], value2=new[:120]))
        return lines

    def prepare_proposals(self, rt, db, raw, now=None, directory=None, goal_path=None, smoke=False):
        """Validate a proposals file. Returns (accepted, skipped); nothing is written."""
        now = time.time() if now is None else now
        config = rules_config(rt)
        try:
            doc = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except ValueError:
            raise ValueError('proposals_file_is_not_valid_json') from None
        problems = schema_errors(doc, self.proposals_schema)
        if problems:
            raise ValueError('invalid_proposals: ' + '; '.join(problems[:5]))
        if len(doc['proposals']) > config['max_proposals_per_run']:
            raise ValueError(f'at_most_{config["max_proposals_per_run"]}_proposals_per_run')
        pending = db.execute("SELECT count(*) FROM rule_proposals WHERE status='pending' AND smoke=?", (int(smoke),)).fetchone()[0]
        if pending + len(doc['proposals']) > MAX_PENDING:
            raise ValueError(f'too_many_pending_proposals_wait_for_{self.key}')
        accepted, skipped = [], []
        for n, p in enumerate(doc['proposals']):
            rt.asks.check_secret_free(p['title_zh'], p['rationale_zh'], p.get('text_zh'))
            if not locale.written_for(self.language, p['title_zh']) or not locale.written_for(self.language, p['rationale_zh']):
                raise ValueError(f'proposal_{n}_title_and_rationale_must_be_{self.script}')
            # P3.1 copy standard: the owner reads these on the card (describe rules in words: "BigQuery 只读规则", not ids).
            for field in ('title_zh', 'rationale_zh', 'text_zh'):
                if p.get(field):
                    rt.buttons.check_copy(p[field], f'proposal_{n}_{field}', first_line_max=None)
            fp = fingerprint(p)
            suppressed = db.execute("SELECT max(suppress_until) FROM rule_proposals WHERE fingerprint=? AND status='rejected' "
                                    'AND suppress_until>?', (fp, now)).fetchone()[0]
            if suppressed:
                skipped.append({'index': n, 'fingerprint': fp, 'reason': 'rejected_recently', 'suppressed_until': self.iso(suppressed)})
                continue
            if db.execute("SELECT 1 FROM rule_proposals WHERE fingerprint=? AND status='pending'", (fp,)).fetchone():
                skipped.append({'index': n, 'fingerprint': fp, 'reason': 'already_pending'})
                continue
            if p['kind'] == 'rule_patch':
                if not p.get('patch') or p.get('text_zh'):
                    raise ValueError(f'proposal_{n}_rule_patch_needs_patch_and_no_text_zh')
                category = p.get('category', 'access')
                spec, info = self.load_active(category, directory)
                if p.get('base_version') not in (None, spec['version']):
                    raise ValueError(f'proposal_{n}_base_version_{p["base_version"]}_is_not_active_v{spec["version"]}')
                try:
                    new = self.apply_patch(spec, p['patch'])
                except ValueError as exc:
                    raise ValueError(f'proposal_{n}_patch_not_applicable: {exc}') from None
                if digest(new) == digest(spec):
                    raise ValueError(f'proposal_{n}_patch_changes_nothing')
                accepted.append({'index': n, 'proposal': {**p, 'category': category, 'base_version': spec['version']},
                                 'fingerprint': fp, 'target': info['dir'], 'lines': self.patch_lines(spec, p['patch']),
                                 'from_version': spec['version']})
            else:
                if p.get('patch') or not p.get('text_zh') or not locale.written_for(self.language, p['text_zh']) \
                        or '\n' in p['text_zh'].strip():
                    raise ValueError(f'proposal_{n}_inbox_addendum_needs_one_line_of_{self.script}_text_zh_and_no_patch')
                accepted.append({'index': n, 'proposal': p, 'fingerprint': fp,
                                 'target': str(goal_path or inbox_goal_path(rt)), 'lines': [p['text_zh']]})
        return accepted, skipped

    def proposal_card_text(self, item, proposal_id=None, smoke=False):
        """the owner-facing (P3.1 copy standard): what changes, why, what the buttons do. The patch lines, the evidence and the
        versions are on the dashboard (/rules?proposal=…), linked from the card footer."""
        p = item['proposal']
        test = '🧪 TEST · ' if smoke else ''
        if p['kind'] == 'rule_patch':
            return '\n'.join([self.msg('access.proposal_card_text.1', test=test, short=_short(p['title_zh'], 80)),
                              _short(p['rationale_zh'], 120),
                              self.msg('access.proposal_card_text.2')])
        return '\n'.join([self.msg('access.proposal_card_text.3', test=test, short=_short(p['title_zh'], 80)),
                          self.msg('access.proposal_card_text.4', strip=p['text_zh'].strip()),
                          _short(p['rationale_zh'], 120)])

    def propose(self, rt, db, db_path, raw, now=None, directory=None, goal_path=None, smoke=False, session=None, provider=None,
                live_card=False):
        """rule-propose: validate, then one decision card per proposal [采纳] [不采纳]. The agent never applies anything.
        A smoke proposal card is dry unless `live_card`."""
        now = time.time() if now is None else now
        if not rules_config(rt)['proposals_enabled']:
            raise ValueError('rule_proposals_disabled_rules.proposals_enabled_false')
        caller, actor_provider, actor_session = self._caller(rt)
        session, provider = session or actor_session, provider or actor_provider
        if not session:
            raise ValueError('rule_propose_requires_an_agent_session')
        accepted, skipped = self.prepare_proposals(rt, db, raw, now, directory, goal_path, smoke)
        posted = []
        for item in accepted:
            pid = 'rp' + uuid.uuid4().hex[:14]
            p = item['proposal']
            with rt.transaction(db):
                db.execute('INSERT INTO rule_proposals (id,kind,category,fingerprint,title_zh,proposal,base_version,target,proposer,'
                           'status,created_at,smoke) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (pid, p['kind'], p.get('category'), item['fingerprint'], p['title_zh'], canonical(p),
                            p.get('base_version'), item['target'], caller, 'posting', now, int(bool(smoke))))
            text = self.proposal_card_text(item, pid, smoke)
            cwd = os.getcwd() if not Path(item['target']).name == 'goal.md' else str(Path(item['target']).parent)
            try:
                if smoke:
                    card = rt.smoke_escalation(db, db_path, session, provider, cwd if Path(cwd).is_dir() else os.getcwd(),
                                               text[:480], now=now, options=self.proposal_options,
                                               footer=self.msg('access.propose.1', dashboard_url=rt.DASHBOARD_URL, pid=pid), live_card=live_card)
                else:
                    card = self.ask_owner(rt)(db, db_path, session, provider, cwd if Path(cwd).is_dir() else os.getcwd(), text,
                                       self.proposal_options, never_auto=True, now=now,
                                       footer=self.msg('access.propose.2', dashboard_url=rt.DASHBOARD_URL, pid=pid))
            except Exception as exc:
                with rt.transaction(db):
                    db.execute("UPDATE rule_proposals SET status='failed',error=? WHERE id=?", (str(exc)[:200], pid))
                raise
            with rt.transaction(db):
                db.execute("UPDATE rule_proposals SET status='pending',card_event=? WHERE id=?", (card['event_id'], pid))
            posted.append({'proposal_id': pid, 'kind': p['kind'], 'fingerprint': item['fingerprint'],
                           'card_event': card['event_id'], 'card_ts': card['thread_ts'], 'lines': item['lines']})
        return {'ok': True, 'posted': posted, 'skipped': skipped,
                'next': ('Resolve the routine event as decision. The supervisor applies an adopted proposal itself (code, '
                         'never you); a typed modification means: rule-propose a revised proposal (a new card).')}

    def write_addendum(self, rt, goal_path, line, now):
        """Insert one line into the code-managed addenda block of the <owner key>-inbox goal.md (backup first, atomic)."""
        goal_path = Path(goal_path)
        begin, end = rt.intake.ADDENDA_BEGIN, rt.intake.ADDENDA_END
        text = goal_path.read_text(encoding='utf-8') if goal_path.exists() else ''
        if goal_path.exists():
            backup = goal_path.parent / 'history' / 'goal-md-backups' / f'{dt.datetime.fromtimestamp(now, dt.timezone.utc):%Y%m%dT%H%M%SZ}-goal.md'
            backup.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(text)
        if begin in text and end in text and text.index(begin) < text.index(end):
            head, rest = text.split(begin, 1)
            body, tail = rest.split(end, 1)
            body = body.rstrip('\n') + '\n' + line + '\n'
            new = head + begin + body + end + tail
        else:
            new = text.rstrip('\n') + f'\n\n{self.addenda_heading}\n\n{begin}\n{line}\n{end}\n'
        mode = goal_path.stat().st_mode & 0o777 if goal_path.exists() else 0o600
        fd, tmp = tempfile.mkstemp(prefix='.goal.', dir=str(goal_path.parent))
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(new)
        os.chmod(tmp, mode)
        os.replace(tmp, goal_path)
        return goal_path

    def _allowed_target(self, rt, row):
        target = Path(row['target']).expanduser()
        if row['smoke']:
            return str(target).startswith(('/tmp/' + self.smoke_dir_prefix, '/private/tmp/' + self.smoke_dir_prefix,
                                           str(Path(tempfile.gettempdir()) / self.smoke_dir_prefix)))
        if row['kind'] == 'rule_patch':
            return target.resolve() == self.rules_dir().resolve()
        return target.resolve() == inbox_goal_path(rt).resolve()

    def apply_proposal(self, rt, db, row, actor, now):
        """Code applies an adopted proposal: rule patch -> new version + changelog + switch; addendum -> goal.md block."""
        p = json.loads(row['proposal'])
        if not self._allowed_target(rt, row):
            raise ValueError('proposal_target_not_allowed')
        if p['kind'] == 'inbox_addendum':
            line = self.msg('access.apply_proposal.1', text_zh=p['text_zh'], isoformat=self.local_date(now).isoformat(), id=row['id'])
            self.write_addendum(rt, row['target'], line, now)
            return {'status': 'applied', 'version': None}
        spec, info = self.load_active(p['category'], row['target'])
        new = self.apply_patch(spec, p['patch'])  # re-applied on the CURRENT active version, then validated
        new['version'] = spec['version'] + 1
        new['revision'] = {'from_version': spec['version'], 'proposal': row['id'], 'at': self.iso(now),
                           **({'approved_by': actor} if re.fullmatch(USER_PAT, str(actor)) else {})}
        self.validate_spec(new)
        self.install_spec(new, row['target'], who=f'proposal {row["id"]} adopted by {actor}', note=p['title_zh'], now=now)
        append_changelog(row['target'], {'at': self.iso(now), 'ts': now, 'kind': 'proposal_applied', 'proposal': row['id'],
                                         'fingerprint': row['fingerprint'], 'category': p['category'],
                                         'from_version': spec['version'], 'to_version': new['version'], 'approved_by': actor,
                                         'patch': p['patch'], 'title_zh': p['title_zh']})
        return {'status': 'applied', 'version': new['version']}

    def settle_proposals(self, rt, db, now=None):
        """Supervisor step: decided proposal cards -> applied / rejected (suppressed) / needs_revision / conflict."""
        now = time.time() if now is None else now
        config = rules_config(rt)
        done = []
        for row in db.execute("SELECT * FROM rule_proposals WHERE status='pending' AND card_event IS NOT NULL").fetchall():
            d = _decision(rt, db, row['card_event'])
            if d is None:
                continue
            status, version, error = None, None, None
            if d['kind'] != 'approve':
                status = 'rejected'
            elif d['note']:
                status = 'needs_revision'
            elif not config['proposals_enabled'] and not row['smoke']:
                continue  # kill switch: decided cards wait until proposals are enabled again
            else:
                try:
                    applied = self.apply_proposal(rt, db, row, d['actor'], now)
                    status, version = applied['status'], applied['version']
                except ValueError as exc:
                    status, error = 'conflict', str(exc)[:300]
            with rt.transaction(db):
                db.execute('UPDATE rule_proposals SET status=?,decided_by=?,decided_at=?,applied_at=?,applied_version=?,'
                           "suppress_until=?,error=? WHERE id=? AND status='pending'",
                           (status, d['actor'], d['decided_at'], now if status == 'applied' else None, version,
                            now + config['proposal_suppress_days'] * 86400 if status == 'rejected' else None, error, row['id']))
            reply = {'applied': (self.msg('access.settle_proposals.1', version=version) if version
                                 else self.msg('access.settle_proposals.2', owner_key=self.key)),
                     'rejected': self.msg('access.settle_proposals.3', proposal_suppress_days=config['proposal_suppress_days']),
                     'needs_revision': self.msg('access.settle_proposals.needs_revision'),
                     'conflict': self.msg('access.settle_proposals.4') + (error or '') + self.msg('access.settle_proposals.5')}[status]
            _card_reply(rt, db, row['card_event'], ('🧪 TEST · ' if row['smoke'] else '') + reply, row['id'])
            done.append({'proposal_id': row['id'], 'status': status, 'version': version, 'error': error})
        return done

    def list_proposals(self, db, status=None, limit=50):
        sql = 'SELECT * FROM rule_proposals' + (' WHERE status=?' if status else '') + ' ORDER BY created_at DESC LIMIT ?'
        rows = db.execute(sql, ((status, limit) if status else (limit,))).fetchall()
        return {'ok': True, 'proposals': [{k: r[k] for k in ('id', 'kind', 'category', 'fingerprint', 'title_zh', 'status',
                                                              'base_version', 'applied_version', 'card_event', 'proposer',
                                                              'decided_by', 'error', 'smoke')} |
                                          {'created_at': self.iso(r['created_at']),
                                           'suppress_until': self.iso(r['suppress_until']) if r['suppress_until'] else None}
                                          for r in rows]}

    def inbox_sync(self, rt, db_path, goal_path=None, now=None):
        """Idempotent: ensure the code-managed P3 policy and addenda blocks and the P3.1 capability-blocker and card-copy
        blocks exist in the <owner key>-inbox goal.md, and the two P3.1 blocks in the <owner key>-oncall goal.md (when that task exists)."""
        now = time.time() if now is None else now
        goal_path = Path(goal_path or inbox_goal_path(rt))
        if not goal_path.exists():
            raise ValueError(f'{self.key}_inbox_goal_md_missing_run_intake-bootstrap_first')
        cli = f'python3 {Path(rt.__file__).resolve()} --db {db_path}'
        text = goal_path.read_text(encoding='utf-8')
        new = _replace_block(text, rt.intake.rules_policy_block(cli), rt.intake.POLICY_BEGIN, rt.intake.POLICY_END)
        if rt.intake.ADDENDA_BEGIN not in new:
            new = new.rstrip('\n') + f'\n\n{self.addenda_heading}\n\n{rt.intake.ADDENDA_BEGIN}\n{rt.intake.ADDENDA_END}\n'
        blockers_block = rt.intake.blockers_policy_block(cli)
        new = _replace_block(new, blockers_block, rt.intake.BLOCKERS_BEGIN, rt.intake.BLOCKERS_END,
                             after=rt.intake.POLICY_END)
        cards_block = rt.intake.cards_policy_block(cli)  # P3.1 cards: the copy standard and thread-reply decisions
        new = _replace_block(new, cards_block, rt.intake.CARDS_BEGIN, rt.intake.CARDS_END, after=rt.intake.BLOCKERS_END)
        interaction_block = rt.intake.interaction_policy_block(cli)  # P3.3: reconcile-before-acting + two-plane emoji
        new = _replace_block(new, interaction_block, rt.intake.INTERACTION_BEGIN, rt.intake.INTERACTION_END,
                             after=rt.intake.CARDS_END)
        lifecycle_block = rt.intake.lifecycle_policy_block(cli)  # P34: closure contract + ticket discipline + handoff
        new = _replace_block(new, lifecycle_block, rt.intake.LIFECYCLE_BEGIN, rt.intake.LIFECYCLE_END,
                             after=rt.intake.INTERACTION_END)
        feedback_block = rt.intake.feedback_policy_block(cli)  # P35: capture, distill, promote->propose, the owner-gated adoption
        new = _replace_block(new, feedback_block, rt.intake.FEEDBACK_BEGIN, rt.intake.FEEDBACK_END,
                             after=rt.intake.LIFECYCLE_END)
        intake_cfg = rt.worker_config.load()[0]['intake']
        # P36: topic-scoped threading + off-topic handoff to the inbox (P39: the configured inbox task id)
        handoff_block = rt.intake.handoff_policy_block(cli, intake_cfg['inbox_task'])
        new = _replace_block(new, handoff_block, rt.intake.HANDOFF_BEGIN, rt.intake.HANDOFF_END,
                             after=rt.intake.FEEDBACK_END)
        result = {'ok': True, 'changed': new != text, 'goal': str(goal_path)}
        if new != text:
            result['backup'] = str(_write_goal(goal_path, text, new, now))
        oncall = rt.registry.task_dir(intake_cfg['oncall_task']) / 'goal.md'
        if oncall.exists():
            old = oncall.read_text(encoding='utf-8')
            updated = _replace_block(old, blockers_block, rt.intake.BLOCKERS_BEGIN, rt.intake.BLOCKERS_END)
            updated = _replace_block(updated, cards_block, rt.intake.CARDS_BEGIN, rt.intake.CARDS_END,
                                     after=rt.intake.BLOCKERS_END)
            updated = _replace_block(updated, interaction_block, rt.intake.INTERACTION_BEGIN, rt.intake.INTERACTION_END,
                                     after=rt.intake.CARDS_END)
            updated = _replace_block(updated, lifecycle_block, rt.intake.LIFECYCLE_BEGIN, rt.intake.LIFECYCLE_END,
                                     after=rt.intake.INTERACTION_END)
            updated = _replace_block(updated, feedback_block, rt.intake.FEEDBACK_BEGIN, rt.intake.FEEDBACK_END,
                                     after=rt.intake.LIFECYCLE_END)
            updated = _replace_block(updated, handoff_block, rt.intake.HANDOFF_BEGIN, rt.intake.HANDOFF_END,
                                     after=rt.intake.FEEDBACK_END)
            result['oncall'] = {'goal': str(oncall), 'changed': updated != old}
            if updated != old:
                result['oncall']['backup'] = str(_write_goal(oncall, old, updated, now))
                result['changed'] = True
        else:
            result['oncall'] = {'goal': str(oncall), 'missing': True}
        return result

    def smoke_outcomes(self, rt, db, db_path, card=False, session=None, provider=None, now=None):
        """Synthetic TEST plans with synthetic facts through the ACTIVE spec (read-only; the shipped example if none)."""
        now = time.time() if now is None else now
        try:
            spec, info = self.load_active('access')
            source = 'active'
        except ValueError:
            spec = self.validate_spec(json.loads(self.example_spec.read_text(encoding='utf-8')))
            info, source = {'digest': digest(spec), 'file': self.example_spec.name}, 'example'
        report = {'ok': True, 'test': True, 'spec': {'source': source, 'version': spec['version'], 'file': info.get('file')},
                  'checks': {}}
        for expected, plan in self.pack.smoke_plans(spec).items():
            result = self.rule_check(rt, db, db_path, plan, facts=SMOKE_FACTS, smoke=True, spec=spec, info=info, now=now,
                                summary=self.pack.SMOKE_SUMMARY[expected])
            if card and expected in ('one_tap', self.key) and result['outcome'] == expected:
                result['card'] = self.post_check_card(rt, db, db_path, result['check_id'], result, plan, now, smoke=True,
                                                 session=session, provider=provider, live_card=card)
            report['checks'][expected] = {'check_id': result['check_id'], 'outcome': result['outcome'],
                                          'matched_rules': result['matched_rules'], 'card': result.get('card')}
        report['ok'] = all(v['outcome'] == k for k, v in report['checks'].items())
        if card:
            report['cleanup'] = [f'slack_followup.py smoke-cleanup --event-id {v["card"]["event_id"]}'
                                 for v in report['checks'].values() if v.get('card')]
        return report

    def smoke_proposal(self, rt, db, db_path, card=False, session=None, provider=None, timeout=900.0, poll=3.0, now=None):
        """TEST proposal in a temp rules dir (/tmp/<smoke_dir_prefix>*): adopted (by the card click with --card, else by a
        synthetic TEST decision) and applied by the same code path; the real rules dir is compared before and after."""
        now = time.time() if now is None else now
        real_before = _dir_fingerprint(self.rules_dir())
        temp = Path(tempfile.mkdtemp(prefix=self.smoke_dir_prefix, dir='/tmp'))
        os.chmod(temp, 0o700)
        spec = self.validate_spec(json.loads(self.example_spec.read_text(encoding='utf-8')))
        self.install_spec(spec, temp, who='smoke-rules', note='TEST', now=now)
        path = self.pack.SMOKE_PROPOSAL_PATH
        old = _child(_child(_child(_child(spec, 'rules'), self.pack.SMOKE_RULE), 'checks'), self.pack.SMOKE_CHECK)['params']['max_resources']
        proposal = {'schema': self.proposals_schema_id, 'proposals': [{
            'kind': 'rule_patch', 'category': 'access', 'base_version': spec['version'],
            'title_zh': self.msg('access.smoke_proposal.title_zh'), 'rationale_zh': self.msg('access.smoke_proposal.rationale_zh'),
            'patch': [{'op': 'replace', 'path': path, 'value': old + 1}]}]}
        report = {'ok': False, 'test': True, 'rules_dir': str(temp), 'card': bool(card)}
        if card:
            posted = self.propose(rt, db, db_path, proposal, now=now, directory=temp, smoke=True, session=session,
                             provider=provider, live_card=card)
            pid = posted['posted'][0]['proposal_id']
            report.update(proposal_id=pid, card_event=posted['posted'][0]['card_event'],
                          cleanup=f'slack_followup.py smoke-cleanup --event-id {posted["posted"][0]["card_event"]}')
            end = time.time() + timeout
            while time.time() < end:
                self.settle_proposals(rt, db)
                status = db.execute('SELECT status FROM rule_proposals WHERE id=?', (pid,)).fetchone()[0]
                if status != 'pending':
                    break
                time.sleep(poll)
        else:
            accepted, _ = self.prepare_proposals(rt, db, proposal, now, temp, smoke=True)
            pid = 'rp' + uuid.uuid4().hex[:14]
            item = accepted[0]
            with rt.transaction(db):
                db.execute('INSERT INTO rule_proposals (id,kind,category,fingerprint,title_zh,proposal,base_version,target,'
                           'proposer,status,created_at,smoke) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)',
                           (pid, 'rule_patch', 'access', item['fingerprint'], item['proposal']['title_zh'],
                            canonical(item['proposal']), spec['version'], str(temp), 'smoke-rules', 'pending', now))
            row = db.execute('SELECT * FROM rule_proposals WHERE id=?', (pid,)).fetchone()
            applied = self.apply_proposal(rt, db, row, 'smoke-rules (synthetic TEST decision)', now)
            with rt.transaction(db):
                db.execute("UPDATE rule_proposals SET status='applied',decided_by='smoke',decided_at=?,applied_at=?,"
                           'applied_version=? WHERE id=?', (now, now, applied['version'], pid))
            report['proposal_id'] = pid
        row = db.execute('SELECT * FROM rule_proposals WHERE id=?', (pid,)).fetchone()
        active = self.read_active(temp)
        try:
            new_spec, _ = self.load_active('access', temp)
            new_value = _child(_child(_child(_child(new_spec, 'rules'), self.pack.SMOKE_RULE), 'checks'), self.pack.SMOKE_CHECK)['params']['max_resources']
        except ValueError:
            new_spec, new_value = None, None
        real_after = _dir_fingerprint(self.rules_dir())
        report.update(status=row['status'], active=active['categories'].get('access'), changelog=len(read_changelog(temp, 100)),
                      value={'before': old, 'after': new_value}, real_rules_untouched=real_before == real_after)
        report['ok'] = bool(row['status'] == 'applied' and active['categories'].get('access') == 'access-v2.json' and
                            new_value == old + 1 and report['real_rules_untouched'])
        return report
