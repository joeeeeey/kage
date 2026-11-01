"""P35 learn rules from the owner's card feedback (从卡片反馈提炼规则). Deterministic capture, model-gated adoption.

Every the owner-facing card (ask-<owner key>, rule-check, intake brief / draft, rule / routine / skill proposal, routine
confirmation, blocker stage1 / 2, orphan / deadline / lifecycle) carries a complete causal chain: who sent what ->
how the system handled it -> the owner's choice + free-text feedback = the handling he actually wants. P35 mines that
chain into PROPOSED rules. Adoption is ALWAYS the owner-gated (never auto-adopt) — a candidate goes live only through the
existing P3 rule-proposal card [采纳] [不采纳].

Three layers over one `提炼 -> 你批准 -> 生效` pipeline:
  1. Capture (always automatic): `capture` is a supervisor settle step. For every new the owner decision on a card it writes
     one `interaction_records` row — trigger (envelope kind / author / trust, source task, category / system), handling
     (card type, what the agent proposed / did, options, recommended option, disposition), response (the owner's option +
     provenance, an `override` flag = choice != recommended / not what the agent did, a structured one-line
     DISTILLATION of his intent), outcome. No raw feedback text in the DB: a note on the decision is redacted and
     appended to the private store `<default_corpus_dir>/` (0700 / 0600), the DB keeps only the
     distillation + ids + a private pointer.
  2. Strong-signal card (immediate, CONSERVATIVE): on a single interaction with a strong signal — (a) the owner overrode the
     recommended / done option AND (b) his feedback used generalizable directive language, OR (c) it is the >=2nd
     correction of the same (category, action, shape) — ONE code-owned assistant-bot card asks "要不要固化成规则？"
     [提炼成规则] [只记这次] [不用]. Capped per day, deduped by shape, a declined shape suppressed for N days.
     [提炼成规则] re-dispatches the <owner key>-inbox to draft a concrete candidate rule and `rule-propose` it (the existing
     gated path); [只记这次] records only; [不用] suppresses the shape.
  3. Weekly batch: `review_interactions` (folded into the existing weekly `rules-review-data`) mines accumulated
     records for volume patterns (>=N same-shape interactions handled the same way) and consolidates [只记这次] items ->
     proposals through the existing weekly cards. Aggregates and ids only, never raw text.

The distillation field is produced by the handling session (`interaction-distill`); capture always writes the row even
if no session enriches it. Raw feedback text lives ONLY in the redacted private store, scanned for leftover secrets.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from kage import locale

PROMOTE_FRESH_SECONDS = 3 * 3600


CAPTURE_PER_CYCLE = 200


LEARN_WEEKS = 8


VOLUME_THRESHOLD = 3


PROMOTE_KIND = 'promote_rule_card'


NO_RULE_COOLDOWN_SECONDS = 3 * 86400


DRAFT_STALE_SECONDS = 6 * 3600


APPLY_CLAIM_STALE_SECONDS = 300


REOPEN_SINCE_KEY = 'feedback_reopen_since'


REDRAFT_ENDED_GRACE_SECONDS = 600


PROPOSAL_LIKE = {'rule_proposal', 'rule_check', 'skill_proposal', 'routine_proposal', 'routine_confirmation'}


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS interaction_records (
        id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, card_type TEXT NOT NULL, task_id TEXT, category TEXT,
        system TEXT, trig_kind TEXT, trig_author_kind TEXT, trig_trust TEXT, agent_action TEXT NOT NULL,
        options TEXT, recommended TEXT, choice TEXT NOT NULL, choice_kind TEXT NOT NULL, provenance TEXT NOT NULL,
        override INTEGER NOT NULL DEFAULT 0, directive INTEGER NOT NULL DEFAULT 0, has_feedback INTEGER NOT NULL DEFAULT 0,
        feedback_pointer TEXT, feedback_source_ts TEXT, distillation TEXT, distilled_by TEXT, shape TEXT NOT NULL,
        outcome TEXT, promoted TEXT, decided_by TEXT NOT NULL, decided_at REAL NOT NULL, smoke INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS interaction_records_shape ON interaction_records(shape, decided_at);
      CREATE INDEX IF NOT EXISTS interaction_records_decided ON interaction_records(decided_at);
      CREATE TABLE IF NOT EXISTS promote_cards (
        id TEXT PRIMARY KEY, record_id TEXT NOT NULL, shape TEXT NOT NULL, signal TEXT NOT NULL, state TEXT NOT NULL,
        card_event TEXT, card_channel TEXT, card_ts TEXT, choice TEXT, dispatch_id TEXT, suppress_until REAL,
        decided_by TEXT, decided_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT,
        smoke INTEGER NOT NULL DEFAULT 0);
      CREATE INDEX IF NOT EXISTS promote_cards_shape ON promote_cards(shape, created_at);
      CREATE INDEX IF NOT EXISTS promote_cards_state ON promote_cards(state);
    ''')
    # P37: a draft-before-ask rule card + its redraft discussion. `semantic_shape` is the model-provided semantic
    # request category (what-to-do class) used for the label; `draft` is the structured candidate rule the card carries;
    # the redraft chain and its thread cursor; `guidance_pointer` keeps the owner's redraft guidance in the private corpus.
    _ensure_columns(db, 'interaction_records', {'semantic_shape': 'TEXT'})
    _ensure_columns(db, 'promote_cards', {'draft': 'TEXT', 'redraft_of': 'TEXT', 'guidance_pointer': 'TEXT',
                                          'redraft_cursor': 'TEXT', 'redraft_polled_at': 'REAL'})
    # P38 (F43): when a card whose redraft fell through was reopened (restarts its thread-poll ladder, F38).
    _ensure_columns(db, 'promote_cards', {'reopened_at': 'REAL'})
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_meta'").fetchone():
        db.execute('INSERT OR IGNORE INTO runtime_meta VALUES (?,?)', (REOPEN_SINCE_KEY, f'{time.time():.6f}'))
    db.commit()


def _ensure_columns(db, table, columns):
    """Idempotently ALTER a p35 table to add p37 columns (additive; no existing row or column is rewritten)."""
    have = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
    for name, decl in columns.items():
        if name not in have:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')


def config(rt):
    try:
        return rt.worker_config.load()[0]['feedback_learning']
    except Exception:  # noqa: BLE001 - a malformed config never captures feedback or posts a card by accident
        return {**rt.worker_config.DEFAULTS['feedback_learning'], 'enabled': False}


def _db_path(db):
    row = next((r for r in db.execute('PRAGMA database_list') if r[1] == 'main'), None)
    return row[2] if row else None


_POINTER_RE = re.compile(r'(?P<file>[A-Za-z0-9_-]{1,64}\.md)#(?P<id>[A-Za-z0-9_-]{1,64})')


def _task_category(db, task_id):
    if not task_id:
        return None
    row = db.execute('SELECT category FROM dispatch_meta WHERE task_key=?', (task_id,)).fetchone()
    return row['category'] if row and row['category'] else None


def _origin_table(db, card_event):
    """Which originating record owns this card -> (card_type, agent_action, system, category, outcome). The card is
    the universal `decisions`/`escalation_cards` row; the originating table adds what the agent proposed / did."""
    def one(sql, args=()):
        return db.execute(sql, (card_event, *args)).fetchone()
    try:
        r = one('SELECT kind, category, status FROM rule_proposals WHERE card_event=?')
        if r:
            return 'rule_proposal', 'proposal:' + (r['kind'] or '?'), None, r['category'], r['status']
    except Exception:  # noqa: BLE001 - a table may be absent in an old DB
        pass
    r = one('SELECT outcome, system, action, kind, category, result FROM rule_checks WHERE card_event=?')
    if r:
        return 'rule_check', 'rule_check:' + (r['outcome'] or '?'), r['system'], r['category'], r['result']
    try:
        r = one('SELECT system, operation, type FROM automation_gaps WHERE card_event=?')
        if r:
            return 'blocker_stage1', 'blocker:' + (r['type'] or '?'), r['system'], None, None
        r = one('SELECT system, operation, type FROM automation_gaps WHERE stage2_event=?')
        if r:
            return 'blocker_stage2', 'blocker_done:' + (r['type'] or '?'), r['system'], None, None
        r = one('SELECT system, operation, status FROM gap_proposals WHERE card_event=?')
        if r:
            return 'skill_proposal', 'skill_proposal', r['system'], None, r['status']
    except Exception:  # noqa: BLE001
        pass
    try:
        r = one('SELECT status FROM routine_proposals WHERE card_event=?')
        if r:
            return 'routine_proposal', 'routine_proposal', None, None, r['status']
        r = one('SELECT status FROM routine_confirmations WHERE card_event=?')
        if r:
            return 'routine_confirmation', 'routine_confirm', None, None, r['status']
    except Exception:  # noqa: BLE001
        pass
    r = one('SELECT kind FROM intake_briefs WHERE card_event=?')
    if r:
        ctype = 'intake_draft' if (r['kind'] or '').startswith('draft') else 'intake_brief'
        return ctype, 'intake:' + (r['kind'] or '?'), None, None, None
    try:
        r = one('SELECT reason, state FROM orphan_cards WHERE card_event=?')
        if r:
            return 'orphan', 'orphan:' + (r['reason'] or '?'), None, None, r['state']
        r = one('SELECT disposition FROM task_deadlines WHERE card_event=?')
        if r:
            return 'deadline', 'deadline', None, None, r['disposition']
    except Exception:  # noqa: BLE001
        pass
    return None


def compute_override(origin, choice_id, choice_kind):
    """the owner overrode when: an explicitly recommended option was not chosen; or, on a card where the agent proposed /
    wanted to act, he did not approve it."""
    rec = origin['recommended']
    if rec is not None:
        return choice_id != rec, rec
    if origin['card_type'] in PROPOSAL_LIKE:
        return choice_kind != 'approve', '(approve)'
    return False, None


def shape_key(card_type, category, agent_action):
    raw = f'{card_type}|{category or "-"}|{agent_action or "-"}'
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _get(record, key):
    """Row / dict accessor that tolerates a column absent on an older row object."""
    try:
        return record[key]
    except (KeyError, IndexError):
        return None


def _same_shape_corrections(db, shape, before_id, decided_at):
    """How many PRIOR same-shape interactions were overrides (corrections) — the (c) repeat signal basis."""
    return db.execute('SELECT count(*) FROM interaction_records WHERE shape=? AND override=1 AND smoke=0 AND id!=? '
                      'AND decided_at<=?', (shape, before_id, decided_at)).fetchone()[0]


def strong_signal(db, record, cfg):
    """Return the strong-signal name or None. Conservative (the owner's choice): (a) override AND (b) directive language
    together, OR (c) the >=2nd correction of the same shape. moderate / aggressive progressively loosen."""
    mode = cfg.get('strong_signal_mode', 'conservative')
    override, directive = bool(record['override']), bool(record['directive'])
    prior = _same_shape_corrections(db, record['shape'], record['id'], record['decided_at'])
    repeat = override and (prior + 1) >= 2
    if repeat:
        return 'repeat_correction'
    if override and directive:
        return 'override_directive'
    if mode == 'moderate' and (override or directive):
        return 'override' if override else 'directive'
    if mode == 'aggressive' and (override or directive):
        return 'override' if override else 'directive'
    return None


def _shape_suppressed(db, shape, now):
    """A shape the owner dropped, or one the inbox could not draft a rule for, is cooled for a while (`suppress_until`)."""
    row = db.execute('SELECT max(suppress_until) FROM promote_cards WHERE shape=? AND suppress_until IS NOT NULL',
                     (shape,)).fetchone()
    return bool(row and row[0] and row[0] > now)


def _recent_shape_card(db, shape, now):
    """One live promote flow per shape at a time (drafting / open / awaiting the redraft guidance), and never two in a
    row within a day (the record still counts). A dropped / no-rule shape is cooled by _shape_suppressed."""
    return db.execute(
        "SELECT 1 FROM promote_cards WHERE shape=? AND smoke=0 AND (state IN ('drafting','posting','open',"
        "'awaiting_guidance') OR created_at>?) LIMIT 1", (shape, now - 86400)).fetchone() is not None


def _cards_today(db, now):
    return db.execute('SELECT count(*) FROM promote_cards WHERE created_at>? AND smoke=0', (now - 86400,)).fetchone()[0]


def _shape_history_count(db, shape):
    return db.execute('SELECT count(*) FROM interaction_records WHERE shape=? AND smoke=0', (shape,)).fetchone()[0]


def _redispatch(rt, db, db_path, record, prompt, reason, now):
    """Re-activate the <owner key>-inbox headless with `prompt` (mirrors task_lifecycle._redispatch / blockers' launch)."""
    disp = rt.make_dispatcher(db, db_path)
    cfg_all = rt.worker_config.load()[0]
    inbox_task = cfg_all['intake']['inbox_task']
    task = disp.task_for_key(inbox_task)
    if task is None:
        raise ValueError('inbox_task_not_registered:' + str(inbox_task))
    # P39: the inbox's preferred provider (its pin, else intake.inbox_provider); its stored preference is never rewritten.
    return disp.launch(task, 'auto', prompt, reason=reason, now=now, inherit=True, persist=False)


def _cli(rt, db_path):
    return f'python3 {Path(rt.__file__).resolve()} --db {db_path}'


DRAFT_FIELDS = ('title', 'scenario', 'default', 'override', 'rule', 'impact')


def _reap_stale_drafts(rt, db, now):
    """A drafting card whose <owner key>-inbox run never produced a rule_draft within DRAFT_STALE_SECONDS is reaped and its
    shape briefly cooled, so capture does not keep re-dispatching the same shape."""
    for row in db.execute("SELECT id FROM promote_cards WHERE state='drafting' AND created_at<? AND smoke=0",
                          (now - DRAFT_STALE_SECONDS,)).fetchall():
        with rt.transaction(db):
            db.execute("UPDATE promote_cards SET state='draft_expired',suppress_until=?,updated_at=? WHERE id=?",
                       (now + NO_RULE_COOLDOWN_SECONDS, now, row['id']))


def _launched_apply(db, promote_id):
    """A promote card's apply dispatch, if its launch committed (Dispatcher.launch inserts it in one transaction)."""
    row = db.execute('SELECT dispatch_id FROM dispatches WHERE reason=? ORDER BY created_at DESC LIMIT 1',
                     ('feedback_apply:' + promote_id,)).fetchone()
    return row['dispatch_id'] if row else None


def _newest_redraft(db, promote_id):
    return db.execute('SELECT * FROM promote_cards WHERE redraft_of=? ORDER BY created_at DESC, rowid DESC LIMIT 1',
                      (promote_id,)).fetchone()


def _redraft_is_current(db, row):
    """A chained redraft still owns its discussion: the original card waits on it (`redrafted`) and it is the newest."""
    parent = db.execute('SELECT state FROM promote_cards WHERE id=?', (row['redraft_of'],)).fetchone()
    if parent is None:
        return True
    newest = _newest_redraft(db, row['redraft_of'])
    return parent['state'] == 'redrafted' and newest is not None and newest['id'] == row['id']


def _redraft_dispatch(db, pid, dispatch_id=None):
    """P38 (F43): a redraft's drafting dispatch — by id, else by its reason `feedback_redraft:<pc…>` (a crash / DB lock
    between the launch and recording the id) — or None when it never launched."""
    row = db.execute('SELECT * FROM dispatches WHERE dispatch_id=?', (dispatch_id,)).fetchone() if dispatch_id else None
    return row or db.execute('SELECT * FROM dispatches WHERE reason=? ORDER BY created_at DESC LIMIT 1',
                             ('feedback_redraft:' + pid,)).fetchone()


def _promote_thread_reply(rt, db, card_event, promote_id, smoke, text):
    return rt.rules._card_reply(rt, db, card_event, ('🧪 TEST · ' if smoke else '') + text, promote_id,
                                kind='promote_card_result')


_TS_RE = re.compile(r'\d+\.\d+')


_MENTION_RE = re.compile(r'<[@!#][^>]*>')


def _smoke_drafting(rt, db, record, shape, now, redraft_of=None):
    pid = 'pc' + uuid.uuid4().hex[:14]
    with rt.transaction(db):
        db.execute('INSERT INTO promote_cards (id,record_id,shape,signal,state,created_at,updated_at,smoke,redraft_of) '
                   "VALUES (?,?,?,?,'drafting',?,?,1,?)", (pid, record['id'], shape, 'override_directive', now, now,
                                                           redraft_of))
    return pid


def _smoke_apply(rt, db, db_path, session, provider, now):
    """Drive the [就按这条固化] apply path synthetically into a temp rules dir via the existing smoke rule-proposal path."""
    try:
        return rt.rules.smoke_proposal(rt, db, db_path, card=False, session=session, provider=provider)
    except Exception as exc:  # noqa: BLE001 - the smoke still proves the draft + rich card + redraft path
        return {'ok': False, 'error': rt.safe_error(exc)}


class Learning:
    """The owner-bound half of feedback learning: the capture of the owner's card decisions, the promote card,
    its draft / redraft / apply re-dispatches and replies in the owner's words, the review and the smoke; the
    owner's time zone, Slack user and private feedback store."""

    def __init__(self, language, *, tz, owner_key, owner_name, owner_slack_user, owner_user, corpus_dir_env, default_corpus_dir, smoke_scope, late_binding=None):
        self.late_binding = dict(late_binding or {})
        self.language = language
        self.tz = tz
        self.owner_slack_user = owner_slack_user
        self.owner_user = owner_user
        self.corpus_dir_env = corpus_dir_env
        self.default_corpus_dir = default_corpus_dir
        self.smoke_scope = smoke_scope
        self.key, self.name = owner_key, owner_name
        self.directive_re = re.compile(
                 self.pattern('learning.directive_re.1'))
        self.secret_patterns = [
                 re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S),
                 re.compile(r'\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{16,}'),
                 re.compile(r'\bxox[abposr]-[A-Za-z0-9\-]{10,}'),
                 re.compile(r'\bxapp-[A-Za-z0-9\-]{10,}'),
                 re.compile(r'\bAKIA[0-9A-Z]{16}\b'),
                 re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}'),
                 re.compile(r'\bglpat-[A-Za-z0-9\-_]{16,}'),
                 re.compile(r'\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}'),
                 re.compile(self.pattern('learning.secret_patterns.1'), re.I),
                 re.compile(r"(?P<keep>password\s+)'[^']+'", re.I),
                 re.compile(self.pattern('learning.secret_patterns.2'), re.I),
                 re.compile(r'(?P<keep>\([A-Za-z0-9_.\-]{3,},\s*)(?=[^\s)]*[A-Z])(?=[^\s)]*[a-z])(?=[^\s)]*\d)[^\s)]{8,}'),
                 re.compile(r'(?P<keep>[“"\'`])(?=[^”"\'`\s]*[A-Z])(?=[^”"\'`\s]*[a-z])(?=[^”"\'`\s]*\d)[^”"\'`\s]{10,}(?=[”"\'`])'),
                 re.compile(r'\b[A-Za-z0-9+/]{48,}={0,2}'),
             ]
        self.promote_options = [self.msg('learning.promote_options.1'), self.msg('learning.promote_options.2'), self.msg('learning.promote_options.3'), self.msg('learning.promote_options.4')]
        self.promote_hint = self.msg('learning.promote_hint.1')
        self.promote_reply = {
                 'apply': self.msg('learning.promote_reply.apply', owner_key=self.key),
                 'redraft': self.msg('learning.promote_reply.redraft'),
                 'log': self.msg('learning.promote_reply.log'),
                 'drop': self.msg('learning.promote_reply.drop'),
             }
        self.redraft_not_started = self.msg('learning.redraft_not_started.1')
        self.redraft_fell_through = {
                 'no_rule': self.msg('learning.redraft_fell_through.no_rule'),
                 'draft_expired': self.msg('learning.redraft_fell_through.draft_expired'),
                 'draft_failed': self.msg('learning.redraft_fell_through.draft_failed'),
                 'draft_ended': self.msg('learning.redraft_fell_through.draft_ended'),
                 'not_started': self.redraft_not_started,
             }
        self.card_type_label = {
                 f'ask_{self.key}': self.msg('learning.card_type_label.ask_owner', owner=self.name), 'rule_check': self.msg('learning.card_type_label.rule_check'), 'rule_proposal': self.msg('learning.card_type_label.rule_proposal'),
                 'routine_proposal': self.msg('learning.card_type_label.routine_proposal'), 'routine_confirmation': self.msg('learning.card_type_label.routine_confirmation'), 'skill_proposal': self.msg('learning.card_type_label.skill_proposal'),
                 'blocker_stage1': self.msg('learning.card_type_label.blocker_stage1'), 'blocker_stage2': self.msg('learning.card_type_label.blocker_stage2'), 'intake_brief': self.msg('learning.card_type_label.intake_brief'),
                 'intake_draft': self.msg('learning.card_type_label.intake_draft'), 'orphan': self.msg('learning.card_type_label.orphan'), 'deadline': self.msg('learning.card_type_label.deadline'), 'decision': self.msg('learning.card_type_label.decision')}
        self.draft_prompt = (
                 self.msg('learning.draft_prompt.1', owner=self.name))
        self.apply_prompt = (
                 self.msg('learning.apply_prompt.1', owner=self.name))
        self.redraft_prompt = (
                 self.msg('learning.redraft_prompt.1', owner=self.name))
        self._redraft_prefix_re = re.compile(self.pattern('learning._redraft_prefix_re.1'))
        self.smoke_draft = {'title': self.msg('learning.smoke_draft.title'), 'scenario': self.msg('learning.smoke_draft.scenario'),
                            'default': self.msg('learning.smoke_draft.default'), 'override': self.msg('learning.smoke_draft.override'),
                            'rule': self.msg('learning.smoke_draft.rule'),
                            'impact': self.msg('learning.smoke_draft.impact')}

    def _late(self, name):
        """`name` as the deployment binds it when the call is made (its module's name: a test that replaces it
        reaches every call); else this module's own."""
        hook = self.late_binding.get(name)
        return hook if hook is not None else getattr(self, name, None) or globals()[name]

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def redact(self, text):
        for pattern in self.secret_patterns:
            if 'keep' in pattern.groupindex:
                text = pattern.sub(lambda m: m.group('keep') + '[REDACTED]', text)
            else:
                text = pattern.sub('[REDACTED]', text)
        return text

    def corpus_dir(self):
        return Path(os.environ.get(self.corpus_dir_env) or os.path.expanduser(self.default_corpus_dir))

    def store_feedback(self, rt, shape, record_id, text, decided_at):
        """Append the owner's raw feedback (redacted + secret-scanned) to the private per-shape corpus file (0700 / 0600).
        Returns a pointer `<shape>.md#<record_id>` for the DB; the raw text NEVER enters the DB / workspace / dashboard."""
        cleaned = self.redact(' '.join(str(text or '').split()))
        if not cleaned:
            return None
        try:  # tripwire: a leftover secret means store a placeholder, never the value
            rt.asks.check_secret_free(cleaned)
        except Exception:  # noqa: BLE001
            cleaned = '[REDACTED feedback — tripwire]'
        root = self.corpus_dir()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        target = root / f'{shape}.md'
        stamp = dt.datetime.fromtimestamp(decided_at, self.tz).isoformat()
        line = f'\n- `{record_id}` {stamp}\n\n  {cleaned}\n'
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            stream.write(line)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return f'{shape}.md#{record_id}'

    def read_feedback(self, pointer):
        """P38 (F60): the redacted corpus entries behind a private pointer `<shape>.md#<id>` (oldest first), read back for a
        drafting session on demand. A malformed pointer, a symlink, a non-regular or a missing file reads as nothing."""
        m = _POINTER_RE.fullmatch(str(pointer or ''))
        if not m:
            return []
        try:  # never blocks on a FIFO planted in the corpus dir; only a regular file is read
            fd = os.open(self.corpus_dir() / m['file'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return []
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return []
        with os.fdopen(fd, encoding='utf-8') as stream:
            lines = stream.read().split('\n')
        header = re.compile(r'- `' + re.escape(m['id']) + r'` \S+')
        out = []
        for n, line in enumerate(lines):
            # store_feedback writes "- `<id>` <stamp>", a blank line, then the one-line (whitespace-collapsed) text.
            if header.fullmatch(line):
                body = next((x.strip() for x in lines[n + 1:n + 3] if x.strip()), '')
                if body:
                    out.append(body)
        return out

    def card_origin(self, rt, db, card_event, card_task_id=None):
        """Resolve the causal chain behind a resolved card: card_type, agent_action, system, category, recommended option,
        options, source task, trigger envelope, outcome. Falls back to an ask-<owner key> / generic decision when no originating
        record owns the card (an agent's bare ask-<owner key> card)."""
        card = db.execute('SELECT * FROM escalation_cards WHERE event_id=?', (card_event,)).fetchone()
        options_model = rt.buttons.card_model(card['options']) if card else []
        option_ids = [o['id'] for o in options_model]
        marked = next((o['id'] for o in options_model if o.get('recommended')), None)
        origin = _origin_table(db, card_event)
        task_id = card_task_id or (card['task_id'] if card else None)
        if origin is None:
            esc = db.execute('SELECT watch_id FROM escalations WHERE event_id=?', (card_event,)).fetchone()
            ctype = 'decision'
            if esc and esc['watch_id']:
                try:
                    w = rt.load(db, esc['watch_id'])
                    if w.get('origin') == f'ask_{self.key}':
                        ctype = f'ask_{self.key}'
                    if task_id is None:
                        task_id = rt.registry.task_id_for_cwd(w.get('cwd'))
                except Exception:  # noqa: BLE001
                    pass
            card_type, agent_action, system, category, outcome = ctype, ctype, None, None, None
        else:
            card_type, agent_action, system, category, outcome = origin
        category = category or _task_category(db, task_id)
        trig = None
        try:
            trig = rt.envelopes.lookup(db, card_event)
        except Exception:  # noqa: BLE001 - the envelope is best effort
            trig = None
        return {'card_type': card_type, 'agent_action': agent_action, 'system': system, 'category': category,
                'recommended': marked, 'options': option_ids, 'task_id': task_id, 'outcome': outcome, 'trig': trig,
                'smoke': bool(card['smoke']) if card else False}

    def shape_label(self, record):
        """A safe human label for a shape. P37: prefer the model-provided SEMANTIC request category (what-to-do class) so
        the owner never sees the internal card-type shape ("问 the owner"); fall back to card type + system / category tokens. Never
        raw feedback text."""
        semantic = _get(record, 'semantic_shape')
        if semantic and str(semantic).strip():
            return ' '.join(str(semantic).split())[:120]
        parts = [self.card_type_label.get(record['card_type'], record['card_type'])]
        if record['system']:
            parts.append(record['system'])
        elif record['category']:
            parts.append(record['category'])
        return ' · '.join(parts)

    def _distillation(self, origin, choice_id, override, directive):
        """A short structured one-line fallback distillation (no raw text). A handling session refines it via
        `interaction-distill`."""
        bits = [self.msg('learning._distillation.1', get=self.card_type_label.get(origin['card_type'], origin['card_type']), choice_id=choice_id)]
        if override:
            bits.append(self.msg('learning._distillation.2'))
        if directive:
            bits.append(self.msg('learning._distillation.3'))
        return self.msg('learning._distillation.4').join(bits)

    def capture_one(self, rt, db, decision, now, store=True):
        """Write (idempotently) one interaction_records row for a resolved the owner decision. `decision` is a joined
        decisions ⋈ escalation_cards row. Returns the record dict, or None when already captured."""
        event_id = decision['event_id']
        if db.execute('SELECT 1 FROM interaction_records WHERE event_id=?', (event_id,)).fetchone():
            return None
        origin = self.card_origin(rt, db, event_id, decision['task_id'])
        option = rt.buttons.resolve_choice(decision['choice'], rt.buttons.card_model(decision['options']))
        choice_id, choice_kind = option['id'], option['kind']
        override, rec = compute_override(origin, choice_id, choice_kind)
        note = decision['note']
        directive = bool(note and self.directive_re.search(note))
        shape = shape_key(origin['card_type'], origin['category'], origin['agent_action'])
        rid = 'ir' + uuid.uuid4().hex[:14]
        pointer = None
        if store and note:
            try:
                pointer = self._late('store_feedback')(rt, shape, rid, note, decision['decided_at'])
            except Exception as exc:  # noqa: BLE001 - never block capture on a store write
                rt._meta_error(db, 'feedback_store_error', exc)
        trig = origin['trig'] or {}
        record = {'id': rid, 'event_id': event_id, 'card_type': origin['card_type'], 'task_id': origin['task_id'],
                  'category': origin['category'], 'system': origin['system'], 'trig_kind': trig.get('kind'),
                  'trig_author_kind': trig.get('author_kind'), 'trig_trust': trig.get('trust'),
                  'agent_action': origin['agent_action'], 'options': json.dumps(origin['options'], ensure_ascii=False),
                  'recommended': rec, 'choice': choice_id, 'choice_kind': choice_kind, 'provenance': decision['provenance'],
                  'override': int(override), 'directive': int(directive), 'has_feedback': int(bool(pointer)),
                  'feedback_pointer': pointer,
                  'feedback_source_ts': decision['source_ts'] if decision['provenance'] == 'thread_reply' else None,
                  'distillation': self._distillation(origin, choice_id, override, directive), 'shape': shape,
                  'outcome': origin['outcome'], 'decided_by': decision['actor'], 'decided_at': decision['decided_at'],
                  'smoke': int(bool(decision['smoke']))}
        cols = ('id,event_id,card_type,task_id,category,system,trig_kind,trig_author_kind,trig_trust,agent_action,options,'
                'recommended,choice,choice_kind,provenance,override,directive,has_feedback,feedback_pointer,'
                'feedback_source_ts,distillation,shape,outcome,decided_by,decided_at,smoke,created_at,updated_at')
        vals = [record[k] for k in ('id', 'event_id', 'card_type', 'task_id', 'category', 'system', 'trig_kind',
                                    'trig_author_kind', 'trig_trust', 'agent_action', 'options', 'recommended', 'choice',
                                    'choice_kind', 'provenance', 'override', 'directive', 'has_feedback', 'feedback_pointer',
                                    'feedback_source_ts', 'distillation', 'shape', 'outcome', 'decided_by', 'decided_at',
                                    'smoke')] + [now, now]
        with rt.transaction(db):
            if db.execute('SELECT 1 FROM interaction_records WHERE event_id=?', (event_id,)).fetchone():
                return None
            db.execute(f'INSERT INTO interaction_records ({cols}) VALUES ({",".join("?" * len(vals))})', vals)
        return record

    def _new_decisions(self, db, now, limit):
        """the owner decisions on the owner-facing cards that have no interaction record yet and are not P35 promote cards."""
        return db.execute(
            'SELECT d.event_id, d.choice, d.actor, d.note, d.provenance, d.decided_at, d.source_ts, c.options, c.smoke, '
            'c.task_id FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id WHERE d.actor=? '
            'AND NOT EXISTS (SELECT 1 FROM interaction_records ir WHERE ir.event_id=d.event_id) '
            'AND NOT EXISTS (SELECT 1 FROM promote_cards pc WHERE pc.card_event=d.event_id) '
            'ORDER BY d.decided_at LIMIT ?', (self.owner_slack_user, limit)).fetchall()

    def capture(self, rt, db, db_path=None, now=None, limit=CAPTURE_PER_CYCLE):
        """Supervisor settle step: record every new the owner card decision, then (conservatively) on a strong signal re-dispatch
        <owner key>-inbox to DRAFT a concrete candidate rule first (draft-before-ask); the card is posted later by `rule_draft`."""
        now = time.time() if now is None else now
        db_path = db_path or _db_path(db)
        cfg = config(rt)
        if not cfg['enabled']:
            return {'captured': 0, 'drafts': 0}
        captured, drafted = 0, 0
        for decision in self._new_decisions(db, now, limit):
            try:
                record = self.capture_one(rt, db, decision, now)
            except Exception as exc:  # noqa: BLE001 - one bad decision never stops capture
                rt._meta_error(db, 'feedback_capture_error', exc)
                continue
            if record is None or record['smoke']:
                continue
            captured += 1
            if now - record['decided_at'] <= PROMOTE_FRESH_SECONDS:
                try:
                    if self.maybe_promote(rt, db, record, cfg, now, db_path=db_path):
                        drafted += 1
                except Exception as exc:  # noqa: BLE001
                    rt._meta_error(db, 'feedback_promote_error', exc)
        return {'captured': captured, 'drafts': drafted}

    def maybe_promote(self, rt, db, record, cfg, now, smoke=False, db_path=None):
        """On a strong signal, re-dispatch <owner key>-inbox to DRAFT a concrete candidate rule FIRST (draft-before-ask); the card
        is posted later by `rule_draft`, and only if a specific, articulable rule exists. Subject to the conservative gates."""
        signal = strong_signal(db, record, cfg)
        if signal is None:
            return None
        if not smoke:
            if _shape_suppressed(db, record['shape'], now) or _recent_shape_card(db, record['shape'], now):
                return None
            if _cards_today(db, now) >= cfg['max_promote_cards_per_day']:
                return None
        return self.dispatch_draft(rt, db, record, signal, now, db_path=db_path, smoke=smoke)

    def dispatch_draft(self, rt, db, record, signal, now, db_path=None, smoke=False, redraft_of=None):
        """Create a `drafting` promote card and re-dispatch <owner key>-inbox to draft a concrete candidate rule (then `rule_draft`
        posts the rich card, or logs a reasoned no-rule). Returns {'promote_id', 'dispatch_id', 'signal', 'shape'}."""
        db_path = db_path or _db_path(db)
        pid = 'pc' + uuid.uuid4().hex[:14]
        with rt.transaction(db):
            db.execute('INSERT INTO promote_cards (id,record_id,shape,signal,state,created_at,updated_at,smoke,redraft_of) '
                       "VALUES (?,?,?,?,'drafting',?,?,?,?)",
                       (pid, record['id'], record['shape'], signal, now, now, int(smoke), redraft_of))
            db.execute('UPDATE interaction_records SET promoted=?,updated_at=? WHERE id=?', (pid, now, record['id']))
        prompt = self.draft_prompt.format(cli=_cli(rt, db_path), shape=record['shape'], signal=signal, pid=pid)
        try:
            dispatch_id = self._late('_redispatch')(rt, db, db_path, record, prompt, 'feedback_draft:' + pid, now)
        except Exception as exc:  # noqa: BLE001 - recorded; the shape re-drafts after the 24 h dedupe window
            with rt.transaction(db):
                db.execute("UPDATE promote_cards SET state='draft_failed',error=?,updated_at=? WHERE id=?",
                           ((str(exc) if isinstance(exc, ValueError) else rt.safe_error(exc))[:300], now, pid))
            raise
        with rt.transaction(db):
            db.execute('UPDATE promote_cards SET dispatch_id=?,updated_at=? WHERE id=?', (dispatch_id, now, pid))
        return {'promote_id': pid, 'dispatch_id': dispatch_id, 'signal': signal, 'shape': record['shape']}

    def rule_draft(self, rt, db, db_path, promote_id, fields=None, no_rule=False, reason=None, basis_count=None, live_card=False,
                   now=None):
        """The re-dispatched <owner key>-inbox session's output for a `drafting` promote card: either a concrete drafted rule
        (-> post the rich, human-readable card) or a reasoned no-rule (-> log, no card, cool the shape). Every card field is
        secret-scanned and passed through the P3.1 copy standard (ids / hashes / jargon refused): the card is the owner-facing."""
        now = time.time() if now is None else now
        db_path = db_path or _db_path(db)
        row = db.execute('SELECT * FROM promote_cards WHERE id=?', (promote_id,)).fetchone()
        if row is None:
            raise ValueError('unknown_promote_card:' + str(promote_id))
        if row['state'] not in ('drafting', 'awaiting_guidance', 'draft_failed'):
            raise ValueError('promote_card_not_awaiting_a_draft:' + str(row['state']))
        if row['redraft_of'] and not row['smoke'] and not _redraft_is_current(db, row):
            # P38 (F43): the discussion went back to the original card (reopened, or the owner replied there again): a late
            # rule-draft on this abandoned redraft must not post a second live card next to it.
            raise ValueError('redraft_abandoned_the_original_card_is_live_again')
        record = db.execute('SELECT * FROM interaction_records WHERE id=?', (row['record_id'],)).fetchone()
        if no_rule:
            with rt.transaction(db):
                db.execute("UPDATE promote_cards SET state='no_rule',choice='no_rule',error=?,suppress_until=?,updated_at=? "
                           'WHERE id=?', ((' '.join(str(reason or '').split()))[:300] or 'no_coherent_rule',
                                          now + NO_RULE_COOLDOWN_SECONDS, now, promote_id))
            return {'ok': True, 'promote_id': promote_id, 'carded': False, 'reason': 'no_rule'}
        fields = fields or {}
        draft = {key: rt.buttons.rule_field(fields.get(key), key) for key in DRAFT_FIELDS}
        joined = '\n'.join(draft[k] for k in DRAFT_FIELDS)
        rt.asks.check_secret_free(joined)  # the whole card copy: no secrets
        rt.buttons.check_copy(joined, 'rule_draft', first_line_max=None, refs=False)  # no rule ids / hashes / jargon
        # (P41 refs=False: a rule may name a kind of card, e.g. 合并审批卡; it points the owner nowhere)
        if basis_count is None:
            basis_count = _shape_history_count(db, record['shape']) if record is not None else 0
        draft['basis_count'] = int(basis_count)
        # The semantic scenario name becomes the record's semantic shape, so /learning + history read it (not "问 the owner").
        if record is not None and not _get(record, 'semantic_shape'):
            with rt.transaction(db):
                db.execute('UPDATE interaction_records SET semantic_shape=?,updated_at=? WHERE id=?',
                           (draft['title'][:120], now, record['id']))
        return self._post_rule_card(rt, db, promote_id, draft, record, now, live_card=live_card)

    def _post_rule_card(self, rt, db, promote_id, draft, record, now, live_card=False):
        row = db.execute('SELECT * FROM promote_cards WHERE id=?', (promote_id,)).fetchone()
        smoke = bool(row['smoke'])
        body_blocks = rt.buttons.rule_card_blocks(draft)
        fallback = rt.buttons.rule_card_fallback(draft)   # supervisor_card prepends the the owner mention for readback
        if smoke:
            header = dict(body_blocks[0])
            header['text'] = {'type': 'plain_text', 'text': ('🧪 TEST · ' + header['text']['text'])[:150], 'emoji': True}
            body_blocks = [header] + body_blocks[1:]
            fallback = '🧪 TEST · ' + fallback
        footer = self.msg('learning._post_rule_card.1', basis_count=draft['basis_count'], dashboard_url=rt.DASHBOARD_URL)
        with rt.transaction(db):
            db.execute("UPDATE promote_cards SET state='posting',draft=?,updated_at=? WHERE id=?",
                       (json.dumps(draft, ensure_ascii=False), now, promote_id))
        try:
            # task_id=None keeps the card its own top-level topic (p36), so its thread replies are unambiguous for redraft.
            card = rt.supervisor_card(db, fallback, self.promote_options, kind=PROMOTE_KIND, footer=footer, task_id=None,
                                      smoke=smoke, live_card=live_card, now=now, body_blocks=body_blocks, hint=self.promote_hint)
        except Exception as exc:  # noqa: BLE001 - recorded; the shape re-drafts after the dedupe window
            with rt.transaction(db):
                db.execute("UPDATE promote_cards SET state='draft_failed',error=?,updated_at=? WHERE id=?",
                           (rt.safe_error(exc), now, promote_id))
            raise
        with rt.transaction(db):
            db.execute("UPDATE promote_cards SET state='open',card_event=?,card_channel=?,card_ts=?,updated_at=? WHERE id=?",
                       (card['event_id'], card['channel'], card['thread_ts'], now, promote_id))
        return {'ok': True, 'promote_id': promote_id, 'carded': True, 'card': card, 'shape': row['shape'],
                'basis_count': draft['basis_count']}

    def _dispatch_apply(self, rt, db, db_path, record, draft, promote_id, now):
        prompt = self.apply_prompt.format(cli=_cli(rt, db_path), shape=record['shape'] if record is not None else '',
                                     scenario=draft.get('scenario', self.msg('learning._dispatch_apply.1')), rule=draft.get('rule', self.msg('learning._dispatch_apply.2')))
        return self._late('_redispatch')(rt, db, db_path, record, prompt, 'feedback_apply:' + promote_id, now)

    def settle_promote(self, rt, db, db_path=None, now=None):
        """Supervisor settle step: the owner's click on a rule-distillation card routes back here. Only the owner's own click counts.
        [就按这条固化] -> re-dispatch the inbox to formalize + rule-propose (the EXISTING the owner-gated apply path, so the owner still
        sees and [采纳]s the exact final rule); [我改一下] -> wait for his thread guidance (settle_redraft redrafts);
        [只记这次] -> record only; [先不用] -> suppress the shape.

        P38 (F55): a click is claimed (conditional UPDATE off `open`) before any side effect; an apply is claimed as
        `applying`, launched, then finalized `resolved`, and a stale `applying` is recovered without a second launch
        (_recover_applying). Each row is isolated: one bad row never blocks the rest. P38 (F43): a card whose redraft fell
        through is reopened first, so a click the owner made on it meanwhile settles in this same pass."""
        now = time.time() if now is None else now
        db_path = db_path or _db_path(db)
        cfg = config(rt)
        _reap_stale_drafts(rt, db, now)
        if cfg['enabled']:  # a switched-off pipeline reads no thread, so it reopens nothing to listen
            self._reopen_after_failed_redraft(rt, db, now)
        done = self._recover_applying(rt, db, db_path, cfg, now)
        for row in db.execute("SELECT * FROM promote_cards WHERE state='open' AND card_event IS NOT NULL").fetchall():
            try:
                item = self._settle_click(rt, db, db_path, row, cfg, now)
            except Exception as exc:  # noqa: BLE001 - P38 (F55): an unclaimed row retries next settle, the rest go on
                rt._meta_error(db, 'feedback_promote_settle_error', exc)
                continue
            if item is not None:
                done.append(item)
        return done

    def _settle_click(self, rt, db, db_path, row, cfg, now):
        d = rt.rules._decision(rt, db, row['card_event'])
        if d is None or d['actor'] != self.owner_user(rt):
            return None
        choice, suppress, state = d['option'], None, 'resolved'
        applying = choice == 'apply' and not row['smoke']
        if choice == 'redraft':
            state = 'awaiting_guidance'   # keep polling the thread; settle_redraft re-dispatches on the owner's guidance
        elif choice == 'drop':
            suppress = now + cfg['suppress_days'] * 86400
        # P38 (F55): claim first — only the settle whose conditional UPDATE moves the row off `open` acts on the click.
        with rt.transaction(db):
            claimed = db.execute('UPDATE promote_cards SET state=?,choice=?,dispatch_id=NULL,suppress_until=?,decided_by=?,'
                                 "decided_at=?,error=NULL,updated_at=? WHERE id=? AND state='open'",
                                 ('applying' if applying else state, choice, suppress, d['actor'], d['decided_at'], now,
                                  row['id'])).rowcount
        if not claimed:
            return None
        dispatch_id, error = None, None
        if applying:
            dispatch_id, error, choice = self._apply_claimed(rt, db, db_path, row, now)
        _promote_thread_reply(rt, db, row['card_event'], row['id'], row['smoke'], self._click_reply(choice, error, cfg))
        return {'promote_id': row['id'], 'choice': choice, 'dispatch_id': dispatch_id, 'shape': row['shape'],
                'state': state}

    def _click_reply(self, choice, error, cfg):
        return self.promote_reply.get(choice, self.msg('learning._click_reply.1') + (error or '') + self.msg('learning._click_reply.2')).format(
            days=cfg['suppress_days'])

    def _apply_claimed(self, rt, db, db_path, row, now, dispatch_id=None):
        """Launch the formalize dispatch of an `applying` row (unless `dispatch_id`: it already launched), then finalize it
        `resolved`. Returns (dispatch_id, error, choice)."""
        error, choice = None, 'apply'
        if dispatch_id is None:
            record = db.execute('SELECT * FROM interaction_records WHERE id=?', (row['record_id'],)).fetchone()
            draft = json.loads(row['draft']) if row['draft'] else {}
            try:
                dispatch_id = self._late('_dispatch_apply')(rt, db, db_path, record, draft, row['id'], now)
            except Exception as exc:  # noqa: BLE001 - recorded; the owner can launch by hand
                dispatch_id = _launched_apply(db, row['id'])  # raised after its insert committed: it did launch
                if dispatch_id is None:
                    error, choice = (str(exc) if isinstance(exc, ValueError) else rt.safe_error(exc))[:300], 'apply_failed'
        with rt.transaction(db):
            db.execute("UPDATE promote_cards SET state='resolved',choice=?,dispatch_id=?,error=?,updated_at=? WHERE id=? "
                       "AND state='applying'", (choice, dispatch_id, error, now, row['id']))
        return dispatch_id, error, choice

    def _recover_applying(self, rt, db, db_path, cfg, now):
        """P38 (F55): an `applying` row older than APPLY_CLAIM_STALE_SECONDS was claimed by a settle that died or hit a DB
        lock before finalizing. Its apply dispatch row (reason `feedback_apply:<id>`) is the commit point of the launch:
        if it exists the launch happened — finalize with it, never launch again; if not, nothing was launched — launch now.
        The stale claim is re-stamped by a conditional UPDATE first, so two recoverers never both act."""
        done = []
        for row in db.execute("SELECT * FROM promote_cards WHERE state='applying' AND updated_at<?",
                              (now - APPLY_CLAIM_STALE_SECONDS,)).fetchall():
            try:
                with rt.transaction(db):
                    taken = db.execute("UPDATE promote_cards SET updated_at=? WHERE id=? AND state='applying' AND "
                                       'updated_at=?', (now, row['id'], row['updated_at'])).rowcount
                if not taken:
                    continue
                dispatch_id, error, choice = self._apply_claimed(rt, db, db_path, row, now, _launched_apply(db, row['id']))
                _promote_thread_reply(rt, db, row['card_event'], row['id'], row['smoke'], self._click_reply(choice, error, cfg))
                done.append({'promote_id': row['id'], 'choice': choice, 'dispatch_id': dispatch_id, 'shape': row['shape'],
                             'state': 'resolved', 'recovered': True})
            except Exception as exc:  # noqa: BLE001 - retried once the re-stamped claim is stale again
                rt._meta_error(db, 'feedback_promote_settle_error', exc)
        return done

    def _posted_card(self, rt, db, child):
        """P38 (F43): the redraft's rule card when it reached Slack although its row never went `open` (its rule-draft
        raised or died after the post): its own link, else the rule card no promote row owns that was stamped with that
        post's time (supervisor_card's `created` = the `now` of the row's last update). (event_id, channel, thread_ts)."""
        sql = ('SELECT c.event_id, c.channel, x.thread_ts FROM escalation_cards c JOIN escalations x ON '
               'x.event_id=c.event_id WHERE x.thread_ts IS NOT NULL AND c.dry=0 AND ')
        if child['card_event']:
            return db.execute(sql + 'c.event_id=?', (child['card_event'],)).fetchone()
        options = json.dumps(rt.buttons.validate_options(self.promote_options), ensure_ascii=False)
        return db.execute(sql + 'c.created=? AND c.options=? AND NOT EXISTS (SELECT 1 FROM promote_cards p WHERE '
                          'p.card_event=c.event_id) ORDER BY c.rowid LIMIT 1', (child['updated_at'], options)).fetchone()

    def _fell_through(self, rt, db, child, now):
        """P38 (F43): why the newest redraft of a `redrafted` card will not post its card (a REDRAFT_FELL_THROUGH key),
        'posted' when its card reached Slack after all, or None while it may still arrive. Every crash point of the chain
        lands here: a `drafting` / `posting` redraft whose dispatch never launched or is no longer waiting / running is
        reported REDRAFT_ENDED_GRACE_SECONDS after that, not left for the 6 h reaper."""
        state = child['state']
        if state in ('no_rule', 'draft_expired'):
            return state
        if state not in ('drafting', 'posting', 'draft_failed'):
            return None
        d = _redraft_dispatch(db, child['id'], child['dispatch_id'])
        if d is not None and d['state'] in (*rt.dispatch.ACTIVE, 'paused', f'needs_{self.key}'):
            return None  # its drafting session may still (re)try rule-draft (a paused / asking one resumes later)
        if state != 'draft_failed':
            ended = (d['ended_at'] or d['updated_at']) if d is not None else child['created_at']
            if now - ended < REDRAFT_ENDED_GRACE_SECONDS:
                return None
        if state != 'drafting' and self._posted_card(rt, db, child) is not None:
            return 'posted'
        if state == 'drafting':
            return 'draft_ended' if d is not None else 'not_started'
        return 'draft_failed'

    def _reopen_hint(self, db, card_event):
        """The 'this card still counts' tail of a P38 (F43) notice; the buttons are offered only while the card has them."""
        if db.execute('SELECT 1 FROM decisions WHERE event_id=?', (card_event,)).fetchone():
            return self.msg('learning._reopen_hint.1')
        return self.msg('learning._reopen_hint.2')

    def _no_rule_reason(self, rt, child):
        """The drafting session's one-line no-rule reason, quoted only when secret-free and within the copy standard."""
        reason = ' '.join(str(child['error'] or '').split())
        if child['state'] != 'no_rule' or not reason or reason == 'no_coherent_rule':
            return ''
        try:
            rt.asks.check_secret_free(reason)
        except Exception:  # noqa: BLE001
            return ''
        return '' if rt.buttons.copy_problems(reason, None) else self.msg('learning._no_rule_reason.1', excerpt=rt.buttons.excerpt(reason, 60))

    def _reopen_after_failed_redraft(self, rt, db, now):
        """P38 (F43): the owner was promised a redrafted card. When the newest redraft of a `redrafted` card came back no-rule,
        expired (the 6 h reaper), failed to post, or its drafting session ended / never started without a card
        (_fell_through), the discussion goes back to the card he replied on: reopen it and say so in its thread —
        `awaiting_guidance` when he had clicked [我改一下] (its buttons are gone), else `open` (its buttons work, and a click
        he made on it meanwhile settles normally — then the notice invites no reply, that click retires the card in this
        same pass). The thread cursor moves to the notice, so only what he types after it is new guidance. A redraft whose
        card reached Slack although its row never went `open` is made the live card instead (no notice)."""
        meta = db.execute('SELECT value FROM runtime_meta WHERE key=?', (REOPEN_SINCE_KEY,)).fetchone()
        since = float(meta[0]) if meta and _TS_RE.fullmatch(str(meta[0])) else now
        out = []
        for parent in db.execute("SELECT * FROM promote_cards WHERE state='redrafted' AND smoke=0 AND "
                                 'card_event IS NOT NULL').fetchall():
            try:
                child = _newest_redraft(db, parent['id'])
                if child is None or child['created_at'] < since:
                    continue
                because = self._fell_through(rt, db, child, now)
                if because is None:
                    continue
                if because == 'posted':
                    card = self._posted_card(rt, db, child)
                    with rt.transaction(db):
                        db.execute("UPDATE promote_cards SET state='open',card_event=?,card_channel=?,card_ts=?,error=NULL,"
                                   "updated_at=? WHERE id=? AND state IN ('posting','draft_failed')",
                                   (card['event_id'], card['channel'], card['thread_ts'], now, child['id']))
                        # The post landed but supervisor_card died before its last write: make the buttons decidable.
                        db.execute("UPDATE escalations SET stage=? WHERE event_id=? AND stage IN ('sending','sent')",
                                   (rt.SUPERVISOR_STAGE, card['event_id']))
                    out.append({'promote_id': parent['id'], 'redraft_pid': child['id'], 'kept': 'posted'})
                    continue
                d = rt.rules._decision(rt, db, parent['card_event'])
                state = 'awaiting_guidance' if d is not None and d['option'] == 'redraft' else 'open'
                with rt.transaction(db):
                    reopened = db.execute("UPDATE promote_cards SET state=?,reopened_at=?,updated_at=? WHERE id=? AND "
                                          "state='redrafted'", (state, now, now, parent['id'])).rowcount
                if not reopened:
                    continue
                text = self.redraft_fell_through[because].format(reason=self._no_rule_reason(rt, child))
                if d is None or d['option'] == 'redraft' or d['actor'] != self.owner_user(rt):
                    text += self._reopen_hint(db, parent['card_event'])
                ts = _promote_thread_reply(rt, db, parent['card_event'], parent['id'], parent['smoke'], text)
                notice = ts if ts and _TS_RE.fullmatch(str(ts)) else f'{now:.6f}'  # Slack's clock when it posted
                cursor = max([c for c in (parent['redraft_cursor'], notice) if c and _TS_RE.fullmatch(str(c))], key=float)
                with rt.transaction(db):
                    db.execute('UPDATE promote_cards SET redraft_cursor=? WHERE id=?', (cursor, parent['id']))
                out.append({'promote_id': parent['id'], 'reopened': state, 'redraft_pid': child['id'], 'because': because})
            except Exception as exc:  # noqa: BLE001 - one bad chain never blocks the settle
                rt._meta_error(db, 'feedback_redraft_reopen_error', exc)
        return out

    def _redraft_guidance(self, text):
        """the owner's thread reply minus mentions and a bare "我改一下" lead-in; empty when there is no actual steer yet."""
        body = ' '.join(_MENTION_RE.sub(' ', str(text or '')).split())
        body = self._redraft_prefix_re.sub('', body).strip()
        return body if len(body) >= 2 else ''

    def settle_redraft(self, rt, db, db_path=None, now=None):
        """Supervisor settle step: poll the thread of each live rule card (open / awaiting guidance). the owner's free-text reply
        there means "redraft with this guidance" — re-dispatch <owner key>-inbox to redraft and supersede the card. Terminal
        actions (就按这条固化 / 只记这次 / 先不用) stay button-only. Reads Slack, bounded to live non-dry rule-card threads.

        P38 (F38): one blocking user-token read per live rule card on every 10 s settle starved intake and watches of the
        shared token. A thread is now polled on the code-owned card ladder (card_replies.CODE_POLL, by age since the card
        was posted / the owner clicked [我改一下] / it was reopened), throttled by `redraft_polled_at`, and not at all once that
        age passes card_replies.CODE_CARD_DAYS (its buttons still work)."""
        now = time.time() if now is None else now
        db_path = db_path or _db_path(db)
        cfg = config(rt)
        if not cfg['enabled']:
            return []
        out = []
        rows = db.execute(
            'SELECT p.*, c.channel AS ch, c.thread_root, c.created AS card_created, x.thread_ts FROM promote_cards p '
            'JOIN escalation_cards c ON c.event_id=p.card_event JOIN escalations x ON x.event_id=p.card_event '
            "WHERE p.state IN ('open','awaiting_guidance') AND p.smoke=0 AND c.dry=0 AND x.thread_ts IS NOT NULL").fetchall()
        for row in rows:
            age = now - max(row['card_created'] or 0, row['decided_at'] or 0, row['reopened_at'] or 0)
            if age > rt.card_replies.CODE_CARD_DAYS * 86400:
                continue
            if row['redraft_polled_at'] and now - row['redraft_polled_at'] < rt.card_replies._poll_every(age):
                continue
            try:
                out.extend(self._poll_redraft(rt, db, db_path, row, cfg, now))
            except Exception as exc:  # noqa: BLE001 - polled again on the ladder
                rt._meta_error(db, 'feedback_redraft_error', exc)
        return out

    def _poll_redraft(self, rt, db, db_path, row, cfg, now):
        channel, card_ts = row['ch'], row['thread_ts']
        cursor = row['redraft_cursor'] or card_ts
        # P38 (F38): stamp the poll before the read, so a failing read (429 / timeout) also waits for the ladder.
        with rt.transaction(db):
            db.execute('UPDATE promote_cards SET redraft_polled_at=?,updated_at=? WHERE id=?', (now, now, row['id']))
        found = rt.slack.api_call('conversations.replies', rt.api_identity(), params={
            'channel': channel, 'ts': card_ts, 'oldest': cursor, 'limit': 50, 'include_all_metadata': 'true'})
        msgs = sorted((m for m in found.get('messages', []) if _TS_RE.fullmatch(str(m.get('ts') or '')) and
                       float(m['ts']) > float(cursor)), key=lambda m: float(m['ts']))
        out = []
        for m in msgs:
            typed = getattr(rt.card_replies, f'{self.key}_typed')(rt, db, channel, m, card_ts)
            guidance = self._redraft_guidance(m.get('text')) if typed else ''
            if guidance:
                # P38 (F43): the cursor passes his guidance in the transaction that chains the redraft (_do_redraft), so a
                # DB lock / crash before that re-reads this reply on the next poll instead of dropping it.
                out.append(self._do_redraft(rt, db, db_path, row, m, guidance, now))
                break  # one redraft per card per cycle; the new card takes over the discussion
            with rt.transaction(db):
                db.execute('UPDATE promote_cards SET redraft_cursor=?,redraft_polled_at=?,updated_at=? WHERE id=?',
                           (m['ts'], now, now, row['id']))
            if typed and row['state'] != 'awaiting_guidance':
                # a bare "我改一下" with no specifics yet: prompt once and keep waiting — P38 (F38): like a [我改一下] click,
                # it restarts the thread-poll ladder (reopened_at), so the specifics he types next are read promptly
                _promote_thread_reply(rt, db, row['card_event'], row['id'], row['smoke'], self.promote_reply['redraft'])
                with rt.transaction(db):
                    db.execute("UPDATE promote_cards SET state='awaiting_guidance',reopened_at=?,updated_at=? WHERE id=?",
                               (now, now, row['id']))
        return out

    def _do_redraft(self, rt, db, db_path, row, m, guidance, now):
        """Chain a new `drafting` card from this one and re-dispatch <owner key>-inbox to redraft with the owner's guidance; retire this
        card. P38 (F60): the owner's guidance goes ONLY to the private corpus (keyed by the new card); the re-dispatch brief
        carries the new card's id and the drafting session reads the guidance with `learning-review --promote`. P38 (F43):
        the redraft is chained, this card retired (`redrafted`) and the thread cursor moved past his reply in ONE
        transaction BEFORE the launch: a crash / DB lock before it re-reads the reply next poll; anywhere after it leaves a
        `drafting` redraft that either drafts or is reported (_reopen_after_failed_redraft). When the redraft cannot start
        (the launch failed, or the corpus write did), this card goes back to what it was and says so."""
        record = db.execute('SELECT * FROM interaction_records WHERE id=?', (row['record_id'],)).fetchone()
        draft = json.loads(row['draft']) if row['draft'] else {}
        pid = 'pc' + uuid.uuid4().hex[:14]
        pointer = None
        try:
            pointer = self._late('store_feedback')(rt, row['shape'], pid, 'redraft: ' + guidance, float(m['ts']))
        except Exception as exc:  # noqa: BLE001 - then the redraft cannot start (below); never a copy in the brief
            rt._meta_error(db, 'feedback_store_error', exc)
        prompt = self.redraft_prompt.format(cli=_cli(rt, db_path), shape=row['shape'], pid=pid,
                                       scenario=draft.get('scenario', self.msg('learning._do_redraft.1')), rule=draft.get('rule', self.msg('learning._do_redraft.2')))
        dispatch_id, error = None, (None if pointer else 'guidance_not_stored')
        with rt.transaction(db):
            prev = db.execute('SELECT state,choice,guidance_pointer,decided_by,decided_at FROM promote_cards WHERE id=?',
                              (row['id'],)).fetchone()
            db.execute('INSERT INTO promote_cards (id,record_id,shape,signal,state,error,created_at,updated_at,smoke,'
                       'redraft_of,guidance_pointer) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (pid, row['record_id'], row['shape'], row['signal'], 'draft_failed' if error else 'drafting', error,
                        now, now, row['smoke'], row['id'], pointer))
            db.execute('UPDATE promote_cards SET redraft_cursor=?,updated_at=? WHERE id=?', (m['ts'], now, row['id']))
            if error is None:
                # the old card records the guidance it received (pointer only) and that it was superseded by the redraft
                db.execute("UPDATE promote_cards SET state='redrafted',choice='redraft',guidance_pointer=?,decided_by=?,"
                           'decided_at=? WHERE id=?', (pointer, self.owner_user(rt), float(m['ts']), row['id']))
        if error is None:
            try:
                dispatch_id = self._late('_redispatch')(rt, db, db_path, record, prompt, 'feedback_redraft:' + pid, now)
            except Exception as exc:  # noqa: BLE001 - recorded on the new card; this card goes back (below)
                launched = _redraft_dispatch(db, pid)  # raised after its insert committed: it did launch
                if launched is not None:
                    dispatch_id = launched['dispatch_id']
                else:
                    error = (str(exc) if isinstance(exc, ValueError) else rt.safe_error(exc))[:300]
                    with rt.transaction(db):
                        db.execute("UPDATE promote_cards SET state='draft_failed',error=?,updated_at=? WHERE id=?",
                                   (error, now, pid))
                        db.execute('UPDATE promote_cards SET state=?,choice=?,guidance_pointer=?,decided_by=?,decided_at=?,'
                                   "updated_at=? WHERE id=? AND state='redrafted'", (*prev, now, row['id']))
        if error:
            # P38 (F43): the redraft never started. This card stays as it was (its buttons / thread still count) and tells
            # the owner, instead of retiring it behind a promised card that never comes; his next reply here retries.
            _promote_thread_reply(rt, db, row['card_event'], row['id'], row['smoke'],
                                  self.redraft_not_started + self._reopen_hint(db, row['card_event']))
            return {'promote_id': row['id'], 'choice': 'redraft_failed', 'redraft_pid': pid, 'dispatch_id': None,
                    'error': error, 'shape': row['shape']}
        try:
            with rt.transaction(db):
                db.execute('UPDATE promote_cards SET dispatch_id=?,updated_at=? WHERE id=?', (dispatch_id, now, pid))
        except Exception as exc:  # noqa: BLE001 - P38 (F43): found by its reason (_redraft_dispatch); the owner still hears
            rt._meta_error(db, 'feedback_redraft_error', exc)
        # P38 (F43): no "马上来" promise — if the redraft falls through, _reopen_after_failed_redraft says so here.
        _promote_thread_reply(rt, db, row['card_event'], row['id'], row['smoke'],
                              self.msg('learning._do_redraft.3', excerpt=rt.buttons.excerpt(guidance)))
        return {'promote_id': row['id'], 'choice': 'redraft', 'redraft_pid': pid, 'dispatch_id': dispatch_id,
                'shape': row['shape']}

    def distill(self, rt, db, db_path, event_id, intent, directive=None, feedback_file=None, semantic=None, now=None):
        """A handling session fills the structured distillation of the owner's intent (and, optionally, redacts his raw reply
        into the private store) for the interaction it just handled, then strong-signal is re-evaluated. Raw text is passed
        by FILE, never on argv, and never stored in the DB. P37: `--scenario` sets the SEMANTIC request category (the
        what-to-do class) that the promote card / dashboard show instead of the internal card-type shape."""
        now = time.time() if now is None else now
        intent = ' '.join(str(intent or '').split())
        if not intent or len(intent) > 200:
            raise ValueError('intent_must_be_1_to_200_characters')
        rt.asks.check_secret_free(intent)
        semantic = ' '.join(str(semantic or '').split())[:120] or None
        if semantic:
            rt.asks.check_secret_free(semantic)
        actor = '%s:%s' % rt.claude.actor() if rt.claude.actor()[1] else 'unknown'
        row = db.execute('SELECT ir.* FROM interaction_records ir WHERE ir.event_id=?', (event_id,)).fetchone()
        if row is None:  # capture on demand so a session can distill in the same breath as handling the reply
            dec = db.execute('SELECT d.event_id, d.choice, d.actor, d.note, d.provenance, d.decided_at, d.source_ts, '
                             'c.options, c.smoke, c.task_id FROM decisions d JOIN escalation_cards c ON c.event_id=d.event_id '
                             'WHERE d.event_id=? AND d.actor=?', (event_id, self.owner_user(rt))).fetchone()
            if dec is None:
                raise ValueError(f'no_{self.key}_decision_for_this_event')
            self.capture_one(rt, db, dec, now)
            row = db.execute('SELECT ir.* FROM interaction_records ir WHERE ir.event_id=?', (event_id,)).fetchone()
        pointer = row['feedback_pointer']
        if feedback_file:
            text = Path(feedback_file).read_text(encoding='utf-8')[:4000]
            if directive is None:
                directive = bool(self.directive_re.search(text))
            pointer = self._late('store_feedback')(rt, row['shape'], row['id'], text, row['decided_at']) or pointer
        new_directive = int(bool(directive)) if directive is not None else row['directive']
        new_semantic = semantic or _get(row, 'semantic_shape')
        with rt.transaction(db):
            db.execute('UPDATE interaction_records SET distillation=?,distilled_by=?,directive=?,has_feedback=?,'
                       'feedback_pointer=?,semantic_shape=?,updated_at=? WHERE id=?',
                       (intent, actor, new_directive, int(bool(pointer)), pointer, new_semantic, now, row['id']))
        record = db.execute('SELECT * FROM interaction_records WHERE id=?', (row['id'],)).fetchone()
        promoted = None
        if not record['smoke'] and record['promoted'] is None and now - record['decided_at'] <= PROMOTE_FRESH_SECONDS:
            promoted = self.maybe_promote(rt, db, dict(record), config(rt), now, db_path=db_path)
            if promoted:
                rt.start(db_path)
        return {'ok': True, 'record': row['id'], 'event_id': event_id, 'directive': bool(new_directive),
                'promoted': promoted['promote_id'] if promoted else None}

    def _week_start(self, ts):
        day = dt.datetime.fromtimestamp(ts, self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return (day - dt.timedelta(days=day.weekday())).timestamp()

    def review_interactions(self, rt, db, start, now):
        """Weekly review input (folded into rules-review-data): same-shape volume patterns and [只记这次] items to
        consolidate into proposals. Aggregates and ids only — never raw feedback or the distillation text."""
        rows = [dict(r) for r in db.execute('SELECT * FROM interaction_records WHERE decided_at>=? AND smoke=0 ORDER BY '
                                            'decided_at', (start,))]
        by_shape = {}
        for r in rows:
            s = by_shape.setdefault(r['shape'], {'shape': r['shape'], 'label': self.shape_label(r), 'card_type': r['card_type'],
                                                 'system': r['system'], 'category': r['category'], 'agent_action': r['agent_action'],
                                                 'count': 0, 'overrides': 0, 'directive': 0, 'choices': {}, 'record_ids': []})
            s['count'] += 1
            s['overrides'] += int(bool(r['override']))
            s['directive'] += int(bool(r['directive']))
            s['choices'][r['choice']] = s['choices'].get(r['choice'], 0) + 1
            if len(s['record_ids']) < 20:
                s['record_ids'].append(r['id'])
        # A volume pattern: >=N same-shape interactions handled the same way (one dominant choice).
        volume = []
        for s in by_shape.values():
            top = max(s['choices'].values()) if s['choices'] else 0
            if s['count'] >= VOLUME_THRESHOLD and top >= VOLUME_THRESHOLD:
                promoted = db.execute("SELECT count(*) FROM promote_cards WHERE shape=? AND state='resolved'", (s['shape'],)).fetchone()[0]
                volume.append({**{k: s[k] for k in ('shape', 'label', 'card_type', 'system', 'category', 'agent_action',
                                                    'count', 'overrides', 'record_ids')},
                               'dominant_choice': max(s['choices'], key=s['choices'].get), 'dominant_count': top,
                               'already_carded': promoted})
        logged = [{'promote_id': r['id'], 'shape': r['shape'], 'label': self._loads_label(db, r['record_id'])}
                  for r in db.execute("SELECT * FROM promote_cards WHERE choice='log' AND created_at>=? AND smoke=0",
                                      (start,)).fetchall()]
        return {'total': len(rows), 'shapes': len(by_shape),
                'overrides': sum(int(bool(r['override'])) for r in rows),
                'volume_patterns': sorted(volume, key=lambda x: -x['count']), 'logged_only': logged,
                'note': 'aggregates + ids only; raw feedback is in the private corpus; propose via rule-propose'}

    def _loads_label(self, db, record_id):
        r = db.execute('SELECT * FROM interaction_records WHERE id=?', (record_id,)).fetchone()
        return self.shape_label(r) if r else '?'

    def shape_history(self, rt, db, shape):
        """Same-shape history for a distillation session: aggregates, record ids, distillations and PRIVATE pointers (paths
        only). Never the raw feedback text — that is read from the private corpus by the session if it needs context."""
        rows = [dict(r) for r in db.execute('SELECT * FROM interaction_records WHERE shape=? AND smoke=0 ORDER BY decided_at '
                                            'DESC LIMIT 100', (shape,))]
        if not rows:
            return {'ok': True, 'shape': shape, 'count': 0, 'records': [], 'note': 'no same-shape interactions recorded'}
        choices, overrides, directive = {}, 0, 0
        records = []
        for r in rows:
            choices[r['choice']] = choices.get(r['choice'], 0) + 1
            overrides += int(bool(r['override']))
            directive += int(bool(r['directive']))
            records.append({'id': r['id'], 'decided_at': self.iso_local(r['decided_at']), 'choice': r['choice'],
                            'override': bool(r['override']), 'directive': bool(r['directive']),
                            'distillation': r['distillation'], 'feedback_pointer': r['feedback_pointer']})
        return {'ok': True, 'shape': shape, 'label': self.shape_label(rows[0]), 'card_type': rows[0]['card_type'],
                'system': rows[0]['system'], 'category': rows[0]['category'], 'agent_action': rows[0]['agent_action'],
                'count': len(rows), 'overrides': overrides, 'directive': directive, 'choices': choices,
                'corpus_dir': str(self.corpus_dir()), 'records': records,
                'note': 'raw feedback is only in corpus_dir (redacted); the DB keeps the distillation + pointer'}

    def promote_brief(self, rt, db, promote_id):
        """P38 (F60) `learning-review --promote <pc…>`: what a drafting session needs about one rule-distillation card,
        read on demand instead of carried in its brief (a brief lands in dispatches.prompt, prompt.md and the `claude -p`
        argv): the draft it redrafts and the owner's redraft guidance, read back from the private corpus (secret-redacted when
        stored). Printed to the calling session — so, like any tool output, it is also in that session's own run log /
        transcript; this command itself writes nothing."""
        row = db.execute('SELECT * FROM promote_cards WHERE id=?', (promote_id,)).fetchone()
        if row is None:
            raise ValueError('unknown_promote_card:' + str(promote_id))
        parent = (db.execute('SELECT draft FROM promote_cards WHERE id=?', (row['redraft_of'],)).fetchone()
                  if row['redraft_of'] else None)
        return {'ok': True, 'promote_id': row['id'], 'state': row['state'], 'shape': row['shape'],
                'label': self._loads_label(db, row['record_id']), 'redraft_of': row['redraft_of'],
                'previous_draft': json.loads(parent['draft']) if parent is not None and parent['draft'] else None,
                'guidance': [re.sub(r'^redraft:\s*', '', g) for g in self.read_feedback(row['guidance_pointer'])],
                'note': f"guidance = {self.name}'s own words from the private corpus (redacted): redraft from it, never quote it verbatim in your messages / final reply, a file, the DB or another brief"}

    def iso_local(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz).isoformat()

    def aggregate(self, db, now, weeks=LEARN_WEEKS):
        """Read-only dashboard aggregate (/learning). Counts by shape, override rate, promote cards, adoption. No raw
        text, no distillation text, no card text — safe shape labels only."""
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'interaction_records' not in tables:
            return {'generated_at': now, 'notice': 'learning_tables_missing', 'weeks': [], 'shapes': [], 'promotes': [],
                    'counts': {}}
        rows = [dict(r) for r in db.execute('SELECT * FROM interaction_records WHERE smoke=0')]
        first = self._week_start(now) - (weeks - 1) * 7 * 86400
        buckets = {first + n * 7 * 86400: {'records': 0, 'overrides': 0, 'directive': 0, 'promotes': 0, 'distill': 0}
                   for n in range(weeks)}
        by_type, by_shape = {}, {}
        for r in rows:
            by_type[r['card_type']] = by_type.get(r['card_type'], 0) + 1
            s = by_shape.setdefault(r['shape'], {'shape': r['shape'], 'label': self.shape_label(r), 'card_type': r['card_type'],
                                                 'count': 0, 'overrides': 0, 'directive': 0, 'last_at': 0})
            s['count'] += 1
            s['overrides'] += int(bool(r['override']))
            s['directive'] += int(bool(r['directive']))
            s['last_at'] = max(s['last_at'], r['decided_at'])
            b = buckets.get(self._week_start(r['decided_at']))
            if b is not None:
                b['records'] += 1
                b['overrides'] += int(bool(r['override']))
                b['directive'] += int(bool(r['directive']))
        promotes = [dict(p) for p in db.execute('SELECT * FROM promote_cards WHERE smoke=0 ORDER BY created_at DESC')]
        # P37: a promote card is first `drafting` (no Slack card yet), then `open` -> apply / redraft / log / drop, or
        # `no_rule` (the inbox could not articulate a rule). `distill` is the legacy p35 adopt choice, folded into `apply`.
        pcount = {'posted': 0, 'carded': 0, 'drafting': 0, 'apply': 0, 'redraft': 0, 'log': 0, 'drop': 0, 'no_rule': 0,
                  'open': 0, 'by_signal': {}}
        shape_to_label = {s['shape']: s['label'] for s in by_shape.values()}
        for p in promotes:
            pcount['posted'] += 1
            pcount['by_signal'][p['signal']] = pcount['by_signal'].get(p['signal'], 0) + 1
            if p['card_event']:
                pcount['carded'] += 1
            if p['state'] in ('drafting', 'posting'):
                pcount['drafting'] += 1
            elif p['state'] in ('open', 'awaiting_guidance'):
                pcount['open'] += 1
            choice = 'apply' if p['choice'] == 'distill' else p['choice']
            if choice in ('apply', 'redraft', 'log', 'drop', 'no_rule'):
                pcount[choice] += 1
            b = buckets.get(self._week_start(p['created_at']))
            if b is not None:
                b['promotes'] += 1
                b['distill'] += int(choice == 'apply')
        pcount['distill'] = pcount['apply']  # legacy alias kept for the /learning dashboard template
        overrides = sum(int(bool(r['override'])) for r in rows)
        decided = pcount['apply'] + pcount['log'] + pcount['drop']
        counts = {'total': len(rows), 'overrides': overrides,
                  'override_rate': round(overrides / len(rows), 4) if rows else None,
                  'with_feedback': sum(int(bool(r['has_feedback'])) for r in rows),
                  'distilled': sum(int(bool(r['distilled_by'])) for r in rows), 'by_card_type': by_type,
                  'promote': pcount, 'adoption_rate': round(pcount['apply'] / decided, 4) if decided else None}
        recent_promotes = [{'id': p['id'], 'shape': p['shape'], 'label': shape_to_label.get(p['shape'], p['shape']),
                            'signal': p['signal'], 'state': p['state'], 'choice': p['choice'], 'created_at': p['created_at']}
                           for p in promotes[:30]]
        top_shapes = sorted(by_shape.values(), key=lambda s: (-s['count'], -s['overrides']))[:30]
        week_list = [{'start': s, **b} for s, b in sorted(buckets.items())]
        return {'generated_at': now, 'counts': counts, 'weeks': week_list, 'shapes': top_shapes,
                'promotes': recent_promotes}

    def smoke(self, rt, db, db_path, provider=None, session=None, card=False, now=None):
        """TEST the P37 flow end to end, dry by default (no real Slack): a synthetic strong-signal interaction -> a drafting
        promote card -> `rule_draft` posts a rich TEST card (场景 / 现在怎么处理 vs 你的做法 / 建议的规则 / 会影响什么 + 4
        buttons) -> a synthetic redraft -> a second rich TEST card -> the the owner-gated apply reuses the TEST rule proposal in a
        temp rules dir. The real rules dir, config and workspaces are untouched. `card` posts a REAL TEST card to click."""
        now = time.time() if now is None else now
        provider = provider or rt.claude.actor()[0] or 'claude'
        session = session or rt.claude.actor()[1]
        # A synthetic captured record carrying both an override and directive language (strong signal (a)+(b)).
        rid = 'ir' + uuid.uuid4().hex[:14]
        shape = shape_key('rule_check', 'infra', f'rule_check:{self.key}')
        with rt.transaction(db):
            db.execute('INSERT INTO interaction_records (id,event_id,card_type,task_id,category,system,agent_action,options,'
                       'recommended,choice,choice_kind,provenance,override,directive,has_feedback,distillation,shape,'
                       'semantic_shape,decided_by,decided_at,smoke,created_at,updated_at) VALUES '
                       "(?,?,?,?,?,?,?,?,?,?,?,?,1,1,0,?,?,?,?,?,1,?,?)",
                       (rid, 'smoke' + uuid.uuid4().hex[:18], 'rule_check', None, 'infra', self.msg('learning.smoke.1'), f'rule_check:{self.key}',
                        '["approve","reject"]', '(approve)', 'reject', 'reject', 'thread_reply',
                        self.msg('learning.smoke.2'), shape, self.smoke_draft['title'], self.owner_user(rt), now, now, now))
        record = dict(db.execute('SELECT * FROM interaction_records WHERE id=?', (rid,)).fetchone())
        first = _smoke_drafting(rt, db, record, shape, now)
        d1 = self.rule_draft(rt, db, db_path, first, fields=self.smoke_draft, basis_count=1, live_card=card, now=now)
        second = _smoke_drafting(rt, db, record, shape, now, redraft_of=first)
        adjusted = {**self.smoke_draft, 'rule': self.smoke_draft['rule'] + self.msg('learning.smoke.3', smoke_scope=self.smoke_scope)}
        d2 = self.rule_draft(rt, db, db_path, second, fields=adjusted, basis_count=1, live_card=card, now=now)
        result = {'ok': False, 'record': rid, 'promote_id': first, 'redraft_pid': second,
                  'card_event': d1['card']['event_id'], 'redraft_card_event': d2['card']['event_id'], 'shape': shape,
                  'buttons': [o.split('=', 1)[1].split(':', 1)[0] for o in self.promote_options],
                  'blocks': rt.buttons.rule_card_blocks(self.smoke_draft)}
        if card:
            result['note'] = (self.msg('learning.smoke.4'))
            result['ok'] = bool(d1['carded'] and d2['carded'])
            return result
        result['proposal'] = _smoke_apply(rt, db, db_path, session, provider, now)
        result['ok'] = bool(d1['carded'] and d2['carded'] and result['proposal'].get('ok'))
        return result
