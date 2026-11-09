"""Event envelope: what an event is, who wrote it and what authority it carries, derived by code, never by a model.

Every event gets kind / author_kind / trust / authority / thread_root / burst_id / hops, plus the rule that decided
it and, for agent messages, the sender (provider, session, task). They live in one additive side table
(`event_envelopes`, 1:1 with events), so a rollback needs no schema change. The only delivery effect is the self-wake
suppression of messages the instance's own agents sent (`self_wake`).

Authority comes only from the owner (and an owner-listed delegate in their window): the owner hand-typing is
`<key>_intent`; a verified button decision is `decision_verified`; everything else is `none`.

The owner's own trust class and intent are named by the owner key (owner.key in the deployment profile): trust
`<key>`, authority `<key>_intent`, rules `<key>_hand_typed` / `<key>_non_message`. A deployment keeps the bytes it has
stored by keeping its key. Channel-specific reading of a message (metadata, app ids, guest flags) is the channel
adapter's (kage.adapters.slack.envelope).
"""
from __future__ import annotations
import json
from pathlib import Path
import re

KINDS = ('slack_message', 'decision_answer', 'ask_answer', 'routine', 'agent_message', 'job_result',
         'control_message', 'owner_relay')  # Phase 2 C1: the owner's control message; its words relayed to a task
AUTHOR_KINDS = ('human', 'agent', 'system')
FIELDS = ('kind', 'author_kind', 'trust', 'authority', 'thread_root', 'burst_id', 'hops')
USER_RE = re.compile(r'[UW][A-Z0-9]+')
DDL = '''
  CREATE TABLE IF NOT EXISTS event_envelopes (
    event_id TEXT PRIMARY KEY, kind TEXT, author_kind TEXT, trust TEXT, authority TEXT,
    thread_root TEXT, burst_id TEXT, hops INTEGER, rule TEXT, sender_provider TEXT,
    sender_session TEXT, sender_task_cwd TEXT, created_at REAL NOT NULL);
'''


def initialize(db):
    db.executescript(DDL)


# ---- the people registry: employment / entity / identity status only, never private notes ----

def index_people(raw):
    """Chat user id -> {'employment_status', 'entity', 'slack_status', 'guest'} from a people registry document."""
    out = {}
    people = raw.get('people') if isinstance(raw, dict) else None
    for person in people if isinstance(people, list) else []:
        if not isinstance(person, dict):
            continue
        employment = person.get('employment') if isinstance(person.get('employment'), dict) else {}
        status, entity = employment.get('status'), employment.get('entity')
        info = {'employment_status': status if isinstance(status, str) else None,
                'entity': entity if isinstance(entity, str) else None}
        for ident in person.get('identities') or []:
            if not isinstance(ident, dict) or ident.get('type') != 'slack':
                continue
            value = str(ident.get('value') or '')
            if USER_RE.fullmatch(value):
                out[value] = {**info, 'slack_status': ident.get('status') if isinstance(ident.get('status'), str) else None,
                              'guest': ident.get('guest') is True}
    return out


