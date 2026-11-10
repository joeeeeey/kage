"""`kage init` (O1c4): generate a new deployment from the answers and the verified records, or adopt an existing one.

A deployment is a private directory (`~/.local/share/kage/deploy-<key>/` by default), a git repository init never
pushes:

  deploy/owner.json      kage.owner/v1: the answers' name / key / language / timezone; the Slack workspace, the owner's
                         user, the bot user and the app only from the verified records (identity.load / load_bot)
  deploy/org.json        kage.org/v1: tracker none, no channels
  deploy/instance.json   kage.instance/v1: the workspace root, the inbox rules file, and `assembly` (program, lock,
                         runtime version) with no `values`: every assembly value takes its neutral, owner-scoped default
  deploy/packs/default/access.py, deploy/packs/<key>/playbook.py   the neutral templates (kage.packs), pinned
  kage.lock              the running release (BUNDLE.json, verified), every switch on Kage, the two pack pins
  scripts/kage.py        the deployment's program: runs the pinned release on deploy/ (`scripts/kage.py --help`)
  SKILL.md, references/, scripts/dispatch_runner.py   what its dispatched runs read (kage.onboard.dispatch_files, O1c5b)
  services.json          followup / dashboard / receiver for launchd (kage.onboard.services, O1c5e: absolute paths, this
                         Python, the PATH init ran with for the agents' CLIs); loaded only by `services start`
  .gitignore, README.md

Nothing is written in place. The new tree is built in a staging directory next to the target (0700 directories, 0600
files; the program 0700), checked (the profile loads, the lock pins this release, both packs verify), then published by
rename. On an existing deployment the same tree is compared file by file (`plan`): init owns the files above, keeps
every other file, never replaces a pack (a pack whose bytes differ from its pin refuses), and refuses a symlink, a
directory of another owner key or a verified identity that differs from the profile. `--apply` publishes the
changes; without it init only prints them. A new git commit records each publish. An interruption leaves either the
old or the new tree (a leftover `<target>.kage-old-*` names itself and refuses the next run).

`adopt(deployment_dir)` records the onboarding row and nothing else (a deployment built before init).

Also outside the deployment: the state directory (0700) and the workspace root (0700) are created when missing, and a
card channel answer writes the decisions config (0600). init sends no message, reads no conversation, loads no
service.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

import kage
from kage import packs, profile
from kage.core import mode as _mode
from kage.onboard import answers as _answers
from kage.onboard import dispatch_files
from kage.onboard import services as _services
from kage.onboard import identity

SWITCHES = ('access', 'asks', 'assembly', 'authority', 'avatars', 'blockers', 'buttons', 'cards', 'claims', 'cli',
            'commands', 'dashboard', 'dashboard_rest', 'dashboard_web', 'decisions', 'delivery', 'dispatch_cards',
            'dispatch_core', 'dispatch_engine', 'dispatch_rest', 'envelope', 'escalation', 'followup_core', 'health',
            'intake_cli', 'intake_core', 'intake_engine', 'intake_playbook', 'intake_rest', 'learning', 'lifecycle',
            'liveness', 'loop', 'memory', 'previews', 'profile', 'prompts', 'providers', 'receiver', 'registry',
            'rename', 'routines', 'rules', 'runlog', 'runner', 'slack_client', 'store', 'threads', 'trace',
            'trace_rest', 'transport', 'worker_config')
LOCK_SCHEMA = 'kage.lock/v1'
PROGRAM = 'scripts/kage.py'
EXECUTABLES = (PROGRAM, dispatch_files.RUNNER)  # 0700; every other file 0600
ACCESS_PACK = ('default.access', 'deploy/packs/default/access.py')
PACK_FILES = ('deploy/packs/default/access.py',)  # + deploy/packs/<key>/playbook.py
OLD_SUFFIX = '.kage-old-'
STAGING_PREFIX = '.kage-init-'
ONBOARDING_TABLE = ('CREATE TABLE IF NOT EXISTS onboarding_steps (step TEXT PRIMARY KEY, status TEXT NOT NULL, '
                    'at REAL NOT NULL, detail TEXT NOT NULL)')
GIT_ENV = {'GIT_AUTHOR_NAME': 'kage init', 'GIT_AUTHOR_EMAIL': 'kage-init@localhost',
           'GIT_COMMITTER_NAME': 'kage init', 'GIT_COMMITTER_EMAIL': 'kage-init@localhost',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'}

PROGRAM_TEXT = '''#!/usr/bin/env python3
"""This deployment's program, written by `kage init`: run the Kage release kage.lock pins on deploy/.

  scripts/kage.py --help | doctor | status | ...

