"""Consumer liveness: is (provider, session) live_idle | live_busy | dead | unknown?

Read-only local probes, cached briefly; no chat, no store, no model. Validated read-only on a
macOS workstation (Claude Code and Codex CLIs, 2026-10).

Claude
  Primary: `claude agents --json` -> [{sessionId, kind, status, pid, cwd, ...}]
  (interactive and background sessions; status idle|busy).
  Cross-check: ~/.claude/sessions/<pid>.json (only *.json; the *.key files next to
  them are never opened). A file counts only if its pid is alive AND its `procStart`
  equals `TZ=UTC LC_ALL=C ps -o lstart=` (rules out pid reuse).
  dead    = agents CLI succeeded, session not listed, and no live session file.
  unknown = listed nowhere by the CLI but a live file exists (e.g. a parked session the
            CLI hides), or the CLI failed and no live file exists.
Codex
  Primary: a process whose name starts with `codex` holds the session rollout file
  (~/.codex/sessions/YYYY/MM/DD/rollout-...-<id>.jsonl) open, from
  `lsof -nP -w -c codex -F pn`. Newer TUIs are frontends of the shared app-server
  daemon, which holds the rollout of every thread a TUI has open; older TUIs and
  `codex exec` hold it themselves. Either counts as live.
  Busy/idle: last of task_started / task_complete / turn_aborted in the rollout tail.
  dead    = lsof succeeded (exit 0, or exit 1 with no output) and nobody holds it.
  unknown = lsof missing/failed/timed out.
Only `dead` lets the dispatcher revive; `unknown` keeps today's delivery.
Context cap: `session_bytes` is the size of a session's on-disk transcript (the Codex rollout above, or
~/.claude/projects/<dir>/<session>.jsonl), stat only — the file is never opened.
"""
from __future__ import annotations
import glob
import json
import os
from pathlib import Path
import re
import subprocess
import time

LIVE_IDLE, LIVE_BUSY, DEAD, UNKNOWN = 'live_idle', 'live_busy', 'dead', 'unknown'
LIVE = (LIVE_IDLE, LIVE_BUSY)
STATES = (LIVE_IDLE, LIVE_BUSY, DEAD, UNKNOWN)
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
ROLLOUT_RE = re.compile(r'/sessions/(?:.+/)?rollout-[^/]*?(' + UUID_RE.pattern + r')\.jsonl$')
TURN_EVENTS = ('task_started', 'task_complete', 'turn_aborted')
PS_ENV = {'TZ': 'UTC', 'LC_ALL': 'C', 'PATH': '/bin:/usr/bin:/usr/sbin:/sbin'}
MAX_SESSION_FILE = 64 * 1024
TAIL_BYTES = 256 * 1024


def _norm(text):
    return ' '.join(str(text or '').split()) or None


def is_live(state):
    return state in LIVE


