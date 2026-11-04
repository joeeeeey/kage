"""The secrets port (O1c3): where a deployment's tokens live, one store per owner key. Answers files, deployment
files, git, argv, logs and reports only ever hold a reference (`kage-secret://<key>/<name>`), never a value.

- `get(name)` the value or None; `put(name, value)`; `delete(name)` True when something was removed;
  `has(name)`.
- Adapters: `KeychainStore` (macOS, through /usr/bin/security: the value goes only on stdin, `security -i`, and comes
  back on stdout); `FileStore` (one 0600 file per name in a 0700 directory, for a machine without a Keychain);
  `MemoryStore` (tests). `for_owner(key)` picks the Keychain on macOS, else the file store; KAGE_SECRETS_BACKEND
  (`file` / `memory`) overrides that, so test harnesses on a scratch HOME never reach the user's Keychain.
- Names are checked. A value is token characters only (letters, digits and `._~+/=:@%-`, starting with a letter or a
  digit), so the stdin line needs no quoting and no value reads as an option. No value appears in a repr, an error or
  a log line.
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

SCHEME = 'kage-secret://'
KEY_RE = re.compile(r'[a-z][a-z0-9_]{0,31}')  # the owner key of kage.profile
NAME_RE = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}')
VALUE_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~+/=:@%-]{0,2047}')
NAME_PATH_RE = re.compile(r'[A-Za-z0-9_./-]+')  # a keychain path written into the stdin line without quoting
SLACK_BOT, SLACK_APP, SLACK_USER = 'slack-bot-token', 'slack-app-token', 'slack-user-token'  # the store's names
SECURITY = '/usr/bin/security'
BACKEND_ENV = 'KAGE_SECRETS_BACKEND'
MAX_LINE = 4000  # security -i reads about 4 KB per line and runs the rest as another command (checked on macOS)
NOT_FOUND = 44  # security(1): the item could not be found


class SecretError(RuntimeError):
    """A store refused or failed; the message never carries a value."""


def check_key(key):
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise SecretError('secret_owner_key_invalid')
    return key


def check_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SecretError('secret_name_invalid')
    return name


def check_value(value):
    if not isinstance(value, str) or not VALUE_RE.fullmatch(value):
        raise SecretError('secret_value_refused_empty_too_long_or_not_a_plain_token')
    return value


def ref(key, name):
    """The non-secret reference an answers file or a report may hold."""
    return f'{SCHEME}{check_key(key)}/{check_name(name)}'


def parse_ref(text):
    """(key, name) of a reference; SecretError for anything else."""
    if not isinstance(text, str) or not text.startswith(SCHEME) or text.count('/') != 3:
        raise SecretError('secret_reference_invalid')
    key, name = text[len(SCHEME):].split('/')
    return check_key(key), check_name(name)


class Store:
    kind = None

    def __init__(self, key):
        self.key = check_key(key)

    def get(self, name):
        raise NotImplementedError

    def put(self, name, value):
        raise NotImplementedError

    def delete(self, name):
        raise NotImplementedError

    def has(self, name):
        return self.get(name) is not None

    def ref(self, name):
        return ref(self.key, name)

    def __repr__(self):
        return f'<{type(self).__name__} owner={self.key}>'


class MemoryStore(Store):
    kind = 'memory'

    def __init__(self, key):
        super().__init__(key)
        self._values = {}

    def get(self, name):
        return self._values.get(check_name(name))

    def put(self, name, value):
        self._values[check_name(name)] = check_value(value)

    def delete(self, name):
        return self._values.pop(check_name(name), None) is not None


class FileStore(Store):
    """`root/<name>`, 0600, in a 0700 directory owned by this user; written through a temp file and a rename, so a
    reader sees the old value or the new one. A symlink, a wider mode or another owner is refused, never repaired."""
    kind = 'file'

    def __init__(self, key, root):
        super().__init__(key)
        self.root = Path(root).expanduser()

    def _dir(self, create):
        if not self.root.exists() and create:
            self.root.mkdir(mode=0o700, parents=True)
        if not self.root.exists():
            return None
        st = os.lstat(self.root)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise SecretError('secret_store_directory_not_private')
        return self.root

    def _file(self, name):
        root = self._dir(create=False)
        if root is None:
            return None
        path = root / check_name(name)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise SecretError('secret_file_not_private')
        return path

    def get(self, name):
        path = self._file(name)
        return None if path is None else path.read_text(encoding='ascii').strip()

    def put(self, name, value):
        check_value(value)
        root = self._dir(create=True)
        self._file(name)  # refuses a symlink or a wider file in the way
        fd, tmp = tempfile.mkstemp(dir=root, prefix='.tmp-')
        try:
            with os.fdopen(fd, 'w', encoding='ascii') as f:
                f.write(value + '\n')
            os.chmod(tmp, 0o600)
            os.replace(tmp, root / check_name(name))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def delete(self, name):
        path = self._file(name)
        if path is None:
            return False
        path.unlink()
        return True


class KeychainStore(Store):
    """Generic-password items, service `kage.<key>`, account = the name. `keychain` is a keychain file (tests and
    checks), else the user's default keychain. `run` is subprocess.run (injected by tests)."""
    kind = 'keychain'

    def __init__(self, key, *, keychain=None, security=SECURITY, run=None):
        super().__init__(key)
        self.service = f'kage.{self.key}'
        self.keychain = None if keychain is None else str(keychain)
        self.security = security
        self._run = run or subprocess.run

    def _call(self, args):
        argv = [self.security, *args, *([self.keychain] if self.keychain else [])]
        return self._run(argv, capture_output=True, text=True, timeout=30)

    @staticmethod
    def _failed(what, proc, detail=True):
        """rc and the first stderr line; a write's stderr is never quoted (a line security split could echo part of
        the value as an unknown command)."""
        lines = (proc.stderr or '').strip().splitlines()[:1] if detail else []
        return SecretError(f'keychain_{what}_failed_rc_{proc.returncode}' + (f': {lines[0][:200]}' if lines else ''))

    def get(self, name):
        proc = self._call(['find-generic-password', '-s', self.service, '-a', check_name(name), '-w'])
        if proc.returncode == NOT_FOUND:
            return None
        if proc.returncode:
            raise self._failed('read', proc)
        return proc.stdout.rstrip('\n')

    def put(self, name, value):
        check_name(name)
        check_value(value)
        line = f'add-generic-password -U -s {self.service} -a {name} -w {value}'
        if self.keychain:
            if not NAME_PATH_RE.fullmatch(self.keychain):
                raise SecretError('keychain_path_needs_quoting')
            line += f' {self.keychain}'
        if len(line) >= MAX_LINE:
            raise SecretError('keychain_write_line_too_long')
        proc = self._run([self.security, '-i'], input=line + '\n', capture_output=True, text=True, timeout=30)
        if proc.returncode:
            raise self._failed('write', proc, detail=False)
        if self.get(name) != value:
            raise SecretError('keychain_write_not_read_back')

    def delete(self, name):
        proc = self._call(['delete-generic-password', '-s', self.service, '-a', check_name(name)])
        if proc.returncode == NOT_FOUND:
            return False
        if proc.returncode:
            raise self._failed('delete', proc)
        return True



def for_owner(key, *, backend=None, home=None):
    """The owner's store: `keychain` on macOS (the default), else `file` under ~/.config/kage/<key>/secrets."""
    check_key(key)
    backend = (backend or os.environ.get(BACKEND_ENV)
               or ('keychain' if sys.platform == 'darwin' and os.path.exists(SECURITY) else 'file'))
    if backend == 'keychain':
        return KeychainStore(key)
    if backend == 'file':
        return FileStore(key, Path(home or Path.home()) / '.config' / 'kage' / key / 'secrets')
    if backend == 'memory':
        return MemoryStore(key)
    raise SecretError('secret_backend_unknown')
