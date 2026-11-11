"""Role facts in the people registry (0.78.7, F19): an agent PROPOSES, code verifies the approval and WRITES.

The registry (`people.json`, kage.core.envelope) is read by the rule engine for a person's role and employment. An
agent never writes it: `propose` takes a patch of whitelisted fields (FIELDS: role.title / function / disciplines /
team.name / team.lead_person_id, employment.status / as_of / contract — never the owner's private view, authority,
flags or identities) and one of two approvals, both verified by code:
  - HR: the confirmation HR typed in Slack, read back here (the author is one of the access rules' `defaults.hr`, a
    person not a bot, at most HR_MAX_AGE_DAYS old, an affirmative answer, in a thread that names the person with a
    mention). One HR message confirms one write per person. Written at once, provenance `hr_confirmed`.
  - The owner or an approver: a code-owned card [写入] [不写] (kage.core.escalation.supervisor_card); the supervisor's
    `settle` applies a click by an approver (an out-of-office delegate does not count), provenance `owner_approved`.
A write is a new revision of the file: the previous bytes are kept under `people-revisions/` (0600), the new file is
written atomically under a lock (mode kept), each changed field gets `provenance[field]` (the source, as the existing
string entries) and `provenance_records[field]` ({source, at, by, source_link, proposal}), and one line per field goes
to `people.changes.jsonl` next to the file. The values a proposal was made against must still be the current ones
(else `stale`, nothing written). The rule engine reads the file again on its next check (it caches by mtime).
`show` reads one person's role / employment and their provenance, never the private view.
"""
from __future__ import annotations
import contextlib
import copy
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.parse
import uuid

from kage import locale

FIELDS = ('role.title', 'role.function', 'role.disciplines', 'role.team.name', 'role.team.lead_person_id',
          'employment.status', 'employment.as_of', 'employment.contract')
SECTIONS = ('role', 'employment')
STATUSES = ('active', 'work_trial', 'left', 'external')
ALIASES = {'employment.since': 'employment.as_of'}
HR_MAX_AGE_DAYS = 14
SOURCES = ('hr_confirmed', 'owner_approved')
SLUG_RE = re.compile(r'[a-z][a-z0-9_]{0,39}')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
USER_RE = re.compile(r'[UW][A-Z0-9]{2,}')
LINK_RE = re.compile(r'https://(?:[a-z0-9-]+\.)?slack\.com/archives/([CDG][A-Z0-9]{2,})/p(\d{10})(\d{6})(?:\?([\w=&.%-]*))?')
CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
AFFIRM_RE = re.compile(r"(?i)(?<![a-z])(?:yes|yep|yeah|correct|confirm(?:ed)?|right|accurate|true|approved?)(?![a-z])|"
                       r'是的|是滴|对的|^对|没错|确认|确定|正确|属实|无误|同意|是$')
NEGATE_RE = re.compile(r"(?i)(?<![a-z])(?:no|not|nope|wrong|incorrect|isn't|false|never)(?![a-z])|"
                       r'不是|不对|不正确|错了|有误|不确定|否|别写|不要|不用')
OPTION_STYLES = (('write', 'primary'), ('skip', None))

DDL = '''
  CREATE TABLE IF NOT EXISTS people_proposals (
    id TEXT PRIMARY KEY, person TEXT NOT NULL, patch TEXT NOT NULL, base TEXT NOT NULL, note TEXT, source TEXT NOT NULL,
    hr_channel TEXT, hr_ts TEXT, hr_user TEXT, hr_link TEXT, state TEXT NOT NULL, card_event TEXT, card_channel TEXT,
    card_ts TEXT, decided_by TEXT, decided_at REAL, written_at REAL, backup TEXT, error TEXT, created_at REAL NOT NULL,
    updated_at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS people_proposals_state ON people_proposals(state);
'''


def initialize(db):
    db.executescript(DDL)
    db.commit()


# ---------------------------------------------------------------------------
# The registry file
# ---------------------------------------------------------------------------

def load(path):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError('people_file_must_be_a_regular_file')
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        raise ValueError('people_file_is_not_valid_json') from None
    if not isinstance(raw, dict) or not isinstance(raw.get('people'), list):
        raise ValueError('people_file_has_no_people_list')
    return raw


def slack_id(person):
    for ident in person.get('identities') or []:
        if isinstance(ident, dict) and ident.get('type') == 'slack' and USER_RE.fullmatch(str(ident.get('value') or '')):
            if ident.get('primary', True):
                return str(ident['value'])
    return None


