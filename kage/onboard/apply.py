"""Verification and application of the owner's confirmed candidates (onboarding design §4.3, O3c).

Verification is read-only and costs at most a few Slack reads: the owner's `name` / `timezone` against the verified
owner identity's profile (`users.info`), a person by exact real or display name in the workspace directory
(`users.list`, capped pages; the Slack id is resolved by code, never taken from text). Results sit next to the items
(`onboarding_verify`); only `verified` facts may be accepted in bulk (design §0.4 #6).

Applying turns the owner's accepted or edited items into the confirmed profile:

- always the private `~/.local/share/kage/private/<key>/confirmed.json` (0600), the one place later prompts read it;
- a **generated** deployment (its store has the `instance.mode` row that `kage init` writes): the owner fields
  `owner.json` has are written there and committed, people are appended to the deployment's people file (with a Slack
  id only when verified), rules are written to the live registry as `draft` (inert until the owner activates one);
- an **adopted** deployment (no mode row: it predates onboarding, and the services read a repository the skill keeps a
  copy of): nothing of the deployment is written; a `proposal.json` next to `confirmed.json` lists the changes instead.

Applied items are final. Never from an agent session.
"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time

from kage.core import mode as _mode
from kage.onboard import extract as _extract
from kage.onboard import intro as _intro

VERIFY_TABLE = '''CREATE TABLE IF NOT EXISTS onboarding_verify (item_id TEXT PRIMARY KEY, result TEXT NOT NULL,
                                                              detail TEXT, slack_id TEXT, at REAL NOT NULL)'''
RESULTS = ('verified', 'contradicted', 'ambiguous', 'not_found', 'unverifiable', 'error')
MAX_PAGES = 10
OWNER_JSON_FIELDS = ('name', 'short_name', 'timezone', 'language')  # the candidate fields owner.json carries
USER_RE = re.compile(r'[UW][A-Z0-9]{4,20}')


def _owner_only(env):
    env = os.environ if env is None else env
    if any(env.get(k) for k in _mode.AGENT_ENV):
        raise ValueError('only_the_owner_runs_onboarding')


def _same(a, b):
    return ' '.join(str(a or '').split()).casefold() == ' '.join(str(b or '').split()).casefold()


def results(db):
    """item id -> {result, detail, slack_id, at}; empty when nothing was verified."""
    try:
        return {r[0]: {'result': r[1], 'detail': r[2], 'slack_id': r[3], 'at': r[4]} for r in db.execute(
            'SELECT item_id, result, detail, slack_id, at FROM onboarding_verify')}
    except sqlite3.OperationalError:
        return {}


def verify(db, slack_read, owner_user, *, env=None, now=None, max_pages=MAX_PAGES):
    """Check the latest extraction's candidates; `slack_read(method, params) -> dict` makes one read. Returns counts."""
    _owner_only(env)
    now = time.time() if now is None else now
    items = [i for i in _extract.candidates(db) if i['status'] != 'applied']
    profile, directory = None, None
    out = {}
    for item in items:
        value = (item.get('decision') or {}).get('value') or item['value']
        try:
            if item['target'] == 'owner':
                if profile is None:
                    user = (slack_read('users.info', {'user': owner_user}) or {}).get('user') or {} if owner_user else {}
                    profile = {'name': [user.get('real_name'), (user.get('profile') or {}).get('real_name'),
                                        (user.get('profile') or {}).get('display_name')], 'timezone': user.get('tz')}
                checks = {f: v for f, v in value.items() if f in ('name', 'timezone')}
                if not checks or not owner_user:
                    out[item['id']] = ('unverifiable', None, None)
                    continue
                wrong = [f for f, v in checks.items() if not (
                    any(_same(v, n) for n in profile['name'] if n) if f == 'name' else _same(v, profile['timezone']))]
                out[item['id']] = ('contradicted', 'slack says ' + ', '.join(
                    f"{f}={profile['timezone'] if f == 'timezone' else next((n for n in profile['name'] if n), '')}"
                    for f in wrong), None) if wrong else ('verified', None, owner_user)
            elif item['target'] == 'people':
                if directory is None:
                    directory, cursor = [], None
                    for _ in range(max_pages):
                        page = slack_read('users.list', {'limit': 200, **({'cursor': cursor} if cursor else {})}) or {}
                        directory += [m for m in page.get('members') or [] if isinstance(m, dict)
                                      and not m.get('deleted') and not m.get('is_bot')]
                        cursor = (page.get('response_metadata') or {}).get('next_cursor')
                        if not cursor:
                            break
                found = [m for m in directory if any(_same(value.get('name'), n) for n in (
                    m.get('real_name'), (m.get('profile') or {}).get('real_name'),
                    (m.get('profile') or {}).get('display_name')) if n)]
                ids = sorted({m['id'] for m in found if USER_RE.fullmatch(str(m.get('id') or ''))})
                out[item['id']] = (('verified', None, ids[0]) if len(ids) == 1 else
                                   ('ambiguous', f'{len(ids)} people with this name', None) if ids else
                                   ('not_found', None, None))
            else:
                out[item['id']] = ('unverifiable', None, None)
        except Exception as exc:  # noqa: BLE001 - one unreadable answer marks its item, never the run
            out[item['id']] = ('error', type(exc).__name__, None)
    db.execute(VERIFY_TABLE)
    with db:
        for item_id, (result, detail, slack_id) in out.items():
            db.execute('INSERT OR REPLACE INTO onboarding_verify (item_id, result, detail, slack_id, at) VALUES '
                       '(?, ?, ?, ?, ?)', (item_id, result, detail, slack_id, now))
    counts = {}
    for result, _, _ in out.values():
        counts[result] = counts.get(result, 0) + 1
    return {'checked': len(out), 'results': counts}


