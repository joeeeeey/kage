"""P3.1 capability blockers (能力阻塞) and the automation gap ledger (自动化缺口台账). Deterministic, no model.

A capability blocker is something an agent cannot do by itself: no API or skill for the operation, a credential that
is missing or expired, a verification code, missing permission or a UI-only step. Decision blockers (the owner has to choose)
stay with ask-<owner key> / rule-check. P47: waiting on a person is never a blocker (the external_owner type is gone, R-0004:
`ask` with a deadline, followed up at least twice), and a card from the CLI passes the card gate (card_gate).

- `report-blocker` (agent session) records one `automation_gaps` row and posts ONE assistant card to the owner in the
  the owner-only decision channel. The card is ask-<owner key>'s card + decision watch, owned by the reporting session, so clicks
  and typed replies come back to it as events. Type-specific default options choose HOW to get unblocked; the card
  authorizes nothing (the task's own authorization and rule-check still apply). A blocker still waiting for the owner with
  the same task + system + operation is never carded twice: the existing one is returned.
- the owner doing it himself ([我来网页操作] / [我去 1Password 补凭证]) gets a second card from the supervisor
  ([已完成] / [已放好]); every click is an event to the reporting session, which verifies by read-back and then runs
  `resolve-blocker --how <owner key>_manual|credential_added|agent_browser|skill_built|skipped`.
- mfa_code: the owner replies with the code in the card thread. It is input, never stored: the card watches are
  `secret_input` (event digests hash no text), `context --event` masks it, and `blocker-code` hands it straight to a
  command's stdin / env / `{code}` argument and masks it in that command's output.
- Supervisor (`settle`, every 10 s): records choices, posts the stage-2 cards, one reminder per card that waits longer
  than `blockers.reminder_hours` (24), stops the card watches of resolved blockers, and applies decided weekly skill
  proposals (`settle_skill_proposals`: bootstrap a workspace + launch its first session headless).
P45: the P3.1 run-end guard (a card when a run wrote blocker lines only into status.md) is gone; its `blocker_guard`
table stays (DDL only) so old databases and the card-destination check keep their rows.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from kage import locale

TYPES = ('no_api', 'no_skill', 'auth_missing', 'auth_expired', 'mfa_code', 'permission', 'ui_only')


REMOVED_TYPES = ('external_owner',)  # P47 (R-0004): refused with the ask hint; old rows keep their TYPE_ZH label


UNRESOLVED = ('open', 'in_progress')


GAP_ID_RE = re.compile(r'bk[0-9a-f]{14}')


PROPOSAL_ID_RE = re.compile(r'gp[0-9a-f]{14}')


SYSTEM_RE = re.compile(r'[a-z0-9][a-z0-9._-]{0,39}')


SLUG_RE = re.compile(r'[a-z0-9][a-z0-9-]{2,50}')


FIRST_LINE_MAX = 120   # P3.1 copy standard: the card's first line (operation names are shortened to fit)


STAGE2_ATTEMPTS = 3


POSTING_STALE_SECONDS = 300


CLOSE_GIVE_UP_SECONDS = 7 * 86400


CODE_MAX_AGE_SECONDS = 1800


CODE_OUTPUT_LIMIT = 20000


DASHBOARD_URL = 'http://127.0.0.1:8776'


SMOKE_SYSTEM = 'smoke-test'


def config(rt):
    try:
        return rt.worker_config.load()[0]['blockers']
    except Exception:  # noqa: BLE001 - a malformed config never posts cards or launches tasks by accident
        return {**rt.worker_config.DEFAULTS['blockers'], 'enabled': False, 'skill_proposals_enabled': False}


def _db_path(db):
    row = next((r for r in db.execute('PRAGMA database_list') if r[1] == 'main'), None)
    return row[2] if row else None


def normalize_system(value):
    text = str(value or '').strip().lower()
    if not SYSTEM_RE.fullmatch(text):
        raise ValueError('system_must_be_a_short_slug_like_authentik_or_aws-console')
    return text


def _line(value, field, cap):
    text = ' '.join(str(value or '').split())
    if not text or len(text) > cap:
        raise ValueError(f'{field}_must_be_1_to_{cap}_characters')
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError(f'{field}_must_be_plain_text')
    return text


def op_key(operation):
    """Dedupe key of an operation: case, whitespace and trailing punctuation do not make a new path."""
    return ' '.join(str(operation).lower().split()).strip(' .。,，;；:：!！?？')


def _gap(db, gap_id):
    if not GAP_ID_RE.fullmatch(str(gap_id or '')):
        raise ValueError('blocker_id_must_look_like_bk + 14 hex')
    row = db.execute('SELECT * FROM automation_gaps WHERE id=?', (gap_id,)).fetchone()
    if row is None:
        raise ValueError('unknown_blocker')
    return row


def _fit(template, operation, limit=FIRST_LINE_MAX):
    """`template` with {op}: the operation shortened so the whole line stays within `limit` characters (the line is
    cut as a last resort, so a card is never refused for its own first line)."""
    room = limit - len(template.replace('{op}', ''))
    op = operation if len(operation) <= room else operation[:max(room - 1, 1)] + '…'
    line = template.replace('{op}', op)
    return line if len(line) <= limit else line[:limit - 1] + '…'


def gap_url(gap_id):
    return f'{DASHBOARD_URL}/gaps/{gap_id}'


def _summary(row, **extra):
    return {'ok': True, 'blocker_id': row['id'], 'status': row['status'], 'task_id': row['task_id'],
            'system': row['system'], 'operation': row['operation'], 'type': row['type'],
            'occurrences': row['occurrences'], 'attempt': row['attempt'],
            'card': {'event_id': row['card_event'], 'channel': row['card_channel'], 'thread_ts': row['card_ts']}
            if row['card_event'] else None,
            'options': [{k: o[k] for k in ('id', 'label', 'resolution')} for o in json.loads(row['options'])],
            'smoke': bool(row['smoke']), **extra}


def _current(db, task_id, system, key):
    return db.execute("SELECT * FROM automation_gaps WHERE task_id=? AND system=? AND op_key=? AND status IN "
                      "('open','in_progress') ORDER BY created_at DESC LIMIT 1", (task_id, system, key)).fetchone()


SECRET_NEXT = ('secret_input: this thread may carry a verification code. It is masked here. Pass it straight to the '
               'command that needs it: blocker-code --id {id} --event {event} --stdin|--env NAME -- CMD (or a {{code}} '
               'argument). Never print, store, log or quote it (status.md, notes, Slack, files).')


def _option_label(row, option_id):
    return next((o['label'] for o in json.loads(row['options']) if o['id'] == option_id), option_id)


def annotation(rt, db, eid):
    """` blocker=<id>:stage1|stage2|proposal|reply` (+ ` secret_input`) for the queued-prompt line; never text."""
    try:
        row = db.execute('SELECT g.id, g.card_event, g.type, d.event_id FROM decisions d JOIN automation_gaps g ON '
                         'g.card_event=d.event_id OR g.stage2_event=d.event_id WHERE d.delivered_event=?',
                         (eid,)).fetchone()
        if row is not None:
            stage = 'stage1' if row['card_event'] == row['event_id'] else 'stage2'
            return f' blocker={row["id"]}:{stage}' + (' secret_input' if row['type'] == 'mfa_code' else '')
        e = db.execute('SELECT watch_id FROM events WHERE id=?', (eid,)).fetchone()
        w = rt.load(db, e['watch_id']) if e else {}
    except (sqlite3.Error, ValueError, KeyError):
        return ''
    if not w.get('blocker_id'):
        return ''
    kind = 'proposal' if _rejected_proposal(db, eid) else 'reply'
    return f' blocker={w["blocker_id"]}:{kind}' + (' secret_input' if w.get('secret_input') else '')


def _rejected_proposal(db, eid):
    """A "✏️ 不对" click on propose-decision's confirmation is an event on the card watch too (not a typed reply)."""
    return db.execute('SELECT 1 FROM decision_proposals WHERE rejected_event=?', (eid,)).fetchone() is not None


_MASK_RE = re.compile(r'(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{2,}[A-Za-z0-9](?![A-Za-z0-9])'
                      r'|(?<![A-Za-z0-9])[A-Z]{4,12}(?![A-Za-z0-9])'
                      r'|(?<!\d)\d{2,4}(?:[ -]\d{2,4}){1,3}(?!\d)')


MASK = '••••'


def mask_codes(text):
    """Mask anything that could be a one-time code (tokens with a digit, all-caps tokens, digit groups)."""
    return _MASK_RE.sub(MASK, str(text or ''))


def extract_code(text):
    """The code in the owner's reply: the whole reply when it is one short token, else the only 4-10 digit number, else the
    only short token with a digit. None when missing or ambiguous (never guessed)."""
    raw = re.sub(r'<[^>]*>', ' ', html.unescape(str(text or '')))
    compact = raw.strip(' \t\r\n.。,，:：;；!！`*_"\'“”')
    if (re.fullmatch(r'[A-Za-z0-9]{4,12}(?:-[A-Za-z0-9]{2,8})?', compact) and re.search(r'\d', compact)) or \
            re.fullmatch(r'[A-Z]{4,10}', compact):
        return compact
    joined = re.sub(r'[\s-]', '', compact)
    if re.fullmatch(r'\d{4,10}', joined):
        return joined
    digits = set(re.findall(r'(?<![A-Za-z0-9-])\d{4,10}(?![A-Za-z0-9-])', raw))
    if len(digits) == 1:
        return digits.pop()
    if digits:
        return None
    tokens = {t for t in re.findall(r'(?<![A-Za-z0-9-])[A-Za-z0-9]{2,8}(?:-[A-Za-z0-9]{2,8})?(?![A-Za-z0-9-])', raw)
              if re.search(r'\d', t) and 4 <= len(t) <= 16}
    if len(tokens) != 1:
        return None
    token = tokens.pop()
    return token.replace('-', '') if re.fullmatch(r'\d{2,5}-\d{2,5}', token) else token  # 123-456 -> 123456


def _decision(rt, db, card_event):
    d = db.execute('SELECT d.*, c.options FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                   'WHERE d.event_id=?', (card_event,)).fetchone()
    if d is None:
        return None
    return d, rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))