The release is <versions>/<version>-<commit12> ($KAGE_VERSIONS_DIR, else ~/.local/share/kage/versions). It must verify
against the lock (its BUNDLE.json, MANIFEST.sha256 and every file) before any of its code but the checker runs.
Exit 78 when it does not."""
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    pin = json.loads((ROOT / 'kage.lock').read_text(encoding='utf-8'))['kage']
    versions = Path(os.environ.get('KAGE_VERSIONS_DIR') or Path.home() / '.local/share/kage/versions')
    release = versions / f"{pin['version']}-{pin['commit'][:12]}"
    spec = importlib.util.spec_from_file_location('kage_release_check', release / 'kage' / 'service.py')
    try:
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        problems = check.release_problems(release, pin)
    except (OSError, AttributeError) as exc:
        problems = [f'release_unreadable: {type(exc).__name__}']
    if problems:
        print(json.dumps({'error': 'KageRefused', 'detail': f'release {release.name}: ' + ', '.join(problems[:5])}),
              file=sys.stderr)
        return 78
    sys.path.insert(0, str(release))
    from kage import __main__ as kage_main
    return kage_main.main(['--deployment', str(ROOT / 'deploy'), *sys.argv[1:]])


if __name__ == '__main__':
    sys.exit(main())
'''

GITIGNORE = '__pycache__/\n*.pyc\n.DS_Store\n'

README = '''# Kage deployment of {name} ({key})

Written by `kage init` from the answers file; Kage {version}. Private: never push it anywhere public.

- `deploy/`: the profile (owner, org, instance) and the packs, pinned in `kage.lock`.
- `scripts/kage.py`: this deployment's program (`scripts/kage.py --help`, `scripts/kage.py doctor`).
- Tokens are in the secrets store, never in these files. State: `{state}`.

Re-run `kage init --answers <file>` to see what would change; add `--apply` to write it.
'''


class InitError(ValueError):
    """init refused (said why); nothing was published."""


def default_target(key, home=None):
    return Path(home or Path.home()) / '.local/share/kage' / f'deploy-{key}'


def state_dir(key, home=None):
    return Path(home or Path.home()) / '.local/share/kage' / f'state-{key}'


def config_dir(key, home=None):
    return Path(home or Path.home()) / '.config/kage' / key


def release_pin(release=None):
    """{version, commit, tree_sha256} of a verified release of this Kage (default: the one running)."""
    from kage import service
    release = Path(release).expanduser() if release else Path(kage.__file__).resolve().parents[1]
    try:
        meta = json.loads((release / 'BUNDLE.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise InitError('init_needs_a_release: run it from an installed Kage release, or name one with --release') from None
    pin = {k: meta.get(k) for k in ('version', 'commit', 'tree_sha256')}
    if pin['version'] != kage.__version__:
        raise InitError(f"release_is_another_version: {pin['version']} (this is Kage {kage.__version__})")
    problems = service.release_problems(release, pin)
    if problems:
        raise InitError('release_does_not_verify: ' + ', '.join(problems[:5]))
    return pin


def _json(data):
    return (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def records(key, home=None):
    """(owner identity, bot) from the state directory, or InitError: a profile never takes a typed identity."""
    d = state_dir(key, home)
    try:
        owner, bot = identity.load(d), identity.load_bot(d)
    except identity.TokenRefused as exc:
        raise InitError(f'verified_records_refused: {exc.code}') from None
    if owner is None:
        raise InitError('owner_identity_not_verified: run `kage init` (the wizard) to prove it first')
    if bot is None:
        raise InitError('bot_not_verified: run `kage init` (the wizard) to check the bot token first')
    if owner['team_id'] != bot['team_id']:
        raise InitError('owner_and_bot_in_different_workspaces')
    if owner['user_id'] == bot['bot_user_id']:
        raise InitError('the_owner_is_the_bot')
    return owner, bot


def files(answers, owner_record, bot_record, pin):
    """The tree init owns: {relative path: bytes} (packs included)."""
    who = answers['owner']
    key = who['key']
    access = packs.template('default.access')
    playbook = packs.template('default.playbook')
    playbook_rel = f'deploy/packs/{key}/playbook.py'
    owner = {
        'schema': profile.OWNER_SCHEMA,
        'owner': {'name': who['name'], 'short_name': who['short_name'], 'key': key, 'language': who['language'],
                  'timezone': who['timezone'], 'timezone_fallback_offset': answers['_fallback_offset']},
        'identities': {'slack': {'workspace': owner_record['team_id'], 'workspace_url': bot_record['url'],
                                 'user': owner_record['user_id']}},
        'assistant': {'display': f"{who['short_name']}'s kage",
                      'slack': {'bot_user': bot_record['bot_user_id'], 'app': bot_record['app_id']}},
    }
    org = {'schema': profile.ORG_SCHEMA, 'name': who['name'], 'tickets': {'tracker': 'none'},
           'channels': {}}
    instance = {
        'schema': profile.INSTANCE_SCHEMA,
        'paths': {'workspace_root': answers['workspace_root'], 'inbox_rules': f'~/.config/kage/{key}/inbox-rules.md'},
        'assembly': {'program': PROGRAM, 'lock': 'kage.lock', 'runtime_version': f"kage-{pin['version']}"},
    }
    lock = {'schema': LOCK_SCHEMA, 'kage': dict(pin), 'modules': dict.fromkeys(SWITCHES, 'kage'),
            'packs': {'default.access': {'path': ACCESS_PACK[1], 'sha256': _sha(access)},
                      f'{key}.playbook': {'path': playbook_rel, 'sha256': _sha(playbook)}}}
    readme = README.format(name=who['name'], key=key, version=pin['version'], state=f'~/.local/share/kage/state-{key}/')
    return {'deploy/owner.json': _json(owner), 'deploy/org.json': _json(org), 'deploy/instance.json': _json(instance),
            ACCESS_PACK[1]: access, playbook_rel: playbook, 'kage.lock': _json(lock),
            PROGRAM: PROGRAM_TEXT.encode('utf-8'), '.gitignore': GITIGNORE.encode('utf-8'),
            'README.md': readme.encode('utf-8'),
            **dispatch_files.files(key=key, name=who['short_name'], owner_user=owner_record['user_id'],
                                   version=pin['version'], program=PROGRAM, language=who['language'])}


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def keep_local(target, wanted):
    """F11 (0.78.1): a re-run keeps what the deployment added on top of what init writes: instance.json keys init does
    not write (e.g. `console`, `assembly.values` / `modules`, extra `paths`), kage.lock packs init does not write and
    the pin of every pack file it keeps (init never replaces a pack), extra lock switches, and a dashboard port that
    differs from the default. Everything init writes itself is still regenerated from the answers."""
    target = Path(target)
    if not target.is_dir():
        return wanted
    old = _read_json(target / 'deploy/instance.json')
    if isinstance(old, dict):
        new = json.loads(wanted['deploy/instance.json'])
        for key, value in old.items():
            if key not in new:
                new[key] = value
        new['paths'] = {**(old.get('paths') or {}), **new['paths']}
        for key, value in (old.get('assembly') or {}).items():
            new['assembly'].setdefault(key, value)
        wanted['deploy/instance.json'] = _json(new)
    old = _read_json(target / 'kage.lock')
    if isinstance(old, dict):
        new = json.loads(wanted['kage.lock'])
        for name, pin in (old.get('packs') or {}).items():
            path = isinstance(pin, dict) and pin.get('path')
            if name not in new['packs'] or (path and (target / path).is_file()):
                new['packs'][name] = pin
        for name, value in (old.get('modules') or {}).items():
            new['modules'].setdefault(name, value)
        wanted['kage.lock'] = _json(new)
    old = _read_json(target / 'services.json')
    if isinstance(old, dict) and 'services.json' in wanted:
        new = json.loads(wanted['services.json'])
        args = ((old.get('services') or {}).get('dashboard') or {}).get('args')
        mine = new['services']['dashboard']['args']
        if isinstance(args, list) and len(args) == len(mine) and args[:-1] == mine[:-1] and str(args[-1]).isdigit():
            new['services']['dashboard']['args'] = args
            wanted['services.json'] = _json(new)
    return wanted


def _is_pack(rel):
    return rel.startswith('deploy/packs/')


def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _check_private_dir(path, what):
    st = _lstat(path)
    if st is None:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise InitError(f'{what}_is_not_a_directory: {path}')
    if st.st_uid != os.getuid():
        raise InitError(f'{what}_belongs_to_another_user: {path}')
    return True


def plan(target, wanted):
    """What publishing `wanted` into `target` would do: [{path, action}] with action create / same / change / keep, or
    InitError. `keep`: a pack that already exists (init never replaces one)."""
    target = Path(target)
    for leftover in target.parent.glob(target.name + OLD_SUFFIX + '*') if target.parent.is_dir() else ():
        raise InitError(f'interrupted_earlier_init: {leftover} is left from an interrupted publish; restore or remove it')
    if not _check_private_dir(target, 'deployment'):
        return [{'path': rel, 'action': 'create'} for rel in sorted(wanted)]
    owner_file = target / 'deploy/owner.json'
    if owner_file.exists():
        try:
            existing = json.loads(owner_file.read_text(encoding='utf-8'))
            new = json.loads(wanted['deploy/owner.json'])
        except ValueError:
            raise InitError('existing_owner_json_unreadable') from None
        if (existing.get('owner') or {}).get('key') != new['owner']['key']:
            raise InitError('deployment_belongs_to_another_owner_key')
        if (existing.get('identities') or {}).get('slack') != new['identities']['slack']:
            raise InitError('verified_identity_differs_from_the_deployment')
    lock_pins = {}
    try:
        lock_pins = {e['path']: e['sha256'] for e in json.loads((target / 'kage.lock').read_text())['packs'].values()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    out = []
    for rel in sorted(wanted):
        path = target / rel
        for parent in reversed(list(path.relative_to(target).parents)[:-1]):
            st = _lstat(target / parent)
            if st is not None and (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)):
                raise InitError(f'not_a_plain_directory: {parent}')
        st = _lstat(path)
        if st is None:
            out.append({'path': rel, 'action': 'create'})
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise InitError(f'not_a_plain_file: {rel}')
        data = path.read_bytes()
        if _is_pack(rel):
            if data != wanted[rel] and lock_pins.get(rel) != _sha(data):  # only a pinned pack is kept as it is
                raise InitError(f'pack_differs_from_its_pin: {rel}')
            out.append({'path': rel, 'action': 'keep' if data != wanted[rel] else 'same'})
        else:
            out.append({'path': rel, 'action': 'same' if data == wanted[rel] else 'change'})
    return out


def _effective(target, wanted, steps):
    """`wanted` with every kept pack's existing bytes, and the lock re-pinned to them (pins unchanged: plan() refused
    a pack that differs from its pin)."""
    tree = dict(wanted)
    kept = [s['path'] for s in steps if s['action'] == 'keep']
    if kept:
        lock = json.loads(tree['kage.lock'])
        for rel in kept:
            tree[rel] = (Path(target) / rel).read_bytes()
            for entry in lock['packs'].values():
                if entry['path'] == rel:
                    entry['sha256'] = _sha(tree[rel])
        tree['kage.lock'] = _json(lock)
    return tree


def _actions(target, tree, steps):
    """The steps again, every non-pack file compared with the effective tree (the lock may carry a kept pack's pin)."""
    out = []
    for s in steps:
        if s['action'] in ('same', 'change') and not _is_pack(s['path']):
            same = (Path(target) / s['path']).read_bytes() == tree[s['path']]
            s = {**s, 'action': 'same' if same else 'change'}
        out.append(s)
    return out


def diff_text(target, wanted, steps):
    """A short unified diff of the changed text files (never a pack's body)."""
    import difflib
    lines = []
    for s in steps:
        if s['action'] == 'change':
            old = (Path(target) / s['path']).read_text(encoding='utf-8', errors='replace').splitlines(keepends=True)
            new = wanted[s['path']].decode('utf-8', errors='replace').splitlines(keepends=True)
            lines += difflib.unified_diff(old, new, f"a/{s['path']}", f"b/{s['path']}")
    return ''.join(lines)


def _write_tree(root, tree):
    for rel, data in sorted(tree.items()):
        path = root / rel
        for parent in reversed(list(path.relative_to(root).parents)[:-1]):
            d = root / parent
            if not d.exists():
                d.mkdir(mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(path, 0o700 if rel in EXECUTABLES else 0o600)


def _check_staged(root, key):
    """The staged deployment loads and pins this release and its packs (no unit runs, nothing written)."""
    from kage import app
    environ = os.environ.pop('KAGE_LOCK', None)
    try:
        a = app.App(root / 'deploy')
        a.verify()
        for name in ('default.access', f'{key}.playbook'):
            a.bridge.pack(name)
    except (app.AssemblyError, profile.ProfileError) as exc:
        raise InitError(f'generated_deployment_refused: {exc}') from None
    finally:
        if environ is not None:
            os.environ['KAGE_LOCK'] = environ


def _git(root, *args):
    git = shutil.which('git')
    if git is None:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_ENV)
    return subprocess.run([git, '-c', 'commit.gpgsign=false', '-c', f'core.hooksPath={os.devnull}', '-C', str(root),
                           *args], capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)


def _commit(root, message, paths):
    if _git(root, 'rev-parse', '--git-dir') is None:
        return None
    if not (root / '.git').exists():
        _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'add', '--', *paths)
    done = _git(root, 'commit', '-q', '-m', message)
    head = _git(root, 'rev-parse', '--short', 'HEAD')
    return head.stdout.strip() if done.returncode == 0 and head.returncode == 0 else None