def find(raw, person):
    """The one person whose id or Slack id is `person`."""
    hits = [p for p in raw['people'] if isinstance(p, dict) and (p.get('id') == person or any(
        isinstance(i, dict) and i.get('type') == 'slack' and i.get('value') == person for i in p.get('identities') or []))]
    if not hits:
        raise ValueError('person_not_found')
    if len(hits) > 1:
        raise ValueError('person_is_ambiguous_use_the_person_id')
    return hits[0]


def display_name(person):
    name = person.get('name') if isinstance(person.get('name'), dict) else {}
    display = name.get('display')
    return display.strip()[:60] if isinstance(display, str) and display.strip() else None


def get(node, field):
    for key in field.split('.'):
        node = node.get(key) if isinstance(node, dict) else None
    return node


def _set(node, field, value):
    keys = field.split('.')
    for key in keys[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    node[keys[-1]] = value


def show(path, person, fields=SECTIONS):
    """One person's role / employment facts and their provenance (never the private view, authority or flags)."""
    if not fields or set(fields) - set(SECTIONS):
        raise ValueError('fields_must_be_role_and_or_employment')
    raw = load(path)
    target = find(raw, person)
    out = {'person': target.get('id'), 'name': display_name(target), 'slack': slack_id(target)}
    prov = target.get('provenance') if isinstance(target.get('provenance'), dict) else {}
    records = target.get('provenance_records') if isinstance(target.get('provenance_records'), dict) else {}
    for section in fields:
        value = target.get(section)
        out[section] = copy.deepcopy(value) if isinstance(value, dict) else None
    out['provenance'] = {k: v for k, v in prov.items() if isinstance(k, str) and k.split('.')[0] in fields}
    out['provenance_records'] = {k: v for k, v in records.items() if isinstance(k, str) and k.split('.')[0] in fields}
    out['revision'] = raw.get('revision')
    return out


def _flatten(patch, prefix=''):
    out = {}
    for key, value in patch.items():
        name = f'{prefix}{key}'
        if isinstance(value, dict) and name in ('role', 'employment', 'role.team'):
            out.update(_flatten(value, name + '.'))
        else:
            out[ALIASES.get(name, name)] = value
    return out


def _text(value, field, limit=80):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit or CTRL_RE.search(value):
        raise ValueError(f'{field}_must_be_text_of_1_to_{limit}_characters'.replace('.', '_'))
    return value.strip()


def validate_patch(raw, target, patch, functions=None):
    """{field: value} for the changed whitelisted fields; refuses any other field (private view, authority, flags,
    identities, …) and any malformed value. `functions` = the access pack's role functions when known."""
    if not isinstance(patch, dict) or not patch:
        raise ValueError('patch_must_be_a_non_empty_object')
    flat = _flatten(patch)
    unknown = sorted(set(flat) - set(FIELDS))
    if unknown:
        raise ValueError('field_not_allowed_' + re.sub(r'[^a-z0-9_]', '_', unknown[0].lower())[:60])
    out = {}
    for field, value in flat.items():
        if field in ('role.title', 'role.team.name'):
            value = _text(value, field)
        elif field == 'role.function':
            if not isinstance(value, str) or not SLUG_RE.fullmatch(value) or (functions and value not in functions):
                raise ValueError('role_function_must_be_one_of_' + '_'.join(functions or ('a_slug',)))
        elif field == 'role.disciplines':
            if not isinstance(value, list) or len(value) > 12 or len(set(value)) != len(value) or any(
                    not isinstance(v, str) or not SLUG_RE.fullmatch(v) for v in value):
                raise ValueError('role_disciplines_must_list_up_to_12_slugs')
        elif field == 'role.team.lead_person_id':
            if value is not None:
                if not isinstance(value, str) or value == target.get('id') or not any(
                        isinstance(p, dict) and p.get('id') == value for p in raw['people']):
                    raise ValueError('role_team_lead_person_id_must_be_another_person_in_the_registry')
        elif field == 'employment.status':
            if value not in STATUSES:
                raise ValueError('employment_status_must_be_one_of_' + '_'.join(STATUSES))
        elif field == 'employment.as_of':
            try:
                if not isinstance(value, str) or not DATE_RE.fullmatch(value):
                    raise ValueError
                dt.date.fromisoformat(value)
            except ValueError:
                raise ValueError('employment_as_of_must_be_a_date_yyyy_mm_dd') from None
        elif field == 'employment.contract':
            if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
                raise ValueError('employment_contract_must_be_a_slug')
        if get(target, field) != value:
            out[field] = value
    if not out:
        raise ValueError('nothing_to_change_the_registry_already_says_this')
    return out


@contextlib.contextmanager
def _lock(path):
    lock = Path(str(path) + '.lock')
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _private_write(path, data, mode=0o600):
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec='seconds')


