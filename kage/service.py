"""The service launcher: launchd runs `python3 <KAGE_HOME>/current/kage/service.py --deployment DIR NAME`.

DIR is the deployment directory (the profile, `deploy/` of the deployment repository); the directory it sits in holds
the deployment's `kage.lock` (`$KAGE_LOCK` overrides) and `services.json`. The launcher refuses unless the release it
runs from — this file's release, reached through `current` — is the one the lock pins and it verifies (BUNDLE.json,
MANIFEST.sha256 and every listed file's sha256). Then it replaces itself (exec) with the service's program from
services.json `services.NAME`: the interpreter, the program (`script`, relative to `skills`) and its arguments, in its
working directory, with the environment launchd gave it plus where the deployment lives (KAGE_LOCK,
KAGE_DEPLOYMENT_DIR) and where its releases are (KAGE_VERSIONS_DIR).

Exit 64: usage; 78: refused (launchd retries after its throttle interval).

  KAGE_SERVICE_DRY_RUN=1 …   print the plan as JSON (argv, the environment it adds, the directory); run nothing

Standard library only, imported by path: nothing but this file runs before the checks pass.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sys

SHA256_RE = re.compile(r'[0-9a-f]{64}')
IGNORED = ('__pycache__',)


class Refused(Exception):
    """The service must not start (said why)."""


def release_problems(release, pin):
    """What is wrong with a release directory against a lock's `kage` pin ([] when it verifies)."""
    try:
        meta = json.loads((release / 'BUNDLE.json').read_text(encoding='utf-8'))
        manifest = (release / 'MANIFEST.sha256').read_bytes()
    except (OSError, ValueError):
        return ['release_metadata_unreadable']
    problems = [f'bundle_{key}_mismatch' for key in ('version', 'commit', 'tree_sha256') if meta.get(key) != pin.get(key)]
    if hashlib.sha256(manifest).hexdigest() != pin.get('tree_sha256'):
        problems.append('manifest_hash_mismatch')
    listed = {}
    for line in manifest.decode('utf-8', errors='replace').splitlines():
        digest, _, name = line.partition('  ')
        listed[name] = digest
    present = {p.relative_to(release).as_posix() for p in (release / 'kage').rglob('*')
               if p.is_file() and not any(part in IGNORED for part in p.parts)}
    if set(listed) != present:
        problems.append('release_files_differ_from_manifest')
    for name in sorted(set(listed) & present):
        if hashlib.sha256((release / name).read_bytes()).hexdigest() != listed[name]:
            problems.append(f'file_hash_mismatch:{name}')
    return problems


def plan(deployment_dir, name, environ, release=None):
    """(argv, env, workdir) of service NAME, or Refused."""
    deployment_dir = Path(deployment_dir).expanduser().absolute()
    root = deployment_dir.parent
    release = Path(release) if release else Path(__file__).resolve().parents[1]
    lock_file = Path(environ.get('KAGE_LOCK') or root / 'kage.lock')
    try:
        lock = json.loads(lock_file.read_text(encoding='utf-8'))
        services = json.loads((root / 'services.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise Refused(f'deployment_unreadable: {type(exc).__name__}') from None
    pin = lock.get('kage') or {}
    if not isinstance(pin.get('commit'), str) or not SHA256_RE.fullmatch(str(pin.get('tree_sha256'))):
        raise Refused('lock_has_no_kage_pin')
    expected = f"{pin.get('version')}-{pin['commit'][:12]}"
    if release.name != expected:
        raise Refused(f'release_not_pinned: running {release.name}, the lock pins {expected}')
    problems = release_problems(release, pin)
    if problems:
        raise Refused('release_does_not_verify: ' + ', '.join(problems[:5]))
    service = (services.get('services') or {}).get(name)
    if not isinstance(service, dict):
        raise Refused(f'no_service: {name}')
    argv = [service['python'], str(Path(services['skills']) / service['script']), *service.get('args', [])]
    env = {**environ, 'KAGE_LOCK': str(lock_file), 'KAGE_DEPLOYMENT_DIR': str(deployment_dir),
           'KAGE_VERSIONS_DIR': str(release.parent)}
    return argv, env, service['workdir']


def main(argv=None, environ=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = dict(os.environ if environ is None else environ)
    if len(argv) != 3 or argv[0] != '--deployment':
        print('usage: python3 <KAGE_HOME>/current/kage/service.py --deployment DIR NAME', file=sys.stderr)
        return 64
    try:
        program, env, workdir = plan(argv[1], argv[2], environ)
    except (Refused, KeyError, TypeError) as exc:
        print(f'kage service: refused: {exc}', file=sys.stderr)
        return 78
    if environ.get('KAGE_SERVICE_DRY_RUN') == '1':
        added = {k: v for k, v in env.items() if environ.get(k) != v}
        print(json.dumps({'argv': program, 'env_added': added, 'cwd': workdir}, sort_keys=True))
        return 0
    os.chdir(workdir)
    os.execve(program[0], program, env)


if __name__ == '__main__':
    sys.exit(main())