class People:
    """The people registry file, read again only when it changes. `path` is a callable (read on every load)."""

    def __init__(self, path):
        self.path = path
        self.cache = {}

    def load(self, path=None):
        """Chat user id -> employment / entity facts. A missing or malformed file means no facts."""
        path = Path(path).expanduser() if path else Path(self.path())
        try:
            st = path.stat()
        except OSError:
            return {}
        key = (str(path), st.st_mtime_ns, st.st_size)
        if self.cache.get('key') != key:
            try:
                value = index_people(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                value = {}
            self.cache.clear()
            self.cache.update(key=key, value=value)
        return self.cache['value']


def registry_external(info):
    if not info:
        return False
    entity = info.get('entity') or ''
    return (info.get('guest') or info.get('slack_status') in ('guest', 'foreign')
            or info.get('employment_status') in ('external', 'left') or entity.startswith('external:'))


def active_delegates(config, ts, buttons):
    """Delegates listed in the decisions config whose out-of-office window covers `ts`."""
    try:
        if config and buttons.ooo_in_window(config, float(ts)):
            return set(config['ooo']['delegates'])
    except (KeyError, TypeError, ValueError):
        pass
    return set()


def hops(value):
    try:
        return max(0, min(int(value), 1000))
    except (TypeError, ValueError):
        return 0


def envelope(kind, author_kind, trust, authority, rule, thread_root=None, hops=0, sender=None):
    sender = sender or {}
    return {'kind': kind, 'author_kind': author_kind, 'trust': trust, 'authority': authority,
            'thread_root': thread_root, 'burst_id': thread_root, 'hops': hops, 'rule': rule,
            'sender_provider': sender.get('provider'), 'sender_session': sender.get('session'),
            'sender_task_cwd': sender.get('task_cwd')}


class Envelopes:
    """The owner-dependent part: trust classes and authorities named by the owner key."""

    def __init__(self, *, owner_key, owner_user):
        self.key, self.owner_user = owner_key, owner_user
        self.trusts = (owner_key, 'delegate', 'colleague', 'external', 'agent', 'system')
        self.authorities = ('none', f'{owner_key}_intent', 'decision_verified', 'routine_owner')
        self.owner_trust, self.owner_intent = owner_key, f'{owner_key}_intent'
        self.rule_hand_typed, self.rule_non_message = f'{owner_key}_hand_typed', f'{owner_key}_non_message'
        self.owner_like = frozenset  # 0.77.0: who else counts as the owner (kage.app wires authority.owner_like_users)

    def person_trust(self, user, *, delegates=(), other_team=False, guest=False, people=None):
        """(trust, rule) of a human user. Names, mentions and text never change it; the channel adapter says whether
        the user is from another workspace (`other_team`) or a guest account (`guest`)."""
        if user == self.owner_user:
            return self.owner_trust, self.owner_trust
        if user and user in self.owner_like():
            return self.owner_trust, 'owner_like_approver'
        if user in delegates:
            return 'delegate', 'ooo_delegate'
        if other_team:
            return 'external', 'external_team'
        if guest:
            return 'external', 'external_guest'
        if registry_external((people or {}).get(user)):
            return 'external', 'external_registry'
        return 'colleague', 'colleague'

    def interaction(self, kind, actor, *, thread_root=None, verified_decision=False, system=False, role=None,
                    delegates=(), people=None):
        """Envelope of a runtime-created event (card click, typed confirmation, expiry)."""
        if system:
            return envelope(kind, 'system', 'system', 'none', 'system_runtime', thread_root)
        trust, rule = self.person_trust(str(actor or ''), delegates=set(delegates) | (
            {actor} if role == 'ooo_delegate' else set()), people=people)
        return envelope(kind, 'human', trust, 'decision_verified' if verified_decision else 'none',
                        'verified_decision' if verified_decision else rule, thread_root)


def self_wake(env, w, provider, task_of):
    """True when an agent-authored message would wake the session or task that sent it."""
    if not env or env.get('author_kind') != 'agent':
        return False
    if env.get('sender_session') and env['sender_session'] == w.get('session') and \
            (env.get('sender_provider') or provider) == provider:
        return True
    sender_task = task_of(env['sender_task_cwd']) if env.get('sender_task_cwd') else None
    return sender_task is not None and sender_task == task_of(w.get('cwd'))


def store(db, event_id, env, now):
    """Inside the caller's transaction; an envelope failure never blocks the event."""
    if not env:
        return
    try:
        db.execute('INSERT OR IGNORE INTO event_envelopes (event_id,kind,author_kind,trust,authority,thread_root,'
                   'burst_id,hops,rule,sender_provider,sender_session,sender_task_cwd,created_at) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (event_id, env['kind'], env['author_kind'], env['trust'], env['authority'], env['thread_root'],
                    env['burst_id'], env['hops'], env['rule'], env['sender_provider'], env['sender_session'],
                    env['sender_task_cwd'], now))
    except Exception:  # noqa: BLE001 - shadow data must never break ingest
        pass


def lookup(db, event_id):
    try:
        row = db.execute('SELECT * FROM event_envelopes WHERE event_id=?', (event_id,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return {k: row[k] for k in (*FIELDS, 'rule')}


def prompt_suffix(env):
    if not env:
        return ''
    return (f" envelope=kind:{env['kind']},author:{env['author_kind']},trust:{env['trust']},"
            f"authority:{env['authority']},hops:{env['hops']}")
