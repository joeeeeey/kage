"""Stuck chat-provider claims. Deterministic, no model.

The Claude delivery gives a session ONE outstanding claimed batch: while any of its events is `claimed`, claim()
returns nothing. A live session that claimed a batch, acted and never resolved it therefore blocks every later delivery
to it (its waiter spins; a verified approval sits queued behind it). `scan` (supervisor, with the delivery sweeps) finds
a session whose oldest open claim is older than `dispatch.stuck_claim_minutes` while newer events queue behind it and
posts ONE code-owned bot card to the owner per stuck batch. Nothing is released automatically — the consumer may already
have acted on the batch. `settle` applies the owner's click: requeue puts the exact claimed rows back in the queue (the
session re-claims them), settle marks them handled, later records only. Each re-verifies the exact rows in one
transaction; the claims' release reason and the `stuck_claims` row record who / when / which events. A card whose batch
the session settled by itself is closed. A dead session's claims belong to the dispatcher's stranded sweep (skipped here).

`StuckClaims` holds the owner-facing words (the owner's language catalog, `stuck.*`), the owner's chat user (only the
owner's click settles a card) and the workspace-name prefix a card leaves out of a task's name.
"""
from __future__ import annotations
import json
import time
import uuid

from kage import locale

DDL = '''
  CREATE TABLE IF NOT EXISTS stuck_claims (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT, event_ids TEXT NOT NULL, oldest_claimed_at REAL NOT NULL,
    queued_behind INTEGER NOT NULL, state TEXT NOT NULL, card_event TEXT, card_channel TEXT, card_ts TEXT, choice TEXT,
    decided_by TEXT, decided_at REAL, result TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS stuck_claims_session ON stuck_claims(session_id, state);
'''
OPTION_STYLES = (('requeue', 'approve'), ('settle', 'other'), ('later', 'reject'))
REPLY_KEYS = ('requeue', 'settle', 'later', 'none')


def initialize(db):
    db.executescript(DDL)
    db.commit()


def minutes(rt):
    """dispatch.stuck_claim_minutes (0 = off); a malformed config never cards by accident."""
    try:
        return rt.worker_config.load()[0]['dispatch']['stuck_claim_minutes']
    except ValueError:
        return 0


def task_of_session(rt, db, session):
    """The task a Claude session works for: its watches' workspace, else its dispatched runs, else a current session."""
    for row in db.execute("SELECT json_extract(data,'$.cwd'), json_extract(data,'$.task_id') FROM watches WHERE "
                          "coalesce(json_extract(data,'$.provider'),'codex')='claude' AND json_extract(data,'$.session')=?",
                          (session,)):
        tid = row[1] or rt.registry.task_id_for_cwd(row[0])
        if tid:
            return tid
    row = db.execute("SELECT task FROM consumer_runs WHERE provider='claude' AND session=? AND task NOT LIKE 'session:%' "
                     'ORDER BY started_at DESC LIMIT 1', (session,)).fetchone() or db.execute(
        "SELECT task_id FROM tasks WHERE current_provider='claude' AND current_session=? LIMIT 1", (session,)).fetchone()
    return row[0] if row else None


def _close_card(rt, db, row, line):
    """Best effort: replace the buttons of an undecided card with `line` (bot identity)."""
    eid = row['card_event']
    if not eid or db.execute('SELECT 1 FROM decisions WHERE event_id=?', (eid,)).fetchone():
        return
    try:
        esc = db.execute('SELECT thread_ts FROM escalations WHERE event_id=?', (eid,)).fetchone()
        message = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
            'channel': row['card_channel'], 'ts': esc['thread_ts'], 'limit': 1})['messages'][0]
        if message.get('ts') != esc['thread_ts'] or message.get('user') != rt.BOT:
            raise RuntimeError('card_readback_failed')
        block = {'type': 'context', 'block_id': rt.buttons.OUTCOME_BLOCK_ID,
                 'elements': [{'type': 'mrkdwn', 'text': line}]}
        rt.slack.api_call('chat.update', rt.api_identity(bot=True), json_body={
            'channel': row['card_channel'], 'ts': esc['thread_ts'], 'text': message.get('text', ''),
            'blocks': rt.buttons.decided_blocks(message.get('blocks'), block)})
    except Exception as exc:  # noqa: BLE001 - only the card keeps stale buttons
        with rt.transaction(db):
            db.execute('UPDATE stuck_claims SET error=? WHERE id=?', ('card_close_failed:' + rt.safe_error(exc), row['id']))


