"""Readable logs of agent runs. Stdlib only: no chat, no store.

A run directory (`runs/<run-id>/`, 0700) holds the provider's raw stdout in `events.jsonl` (Codex `exec --json` JSONL
or Claude `-p --output-format stream-json`). This module renders it into `readable.log`, one masked line per step in
the owner's language and clock (labels and phrases from the catalog, `runlog.*`), e.g. `14:03:12 · <cmd> · git status`.

Rendering is incremental and idempotent: `.readable.state.json` keeps the byte offset already rendered plus small
counters (steps, last summary, tool ids, `markers`: the provider-level overload / reconnect / stream-disconnect /
API-retry events a provider-health monitor counts), and an fcntl lock (`.readable.lock`) makes the detached runner, the
supervisor, a terminal tail and anything else safe to call `update()` concurrently. Bodies never leave the run
directory; callers that need a summary read the masked state / lines from here.

Masking happens before truncation, on every rendered string: known token formats, bearer and Authorization headers,
URL credentials, private-key blocks and `NAME=value` assignments whose name contains TOKEN / KEY / SECRET / PASSWORD /
CREDENTIAL (also JSON `"api_key": "..."` and `--token X` style flags), and one-time verification codes next to their
label (验证码 123456, MFA code: 123456, `--token-code 123456`).
"""
from __future__ import annotations
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import time

from kage import locale

READABLE = 'readable.log'
STATE = '.readable.state.json'
LOCK = '.readable.lock'
EVENTS = 'events.jsonl'
SUMMARY_LIMIT = 200
PROGRESS_LIMIT = 80
MAX_CHUNK = 4 * 1024 * 1024
MAX_TOOL_IDS = 200
MASK = '***'
# Provider-level trouble in a Codex error event / a Claude API error (never tool output): overload, reconnects,
# stream disconnects, 5xx / 429, timeouts. A Claude `api_retry` always counts.
HEALTH_MARKER_RE = re.compile(r'overload|high demand|reconnect|stream\s*(?:disconnect|error|closed)|disconnected|'
                              r'retrying|temporarily unavailable|service unavailable|too many requests|rate[ _-]?limit|'
                              r'\b(?:429|500|502|503|504|529)\b|timed? ?out|timeout', re.I)

LABEL_KINDS = ('start', 'say', 'cmd', 'exit', 'edit', 'tool', 'error', 'retry', 'result', 'todo', 'note', 'raw', 'end')
STEP_KINDS = ('cmd', 'tool', 'edit')
SUMMARY_KINDS = ('say', 'cmd', 'exit', 'edit', 'tool', 'error', 'retry', 'result', 'todo')
EDIT_TOOLS = ('Edit', 'MultiEdit', 'Write', 'NotebookEdit')

# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

_UP = r'(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIALS?|COOKIE|APIKEY)'
_LOW = r'(?:token|key|secret|password|passwd|passphrase|credentials?|cookie|apikey|pwd)'
_CAMEL = r'(?:Token|Key|Secret|Password|Passwd|Passphrase|Credentials?|Cookie)'
# A name whose segment (split on _ . -) is a secret word, or a camelCase name ending in one:
# GITHUB_TOKEN, api_key, client-secret, apiKey, accessToken. Never `monkey` or `keyboard`.
_SECRET_NAME = (r'(?<![A-Za-z0-9_.-])(?:(?:[A-Za-z0-9]+[_.-])*(?:' + _UP + '|' + _LOW + r')(?:[_.-][A-Za-z0-9]+)*'
                r'|[a-z][a-z0-9]*' + _CAMEL + r'(?:[A-Z][A-Za-z0-9]*)?)(?![A-Za-z0-9])')