def write(path, person, patch, base, *, source, by, source_link, proposal, now=None):
    """Apply a verified patch as a new revision (see the module doc). Raises ValueError('stale_…') when a field no longer
    holds the value the proposal was made against."""
    if source not in SOURCES:
        raise ValueError('unknown_source')
    now = time.time() if now is None else now
    path = Path(path).expanduser()
    with _lock(path):
        before = path.read_bytes()
        raw = load(path)
        target = find(raw, person)
        changed = [f for f in patch if get(target, f) != base.get(f)]
        if changed:
            raise ValueError('stale_registry_changed_since_the_proposal:' + changed[0])
        revisions = path.parent / 'people-revisions'
        revisions.mkdir(mode=0o700, exist_ok=True)
        backup = revisions / f'{path.stem}-{dt.datetime.fromtimestamp(now, dt.timezone.utc):%Y%m%dT%H%M%SZ}-{proposal}.json'
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(before)
        prov = target.get('provenance') if isinstance(target.get('provenance'), dict) else {}
        records = target.get('provenance_records') if isinstance(target.get('provenance_records'), dict) else {}
        for field, value in patch.items():
            _set(target, field, copy.deepcopy(value))
            prov[field] = source
            records[field] = {'source': source, 'at': _iso(now), 'by': by, 'source_link': source_link,
                              'proposal': proposal}
        target['provenance'], target['provenance_records'] = prov, records
        raw['revision'] = (raw.get('revision') if isinstance(raw.get('revision'), int) else 0) + 1
        raw['updated_at'] = _iso(now)
        mode = path.stat().st_mode & 0o777
        _private_write(path, (json.dumps(raw, ensure_ascii=False, indent=2) + '\n').encode('utf-8'), mode or 0o600)
        log = path.parent / f'{path.stem}.changes.jsonl'
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            for field, value in patch.items():
                stream.write(json.dumps({'at': _iso(now), 'ts': now, 'person': target.get('id'), 'field': field,
                                         'old': base.get(field), 'new': value, 'source': source, 'by': by,
                                         'source_link': source_link, 'proposal': proposal, 'revision': raw['revision'],
                                         'backup': str(backup)}, ensure_ascii=False) + '\n')
    return {'person': target.get('id'), 'fields': sorted(patch), 'revision': raw['revision'], 'backup': str(backup),
            'source': source}


# ---------------------------------------------------------------------------
# HR's confirmation, read back from Slack
# ---------------------------------------------------------------------------

def parse_link(link):
    """(channel, ts, thread_ts or None) of a Slack message link, else ValueError."""
    match = LINK_RE.fullmatch(str(link or '').strip())
    if not match:
        raise ValueError('hr_confirmation_must_be_a_slack_message_link')
    channel, ts = match[1], f'{match[2]}.{match[3]}'
    query = urllib.parse.parse_qs(match[4] or '')
    thread = (query.get('thread_ts') or [None])[0]
    return channel, ts, thread if thread and re.fullmatch(r'\d+\.\d+', thread) else None


def _plain(text):
    return re.sub(r'<[@!#][^>]*>', ' ', str(text or '')).strip()


def check_hr(messages, ts, hr_users, person_slack, now, max_age_days=HR_MAX_AGE_DAYS):
    """The HR message at `ts` in `messages` (one Slack thread, oldest first): its author when it is a verified
    confirmation about the person, else ValueError with the reason."""
    message = next((m for m in messages if m.get('ts') == ts), None)
    if message is None:
        raise ValueError('hr_message_not_found')
    user = message.get('user')
    if message.get('bot_id') or message.get('subtype') not in (None, 'thread_broadcast') or not user:
        raise ValueError('hr_message_is_not_a_person_s_own_message')
    if user not in hr_users:
        raise ValueError('hr_message_author_is_not_hr')
    if user == person_slack:
        raise ValueError('hr_cannot_confirm_their_own_role')
    if now - float(ts) > max_age_days * 86400:
        raise ValueError(f'hr_message_older_than_{max_age_days}_days')
    text = _plain(message.get('text'))
    if NEGATE_RE.search(text) or not AFFIRM_RE.search(text):
        raise ValueError('hr_message_is_not_a_clear_confirmation')
    mention = f'<@{person_slack}>'
    if not any(mention in str(m.get('text') or '') for m in messages if float(m.get('ts') or 0) <= float(ts)):
        raise ValueError('hr_thread_does_not_mention_the_person')
    return user


