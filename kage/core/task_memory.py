"""Task workspace memory helpers: notes, agent messages and the status.md rev header. No chat, no store.

status.md convention (P1a). Line 1 of status.md may carry a machine header:

    <!-- rev: 7 · last-writer: codex:01a1b2c3-... · at: 2026-10-18T15:04:05+00:00 -->

`memory-guard check` reports it; `memory-guard bump --expect-rev N` atomically (fcntl lock on
status.md.lock) verifies the header still says N, then writes N+1 with the caller as last
writer. A refused bump means another writer changed status.md in between: re-read, merge, retry.
A missing header counts as rev 0. The owner may edit anything; the header is advisory for agents.

Notes: a session that is not the task's current session writes
`<workspace>/history/notes/<UTC ts>-<session or the owner's key>.md`; the current session merges it into
status.md and resolves the note event. Agent messages (`send-to-task`) are kept in
`<workspace>/history/messages/`. Both are plain Markdown, mode 0600, never secrets.
"""
import contextlib
import datetime as dt
import fcntl
import os
from pathlib import Path
import re

HEADER_RE = re.compile(r'^<!--\s*rev:\s*(\d+)\s*·\s*last-writer:\s*(\S+)\s*·\s*at:\s*(\S+)\s*-->\s*$')
MAX_TEXT = 20000


def utc_stamp(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def utc_iso(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec='seconds')


def _safe_name(text):
    return re.sub(r'[^A-Za-z0-9._-]', '-', str(text))[:60] or 'unknown'


def _write_new(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)
    return path


def _unique(directory, stem):
    path = directory / f'{stem}.md'
    n = 2
    while path.exists():
        path = directory / f'{stem}-{n}.md'
        n += 1
    return path


def clean_text(text):
    text = str(text or '').replace('\r\n', '\n').strip()
    if not text:
        raise ValueError('text_required')
    if len(text) > MAX_TEXT:
        raise ValueError('text_must_be_at_most_20000_characters')
    return text


def write_note(workspace, task_id, author, text, now, current=None):
    """history/notes/<UTC ts>-<author>.md; returns the path."""
    text = clean_text(text)
    author_part = author.split(':', 1)[1] if ':' in author else author
    path = _unique(Path(workspace) / 'history' / 'notes', f'{utc_stamp(now)}-{_safe_name(author_part)}')
    body = (f'# Note\n\n- task: {task_id}\n- author: {author}\n- created: {utc_iso(now)}\n'
            f'- current session when written: {current or "none"}\n\n{text}\n')
    return _write_new(path, body)


def read_header(status_path):
    try:
        with open(status_path, encoding='utf-8') as stream:
            first = stream.readline()
    except OSError:
        return None
    match = HEADER_RE.match(first.strip())
    return {'rev': int(match[1]), 'last_writer': match[2], 'at': match[3]} if match else None


def status_path(workspace):
    return Path(workspace) / 'status.md'


@contextlib.contextmanager
def _locked(path):
    lock = path.with_name(path.name + '.lock')
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def check(workspace, expect=None):
    path = status_path(workspace)
    if not path.exists():
        raise ValueError('status_md_missing')
    header = read_header(path)
    rev = header['rev'] if header else 0
    result = {'ok': expect is None or int(expect) == rev, 'path': str(path), 'rev': rev,
              'last_writer': header and header['last_writer'], 'at': header and header['at'], 'header': bool(header)}
    if expect is not None and not result['ok']:
        result['reason'] = 'status_md_changed_since_rev_%d_reread_and_merge' % int(expect)
    return result


def bump(workspace, writer, now, expect=None):
    path = status_path(workspace)
    if not path.exists():
        raise ValueError('status_md_missing')
    with _locked(path):
        lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
        header = HEADER_RE.match(lines[0].strip()) if lines else None
        rev = int(header[1]) if header else 0
        if expect is not None and int(expect) != rev:
            return {'ok': False, 'path': str(path), 'rev': rev, 'last_writer': header and header[2],
                    'reason': 'status_md_changed_since_rev_%d_reread_and_merge' % int(expect)}
        line = f'<!-- rev: {rev + 1} · last-writer: {writer} · at: {utc_iso(now)} -->\n'
        body = lines[1:] if header else lines
        tmp = path.with_name('.' + path.name + '.tmp')
        mode = path.stat().st_mode & 0o777
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(line + ''.join(body))
        os.replace(tmp, path)
    return {'ok': True, 'path': str(path), 'rev': rev + 1, 'previous_rev': rev, 'last_writer': writer}

class TaskMemory:
    """The helpers that name the owner (an agent message is never their intent)."""

    def __init__(self, owner_name):
        self.owner = owner_name

    def write_message(self, workspace, to_task, sender, from_task, text, now, hops=0):
        text = clean_text(text)
        origin = from_task or sender.split(':', 1)[-1][:8]
        path = _unique(Path(workspace) / 'history' / 'messages', f'{utc_stamp(now)}-from-{_safe_name(origin)}')
        body = (f'# Agent message\n\n- to task: {to_task}\n- from task: {from_task or "none"}\n- sender: {sender}\n'
                f'- created: {utc_iso(now)}\n- hops: {hops}\n- authority: none (agent message; never {self.owner} intent)\n\n{text}\n')
        return _write_new(path, body)
