"""The approvers (0.78.9, F21): who may decide the cards, edited from the console.

With `intake.owner_like: approvers` (a bot-only deployment) the approvers ARE the deployment's owners in practice: their
clicks decide cards and their messages count as the owner's (kage.core.authority). Two places must agree and are written
by one save here: the decisions config (`decisions.json` `approvers`) and the active access rules (`defaults.approvers`,
`defaults.approver_names_zh`, any rule `bounds.approvers`) as a NEW rules version through the rule engine's own
`install_spec`. Everything is validated before anything is written; the decisions file is backed up first and put back
when the rules install fails. One line per save goes to `approvers.changes.jsonl` next to the decisions file (who = the
console actor, e.g. `dashboard:<Cloudflare Access email>`); the supervisor posts a notice for every new line
(`Notices.step`).

Each entry is a Slack user id or an email, resolved through Slack (users.info / users.lookupByEmail) to an active
person of this workspace: never a bot, a deleted account, a guest of another workspace or the deployment's own bot. The
deployment owner stays an approver (the rules require it) and is never added or removed here: the owner identity is a
verified record, changed only by the deployment's own init. 1..MAX approvers (the rules' limit); an HR contact cannot
be one.
"""
from __future__ import annotations
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from kage import locale

MAX = 5
USER_RE = re.compile(r'[UW][A-Z0-9]{2,}')
EMAIL_RE = re.compile(r'[^@\s<>"]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}')
LOG_NAME = 'approvers.changes.jsonl'
NOTICE_KEY = 'approvers_notice_cursor'


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec='seconds')


def _read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('decisions_config_is_not_an_object')
    return raw