def publish(target, tree, steps, key, *, message):
    """Build the staging tree (a copy of the existing deployment with `tree` written over it), check it, swap it in.
    Returns the git head or None."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX + target.name + '-', dir=target.parent))
    try:
        if target.exists():
            shutil.rmtree(staging)
            shutil.copytree(target, staging, symlinks=True)
        os.chmod(staging, 0o700)
        _write_tree(staging, {s['path']: tree[s['path']] for s in steps if s['action'] in ('create', 'change')})
        _check_staged(staging, key)
        head = _commit(staging, message, sorted(tree))
        if target.exists():
            old = target.with_name(target.name + OLD_SUFFIX + str(int(time.time())))
            os.rename(target, old)
            os.rename(staging, target)
            shutil.rmtree(old)
        else:
            os.rename(staging, target)
        return head
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _private_mkdir(path):
    path = Path(path)
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    _check_private_dir(path, 'directory')
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise InitError(f'directory_not_private: {path}')


def outside_files(answers, owner_record, home=None):
    """{absolute path: bytes} init writes outside the deployment: the decisions config for a card channel."""
    key, cards = answers['owner']['key'], answers['slack']['cards']
    if cards == 'dm':
        return {}
    return {config_dir(key, home) / 'decisions.json':
            _json({'decision_channel': cards, 'approvers': [owner_record['user_id']]})}


def record_step(db_path, step, status, detail, *, clock=time.time):
    """The onboarding row (additive table, created on first use)."""
    with sqlite3.connect(db_path) as db:
        db.execute(ONBOARDING_TABLE)
        db.execute('INSERT OR REPLACE INTO onboarding_steps (step, status, at, detail) VALUES (?, ?, ?, ?)',
                   (step, status, clock(), json.dumps(detail, sort_keys=True)))


def run(answers, *, target=None, apply=False, release=None, home=None):
    """Plan, and with `apply` (always for a new deployment) publish. Returns a report (no secret in it)."""
    key = answers['owner']['key']
    home = Path(home or Path.home())
    target = Path(target).expanduser() if target else default_target(key, home)
    owner_record, bot_record = records(key, home)
    pin = release_pin(release)
    wanted = files(answers, owner_record, bot_record, pin)
    wanted['services.json'] = _json(_services.spec(key=key, target=target, state=state_dir(key, home),
                                                   python=sys.executable,
                                                   path=_services.service_path(os.environ.get('PATH', ''))))
    keep_local(target, wanted)
    steps = plan(target, wanted)
    tree = _effective(target, wanted, steps)
    steps = _actions(target, tree, steps)
    outside = outside_files(answers, owner_record, home)
    outside_steps = []
    for path, data in sorted(outside.items()):
        st = _lstat(path)
        if st is not None and (stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode)):
            raise InitError(f'not_a_plain_file: {path}')
        outside_steps.append({'path': str(path), 'action': 'create' if st is None else (
            'same' if path.read_bytes() == data else 'change')})
    new = not target.exists()
    changes = [s for s in steps + outside_steps if s['action'] in ('create', 'change')]
    report = {'target': str(target), 'new': new, 'release': pin['version'], 'steps': steps + outside_steps,
              'diff': '' if new else diff_text(target, tree, steps), 'applied': False, 'git': None}
    if not changes or (not new and not apply):
        return report
    _private_mkdir(state_dir(key, home))
    root = answers['workspace_root']
    _private_mkdir(home / root[2:] if root.startswith('~/') else Path(root))
    report['git'] = publish(target, tree, steps, key, message=f"kage init: Kage {pin['version']}"
                            + ('' if new else ' (re-run)'))
    for path, data in outside.items():
        _private_mkdir(path.parent)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.kage-init-')
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    record_step(state_dir(key, home) / 'state.sqlite', 'init', 'done',
                {'release': pin['version'], 'changed': len(changes), 'git': report['git']})
    with sqlite3.connect(state_dir(key, home) / 'state.sqlite') as db:  # O2a: a new instance starts `installed`
        _mode.write(db, 'installed', 'init', 'kage init')
    report['applied'] = True
    return report


def adopt(deployment_dir, *, home=None, clock=time.time):
    """Record that this existing deployment is onboarded: one row in its own state store; nothing else changes."""
    from kage import app
    try:
        a = app.App(deployment_dir)
        a.verify()
        db = Path(home or Path.home()) / a.value('hub.DEFAULT_DB')
    except (app.AssemblyError, profile.ProfileError) as exc:
        raise InitError(f'deployment_refused: {exc}') from None
    if not db.parent.is_dir():
        raise InitError(f'state_directory_missing: {db.parent}')
    record_step(db, 'init', 'adopted', {'release': kage.__version__, 'owner': a.deployment.owner.key}, clock=clock)
    return {'adopted': str(a.dir), 'db': str(db), 'owner': a.deployment.owner.key}
