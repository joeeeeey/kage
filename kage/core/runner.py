"""The detached wrapper of ONE dispatched agent run (stdlib only, no chat, no store).

The supervisor starts it as a new session (own process group, stdin /dev/null, cwd = the task workspace). It runs the
provider CLI given after `--` with stdin /dev/null, raw stdout -> <run>/events.jsonl and stderr -> <run>/stderr.log
(both 0600), waits, and writes <run>/exit.json atomically, so the outcome survives a supervisor restart.
SIGTERM/SIGINT/SIGHUP are forwarded to the child; a stop kills the whole group.

While waiting it renders new complete lines of events.jsonl into the masked <run>/readable.log about once a second,
through the run-log module the deployment hands in (`load_run_log()`, kage.core.runlog underneath). The child still
writes straight to the file, so a renderer problem can never block or kill the child; the supervisor and a terminal
tail render the same way under the same lock if this one cannot."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

RENDER_EVERY_SECONDS = 1.0


def _write_json(path, data):
    tmp = path.with_name('.' + path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _provider(run_dir, command, run_log):
    found = run_log.provider_of(run_dir) if run_log else None
    if found:
        return found
    name = Path(command[0]).name if command else ''
    return 'claude' if name.startswith('claude') else 'codex'


def _render(run_log, run_dir, provider):
    if run_log is None:
        return
    try:
        run_log.update(run_dir, provider)
    except Exception:  # noqa: BLE001
        pass


def _stderr_tail(run_dir):
    try:
        with open(run_dir / 'stderr.log', 'rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 4096))
            return stream.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def main(argv, load_run_log=lambda: None):
    """`load_run_log()`: the run-log module (update / finish / provider_of) or None; rendering is optional."""
    os.umask(0o077)
    if len(argv) < 4 or argv[0] != '--run-dir' or argv[2] != '--':
        print('usage: dispatch_runner.py --run-dir DIR -- COMMAND...', file=sys.stderr)
        return 2
    run_dir, command = Path(argv[1]), argv[3:]
    if not run_dir.is_dir() or run_dir.is_symlink() or run_dir.stat().st_mode & 0o077:
        print('run_dir_must_be_a_private_0700_directory', file=sys.stderr)
        return 2
    out = os.open(run_dir / 'events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    err = os.open(run_dir / 'stderr.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    started = time.time()
    stopped = {'signal': None}
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    except OSError as exc:
        _write_json(run_dir / 'exit.json', {'exit_code': 127, 'error': type(exc).__name__, 'started_at': started,
                                            'ended_at': time.time(), 'stopped': False})
        return 127
    _write_json(run_dir / 'child.json', {'pid': child.pid, 'runner_pid': os.getpid(), 'started_at': started})

    def forward(signum, _frame):
        stopped['signal'] = signum
        try:
            child.send_signal(signum)
        except OSError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, forward)
    run_log = load_run_log()
    provider = _provider(run_dir, command, run_log)
    while True:
        try:
            code = child.wait(timeout=RENDER_EVERY_SECONDS)
            break
        except subprocess.TimeoutExpired:
            _render(run_log, run_dir, provider)
    exit_info = {'exit_code': code, 'signal': -code if code < 0 else None, 'started_at': started,
                 'ended_at': time.time(), 'stopped': stopped['signal'] is not None}
    _write_json(run_dir / 'exit.json', exit_info)  # first: the outcome must never wait for rendering
    if run_log is not None:
        try:
            run_log.finish(run_dir, exit_info, provider, stderr_tail=_stderr_tail(run_dir))
        except Exception:  # noqa: BLE001
            pass
    return 0