def overview(db, claude, limit_minutes, now=None, limit=20):
    """Read model (dashboard /health, `health` CLI): blocked sessions and the latest cards. Ids and counts only."""
    now = time.time() if now is None else now
    if 'stuck_claims' not in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return {'blocked': [], 'cards': [], 'open_claims': 0}
    blocked = []
    for session, claims in claude.stuck_claims(db, now - limit_minutes * 60 if limit_minutes else now).items():
        behind = claude.queued_behind(db, session)
        if behind:
            blocked.append({'session': session, 'claimed': len(claims), 'oldest_age': now - claims[0][1],
                            'queued_behind': behind})
    cards = [{**dict(r), 'events': len(json.loads(r['event_ids'])), 'event_ids': None} for r in db.execute(
        'SELECT id, session_id, task_id, event_ids, state, choice, decided_at, created_at FROM stuck_claims '
        'ORDER BY created_at DESC LIMIT ?', (limit,))]
    open_claims = db.execute("SELECT count(*) FROM claude_claims c JOIN events e ON e.id=c.event_id WHERE "
                             "c.released_at IS NULL AND e.status='claimed'").fetchone()[0]
    return {'blocked': blocked, 'cards': cards, 'open_claims': open_claims}


class StuckClaims:
    """The owner-bound half: card wording, the scan that cards stuck batches and the settle of the owner's click."""

    def __init__(self, language, *, owner_user, task_prefix):
        self.language, self.owner_user, self.task_prefix = language, owner_user, task_prefix
        self.options = [f'{choice}={self.msg(f"stuck.option_{choice}")}:{style}' for choice, style in OPTION_STYLES]
        self.reply = {key: self.msg(f'stuck.reply_{key}') for key in REPLY_KEYS}  # templates, formatted with n

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def _age(self, seconds):
        if seconds >= 86400:
            return self.msg('stuck.age_days', n=int(seconds // 86400))
        if seconds >= 3600:
            return self.msg('stuck.age_hours', n=int(seconds // 3600))
        return self.msg('stuck.age_minutes', n=max(1, int(seconds // 60)))

    def card_text(self, task_id, claimed, age_seconds, behind):
        """One plain sentence first (whose session, how many, how long, what it blocks): it opens with what the owner
        has to do and says what each button does."""
        who = (self.msg('stuck.who_task', task=self.task_prefix.sub("", task_id)) if task_id else
               self.msg('stuck.who_unknown'))
        return self.msg('stuck.card', who=who, claimed=claimed, age=self._age(age_seconds), behind=behind)

    def scan(self, rt, db, now=None):
        """Supervisor step: close cards the session already settled; card each newly stuck batch once."""
        now = time.time() if now is None else now
        limit = minutes(rt)
        out = []
        with rt.transaction(db):  # a card post interrupted by a crash counts as a failed post (retried 30 min after it)
            db.execute("UPDATE stuck_claims SET state='failed',error='stale_posting' WHERE state='posting' AND updated_at<?",
                       (now - 600,))
        for row in db.execute("SELECT * FROM stuck_claims WHERE state='open'").fetchall():
            if rt.claude.exact_claimed(db, row['session_id'], json.loads(row['event_ids'])):
                continue
            with rt.transaction(db):
                db.execute("UPDATE stuck_claims SET state='cleared',updated_at=? WHERE id=? AND state='open'", (now, row['id']))
            _close_card(rt, db, row, self.msg('stuck.cleared'))
            out.append({'id': row['id'], 'action': 'cleared'})
        if not limit:
            return out
        for session, claims in rt.claude.stuck_claims(db, now - limit * 60).items():
            ids = sorted(eid for eid, _ in claims)
            behind = rt.claude.queued_behind(db, session)
            if not behind or db.execute("SELECT 1 FROM stranded_deliveries WHERE session=? AND state='suspect' LIMIT 1",
                                        (session,)).fetchone():
                continue
            out.append(self.post_card(rt, db, session, ids, claims[0][1], behind, now))
        return [o for o in out if o]

    def post_card(self, rt, db, session, ids, oldest, behind, now):
        """One card per stuck batch — the same events claimed at the same time. Deduped in the insert's transaction: an
        open (or just posting) card of the session, or an earlier row for exactly this batch; a failed post, or one lost to
        a crash, is retried 30 minutes after the attempt. A batch re-claimed after a requeue that sticks again is new."""
        task_id = task_of_session(rt, db, session)
        sid = 'sc' + uuid.uuid4().hex[:14]
        with rt.transaction(db):
            if db.execute("SELECT 1 FROM stuck_claims WHERE session_id=? AND ((state='open') OR (state='posting' AND "
                          "updated_at>?) OR (event_ids=? AND oldest_claimed_at=? AND state NOT IN ('posting','failed')) OR "
                          "(event_ids=? AND oldest_claimed_at=? AND state='failed' AND updated_at>?)) LIMIT 1",
                          (session, now - 600, json.dumps(ids), oldest, json.dumps(ids), oldest, now - 1800)).fetchone():
                return None
            db.execute('INSERT INTO stuck_claims (id,session_id,task_id,event_ids,oldest_claimed_at,queued_behind,state,'
                       "created_at,updated_at) VALUES (?,?,?,?,?,?,'posting',?,?)",
                       (sid, session, task_id, json.dumps(ids), oldest, behind, now, now))
        try:
            card = rt.supervisor_card(db, self.card_text(task_id, len(ids), now - oldest, behind), self.options,
                                      kind='stuck_claim_card', footer=self.msg('stuck.footer', url=rt.DASHBOARD_URL), now=now)
        except Exception as exc:  # noqa: BLE001 - recorded; the batch is carded again after 30 minutes
            with rt.transaction(db):
                db.execute("UPDATE stuck_claims SET state='failed',error=?,updated_at=? WHERE id=?",
                           (rt.safe_error(exc), now, sid))
            return {'id': sid, 'action': 'card_failed'}
        with rt.transaction(db):
            db.execute("UPDATE stuck_claims SET state='open',card_event=?,card_channel=?,card_ts=?,updated_at=? WHERE id=?",
                       (card['event_id'], card['channel'], card['thread_ts'], now, sid))
        return {'id': sid, 'action': 'carded', 'session': session, 'events': ids, 'card': card['event_id']}

    def settle(self, rt, db, now=None):
        """Supervisor step: the owner's click on a stuck-claim card (only the owner's) is applied here, then confirmed
        in the thread."""
        now = time.time() if now is None else now
        done = []
        for row in db.execute("SELECT * FROM stuck_claims WHERE state='open' AND card_event IS NOT NULL").fetchall():
            d = db.execute('SELECT d.*, c.options FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                           'WHERE d.event_id=?', (row['card_event'],)).fetchone()
            if d is None or d['actor'] != self.owner_user:
                continue
            choice = rt.buttons.resolve_choice(d['choice'], rt.buttons.card_model(d['options']))['id']
            ids = json.loads(row['event_ids'])
            reason = f'stuck_claim_{choice}:{row["id"]}'
            if choice == 'requeue':
                changed = rt.claude.requeue_claimed(db, row['session_id'], ids, reason, now)
            elif choice == 'settle':
                changed = rt.claude.settle_claimed(db, row['session_id'], ids, reason, now)
            else:
                changed = []
            result = {'changed': changed, 'unchanged': [i for i in ids if i not in changed]}
            with rt.transaction(db):
                db.execute("UPDATE stuck_claims SET state='resolved',choice=?,decided_by=?,decided_at=?,result=?,updated_at=? "
                           "WHERE id=? AND state='open'", (choice, d['actor'], d['decided_at'], json.dumps(result), now,
                                                           row['id']))
            reply = self.reply['none'] if choice != 'later' and not changed else self.reply[choice].format(n=len(changed))
            rt.rules._card_reply(rt, db, row['card_event'], reply, row['id'], kind='stuck_claim_result')
            done.append({'id': row['id'], 'choice': choice, **result})
        return done