def accept_verified(db, *, env=None, now=None):
    """Accept every candidate still open whose verification is `verified` (the only bulk acceptance)."""
    _owner_only(env)
    done = []
    checked = results(db)
    for item in _extract.candidates(db):
        if item['status'] == 'candidate' and (checked.get(item['id']) or {}).get('result') == 'verified':
            _extract.decide(db, item['id'], 'accept', env={}, now=now)
            done.append(item['id'])
    return {'accepted': len(done), 'ids': done}


def _write_private(path, data):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def generated(db):
    """True for a store `kage init` made (it has the instance.mode row); False for an adopted one."""
    try:
        return db.execute('SELECT 1 FROM runtime_meta WHERE key=?', (_mode.KEY,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _rule(value, quote, rule_id, today):
    return {'id': rule_id, 'kind': value['kind'], 'title': value['title'], 'statement': value['statement'],
            'why': [f'Owner self-introduction: "{quote}"'], 'scope': value['scope'], 'enforcement': value['enforcement'],
            'cases': [], 'status': 'draft', 'created': today, 'retired': None, 'supersedes': []}


def apply(db, key, *, deployment_dir, people_path=None, registry=None, home=None, env=None, now=None):
    """Apply the accepted / edited items of the latest extraction (see the module text). Returns what was written."""
    _owner_only(env)
    now = time.time() if now is None else now
    checked = results(db)
    items = [i for i in _extract.candidates(db) if i['status'] in ('accepted', 'edited')]
    if not items:
        return {'applied': 0}
    direct = generated(db)
    private = _intro.private_dirs(_intro.root(key, home)).parent
    today = dt.date.fromtimestamp(now).isoformat()
    owner_fields, people, rules, skipped = {}, [], [], []
    for item in items:
        value = (item.get('decision') or {}).get('value') or item['value']
        if item['target'] == 'owner':
            owner_fields.update({k: v for k, v in value.items() if k in OWNER_JSON_FIELDS and isinstance(v, str)})
        elif item['target'] == 'people':
            verdict = checked.get(item['id']) or {}
            person = {k: value[k] for k in ('name', 'role', 'relationship', 'communication') if k in value}
            if verdict.get('result') == 'verified' and verdict.get('slack_id'):
                person['identities'] = [{'type': 'slack', 'value': verdict['slack_id']}]
            people.append(person)
        elif item['target'] == 'rule':
            rules.append((item, value))
    written = {'owner_fields': [], 'people': 0, 'rules': [], 'commit': None}
    if direct:
        deployment_dir = Path(deployment_dir)
        if owner_fields:
            path = deployment_dir / 'owner.json'
            doc = json.loads(path.read_text(encoding='utf-8'))
            changed = sorted(k for k, v in owner_fields.items() if doc['owner'].get(k) != v)
            if changed:
                doc['owner'].update({k: owner_fields[k] for k in changed})
                tmp = path.with_name('.owner.json.tmp')
                tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                os.chmod(tmp, path.stat().st_mode & 0o777)
                os.replace(tmp, path)
                written['owner_fields'] = changed
                if (deployment_dir.parent / '.git').exists() or (deployment_dir / '.git').exists():
                    repo = deployment_dir if (deployment_dir / '.git').exists() else deployment_dir.parent
                    subprocess.run(['git', '-C', str(repo), 'add', str(path)], check=True, capture_output=True)
                    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=kage', '-c', 'user.email=kage@localhost',
                                    'commit', '-q', '-m', 'onboarding: owner fields confirmed by the owner'],
                                   check=True, capture_output=True)
                    written['commit'] = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                                                       capture_output=True, text=True).stdout.strip()
        if people and people_path:
            path = Path(people_path).expanduser()
            doc = _read_json(path, {'people': []})
            if not isinstance(doc, dict) or not isinstance(doc.get('people'), list):
                raise ValueError('people_file_has_no_people_list')
            known = {p.get('name') for p in doc['people'] if isinstance(p, dict)}
            fresh = [p for p in people if p.get('name') not in known]
            doc['people'] += fresh
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_private(path, doc)
            written['people'] = len(fresh)
        if rules and registry is not None:
            live = Path(registry.live_dir())
            live.mkdir(parents=True, exist_ok=True)
            taken = [int(p.stem[2:]) for p in live.glob('R-*.json') if p.stem[2:].isdigit()]
            next_id = max(taken, default=0) + 1
            for item, value in rules:
                rule = _rule(value, item['quote'], f'R-{next_id:04d}', today)
                problems = registry.rule_problems(rule, f'{rule["id"]}.json')
                if problems:
                    skipped.append({'id': item['id'], 'why': problems[0]})
                    continue
                (live / f'{rule["id"]}.json').write_text(json.dumps(rule, ensure_ascii=False, indent=2) + '\n',
                                                         encoding='utf-8')
                written['rules'].append(rule['id'])
                next_id += 1
    else:
        proposal = {'schema': 'kage.onboarding.proposal/v1', 'at': now, 'owner_fields': owner_fields, 'people': people,
                    'rules': [_rule(value, item['quote'], None, today) for item, value in rules]}
        _write_private(private / 'proposal.json', proposal)
    confirmed = _read_json(private / 'confirmed.json', {'schema': 'kage.onboarding.confirmed/v1', 'items': []})
    confirmed['items'] = [i for i in confirmed['items'] if i.get('id') not in {x['id'] for x in items}] + [
        {'id': i['id'], 'target': i['target'], 'value': (i.get('decision') or {}).get('value') or i['value'],
         'quote': i['quote'], 'verify': (checked.get(i['id']) or {}).get('result'), 'applied_at': now} for i in items]
    _write_private(private / 'confirmed.json', confirmed)
    with db:
        for item in items:
            db.execute("UPDATE onboarding_items SET status='applied' WHERE id=? AND status IN ('accepted', 'edited')",
                       (item['id'],))
    return {'applied': len(items), 'deployment': 'generated' if direct else 'adopted', 'written': written,
            'skipped': skipped, 'confirmed': str(private / 'confirmed.json'),
            **({} if direct else {'proposal': str(private / 'proposal.json')})}
