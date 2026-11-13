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
    engine (load_active / install_spec / validate_spec) and a Slack lookup `slack(method, params) -> payload`.

    0.78.10 (F22): OWNERS and ACTIVE APPROVERS apart. The owners (decisions.json `owners`) count as the owner
    (kage.core.authority.owner_like_users), may use the console and may switch the approvers; the active approvers
    (decisions.json `approvers`, a non-empty subset of the owners and the deployment owner) are the ones cards mention and
    whose clicks or typed answers decide. The access rules get the active approvers plus the deployment owner (they require
    it). A file without `owners` reads as before: every approver is an owner."""

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

    def owners_of(self, config):
        return list(config.get('owners') or config['approvers'])

    def state(self):
        """Owners and active approvers with names and where each one is listed, the digest a save must quote, the
        pending scheduled switch back, and recent changes."""
        config = self.load_decisions()
        spec, info = self._rules()
        defaults = (spec or {}).get('defaults') or {}
        in_rules = list(defaults.get('approvers') or [])
        stored = _read_json(self.decisions_path).get('owner_names') if self.decisions_path.exists() else None
        names = {**self.names(), **(defaults.get('approver_names_zh') or {}),
                 **(stored if isinstance(stored, dict) else {})}
        owners = self.owners_of(config)
        ids = list(dict.fromkeys([self.owner] + owners + list(config['approvers'])))
        people = [{'id': u, 'name': names.get(u), 'owner': u == self.owner, 'is_owner': u in owners or u == self.owner,
                   'active': u in config['approvers'], 'in_decisions': u in config['approvers'],
                   'in_rules': u in in_rules if spec else None} for u in ids]
        return {'owner': {'id': self.owner, 'name': names.get(self.owner)},
                'owners': [p for p in people if p['is_owner']], 'approvers': [p for p in people if p['active']],
                'people': people, 'split': bool(config.get('owners')),
                'rules': {'version': info['version'], 'file': info['file']} if info else None,
                'hr': list(defaults.get('hr') or []), 'max': MAX, 'digest': self.digest(config, spec),
                'schedule': read_schedule(self.decisions_path), 'decisions_source': config.get('source'),
                'changes': changes(self.decisions_path)}

    @staticmethod
    def digest(config, spec):
        payload = json.dumps([sorted(config['approvers']), sorted(config.get('owners') or []), (spec or {}).get('version'),
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

    def _entries(self, entries, field):
        if not isinstance(entries, list) or not entries or len(entries) > 20 or \
                any(not isinstance(e, str) or len(e) > 254 for e in entries):
            raise ValueError(f'{field}_must_be_a_list_of_slack_ids_or_emails')
        if len({e.strip() for e in entries}) > MAX:  # before any Slack lookup
            raise ValueError(f'at_most_{MAX}_{field}')
        resolved, seen = [], set()
        for entry in entries:
            person = {'id': self.owner, 'name': None} if entry.strip() == self.owner else self.resolve(entry)
            if person['id'] not in seen:
                seen.add(person['id'])
                resolved.append(person)
        return resolved

    def plan(self, approvers, digest, owners=None):
        """(active ids, owner ids, new rules spec or None, resolved people, added, removed, old active, old owners) for the
        requested lists; raises ValueError(code) before anything is written. `owners` None: the owners stay as they are
        (a file without owners: they follow the approvers, as before 0.78.10)."""
        config = self.load_decisions()
        spec, _ = self._rules()
        if digest != self.digest(config, spec):
            raise ValueError('approvers_changed_since_loaded_reload')
        old, old_owners = list(config['approvers']), self.owners_of(config)
        resolved = self._entries(approvers, 'approvers')
        owner_people = self._entries(owners, 'owners') if owners is not None else None
        ids = [p['id'] for p in resolved]
        if owner_people is None:
            owner_ids = list(dict.fromkeys(old_owners + ids)) if config.get('owners') else None
        else:
            owner_ids = [p['id'] for p in owner_people]
            if self.owner not in owner_ids:
                owner_ids.insert(0, self.owner)
        if owner_ids is None:  # the pre-0.78.10 shape: the approvers are the owners; the deployment owner stays one
            if self.owner not in ids:
                raise ValueError('the_owner_stays_an_approver_change_the_owner_through_the_deployment')
        else:
            if len(owner_ids) > MAX:
                raise ValueError(f'at_most_{MAX}_owners')
            outside = [u for u in ids if u not in owner_ids]
            if outside:
                raise ValueError('an_active_approver_must_be_one_of_the_owners')
        if len(ids) > MAX:
            raise ValueError(f'at_most_{MAX}_approvers')
        hr = set(((spec or {}).get('defaults') or {}).get('hr') or [])
        if hr & set(ids + (owner_ids or [])):
            raise ValueError('an_hr_contact_cannot_be_an_approver')
        added, removed = [u for u in ids if u not in old], [u for u in old if u not in ids]
        new_spec = None
        rule_ids = ids if self.owner in ids else [self.owner] + ids  # the rules require the deployment owner
        if spec is not None:
            new_spec = copy.deepcopy(spec)
            defaults = new_spec['defaults']
            names = dict(defaults.get('approver_names_zh') or {})
            for p in resolved + (owner_people or []):
                if p['name'] and p['id'] != self.owner:
                    names[p['id']] = p['name']
            defaults['approvers'] = rule_ids
            defaults['approver_names_zh'] = {u: n for u, n in names.items() if u in rule_ids}
            for rule in new_spec.get('rules') or []:
                if isinstance(rule.get('bounds'), dict) and isinstance(rule['bounds'].get('approvers'), list):
                    rule['bounds']['approvers'] = list(rule_ids)
            new_spec['version'] = int(spec['version']) + 1
            self.rules.validate_spec(new_spec)
        same_rules = spec is None or spec['defaults']['approvers'] == rule_ids
        if not added and not removed and same_rules and (owner_ids is None or owner_ids == old_owners and config.get('owners')):
            raise ValueError('nothing_to_change')
        return ids, owner_ids, new_spec, resolved + (owner_people or []), added, removed, old, old_owners

    def save(self, approvers, digest, who, now=None, owners=None, schedule=None, source=None):
        """Write decisions.json and the new rules version (see the module doc); returns the change and the new state.
        A save clears a pending scheduled switch back unless it sets one (`schedule`: {at, then})."""
        now = time.time() if now is None else now
        ids, owner_ids, new_spec, resolved, added, removed, old, old_owners = self.plan(approvers, digest, owners)
        path = self.decisions_path
        before = path.read_bytes() if path.exists() else None
        raw = _read_json(path)
        raw['approvers'] = ids
        if owner_ids is not None:
            raw['owners'] = owner_ids
            known = raw.get('owner_names') if isinstance(raw.get('owner_names'), dict) else {}
            fresh = {p['id']: p['name'] for p in resolved if p.get('name')}
            raw['owner_names'] = {u: (fresh.get(u) or known.get(u)) for u in owner_ids if fresh.get(u) or known.get(u)}
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
        write_schedule(path, schedule)
        names = {p['id']: p['name'] for p in resolved}
        record = {'at': _iso(now), 'ts': now, 'who': who, 'old': old, 'new': ids, 'added': added, 'removed': removed,
                  'owners_old': old_owners, 'owners_new': owner_ids if owner_ids is not None else old_owners,
                  'names': {u: names.get(u) for u in set(added) | set(owner_ids or []) if names.get(u)},
                  'rules_version': new_spec['version'] if new_spec else None, 'backup': str(backup) if backup else None,
                  'until': (schedule or {}).get('at'), 'source': source}
        log = log_path(path)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        return {'ok': True, 'change': record, 'rules': installed, 'state': self.state()}

    def switch(self, names, who, until=None, source=None, now=None):
        """0.78.10 (F22): an owner's "审批人换成 X" — the active approvers become the named owners (owners stay); with
        `until` (a UTC timestamp) the previous approvers come back then (`Notices.step`). Names are owner ids or the
        owners' names (see `match_owners`)."""
        now = time.time() if now is None else now
        st = self.state()
        owners = [dict(o) for o in st['owners']]
        for o in owners:  # an owner without a stored name: Slack's (so a first name finds them)
            if not o.get('name'):
                try:
                    o['name'] = self.resolve(o['id'])['name']
                except ValueError:
                    pass
        ids = match_owners(names, owners)
        if until is not None and not now < until <= now + 90 * 86400:
            raise ValueError('until_must_be_within_90_days')
        previous = [p['id'] for p in st['approvers']]
        if ids == previous:
            raise ValueError('nothing_to_change')
        schedule = {'at': until, 'then': previous, 'set_by': who} if until else None
        return self.save(ids, st['digest'], who, now=now, owners=[p['id'] for p in st['owners']], schedule=schedule,
                         source=source)


