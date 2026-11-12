"""Provider health. Deterministic: no model runs here, no log is re-read here.

`evaluate` (a supervisor step) looks at the dispatcher's own run rows over a rolling `window_minutes` window and, per
provider, counts ADVERSE runs — runs that ended in the window or are still running:
  - a provider-level failure: `transient` (overload / 5xx / timeout / network), `rate_limited`, `quota`, `run_timeout`,
    and (0.79.2) the run watchdog's stops `ttft_timeout` / `error_grace`;
  - repeated overload / reconnect / stream-disconnect / API-retry markers in its log (`consumer_runs.health_markers`) on
    a run that ENDED without success (an overload the CLI retried and then succeeded is `retried_ok`, not adverse;
    markers on a still-running run do not count yet);
  - for a long-lived task (per-event triage runs that should be short) a run past its class's slow threshold.
`degrade_threshold` adverse runs in a window holding at least `min_runs` runs -> `degraded`; `recover_checks`
consecutive checks with none in the window -> `healthy` again. Per-model counts are kept for the panel and the notice.

The dispatcher reads `status_map`: fresh starts avoid a degraded provider, fallbacks try healthy ones first, a task
displaced from its preferred provider returns to it once healthy. The owner gets ONE notice per episode (plain text in
their language, `health.*`, not a card): when a provider degrades, and when it is back — announced only after a clean
successful run on it. `dispatch.provider_health.enabled=false`: no evaluation, no notices. The post itself is the
channel's (`post(rt, db, provider, kind, text)`, e.g. kage.adapters.slack.health_notices).
"""
from __future__ import annotations
import json
import time

from kage import locale

PROVIDERS = ('codex', 'claude', 'grok')
LABEL = {'codex': 'Codex', 'claude': 'Claude', 'grok': 'Grok'}
HEALTHY, DEGRADED = 'healthy', 'degraded'
PROVIDER_FAILURES = ('transient', 'rate_limited', 'quota', 'run_timeout', 'ttft_timeout', 'error_grace')  # 0.79.2: the last two
OVERLOAD_FAILURES = ('transient', 'rate_limited', 'error_grace')
MARKERS_PER_RUN = 2  # one stray reconnect is normal noise; two or more in one run is the provider struggling

DDL = '''
  CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY, status TEXT NOT NULL, clean_checks INTEGER NOT NULL DEFAULT 0, since REAL,
    announced TEXT NOT NULL DEFAULT 'healthy', notice_ts TEXT, notice_error TEXT, updated_at REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS provider_health_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT, at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS provider_health_events_at ON provider_health_events(at);
'''


def initialize(db):
    db.executescript(DDL)
    db.commit()


def status_map(db):
    """{provider: status} for providers with a row (a provider without one is healthy)."""
    return {r[0]: r[1] for r in db.execute('SELECT provider, status FROM provider_health')}


def _row(db, provider):
    row = db.execute('SELECT * FROM provider_health WHERE provider=?', (provider,)).fetchone()
    return dict(row) if row else {'provider': provider, 'status': HEALTHY, 'clean_checks': 0, 'since': None,
                                  'announced': HEALTHY, 'notice_ts': None, 'notice_error': None}


def _limits(ph, long_lived):
    """P39: {task_id: slow seconds}. `long_lived` is `worker_config.slow_limits` (per task class), or a plain set of
    task ids that all use slow_run_minutes (the pre-P39 shape)."""
    if isinstance(long_lived, dict):
        return long_lived
    return {task_id: ph['slow_run_minutes'] * 60 for task_id in long_lived}


def window(db, provider, now, ph, long_lived=()):
    """Counts over the rolling window for one provider (runs that ended in it, or are still running)."""
    since, limits = now - ph['window_minutes'] * 60, _limits(ph, long_lived)
    out = {'runs': 0, 'adverse': 0, 'failed': 0, 'overload': 0, 'slow': 0, 'retried': 0, 'retried_ok': 0, 'markers': 0}
    models = {}
    for r in db.execute("SELECT task, model, state, started_at, ended_at, outcome, error_class, trigger, health_markers "
                        "FROM consumer_runs WHERE provider=? AND (state='running' OR ended_at>=?)", (provider, since)):
        running = r['state'] == 'running'
        markers = r['health_markers'] or 0
        end = now if running else (r['ended_at'] or now)
        failed = not running and r['outcome'] == 'failed' and r['error_class'] in PROVIDER_FAILURES
        # P39: repeated markers are adverse only on a run that ended without success — the CLI retried an overload
        # and finished is the provider coping (retried_ok), and a running run's markers wait for its outcome.
        repeated = markers >= MARKERS_PER_RUN
        overload = (repeated and not running and r['outcome'] != 'success') or (
            failed and r['error_class'] in OVERLOAD_FAILURES)
        slow = r['task'] in limits and end - (r['started_at'] or end) >= limits[r['task']]
        adverse = failed or overload or slow
        out['runs'] += 1
        out['adverse'] += adverse
        out['failed'] += failed
        out['overload'] += overload
        out['slow'] += slow
        out['retried'] += r['trigger'] == 'transient_retry'
        out['retried_ok'] += repeated and not running and r['outcome'] == 'success'
        out['markers'] += markers
        model = models.setdefault(r['model'] or '?', {'model': r['model'] or '?', 'runs': 0, 'adverse': 0})
        model['runs'] += 1
        model['adverse'] += adverse
    out['models'] = sorted(models.values(), key=lambda m: (-m['adverse'], -m['runs'], m['model']))
    return out