_RANDOM = r'(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{16,}'
MASK_RES = (
    # Whole private-key blocks (or a dangling BEGIN line when the block was cut).
    (re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)', re.S),
     '-----PRIVATE KEY ' + MASK + '-----'),
    # Authorization / cookie headers (curl -H, raw HTTP, JSON headers).
    (re.compile(r'(?i)\b((?:proxy-)?authorization|cookie|set-cookie|x-api-key|api-key|x-auth-token)(["\']?\s*[:=]\s*["\']?)'
                r'(?:(?:bearer|basic|token)\s+)?[^\s"\',;]+'), lambda m: m[1] + m[2] + MASK),
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}'), 'Bearer ' + MASK),
    # Credentials inside URLs: scheme://user:secret@host
    (re.compile(r'([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@', re.I), lambda m: m[1] + MASK + '@'),
    # Known token formats.
    (re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{6,}'), 'xox?-' + MASK),
    (re.compile(r'\bxapp-[A-Za-z0-9-]{6,}'), 'xapp-' + MASK),
    (re.compile(r'\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{12,}'), 'sk-' + MASK),
    (re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'), 'AKIA' + MASK),
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{16,}'), 'gh_' + MASK),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{16,}'), 'github_pat_' + MASK),
    (re.compile(r'\bglpat-[A-Za-z0-9_-]{16,}'), 'glpat-' + MASK),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{30,}'), 'AIza' + MASK),
    # Linear / Notion / 1Password service-account style prefixes (random bodies only, not identifiers).
    (re.compile(r'\b(lin_api|lin_oauth|ntn|secret|ops)_' + _RANDOM), lambda m: m[1] + '_' + MASK),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*'), 'eyJ' + MASK),
    # JSON/dict keys: "api_key": "value", 'password': 'x'
    (re.compile(r'(["\']?' + _SECRET_NAME + r'["\']?\s*:\s*)(["\'])(?:\\.|(?!\2).)*\2'),
     lambda m: m[1] + m[2] + MASK + m[2]),
    # YAML-ish unquoted values: password: hunter2hunter
    (re.compile(r'(' + _SECRET_NAME + r'\s*:\s*)(?!["\'*])([^\s,;]{6,})'), lambda m: m[1] + MASK),
    # Shell/env assignments: export GITHUB_TOKEN=..., OPENAI_API_KEY="...", password=hunter2
    (re.compile(r'(' + _SECRET_NAME + r'\s*=(?!=)\s*)("[^"]*"|\'[^\']*\'|[^\s"\'&;|]+)'), lambda m: m[1] + MASK),
    # CLI flags: --token X, --api-key=X, --password X (not --key-file, --author, --password-stdin)
    (re.compile(r'(?i)(--(?:[a-z0-9-]*-)?(?:token|key|secret|password|passwd|credentials?|auth)(?:=|\s+))(?!-)'
                r'("[^"]*"|\'[^\']*\'|\S+)'), lambda m: m[1] + MASK),
    # Chinese labels: 密码：xxx / 密钥=xxx
    (re.compile(r'((?:密码|口令|密钥|令牌)\s*[:=：]\s*)\S+'), lambda m: m[1] + MASK),
    # One-time verification codes next to their label: 验证码 123456 · MFA code: 123-456 · OTP=1234
    (re.compile(r'(?i)((?<![A-Za-z])(?:验证码|校验码|动态码|动态口令|安全码|one[- ]?time (?:pass)?code|verification code|'
                r'security code|sign[- ]?in code|log[- ]?in code|auth(?:entication)? code|passcode|otp|totp|'
                r'mfa(?: code)?|2fa(?: code)?)["\']?\s*(?:is|是|为|[:=：])?\s*["\']?)([A-Za-z0-9]{2,10}(?:[ -][A-Za-z0-9]{2,10})?)'),
     lambda m: m[1] + MASK if re.search(r'\d', m[2]) else m[0]),
    # ...and CLI flags carrying one: --token-code 123456, --mfa-code=123456, --otp 123456
    (re.compile(r'(?i)(--(?:[a-z0-9-]*-)?(?:code|otp|totp|mfa|passcode)(?:=|\s+))(?!-)("[^"]*"|\'[^\']*\'|\S+)'),
     lambda m: m[1] + MASK),
)
MENTION_RE = re.compile(r'<[@!#][^>]*>|(?<![\w<])@(?:here|channel|everyone)\b', re.I)


def mask(text):
    """Mask secret-looking values; safe to call repeatedly (idempotent on already-masked text)."""
    text = str(text or '')
    for regex, repl in MASK_RES:
        text = regex.sub(repl, text)
    return text