def schedule_path(decisions_path):
    return Path(decisions_path).expanduser().parent / 'approvers.schedule.json'


def read_schedule(decisions_path):
    try:
        raw = json.loads(schedule_path(decisions_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get('at'), (int, float)) and isinstance(raw.get('then'), list) else None


def write_schedule(decisions_path, schedule):
    path = schedule_path(decisions_path)
    if schedule:
        _private_write(path, (json.dumps(schedule, ensure_ascii=False) + '\n').encode('utf-8'))
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def _norm(text):
    return re.sub(r'[\s._-]+', '', str(text or '').casefold())


def match_owners(names, owners):
    """The owner ids `names` mean: a Slack id / mention, or a name that matches exactly one owner (its full name, or one
    word of it, any case). Else ValueError naming the first name that matched none or several."""
    out = []
    for name in names:
        raw = str(name).strip().strip('<@>').split('|')[0]
        if USER_RE.fullmatch(raw):
            hits = [o for o in owners if o['id'] == raw]
        else:
            key = _norm(raw)
            hits = [o for o in owners if key and (_norm(o.get('name')) == key or key in
                                                   [_norm(w) for w in re.split(r'\s+', str(o.get('name') or ''))])]
        if len(hits) != 1:
            raise ValueError(('approver_name_matches_several_owners:' if hits else 'approver_name_is_not_an_owner:') +
                             re.sub(r'[^a-z0-9_]', '_', raw.lower())[:30])
        if hits[0]['id'] not in out:
            out.append(hits[0]['id'])
    if not out:
        raise ValueError('approver_name_is_not_an_owner')
    return out


SWITCH_RE = re.compile(r'(?:审批人|审批|approvers?)\s*(?:换成|改成|改为|换为|设为|设成|变成|改到|换到|->|→|=|:|：|\bto\b|\bis now\b)\s*'
                       r'(?P<names>.+?)\s*(?:[,，。.;；]|$)', re.I)
UNTIL_RE = re.compile(r'(?:到|至|until|till|through)\s*(?:(?P<y>\d{4})[-/年])?(?P<m>\d{1,2})\s*[-/月]\s*(?P<d>\d{1,2})\s*(?:日|号)?', re.I)
NAME_SPLIT_RE = re.compile(r'\s*(?:和|与|、|,|，|/|\band\b|&)\s*', re.I)


def parse_switch(text, tz, now):
    """(names, until UTC timestamp or None) when `text` is an approver switch ("审批人换成 lin", "approver -> Sam",
    "我请假到 11-20，审批人换成 lin"), else None. The until date is a day in the owner's time zone: the switch back
    happens when it is over (the next day, 00:00)."""
    plain = re.sub(r'<@([UW][A-Z0-9]{2,})(?:\|[^>]*)?>', r'\1', str(text or ''))
    plain = re.sub(r'<[!#][^>]*>', ' ', plain).strip()
    match = SWITCH_RE.search(plain)
    if not match:
        return None
    names = [n for n in NAME_SPLIT_RE.split(UNTIL_RE.sub('', match['names']).strip()) if n.strip()]
    if not names or len(names) > MAX:
        return None
    until = None
    when = UNTIL_RE.search(plain)
    if when:
        local = dt.datetime.fromtimestamp(now, tz)
        year = int(when['y']) if when['y'] else local.year
        try:
            day = dt.date(year, int(when['m']), int(when['d']))
        except ValueError:
            raise ValueError('until_is_not_a_date') from None
        if not when['y'] and day < local.date():
            day = dt.date(year + 1, day.month, day.day)
        until = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), tz).timestamp()
    return names, until