def _proved(db, provider, since, limits):
    """A clean successful run on the provider started after it turned healthy again: no repeated markers and, for a
    long-lived task, not slow by its class threshold (P39 `limits`, see `_limits`) — the recovery notice waits for one,
    so it never announces a provider still crawling."""
    return any(r['task'] not in limits or r['ended_at'] - r['started_at'] < limits[r['task']] for r in db.execute(
        "SELECT task, started_at, ended_at FROM consumer_runs WHERE provider=? AND state='ended' AND outcome='success' "
        'AND started_at>=? AND coalesce(health_markers,0)<?', (provider, since or 0, MARKERS_PER_RUN)))


def _save(rt, db, row, now, event=None, detail=None):
    with rt.transaction(db):
        db.execute('INSERT INTO provider_health (provider,status,clean_checks,since,announced,notice_ts,notice_error,'
                   'updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET status=excluded.status,'
                   'clean_checks=excluded.clean_checks,since=excluded.since,announced=excluded.announced,'
                   'notice_ts=excluded.notice_ts,notice_error=excluded.notice_error,updated_at=excluded.updated_at',
                   (row['provider'], row['status'], row['clean_checks'], row['since'], row['announced'],
                    row['notice_ts'], row['notice_error'], now))
        if event:
            db.execute('INSERT INTO provider_health_events (provider,kind,detail,at) VALUES (?,?,?,?)',
                       (row['provider'], event, json.dumps(detail, ensure_ascii=False, sort_keys=True), now))


# ---------------------------------------------------------------------------
# The notice (a plain message in the owner's decision channel, NOT a decision card)
# ---------------------------------------------------------------------------

def default_model(config, provider):
    from kage.adapters.providers.grok import DEFAULTS
    spec = config['providers'].get(provider, DEFAULTS if provider == 'grok' else {})
    return spec['model_default'] if provider == 'claude' else spec['model']


# ---------------------------------------------------------------------------
# Read model (dashboard /health, `health` CLI): read-only, counts and ids only
# ---------------------------------------------------------------------------

def displaced_tasks(db, wc, config, limit=50):
    """Tasks whose current session exists only because of a fallback / health reroute, on a non-preferred provider.
    P39: preferred = the task's pinned launch provider (`dispatch_prefs`), else its intake role / category provider —
    the same choice as the dispatcher's `_preferred`."""
    out = []
    # A database the P39 daemon has not migrated yet (read-only dashboard) has no dispatch_prefs: dispatch_meta only.
    prefs = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatch_prefs'").fetchone()
    pick = ('p.provider AS pin, CASE WHEN p.task_key IS NULL THEN m.category ELSE p.category END AS category'
            if prefs else 'NULL AS pin, m.category AS category')
    join = 'LEFT JOIN dispatch_prefs p ON p.task_key=t.task_id ' if prefs else ''
    for r in db.execute(f"SELECT t.task_id, t.current_provider, {pick} FROM tasks t LEFT JOIN dispatch_meta m ON "
                        f"m.task_key=t.task_id {join}WHERE t.current_session IS NOT NULL AND coalesce(t.current_source,'')!="
                        "'manual' AND EXISTS (SELECT 1 FROM consumer_runs r WHERE r.task=t.task_id AND "
                        "r.provider=t.current_provider AND r.session=t.current_session AND r.mode='fresh' AND "
                        "(r.trigger='fallback' OR r.route_reason='degraded_reroute')) ORDER BY t.task_id"):
        preferred = (r['pin'] if r['pin'] in PROVIDERS else None) or wc.preferred_provider(config, r['task_id'],
                                                                                           r['category'])
        if preferred != r['current_provider']:
            out.append({'task_id': r['task_id'], 'current_provider': r['current_provider'], 'preferred': preferred})
    return out[:limit]