def read_thread(rt, channel, ts, thread):
    """The thread holding the message (bot identity first, then the user identity), oldest first."""
    root = thread or ts
    errors = []
    for bot in (True, False):
        try:
            token = rt.api_identity(bot=bot)
        except Exception as exc:  # noqa: BLE001 - this identity is not configured here
            errors.append(type(exc).__name__)
            continue
        try:
            data = rt.slack.api_call('conversations.replies', token, params={'channel': channel, 'ts': root, 'limit': 200})
            return data.get('messages') or []
        except Exception as exc:  # noqa: BLE001 - try the other identity
            errors.append(rt.safe_error(exc))
    raise ValueError('hr_thread_unreadable:' + ','.join(errors)[:120])


# ---------------------------------------------------------------------------
# Proposals and the card
# ---------------------------------------------------------------------------

def _functions(rt):
    try:
        return tuple(f for f in rt.rules.ACCESS_POLICY.pack.FUNCTIONS if f != 'unknown')
    except AttributeError:
        return None


def _hr_users(rt):
    try:
        spec = rt.rules.load_active('access')[0]
    except (ValueError, OSError, AttributeError):
        raise ValueError('access_rules_not_installed_no_hr_list') from None
    hr = [str(u) for u in (spec.get('defaults') or {}).get('hr') or []]
    if not hr:
        raise ValueError('access_rules_name_no_hr')
    return set(hr)