def from_runtime(rt):
    """An Approvers over the supervisor's / CLI's runtime (its decisions config, rule engine, bot-token Slack reads)."""
    from kage.core import control as _control

    def slack(method, params):
        return rt.slack.api_call(method, rt.api_identity(bot=True), params=params)
    def names():
        try:
            return rt.intake.display_names(rt.envelopes.people_path())
        except Exception:  # noqa: BLE001 - names are a nicety
            return {}
    return Approvers(owner_user=_control._owner_user(rt), bot_user=getattr(rt, 'BOT', None), team=getattr(rt, 'TEAM', None),
                     decisions_path=rt.buttons.config_path, load_decisions=rt.buttons.load_config, rules=rt.rules,
                     slack=slack, names=names)


def handle_switch(rt, db, channel, m, tz=None, now=None):
    """0.78.10 (F22): an owner's own hand-typed approver switch in Slack, applied by code (never an agent): None when `m`
    is not one; else the result, also said in the message's thread. Only an owner (or the deployment owner), only a
    plain message (no app / bot / subtype)."""
    from kage.core import control as _control
    now = time.time() if now is None else now
    if tz is None:
        try:
            tz = rt.DEPLOYMENT.owner.tz()
        except AttributeError:
            tz = dt.timezone.utc
    if m.get('subtype') not in (None, 'thread_broadcast') or m.get('bot_id') or m.get('app_id'):
        return None
    user = str(m.get('user') or '')
    if user not in _control.owner_users(rt):
        return None
    try:
        parsed = parse_switch(m.get('text'), tz, now)
    except ValueError as exc:
        parsed, error = None, str(exc)
    else:
        error = None
    if parsed is None and error is None:
        return None
    db.execute('CREATE TABLE IF NOT EXISTS approver_switch_messages (channel TEXT NOT NULL, ts TEXT NOT NULL, user TEXT, '
               'outcome TEXT, at REAL NOT NULL, PRIMARY KEY (channel, ts))')
    if db.in_transaction:
        db.commit()
    with rt.transaction(db):  # one message, one switch (the push and the fallback poll may both see it)
        seen = db.execute('SELECT outcome FROM approver_switch_messages WHERE channel=? AND ts=?',
                          (channel, str(m.get('ts')))).fetchone()
        if seen is None:
            db.execute("INSERT INTO approver_switch_messages (channel,ts,user,outcome,at) VALUES (?,?,?,'handling',?)",
                       (channel, str(m.get('ts')), user, now))
    if seen is not None:
        return {'ok': True, 'outcome': 'duplicate', 'reason': 'approver_switch_already_handled'}
    editor = from_runtime(rt)
    link = f'https://slack.com/archives/{channel}/p{str(m.get("ts")).replace(".", "")}'
    try:
        if error:
            raise ValueError(error)
        out = editor.switch(parsed[0], f'slack:{user}', until=parsed[1], source=link, now=now)
        names = {p['id']: p.get('name') or p['id'] for p in out['state']['people']}
        reply = locale.message(_language(rt), 'approvers.switched', people=locale.message(_language(rt), 'approvers.joiner')
                               .join(names.get(u, u) for u in out['change']['new'])) + (
            locale.message(_language(rt), 'approvers.switched_until', date=dt.datetime.fromtimestamp(parsed[1], tz)
                           .strftime('%m-%d %H:%M')) if parsed[1] else '')
        result = {'ok': True, 'outcome': 'approvers_switched', 'approvers': out['change']['new'], 'until': parsed[1]}
    except ValueError as exc:
        reply = locale.message(_language(rt), 'approvers.switch_failed', why=str(exc)[:120])
        result = {'ok': False, 'outcome': 'approvers_switch_refused', 'reason': str(exc)[:120]}
    try:
        sent = rt.slack.api_call('chat.postMessage', rt.api_identity(bot=True), json_body={
            'channel': channel, 'thread_ts': m.get('thread_ts') or m.get('ts'), 'text': reply, 'unfurl_links': False,
            'metadata': rt.agent_metadata('approvers_switch', None)})
        rt.record_send(db, channel, (sent or {}).get('ts'), 'approvers_switch', None)
    except Exception:  # noqa: BLE001 - the change stands; the decision-channel notice still reports it
        result['reply'] = 'failed'
    with rt.transaction(db):
        db.execute('UPDATE approver_switch_messages SET outcome=? WHERE channel=? AND ts=?',
                   (result['outcome'], channel, str(m.get('ts'))))
    return result