def overview(db, wc, config, now=None, limit=20):
    now = time.time() if now is None else now
    ph = config['dispatch']['provider_health']
    limits = wc.slow_limits(config)
    providers = []
    for provider in PROVIDERS:
        if provider == 'grok' and 'grok' not in config['providers'] and not db.execute(
                "SELECT 1 FROM consumer_runs WHERE provider='grok' LIMIT 1").fetchone():
            continue  # an optional provider this deployment never configured nor ran
        row = _row(db, provider)
        providers.append({'provider': provider, 'label': LABEL[provider], 'model': default_model(config, provider),
                          'status': row['status'], 'since': row['since'], 'clean_checks': row['clean_checks'],
                          'announced': row['announced'], 'notice_error': row['notice_error'],
                          'window': window(db, provider, now, ph, limits)})
    events = [{'provider': r['provider'], 'kind': r['kind'], 'detail': json.loads(r['detail'] or 'null'), 'at': r['at']}
              for r in db.execute('SELECT * FROM provider_health_events ORDER BY at DESC, id DESC LIMIT ?', (limit,))]
    # Every session / provider switch the dispatcher made, with its reason (long_lived, context_cap, auto_return, ...).
    switches = [dict(r) for r in db.execute("SELECT task_key, reason, from_provider, to_provider, dispatch_id, at FROM "
                                            "dispatch_routes WHERE decision='switched' ORDER BY at DESC, id DESC LIMIT ?",
                                            (limit,))]
    return {'config': ph, 'providers': providers, 'events': events, 'switches': switches,
            'displaced': displaced_tasks(db, wc, config)}

class ProviderHealth:
    """The monitor pass and its notice, in the owner's language; `post` sends a notice and returns its id."""

    def __init__(self, language, *, post):
        self.language, self.post = language, post

    def msg(self, key, **values):
        return locale.message(self.language, key, **values)

    def evaluate(self, rt, db, now=None):
        """One monitor pass. Returns the state changes / notices, e.g. [{'provider': 'codex', 'event': 'degraded'}]."""
        now = time.time() if now is None else now
        try:
            config = rt.worker_config.load()[0]
        except ValueError as exc:  # a malformed config never degrades or notices by accident
            rt._meta_error(db, 'provider_health_config_error', exc)
            return []
        ph = config['dispatch']['provider_health']
        if not ph['enabled']:
            return []
        limits = rt.worker_config.slow_limits(config)  # per task class (an on-call task may have its own threshold)
        out = []
        for provider in PROVIDERS:
            old = _row(db, provider)
            row = dict(old)
            counts = window(db, provider, now, ph, limits)
            event = None
            # P39: one adverse run among too few runs is noise, never an episode (min_runs).
            if row['status'] == HEALTHY and counts['runs'] >= ph['min_runs'] and counts['adverse'] >= ph['degrade_threshold']:
                row.update(status=DEGRADED, clean_checks=0, since=now)
                event = DEGRADED
            elif row['status'] == DEGRADED:
                row['clean_checks'] = row['clean_checks'] + 1 if counts['adverse'] == 0 else 0
                if row['clean_checks'] >= ph['recover_checks']:
                    row.update(status=HEALTHY, clean_checks=0, since=now)
                    event = HEALTHY
            if event or row != old:
                _save(rt, db, row, now, event, {k: v for k, v in counts.items() if k != 'models'} if event else None)
            if event:
                out.append({'provider': provider, 'event': event, 'adverse': counts['adverse'], 'runs': counts['runs']})
            # Notices: one per episode (deduped by `announced`); a failed post is retried on the next pass.
            if row['status'] == DEGRADED and row['announced'] != DEGRADED:
                kind = DEGRADED
            elif row['status'] == HEALTHY and row['announced'] == DEGRADED and \
                    _proved(db, provider, row['since'], limits):
                kind = HEALTHY
            else:
                continue
            try:
                text = self.notice_text(provider, kind, counts, config, status_map(db), ph['window_minutes'])
                row.update(announced=kind, notice_ts=self.post(rt, db, provider, kind, text), notice_error=None)
                _save(rt, db, row, now, 'notice_' + kind, {'adverse': counts['adverse'], 'runs': counts['runs']})
                out.append({'provider': provider, 'event': 'notice_' + kind})
            except Exception as exc:  # noqa: BLE001 - the health state stands; only the notice is missing
                row['notice_error'] = rt.safe_error(exc)
                _save(rt, db, row, now)
        return out

    def notice_text(self, provider, kind, counts, config, health, window_minutes):
        """The copy standard: the first line is one plain sentence (model, what happened, what the runtime did)."""
        top = (counts.get('models') or [{}])[0]
        model = top.get('model') if top.get('model') not in (None, '?') else default_model(config, provider)
        name = self.msg('health.name', model=model, provider=LABEL[provider])
        if kind == HEALTHY:
            return self.msg('health.recovered', name=name)
        from kage.core.worker_config import fallback_after
        order = fallback_after(config, provider)
        alt = next((p for p in order if p != provider and health.get(p) != DEGRADED), None)
        first = (self.msg('health.degraded_switched', name=name, alt=LABEL[alt]) if alt else
                 self.msg('health.degraded_both', name=name))
        why = self.msg('health.why_joiner').join(part for part in (
            self.msg('health.why_overload', n=counts['overload']) if counts['overload'] else None,
            self.msg('health.why_failed', n=counts['failed']) if counts['failed'] else None,
            self.msg('health.why_slow', n=counts['slow']) if counts['slow'] else None) if part)
        return first + self.msg('health.window', minutes=window_minutes, runs=counts['runs'], adverse=counts['adverse'], why=why)