def short(text, limit=SUMMARY_LIMIT):
    """One masked line: whitespace collapsed, Slack mention syntax removed, truncated with …"""
    text = MENTION_RE.sub('', mask(text))
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:max(1, limit - 1)].rstrip() + '…'


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _event_time(event, seen_at):
    stamp = event.get('timestamp') if isinstance(event, dict) else None
    if isinstance(stamp, str) and stamp:
        text = stamp.strip().replace('Z', '+00:00')
        text = re.sub(r'(\.\d{6})\d+', r'\1', text)
        try:
            parsed = dt.datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    return seen_at


def _first_line(text):
    for line in str(text or '').splitlines():
        if line.strip():
            return line.strip()
    return ''


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(str(c.get('text') or '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
    return ''


class Renderer:
    """Stateful per run; `feed` turns one raw event into zero or more entries."""

    def __init__(self, provider, state=None, log=None):
        self.provider = provider if provider in ('codex', 'claude', 'grok') else 'codex'
        self.log = log or RunLog()
        self.msg = self.log.msg
        state = state or {}
        self.steps = int(state.get('steps') or 0)
        self.tools = dict(state.get('tools') or {})
        self.started_cmds = list(state.get('started_cmds') or [])
        self.markers = int(state.get('markers') or 0)

    def snapshot(self):
        return {'steps': self.steps, 'tools': dict(list(self.tools.items())[-MAX_TOOL_IDS:]),
                'started_cmds': self.started_cmds[-MAX_TOOL_IDS:], 'markers': self.markers}

    def _marker(self, text, always=False):
        if always or HEALTH_MARKER_RE.search(str(text or '')):
            self.markers += 1

    def _entry(self, at, kind, text, step=False):
        if step:
            self.steps += 1
        return {'at': at, 'kind': kind, 'text': short(text), 'step': self.steps if step else None}

    def feed(self, event, seen_at):
        if not isinstance(event, dict):
            return []
        at = _event_time(event, seen_at)
        try:
            return {'claude': self._claude, 'codex': self._codex, 'grok': self._grok}[self.provider](event, at)
        except Exception:  # noqa: BLE001 - a surprising event shape never breaks the log
            return [self._entry(at, 'raw', str(event.get('type') or 'event'))]

    def _grok(self, event, at):
        from kage.adapters.providers.grok import Grok
        info = Grok().parse_run_log(json.dumps(event))
        if info['cli_ok'] is False:
            message = ' '.join(info['errors'])
            self._marker(message, always=True)
            return [self._entry(at, 'error', message)]
        if info['final'] is not None:
            return [self._entry(at, 'result', info['final'])]
        return []

    # ---- Codex `exec --json` ----
    def _codex(self, e, at):
        kind = e.get('type')
        if kind == 'thread.started':
            return [self._entry(at, 'start', self.msg('runlog.codex_start', id=str(e.get('thread_id') or '')[:8]))]
        if kind == 'turn.completed':
            return [self._entry(at, 'result', self.msg('runlog.turn_done'))]
        if kind == 'turn.failed':
            error = e.get('error') or {}
            message = error.get('message') if isinstance(error, dict) else error
            self._marker(message)
            return [self._entry(at, 'error', message)]
        if kind == 'error':
            self._marker(e.get('message'))
            return [self._entry(at, 'error', e.get('message') or 'error')]
        if kind not in ('item.started', 'item.completed', 'item.updated'):
            return []
        item = e.get('item') or {}
        itype, iid = item.get('type'), str(item.get('id') or '')
        done = kind == 'item.completed'
        if itype in ('agent_message', 'assistant_message'):
            return [self._entry(at, 'say', item.get('text'))] if done and item.get('text') else []
        if itype == 'command_execution':
            command = item.get('command') or ''
            if kind == 'item.started':
                self.started_cmds.append(iid)
                return [self._entry(at, 'cmd', '$ ' + str(command), step=True)]
            if not done:
                return []
            out = []
            if iid not in self.started_cmds:
                out.append(self._entry(at, 'cmd', '$ ' + str(command), step=True))
            code = item.get('exit_code')
            tail = ''
            if code not in (0, None):
                lines = [ln for ln in str(item.get('aggregated_output') or '').splitlines() if ln.strip()]
                tail = ' · ' + lines[-1].strip() if lines else ''
            status = f'exit {code}' if code is not None else str(item.get('status') or self.msg('runlog.ended'))
            out.append(self._entry(at, 'exit', status + tail))
            return out
        if itype == 'file_change':
            if not done:
                return []
            changes = item.get('changes') or []
            text = ', '.join(f"{c.get('kind', 'update')} {c.get('path', '?')}" for c in changes[:6] if isinstance(c, dict))
            if len(changes) > 6:
                text += f' …(+{len(changes) - 6})'
            if item.get('status') == 'failed':
                text += self.msg('runlog.edit_failed')
            return [self._entry(at, 'edit', text or self.msg('runlog.file_change'), step=True)]
        if itype == 'mcp_tool_call':
            if kind == 'item.started' or (done and iid not in self.tools):
                self.tools[iid] = 'mcp'
                args = item.get('arguments')
                text = f"{item.get('server', '?')}.{item.get('tool', '?')} {json.dumps(args, ensure_ascii=False) if args else ''}"
                out = [self._entry(at, 'tool', text, step=True)]
            else:
                out = []
            if done and (item.get('error') or item.get('status') == 'failed'):
                err = item.get('error')
                out.append(self._entry(at, 'error', (err.get('message') if isinstance(err, dict) else err) or self.msg('runlog.mcp_failed')))
            return out
        if itype == 'web_search':
            return [self._entry(at, 'tool', 'web_search ' + str(item.get('query') or ''), step=True)] if done else []
        if itype == 'todo_list':
            items = [i for i in item.get('items') or [] if isinstance(i, dict)]
            if not items:
                return []
            finished = sum(1 for i in items if i.get('completed'))
            nxt = next((i.get('text') for i in items if not i.get('completed')), '')
            return [self._entry(at, 'todo', self.msg('runlog.done_of', done=finished, total=len(items)) + (self.msg('runlog.next', text=nxt) if nxt else ''))]
        if itype == 'error':
            if done:
                self._marker(item.get('message'))
            return [self._entry(at, 'error', item.get('message') or 'error')] if done else []
        return []  # reasoning and unknown item types stay out of the readable log

    # ---- Claude `-p --output-format stream-json --verbose` ----
    def _claude(self, e, at):
        kind = e.get('type')
        if kind == 'system':
            sub = e.get('subtype')
            if sub == 'init':
                return [self._entry(at, 'start', self.msg('runlog.claude_start', id=str(e.get('session_id') or '')[:8], model=e.get('model') or ''))]
            if sub == 'api_retry':
                self._marker(None, always=True)
                status = e.get('error_status')
                return [self._entry(at, 'retry', self.msg('runlog.api_retry', attempt=e.get('attempt') or '')
                                                + (f' · HTTP {status}' if status else '')
                                                + (f" · {e.get('error')}" if e.get('error') else ''))]
            if sub == 'compact_boundary':
                return [self._entry(at, 'note', self.msg('runlog.compacted'))]
            if sub == 'hook_response' and str(e.get('outcome') or 'success') not in ('success', 'ok'):
                return [self._entry(at, 'note', f"hook {e.get('hook_name') or ''} {e.get('outcome')}")]
            return []
        if kind == 'assistant':
            message = e.get('message') or {}
            out = []
            if e.get('error') or e.get('is_api_error_message'):
                text = _content_text(message.get('content')) or e.get('error')
                self._marker(f"{e.get('error') or ''} {text or ''}")
                out.append(self._entry(at, 'error', text))
                return out
            for block in message.get('content') or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get('type')
                if btype == 'text' and str(block.get('text') or '').strip():
                    out.append(self._entry(at, 'say', block['text']))
                elif btype == 'tool_use':
                    out.append(self._tool_use(block, at))
            return out
        if kind == 'user':
            message = e.get('message') or {}
            out = []
            blocks = message.get('content') if isinstance(message.get('content'), list) else []
            for block in blocks:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    entry = self._tool_result(block, at)
                    if entry:
                        out.append(entry)
            return out
        if kind == 'result':
            ok = not e.get('is_error') and e.get('subtype') == 'success'
            parts = [self.msg('runlog.succeeded') if ok else self.msg('runlog.failed_with', subtype=e.get('subtype') or 'error')]
            if e.get('num_turns') is not None:
                parts.append(self.msg('runlog.turns', n=e['num_turns']))
            if e.get('duration_ms') is not None:
                parts.append(self.log.duration(float(e['duration_ms']) / 1000))
            text = ' · '.join(parts)
            body = e.get('result') if isinstance(e.get('result'), str) else ' '.join(str(x) for x in e.get('errors') or [])
            if body:
                text += ' · ' + body
            return [self._entry(at, 'result' if ok else 'error', text)]
        return []

    def _tool_use(self, block, at):
        name = str(block.get('name') or 'tool')
        args = block.get('input') if isinstance(block.get('input'), dict) else {}
        tid = str(block.get('id') or '')
        if name == 'Bash':
            self.tools[tid] = 'cmd'
            return self._entry(at, 'cmd', '$ ' + str(args.get('command') or ''), step=True)
        if name in EDIT_TOOLS:
            self.tools[tid] = 'edit:' + name
            return self._entry(at, 'edit', f"{name} {args.get('file_path') or args.get('notebook_path') or ''}", step=True)
        self.tools[tid] = 'tool:' + name
        if name == 'Read':
            detail = args.get('file_path') or ''
        elif name in ('Grep', 'Glob'):
            detail = ' '.join(str(x) for x in (args.get('pattern'), args.get('path')) if x)
        elif name == 'WebFetch':
            detail = args.get('url') or ''
        elif name == 'WebSearch':
            detail = args.get('query') or ''
        elif name in ('Task', 'Agent'):
            detail = args.get('description') or args.get('subagent_type') or ''
        elif name == 'TodoWrite':
            todos = [t for t in args.get('todos') or [] if isinstance(t, dict)]
            active = next((t.get('content') for t in todos if t.get('status') == 'in_progress'), '')
            detail = self.msg('runlog.done_of', done=sum(1 for t in todos if t.get('status') == 'completed'), total=len(todos)) + (f' · {active}' if active else '')
        elif name == 'Skill':
            detail = args.get('skill') or args.get('command') or ''
        else:
            detail = json.dumps(args, ensure_ascii=False, sort_keys=True) if args else ''
        return self._entry(at, 'tool', f'{name} {detail}'.strip(), step=True)

    def _tool_result(self, block, at):
        tid = str(block.get('tool_use_id') or '')
        kind = self.tools.pop(tid, None)
        text = _content_text(block.get('content'))
        error = bool(block.get('is_error'))
        if kind == 'cmd':
            code = 0
            if error:
                match = re.search(r'exit code (\d+)', text, re.I)
                code = int(match[1]) if match else 1
                lines = [ln for ln in text.splitlines() if ln.strip() and not re.match(r'(?i)exit code \d+\s*$', ln.strip())]
                return self._entry(at, 'exit', f'exit {code}' + (' · ' + lines[-1].strip() if lines else ''))
            return self._entry(at, 'exit', 'exit 0')
        if error:
            name = (kind or 'tool').split(':', 1)[-1]
            return self._entry(at, 'error', self.msg('runlog.tool_failed', name=name, line=_first_line(text) or self.msg('runlog.failed')))
        return None


def _feed_line(renderer, raw, seen_at):
    raw = raw.strip()
    if not raw:
        return []
    if not raw.startswith('{'):
        return [renderer._entry(seen_at, 'raw', raw)]
    try:
        event = json.loads(raw)
    except ValueError:
        return [renderer._entry(seen_at, 'raw', raw[:400])]
    return renderer.feed(event, seen_at)


# ---------------------------------------------------------------------------
# Incremental files (runner / supervisor / terminal tail)
# ---------------------------------------------------------------------------

def _open_private(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


@contextlib.contextmanager
def _locked(run_dir, blocking):
    fd = _open_private(Path(run_dir) / LOCK, os.O_WRONLY | os.O_CREAT)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_state(run_dir):
    try:
        with open(_open_private(Path(run_dir) / STATE, os.O_RDONLY), encoding='utf-8') as stream:
            state = json.load(stream)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(run_dir, state):
    path = Path(run_dir) / STATE
    tmp = path.with_name(path.name + '.tmp')
    fd = _open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(state, stream, ensure_ascii=False)
    os.replace(tmp, path)


def _append(run_dir, lines):
    if not lines:
        return
    fd = _open_private(Path(run_dir) / READABLE, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        stream.write(''.join(line + '\n' for line in lines))


def provider_of(run_dir, default=None):
    try:
        meta = json.loads((Path(run_dir) / 'meta.json').read_text(encoding='utf-8'))
        if meta.get('provider') in ('codex', 'claude', 'grok'):
            return meta['provider']
    except (OSError, ValueError):
        pass
    return default


def read_lines(run_dir, limit=None):
    """Lines of readable.log (all, or the last `limit`). Never creates files."""
    try:
        with open(_open_private(Path(run_dir) / READABLE, os.O_RDONLY), encoding='utf-8', errors='replace') as stream:
            lines = stream.read().splitlines()
    except OSError:
        return []
    return lines[-limit:] if limit else lines


def read_from(path, offset, limit=262144):
    """(text, new_offset) of complete lines after byte `offset` of a private file; ('', offset) if none."""
    try:
        fd = _open_private(Path(path), os.O_RDONLY)
    except OSError:
        return '', offset
    with os.fdopen(fd, 'rb') as stream:
        size = stream.seek(0, os.SEEK_END)
        if offset > size:
            offset = 0
        stream.seek(offset)
        data = stream.read(limit)
    end = data.rfind(b'\n')
    if end < 0:
        return '', offset
    return data[:end + 1].decode('utf-8', 'replace'), offset + end + 1


class RunLog:
    """The owner's words and clock for run logs, and the rendering that uses them (render_text, update, finish)."""

    def __init__(self, language='en', tz=dt.timezone.utc):
        self.language, self.tz = language, tz
        self.labels = {kind: self.msg('runlog.label.' + kind) for kind in LABEL_KINDS}

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def clock(self, ts):
        return dt.datetime.fromtimestamp(float(ts), self.tz).strftime('%H:%M:%S')

    def duration(self, seconds):
        seconds = max(0, int(seconds or 0))
        if seconds < 60:
            return self.msg('runlog.seconds', s=seconds)
        minutes, seconds = divmod(seconds, 60)
        if minutes < 60:
            return self.msg('runlog.minutes_seconds', m=minutes, s=seconds) if seconds else self.msg('runlog.minutes', m=minutes)
        hours, minutes = divmod(minutes, 60)
        return self.msg('runlog.hours_minutes', h=hours, m=minutes)

    def format_entry(self, entry):
        return f"{self.clock(entry['at'])} · {self.labels.get(entry['kind'], entry['kind'])} · {entry['text']}"

    def summary_of(self, entry, limit=PROGRESS_LIMIT):
        return self.msg('runlog.summary', label=self.labels.get(entry['kind'], entry['kind']), text=short(entry['text'], limit))

    def is_step_line(self, line):
        return any(f' · {self.labels[k]} · ' in line for k in ('cmd', 'tool', 'edit', 'exit', 'error', 'say', 'result', 'retry'))

    def render_text(self, provider, text, seen_at=None):
        """Render a whole raw stream in memory (no files written). Returns (lines, renderer)."""
        seen_at = time.time() if seen_at is None else seen_at
        renderer = Renderer(provider, None, self)
        lines = []
        for raw in str(text or '').splitlines():
            for entry in _feed_line(renderer, raw, seen_at):
                lines.append(self.format_entry(entry))
        return lines, renderer

    def update(self, run_dir, provider=None, now=None, blocking=False):
        """Render new complete lines of events.jsonl into readable.log. Returns the state, or the
        last saved state when another process holds the lock (non-blocking callers never wait)."""
        run_dir = Path(run_dir)
        if not run_dir.is_dir() or run_dir.is_symlink():
            return {}
        now = time.time() if now is None else now
        with _locked(run_dir, blocking) as got:
            if not got:
                return read_state(run_dir)
            state = read_state(run_dir)
            provider = provider or state.get('provider') or provider_of(run_dir, 'codex')
            state['provider'] = provider
            offset = int(state.get('offset') or 0)
            try:
                fd = _open_private(run_dir / EVENTS, os.O_RDONLY)
            except FileNotFoundError:
                return state
            with os.fdopen(fd, 'rb') as stream:
                # Codex events carry no timestamp: use when we saw them, but never later than the file's last
                # write (a log rendered long after the run, e.g. by a terminal tail, keeps roughly the right times).
                mtime = os.fstat(stream.fileno()).st_mtime
                now = min(now, mtime) if mtime else now
                size = stream.seek(0, os.SEEK_END)
                if size < offset:  # truncated/replaced: start over from the top
                    offset = 0
                    state.update(offset=0, steps=0, tools={}, started_cmds=[], markers=0)
                stream.seek(offset)
                chunk = stream.read(MAX_CHUNK)
            renderer = Renderer(provider, state, self)
            end = chunk.rfind(b'\n')
            if end < 0:
                if len(chunk) < MAX_CHUNK:
                    return state  # an incomplete line: wait for its newline
                consumed = len(chunk)  # one oversized line: skip it rather than stall forever
                entries = [renderer._entry(now, 'note', self.msg('runlog.skipped_line'))]
            else:
                consumed = end + 1
                entries = []
                for raw in chunk[:consumed].decode('utf-8', 'replace').splitlines():
                    entries.extend(_feed_line(renderer, raw, now))
            lines = [self.format_entry(e) for e in entries]
            _append(run_dir, lines)
            state.update(renderer.snapshot())
            state['offset'] = offset + consumed
            state['lines'] = int(state.get('lines') or 0) + len(lines)
            for entry in entries:
                if entry['kind'] in SUMMARY_KINDS:
                    if entry['kind'] == 'exit' and state.get('last_kind') == 'cmd' and state.get('last_summary'):
                        # "<cmd>: $ git status → exit 0" reads better than a bare "<exit>: exit 0"
                        state['last_summary'] = f"{state['last_summary']} → {short(entry['text'], 40)}"
                    else:
                        state['last_summary'] = self.summary_of(entry)
                    state['last_kind'] = entry['kind']
                    state['last_at'] = entry['at']
            state['updated_at'] = now
            _write_state(run_dir, state)
            return state


    def finish(self, run_dir, exit_info=None, provider=None, now=None, stderr_tail=None):
        """Render the rest, then append one end line exactly once (idempotent)."""
        run_dir = Path(run_dir)
        state = self.update(run_dir, provider, now, blocking=True)
        if not run_dir.is_dir() or state.get('finished'):
            return state
        now = time.time() if now is None else now
        with _locked(run_dir, True):
            state = read_state(run_dir) or state
            if state.get('finished'):
                return state
            exit_info = exit_info or {}
            code = exit_info.get('exit_code')
            started = exit_info.get('started_at')
            ended = exit_info.get('ended_at') or now
            parts = [f'exit {code}' if code is not None else self.msg('runlog.process_ended')]
            if exit_info.get('stopped'):
                parts.append(self.msg('runlog.stopped'))
            if started:
                parts.append(self.msg('runlog.took', duration=self.duration(float(ended) - float(started))))
            lines = []
            if code not in (0, None) and stderr_tail:
                last = [ln for ln in str(stderr_tail).splitlines() if ln.strip()]
                if last:
                    lines.append(self.format_entry({'at': ended, 'kind': 'error', 'text': short(last[-1])}))
            lines.append(self.format_entry({'at': ended, 'kind': 'end', 'text': ' · '.join(parts)}))
            _append(run_dir, lines)
            state.update(finished=True, finished_at=now, lines=int(state.get('lines') or 0) + len(lines),
                         exit_code=code)
            _write_state(run_dir, state)
            return state