def _record_choices(rt, db, now, cfg):
    """the owner's clicks -> choice / stage2_choice. Recently resolved blockers are included: the agent may resolve (after
    its read-back) before this step saw the click that triggered it."""
    out = []
    for row in db.execute("SELECT * FROM automation_gaps WHERE (status IN ('open','in_progress') OR resolved_at>?) AND "
                          '((card_event IS NOT NULL AND choice IS NULL) OR (stage2_event IS NOT NULL AND '
                          'stage2_choice IS NULL))', (now - 86400,)).fetchall():
        if row['card_event'] and row['choice'] is None:
            found = _decision(rt, db, row['card_event'])
            if found:
                d, option = found
                with rt.transaction(db):
                    db.execute("UPDATE automation_gaps SET choice=?,chosen_by=?,chosen_at=?,status=CASE WHEN status='open' "
                               "THEN 'in_progress' ELSE status END,updated_at=? WHERE id=? AND choice IS NULL",
                               (option['id'], d['actor'], d['decided_at'], now, row['id']))
                out.append((row['id'], 'stage1', option['id']))
        if row['stage2_event'] and row['stage2_choice'] is None:
            found = _decision(rt, db, row['stage2_event'])
            if found:
                d, option = found
                with rt.transaction(db):
                    db.execute('UPDATE automation_gaps SET stage2_choice=?,stage2_by=?,stage2_at=?,updated_at=? WHERE id=? '
                               'AND stage2_choice IS NULL', (option['id'], d['actor'], d['decided_at'], now, row['id']))
                out.append((row['id'], 'stage2', option['id']))
    return out


def _post_reminder(rt, db, row, channel, ts, text, column, now):
    try:
        sent = rt.slack.api_call('chat.postMessage', rt.api_identity(bot=True), json_body={
            'channel': channel, 'thread_ts': ts, 'text': text, 'unfurl_links': False, 'unfurl_media': False,
            'metadata': rt.agent_metadata('blocker_reminder', None, blocker_id=row['id'])})
        rt.record_send(db, channel, (sent or {}).get('ts'), 'blocker_reminder', row['id'])
        with rt.transaction(db):
            db.execute(f'UPDATE automation_gaps SET {column}=?,updated_at=? WHERE id=?', ((sent or {}).get('ts'), now,
                                                                                         row['id']))
        return 'reminded'
    except Exception as exc:  # noqa: BLE001 - one reminder only: a failed post is not retried
        with rt.transaction(db):
            db.execute('UPDATE automation_gaps SET close_error=? WHERE id=?', ('reminder_failed:' + rt.safe_error(exc),
                                                                               row['id']))
        return 'failed'


def _stop_watches(rt, db, now, cfg):
    """Resolved / superseded blockers: stop their card watches once no event of them is pending (no Slack call)."""
    out = []
    for row in db.execute("SELECT * FROM automation_gaps WHERE status IN ('resolved','superseded') AND cards_closed_at "
                          'IS NULL').fetchall():
        done = True
        for eid in (row['card_event'], row['stage2_event']):
            if not eid:
                continue
            esc = db.execute('SELECT watch_id FROM escalations WHERE event_id=?', (eid,)).fetchone()
            if not esc or not esc['watch_id']:
                continue
            with rt.transaction(db):
                try:
                    w = rt.load(db, esc['watch_id'])
                except ValueError:
                    continue
                if w['state'] in ('stopped', 'expired'):
                    continue
                if db.execute("SELECT 1 FROM events WHERE watch_id=? AND status!='handled'", (w['id'],)).fetchone():
                    done = False
                    continue
                w.update(state='stopped', stop_reason='blocker_resolved', stopped_at=now)
                rt.save(db, w)
                if rt.claude.provider(w) == 'claude':
                    rt.claude.resolved(db, w['session'])
        if done or now - (row['resolved_at'] or now) > CLOSE_GIVE_UP_SECONDS:
            with rt.transaction(db):
                db.execute('UPDATE automation_gaps SET cards_closed_at=?,close_error=coalesce(close_error,?) WHERE id=?',
                           (now, None if done else 'pending_events_left', row['id']))
            out.append(row['id'])
    return out


def _bucket(counter, key, n=1):
    counter[key] = counter.get(key, 0) + n


def _cjk(text):
    return bool(re.search('[㐀-䶿一-鿿]', text or ''))


def fingerprint(system, operation):
    return hashlib.sha256(f'{normalize_system(system)}\n{op_key(operation)}'.encode()).hexdigest()[:16]


def smoke_workspace(rt, now):
    stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))
    path = rt.registry.smoke_root() / f'{rt.registry.SMOKE_PREFIX}{stamp}-blocker-{uuid.uuid4().hex[:4]}'
    path.mkdir(parents=True, mode=0o700)
    (path / 'history').mkdir()
    (path / 'goal.md').write_text('# TEST smoke-blocker\n\nThrowaway workspace for the report-blocker end-to-end test. '
                                  'Nothing here is real work.\n', encoding='utf-8')
    (path / 'status.md').write_text(f'<!-- rev: 1 · last-writer: smoke-blocker · at: {rt.task_memory.utc_iso(now)} -->\n'
                                    '# Status\n\n- TEST workspace created by smoke-blocker.\n', encoding='utf-8')
    return path


def synthetic_decision(rt, db, card_event, option_id, now):
    """TEST cards only: record a decision without a Slack click and deliver it like `decide` would (event on the card
    watch); the card's buttons are replaced by the outcome line, attributed to the assistant (never to the owner)."""
    card = db.execute('SELECT * FROM escalation_cards WHERE event_id=?', (card_event,)).fetchone()
    esc = db.execute('SELECT * FROM escalations WHERE event_id=?', (card_event,)).fetchone()
    if card is None or esc is None or not card['smoke']:
        raise ValueError('synthetic_decisions_are_for_test_cards_only')
    model = rt.buttons.card_model(card['options'])
    choice = rt.buttons.normalize_choice(option_id, model)
    with rt.transaction(db):
        db.execute('INSERT OR IGNORE INTO decisions (event_id,choice,actor,role,decided_at,card_channel,card_ts,stage,'
                   'provenance) VALUES (?,?,?,?,?,?,?,?,?)', (card_event, choice, rt.BOT, 'smoke', now,
                                                              card['channel'], esc['thread_ts'], 'recorded',
                                                              'smoke_synthetic'))
    rt._deliver_decision(db, card_event, f'{now:.6f}', now)
    try:
        rt._update_card(db, card_event, model, rt.api_identity(), rt.api_identity(bot=True))
    except Exception:  # noqa: BLE001 - only the TEST card's look
        pass
    return db.execute('SELECT delivered_event FROM decisions WHERE event_id=?', (card_event,)).fetchone()[0]


def synthetic_reply(rt, db, watch_id, now):
    """TEST: an event on the card watch standing in for a typed thread reply (no Slack message, no text; the assistant
    as author, never the owner)."""
    w = rt.load(db, watch_id)
    if not w.get('smoke'):
        raise ValueError('synthetic_replies_are_for_test_watches_only')
    ts = f'{now + 0.5:.6f}'
    with rt.transaction(db):
        return rt._queue_owner_event(db, w, w['channel'], ts, w['thread'], rt.BOT, {'smoke_reply': ts}, now,
                                     rt.interaction_envelope(w, 'slack_message', rt.BOT, w['thread'], now, system=True))


