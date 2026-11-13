"""`kage owner-transfer --to <Slack user id>` (0.79.3, F30): hand an existing deployment to another person of its
workspace. The deployment key, every path, the state database, the config, the rules and the people registry stay.

The new owner proves who they are as at init (kage.onboard.identity, `dm_code`): the command prints a one-time OWNER
CODE and waits up to 10 minutes for that person, and only that person, to send it to the bot as a direct message. Then
it rewrites, with a copy of every file it touches in `<state>/owner-transfer/<UTC time>/` first:

  deploy/owner.json        identities.slack.user and the owner's name / short name (from Slack, or --name /
                           --short-name); the deployment's git records it
  dispatch files           SKILL.md and references/ the dispatched runs read, when they are init's own text
  owner-identity.json      the new verified record, with the names (a later `init --apply` takes them from it)
  decisions.json           owners and active approvers: the old owner replaced by the new one (`--keep-old-owner`: the
                           new one added, the old one stays); owner_names
  access rules             a new version: defaults.owner, defaults.approvers / approver_names_zh and every rule's
                           bounds.approvers, as the console's approver save writes them
  approvers.changes.jsonl  one line: the supervisor posts its notice in the decision channel
  owner-transfer.jsonl     the audit line (state directory)

A failure after the proof puts every file back from the copy. `--dry-run` checks the bot token and the new owner and
prints the plan: nothing is written, no code is made. Host only: refused in an agent session and in a dispatched run.
No terminal needed: the code is one flushed stdout line (`--json`: a JSON line {"owner_code": …}). Afterwards restart
the services (they read the profile when they start) and run doctor (`owner.identity` verified for the new owner).
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

from kage import profile
from kage.core import approvers as _approvers
from kage.onboard import dispatch_files, identity, init, tokens

TIMEOUT = identity.CODE_TTL
POLL_SECONDS = 5
AGENT_ENV = ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID')
BACKUPS, AUDIT = 'owner-transfer', 'owner-transfer.jsonl'
DISPATCH_TEXT = (dispatch_files.SKILL, dispatch_files.RULES, dispatch_files.EXAMPLE_SPEC)


class TransferRefused(ValueError):
    """The transfer was refused (said why); nothing was written, or everything was put back."""


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec='seconds')


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _json(data):
    return (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def _write(path, data, mode=0o600):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.kage-transfer-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def swap(ids, old, new, keep=False):
    """`ids` with `old` replaced in place by `new` (`keep`: `new` added after `old`); no duplicates."""
    out = []
    for u in ids:
        out += [old, new] if u == old and keep else [new] if u == old else [u]
    return list(dict.fromkeys(out))


def new_decisions(raw, old, new, name, keep):
    """The decisions config after the transfer, or None when there is none (cards in the owner's DM)."""
    if not isinstance(raw, dict):
        return None
    out = copy.deepcopy(raw)
    out['approvers'] = swap(list(raw.get('approvers') or [old]), old, new, keep)
    if raw.get('owners') is not None:
        owners = swap(list(raw['owners']), old, new, keep)
        out['owners'] = owners if new in owners else [new, *owners]
        if len(out['owners']) > _approvers.MAX:
            raise TransferRefused(f'at_most_{_approvers.MAX}_owners')
    if isinstance(raw.get('owner_names'), dict) or raw.get('owners') is not None:
        names = dict(raw.get('owner_names') or {})
        if not keep:
            names.pop(old, None)
        names[new] = name
        out['owner_names'] = names
    return out


def new_rules(spec, old, new, name, keep):
    """The next access rules version: the new owner in defaults.owner and in every approver list (or None)."""
    if spec is None:
        return None
    out = copy.deepcopy(spec)
    defaults = out['defaults']
    ids = swap(list(defaults.get('approvers') or []), old, new, keep)
    ids = ids if new in ids else [new, *ids]
    if new in set(defaults.get('hr') or []):
        raise TransferRefused('the_new_owner_is_an_hr_contact_in_the_rules')
    names = dict(defaults.get('approver_names_zh') or {})
    names[new] = name
    defaults.update(owner=new, approvers=ids, approver_names_zh={u: n for u, n in names.items() if u in ids})
    for rule in out.get('rules') or []:
        if isinstance(rule.get('bounds'), dict) and isinstance(rule['bounds'].get('approvers'), list):
            rule['bounds']['approvers'] = list(ids)
    out['version'] = int(spec['version']) + 1
    return out


class Transfer:
    """One transfer of the deployment at `deploy_dir` (its deploy/ directory). `rules`: the access rule engine (load_active
    / validate_spec / install_spec) or None; `authority`: the decisions config check for the NEW owner (load_config)."""

    def __init__(self, *, deploy_dir, state_dir, to, bot_token, decisions_path, rules=None, authority=None, name=None,
                 short_name=None, keep_old_owner=False, call=tokens.slack_call, clock=time.time, sleep=time.sleep,
                 say=print, timeout=TIMEOUT):
        self.deploy, self.state = Path(deploy_dir), Path(state_dir)
        self.root = self.deploy.parent
        self.to, self.bot_token, self.rules, self.authority = to, bot_token, rules, authority
        self.decisions_path = Path(decisions_path) if decisions_path else None
        self.name, self.short_name, self.keep = name, short_name, keep_old_owner
        self.call, self.clock, self.sleep, self.say, self.timeout = call, clock, sleep, say, min(timeout, TIMEOUT)

    def _slack(self, method, params):
        payload, _headers = self.call(method, self.bot_token, params)
        if not isinstance(payload, dict) or payload.get('ok') is not True:
            raise RuntimeError(f"Slack API error: {payload.get('error') if isinstance(payload, dict) else 'unknown'}")
        return payload

    def plan(self):
        """Every check that needs no code, and what would be written: {old, new, names, files: [{path, what}]}."""
        owner_raw = _read_json(self.deploy / 'owner.json')
        if not isinstance(owner_raw, dict):
            raise TransferRefused('owner_json_unreadable')
        owner = profile.parse_owner(owner_raw)
        old, new = owner.slack_user, self.to
        if not old or not owner.slack_workspace or not owner.assistant_slack_bot_user:
            raise TransferRefused('profile_has_no_verified_slack_identity')
        if not identity.USER_RE.fullmatch(new or ''):
            raise TransferRefused('to_must_be_a_slack_user_id')
        if new == old:
            raise TransferRefused('already_the_owner')
        if new == owner.assistant_slack_bot_user:
            raise TransferRefused('the_new_owner_is_the_bot')
        try:
            ok, detail = identity.status(owner, self.state)
        except tokens.TokenRefused as exc:
            raise TransferRefused(f'owner_identity_record_refused: {exc.code}') from None
        if ok is False:
            raise TransferRefused(f'owner_identity_not_consistent: {detail} (run doctor first)')
        if not self.bot_token:
            raise TransferRefused('bot_token_not_in_the_secrets_store')
        try:
            bot = tokens.check_bot(self.bot_token, call=self.call)
        except tokens.TokenRefused as exc:
            raise TransferRefused(f'bot_token_refused: {exc.code}') from None
        if bot['team_id'] != owner.slack_workspace or bot['bot_user_id'] != owner.assistant_slack_bot_user:
            raise TransferRefused('bot_token_is_not_this_deployments_bot')
        person = _approvers.Approvers(owner_user=old, bot_user=bot['bot_user_id'], team=bot['team_id'],
                                      decisions_path=self.decisions_path or self.state / 'none.json',
                                      load_decisions=dict, rules=None, slack=self._slack)
        try:
            found = person.resolve(new)
        except ValueError as exc:
            raise TransferRefused(f'new_owner_refused: {str(exc).replace("approver", "owner")}') from None
        name = (self.name or found['name']).strip()
        short_name = (self.short_name or name.split()[0]).strip()
        owner_new = copy.deepcopy(owner_raw)
        owner_new['identities']['slack']['user'] = new
        owner_new['owner'].update(name=name, short_name=short_name)
        try:
            profile.parse_owner(owner_new)
        except profile.ProfileError as exc:
            raise TransferRefused(f'new_profile_refused: {exc}') from None
        files = {self.deploy / 'owner.json': _json(owner_new)}
        org = _read_json(self.deploy / 'org.json')
        if isinstance(org, dict) and org.get('name') == owner.name:  # init writes the owner's name there
            files[self.deploy / 'org.json'] = _json({**org, 'name': name})
        files.update(self._dispatch(owner, name, new, short_name))
        decisions_raw = _read_json(self.decisions_path) if self.decisions_path and self.decisions_path.exists() else None
        if self.decisions_path and self.decisions_path.exists() and not isinstance(decisions_raw, dict):
            raise TransferRefused('decisions_config_unreadable')
        decisions = new_decisions(decisions_raw, old, new, name, self.keep)
        if decisions is not None:
            files[self.decisions_path] = _json(decisions)
            self._check_decisions(files[self.decisions_path])
        spec = info = None
        if self.rules is not None:
            try:
                spec, info = self.rules.load_active('access')
            except (ValueError, OSError):
                spec = info = None
        rules = new_rules(spec, old, new, name, self.keep)
        if rules is not None:
            try:
                self.rules.validate_spec(rules)
            except ValueError as exc:
                raise TransferRefused(f'new_rules_refused: {exc}') from None
        return {'old': old, 'new': new, 'name': name, 'short_name': short_name, 'bot': bot, 'files': files,
                'decisions_old': decisions_raw, 'decisions': decisions, 'rules': rules, 'rules_info': info,
                'workspace': owner.slack_workspace}

    def _dispatch(self, owner, name, new, short_name):
        """The dispatch files and the README that still hold init's text for the old owner, rewritten for the new one."""
        lock = _read_json(self.root / 'kage.lock') or {}
        version = (lock.get('kage') or {}).get('version')
        if not version:
            return {}
        common = {'key': owner.key, 'version': version, 'program': init.PROGRAM, 'language': owner.language}
        before = dispatch_files.files(name=owner.short_name, owner_user=owner.slack_user, **common)
        after = dispatch_files.files(name=short_name, owner_user=new, **common)
        state = f'~/.local/share/kage/state-{owner.key}/'
        before['README.md'], after['README.md'] = (init.README.format(name=n, key=owner.key, version=version,
                                                                      state=state).encode('utf-8')
                                                   for n in (owner.name, name))
        out = {}
        for rel in (*DISPATCH_TEXT, 'README.md'):
            path = self.root / rel
            if path.is_file() and path.read_bytes() == before[rel] and after[rel] != before[rel]:
                out[path] = after[rel]
        return out

    def _check_decisions(self, data):
        if self.authority is None:
            return
        fd, tmp = tempfile.mkstemp(dir=self.decisions_path.parent, prefix='.kage-transfer-check-')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            os.chmod(tmp, 0o600)
            self.authority.load_config(tmp)
        except ValueError as exc:
            raise TransferRefused(f'new_decisions_config_refused: {exc}') from None
        finally:
            os.unlink(tmp)

    def report(self, plan):
        lines = [f"owner-transfer: {plan['old']} -> {plan['new']} ({plan['name']} / {plan['short_name']})"
                 + (', the old owner stays an owner' if self.keep else '')]
        lines += [f'  rewrite {p}' for p in plan['files']]
        lines.append(f"  rewrite {self.state / identity.RECORD} (the new owner's verified record)")
        if plan['rules'] is not None:
            lines.append(f"  rules   access v{plan['rules_info']['version']} -> v{plan['rules']['version']} "
                         f"(owner + approvers {', '.join(plan['rules']['defaults']['approvers'])})")
        if plan['decisions'] is not None:
            lines.append(f"  approvers {', '.join(plan['decisions']['approvers'])}"
                         + (f"; owners {', '.join(plan['decisions']['owners'])}" if plan['decisions'].get('owners')
                            is not None else ''))
        return lines

    def _backup(self, plan, now):
        """Copy every file the transfer may touch; returns the backup directory and its manifest."""
        d = self.state / BACKUPS / dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        d.mkdir(parents=True, mode=0o700)
        os.chmod(d.parent, 0o700)
        paths = [*plan['files'], self.state / identity.RECORD]
        if plan['rules_info']:
            paths.append(Path(plan['rules_info']['dir']) / 'active.json')
        manifest = []
        for i, path in enumerate(paths):
            entry = {'path': str(path), 'copy': None}
            if path.exists():
                entry['copy'] = f'{i:02d}-{path.name}'
                shutil.copy2(path, d / entry['copy'])
                os.chmod(d / entry['copy'], 0o600)
            manifest.append(entry)
        _write(d / 'manifest.json', _json(manifest))
        return d, manifest

    @staticmethod
    def _restore(d, manifest):
        problems = []
        for entry in manifest:
            path = Path(entry['path'])
            try:
                if entry['copy'] is None:
                    if path.exists():
                        path.unlink()
                else:
                    _write(path, (d / entry['copy']).read_bytes(), os.stat(d / entry['copy']).st_mode & 0o777)
            except OSError as exc:
                problems.append(f'{path}: {type(exc).__name__}')
        return problems

    def prove(self, plan):
        """The OWNER CODE, then wait for the new owner's DM; returns the verified record or raises TransferRefused."""
        code = identity.new_challenge(self.state, clock=self.clock)
        self.say({'owner_code': code, 'to': plan['new'], 'expires_in': self.timeout})
        started = self.clock()
        while True:
            try:
                return identity.prove_by_dm(self.state, self.bot_token, plan['bot'], call=self.call, clock=self.clock,
                                            expect_user=plan['new'])
            except tokens.TokenRefused as exc:
                if exc.code != 'identity_code_not_found_in_the_bots_direct_messages' or \
                        self.clock() - started > self.timeout:
                    with_challenge = self.state / identity.CHALLENGE
                    if with_challenge.exists():
                        with_challenge.unlink()
                    raise TransferRefused(f'new_owner_not_verified: {exc.code}') from None
            self.sleep(POLL_SECONDS)

    def apply(self, plan, record_of=None):
        """After the proof: write everything (a failure puts every file back). Returns the audit record."""
        now = self.clock()
        d, manifest = self._backup(plan, now)
        try:
            record = record_of(plan) if record_of else self.prove(plan)
        except BaseException:
            self._restore(d, manifest)
            raise
        try:
            record = {**record, 'name': plan['name'], 'short_name': plan['short_name'], 'transferred_from': plan['old']}
            identity._write(self.state / identity.RECORD, record)
            installed = None
            if plan['rules'] is not None:
                installed = self.rules.install_spec(plan['rules'], who=f"owner-transfer:{plan['new']}",
                                                    note='deployment owner transferred', now=now)
            for path, data in plan['files'].items():
                _write(path, data)
            git = None
            if (self.root / '.git').exists():  # last: a commit is the one write the backup cannot take back
                rels = [str(p.relative_to(self.root)) for p in plan['files'] if self.root in p.parents]
                git = init._commit(self.root, f"kage owner-transfer: {plan['old']} -> {plan['new']}", rels)
            old_dec, new_dec = plan['decisions_old'] or {}, plan['decisions']
            if new_dec is not None:
                old_ids, new_ids = list(old_dec.get('approvers') or [plan['old']]), new_dec['approvers']
                owners_old = list(old_dec.get('owners') or old_ids)
                change = {'at': _iso(now), 'ts': now, 'who': f"owner-transfer:{plan['new']}", 'old': old_ids,
                          'new': new_ids, 'added': [u for u in new_ids if u not in old_ids],
                          'removed': [u for u in old_ids if u not in new_ids], 'owners_old': owners_old,
                          'owners_new': new_dec.get('owners') or new_ids, 'names': {plan['new']: plan['name']},
                          'rules_version': plan['rules']['version'] if plan['rules'] else None, 'backup': str(d),
                          'until': None, 'source': 'owner-transfer',
                          'deployment_owner': {'old': plan['old'], 'new': plan['new']}}
                log = _approvers.log_path(self.decisions_path)
                fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, 'a', encoding='utf-8') as stream:
                    stream.write(json.dumps(change, ensure_ascii=False) + '\n')
        except BaseException as exc:
            problems = self._restore(d, manifest)
            raise TransferRefused(f'owner_transfer_failed_{"and_restored" if not problems else "restore_incomplete"}: '
                                  f'{type(exc).__name__}: {exc}' + (f" ({'; '.join(problems)})" if problems else '')) \
                from None
        audit = {'at': _iso(now), 'ts': now, 'old': plan['old'], 'new': plan['new'], 'name': plan['name'],
                 'short_name': plan['short_name'], 'keep_old_owner': self.keep, 'method': record['method'],
                 'evidence': record.get('evidence'), 'files': [str(p) for p in plan['files']], 'git': git,
                 'rules': installed.get('file') if isinstance(installed, dict) else None, 'backup': str(d)}
        fd = os.open(self.state / AUDIT, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(audit, ensure_ascii=False) + '\n')
        return audit


