"""Dispatch core (K12a): the stateless half of the dispatcher — failure classes from a run's error text and its
summary, the question a run ended on and a terminal tool's question as a card question with its options, the
run / failure / breaker card blocks, labels, the progress line, the session tag, run ids, file helpers, the
provider probe state and the waiting / stranded overviews.
"""
import datetime as dt
import json
import os
import re
import uuid

from kage import locale

STRIP_ENV_PREFIX = ('CLAUDE_CODE_',)


PROGRESS_BACKOFF_SECONDS = (60, 900)  # 429: Retry-After or 60s doubling up to 15 min


SESSION_ID_CHARS = 13  # UUIDv7 ids start with a timestamp: sessions started in the same second share 8 characters


WAIT_REASON = 'provider_wait'   # dispatches.wait_reason while a dispatch waits for a probe to see a provider back


PROBE_META = 'provider_probe'   # runtime_meta: {provider: {at, result, error_class, marker, fails, next_at, model, running}}


FAILURE_PATTERNS = (
    ('session_live', r'already has an active writer|session is already (?:running|in use)|is already running'),
    ('session_missing', r'no conversation found|no rollout found|thread[^\n]{0,40}not found|session[^\n]{0,20}not '
                        r'found|no saved session|unknown (?:thread|session)'),
    ('model_unsupported', r'not supported by any configured account'),  # the relay's exact 404 wording
    ('model_unsupported', r'model[^\n]{0,80}(?:does not exist|not found|is not supported|not available)|'
                          r'issue with the selected model|\b404\b[^\n]{0,60}model'),
    ('auth', r'\b40[13]\b|unauthori[sz]ed|forbidden|invalid[ _-]?api[ _-]?key|authentication[_ ](?:error|failed)|'
             r'please run /login|not logged in|oauth token|permission_error|token (?:has )?expired'),
    ('quota', r'usage limit|insufficient[_ ]quota|exceeded your current quota|quota|limit reached|'
              r"you've hit your|hit your (?:session|usage|weekly|daily) limit|credit balance|billing[_ ]error|"
              r'spend(?:ing)? limit|daily limit'),
    ('rate_limited', r'\b429\b|too many requests|rate[_ ]?limit'),
    ('transient', r'\b(?:500|502|503|504|529)\b|overloaded|service unavailable|bad gateway|gateway timeout|'
                  r'timed? ?out|timeout|econnreset|etimedout|enotfound|econnrefused|connection (?:reset|refused|'
                  r'closed|error)|network|stream (?:disconnected|error)|socket hang up|internal[_ ]server[_ ]error|'
                  r'api_error|temporarily unavailable'),
)


FAILURE_RES = tuple((name, re.compile(pattern, re.I)) for name, pattern in FAILURE_PATTERNS)


MCP_NOISE_RE = re.compile(r'rmcp::|\bmcp[_ -]?(?:client|server|oauth)\b|AuthRequired', re.I)


RELAY_STATUS_RE = re.compile(r'unexpected status (\d{3})', re.I)


RETRY_SAME = ('transient', 'rate_limited', 'unknown', 'crashed')


SWITCH_NOW = ('quota', 'session_missing', 'provider_degraded')


NO_SWITCH = ('auth', 'model_unsupported', 'spawn_failed', 'run_timeout')


CLAUDE_RETRY_CATEGORY = {'rate_limit': 'rate_limited', 'overloaded': 'transient', 'authentication_failed': 'auth',
                         'billing_error': 'quota'}


def classify_failure(text):
    """(error_class, marker). `marker` is the short matched token only, never the message. P46.3: lines from an MCP
    server are dropped first, and the relay's last status being a 5xx wins over `auth`."""
    text = '\n'.join(line for line in str(text or '').splitlines() if not MCP_NOISE_RE.search(line))
    relay_5xx = (RELAY_STATUS_RE.findall(text) or ['-'])[-1].startswith('5')
    for name, regex in FAILURE_RES:
        match = None if name == 'auth' and relay_5xx else regex.search(text)
        if match:
            return name, re.sub(r'\s+', ' ', match.group(0))[:40].lower()
    return 'unknown', None


def run_failure(info, exit_info):
    """(error_class, marker) of a CLI run that did not end cleanly (`parse_run_log` info): the patterns over its errors,
    else Claude's api_retry category, else `crashed` when it left no exit.json. Shared by `finalize` and the P44 probe."""
    error_class, marker = classify_failure('\n'.join(info['errors']))
    for category in info['retry_categories']:
        if error_class == 'unknown' and category in CLAUDE_RETRY_CATEGORY:
            error_class, marker = CLAUDE_RETRY_CATEGORY[category], category
    if exit_info is None and error_class == 'unknown':
        error_class = 'crashed'
    return error_class, marker