def fetch_dashboard(url, timeout=5):
    """GET the local dashboard's JSON directly (never through an HTTP proxy from the environment)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url), timeout=timeout) as response:  # noqa: S310 - loopback only
        return json.loads(response.read().decode('utf-8'))


class _identity:
    """Temporarily act as the synthetic TEST session (smoke only)."""

    def __init__(self, provider, session):
        self.provider, self.session, self.saved = provider, session, {}

    def __enter__(self):
        for key in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):
            self.saved[key] = os.environ.pop(key, None)
        os.environ['CLAUDE_CODE_SESSION_ID' if self.provider == 'claude' else 'CODEX_THREAD_ID'] = self.session
        return self

    def __exit__(self, *exc):
        for key in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):
            os.environ.pop(key, None)
            if self.saved.get(key) is not None:
                os.environ[key] = self.saved[key]
        return False


class Blockers:
    """The owner-bound half of blockers: the cards and replies in the owner's words, the owner's clicks, the
    ledger views, the weekly skill proposals and the smoke."""

    def __init__(self, language, *, tz, tz_name, owner_key, owner_name, owner_user, ask_owner, worker_slug, run_id_env,
                 internal_names=()):
        self.language = language
        self.tz = tz
        self.tz_name = tz_name
        self.owner_user = owner_user
        self.ask_owner = ask_owner
        self.worker_slug = worker_slug
        self.run_id_env = run_id_env
        self.key, self.name = owner_key, owner_name
        self.type_zh = {'no_api': self.msg('blockers.type_zh.no_api'), 'no_skill': self.msg('blockers.type_zh.no_skill'), 'auth_missing': self.msg('blockers.type_zh.auth_missing'), 'auth_expired': self.msg('blockers.type_zh.auth_expired'),
                        'mfa_code': self.msg('blockers.type_zh.mfa_code'), 'permission': self.msg('blockers.type_zh.permission'), 'ui_only': self.msg('blockers.type_zh.ui_only'), 'external_owner': self.msg('blockers.type_zh.external_owner')}
        self.resolutions = (f'{self.key}_manual', 'credential_added', 'agent_browser', 'skill_built', 'skipped')
        self.resolution_zh = {f'{self.key}_manual': self.msg('blockers.resolution_zh.owner_manual', owner=self.name), 'credential_added': self.msg('blockers.resolution_zh.credential_added', owner=self.name), 'agent_browser': self.msg('blockers.resolution_zh.agent_browser'),
                              'skill_built': self.msg('blockers.resolution_zh.skill_built'), 'skipped': self.msg('blockers.resolution_zh.skipped')}
        self.owner_manual = (f'{self.key}_manual', 'credential_added')   # resolutions that cost the owner a manual step
        # `internal_names`: the deployment's own names for this runtime (its program's skill, say), never a blocker
        self.internal_systems = {'slack-followup', 'followup', 'task-followup', *internal_names, 'reminder', 'reminders',
                                 'watch', 'watcher', 'watches', 'card', 'cards', 'deadline', 'deadlines', 'self', 'agent', 'runtime',
                                 'supervisor', 'orphan', 'digital-worker', self.worker_slug, f'ask-{self.key}', 'blocker-card'}
        self._bookkeeping_verb = re.compile(self.pattern('blockers._bookkeeping_verb.1'))
        self._runtime_noun = re.compile(self.pattern('blockers._runtime_noun.1'))
        self._self_ref = re.compile(self.pattern('blockers._self_ref.1'))
        self.option_resolution = {'manual': f'{self.key}_manual', 'credential': 'credential_added', 'agent_login': 'agent_browser',
                                  'agent_build': 'skill_built', 'skip': 'skipped'}
        self.option_label = {'manual': self.msg('blockers.option_label.manual'), 'credential': self.msg('blockers.option_label.credential'),
                             'agent_login': self.msg('blockers.option_label.agent_login'), 'agent_build': self.msg('blockers.option_label.agent_build'),
                             'skip': self.msg('blockers.option_label.skip')}
        self.type_options = {
                 'no_api': ('manual', 'agent_login', 'skip'),
                 'no_skill': ('agent_build', ('manual', self.msg('blockers.type_options.1')), 'skip'),
                 'auth_missing': ('credential', 'manual', 'skip'),
                 'auth_expired': (('credential', self.msg('blockers.type_options.2')), 'agent_login', 'manual', 'skip'),
                 'mfa_code': ('manual', 'skip'),
                 'permission': (('manual', self.msg('blockers.type_options.3')), 'skip'),
                 'ui_only': ('manual', 'agent_login', 'skip'),
             }
        self.stage2 = {'manual': (self.msg('blockers.stage2.1'), [self.msg('blockers.stage2.2'), self.msg('blockers.stage2.3')]),
                       'credential': (self.msg('blockers.stage2.4'), [self.msg('blockers.stage2.5'), self.msg('blockers.stage2.6')])}
        self.skill_options = [self.msg('blockers.skill_options.1'), self.msg('blockers.skill_options.2')]
        self.proposals_schema_id = f'{self.worker_slug}.gap-proposals/v1'
        self.ddl = f'''
  CREATE TABLE IF NOT EXISTS automation_gaps (
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL, task_id TEXT NOT NULL, task_path TEXT,
    system TEXT NOT NULL, operation TEXT NOT NULL, op_key TEXT NOT NULL, type TEXT NOT NULL, tried TEXT NOT NULL,
    unblock TEXT NOT NULL, options TEXT NOT NULL DEFAULT '[]', reporter TEXT NOT NULL, provider TEXT NOT NULL,
    session TEXT NOT NULL, run_id TEXT, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, previous_id TEXT,
    superseded_by TEXT, card_event TEXT, card_channel TEXT, card_ts TEXT, card_posted_at REAL, card_error TEXT,
    card_attempts INTEGER NOT NULL DEFAULT 0, choice TEXT, chosen_by TEXT, chosen_at REAL, stage2_event TEXT,
    stage2_channel TEXT, stage2_ts TEXT, stage2_posted_at REAL, stage2_state TEXT, stage2_claimed_at REAL,
    stage2_error TEXT, stage2_attempts INTEGER NOT NULL DEFAULT 0, stage2_choice TEXT, stage2_by TEXT, stage2_at REAL,
    reminded_at REAL, reminder_ts TEXT, stage2_reminded_at REAL, stage2_reminder_ts TEXT, resolution TEXT,
    resolved_at REAL, resolved_by TEXT, resolution_note TEXT, {self.key}_manual INTEGER NOT NULL DEFAULT 0,
    occurrences INTEGER NOT NULL DEFAULT 1, last_reported_at REAL NOT NULL, cards_closed_at REAL, close_error TEXT,
    smoke INTEGER NOT NULL DEFAULT 0);
  CREATE INDEX IF NOT EXISTS automation_gaps_path ON automation_gaps(task_id, system, op_key, status);
  CREATE INDEX IF NOT EXISTS automation_gaps_time ON automation_gaps(created_at);
  CREATE INDEX IF NOT EXISTS automation_gaps_status ON automation_gaps(status, updated_at);
  CREATE INDEX IF NOT EXISTS automation_gaps_card ON automation_gaps(card_event);
  CREATE INDEX IF NOT EXISTS automation_gaps_stage2 ON automation_gaps(stage2_event);
  CREATE TABLE IF NOT EXISTS gap_proposals (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, system TEXT NOT NULL, operation TEXT NOT NULL, op_key TEXT NOT NULL,
    title_zh TEXT NOT NULL, proposal TEXT NOT NULL, proposer TEXT NOT NULL, card_event TEXT, status TEXT NOT NULL,
    created_at REAL NOT NULL, decided_by TEXT, decided_at REAL, suppress_until REAL, task_id TEXT, dispatch_id TEXT,
    launched_at REAL, error TEXT, smoke INTEGER NOT NULL DEFAULT 0);
  CREATE INDEX IF NOT EXISTS gap_proposals_status ON gap_proposals(status, created_at);
  CREATE INDEX IF NOT EXISTS gap_proposals_fingerprint ON gap_proposals(fingerprint, status);
  CREATE INDEX IF NOT EXISTS gap_proposals_card ON gap_proposals(card_event);
  -- P45: no longer written (the run-end guard is gone); kept for old rows and card-destination-check.
  CREATE TABLE IF NOT EXISTS blocker_guard (
    task_id TEXT NOT NULL, day TEXT NOT NULL, run_id TEXT NOT NULL, new_lines INTEGER NOT NULL, created_at REAL NOT NULL,
    channel TEXT, ts TEXT, error TEXT, PRIMARY KEY (task_id, day));
'''
        self.next_reported = (f"Do every part of the task that does not depend on this now; note the blocker id in status.md. {self.name}'s click or reply arrives as an event on this card (context.blocker says what it means). Once it is unblocked, verify by read-back, then resolve-blocker --id {{id}} --how {self.key}_manual|credential_added|agent_browser|skill_built|skipped.")
        self.stage1_next = {
                 'manual': (f'{self.name} will do it himself. Within about 10 s the supervisor posts a second card [✅ {{done}}]; its click arrives as a new event. Resolve this event as decision and keep doing independent work; do not poll.'),
                 'credential': (f'{self.name} will put the credential into 1Password. Within about 10 s the supervisor posts a second card [✅ {{done}}]; its click arrives as a new event. Resolve this event as decision; do not poll.'),
                 'agent_login': (f'{self.name} wants you to sign in yourself with the 1Password credential (op-agent; never print or store secrets). Do the operation, verify by read-back, then resolve-blocker --id {{id}} --how agent_browser. If it still fails, report-blocker again (same system and operation) with what you saw.'),
                 'agent_build': (f'{self.name} wants you to automate it yourself (prefer an official API with a service credential, then a CLI). Verify by read-back, then resolve-blocker --id {{id}} --how skill_built.'),
                 'skip': (f'{self.name} says skip this item: note it in status.md, resolve-blocker --id {{id}} --how skipped, and continue without it.'),
             }
        self.stage2_next = {
                 'done': (f'{self.name} says it is done. Verify by read-back before you rely on it, then resolve-blocker --id {{id}} --how {{how}}. If the read-back fails, report-blocker again with what you saw.'),
                 'skip': f'{self.name} could not do it: resolve-blocker --id {{id}} --how skipped, or report-blocker again with another path.',
             }
        self.public = ('id', 'task_id', 'system', 'operation', 'type', 'status', 'attempt', 'previous_id', 'superseded_by', 'choice',
                       'stage2_choice', 'resolution', f'{self.key}_manual', 'occurrences', 'reporter', 'run_id', 'smoke')

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def _is_internal_bookkeeping(self, system, operation):
        """True when the blocker is about the agent's OWN runtime reminder / watch / card / deadline (not a real external
        action). The system slug naming a runtime mechanism is enough; otherwise a bookkeeping verb + a runtime-mechanism
        noun + a self reference together are required (so "取消 Google Calendar 我建的提醒" on an external product is allowed)."""
        if system in self.internal_systems:
            return True
        op = str(operation or '')
        return bool(self._bookkeeping_verb.search(op) and self._runtime_noun.search(op) and self._self_ref.search(op))

    def initialize(self, db):
        db.executescript(self.ddl)

    def iso(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz).isoformat(timespec='seconds') if ts else None

    def _clock(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz).strftime('%H:%M')

    def option_specs(self, rt, btype, specs=None, recommend=None):
        """(card option specs, stored options). Ids come from the fixed vocabulary; labels may be custom."""
        if specs:
            chosen = []
            for spec in specs:
                option_id, label, _ = rt.buttons.parse_option(spec)
                if option_id not in self.option_resolution:
                    raise ValueError('blocker_option_id_must_be_one_of_' + '_'.join(self.option_resolution))
                chosen.append((option_id, label))
        else:
            chosen = [(o, self.option_label[o]) if isinstance(o, str) else o for o in self.type_options[btype]]
        cards = [f'{i}={label}:{"reject" if i == "skip" else "other"}' for i, label in chosen]
        validated = rt.buttons.validate_options(cards, recommend)  # 2-4, unique, labels <= 30 characters
        stored = [{'id': o['id'], 'label': o['label'], 'resolution': self.option_resolution[o['id']],
                   'stage2': o['id'] in self.stage2} for o in validated]
        return cards, stored

    def waiting_on_owner(self, row):
        """No answer yet, or the owner chose to do it himself and has not clicked the stage-2 card."""
        return row['status'] == 'open' or (row['status'] == 'in_progress' and row['choice'] in self.stage2 and
                                           row['stage2_choice'] is None)

    def card_footer(self, row, smoke=False):
        return (self.msg('blockers.card_footer.1') if smoke else '') + self.msg('blockers.card_footer.2', gap_url=gap_url(row['id']))

    def card_question(self, row, smoke=False, previous=None, shown=None):
        """Stage-1 card (P3.1 copy standard): one plain sentence, how to unblock, then the code / retry / keep-going line.
        What the agent tried is on the dashboard (/gaps/<id>, linked from the footer). P47 `shown` (card_gate): the
        decision, situation and checked lines lead, then where it is stuck."""
        test = '🧪 TEST · ' if smoke else ''
        lines = [_fit(test + self.msg('blockers.card_question.1', system=row['system'], value=self.type_zh[row['type']]),
                      row['operation']),
                 self.msg('blockers.card_question.2') + row['unblock']]
        if shown:
            head = [test + shown['decide'], _fit(self.msg('blockers.card_question.3', system=row['system'], value=self.type_zh[row['type']]), row['operation'])] \
                if shown['decide'] else lines[:1]
            lines = [head[0], *shown['lines'], *head[1:], *lines[1:]]
        if row['type'] == 'mfa_code':
            lines.append(self.msg('blockers.card_question.4'))
        elif previous is not None:
            label = next((o['label'] for o in json.loads(previous['options']) if o['id'] == previous['choice']),
                         previous['choice'] or '—')
            lines.append(self.msg('blockers.card_question.5', attempt=row['attempt'], label=label))
        else:
            lines.append(self.msg('blockers.card_question.6'))
        return '\n'.join(lines)

    def _watch_extra(self, row, stage):
        extra = {'label': 'report-blocker' if stage == 1 else 'report-blocker · ' + self.stage2[row['choice']][0],
                 'blocker_id': row['id'], 'blocker_stage': stage}
        if row['type'] == 'mfa_code':
            extra['secret_input'] = True
        return extra

    def report_blocker(self, rt, db, db_path, *, system, operation, type, tried, unblock, task=None, options=None,
                       recommend=None, now=None, gate=None):
        """CLI: the calling agent session reports a capability blocker (identity from the native environment). P47 `gate`:
        the card fields from the CLI (card_gate)."""
        provider, session = rt.claude.actor()
        if not session:
            raise ValueError('report_blocker_requires_an_agent_session')
        return self.report(rt, db, db_path, provider=provider, session=session, system=system, operation=operation,
                      btype=type, tried=tried, unblock=unblock, task=task, options=options, recommend=recommend, now=now,
                      gate=gate)

    def report(self, rt, db, db_path, *, provider, session, system, operation, btype, tried, unblock, task=None, options=None,
               recommend=None, now=None, smoke=False, live_card=False, gate=None):
        """Record (or deduplicate) a blocker and post its card. `provider` / `session` are verified by the caller.
        A smoke blocker card is dry unless `live_card` (and stage-2 inherits the stage-1 card's dry state). P47 `gate`
        (CLI only): checked before any row; a new card counts against the task's open-card limit (a re-report that
        supersedes its own card does not)."""
        now = time.time() if now is None else now
        uuid.UUID(session)
        provider = rt.claude.provider({'provider': provider})
        if btype in REMOVED_TYPES and rt.rule_registry.active('card.not_waiting_on_person'):
            rt.rule_registry.refuse('card.not_waiting_on_person', 'type_external_owner_was_removed: waiting on an outside '
                                                                  'owner is a person to follow up, not a blocker')
        if btype not in TYPES:
            raise ValueError('type_must_be_one_of_' + '|'.join(TYPES))
        system = normalize_system(system)
        operation = _line(operation, 'operation', 160)
        tried, unblock = _line(tried, 'tried', 500), _line(unblock, 'unblock', 500)
        rt.asks.check_secret_free(operation, tried, unblock)
        if self._is_internal_bookkeeping(system, operation):  # P34: not a real external blocker, do not card the owner
            raise ValueError(self.msg('blockers.report.1'))
        for field, value in (('system', system), ('operation', operation), ('tried', tried), ('unblock', unblock)):
            rt.buttons.check_copy(value, field, first_line_max=None)  # P3.1 copy standard: the owner reads these
        shown = rt.card_gate.check(rt, gate, text=f'{system} {operation} {unblock}', failure=btype in
                                   rt.card_gate.FAILURE_TYPES) if gate is not None else None
        cards, stored = self.option_specs(rt, btype, options, recommend)
        row = rt._resolve_task_arg(db, task or os.getcwd())
        task_id, task_path = row['task_id'], row['path']
        if not Path(task_path).is_dir():
            raise ValueError('task_workspace_missing')
        smoke = bool(smoke or rt.registry.is_smoke_task(task_id))
        key = op_key(operation)
        if gate is not None:
            current = _current(db, task_id, system, key)
            if not (current is not None and self.waiting_on_owner(current)):  # a deduplicated report posts no card
                rt.card_gate.open_limit(rt, db, task_id, current['card_event'] if current is not None else None, now)
        run_id = os.environ.get(self.run_id_env) or None
        previous = None
        with rt.transaction(db):
            current = _current(db, task_id, system, key)
            if current is not None and self.waiting_on_owner(current):
                posting = current['card_error'] == 'posting' and now - current['updated_at'] < POSTING_STALE_SECONDS
                db.execute('UPDATE automation_gaps SET occurrences=occurrences+1,last_reported_at=?,updated_at=CASE WHEN ? '
                           "THEN updated_at ELSE ? END,card_error=CASE WHEN card_event IS NULL THEN 'posting' ELSE card_error "
                           'END WHERE id=?', (now, int(posting), now, current['id']))
                gap = db.execute('SELECT * FROM automation_gaps WHERE id=?', (current['id'],)).fetchone()
                if current['card_event'] or posting:  # carded, or another report is posting its card right now
                    return _summary(gap, deduped=True, next=f'Already reported and still waiting for {self.name}: no new card. '
                                                           + self.next_reported.format(id=gap['id']))
            else:
                gid = 'bk' + uuid.uuid4().hex[:14]
                attempt = 1
                if current is not None:
                    # the owner's chosen path did not unblock it (or he finished stage 2 and the read-back still fails):
                    # a new report supersedes the old one and asks again.
                    previous = current
                    attempt = current['attempt'] + 1
                    db.execute("UPDATE automation_gaps SET status='superseded',superseded_by=?,resolved_at=?,updated_at=? "
                               'WHERE id=?', (gid, now, now, current['id']))
                db.execute('INSERT INTO automation_gaps (id,created_at,updated_at,task_id,task_path,system,operation,op_key,'
                           'type,tried,unblock,options,reporter,provider,session,run_id,status,attempt,previous_id,'
                           'last_reported_at,card_error,smoke) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (gid, now, now, task_id, task_path, system, operation, key, btype, tried, unblock,
                            json.dumps(stored, ensure_ascii=False), f'{provider}:{session}', provider, session, run_id,
                            'open', attempt, previous['id'] if previous else None, now, 'posting', int(smoke)))
                gap = db.execute('SELECT * FROM automation_gaps WHERE id=?', (gid,)).fetchone()
        if previous is not None:
            self.close_cards(rt, db, previous, self.msg('blockers.report.2') + gap['id'] + self.msg('blockers.report.3'))
        if not config(rt)['enabled']:
            with rt.transaction(db):
                db.execute("UPDATE automation_gaps SET card_error='blockers_disabled',updated_at=? WHERE id=?", (now, gap['id']))
            return _summary(_gap(db, gap['id']), deduped=False, superseded=previous['id'] if previous else None,
                            next=f'Blocker cards are off (blockers.enabled=false, or the digital-worker config is invalid): the gap is recorded without a card. If {self.name} is needed now, use ask-{self.key}. '
                                 + self.next_reported.format(id=gap['id']))
        try:
            card = self.ask_owner(rt)(db, db_path, session, provider, task_path, self.card_question(gap, smoke, previous, shown), cards, recommend, now=now, trusted_owner=True, **{f'{self.key}_only': True}, kind='blocker_card', watch_extra=self._watch_extra(gap, 1), smoke=smoke, footer=self.card_footer(gap, smoke), live_card=live_card)
        except Exception as exc:
            with rt.transaction(db):
                db.execute('UPDATE automation_gaps SET card_error=?,card_attempts=card_attempts+1,updated_at=? WHERE id=?',
                           (rt.safe_error(exc), now, gap['id']))
            raise
        with rt.transaction(db):
            db.execute('UPDATE automation_gaps SET card_event=?,card_channel=?,card_ts=?,card_posted_at=?,card_error=NULL,'
                       'card_attempts=card_attempts+1,updated_at=? WHERE id=?',
                       (card['event_id'], card['channel'], card['thread_ts'], now, now, gap['id']))
        return _summary(_gap(db, gap['id']), deduped=False, superseded=previous['id'] if previous else None,
                        watch_id=card['watch_id'], next=self.next_reported.format(id=gap['id']))

    def _may_touch(self, rt, row):
        """The reporting session, a session working in the blocker's task workspace, or the owner (no agent identity)."""
        provider, session = rt.claude.actor()
        if not session:
            if os.environ.get(self.run_id_env) or rt._agent_session():
                raise ValueError('agent_identity_required')
            return self.key
        if (provider, session) == (row['provider'], row['session']) or \
                rt.registry.task_id_for_cwd(os.getcwd()) == row['task_id']:
            return f'{provider}:{session}'
        raise ValueError(f'only_the_reporting_session_a_session_of_the_task_or_{self.key}_may_do_this')

    def resolve_blocker(self, rt, db, gap_id, how, note=None, now=None):
        """After read-back verification (agent) or by the owner: record how the blocker was resolved."""
        now = time.time() if now is None else now
        row = _gap(db, gap_id)
        if how not in self.resolutions:
            raise ValueError('how_must_be_one_of_' + '|'.join(self.resolutions))
        who = self._may_touch(rt, row)
        if row['status'] == 'superseded':
            raise ValueError(f'blocker_superseded_by_{row["superseded_by"]}')
        if row['status'] == 'resolved':
            if row['resolution'] == how:
                return {**_summary(row), 'duplicate': True, 'resolution': row['resolution']}
            raise ValueError(f'blocker_already_resolved_as_{row["resolution"]}')
        if note is not None:
            note = _line(note, 'note', 300)
            rt.asks.check_secret_free(note)
            if row['type'] == 'mfa_code' and re.search(r'(?<![0-9])\d{5,8}(?![0-9])', note):
                raise ValueError('note_must_not_contain_a_verification_code')
        with rt.transaction(db):
            won = db.execute(f"UPDATE automation_gaps SET status='resolved',resolution=?,resolved_at=?,resolved_by=?,resolution_note=?,{self.key}_manual=?,updated_at=? WHERE id=? AND status IN ('open','in_progress')",
                             (how, now, who, note, int(how in self.owner_manual), now, row['id'])).rowcount
        if not won:
            raise ValueError('blocker_changed_concurrently_retry')
        row = _gap(db, gap_id)
        stage = self.close_cards(rt, db, row, self.msg('blockers.resolve_blocker.1', value=self.resolution_zh[how]) if who != self.key else
                            self.msg('blockers.resolve_blocker.2', value=self.resolution_zh[how], owner=self.name))
        return {**_summary(row), 'resolution': how, 'resolved_by': who, f'{self.key}_manual': bool(row[f'{self.key}_manual']),
                'cards': stage, 'next': 'Card watches stop by themselves once their events are resolved.'}

    def close_cards(self, rt, db, row, line):
        """Best effort: replace the buttons of this blocker's undecided cards with `line` (bot identity)."""
        out = {}
        for eid in (row['card_event'], row['stage2_event']):
            if not eid or db.execute('SELECT 1 FROM decisions WHERE event_id=?', (eid,)).fetchone():
                continue
            try:
                esc = db.execute('SELECT thread_ts FROM escalations WHERE event_id=?', (eid,)).fetchone()
                card = db.execute('SELECT channel FROM escalation_cards WHERE event_id=?', (eid,)).fetchone()
                message = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
                    'channel': card['channel'], 'ts': esc['thread_ts'], 'limit': 1})['messages'][0]
                if message.get('ts') != esc['thread_ts'] or message.get('user') != rt.BOT:
                    raise RuntimeError('card_readback_failed')
                block = {'type': 'context', 'block_id': rt.buttons.OUTCOME_BLOCK_ID,
                         'elements': [{'type': 'mrkdwn', 'text': f'{line} · {self._clock(time.time())} ({self.tz_name})'}]}
                rt.slack.api_call('chat.update', rt.api_identity(bot=True), json_body={
                    'channel': card['channel'], 'ts': esc['thread_ts'], 'text': message.get('text', ''),
                    'blocks': rt.buttons.decided_blocks(message.get('blocks'), block)})
                out[eid] = 'card_closed'
            except Exception as exc:  # noqa: BLE001 - the resolution stands; only the card keeps stale buttons
                out[eid] = 'card_update_failed:' + rt.safe_error(exc)
                with rt.transaction(db):
                    db.execute('UPDATE automation_gaps SET close_error=? WHERE id=?', (out[eid], row['id']))
        return out

    def event_context(self, rt, db, eid, w=None):
        """`context.blocker`: a click on a blocker card (stage 1 / stage 2) or a typed reply in its thread; else None."""
        try:
            d = db.execute('SELECT d.event_id, d.choice, d.actor, d.decided_at, d.note, c.options FROM decisions d '
                           'JOIN escalation_cards c ON c.event_id=d.event_id WHERE d.delivered_event=?', (eid,)).fetchone()
            row, stage, option = None, None, None
            if d is not None:
                row = db.execute('SELECT * FROM automation_gaps WHERE card_event=? OR stage2_event=?',
                                 (d['event_id'], d['event_id'])).fetchone()
                if row is None:
                    return None
                stage = 1 if row['card_event'] == d['event_id'] else 2
                option = rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))
            else:
                if w is None:
                    e = db.execute('SELECT watch_id FROM events WHERE id=?', (eid,)).fetchone()
                    w = rt.load(db, e['watch_id']) if e else {}
                if not w.get('blocker_id'):
                    return None
                row = db.execute('SELECT * FROM automation_gaps WHERE id=?', (w['blocker_id'],)).fetchone()
                if row is None:
                    return None
                stage = 'proposal' if _rejected_proposal(db, eid) else 'reply'
        except (sqlite3.Error, ValueError, KeyError):
            return None
        out = {'id': row['id'], 'task_id': row['task_id'], 'system': row['system'], 'operation': row['operation'],
               'type': row['type'], 'status': row['status'], 'stage': stage, 'secret_input': row['type'] == 'mfa_code',
               'authority': 'none', 'card_note': 'The blocker card only chooses how to get unblocked; it authorizes '
                                                'nothing beyond the task (access changes still need rule-check).'}
        if stage == 1:
            out.update(choice={'id': option['id'], 'label': option['label']}, note=d['note'],
                       suggested_resolution=None if option['id'] in self.stage2 else self.option_resolution.get(option['id']),
                       stage2_card=bool(row['stage2_event']))
            out['next'] = self.stage1_next.get(option['id'], 'Act on the choice, then resolve-blocker.').format(
                id=row['id'], done=self.stage2.get(option['id'], ('',))[0])
        elif stage == 2:
            how = self.option_resolution.get(row['choice'], f'{self.key}_manual') if option['id'] == 'done' else 'skipped'
            out.update(choice={'id': option['id'], 'label': option['label']}, note=d['note'], suggested_resolution=how,
                       first_choice={'id': row['choice'], 'label': _option_label(row, row['choice'])})
            out['next'] = self.stage2_next.get(option['id'], self.stage2_next['done']).format(id=row['id'], how=how)
        elif stage == 'proposal':
            out['next'] = f'{self.name} said your reading of his reply was wrong (context.proposal): ask again in the card thread.'
        else:
            out['next'] = (f"{self.name}'s typed reply in the blocker card thread: conversation, not a decision; read it with context, answer as the bot in that thread if needed.")
        if row['type'] == 'mfa_code':
            out['next'] += ' ' + SECRET_NEXT.format(id=row['id'], event=eid)
        return out

    def _reply(self, rt, db, row, event_id, now):
        """the owner's hand-typed reply (exact event, or the newest one) in this blocker's card threads, read live from Slack."""
        threads = []
        for eid in (row['card_event'], row['stage2_event']):
            if not eid:
                continue
            esc = db.execute('SELECT thread_ts, watch_id FROM escalations WHERE event_id=?', (eid,)).fetchone()
            card = db.execute('SELECT channel FROM escalation_cards WHERE event_id=?', (eid,)).fetchone()
            if esc and card and esc['thread_ts']:
                threads.append((card['channel'], esc['thread_ts'], esc['watch_id']))
        if not threads:
            raise ValueError('blocker_has_no_card_thread')
        token = rt.api_identity()
        if event_id:
            e = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            match = next((t for t in threads if e is not None and e['watch_id'] == t[2]), None)
            if match is None:
                raise ValueError('event_is_not_a_reply_in_this_blockers_card_thread')
            if e['author'] != self.owner_user(rt):
                raise ValueError(f'event_is_not_from_{self.key}')
            found = rt.slack.api_call('conversations.replies', token, params={
                'channel': match[0], 'ts': match[1], 'oldest': e['ts'], 'latest': e['ts'], 'inclusive': 'true', 'limit': 2})
            candidates = [(match, m) for m in found.get('messages', []) if m.get('ts') == e['ts']]
        else:
            candidates = []
            for t in threads:  # only replies young enough to still be a usable code
                found = rt.slack.api_call('conversations.replies', token, params={
                    'channel': t[0], 'ts': t[1], 'oldest': f'{now - CODE_MAX_AGE_SECONDS:.6f}', 'inclusive': 'true',
                    'limit': 100})
                candidates += [(t, m) for m in found.get('messages', []) if m.get('ts') != t[1]]
        human = [(t, m) for t, m in candidates if m.get('user') == self.owner_user(rt) and not m.get('bot_id') and not m.get('app_id')
                 and m.get('subtype') in (None, 'file_share') and m.get('thread_ts') == t[1]]
        if not human:
            raise ValueError(f'no_reply_from_{self.key}_in_the_card_thread_within_30_minutes')
        _, message = max(human, key=lambda tm: float(tm[1]['ts']))
        if now - float(message['ts']) > CODE_MAX_AGE_SECONDS:
            raise ValueError(f'newest_reply_is_older_than_30_minutes_ask_{self.key}_for_a_fresh_code')
        return message

    def blocker_code(self, rt, db, gap_id, command, event_id=None, stdin=False, env_name=None, timeout=120, now=None,
                     runner=None):
        """Run `command` with the owner's verification code on stdin, in env `env_name` and/or replacing `{code}` arguments.
        Prints nothing of the code: the command's output comes back with the code masked."""
        now = time.time() if now is None else now
        row = _gap(db, gap_id)
        if row['type'] != 'mfa_code':
            raise ValueError('blocker_code_is_only_for_mfa_code_blockers')
        if row['status'] not in UNRESOLVED:
            raise ValueError(f'blocker_is_{row["status"]}')
        provider, session = rt.claude.actor()
        if not session:
            raise ValueError('blocker_code_requires_an_agent_session')
        self._may_touch(rt, row)
        command = list(command or [])
        if command and command[0] == '--':
            command = command[1:]
        if not command:
            raise ValueError('command_required_after_--')
        placeholder = any('{code}' in a for a in command)
        if not (stdin or env_name or placeholder):
            raise ValueError('say_how_the_code_reaches_the_command_--stdin_--env_NAME_or_a_{code}_argument')
        if env_name is not None and not re.fullmatch(r'[A-Z][A-Z0-9_]{1,63}', env_name):
            raise ValueError('env_name_must_look_like_MFA_CODE')
        message = self._reply(rt, db, row, event_id, now)
        code = extract_code(message.get('text'))
        if code is None:
            raise ValueError(f'no_single_code_in_{self.key}s_reply_ask_him_to_reply_with_only_the_code')
        env = dict(os.environ)
        if env_name:
            env[env_name] = code
        try:
            result = (runner or subprocess.run)([a.replace('{code}', code) for a in command],
                                                input=(code + '\n') if stdin else None, env=env, capture_output=True,
                                                text=True, timeout=max(1, min(int(timeout), 900)))
            exit_code, out, err = result.returncode, result.stdout or '', result.stderr or ''
        except subprocess.TimeoutExpired:
            exit_code, out, err = 124, '', 'timeout'
        except OSError as exc:
            exit_code, out, err = 127, '', type(exc).__name__
        # The command may echo what it was given: the code never leaves this function unmasked.
        out, err = out.replace(code, MASK), err.replace(code, MASK)
        return {'ok': exit_code == 0, 'blocker_id': row['id'], 'exit_code': exit_code, 'reply_ts': message['ts'],
                'stdout': out[-CODE_OUTPUT_LIMIT:], 'stderr': err[-8000:], 'secret': 'used once; never printed or stored',
                'next': f'Verify by read-back, then resolve-blocker --id %s --how {self.key}_manual.' % row['id']}

    def settle(self, rt, db, now=None):
        """Supervisor step (every 10 s). Each part fails on its own; Slack posts are claimed in SQLite first, so the
        supervisor and a CLI (smoke) never post the same card or reminder twice."""
        now = time.time() if now is None else now
        cfg = config(rt)
        done = {}
        for name, step in (('choices', _record_choices), ('stage2', self._post_stage2_cards), ('reminders', self._remind),
                           ('watches', _stop_watches)):
            try:
                done[name] = step(rt, db, now, cfg)
            except Exception as exc:  # noqa: BLE001
                rt._meta_error(db, 'blockers_settle_error', exc)
                done[name] = 'error'
        return done

    def stage2_question(self, row, smoke=False):
        done = self.stage2[row['choice']][0]
        test = '🧪 TEST · ' if smoke else ''
        label = _option_label(row, row['choice'])
        label = label.split(self.msg('blockers.stage2_question.1'))[0]  # "我来网页操作，做完点“已完成”" -> "我来网页操作"
        return '\n'.join([_fit(test + self.msg('blockers.stage2_question.2', label=label, system=row['system'], done=done),
                                row['operation']),
                           self.msg('blockers.stage2_question.3')])

    def _post_stage2_cards(self, rt, db, now, cfg):
        if not cfg['enabled']:
            return 'disabled'
        out = []
        rows = db.execute("SELECT * FROM automation_gaps WHERE status='in_progress' AND choice IN ('manual','credential') AND "
                          'stage2_event IS NULL AND stage2_attempts<?', (STAGE2_ATTEMPTS,)).fetchall()
        for row in rows:
            with rt.transaction(db):
                won = db.execute("UPDATE automation_gaps SET stage2_state='posting',stage2_claimed_at=?,"
                                 'stage2_attempts=stage2_attempts+1,updated_at=? WHERE id=? AND stage2_event IS NULL AND '
                                 "(stage2_state IS NULL OR (stage2_state='failed' AND coalesce(stage2_claimed_at,0)<?) OR "
                                 "(stage2_state='posting' AND stage2_claimed_at<?))",
                                 (now, now, row['id'], now - 60 * row['stage2_attempts'], now - POSTING_STALE_SECONDS)).rowcount
            if not won:
                continue
            # Stage 2 inherits stage 1's dry state: a dry smoke blocker's [已完成] card is dry too (no real Slack post).
            stage1 = db.execute('SELECT dry FROM escalation_cards WHERE event_id=?', (row['card_event'],)).fetchone()
            stage2_live = not (stage1 and stage1['dry'])
            try:
                card = self.ask_owner(rt)(db, _db_path(db), row['session'], row['provider'], row['task_path'], self.stage2_question(row, bool(row['smoke'])), self.stage2[row['choice']][1], now=now, trusted_owner=True, **{f'{self.key}_only': True}, kind='blocker_done_card', watch_extra=self._watch_extra(row, 2), smoke=bool(row['smoke']), registry_note=False, footer=self.card_footer(row, bool(row['smoke'])), parent_event=row['card_event'], live_card=stage2_live)
            except Exception as exc:  # noqa: BLE001 - retried later (STAGE2_ATTEMPTS, backoff)
                with rt.transaction(db):
                    db.execute("UPDATE automation_gaps SET stage2_state='failed',stage2_error=?,updated_at=? WHERE id=?",
                               (rt.safe_error(exc), now, row['id']))
                out.append((row['id'], 'failed'))
                continue
            with rt.transaction(db):
                db.execute("UPDATE automation_gaps SET stage2_event=?,stage2_channel=?,stage2_ts=?,stage2_posted_at=?,"
                           "stage2_state='posted',stage2_error=NULL,updated_at=? WHERE id=?",
                           (card['event_id'], card['channel'], card['thread_ts'], now, now, row['id']))
            out.append((row['id'], 'posted'))
        return out

    def _remind(self, rt, db, now, cfg):
        """One reminder in the card thread when a card waits longer than reminder_hours (stage 1 and stage 2 each)."""
        if not cfg['enabled']:
            return 'disabled'
        wait = cfg['reminder_hours'] * 3600
        out = []
        for row in db.execute("SELECT * FROM automation_gaps WHERE status='open' AND card_ts IS NOT NULL AND reminded_at IS "
                              'NULL AND card_posted_at<=?', (now - wait,)).fetchall():
            if db.execute('SELECT 1 FROM decisions WHERE event_id=?', (row['card_event'],)).fetchone():
                continue  # decided; the choice is recorded at the next step
            if rt.card_replies.continued(rt, db, row['card_event']):
                continue  # P46: a newer card in its thread took it over (its buttons are gone)
            with rt.transaction(db):  # mark before posting: never two reminders
                if db.execute('UPDATE automation_gaps SET reminded_at=? WHERE id=? AND reminded_at IS NULL',
                              (now, row['id'])).rowcount != 1:
                    continue
            hours = int((now - row['card_posted_at']) // 3600)
            text = (self.msg('blockers._remind.1', owner_user=self.owner_user(rt), task_id=row['task_id'], system=row['system'], escape=rt.buttons.escape(row['operation']), hours=hours))
            out.append((row['id'], 'stage1', _post_reminder(rt, db, row, row['card_channel'], row['card_ts'], text,
                                                            'reminder_ts', now)))
        for row in db.execute("SELECT * FROM automation_gaps WHERE status='in_progress' AND stage2_ts IS NOT NULL AND "
                              'stage2_choice IS NULL AND stage2_reminded_at IS NULL AND stage2_posted_at<=?',
                              (now - wait,)).fetchall():
            if db.execute('SELECT 1 FROM decisions WHERE event_id=?', (row['stage2_event'],)).fetchone():
                continue
            with rt.transaction(db):
                if db.execute('UPDATE automation_gaps SET stage2_reminded_at=? WHERE id=? AND stage2_reminded_at IS NULL',
                              (now, row['id'])).rowcount != 1:
                    continue
            done = self.stage2.get(row['choice'], (self.msg('blockers._remind.2'),))[0]
            text = (self.msg('blockers._remind.3', owner_user=self.owner_user(rt), task_id=row['task_id'], system=row['system'], escape=rt.buttons.escape(row['operation']), done=done))
            out.append((row['id'], 'stage2', _post_reminder(rt, db, row, row['stage2_channel'], row['stage2_ts'], text,
                                                            'stage2_reminder_ts', now)))
        return out

    def _public(self, row, now):
        out = {k: row[k] for k in self.public}
        out.update(smoke=bool(row['smoke']), **{f'{self.key}_manual': bool(row[f'{self.key}_manual'])}, created_at=row['created_at'], last_reported_at=row['last_reported_at'], resolved_at=row['resolved_at'], card=bool(row['card_event']), stage2_card=bool(row['stage2_event']), reminded=bool(row['reminded_at']), **{f'waiting_on_{self.key}': self.waiting_on_owner(row)}, age_hours=round((now - row['created_at']) / 3600, 1) if row['status'] in UNRESOLVED else None)
        return out

    def gaps_list(self, db, status=None, task=None, system=None, limit=100, now=None):
        now = time.time() if now is None else now
        where, args = [], []
        if status and status != 'all':
            statuses = ('open', 'in_progress') if status == 'unresolved' else (status,)
            where.append('status IN (%s)' % ','.join('?' * len(statuses)))
            args += statuses
        if task:
            where.append('task_id=?')
            args.append(task)
        if system:
            where.append('system=?')
            args.append(normalize_system(system))
        sql = 'SELECT * FROM automation_gaps' + (' WHERE ' + ' AND '.join(where) if where else '') + \
              ' ORDER BY created_at DESC LIMIT ?'
        rows = db.execute(sql, (*args, max(1, min(int(limit), 500)))).fetchall()
        return {'ok': True, 'gaps': [self._public(r, now) for r in rows],
                'note': 'tried / unblock text only with gaps show --id; verification codes are never stored.'}

    def gap_detail(self, db, gap_id, now=None):
        now = time.time() if now is None else now
        row = _gap(db, gap_id)
        out = self._public(row, now)
        out.update(tried=row['tried'], unblock=row['unblock'], resolution_note=row['resolution_note'],
                   resolved_by=row['resolved_by'], options=json.loads(row['options']), chosen_by=row['chosen_by'],
                   chosen_at=row['chosen_at'], stage2_by=row['stage2_by'], stage2_at=row['stage2_at'],
                   card_error=row['card_error'], stage2_error=row['stage2_error'], close_error=row['close_error'],
                   cards=[{'stage': n, 'channel': ch, 'ts': ts, 'posted_at': at}
                          for n, ch, ts, at in ((1, row['card_channel'], row['card_ts'], row['card_posted_at']),
                                                (2, row['stage2_channel'], row['stage2_ts'], row['stage2_posted_at'])) if ts],
                   reminders=[x for x in (row['reminded_at'], row['stage2_reminded_at']) if x])
        return {'ok': True, 'gap': out}

    def review_aggregate(self, db, start, now):
        """`rules-review-data` → `gaps`: counts by system / type / resolution, top paths, the owner's manual takeovers, open
        blockers and existing skill proposals. Labels and ids only (never tried / unblock text, never codes)."""
        rows = db.execute('SELECT * FROM automation_gaps WHERE smoke=0 AND (created_at>=? OR resolved_at>=? OR status IN '
                          "('open','in_progress'))", (start, start)).fetchall()
        window = [r for r in rows if r['created_at'] >= start]
        by_system, by_type, by_resolution, paths = {}, {}, {}, {}
        for r in window:
            _bucket(by_system, r['system'])
            _bucket(by_type, r['type'])
            _bucket(by_resolution, r['resolution'] or r['status'])
            p = paths.setdefault((r['system'], r['op_key']), {'system': r['system'], 'operation': r['operation'],
                                                              'reports': 0, 'occurrences': 0, f'{self.key}_manual': 0, 'open': 0,
                                                              'types': {}, 'tasks': set(), 'gap_ids': []})
            p['reports'] += 1
            p['occurrences'] += r['occurrences']
            p[f'{self.key}_manual'] += r[f'{self.key}_manual']
            p['open'] += int(r['status'] in UNRESOLVED)
            _bucket(p['types'], r['type'])
            p['tasks'].add(r['task_id'])
            if len(p['gap_ids']) < 10:
                p['gap_ids'].append(r['id'])
        top = sorted(paths.values(), key=lambda p: (-p['reports'], -p[f'{self.key}_manual'], -p['occurrences'], p['system']))[:10]
        for p in top:
            p['tasks'] = len(p['tasks'])
        manual = [r for r in window if r[f'{self.key}_manual']]
        unresolved = [r for r in rows if r['status'] in UNRESOLVED]
        proposals = {'pending': [], 'suppressed': [], 'launched': []}
        for p in db.execute('SELECT * FROM gap_proposals WHERE smoke=0 ORDER BY created_at DESC LIMIT 50'):
            item = {'id': p['id'], 'system': p['system'], 'operation': p['operation'], 'status': p['status']}
            if p['status'] in ('posting', 'pending', 'launching'):
                proposals['pending'].append(item)
            elif p['status'] == 'rejected' and (p['suppress_until'] or 0) > now:
                proposals['suppressed'].append({**item, 'until': self.iso(p['suppress_until'])})
            elif p['status'] == 'launched':
                proposals['launched'].append({**item, 'task_id': p['task_id'], 'at': self.iso(p['launched_at'])})
        return {'reported': len(window), 'occurrences': sum(r['occurrences'] for r in window), 'by_system': by_system,
                'by_type': by_type, 'by_resolution': by_resolution, 'top_paths': top,
                f'{self.key}_manual': {'total': len(manual), 'by_system': {s: sum(1 for r in manual if r['system'] == s)
                                                                    for s in sorted({r['system'] for r in manual})}},
                'open': {'count': len(unresolved), f'waiting_on_{self.key}': sum(1 for r in unresolved if self.waiting_on_owner(r)),
                         'oldest_age_hours': round(max((now - r['created_at'] for r in unresolved), default=0) / 3600, 1),
                         'ids': [r['id'] for r in unresolved][:20]},
                'skill_proposals': proposals}

    def _week_start(self, ts):
        local = dt.datetime.fromtimestamp(ts, self.tz)
        return (local - dt.timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def dashboard_overview(self, db, now, weeks=8):
        """/api/gaps: system / operation / type / status and counts only (no tried / unblock / notes)."""
        rows = db.execute('SELECT * FROM automation_gaps ORDER BY created_at DESC').fetchall()
        first = self._week_start(now) - (weeks - 1) * 7 * 86400
        week = {first + n * 7 * 86400: {'reported': 0, 'resolved': 0, f'{self.key}_manual': 0, 'by_type': {}} for n in range(weeks)}
        counts = {'open': 0, 'in_progress': 0, 'resolved': 0, 'superseded': 0, 'total': 0, f'{self.key}_manual': 0,
                  f'waiting_on_{self.key}': 0}
        by_system, by_type, by_resolution, paths = {}, {}, {}, {}
        for r in rows:
            if r['smoke']:
                continue
            counts['total'] += 1
            _bucket(counts, r['status'])
            counts[f'{self.key}_manual'] += r[f'{self.key}_manual']
            counts[f'waiting_on_{self.key}'] += int(self.waiting_on_owner(r))
            s = by_system.setdefault(r['system'], {'system': r['system'], 'total': 0, 'open': 0, f'{self.key}_manual': 0})
            s['total'] += 1
            s['open'] += int(r['status'] in UNRESOLVED)
            s[f'{self.key}_manual'] += r[f'{self.key}_manual']
            _bucket(by_type, r['type'])
            if r['resolution']:
                _bucket(by_resolution, r['resolution'])
            p = paths.setdefault((r['system'], r['op_key']), {'system': r['system'], 'operation': r['operation'], 'reports': 0,
                                                              'occurrences': 0, f'{self.key}_manual': 0, 'open': 0, 'last_at': 0})
            p['reports'] += 1
            p['occurrences'] += r['occurrences']
            p[f'{self.key}_manual'] += r[f'{self.key}_manual']
            p['open'] += int(r['status'] in UNRESOLVED)
            p['last_at'] = max(p['last_at'], r['last_reported_at'])
            w = week.get(self._week_start(r['created_at']))
            if w is not None:
                w['reported'] += 1
                _bucket(w['by_type'], r['type'])
            w = week.get(self._week_start(r['resolved_at'])) if r['resolution'] else None
            if w is not None:
                w['resolved'] += 1
                w[f'{self.key}_manual'] += r[f'{self.key}_manual']
        return {'counts': counts, 'by_system': sorted(by_system.values(), key=lambda s: (-s['total'], s['system'])),
                'by_type': by_type, 'by_resolution': by_resolution,
                'weeks': [{'start': k, **v} for k, v in sorted(week.items())],
                'top_paths': sorted(paths.values(), key=lambda p: (-p['reports'], -p[f'{self.key}_manual'], p['system']))[:15],
                'open': [self._public(r, now) for r in rows if r['status'] in UNRESOLVED][:100],
                'recent': [self._public(r, now) for r in rows][:60],
                'proposals': [{k: p[k] for k in ('id', 'system', 'operation', 'status', 'created_at', 'decided_at',
                                                 'task_id', 'dispatch_id', 'launched_at', 'suppress_until')} |
                              {'smoke': bool(p['smoke']), 'error': (p['error'] or '')[:120] or None}
                              for p in db.execute('SELECT * FROM gap_proposals ORDER BY created_at DESC LIMIT 30')]}

    def _proposal_items(self, rt, db, raw, cfg, now):
        try:
            doc = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except ValueError:
            raise ValueError('proposals_file_is_not_valid_json') from None
        if not isinstance(doc, dict) or doc.get('schema') != self.proposals_schema_id or not isinstance(doc.get('proposals'), list):
            raise ValueError(f'proposals_file_needs_schema_{self.proposals_schema_id}_and_a_proposals_list')
        if not 1 <= len(doc['proposals']) <= cfg['max_skill_proposals_per_run']:
            raise ValueError(f'1_to_{cfg["max_skill_proposals_per_run"]}_proposals_per_run')
        allowed = {'system', 'operation', 'title_zh', 'rationale_zh', 'brief_zh', 'slug', 'provider', 'category',
                   'important', 'evidence'}
        accepted, skipped = [], []
        for n, p in enumerate(doc['proposals']):
            if not isinstance(p, dict) or set(p) - allowed:
                raise ValueError(f'proposal_{n}_unknown_keys_' + '_'.join(sorted(set(p) - allowed if isinstance(p, dict) else [])))
            system = normalize_system(p.get('system'))
            operation = _line(p.get('operation'), f'proposal_{n}_operation', 160)
            title = _line(p.get('title_zh'), f'proposal_{n}_title_zh', 80)
            rationale = _line(p.get('rationale_zh'), f'proposal_{n}_rationale_zh', 300)
            brief = str(p.get('brief_zh') or '').strip()
            if not brief or len(brief) > 4000:
                raise ValueError(f'proposal_{n}_brief_zh_must_be_1_to_4000_characters')
            if not (_cjk(title) and _cjk(rationale) and _cjk(brief)):
                raise ValueError(f'proposal_{n}_title_rationale_and_brief_must_be_chinese')
            rt.asks.check_secret_free(operation, title, rationale, brief)
            for field, value in (('title_zh', title), ('rationale_zh', rationale), ('operation', operation)):
                rt.buttons.check_copy(value, f'proposal_{n}_{field}', first_line_max=None)  # on the card
            slug = str(p.get('slug') or '')
            if not SLUG_RE.fullmatch(slug):
                raise ValueError(f'proposal_{n}_slug_must_be_3_to_51_lowercase_ascii_letters_digits_or_dashes')
            provider = p.get('provider') or 'auto'
            if provider not in ('auto', 'codex', 'claude'):
                raise ValueError(f'proposal_{n}_provider_must_be_auto_codex_or_claude')
            category = p.get('category') or 'dev'
            if not rt.worker_config.CATEGORY_RE.fullmatch(str(category)):
                raise ValueError(f'proposal_{n}_invalid_category')
            evidence = p.get('evidence') or {}
            ids = evidence.get('gap_ids') if isinstance(evidence, dict) else None
            if not isinstance(ids, list) or not ids or len(ids) > 50 or not all(GAP_ID_RE.fullmatch(str(i)) for i in ids):
                raise ValueError(f'proposal_{n}_evidence_gap_ids_must_list_1_to_50_blocker_ids')
            key = op_key(operation)
            known = db.execute('SELECT count(*) FROM automation_gaps WHERE smoke=0 AND system=? AND op_key=? AND id IN (%s)'
                               % ','.join('?' * len(ids)), (system, key, *ids)).fetchone()[0]
            if known == 0:
                raise ValueError(f'proposal_{n}_evidence_must_name_recorded_gaps_of_this_system_and_operation')
            fp = fingerprint(system, operation)
            suppressed = db.execute("SELECT max(suppress_until) FROM gap_proposals WHERE fingerprint=? AND status='rejected' "
                                    'AND suppress_until>?', (fp, now)).fetchone()[0]
            if suppressed:
                skipped.append({'index': n, 'fingerprint': fp, 'reason': 'rejected_recently', 'until': self.iso(suppressed)})
                continue
            if db.execute("SELECT 1 FROM gap_proposals WHERE fingerprint=? AND status IN ('posting','pending','launching')",
                          (fp,)).fetchone():
                skipped.append({'index': n, 'fingerprint': fp, 'reason': 'already_pending'})
                continue
            if db.execute("SELECT 1 FROM gap_proposals WHERE fingerprint=? AND status='launched' AND launched_at>?",
                          (fp, now - cfg['skill_proposal_suppress_days'] * 86400)).fetchone():
                skipped.append({'index': n, 'fingerprint': fp, 'reason': 'already_launched'})
                continue
            accepted.append({'system': system, 'operation': operation, 'op_key': key, 'title_zh': title,
                             'rationale_zh': rationale, 'brief_zh': brief, 'slug': slug, 'provider': provider,
                             'category': category, 'important': bool(p.get('important')),
                             'evidence': {'gap_ids': list(dict.fromkeys(ids)), 'matching': known}, 'fingerprint': fp})
        return accepted, skipped

    def proposal_card_text(self, item, pid, stats):
        """Skill proposal card (P3.1 copy standard): one plain sentence, why (with the counts), what the buttons do. The
        task brief goes to the card thread right after the card."""
        rationale = item['rationale_zh'] if len(item['rationale_zh']) <= 120 else item['rationale_zh'][:119] + '…'
        return '\n'.join([
            _fit(self.msg('blockers.proposal_card_text.1', system=item['system'], value=item['title_zh'][:30]),
                 item['operation']),
            self.msg('blockers.proposal_card_text.2', rationale=rationale, reports=stats['reports'], manual=stats['manual']),
            self.msg('blockers.proposal_card_text.3')])

    def propose_skills(self, rt, db, db_path, raw, now=None):
        """gap-propose (agent session, weekly review): validate, then one never-auto card per proposal."""
        now = time.time() if now is None else now
        cfg = config(rt)
        if not cfg['skill_proposals_enabled']:
            raise ValueError('skill_proposals_disabled_blockers.skill_proposals_enabled_false')
        provider, session = rt.claude.actor()
        if not session:
            raise ValueError('gap_propose_requires_an_agent_session')
        accepted, skipped = self._proposal_items(rt, db, raw, cfg, now)
        cwd = rt.registry.task_id_for_cwd(os.getcwd()) and os.getcwd()
        if not cwd:
            inbox = rt.registry.task_dir(rt.worker_config.load()[0]['intake']['inbox_task'])
            cwd = str(inbox) if inbox.is_dir() else os.getcwd()
        posted = []
        for item in accepted:
            pid = 'gp' + uuid.uuid4().hex[:14]
            stats = db.execute(f"SELECT count(*), coalesce(sum({self.key}_manual),0), coalesce(sum(status IN ('open','in_progress')),0) FROM automation_gaps WHERE smoke=0 AND system=? AND op_key=?",
                               (item['system'], item['op_key'])).fetchone()
            with rt.transaction(db):
                db.execute('INSERT INTO gap_proposals (id,fingerprint,system,operation,op_key,title_zh,proposal,proposer,'
                           'status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (pid, item['fingerprint'], item['system'], item['operation'], item['op_key'], item['title_zh'],
                            json.dumps(item, ensure_ascii=False, sort_keys=True), f'{provider}:{session}', 'posting', now))
            text = self.proposal_card_text(item, pid, {'reports': stats[0], 'manual': stats[1], 'open': stats[2]})
            try:
                card = self.ask_owner(rt)(db, db_path, session, provider, cwd, text, self.skill_options, never_auto=True, now=now,
                                   kind='gap_proposal_card', footer=self.msg('blockers.propose_skills.1', dashboard_url=DASHBOARD_URL))
            except Exception as exc:
                with rt.transaction(db):
                    db.execute("UPDATE gap_proposals SET status='failed',error=? WHERE id=?", (str(exc)[:200], pid))
                raise
            # The task brief is detail: one bot reply in the card thread (best effort), never on the card itself.
            rt.rules._card_reply(rt, db, card['event_id'], self.msg('blockers.propose_skills.2', title_zh=item['title_zh'], brief_zh=item['brief_zh']), pid,
                                 kind='gap_proposal_brief')
            with rt.transaction(db):
                db.execute("UPDATE gap_proposals SET status='pending',card_event=? WHERE id=?", (card['event_id'], pid))
            posted.append({'proposal_id': pid, 'system': item['system'], 'operation': item['operation'],
                           'fingerprint': item['fingerprint'], 'card_event': card['event_id'], 'card_ts': card['thread_ts']})
        return {'ok': True, 'posted': posted, 'skipped': skipped,
                'next': (self.msg('blockers.propose_skills.next'))}

    def skill_task_prompt(self, rt, db_path, p, item, task_id):
        cli = f'python3 {Path(rt.__file__).resolve()} --db {db_path}'
        ids = ', '.join(item['evidence']['gap_ids'])
        return '\n'.join([
            self.msg('blockers.skill_task_prompt.1', task_id=task_id, owner=self.name, id=p['id']),
            self.msg('blockers.skill_task_prompt.2', system=item['system'], operation=item['operation'], owner=self.name),
            self.msg('blockers.skill_task_prompt.3', ids=ids, cli=cli),
            self.msg('blockers.skill_task_prompt.4', owner_key=self.key, owner=self.name),
            self.msg('blockers.skill_task_prompt.5', cli=cli),
            '', self.msg('blockers.skill_task_prompt.6'), item['brief_zh']])

    def launch_skill_task(self, rt, db, db_path, p, now, runner=None):
        """Bootstrap `local-task-<today>-<slug>` (no Linear, no iTerm) and queue its first headless session."""
        item = json.loads(p['proposal'])
        config_all = rt.worker_config.load()[0]
        provider = item['provider'] if item['provider'] != 'auto' else \
            rt.worker_config.provider_order(config_all, item['category'])[0]
        root = rt.registry.root()
        name = f'local-task-{time.strftime("%Y%m%d", time.localtime(now))}-{item["slug"]}'  # the bootstrap's local date
        if (root / name).exists():
            raise ValueError('workspace_exists_' + name)
        context = (self.msg('blockers.launch_skill_task.1', owner=self.name, id=p['id'], card_event=p['card_event'], join=', '.join(item['evidence']['gap_ids'])))
        report = rt.intake.run_bootstrap(item['title_zh'], item['brief_zh'], context, linear=False, agent=provider,
                                         root=root, slug=item['slug'], runner=runner)
        path = Path(report['workspace'])
        task_id = rt.registry.task_id_for_cwd(str(path))
        if not task_id or report.get('reused_workspace'):
            raise ValueError('bootstrap_did_not_create_a_new_workspace')
        with rt.transaction(db):
            rt.registry.ensure_task(db, task_id, now)
        disp = rt.make_dispatcher(db, db_path)
        # P39: the owner approved this new task, so its launch settings become its preference (an `auto` provider is no pin).
        dispatch_id = disp.launch(disp.task_for_key(task_id), item['provider'], self.skill_task_prompt(rt, db_path, p, item, task_id),
                                  important=item['important'], category=item['category'], reason='gap_skill:' + p['id'],
                                  now=now, persist=True)
        return task_id, dispatch_id

    def settle_skill_proposals(self, rt, db, now=None, runner=None):
        """Supervisor step: [建任务做这个 skill] -> code bootstraps + launches; [先不做] -> suppressed; a condition -> the inbox
        proposes again. Kill switch: decided cards wait while blockers.skill_proposals_enabled is false."""
        now = time.time() if now is None else now
        cfg = config(rt)
        done = []
        for p in db.execute("SELECT * FROM gap_proposals WHERE status='pending' AND card_event IS NOT NULL").fetchall():
            found = _decision(rt, db, p['card_event'])
            if found is None:
                continue
            d, option = found
            status, error, task_id, dispatch_id = None, None, None, None
            if option['kind'] != 'approve':
                status = 'rejected'
            elif d['note']:
                status = 'needs_revision'
            elif not cfg['skill_proposals_enabled']:
                continue
            else:
                with rt.transaction(db):
                    if db.execute("UPDATE gap_proposals SET status='launching',decided_by=?,decided_at=? WHERE id=? AND "
                                  "status='pending'", (d['actor'], d['decided_at'], p['id'])).rowcount != 1:
                        continue
                try:
                    task_id, dispatch_id = self.launch_skill_task(rt, db, _db_path(db), p, now, runner)
                    status = 'launched'
                except Exception as exc:  # noqa: BLE001 - recorded; the owner can launch by hand
                    status, error = 'launch_failed', (str(exc) if isinstance(exc, ValueError) else rt.safe_error(exc))[:300]
            with rt.transaction(db):
                db.execute('UPDATE gap_proposals SET status=?,decided_by=?,decided_at=?,suppress_until=?,task_id=?,'
                           'dispatch_id=?,launched_at=?,error=? WHERE id=?',
                           (status, d['actor'], d['decided_at'],
                            now + cfg['skill_proposal_suppress_days'] * 86400 if status == 'rejected' else None,
                            task_id, dispatch_id, now if status == 'launched' else None, error, p['id']))
            reply = {'launched': self.msg('blockers.settle_skill_proposals.1', task_id=task_id, dispatch_id=dispatch_id),
                     'rejected': self.msg('blockers.settle_skill_proposals.2', skill_proposal_suppress_days=cfg['skill_proposal_suppress_days']),
                     'needs_revision': self.msg('blockers.settle_skill_proposals.needs_revision'),
                     'launch_failed': self.msg('blockers.settle_skill_proposals.3') + (error or '') + self.msg('blockers.settle_skill_proposals.4')}[status]
            rt.rules._card_reply(rt, db, p['card_event'], reply, p['id'], kind='gap_proposal_result')
            done.append({'proposal_id': p['id'], 'status': status, 'task_id': task_id, 'error': error})
        return done

    def smoke(self, rt, db, db_path, provider='claude', dashboard=DASHBOARD_URL, now=None, wait=30.0, poll=1.0, sleep=time.sleep,
              live_card=False):
        """Post-install TEST: a 🧪 TEST blocker on a TEST task, dedupe, synthetic click -> stage-2 card -> synthetic
        typed reply and [已完成] -> resolve -> gap row -> /api/gaps. A synthetic Claude session owns the TEST cards (its
        events stay in the SQLite outbox: no model run). Dry by default (no real Slack posts, no cleanup); `live_card`
        posts the two TEST cards to the PRIVATE decision channel for receiver E2E and leaves them decided, for audit."""
        now = time.time() if now is None else now
        if not dashboard.startswith(('http://127.0.0.1:', 'http://localhost:')):
            raise ValueError('dashboard_must_be_the_local_loopback_dashboard')
        if not config(rt)['enabled']:
            raise ValueError('blocker_cards_disabled_blockers.enabled_false')
        session = str(uuid.uuid4())
        path = smoke_workspace(rt, now)
        task_id = rt.registry.task_id_for_cwd(str(path))
        checks = {}
        report_args = dict(provider=provider, session=session, system=SMOKE_SYSTEM, operation=self.msg('blockers.smoke.1'),
                           btype='ui_only', tried=self.msg('blockers.smoke.2'), unblock=self.msg('blockers.smoke.3'),
                           task=task_id, smoke=True, live_card=live_card)
        first = self.report(rt, db, db_path, now=now, **report_args)
        checks['card_posted'] = bool(first['card'])
        second = self.report(rt, db, db_path, now=now + 1, **report_args)
        checks['dedupe'] = second['blocker_id'] == first['blocker_id'] and second.get('deduped') is True and \
            db.execute('SELECT count(*) FROM escalation_cards c JOIN escalations x ON x.event_id=c.event_id JOIN watches w ON '
                       "w.id=x.watch_id WHERE json_extract(w.data,'$.blocker_id')=?", (first['blocker_id'],)).fetchone()[0] == 1
        gid = first['blocker_id']
        stage1_event = synthetic_decision(rt, db, first['card']['event_id'], 'manual', now + 2)
        ctx1 = self.event_context(rt, db, stage1_event)
        checks['stage1_event'] = bool(ctx1 and ctx1['stage'] == 1 and ctx1['choice']['id'] == 'manual')
        watch1 = db.execute('SELECT watch_id FROM events WHERE id=?', (stage1_event,)).fetchone()[0]
        reply_event = synthetic_reply(rt, db, watch1, now + 3)
        ctx_reply = self.event_context(rt, db, reply_event)
        checks['typed_reply_event'] = bool(ctx_reply and ctx_reply['stage'] == 'reply' and ctx_reply['id'] == gid)
        end = time.time() + wait
        row = _gap(db, gid)
        while row['stage2_event'] is None and time.time() < end:
            self.settle(rt, db)
            row = _gap(db, gid)
            if row['stage2_event'] is None:
                sleep(poll)
        checks['stage2_card'] = row['stage2_event'] is not None
        stage2_event = synthetic_decision(rt, db, row['stage2_event'], 'done', now + 4) if row['stage2_event'] else None
        ctx2 = self.event_context(rt, db, stage2_event) if stage2_event else None
        checks['stage2_event'] = bool(ctx2 and ctx2['suggested_resolution'] == f'{self.key}_manual')
        with rt.transaction(db):  # the synthetic session never runs: settle its TEST events
            db.execute("UPDATE events SET status='handled',disposition='smoke_test' WHERE id IN (?,?,?)",
                       (stage1_event, reply_event, stage2_event))
        with _identity(provider, session):
            resolved = self.resolve_blocker(rt, db, gid, f'{self.key}_manual', note='TEST smoke-blocker', now=now + 5)
        checks['resolved'] = resolved['resolution'] == f'{self.key}_manual' and resolved[f'{self.key}_manual']
        self.settle(rt, db)
        row = _gap(db, gid)
        checks['gap_row'] = (row['status'], row['choice'], row['stage2_choice'], row['smoke']) == \
            ('resolved', 'manual', 'done', 1)
        cleanup = []
        for eid in (row['card_event'], row['stage2_event']):
            if eid:
                try:
                    cleanup.append(rt.smoke_cleanup(db, eid)['event_id'])
                except ValueError:
                    pass
        try:
            data = fetch_dashboard(dashboard.rstrip('/') + '/api/gaps')
            listed = next((g for g in data.get('recent', []) if g.get('id') == gid), None)
            checks['dashboard'] = bool(listed and listed.get('smoke') and listed.get('status') == 'resolved')
            dashboard_state = 'ok' if checks['dashboard'] else 'gap_not_listed'
        except (OSError, ValueError, urllib.error.URLError) as exc:
            checks['dashboard'] = False
            dashboard_state = 'unreachable:' + type(exc).__name__
        return {'ok': all(checks.values()), 'test': True, 'task_id': task_id, 'workspace': str(path), 'blocker_id': gid,
                'session': session, 'provider': provider, 'checks': checks, 'dashboard': dashboard_state,
                'cards': [first['card']['event_id'] if first['card'] else None, row['stage2_event']],
                'cleaned_up': cleanup}