def _private_write(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def log_path(decisions_path):
    return Path(decisions_path).expanduser().parent / LOG_NAME


def changes(decisions_path, limit=20):
    """Newest-first change lines (tolerates a missing or partly malformed log)."""
    try:
        lines = log_path(decisions_path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
        if len(out) >= limit:
            break
    return out


class Approvers:
    """The deployment-bound half: the owner and the bot (never added or removed here), the decisions file, the rule
    engine (load_active / install_spec / validate_spec) and a Slack lookup `slack(method, params) -> payload`."""

    def __init__(self, *, owner_user, bot_user, team, decisions_path, load_decisions, rules, slack, names=None):
        self.owner, self.bot, self.team = owner_user, bot_user, team
        self._path, self.load_decisions, self.rules, self.slack = decisions_path, load_decisions, rules, slack
        self.names = names or (lambda: {})

    @property
    def decisions_path(self):
        """The decisions file (a callable is read on every use: its env override may change)."""
        return Path(self._path() if callable(self._path) else self._path).expanduser()

    def _rules(self):
        try:
            return self.rules.load_active('access')
        except (ValueError, OSError):
            return None, None

    def state(self):
        """The current approvers with names, where each one is listed, the digest a save must quote, and recent changes."""
        config = self.load_decisions()
        spec, info = self._rules()
        defaults = (spec or {}).get('defaults') or {}
        in_rules = list(defaults.get('approvers') or [])
        names = {**self.names(), **(defaults.get('approver_names_zh') or {})}
        ids = list(dict.fromkeys(list(config['approvers']) + in_rules))
        return {'owner': {'id': self.owner, 'name': names.get(self.owner)},
                'approvers': [{'id': u, 'name': names.get(u), 'owner': u == self.owner, 'in_decisions': u in config['approvers'],
                               'in_rules': u in in_rules if spec else None} for u in ids],
                'rules': {'version': info['version'], 'file': info['file']} if info else None,
                'hr': list(defaults.get('hr') or []), 'max': MAX, 'digest': self.digest(config, spec),
                'decisions_source': config.get('source'), 'changes': changes(self.decisions_path)}

    @staticmethod
    def digest(config, spec):
        payload = json.dumps([sorted(config['approvers']), (spec or {}).get('version'),
                              sorted(((spec or {}).get('defaults') or {}).get('approvers') or [])])
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def resolve(self, entry):
        """{id, name} of an active person of this workspace for a Slack user id or an email, else ValueError(code)."""
        entry = str(entry or '').strip()
        try:
            if USER_RE.fullmatch(entry):
                user = (self.slack('users.info', {'user': entry}) or {}).get('user') or {}
            elif EMAIL_RE.fullmatch(entry):
                user = (self.slack('users.lookupByEmail', {'email': entry}) or {}).get('user') or {}
            else:
                raise ValueError('approver_must_be_a_slack_user_id_or_an_email')
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - users_not_found, missing_scope, a network error: a code only
            raise ValueError('approver_not_found_in_slack:' + re.sub(r'[^a-z0-9_]', '_', str(exc).lower())[-60:]) from None
        uid = str(user.get('id') or '')
        if not USER_RE.fullmatch(uid):
            raise ValueError('approver_not_found_in_slack')
        if user.get('is_bot') or uid in (self.bot, 'USLACKBOT') or user.get('is_app_user'):
            raise ValueError('approver_must_be_a_person_not_a_bot')
        if user.get('deleted'):
            raise ValueError('approver_account_is_deactivated')
        if user.get('is_restricted') or user.get('is_ultra_restricted') or \
                (self.team and user.get('team_id') and user.get('team_id') != self.team):
            raise ValueError('approver_must_be_a_full_member_of_this_workspace')
        profile = user.get('profile') if isinstance(user.get('profile'), dict) else {}
        name = profile.get('real_name') or profile.get('display_name') or user.get('real_name') or user.get('name') or uid
        return {'id': uid, 'name': str(name).strip()[:60]}

    def plan(self, entries, digest):
        """(new decisions config, new rules spec or None, resolved, added, removed) for the requested approver list; raises
        ValueError(code) before anything is written."""
        if not isinstance(entries, list) or not entries or len(entries) > 20 or \
                any(not isinstance(e, str) or len(e) > 254 for e in entries):
            raise ValueError('approvers_must_be_a_list_of_slack_ids_or_emails')
        if len({e.strip() for e in entries}) > MAX:  # before any Slack lookup
            raise ValueError(f'at_most_{MAX}_approvers')
        config = self.load_decisions()
        spec, _ = self._rules()
        if digest != self.digest(config, spec):
            raise ValueError('approvers_changed_since_loaded_reload')
        resolved, seen = [], set()
        for entry in entries:
            person = {'id': self.owner, 'name': None} if entry.strip() == self.owner else self.resolve(entry)
            if person['id'] not in seen:
                seen.add(person['id'])
                resolved.append(person)
        ids = [p['id'] for p in resolved]
        if self.owner not in ids:
            raise ValueError('the_owner_stays_an_approver_change_the_owner_through_the_deployment')
        if len(ids) > MAX:
            raise ValueError(f'at_most_{MAX}_approvers')
        hr = set(((spec or {}).get('defaults') or {}).get('hr') or [])
        if hr & set(ids):
            raise ValueError('an_hr_contact_cannot_be_an_approver')
        old = list(config['approvers'])
        added, removed = [u for u in ids if u not in old], [u for u in old if u not in ids]
        new_spec = None
        if spec is not None:
            new_spec = copy.deepcopy(spec)
            defaults = new_spec['defaults']
            names = dict(defaults.get('approver_names_zh') or {})
            for p in resolved:
                if p['name'] and p['id'] != self.owner:
                    names[p['id']] = p['name']
            defaults['approvers'] = ids
            defaults['approver_names_zh'] = {u: n for u, n in names.items() if u in ids}
            for rule in new_spec.get('rules') or []:
                if isinstance(rule.get('bounds'), dict) and isinstance(rule['bounds'].get('approvers'), list):
                    rule['bounds']['approvers'] = list(ids)
            new_spec['version'] = int(spec['version']) + 1
            self.rules.validate_spec(new_spec)
        if not added and not removed and (spec is None or spec['defaults']['approvers'] == ids):
            raise ValueError('nothing_to_change')
        return ids, new_spec, resolved, added, removed, old

    def save(self, entries, digest, who, now=None):
        """Write decisions.json and the new rules version (see the module doc); returns the change and the new state."""
        now = time.time() if now is None else now
        ids, new_spec, resolved, added, removed, old = self.plan(entries, digest)
        path = Path(self.decisions_path).expanduser()
        before = path.read_bytes() if path.exists() else None
        raw = _read_json(path)
        raw['approvers'] = ids
        backup = None
        if before is not None:
            revisions = path.parent / 'decisions-revisions'
            revisions.mkdir(mode=0o700, exist_ok=True)
            backup = revisions / f'{path.stem}-{dt.datetime.fromtimestamp(now, dt.timezone.utc):%Y%m%dT%H%M%S}Z.json'
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(before)
        _private_write(path, (json.dumps(raw, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        try:
            self.load_decisions()  # the runtime's own validation of what was written
            installed = self.rules.install_spec(new_spec, who=who, note='approvers changed in the console', now=now) \
                if new_spec is not None else None
        except Exception:
            if before is None:
                with contextlib.suppress(OSError):
                    path.unlink()
            else:
                _private_write(path, before)
            raise
        names = {p['id']: p['name'] for p in resolved}
        record = {'at': _iso(now), 'ts': now, 'who': who, 'old': old, 'new': ids, 'added': added, 'removed': removed,
                  'names': {u: names.get(u) for u in added if names.get(u)},
                  'rules_version': new_spec['version'] if new_spec else None, 'backup': str(backup) if backup else None}
        log = log_path(path)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        return {'ok': True, 'change': record, 'rules': installed, 'state': self.state()}


class Notices:
    """The supervisor's step: one notice in the decision channel per new line of the approvers change log, in the owner's
    language (who changed it, who was added / removed, the approvers now). The first run only sets the cursor."""

    def __init__(self, language):
        self.language = language

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def step(self, rt, db, now=None):
        try:
            path = log_path(rt.buttons.config_path())
            lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
        except (OSError, AttributeError):
            return []
        row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (NOTICE_KEY,)).fetchone()
        cursor = int(row[0]) if row and str(row[0]).isdigit() else None
        if cursor is None or cursor > len(lines):
            with rt.transaction(db):
                db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (NOTICE_KEY, str(len(lines))))
            return []
        out = []
        for index in range(cursor, len(lines)):
            try:
                record = json.loads(lines[index])
                text = self.text(rt, db, record)
                rt.provider_health.post_notice(rt, db, 'approvers', 'changed', text, context_key='approvers.notice_context',
                                              page='/settings')
                out.append(index)
            except ValueError:
                pass  # a malformed line is skipped
            except Exception as exc:  # noqa: BLE001 - retried on the next pass
                rt._meta_error(db, 'approvers_notice_error', exc)
                break
            with rt.transaction(db):
                db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (NOTICE_KEY, str(index + 1)))
        return out

    def text(self, rt, db, record):
        names = record.get('names') or {}
        who = str(record.get('who') or '').split(':', 1)[-1] or self.msg('approvers.someone')

        def name(uid):
            if names.get(uid):
                return names[uid]
            try:
                return rt.intake.person_name(rt, db, uid) or uid
            except Exception:  # noqa: BLE001 - the id then
                return uid

        def people(ids):
            return self.msg('approvers.joiner').join(name(u) for u in ids) or self.msg('approvers.none')
        parts = []
        if record.get('added'):
            parts.append(self.msg('approvers.added', people=people(record['added'])))
        if record.get('removed'):
            parts.append(self.msg('approvers.removed', people=people(record['removed'])))
        return self.msg('approvers.notice', who=who, changes=self.msg('approvers.joiner_changes').join(parts) or
                        self.msg('approvers.synced'), now=people(record.get('new') or []))
