"""Rule registry: one JSON file per rule (R-NNNN.json) plus case files, in a live directory, else a seed directory.

A registry rule is the switch and the text of a code check (a guard): the guard runs only while its rule is active, and
its refusal carries the rule's hint, id and title. Agents only read the registry; installing a rule is the owner's.

The registry knows nothing about a particular deployment. The caller gives it:
  live_dir   a callable returning the live directory (read each time, so an environment change is seen)
  seed       the directory used when the live one does not exist, and per file when a live file is unreadable
  guards     the guard names the runtime implements (an unknown guard in a rule is a lint problem)
  language   the catalog for titles, levels and the `show` text (kage.locale)
State (the parsed-file cache) lives in the Registry object, never in this module.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import time

from kage import locale

KINDS = ('fact', 'limit', 'preference', 'principle', 'gotcha')
STATUSES = ('draft', 'active', 'retired')
LEVELS = ('L0', 'L1', 'L2', 'L3', 'L4')
FIELDS = ('id', 'kind', 'title', 'statement', 'why', 'scope', 'enforcement', 'cases', 'status', 'created', 'retired',
          'supersedes')
ID_RE = re.compile(r'R-\d{4}')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
SENTENCE_RE = re.compile(r'(?<=[.!?])\s+|(?<=[。！？])')


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8')), None
    except (OSError, ValueError):
        return None, f'{Path(path).name}: not readable JSON'


class Registry:
    def __init__(self, *, live_dir, seed, guards, language):
        self.live_dir = live_dir
        self.seed_dir = Path(seed)
        self.guards = tuple(guards)
        self.language = language
        self.title_re = re.compile(locale.pattern(language, 'registry.title_script'))
        self._cache = {}

    def _msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def load(self, directory=None):
        """{'source': live|seed|dir, 'dir', 'files': [(name, rule)], 'cases', 'problems'}. An unreadable live file falls
        back to the seed file of the same name (fail closed to what the owner approved); lint reports it."""
        d, source = (Path(directory), 'dir') if directory else (Path(self.live_dir()), 'live')
        if not d.is_dir() and not directory:
            d = self.seed_dir
        source = 'seed' if d.resolve() == self.seed_dir.resolve() else source
        case_files = [d / 'cases.json'] + sorted(d.glob('cases-*.json'))  # a later release may add its own cases file
        paths = sorted(d.glob('R-*.json')) + case_files
        key = (str(d), tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.exists()))
        if self._cache.get(str(d), (None,))[0] == key:
            return self._cache[str(d)][1]
        files, problems, cases = [], [], {}
        for p in paths[:-len(case_files)]:
            rule, problem = _read(p)
            if problem:
                problems.append(problem)
                rule = _read(self.seed_dir / p.name)[0] if d != self.seed_dir else None
            if isinstance(rule, dict):
                files.append((p.name, rule))
        for p in (p for p in case_files if p.exists()):  # never overriding a case an earlier file has
            more, problem = _read(p)
            problems += [problem] * bool(problem)
            for cid, case in (more if isinstance(more, dict) else {}).items():
                cases.setdefault(cid, case)
        out = {'source': source, 'dir': str(d), 'files': files, 'cases': cases, 'problems': problems}
        self._cache[str(d)] = (key, out)
        return out

    def rule_for(self, guard, directory=None):
        return next((r for _, r in self.load(directory)['files'] if (r.get('enforcement') or {}).get('guard') == guard),
                    None)

    def active(self, guard, directory=None):
        """The active rule whose code check is `guard`, else None (retired, draft or missing: the check is off)."""
        rule = self.rule_for(guard, directory)
        return rule if rule and rule.get('status') == 'active' else None

    def refuse(self, guard, detail):
        """Raise the agent-facing refusal: what is wrong, then the rule's hint (how to fix it), id and title."""
        rule = self.rule_for(guard) or {}
        raise ValueError(f'{detail}. Fix: {(rule.get("enforcement") or {}).get("hint", "")} '
                         f'[{rule.get("id", guard)} {rule.get("title", "")}]')

    def rule_problems(self, rule, name=None):
        p = []
        if not isinstance(rule, dict):
            return ['not a JSON object']
        p += [f'missing or extra fields: {sorted(set(FIELDS) ^ set(rule))}'] * (set(rule) != set(FIELDS))
        rid = str(rule.get('id'))
        p += [f'id {rid!r} is not R-NNNN'] * (not ID_RE.fullmatch(rid))
        p += [f'file name must be {rid}.json'] * bool(name and name != f'{rid}.json')
        p += [f'kind must be one of {"/".join(KINDS)}'] * (rule.get('kind') not in KINDS)
        p += [f'status must be one of {"/".join(STATUSES)}'] * (rule.get('status') not in STATUSES)
        p += [self._msg('registry.problem.title')] * (not self.title_re.search(str(rule.get('title') or '')))
        statement = str(rule.get('statement') or '').strip()
        p += ['statement is empty'] * (not statement)
        p += ['statement is longer than 2 sentences'] * (len([s for s in SENTENCE_RE.split(statement) if s.strip()]) > 2)
        p += ['why must list an incident or decision'] * (not isinstance(rule.get('why'), list) or not rule['why'])
        scope = rule.get('scope') if isinstance(rule.get('scope'), dict) else {}
        p += ['scope needs actions and task_classes lists'] * (not all(isinstance(scope.get(k), list) for k in
                                                                      ('actions', 'task_classes')))
        enf = rule.get('enforcement') if isinstance(rule.get('enforcement'), dict) else {}
        p += [f'enforcement.level must be one of {"/".join(LEVELS)}'] * (enf.get('level') not in LEVELS)
        p += [f'unknown guard {enf.get("guard")!r} (the code knows {", ".join(self.guards)})'] * bool(
            enf.get('guard') and enf.get('guard') not in self.guards)
        p += ['enforcement.hint is empty'] * (not str(enf.get('hint') or '').strip())
        p += ['cases must be a list'] * (not isinstance(rule.get('cases'), list))
        p += ['an active rule needs at least one case'] * (rule.get('status') == 'active' and not rule.get('cases'))
        p += ['created must be YYYY-MM-DD'] * (not DATE_RE.fullmatch(str(rule.get('created'))))
        p += ['a retired rule needs its retired date'] * (rule.get('status') == 'retired' and
                                                         not DATE_RE.fullmatch(str(rule.get('retired'))))
        p += ['supersedes must be a list'] * (not isinstance(rule.get('supersedes'), list))
        return p

    def lint(self, directory=None):
        """Every rule file valid; no duplicate id or guard; every case of an active rule in a cases file."""
        reg = self.load(directory)
        problems, ids, guards = list(reg['problems']) + ['no rule files'] * (not reg['files']), {}, {}
        for name, rule in reg['files']:
            problems += [f'{name}: {x}' for x in self.rule_problems(rule, name)]
            if rule.get('id') in ids:
                problems.append(f'{name}: duplicate id {rule["id"]} (also {ids[rule["id"]]})')
            ids.setdefault(rule.get('id'), name)
            guard = (rule.get('enforcement') or {}).get('guard')
            if guard and guard in guards:
                problems.append(f'{name}: duplicate guard {guard} (also {guards[guard]})')
            guards.setdefault(guard, name)
            problems += [f'{name}: case {c} is not in cases.json' for c in rule.get('cases') or []
                         if rule.get('status') == 'active' and c not in reg['cases']]
        return {'ok': not problems, 'source': reg['source'], 'dir': reg['dir'], 'rules': len(reg['files']),
                'active': sum(r.get('status') == 'active' for _, r in reg['files']), 'problems': problems}

    def overview(self, directory=None):
        """The owner-facing list: title, how it is checked, why, status."""
        reg, out = self.load(directory), []
        for _, r in sorted(reg['files'], key=lambda x: str(x[1].get('id'))):
            enf, scope = r.get('enforcement') or {}, r.get('scope') or {}
            level = enf.get('level')
            label = self._msg(f'registry.level.{level}') if level in LEVELS else level
            how = self._msg('registry.how', level=label, guard=enf.get('guard'),
                            actions=self._msg('registry.actions_separator').join(scope.get('actions') or []))
            status = r.get('status')
            out.append({'id': r.get('id'), 'title': r.get('title'), 'how': how,
                        'why': self._msg('registry.why_separator').join(r.get('why') or []),
                        'status': self._msg(f'registry.status.{status}') if status in STATUSES else status,
                        'statement': r.get('statement')})
        return {'source': reg['source'], 'dir': reg['dir'], 'rules': out}

    def show_text(self, directory=None):
        view = self.overview(directory)
        where = self._msg('registry.show.where_live' if view['source'] == 'live' else 'registry.show.where_seed')
        head = self._msg('registry.show.head', where=where, dir=view['dir'])
        return '\n'.join([head] + [self._msg('registry.show.rule', **{k: r[k] for k in ('id', 'title', 'status', 'how',
                                                                                         'why')})
                                   for r in view['rules']])

    def seed(self, target=None):
        """Create the live registry from the seed when it is absent (0700 / 0600); never touches an existing directory."""
        target = Path(target or self.live_dir())
        if target.exists():
            return {'ok': True, 'seeded': False, 'dir': str(target)}
        target.mkdir(parents=True, mode=0o700)
        for p in sorted(self.seed_dir.glob('*.json')):
            shutil.copyfile(p, target / p.name)
            (target / p.name).chmod(0o600)
        return {'ok': True, 'seeded': True, 'dir': str(target)}

    def install(self, path, who, now=None):
        """Install one rule file (the owner's action, checked by the caller): validate it, write it into the live
        registry (seeded first when absent), lint the whole registry (restored on failure), append changelog.jsonl."""
        rule, problem = _read(path)
        problems = [problem] if problem else self.rule_problems(rule)
        if problems:
            raise ValueError('registry_rule_invalid: ' + '; '.join(problems))
        d = Path(self.live_dir())
        self.seed(d)
        target = d / f'{rule["id"]}.json'
        old = target.read_bytes() if target.exists() else None
        target.write_text(json.dumps(rule, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        target.chmod(0o600)
        result = self.lint(d)
        if not result['ok']:
            target.write_bytes(old) if old is not None else target.unlink()
            raise ValueError('registry_lint_failed_nothing_changed: ' + '; '.join(result['problems']))
        now = time.time() if now is None else now
        with os.fdopen(os.open(d / 'changelog.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a',
                       encoding='utf-8') as log:
            log.write(json.dumps({'ts': now, 'kind': 'registry_install', 'id': rule['id'], 'status': rule['status'],
                                  'replaced': old is not None, 'who': who}, ensure_ascii=False) + '\n')
        return {'ok': True, 'dir': str(d), 'id': rule['id'], 'status': rule['status'], 'replaced': old is not None,
                'lint': result}
