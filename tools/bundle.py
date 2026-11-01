#!/usr/bin/env python3
"""Build and verify a Kage release bundle: the one artifact a deployment installs.

  bundle.py build REF OUTDIR   archive the `kage/` package at git REF (a tag for a release) into
                               OUTDIR/kage-<version>-<commit12>.tar.gz and print its sha256
  bundle.py verify DIR         check an extracted bundle: every file matches MANIFEST.sha256, nothing is
                               missing or extra, and BUNDLE.json's tree_sha256 is the sha256 of the manifest

A bundle holds `kage/`, `MANIFEST.sha256` (one "sha256  path" line per file, sorted) and `BUNDLE.json`
({"name", "version", "commit", "ref", "tree_sha256"}). The tarball is deterministic: sorted entries, mtime 0,
owner 0:0, modes 0644 / 0755. A deployment pins the version, the commit and tree_sha256 in its lock file and
installs the bundle read-only under <versions dir>/<version>-<commit12>/.
"""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
IGNORED = ('__pycache__',)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def manifest_of(directory):
    lines = []
    for path in sorted(Path(directory, 'kage').rglob('*')):
        if path.is_file() and not any(part in IGNORED for part in path.parts):
            lines.append(f'{sha256(path.read_bytes())}  {path.relative_to(directory).as_posix()}')
    return ('\n'.join(lines) + '\n').encode()


def verify(directory):
    """Return a list of problems with an extracted bundle (empty when it verifies)."""
    directory = Path(directory)
    problems = []
    try:
        manifest = (directory / 'MANIFEST.sha256').read_bytes()
        meta = json.loads((directory / 'BUNDLE.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return [f'unreadable bundle metadata: {exc}']
    if meta.get('tree_sha256') != sha256(manifest):
        problems.append('BUNDLE.json tree_sha256 is not the sha256 of MANIFEST.sha256')
    if manifest_of(directory) != manifest:
        problems.append('the files under kage/ do not match MANIFEST.sha256')
    return problems


def extract(tar, dest):
    """tar.extractall with the 'data' filter; on a Python without extraction filters (3.9, as macOS ships it) the same
    refusal by hand first: regular files and directories only, relative names that stay inside `dest`."""
    if hasattr(tarfile, 'data_filter'):
        tar.extractall(dest, filter='data')
        return
    root = os.path.realpath(dest)
    for member in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, member.name))
        if not (member.isfile() or member.isdir()) or os.path.isabs(member.name) or '..' in Path(member.name).parts \
                or not (target == root or target.startswith(root + os.sep)):
            raise SystemExit(f'refusing archive member {member.name!r}: not a plain file or directory inside it')
    tar.extractall(dest)


def build(ref, outdir):
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--verify', f'{ref}^{{commit}}'],
                            capture_output=True, text=True, check=True).stdout.strip()
    archive = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', commit, 'kage'],
                             capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            extract(tar, tmp)
        init = Path(tmp, 'kage/__init__.py').read_text(encoding='utf-8')
        version = re.search(r"^__version__ = '([^']+)'$", init, re.M).group(1)
        manifest = manifest_of(tmp)
        meta = {'name': 'kage', 'version': version, 'commit': commit, 'ref': ref, 'tree_sha256': sha256(manifest)}
        Path(tmp, 'MANIFEST.sha256').write_bytes(manifest)
        Path(tmp, 'BUNDLE.json').write_text(json.dumps(meta, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        problems = verify(tmp)
        if problems:
            raise SystemExit('bundle does not verify: ' + '; '.join(problems))
        name = f'kage-{version}-{commit[:12]}'
        out = Path(outdir, f'{name}.tar.gz')
        out.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as tar:
            paths = sorted(p for p in Path(tmp).rglob('*') if not any(part in IGNORED for part in p.parts))
            for path in paths:
                info = tar.gettarinfo(str(path), arcname=f'{name}/{path.relative_to(tmp).as_posix()}')
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mode = 0o755 if path.is_dir() or info.mode & 0o111 else 0o644
                if path.is_file():
                    with open(path, 'rb') as fh:
                        tar.addfile(info, fh)
                else:
                    tar.addfile(info)
        with open(out, 'wb') as fh, gzip.GzipFile(filename='', mode='wb', fileobj=fh, mtime=0) as gz:
            gz.write(buf.getvalue())
        print(json.dumps({**meta, 'bundle': str(out), 'bundle_sha256': sha256(out.read_bytes())}, indent=2))


def main(argv):
    if len(argv) == 3 and argv[0] == 'build':
        build(argv[1], argv[2])
        return 0
    if len(argv) == 2 and argv[0] == 'verify':
        problems = verify(argv[1])
        print('\n'.join(problems) if problems else 'bundle verifies')
        return 1 if problems else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