def _language(rt):
    try:
        return rt.DEPLOYMENT.owner.language
    except AttributeError:
        return getattr(rt, 'LANGUAGE', 'en')


class Notices:
    """The supervisor's step: one notice in the decision channel per new line of the approvers change log, in the owner's
    language (who changed it, who was added / removed, the approvers now). The first run only sets the cursor."""

    def __init__(self, language):
        self.language = language

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def revert(self, rt, db, now=None):
        """0.78.10 (F22): a scheduled switch back that is due (`approvers.schedule.json`): the previous approvers that are
        still owners come back (who = schedule); a failure is retried on the next pass."""
        now = time.time() if now is None else now
        schedule = read_schedule(rt.buttons.config_path())
        if not schedule or schedule['at'] > now:
            return None
        editor = from_runtime(rt)
        st = editor.state()
        owners = [p['id'] for p in st['owners']]
        back = [u for u in schedule['then'] if u in owners]
        if not back or back == [p['id'] for p in st['approvers']]:
            write_schedule(rt.buttons.config_path(), None)
            return {'reverted': False, 'reason': 'nothing_to_restore'}
        out = editor.save(back, st['digest'], 'schedule', now=now, owners=owners, source='schedule')
        return {'reverted': True, 'approvers': out['change']['new']}

    def step(self, rt, db, now=None):
        try:
            self.revert(rt, db, now)
        except Exception as exc:  # noqa: BLE001 - retried on the next pass
            rt._meta_error(db, 'approvers_revert_error', exc)
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
        if record.get('who') == 'schedule':
            who = self.msg('approvers.schedule_who')
        elif USER_RE.fullmatch(who):
            who = name(who)
        parts = []
        moved = record.get('deployment_owner')  # 0.79.3 (F30): `owner-transfer`
        if isinstance(moved, dict) and moved.get('new'):
            parts.append(self.msg('approvers.deployment_owner', new=name(moved['new']), old=name(moved.get('old'))))
        if record.get('added'):
            parts.append(self.msg('approvers.added', people=people(record['added'])))
        if record.get('removed'):
            parts.append(self.msg('approvers.removed', people=people(record['removed'])))
        owners_old, owners_new = record.get('owners_old') or [], record.get('owners_new') or []
        if owners_new and owners_new != owners_old:
            parts.append(self.msg('approvers.owners_now', people=people(owners_new)))
        text = self.msg('approvers.notice', who=who, changes=self.msg('approvers.joiner_changes').join(parts) or
                        self.msg('approvers.synced'), now=people(record.get('new') or []))
        if record.get('until'):
            try:
                tz = rt.DEPLOYMENT.owner.tz()
            except AttributeError:
                tz = dt.timezone.utc
            text += self.msg('approvers.until', date=dt.datetime.fromtimestamp(record['until'], tz).strftime('%m-%d %H:%M'))
        return text