class PeopleRoles:
    """The owner-bound half: card words in the owner's language, `propose` (CLI) and the supervisor's `settle`."""

    def __init__(self, language):
        self.language = language
        self.options = [f'{choice}={self.msg(f"people.option_{choice}")}' + (f':{style}' if style else '')
                        for choice, style in OPTION_STYLES]

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def value_text(self, raw, field, value):
        if value is None or value == []:
            return self.msg('people.value_none')
        if field == 'employment.status':
            return self.msg(f'people.status_{value}')
        if field == 'role.team.lead_person_id':
            try:
                return display_name(find(raw, value)) or value
            except ValueError:
                return value
        if isinstance(value, list):
            return self.msg('people.joiner').join(value)
        return str(value)

    def card_text(self, raw, target, patch, note=None):
        """One plain sentence first (whose role, how many facts), then the changes, then what a write does."""
        name = display_name(target) or self.msg('people.someone')
        changes = self.msg('people.joiner_lines').join(
            self.msg('people.change', field=self.msg('people.field_' + f.replace('.', '_')),
                     old=self.value_text(raw, f, get(target, f)), new=self.value_text(raw, f, v)) for f, v in patch.items())
        text = self.msg('people.card', name=name, n=len(patch)) + '\n' + changes
        if note:
            text += '\n' + self.msg('people.card_note', note=note[:200])
        return text + '\n' + self.msg('people.card_effect')

    def propose(self, rt, db, person, patch, *, hr_link=None, note=None, now=None):
        """Validate the patch; with `hr_link` verify HR's confirmation and write at once, else post the card."""
        now = time.time() if now is None else now
        initialize(db)
        path = rt.envelopes.people_path()
        raw = load(path)
        target = find(raw, person)
        patch = validate_patch(raw, target, patch, _functions(rt))
        if note is not None:
            note = _text(note, 'note', 200)
        rt.asks.check_secret_free(json.dumps(patch, ensure_ascii=False) + ' ' + (note or ''))
        base = {f: copy.deepcopy(get(target, f)) for f in patch}
        pid = 'pp' + uuid.uuid4().hex[:14]
        row = {'id': pid, 'person': target.get('id'), 'patch': json.dumps(patch, ensure_ascii=False),
               'base': json.dumps(base, ensure_ascii=False), 'note': note}
        if hr_link:
            channel, ts, thread = parse_link(hr_link)
            person_slack = slack_id(target)
            if not person_slack:
                raise ValueError('person_has_no_slack_id_use_the_owner_card')
            if db.execute("SELECT 1 FROM people_proposals WHERE hr_channel=? AND hr_ts=? AND person=? AND state='written'",
                          (channel, ts, target.get('id'))).fetchone():
                raise ValueError('hr_confirmation_already_used_for_this_person')
            hr_users = _hr_users(rt)  # before any Slack read
            hr_user = check_hr(read_thread(rt, channel, ts, thread), ts, hr_users, person_slack, now)
            link = f'https://slack.com/archives/{channel}/p{ts.replace(".", "")}' + (f'?thread_ts={thread}' if thread else '')
            with rt.transaction(db):
                db.execute('INSERT INTO people_proposals (id,person,patch,base,note,source,hr_channel,hr_ts,hr_user,hr_link,'
                           "state,created_at,updated_at) VALUES (?,?,?,?,?,'hr_confirmed',?,?,?,?,'writing',?,?)",
                           (pid, row['person'], row['patch'], row['base'], note, channel, ts, hr_user, link, now, now))
            return {'ok': True, 'proposal': pid, 'action': 'written',
                    **self._write(rt, db, pid, row['person'], patch, base, 'hr_confirmed', hr_user, link, now)}
        with rt.transaction(db):
            db.execute('INSERT INTO people_proposals (id,person,patch,base,note,source,state,created_at,updated_at) '
                       "VALUES (?,?,?,?,?,'owner_card','posting',?,?)", (pid, row['person'], row['patch'], row['base'], note,
                                                                         now, now))
        try:
            card = rt.supervisor_card(db, self.card_text(raw, target, patch, note), self.options, kind='people_proposal_card',
                                      footer=self.msg('people.footer'), refs={'proposal_id': pid}, now=now)
        except Exception as exc:  # noqa: BLE001 - recorded; the agent may propose again
            with rt.transaction(db):
                db.execute("UPDATE people_proposals SET state='failed',error=?,updated_at=? WHERE id=?",
                           (rt.safe_error(exc), now, pid))
            raise ValueError('people_card_failed:' + rt.safe_error(exc)) from None
        with rt.transaction(db):
            db.execute("UPDATE people_proposals SET state='open',card_event=?,card_channel=?,card_ts=?,updated_at=? WHERE id=?",
                       (card['event_id'], card['channel'], card['thread_ts'], now, pid))
        return {'ok': True, 'proposal': pid, 'action': 'carded', 'card': card['event_id'], 'fields': sorted(patch)}

    def _write(self, rt, db, pid, person, patch, base, source, by, link, now):
        try:
            result = write(rt.envelopes.people_path(), person, patch, base, source=source, by=by, source_link=link,
                           proposal=pid, now=now)
        except (ValueError, OSError) as exc:
            code = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            state = 'stale' if code.startswith('stale_') else 'failed'
            with rt.transaction(db):
                db.execute('UPDATE people_proposals SET state=?,error=?,updated_at=? WHERE id=?', (state, code[:200], now, pid))
            raise ValueError(code) from None
        with rt.transaction(db):
            db.execute("UPDATE people_proposals SET state='written',written_at=?,backup=?,updated_at=? WHERE id=?",
                       (now, result['backup'], now, pid))
        return result

    def settle(self, rt, db, now=None):
        """Supervisor step: an approver's click on a people card (an out-of-office delegate's does not count) writes or
        declines the proposal, then says so in the card thread."""
        now = time.time() if now is None else now
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='people_proposals'").fetchone():
            return []
        done = []
        for row in db.execute("SELECT * FROM people_proposals WHERE state='open' AND card_event IS NOT NULL").fetchall():
            d = db.execute('SELECT d.*, c.options FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                           'WHERE d.event_id=?', (row['card_event'],)).fetchone()
            if d is None or d['role'] != 'approver':
                continue
            choice = rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))['id']
            with rt.transaction(db):
                db.execute('UPDATE people_proposals SET decided_by=?,decided_at=?,updated_at=? WHERE id=?',
                           (d['actor'], d['decided_at'], now, row['id']))
            if choice != 'write':
                with rt.transaction(db):
                    db.execute("UPDATE people_proposals SET state='declined',updated_at=? WHERE id=? AND state='open'",
                               (now, row['id']))
                rt.rules._card_reply(rt, db, row['card_event'], self.msg('people.reply_skip'), row['id'],
                                     kind='people_proposal_result')
                done.append({'id': row['id'], 'choice': choice})
                continue
            link = f'https://slack.com/archives/{row["card_channel"]}/p{str(row["card_ts"]).replace(".", "")}'
            try:
                result = self._write(rt, db, row['id'], row['person'], json.loads(row['patch']), json.loads(row['base']),
                                     'owner_approved', d['actor'], link, now)
                reply = self.msg('people.reply_written', n=len(result['fields']), revision=result['revision'])
            except ValueError as exc:
                reply = self.msg('people.reply_stale' if str(exc).startswith('stale_') else 'people.reply_failed',
                                 error=str(exc)[:120])
                result = {'error': str(exc)}
            rt.rules._card_reply(rt, db, row['card_event'], reply, row['id'], kind='people_proposal_result')
            done.append({'id': row['id'], 'choice': choice, **result})
        return done
