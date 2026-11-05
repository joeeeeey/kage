"""The owner's self-introduction store (onboarding design §4.2, O3a).

Every version of the raw introduction is a file `<root>/<YYYYMMDDTHHMMSS>.md` (0600) under the owner's private
directory `~/.local/share/kage/private/<key>/intro/` (0700, owned by the user; refused otherwise), listed in
`index.json` with its sha256, length and time. The text never goes into a repository, a task workspace, `/tmp`, a card
or the store: extraction reads it from here and records only its hash. A text already stored (same hash) is not stored
again.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from kage.core.secrets import check_secret_free

MAX_CHARS = 50_000
INDEX = 'index.json'


class IntroError(ValueError):
    """A refused introduction or an unsafe store."""


def root(key, home=None):
    """The owner's intro directory (not created)."""
    return Path(home or Path.home()) / '.local' / 'share' / 'kage' / 'private' / key / 'intro'


def private_dirs(path):
    """Create `path` and its parents below `.local/share/kage` 0700, and refuse a directory that is a link, not the
    user's or open to others."""
    levels = (path.parent.parent, path.parent, path)  # private/, private/<key>/, intro/
    levels[0].parent.mkdir(parents=True, exist_ok=True)
    for d in levels:
        if not d.exists():
            d.mkdir(mode=0o700)
            os.chmod(d, 0o700)  # mkdir's mode is masked by the umask
    for d in levels:
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise IntroError('intro_directory_not_private')
    return path


def _write(path, data):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _index(d):
    path = d / INDEX
    if not path.exists():
        return {'versions': []}
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077:
        raise IntroError('intro_index_not_private')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('versions'), list):
        raise IntroError('intro_index_unreadable')
    return value


def versions(key, home=None):
    """The stored versions, oldest first: [{file, sha256, chars, as_of}]; empty when there is none."""
    d = root(key, home)
    if not d.exists():
        return []
    return _index(private_dirs(d))['versions']


def add(key, text, home=None, now=None):
    """Store one introduction; returns its index entry with `stored` (False when the same text is already there)."""
    if not isinstance(text, str) or not text.strip():
        raise IntroError('intro_is_empty')
    if len(text) > MAX_CHARS:
        raise IntroError(f'intro_is_longer_than_{MAX_CHARS}_characters')
    try:
        check_secret_free(text)
    except ValueError as exc:
        raise IntroError(str(exc)) from None
    now = time.time() if now is None else now
    d = private_dirs(root(key, home))
    index = _index(d)
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    for v in index['versions']:
        if v['sha256'] == digest:
            return {**v, 'stored': False}
    name = time.strftime('%Y%m%dT%H%M%S', time.localtime(now)) + '.md'
    while (d / name).exists() or any(v['file'] == name for v in index['versions']):
        now += 1
        name = time.strftime('%Y%m%dT%H%M%S', time.localtime(now)) + '.md'
    entry = {'file': name, 'sha256': digest, 'chars': len(text), 'as_of': now}
    _write(d / name, text)
    index['versions'].append(entry)
    _write(d / INDEX, json.dumps(index, indent=2, sort_keys=True) + '\n')
    return {**entry, 'stored': True}


def read(key, sha256=None, home=None):
    """(entry, text) of the latest version, or of the version with that hash; IntroError when there is none or the file
    no longer matches its hash."""
    found = versions(key, home)
    if sha256 is not None:
        found = [v for v in found if v['sha256'] == sha256]
    if not found:
        raise IntroError('no_intro_stored' if sha256 is None else 'unknown_intro_version')
    entry = found[-1]
    path = root(key, home) / entry['file']
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077:
        raise IntroError('intro_file_not_private')
    text = path.read_text(encoding='utf-8')
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != entry['sha256']:
        raise IntroError('intro_file_changed_since_stored')
    return entry, text