def parser(prog='owner-transfer'):
    p = argparse.ArgumentParser(prog=prog, description='Hand this deployment to another person of its workspace; '
                                'they prove it with a code sent to the bot as a direct message.')
    p.add_argument('--to', required=True, help="the new owner's Slack user id")
    p.add_argument('--name', help="the new owner's name (default: their Slack real name)")
    p.add_argument('--short-name', help='how texts name the new owner (default: the first word of the name)')
    p.add_argument('--keep-old-owner', action='store_true', help='the old owner stays one of the owners / approvers')
    p.add_argument('--dry-run', action='store_true', help='check and print the plan; write nothing, make no code')
    p.add_argument('--timeout', type=int, default=TIMEOUT, help='seconds to wait for the DM (at most 600)')
    p.add_argument('--db', type=Path, help='the state database (default: the deployment\'s)')
    p.add_argument('--json', action='store_true', help='JSON lines on stdout')
    return p


def _refuse(code, detail):
    print(json.dumps({'error': code, 'detail': detail}), file=sys.stderr, flush=True)
    return 1


def main(assembly, runtime, argv, *, call=tokens.slack_call, store=None, sleep=time.sleep, clock=time.time):
    """`<program> owner-transfer …` on an assembled deployment; exit 0 done (or the dry run), 1 refused, 2 usage."""
    args = parser(f'{Path(str(assembly.program)).name} owner-transfer').parse_args(argv)
    run_id_env = assembly.value('hub.run_id_env')
    if any(os.environ.get(k) for k in AGENT_ENV) or (run_id_env and os.environ.get(run_id_env)):
        return _refuse('TransferRefused', 'owner_transfer_runs_on_the_host_never_in_an_agent_session_or_a_dispatched_run')
    from kage.ports import secrets as _secrets
    owner = assembly.deployment.owner
    rt = runtime.get('RUNTIME')
    db = args.db or Path.home() / assembly.value('hub.DEFAULT_DB')
    buttons = getattr(rt, 'buttons', None)
    decisions_path = buttons.config_path() if buttons is not None else None
    checker = None
    if buttons is not None and getattr(buttons, 'authority', None) is not None:
        checker = copy.copy(buttons.authority)
        checker.fallback = args.to
    try:
        store = store or _secrets.for_owner(owner.key)
        bot_token = store.get(_secrets.SLACK_BOT)
    except _secrets.SecretError as exc:
        return _refuse('TransferRefused', f'secrets_store_refused: {exc}')

    def say(item):
        if args.json:
            print(json.dumps(item, ensure_ascii=False), flush=True)
        elif isinstance(item, dict) and 'owner_code' in item:
            print(f"OWNER CODE: {item['owner_code']}", flush=True)
            print(f"The new owner ({item['to']}) sends exactly this code to the bot as a direct message in Slack. "
                  f"Waiting up to {item['expires_in'] // 60} minutes; nobody else's message counts.", flush=True)
        else:
            print(item, flush=True)
    t = Transfer(deploy_dir=assembly.dir, state_dir=db.parent, to=args.to, bot_token=bot_token,
                 decisions_path=decisions_path, rules=getattr(rt, 'rules', None), authority=checker, name=args.name,
                 short_name=args.short_name, keep_old_owner=args.keep_old_owner, call=call, clock=clock, sleep=sleep,
                 say=say, timeout=args.timeout)
    try:
        plan = t.plan()
        if args.dry_run:
            if args.json:
                say({'dry_run': True, 'old': plan['old'], 'new': plan['new'], 'name': plan['name'],
                     'short_name': plan['short_name'], 'files': [str(p) for p in plan['files']],
                     'rules_version': plan['rules']['version'] if plan['rules'] else None})
            else:
                for line in t.report(plan):
                    say(line)
                say('owner-transfer: dry run, nothing written')
            return 0
        if not args.json:
            for line in t.report(plan):
                say(line)
        audit = t.apply(plan)
    except (TransferRefused, identity.TokenRefused) as exc:
        return _refuse('TransferRefused', getattr(exc, 'code', None) or str(exc))
    except KeyboardInterrupt:
        return _refuse('Interrupted', 'stopped; files restored from the backup')
    if args.json:
        say({'done': True, **audit})
    else:
        say(f"owner-transfer: done, {audit['old']} -> {audit['new']} (backup {audit['backup']}"
            + (f", git {audit['git']}" if audit['git'] else '') + (f", rules {audit['rules']}" if audit['rules'] else '')
            + '). Next: restart the services (they read the profile at start), then run doctor.')
    return 0
