"""`kage doctor` (onboarding step O1a): read-only checks of an installation. It posts nothing and changes nothing.

Each check returns `{id, required, ok, detail, fix}`; `ok` is True, False or None (skipped: not configured here, or
`--offline`). The installation is healthy when no required check is False. The only network calls are two
`auth.test` reads (the bot and the user token), and only without `--offline`.

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
import time
from typing import Any, Callable
import urllib.request

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


def slack_checks(owner, probe: Probe, tokens: dict[str, Callable[[], str]], offline: bool) -> list[dict[str, Any]]:
    out = []
    for kind, required in (('bot', True), ('user', False)):
        name = f'slack.{kind}_identity'
        if offline:
            out.append(check(name, required, None, 'skipped (--offline)'))
            continue
        try:
            token = tokens[kind]()
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
            row = db.execute('SELECT heartbeat FROM runtime WHERE id=1').fetchone() \
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime'").fetchone() else None
        except sqlite3.Error as exc:
            out.append(check('runtime.heartbeat', True, False, f'store unreadable ({type(exc).__name__})',
                             'check the state directory and the store files'))
        else:
            age = probe.clock() - row[0] if row and row[0] else None
            out.append(check('runtime.heartbeat', True, age is not None and age < HEARTBEAT_STALE_SECONDS,
                             f'supervisor heartbeat {int(age)} s ago' if age is not None else 'no supervisor heartbeat',
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


def service_checks(root: Path | None, probe: Probe) -> list[dict[str, Any]]:
    if root is None:
        return [check('services.running', True, None, 'no deployment repository found (services.json)')]
    path = root / 'services.json'
    try:
        services = json.loads(path.read_text(encoding='utf-8')).get('services') or {}
    except (OSError, ValueError):
        return [check('services.running', True, None, f'services.json unreadable in {root}')]
    states = {name: probe.launchd_state(spec.get('label', '')) for name, spec in services.items()}
    down = [name for name, state in states.items() if state != 'running']
    return [check('services.running', True, not down, f'{len(states) - len(down)} / {len(states)} running' +
                  (f'; not running: {", ".join(sorted(down))}' if down else ''),
                  'load the launchd jobs (the deployment\'s services check / render)')]


def provider_checks(providers: list[str], probe: Probe) -> list[dict[str, Any]]:
    found = {p: probe.version(p) for p in providers}
    any_ok = any(found.values())
    return [check(f'provider.{p}', False, v is not None, v or 'not found on PATH', f'install the {p} CLI and log in')
            for p, v in found.items()] + [check('provider.any', True, any_ok, 'at least one agent CLI runs' if any_ok
                                                else 'no agent CLI found', 'install claude or codex')]


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


def run(owner, *, probe: Probe, tokens, db_path: Path, db: sqlite3.Connection | None, health: dict[str, Any] | None,
        profile_dir: Path | None, providers=('claude', 'codex'), offline=False) -> dict[str, Any]:
    root = deployment_root(profile_dir, getattr(owner, 'key', 'owner'), probe.environ)
    scan = [d for d in (root, Path(profile_dir).expanduser() if profile_dir else None) if d is not None and d.is_dir()]
    checks = (slack_checks(owner, probe, tokens, offline) + state_checks(db_path, db) + runtime_checks(db, health, probe)
              + service_checks(root, probe) + provider_checks(list(providers), probe) + clock_checks(owner, probe)
              + secret_checks(scan))
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