def _read_tail(path, limit=262144):
    try:
        with open(path, 'rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - limit))
            return stream.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def _read_head(path, limit=65536):
    try:
        with open(path, 'rb') as stream:
            return stream.read(limit).decode('utf-8', 'replace')
    except OSError:
        return ''


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec='seconds') if ts else None


def new_run_id(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('r%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:6]


PROVIDER_NAME = {'codex': 'Codex', 'claude': 'Claude'}


def _button(text, action, value, style=None):
    button = {'type': 'button', 'text': {'type': 'plain_text', 'text': text, 'emoji': True},
              'action_id': 'sf_dispatch:' + action, 'value': value}
    if style:
        button['style'] = style
    return button


def pair_key(a, b):
    return tuple(sorted((a, b)))


def _mrkdwn(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('`', "'")


def _plan_providers(plan):
    """P44: the providers a dispatch can run on (its plan's, in order) = the providers it waits for."""
    return list(dict.fromkeys(step['provider'] for step in json.loads(plan or '[]')))


def probe_state(db):
    """P44: the providers' probe state (runtime_meta PROBE_META): well-formed provider entries only, so a damaged
    value never freezes the waits."""
    row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (PROBE_META,)).fetchone()
    try:
        state = json.loads(row[0]) if row else {}
    except ValueError:
        return {}
    return {p: s for p, s in state.items() if p in PROVIDER_NAME and isinstance(s, dict) and
            isinstance(s.get('next_at') or 0, (int, float)) and isinstance(s.get('fails', 0), int)
            } if isinstance(state, dict) else {}


def progress_backoff(exc, failures):
    """(seconds, stage) after a failed chat.update; honours Retry-After on HTTP 429 / `ratelimited`."""
    low, high = PROGRESS_BACKOFF_SECONDS
    exponential = min(high, low * 2 ** min(int(failures), 4))
    if getattr(exc, 'code', None) == 429 or 'ratelimited' in str(exc):
        try:
            retry_after = float((getattr(exc, 'headers', None) or {}).get('Retry-After') or 0)
        except (TypeError, ValueError, AttributeError):
            retry_after = 0
        return min(high, max(exponential, retry_after)), 'rate_limited'
    return min(high, max(120, exponential)), 'failed'


def provider_wait_overview(db, now, limit=20):
    """P44 read model (dispatch-status, health, the dashboard): per provider the last probe (time, result, class,
    model), its consecutive failures, the next probe and how many dispatches wait for it; the waiting dispatches and the
    oldest wait. Ids, times and classes only."""
    probes = probe_state(db)
    columns = {r[1] for r in db.execute('PRAGMA table_info(dispatches)')}
    rows = [dict(r) for r in db.execute("SELECT dispatch_id, task_key, plan, wait_since, next_at FROM dispatches WHERE "
                                        "state='waiting' AND wait_reason=? ORDER BY wait_since", (WAIT_REASON,))
            ] if 'wait_reason' in columns else []
    providers = {}
    for provider in PROVIDER_NAME:
        p = probes.get(provider) or {}
        providers[provider] = {'last_probe_at': p.get('at'), 'result': p.get('result'), 'error_class': p.get('error_class'),
                               'model': p.get('model'), 'fails': p.get('fails', 0), 'next_probe_at': p.get('next_at'),
                               'probing': bool(p.get('running')),
                               'waiting': sum(1 for r in rows if provider in _plan_providers(r['plan']))}
    oldest = min((r['wait_since'] for r in rows if r['wait_since']), default=None)
    return {'providers': providers, 'waiting': len(rows), 'oldest_wait_since': oldest,
            'oldest_wait_hours': round((now - oldest) / 3600, 1) if oldest else None,
            'dispatches': [{'dispatch_id': r['dispatch_id'], 'task_key': r['task_key'],
                            'providers': _plan_providers(r['plan']), 'wait_since': r['wait_since'],
                            'next_probe_at': r['next_at']} for r in rows[:limit]]}


def stranded_overview(db, now, limit=20):
    """P38 C1 read model (dashboard / CLI): stranded-delivery counts and the latest rows. Ids and states only."""
    if 'stranded_deliveries' not in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return {'counts': {}, 'recent': []}
    counts = {r[0]: r[1] for r in db.execute('SELECT state, count(*) FROM stranded_deliveries WHERE updated_at>=? '
                                             'GROUP BY state', (now - 86400,))}
    recent = [dict(r) for r in db.execute('SELECT event_id, provider, status, task_key, state, reason, dispatch_id, '
                                          'first_seen, updated_at FROM stranded_deliveries ORDER BY updated_at DESC '
                                          'LIMIT ?', (limit,))]
    return {'counts': counts, 'recent': recent}


class DispatchCore:
    """The owner-bound half of the dispatch core: the owner's time zone and the card words, labels and questions
    in the owner's language."""

    def __init__(self, language, *, tz, tz_name, owner_key, owner_name, run_id_env, task_env, hops_env, run_context_env):
        self.language = language
        self.tz = tz
        self.tz_name = tz_name
        self.run_id_env = run_id_env
        self.task_env = task_env
        self.hops_env = hops_env
        self.run_context_env = run_context_env
        self.key, self.name = owner_key, owner_name
        self.strip_env_exact = ('CODEX_THREAD_ID', 'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_EFFORT', 'CLAUDE_ENV_FILE',
                                self.run_id_env, self.task_env, self.hops_env, self.run_context_env)
        self.error_labels = {
                 'transient': self.msg('dispatch.error_labels.transient'), 'rate_limited': self.msg('dispatch.error_labels.rate_limited'), 'quota': self.msg('dispatch.error_labels.quota'),
                 'auth': self.msg('dispatch.error_labels.auth'), 'model_unsupported': self.msg('dispatch.error_labels.model_unsupported'), 'session_missing': self.msg('dispatch.error_labels.session_missing'),
                 'session_live': self.msg('dispatch.error_labels.session_live'), 'unknown': self.msg('dispatch.error_labels.unknown'), 'task_unresolved': self.msg('dispatch.error_labels.task_unresolved'),
                 'run_timeout': self.msg('dispatch.error_labels.run_timeout'), 'spawn_failed': self.msg('dispatch.error_labels.spawn_failed'), 'stopped': self.msg('dispatch.error_labels.stopped'), 'crashed': self.msg('dispatch.error_labels.crashed'),
                 'provider_degraded': self.msg('dispatch.error_labels.provider_degraded'),
             }
        self.question_hints = re.compile(self.pattern('dispatch.question_hints.1'), re.I)
        self.trigger_label = {'slack_message': self.msg('dispatch.trigger_label.slack_message'), 'decision_answer': self.msg('dispatch.trigger_label.decision_answer'), 'ask_answer': self.msg('dispatch.trigger_label.ask_answer'),
                              'agent_message': self.msg('dispatch.trigger_label.agent_message'), 'note': self.msg('dispatch.trigger_label.note'), 'routine': self.msg('dispatch.trigger_label.routine'), 'launch': self.msg('dispatch.trigger_label.launch')}

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def pattern(self, key):
        return locale.pattern(self.language, key).replace('<owner_key>', self.key).replace('<owner_name>', self.name)

    def final_question(self, text, limit=1200):
        """The question a run ended on, if its final message asks the owner something (deterministic)."""
        text = str(text or '').strip()
        if not text:
            return None
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
        tail = '\n\n'.join(paragraphs[-2:]) if paragraphs else text
        if not self.question_hints.search(tail):
            return None
        return tail[-limit:]

    def _label(self, text, n):
        text = re.sub(r'<[@!#][^>]*>', '', ' '.join(str(text or '').split()))
        return (text[:28] + '…') if len(text) > 29 else (text or self.msg('dispatch._label.1', n=n))

    def hook_question(self, tool_name, tool_input):
        """AskUserQuestion / ExitPlanMode tool input -> (Chinese card question, option specs)."""
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        if tool_name == 'ExitPlanMode':
            plan = str(tool_input.get('plan') or '').strip()
            body = plan[:1100] + ('…' if len(plan) > 1100 else '') if plan else self.msg('dispatch.hook_question.1')
            return self.msg('dispatch.hook_question.2') + body, [self.msg('dispatch.hook_question.3'),
                                                                        self.msg('dispatch.hook_question.4')]
        questions = [q for q in tool_input.get('questions') or [] if isinstance(q, dict) and q.get('question')]
        if not questions:
            return self.msg('dispatch.hook_question.5'), []
        # P3.1 copy standard: the question itself leads; "后台会话在终端里提问" is in the card footer.
        lines = []
        for q in questions[:4]:
            lines.append(str(q['question']) + (self.msg('dispatch.hook_question.6') + str(q.get('header')) + self.msg('dispatch.hook_question.7') if q.get('header') else ''))
        options, labels = [], set()
        first = questions[0]
        for n, option in enumerate(first.get('options') or [], 1):
            label = self._label(option.get('label') if isinstance(option, dict) else option, n)
            if label in labels:
                continue
            labels.add(label)
            options.append(f'o{n}={label}')
        if len(options) > 4:
            options = options[:3] + [self.msg('dispatch.hook_question.8')]
        if len(options) < 2:
            options = [self.msg('dispatch.hook_question.9'), self.msg('dispatch.hook_question.10')]
        if len(questions) > 1:
            lines.append(self.msg('dispatch.hook_question.11'))
        return '\n'.join(lines)[:1500], options

    def strip_agent_env(self, env):
        return {k: v for k, v in env.items() if k not in self.strip_env_exact and not k.startswith(STRIP_ENV_PREFIX)}

    def _clock(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz).strftime('%H:%M')

    def trigger_summary(self, kinds):
        counts = {}
        for kind in kinds:
            counts[kind] = counts.get(kind, 0) + 1
        if not counts:
            return self.msg('dispatch.trigger_summary.1')
        return self.msg('dispatch.trigger_summary.2').join(self.msg('dispatch.trigger_summary.4', get=self.trigger_label.get(k, self.msg('dispatch.trigger_summary.3')), n=n) for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))

    def session_tag(self, dashboard, task_key, provider, session, run_id=None, link=True):
        """P43 card line: the session working on the task, its latest run page and the terminal takeover command, e.g.
        `会话 codex 01a16cde-fe49 · <…/runs/<run>|看板> · 终端接手：fsess take <task>`. Shows what is known: no id yet (a fresh
        run before the CLI reported it) reads `（id 稍后显示）`; no provider (a footer already names it) leaves it out."""
        sid = str(session or '')[:SESSION_ID_CHARS]
        head = self.msg('dispatch.session_tag.1') + (f' {provider}' if provider else '') + (f' {sid}' if sid else self.msg('dispatch.session_tag.2'))
        url = f'{dashboard}/runs/{run_id}' if run_id else f'{dashboard}/runs?task={task_key}'
        target = sid if str(task_key or '').startswith('session:') else task_key  # takeover takes a unique session prefix
        return ' · '.join(x for x in (head, link and self.msg('dispatch.session_tag.3', url=url), target and self.msg('dispatch.session_tag.4', target=target)) if x)

    def run_card_blocks(self, text, status_line, dispatch_id, paused=False, closed=False):
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}},
                  {'type': 'context', 'block_id': 'sf_dispatch_status',
                   'elements': [{'type': 'mrkdwn', 'text': status_line}]}]
        if not closed:
            first = _button(self.msg('dispatch.run_card_blocks.1'), 'resume', dispatch_id, 'primary') if paused else \
                _button(self.msg('dispatch.run_card_blocks.2'), 'stop', dispatch_id, 'danger')
            blocks.append({'type': 'actions', 'block_id': 'sf_dispatch_actions', 'elements': [
                first, _button(self.msg('dispatch.run_card_blocks.3'), 'iterm', dispatch_id), _button(self.msg('dispatch.run_card_blocks.4'), 'view', dispatch_id)]})
        return blocks

    def failure_card_blocks(self, text, dispatch_id, closed_line=None):
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]
        if closed_line:
            return blocks + [{'type': 'context', 'block_id': 'sf_dispatch_status',
                              'elements': [{'type': 'mrkdwn', 'text': closed_line}]}]
        return blocks + [{'type': 'actions', 'block_id': 'sf_dispatch_retry', 'elements': [
            _button(self.msg('dispatch.failure_card_blocks.1'), 'retry', dispatch_id, 'primary'), _button(self.msg('dispatch.failure_card_blocks.2'), 'retry_codex', dispatch_id),
            _button(self.msg('dispatch.failure_card_blocks.3'), 'retry_claude', dispatch_id), _button(self.msg('dispatch.failure_card_blocks.4'), 'stop', dispatch_id, 'danger')]}]

    def breaker_card_blocks(self, text, breaker_id, closed_line=None):
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]
        if closed_line:
            return blocks + [{'type': 'context', 'block_id': 'sf_breaker_status',
                              'elements': [{'type': 'mrkdwn', 'text': closed_line}]}]
        return blocks + [{'type': 'actions', 'block_id': 'sf_breaker_actions', 'elements': [
            {'type': 'button', 'text': {'type': 'plain_text', 'text': self.msg('dispatch.breaker_card_blocks.text'), 'emoji': True},
             'action_id': 'sf_breaker:continue', 'value': breaker_id, 'style': 'primary'},
            {'type': 'button', 'text': {'type': 'plain_text', 'text': self.msg('dispatch.breaker_card_blocks.text_2'), 'emoji': True},
             'action_id': 'sf_breaker:stop', 'value': breaker_id, 'style': 'danger'}]}]

    def progress_line(self, steps, summary, elapsed_seconds):
        """The run card's live status line. `summary` is already masked and mention-free (run_log.short)."""
        minutes = max(0, int(elapsed_seconds // 60))
        return self.msg('dispatch.progress_line.1', int=int(steps), mrkdwn=_mrkdwn(summary), minutes=minutes)

    def _waited(self, seconds):
        minutes = max(0, int(seconds // 60))
        return self.msg('dispatch._waited.1', value=minutes // 60) if minutes >= 60 else self.msg('dispatch._waited.2', minutes=minutes)
