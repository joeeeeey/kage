"""`kage doctor` (onboarding step O1a): read-only checks of an installation. It posts nothing and changes nothing.

Each check returns `{id, required, ok, detail, fix}`; `ok` is True, False or None (skipped: not configured here, or
`--offline`). The installation is healthy when no required check is False. The only network calls are two
`auth.test` reads (the bot and the user token), and only without `--offline`. The secrets store (O1c3) is read
locally, for presence only; no value is printed.

The checks read through a `Probe` (Slack identity, files, the clock, processes), so the same code runs on a live
install and in tests with fakes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.request

from kage.onboard import identity
from kage.onboard.tokens import TokenRefused
from kage.ports.secrets import SLACK_APP, SLACK_BOT, SLACK_USER, SecretError

# Bot-token scopes for what the runtime calls today (chat.postMessage / chat.update, conversations.history / replies /
# info in private channels and IMs, reactions, users.info / lookupByEmail). Reported, never required: an older app can
# hold the same rights under other scopes, and a missing one already fails loudly where it is used.
BOT_SCOPES = ('chat:write', 'groups:history', 'groups:read', 'im:history', 'im:read', 'reactions:read',
              'reactions:write', 'users:read')
# The user token (speaking as the owner, the owner's DMs, search.messages).
USER_SCOPES = ('chat:write', 'search:read', 'im:history', 'groups:history', 'users:read')
TOKEN_RE = re.compile(r'\b(?:xox[abposr]-[0-9A-Za-z-]{10,}|xapp-[0-9A-Za-z-]{10,}|sk-[A-Za-z0-9_-]{20,})')
HEARTBEAT_STALE_SECONDS = 120
CLOCK_SKEW_WARN_SECONDS = 60


def check(id_: str, required: bool, ok: bool | None, detail: str, fix: str = '') -> dict[str, Any]:
    return {'id': id_, 'required': required, 'ok': ok, 'detail': detail, 'fix': fix if ok is False else ''}


class Probe:
    """The facts the checks read. The live probe reads the machine; tests pass their own."""

    def __init__(self, transport=None, environ=None, runner=subprocess.run, which=shutil.which, clock=time.time):
        self.transport, self.environ, self.runner, self.which, self.clock = transport, environ or os.environ, runner, which, clock

    def auth_test(self, token: str) -> tuple[dict[str, Any], dict[str, str] | None]:
        """(payload, lower-cased headers or None) of Slack's auth.test. The identity comes through the deployment's
        transport (its offline guard and error handling); only after that call went through, a second plain read
        fetches the granted scopes (`x-oauth-scopes`), which the transport does not return (None when it fails)."""
        if self.transport is None:
            raise RuntimeError('no_transport: a live auth.test needs the deployment transport (its offline guard)')
        payload = self.transport.api_call('auth.test', token)
        try:
            req = urllib.request.Request('https://slack.com/api/auth.test', headers={'Authorization': f'Bearer {token}'})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return payload, {k.lower(): v for k, v in resp.headers.items()}
        except Exception:  # noqa: BLE001 - the scopes are then unknown, the identity check stands
            return payload, None

    def launchd_state(self, label: str) -> str | None:
        """'running', 'loaded' or None (not loaded) for a launchd job of this user; None off macOS."""
        if not self.which('launchctl'):
            return None
        out = self.runner(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        return 'running' if re.search(r'^\s*state = running\s*$', out.stdout, re.M) else 'loaded'

    def codex_liveness_tool(self) -> str | None:
        """0.78.5 (F17): what tells whether a Codex session is live: lsof, else a readable /proc; None = nothing."""
        if self.which('lsof'):
            return 'lsof'
        return '/proc' if os.path.isdir('/proc/self/fd') else None

    def module_version(self, python: str, module: str) -> str | None:
        """`module.__version__` as the interpreter `python` imports it (the services' own: user site included), or
        None when it does not import."""
        try:
            out = self.runner([python, '-c', f'import sys, {module}; sys.stdout.write(str({module}.__version__))'],
                              capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return (out.stdout.strip()[:40] or None) if out.returncode == 0 else None

    def version(self, binary: str) -> str | None:
        path = self.which(binary)
        if not path:
            return None
        try:
            out = self.runner([path, '--version'], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return (out.stdout or out.stderr).strip().splitlines()[0][:80] if out.returncode == 0 else None

    def system_timezone(self) -> str | None:
        target = Path('/etc/localtime')
        try:
            link = os.readlink(target)
        except OSError:
            return self.environ.get('TZ')
        return link.split('zoneinfo/', 1)[1] if 'zoneinfo/' in link else None


def voice(owner_key: str, secret_store, home=None) -> str | None:
    """F6: the voice of a deployment `kage init` wrote — its answers file, else whether the secrets store holds a user
    token ('as_owner' / 'assistant'); None for a deployment whose own transport reads its tokens."""
    if secret_store is None:
        return None
    from kage.onboard import init  # noqa: PLC0415 - only for its config path
    try:
        found = json.loads((init.config_dir(owner_key, home) / 'answers.json').read_text(encoding='utf-8'))
        if (found.get('slack') or {}).get('voice') in ('assistant', 'as_owner'):
            return found['slack']['voice']
    except (OSError, ValueError, AttributeError):
        pass
    try:
        return 'as_owner' if secret_store.has(SLACK_USER) else 'assistant'
    except SecretError:
        return None


def slack_checks(owner, probe: Probe, tokens: dict[str, Callable[[], str]], offline: bool,
                 voice: str | None = None) -> list[dict[str, Any]]:
    """`voice`: 'assistant' skips the user token (the bot speaks; there is none by design), 'as_owner' requires it,
    None (an adopted deployment) checks a configured one."""
    out = []
    for kind, required in (('bot', True), ('user', voice == 'as_owner')):
        name = f'slack.{kind}_identity'
        if offline:
            out.append(check(name, required, None, 'skipped (--offline)'))
            continue
        if kind == 'user' and voice == 'assistant':
            out.append(check(name, False, None, 'skipped (voice assistant: the bot speaks, no user token)'))
            continue
        try:
            token = tokens[kind]()
            if not token:
                raise LookupError('empty')
        except Exception as exc:  # noqa: BLE001 - a missing token is the finding
            out.append(check(name, required, False if required else None, f'no {kind} token ({type(exc).__name__})',
                             f'store the {kind} token where the deployment reads it'))
            continue
        required = True  # a configured token must be the right identity (the user token is optional, not a wrong one)
        try:
            payload, headers = probe.auth_test(token)
        except Exception as exc:  # noqa: BLE001
            code = re.fullmatch(r'Slack API error: ([a-z_]+)', str(exc))
            out.append(check(name, required, False, f"auth.test failed ({'slack:' + code[1] if code else type(exc).__name__})",
                             'check the network and the token'))
            continue
        expected_user = owner.assistant_slack_bot_user if kind == 'bot' else owner.slack_user
        problems = []
        if not payload.get('ok'):
            problems.append(f"slack:{payload.get('error')}")
        if owner.slack_workspace and payload.get('team_id') != owner.slack_workspace:
            problems.append('another_workspace')
        if expected_user and payload.get('user_id') != expected_user:
            problems.append('not_the_expected_identity')
        if kind == 'bot' and not payload.get('bot_id'):
            problems.append('not_a_bot_token')
        if kind == 'user' and payload.get('bot_id'):
            problems.append('a_bot_token_where_the_owner_token_belongs')
        out.append(check(name, required, not problems, 'the configured identity' if not problems else ', '.join(problems),
                         'use a token of the configured app / owner in the configured workspace'))
        wanted = BOT_SCOPES if kind == 'bot' else USER_SCOPES
        if headers is None:
            out.append(check(f'slack.{kind}_scopes', False, None, 'scopes unknown (the header read failed)'))
            continue
        granted = {s.strip() for s in headers.get('x-oauth-scopes', '').split(',') if s.strip()}
        missing = [s for s in wanted if s not in granted]
        out.append(check(f'slack.{kind}_scopes', False, not missing if not problems else None,
                         f'{len(wanted) - len(missing)} / {len(wanted)} of the usual scopes' +
                         (f'; not granted: {", ".join(missing)}' if missing else ''),
                         'add the scopes to the app and reinstall it' if missing else ''))
    return out


def state_checks(db_path: Path, db: sqlite3.Connection | None) -> list[dict[str, Any]]:
    out = []
    directory = db_path.parent
    try:
        mode = stat.S_IMODE(directory.stat().st_mode)
        out.append(check('state.private', True, mode & 0o077 == 0, f'state directory mode {mode:04o}',
                         f'chmod 700 {directory}'))
    except OSError as exc:
        out.append(check('state.private', True, False, f'state directory unreadable ({type(exc).__name__})',
                         'create the state directory (0700)'))
    if db is not None:
        try:
            result = db.execute('PRAGMA quick_check').fetchone()[0]
            out.append(check('state.database', True, result == 'ok', f'quick_check: {result}',
                             'restore the store from its latest backup'))
        except sqlite3.Error as exc:
            out.append(check('state.database', True, False, f'unreadable ({type(exc).__name__})', 'restore the store'))
    return out


def runtime_checks(db: sqlite3.Connection | None, health: dict[str, Any] | None, probe: Probe) -> list[dict[str, Any]]:
    out = []
    if db is not None:
        try:
            started = db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime'").fetchone()
            row = db.execute('SELECT heartbeat FROM runtime WHERE id=1').fetchone() if started else None
        except sqlite3.Error as exc:
            out.append(check('runtime.heartbeat', True, False, f'store unreadable ({type(exc).__name__})',
                             'check the state directory and the store files'))
        else:
            age = probe.clock() - row[0] if row and row[0] else None
            if not started:  # O1c4: a store init made (its onboarding row) before the runtime ever ran
                out.append(check('runtime.heartbeat', True, None, 'the runtime has not started yet'))
            else:
                out.append(check('runtime.heartbeat', True, age is not None and age < HEARTBEAT_STALE_SECONDS,
                                 f'supervisor heartbeat {int(age)} s ago' if age is not None else
                                 'no supervisor heartbeat',
                                 'start the services (the supervisor writes a heartbeat every few seconds)'))
    if health:
        providers = ((health.get('providers') or {}).get('providers') or [])
        degraded = sorted(p.get('provider', '?') for p in providers if p.get('status') not in ('healthy', 'ok'))
        out.append(check('runtime.providers', False, not degraded, f'{len(providers) - len(degraded)} / {len(providers)} '
                         'providers healthy' + (f'; degraded: {", ".join(degraded)}' if degraded else ''), 'see `health`'))
        counts = (health.get('stranded') or {}).get('counts') or {}
        open_stranded = sum(v for k, v in counts.items() if k not in ('handled', 'held'))
        held = counts.get('held', 0)
        out.append(check('runtime.stranded', False, not open_stranded, f'{open_stranded} stranded deliveries open' +
                         (f', {held} held (their task is paused)' if held else ''),
                         'see `health` (the supervisor re-delivers them)'))
        blocked = len((health.get('stuck_claims') or {}).get('blocked') or [])
        out.append(check('runtime.stuck_claims', False, not blocked, f'{blocked} stuck claims blocking delivery',
                         'see `health` and the stuck-claim card'))
        dest = health.get('card_destination') or {}
        verdicts = [d.get('verdict') for d in dest.get('destinations') or []]
        public = [v for v in verdicts if v not in ('private', 'im')]
        ok = dest.get('status') == 'ok' and not public and not dest.get('config_error')
        out.append(check('cards.destination_private', True, ok if verdicts or dest else None,
                         f"{len(verdicts) - len(public)} / {len(verdicts)} card destinations private, "
                         f"{dest.get('refused_24h', 0)} posts refused in 24 h (cached verdicts)",
                         'run card-destination-check --refresh and fix the decision channel'))
    return out


def deployment_root(profile_dir: Path | None, owner_key: str, environ) -> Path | None:
    """The deployment repository (the directory holding services.json): the services' KAGE_DEPLOYMENT_DIR, else the
    profile directory's parent, else the conventional checkout `<KAGE_HOME>/deploy-<owner key>`; None when none has it."""
    candidates = []
    if environ.get('KAGE_DEPLOYMENT_DIR'):
        candidates.append(Path(environ['KAGE_DEPLOYMENT_DIR']).expanduser().parent)
    if profile_dir is not None:
        candidates.append(Path(profile_dir).expanduser().parent)
    home = Path(environ.get('KAGE_HOME') or Path(environ.get('HOME') or Path.home()) / '.local/share/kage')
    candidates.append(home.expanduser() / f'deploy-{owner_key}')
    return next((c for c in candidates if (c / 'services.json').is_file()), None)


def service_checks(root: Path | None, probe: Probe, heartbeat: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if root is None:
        return [check('services.running', True, None, 'no deployment repository found (services.json)')]
    path = root / 'services.json'
    try:
        services = json.loads(path.read_text(encoding='utf-8')).get('services') or {}
    except (OSError, ValueError):
        return [check('services.running', True, None, f'services.json unreadable in {root}')]
    receiver = receiver_checks(services.get('receiver'), probe)
    if not probe.which('launchctl'):  # 0.77.1 (F10b): no launchd here; the deployment's own supervisor runs them
        ok = heartbeat.get('ok') if heartbeat else None
        return [check('services.running', True, ok, 'no launchd: the followup service judged by the supervisor '
                      f"heartbeat ({heartbeat.get('detail') if heartbeat else 'not read'})",
                      "start the services under the deployment's supervisor (scripts/kage.py run, serve receiver, "
                      'serve dashboard)')] + receiver
    states = {name: probe.launchd_state(spec.get('label', '')) for name, spec in services.items()}
    down = [name for name, state in states.items() if state != 'running']
    return [check('services.running', True, not down, f'{len(states) - len(down)} / {len(states)} running' +
                  (f'; not running: {", ".join(sorted(down))}' if down else ''),
                  'load the launchd jobs (`services start` for a deployment `kage init` wrote; else the deployment\'s own '
                  'service tool)')] + receiver


def receiver_checks(spec: dict[str, Any] | None, probe: Probe) -> list[dict[str, Any]]:
    """0.77.1 (F10c): the Socket Mode receiver needs the `websockets` package (>= 11) in the interpreter it runs with."""
    if not spec:
        return []
    python = spec.get('python') or sys.executable
    found = probe.module_version(python, 'websockets')
    try:
        ok = found is not None and int(found.split('.')[0]) >= 11
    except ValueError:
        ok = False
    return [check('receiver.websockets', True, ok, f'websockets {found} for {python}' if found else
                  f'websockets does not import for {python}', f'{python} -m pip install --user "websockets>=11"')]


def provider_checks(providers: list[str], probe: Probe) -> list[dict[str, Any]]:
    found = {p: probe.version(p) for p in providers}
    any_ok = any(found.values())
    out = [check(f'provider.{p}', False, v is not None, v or 'not found on PATH', f'install the {p} CLI and log in')
           for p, v in found.items()] + [check('provider.any', True, any_ok, 'at least one agent CLI runs' if any_ok
                                               else 'no agent CLI found', 'install claude or codex')]
    if found.get('codex'):  # 0.78.5 (F17): without a liveness tool a Codex task's events wait forever
        tool = probe.codex_liveness_tool()
        out.append(check('liveness.codex', True, tool is not None, f'Codex session liveness via {tool}' if tool else
                         'neither lsof nor /proc: Codex sessions read as unknown, their events are never dispatched',
                         'install lsof'))
    return out


def clock_checks(owner, probe: Probe) -> list[dict[str, Any]]:
    system = probe.system_timezone()
    same = system == owner.timezone if system and owner.timezone else None
    return [check('clock.timezone', False, same, f'system {system or "unknown"}, owner {owner.timezone}',
                  'set the owner timezone in owner.json (or the system one)')]


def secret_checks(roots: list[Path]) -> list[dict[str, Any]]:
    if not roots:
        return [check('secrets.not_in_deployment', True, None, 'no deployment directory found')]
    hits = 0
    files = 0
    seen = set()
    paths = [p for root in roots for p in sorted(Path(root).rglob('*'))]
    for path in paths:
        if path.resolve() in seen:
            continue
        seen.add(path.resolve())
        if '.git' in path.parts or not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        files += 1
        try:
            hits += len(TOKEN_RE.findall(path.read_text(encoding='utf-8', errors='replace')))
        except OSError:
            continue
    return [check('secrets.not_in_deployment', True, hits == 0, f'{files} files scanned, {hits} token-like strings',
                  'move the token into the secrets store and rotate it')]


def identity_checks(owner, state_dir: Path) -> list[dict[str, Any]]:
    """The profile's Slack identity against the verified record (O1c3); an adopted profile is skipped."""
    fix = 'verify the owner: the user token, or the one-time code sent to the bot as a direct message'
    try:
        ok, detail = identity.status(owner, state_dir)
    except (TokenRefused, OSError, ValueError) as exc:
        return [check('owner.identity', True, False,
                      f"identity record unreadable ({getattr(exc, 'code', type(exc).__name__)})", fix)]
    return [check('owner.identity', True, ok, detail, fix)]


def port_checks(store) -> list[dict[str, Any]]:
    """Kage's own transport reads its tokens from the secrets store: bot and app-level token present (user optional).
    A deployment whose own transport reads its tokens is skipped."""
    if store is None:
        return [check('secrets.port', False, None, "the deployment's own transport reads its tokens")]
    try:
        present = {name: store.has(name) for name in (SLACK_BOT, SLACK_APP, SLACK_USER)}
    except SecretError as exc:
        return [check('secrets.port', True, False, f'{store.kind} store: {exc}', 'check the secrets store')]
    detail = f'{store.kind} store: ' + ', '.join(f"{n} {'present' if p else 'absent'}" for n, p in present.items())
    return [check('secrets.port', True, present[SLACK_BOT] and present[SLACK_APP], detail,
                  'store the bot and app-level tokens through onboarding')]


def run(owner, *, probe: Probe, tokens, db_path: Path, db: sqlite3.Connection | None, health: dict[str, Any] | None,
        profile_dir: Path | None, providers=('claude', 'codex'), offline=False, secret_store=None,
        voice: str | None = None) -> dict[str, Any]:
    root = deployment_root(profile_dir, getattr(owner, 'key', 'owner'), probe.environ)
    scan = [d for d in (root, Path(profile_dir).expanduser() if profile_dir else None) if d is not None and d.is_dir()]
    runtime = runtime_checks(db, health, probe)
    heartbeat = next((c for c in runtime if c['id'] == 'runtime.heartbeat'), None)
    checks = (slack_checks(owner, probe, tokens, offline, voice) + state_checks(db_path, db) + runtime
              + service_checks(root, probe, heartbeat) + provider_checks(list(providers), probe) + clock_checks(owner, probe)
              + secret_checks(scan) + port_checks(secret_store) + identity_checks(owner, Path(db_path).parent))
    failed = [c['id'] for c in checks if c['required'] and c['ok'] is False]
    warnings = [c['id'] for c in checks if not c['required'] and c['ok'] is False]
    return {'ok': not failed, 'failed': failed, 'warnings': warnings, 'checks': checks}


def text(report: dict[str, Any]) -> str:
    marks = {True: 'ok  ', False: 'FAIL', None: 'skip'}
    lines = []
    for c in report['checks']:
        mark = marks[c['ok']] if c['required'] or c['ok'] is not False else 'warn'
        lines.append(f"{mark}  {c['id']}: {c['detail']}" + (f"  → {c['fix']}" if c['fix'] else ''))
    lines.append('doctor: ' + ('all required checks pass' if report['ok'] else 'failed: ' + ', '.join(report['failed'])))
    return '\n'.join(lines)