class Probe:
    """One instance per supervisor; every external probe runs at most once per `ttl`."""

    def __init__(self, runner=subprocess.run, claude_sessions_dir=None, codex_home=None, ttl=20, clock=time.time,
                 claude_bin='claude', lsof_bin='lsof', ps_bin='ps', claude_projects_dir=None):
        self.runner, self.ttl, self.clock = runner, ttl, clock
        self.claude_sessions_dir = Path(claude_sessions_dir or Path.home() / '.claude/sessions')
        self.claude_projects_dir = Path(claude_projects_dir or Path.home() / '.claude/projects')
        self.codex_home = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        self.claude_bin, self.lsof_bin, self.ps_bin = claude_bin, lsof_bin, ps_bin
        self._cache = {}
        self._rollouts = {}
        self._transcripts = {}
        self._size_misses = {}

    # ---- plumbing ----
    def _cached(self, key, fn):
        hit = self._cache.get(key)
        now = self.clock()
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (now, value)
        return value

    def clear(self):
        self._cache.clear()

    def _run(self, cmd, timeout=10, env=None):
        try:
            r = self.runner(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                            **({'env': env} if env else {}))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        return r.returncode, r.stdout or ''

    def ps_start(self, pid):
        """Process start time in UTC/C locale (same format as Claude's procStart); None if gone."""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        if pid <= 1:
            return None
        out = self._run([self.ps_bin, '-p', str(pid), '-o', 'lstart='], timeout=5, env=PS_ENV)
        return _norm(out[1]) if out and out[0] == 0 else None

    # ---- Claude ----
    def claude_agents(self):
        """{sessionId: entry} from `claude agents --json`, or None when the CLI failed."""
        def probe():
            out = self._run([self.claude_bin, 'agents', '--json'], timeout=15)
            if not out or out[0] != 0:
                return None
            try:
                items = json.loads(out[1])
            except ValueError:
                return None
            if not isinstance(items, list):
                return None
            found = {}
            for item in items:
                if isinstance(item, dict) and UUID_RE.fullmatch(str(item.get('sessionId') or '')):
                    found[item['sessionId']] = {k: item.get(k) for k in ('kind', 'status', 'state', 'pid', 'cwd')}
            return found
        return self._cached('claude_agents', probe)

    def claude_files(self):
        """{sessionId: entry} for ~/.claude/sessions/<pid>.json whose pid is alive with the same start time."""
        def probe():
            found = {}
            try:
                names = sorted(os.listdir(self.claude_sessions_dir))
            except OSError:
                return found
            for name in names:
                if not re.fullmatch(r'\d+\.json', name):
                    continue  # never touch the *.key credential files
                path = self.claude_sessions_dir / name
                try:
                    if path.is_symlink() or path.stat().st_size > MAX_SESSION_FILE:
                        continue
                    data = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                sid, pid = str(data.get('sessionId') or ''), data.get('pid')
                if not UUID_RE.fullmatch(sid) or str(pid) != name[:-5]:
                    continue
                started = self.ps_start(pid)
                if not started or started != _norm(data.get('procStart')):
                    continue
                found[sid] = {'pid': int(pid), 'pid_start': started, 'kind': data.get('kind'),
                              'status': data.get('status'), 'parked': bool(data.get('parkedJobId'))}
            return found
        return self._cached('claude_files', probe)

    def _claude(self, session):
        agents, files = self.claude_agents(), self.claude_files()
        entry = (agents or {}).get(session)
        if entry is not None and entry.get('pid') and entry.get('status'):
            # status idle|busy|waiting (waiting = blocked on a terminal prompt: still that consumer's turn).
            state = LIVE_IDLE if entry.get('status') == 'idle' else LIVE_BUSY
            return {'state': state, 'source': 'claude_agents', 'pid': entry.get('pid'), 'kind': entry.get('kind'),
                    'status': entry.get('status')}
        if entry is not None:
            # Listed without a live process (a background session whose process exited but which the
            # daemon still tracks as working/blocked/done): never revive over it automatically.
            return {'state': UNKNOWN, 'source': 'claude_agents_without_process', 'kind': entry.get('kind'),
                    'agent_state': entry.get('state')}
        file_entry = files.get(session)
        if agents is None:
            if file_entry and not file_entry['parked']:
                state = LIVE_IDLE if file_entry['status'] == 'idle' else LIVE_BUSY
                return {'state': state, 'source': 'session_file', 'pid': file_entry['pid'], 'kind': file_entry['kind'],
                        'status': file_entry['status']}
            return {'state': UNKNOWN, 'source': 'claude_agents_failed', 'pid': file_entry and file_entry['pid']}
        if file_entry:
            # The CLI omits it (e.g. parked) but its process is alive: never revive over it.
            return {'state': UNKNOWN, 'source': 'session_file_not_listed', 'pid': file_entry['pid'],
                    'parked': file_entry['parked'], 'status': file_entry['status']}
        return {'state': DEAD, 'source': 'claude_agents+session_files'}

    # ---- Codex ----
    def codex_holders(self):
        """{session: [pids]} of codex processes holding rollouts open, or None when lsof failed."""
        def probe():
            out = self._run([self.lsof_bin, '-nP', '-w', '-c', 'codex', '-F', 'pn'], timeout=20)
            if out is None:
                return None
            rc, text = out
            if rc not in (0, 1) or (rc == 1 and text.strip()):
                return None
            held, pid = {}, None
            for line in text.splitlines():
                if line.startswith('p'):
                    try:
                        pid = int(line[1:])
                    except ValueError:
                        pid = None
                elif line.startswith('n') and pid is not None:
                    match = ROLLOUT_RE.search(line[1:])
                    if match:
                        held.setdefault(match[1], [])
                        if pid not in held[match[1]]:
                            held[match[1]].append(pid)
            return held
        return self._cached('codex_holders', probe)

    def rollout_path(self, session):
        cached = self._rollouts.get(session)
        if cached and cached.exists():
            return cached
        matches = sorted(glob.glob(str(self.codex_home / 'sessions' / '*' / '*' / '*' / f'rollout-*{session}.jsonl')))
        path = Path(matches[-1]) if matches else None
        if path:
            self._rollouts[session] = path
        return path

    def codex_turn_state(self, session):
        """'busy' when the last turn event is task_started, 'idle' otherwise; None without a rollout."""
        path = self.rollout_path(session)
        if path is None:
            return None
        try:
            with path.open('rb') as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - TAIL_BYTES))
                lines = stream.read().decode('utf-8', 'replace').splitlines()[1:]
        except OSError:
            return None
        for line in reversed(lines):
            if '"event_msg"' not in line:
                continue
            try:
                payload = json.loads(line).get('payload') or {}
            except ValueError:
                continue
            if payload.get('type') in TURN_EVENTS:
                return 'busy' if payload['type'] == 'task_started' else 'idle'
        return 'idle'

    def _codex(self, session):
        holders = self.codex_holders()
        if holders is None:
            return {'state': UNKNOWN, 'source': 'lsof_failed'}
        pids = holders.get(session)
        if not pids:
            return {'state': DEAD, 'source': 'lsof_rollout_not_open'}
        turn = self.codex_turn_state(session)
        return {'state': LIVE_BUSY if turn == 'busy' else LIVE_IDLE, 'source': 'lsof_rollout_open',
                'pid': pids[0], 'holders': pids, 'turn': turn}

    # ---- P38: session transcript size (context cap) ----
    def claude_transcript_path(self, session):
        cached = self._transcripts.get(session)
        if cached and cached.exists():
            return cached
        matches = sorted(glob.glob(str(self.claude_projects_dir / '*' / f'{session}.jsonl')))
        path = Path(matches[-1]) if matches else None
        if path:
            self._transcripts[session] = path
        return path

    def session_bytes(self, provider, session):
        """Size of the session's transcript on disk, or None when it cannot be found (then it cannot be resumed)."""
        if not UUID_RE.fullmatch(str(session or '')):
            return None
        missed = self._size_misses.get((provider, session))
        if missed is not None and self.clock() - missed < 3 * self.ttl:
            return None  # a missing transcript is looked up again at most every 3 ttl (no glob per supervisor cycle)
        path = self.rollout_path(session) if provider == 'codex' else self.claude_transcript_path(session)
        try:
            size = path.stat().st_size if path else None
        except OSError:
            size = None
        if size is None:
            self._size_misses[(provider, session)] = self.clock()
        else:
            self._size_misses.pop((provider, session), None)
        return size

    # ---- public ----
    def check(self, provider, session):
        if not UUID_RE.fullmatch(str(session or '')):
            return {'state': UNKNOWN, 'source': 'invalid_session', 'provider': provider, 'session': session}

        def probe():
            result = self._claude(session) if provider == 'claude' else self._codex(session)
            return {**result, 'provider': provider, 'session': session, 'checked_at': self.clock()}
        return self._cached(('session', provider, session), probe)
