"""Prompt-cache report of dispatched runs (read-only; stdlib only; no model, no network).

`report` reads the dispatcher's own run rows (`consumer_runs`, opened read-only by the caller) and, per run, the run
dir's `events.jsonl` (the provider CLI's raw stream) and `meta.json`, and returns numbers only — never a message body,
prompt, tool input or result text:
  - Claude (stream-json): the FIRST API call's input side (`input_tokens`, `cache_creation_input_tokens`,
    `cache_read_input_tokens`, and the 5m / 1h split of the cache write), the run totals from the `result` event
    (API calls, turns, output tokens, cost, CLI duration);
  - Codex (`--json`): per run the `turn.completed` usage summed (`input_tokens`, `cached_input_tokens`); the stream has
    no per-call usage, so its "first call" is the whole turn;
  - the run's wall duration (`consumer_runs`), its gap since the previous run of the same task and provider ended (the
    cache TTL question: a gap past the TTL starts cold), and its context profile (`meta.json` `context`, P40; a run
    without one is `p39`).
A run is COLD when its first call read nothing from the cache. Summaries group by task, provider and profile:
cold-start rate (overall and per gap bucket), median first-call input / read / write, the read share of the first
call's input side and median / p90 durations — the before / after view of `dispatch.lean_context`.
P40.1: profile `p40.1:<class>:<flags>` for a run with a constant first message; the whole-run read share (every API call
of the run, from the `result` event's totals); `since_use_seconds` — the gap since the prefix was last used, by a real
run's end or a warmed keep-warm refresh's start; and `keep_warm`: the refreshes (`keep_warm_runs`, never counted as runs)
per task with their first-call numbers and cost, plus the cost per day of the window.
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import time

GAP_BUCKETS = ((300, 'le_5m'), (3600, '5m_1h'), (None, 'gt_1h'))
HEAD_LINES = 4000  # the first assistant event comes right after the init event; never scan a huge stream for it
CANDIDATE_RE = re.compile(r'"type":\s?"(?:assistant|result)"')
CLAUDE_KEYS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _lines(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            yield from stream
    except OSError:
        return


def claude_usage(path):
    """Numbers of one Claude stream-json run log (None when the file is missing or has no API call)."""
    first, result, calls, seen = None, None, 0, set()
    for n, line in enumerate(_lines(path)):
        if first is None and n > HEAD_LINES:
            break
        # Key order differs per event (the result event does not start with "type"): match, then check the top level.
        if not CANDIDATE_RE.search(line):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'result':
            result = event
        elif event.get('type') == 'assistant' and isinstance(event.get('message'), dict):
            message = event['message']
            if message.get('id') not in seen:
                seen.add(message.get('id'))
                calls += 1
                if first is None and isinstance(message.get('usage'), dict):
                    first = message['usage']
    if first is None and result is None:
        return None
    u = first or {}
    split = u.get('cache_creation') if isinstance(u.get('cache_creation'), dict) else {}
    out = {k: _num(u.get(k)) for k in CLAUDE_KEYS}
    out.update(first_input=sum(out[k] for k in CLAUDE_KEYS), write_5m=_num(split.get('ephemeral_5m_input_tokens')),
               write_1h=_num(split.get('ephemeral_1h_input_tokens')), api_calls=calls)
    if result:
        total = result.get('usage') if isinstance(result.get('usage'), dict) else {}
        out.update(turns=result.get('num_turns'), output_tokens=_num(total.get('output_tokens')),
                   total_read=_num(total.get('cache_read_input_tokens')),
                   total_write=_num(total.get('cache_creation_input_tokens')),
                   total_input=_num(total.get('input_tokens')),
                   cost_usd=result.get('total_cost_usd') if isinstance(result.get('total_cost_usd'), (int, float))
                   else None,
                   cli_seconds=round(_num(result.get('duration_ms')) / 1000, 1),
                   api_seconds=round(_num(result.get('duration_api_ms')) / 1000, 1))
    return out


def codex_usage(path):
    """Summed `turn.completed` usage of one Codex --json run log (None without a completed turn)."""
    turns, inp, cached, output = 0, 0, 0, 0
    for line in _lines(path):
        if '"turn.completed"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        usage = event.get('usage') if event.get('type') == 'turn.completed' else None
        if isinstance(usage, dict):
            turns += 1
            inp += _num(usage.get('input_tokens'))
            cached += _num(usage.get('cached_input_tokens'))
            output += _num(usage.get('output_tokens'))
    if not turns:
        return None
    # OpenAI reports cached tokens as a part of input_tokens; no cache-write count exists.
    return {'input_tokens': inp - cached, 'cache_creation_input_tokens': None, 'cache_read_input_tokens': cached,
            'first_input': inp, 'turns': turns, 'output_tokens': output, 'api_calls': None}


def profile_label(meta):
    """`p39` for a run without the P40 context record, else e.g. `p40:inbox:names,no-git,sys-rules,ttl=1h`; P40.1 (a
    constant first message) e.g. `p40.1:inbox:names,no-git,const,no-mem,no-mcp,drop=13,ttl=1h`."""
    ctx = meta.get('context') if isinstance(meta, dict) else None
    if not isinstance(ctx, dict):
        return 'p39'
    parts = []
    if ctx.get('skills') and ctx.get('skills') != 'full':
        parts.append(str(ctx['skills']))
    if ctx.get('git_context') is False:
        parts.append('no-git')
    if ctx.get('rules_in_system_prompt'):
        parts.append('sys-rules')
    if ctx.get('first_message') == 'constant':
        parts.append('const')
    if ctx.get('auto_memory') is False:
        parts.append('no-mem')
    if ctx.get('mcp') == 'none':
        parts.append('no-mcp')
    if isinstance(ctx.get('drop_tools'), int) and ctx['drop_tools'] > 0:
        parts.append(f"drop={ctx['drop_tools']}")
    if ctx.get('cache_ttl') in ('5m', '1h'):
        parts.append('ttl=' + ctx['cache_ttl'])
    version = 'p40.1' if ctx.get('layout') == 'p40.1' else 'p40'
    return f"{version}:{ctx.get('class') or '?'}:{','.join(parts) or 'default'}"


def _meta(log_dir):
    try:
        data = json.loads(Path(log_dir, 'meta.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _bucket(gap):
    if gap is None:
        return 'first'
    return next(label for limit, label in GAP_BUCKETS if limit is None or gap <= limit)


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _p90(values):
    values = sorted(v for v in values if v is not None)
    return values[max(0, -(-9 * len(values) // 10) - 1)] if values else None


def run_rows(db, since, task=None, provider=None):
    """consumer_runs rows started at or after `since` (oldest first), plus the run just before each task / provider's
    first one in the window (so the first gap is known). Nothing is written."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'consumer_runs' not in tables:
        return []
    where, args = ['1=1'], []
    if task:
        where.append('task=?')
        args.append(task)
    if provider:
        where.append('provider=?')
        args.append(provider)
    return [dict(r) for r in db.execute(
        'SELECT run_id, dispatch_id, task, provider, model, effort, mode, trigger, started_at, ended_at, outcome, '
        f'log_dir, route_reason FROM consumer_runs WHERE {" AND ".join(where)} AND started_at >= ? '
        'ORDER BY started_at, run_id', (*args, since - 86400))]


def keep_warm_rows(db, since, task=None):
    """P40.1: keep_warm_runs rows started at or after `since` minus a day (oldest first); [] before P40.1."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'keep_warm_runs' not in tables:
        return []
    where, args = ['started_at >= ?'], [since - 86400]
    if task:
        where.append('task=?')
        args.append(task)
    return [dict(r) for r in db.execute('SELECT kw_id, task, model, effort, state, started_at, ended_at, outcome, '
                                        f'log_dir FROM keep_warm_runs WHERE {" AND ".join(where)} '
                                        'ORDER BY started_at, kw_id', args)]


def keep_warm_report(rows, since, since_hours, limit):
    """P40.1: per task the refreshes in the window (warmed / failed / still running), their first-call read / write
    medians, total cost and the cost per day; the newest `limit` refreshes with their numbers."""
    items = []
    for row in rows:
        if (row['started_at'] or 0) < since:
            continue
        usage = claude_usage(Path(row['log_dir'], 'events.jsonl')) if row['log_dir'] else None
        items.append({'kw_id': row['kw_id'], 'task': row['task'], 'model': row['model'], 'state': row['state'],
                      'outcome': row['outcome'], 'started_at': row['started_at'], 'usage': usage,
                      'cold': (usage['cache_read_input_tokens'] == 0) if usage else None})
    summary = []
    for task in sorted({i['task'] for i in items}):
        mine = [i for i in items if i['task'] == task]
        measured = [i for i in mine if i['usage']]
        cost = round(sum(i['usage'].get('cost_usd') or 0 for i in measured), 4)
        summary.append({'task': task, 'refreshes': len(mine), 'warmed': sum(i['outcome'] == 'warmed' for i in mine),
                        'failed': sum(i['outcome'] in ('failed', 'timeout') for i in mine),
                        'running': sum(i['state'] == 'running' for i in mine),
                        'cold': sum(bool(i['cold']) for i in measured),
                        'first_read_median': _median([i['usage']['cache_read_input_tokens'] for i in measured]),
                        'first_write_median': _median([i['usage']['cache_creation_input_tokens'] for i in measured]),
                        'output_tokens': sum(i['usage'].get('output_tokens') or 0 for i in measured),
                        'cost_usd': cost, 'cost_usd_per_day': round(cost * 24 / since_hours, 4)})
    return {'refreshes_total': len(items), 'summary': summary, 'runs': list(reversed(items))[:max(0, limit)]}


def report(db, since_hours=24.0, task=None, provider=None, limit=50, now=None):
    """The cache report (see the module docstring). `db` is a read-only connection with row_factory sqlite3.Row."""
    now = time.time() if now is None else now
    since = now - since_hours * 3600
    rows = run_rows(db, since, task, provider)
    kw_rows = keep_warm_rows(db, since, task) if provider in (None, 'claude') else []
    warmed = {}
    for row in kw_rows:
        if row['outcome'] == 'warmed':
            warmed.setdefault(row['task'], []).append(row['started_at'])
    last_end, runs = {}, []
    for row in rows:
        key = (row['task'], row['provider'])
        started = row['started_at'] or 0
        prev = last_end.get(key)
        last_end[key] = row['ended_at'] or started
        if started < since:
            continue
        refreshed = max((t for t in warmed.get(row['task'], ()) if t <= started), default=None) \
            if row['provider'] == 'claude' else None
        used = max(x for x in (prev, refreshed) if x is not None) if prev is not None or refreshed is not None else None
        usage = None
        if row['log_dir']:
            log = Path(row['log_dir'], 'events.jsonl')
            usage = claude_usage(log) if row['provider'] == 'claude' else codex_usage(log)
        gap = round(started - prev) if prev is not None and started >= prev else None
        item = {'run_id': row['run_id'], 'task': row['task'], 'provider': row['provider'], 'model': row['model'],
                'mode': row['mode'], 'trigger': row['trigger'], 'outcome': row['outcome'],
                'profile': profile_label(_meta(row['log_dir'])) if row['log_dir'] else 'p39',
                'started_at': started, 'wall_seconds': round(row['ended_at'] - started, 1) if row['ended_at'] else None,
                'gap_seconds': gap, 'gap_bucket': _bucket(gap), 'usage': usage,
                'since_use_seconds': round(started - used) if used is not None and started >= used else None,
                'cold': (usage['cache_read_input_tokens'] == 0) if usage else None}
        runs.append(item)
    return {'ok': True, 'generated_at': now, 'since_hours': since_hours, 'task': task, 'provider': provider,
            'runs_total': len(runs), 'summary': summarize(runs), 'overall': summarize(runs, by=())[0] if runs else None,
            'runs': list(reversed(runs))[:max(0, limit)],
            'keep_warm': keep_warm_report(kw_rows, since, since_hours, limit)}


def summarize(runs, by=('task', 'provider', 'profile')):
    groups = {}
    for run in runs:
        groups.setdefault(tuple(run[k] for k in by), []).append(run)
    out = []
    for key, items in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        measured = [r for r in items if r['usage']]
        first = [r['usage']['first_input'] for r in measured]
        reads = [r['usage']['cache_read_input_tokens'] for r in measured]
        writes = [r['usage']['cache_creation_input_tokens'] for r in measured]
        totals = [r['usage'] for r in measured if r['usage'].get('total_read') is not None]
        total_in = sum(u['total_read'] + u['total_write'] + u.get('total_input', 0) for u in totals)
        buckets = {}
        for r in measured:
            b = buckets.setdefault(r['gap_bucket'], [0, 0])
            b[0] += 1
            b[1] += bool(r['cold'])
        out.append({**dict(zip(by, key)), 'runs': len(items), 'measured': len(measured),
                    'cold_starts': sum(bool(r['cold']) for r in measured),
                    'cold_rate': round(sum(bool(r['cold']) for r in measured) / len(measured), 3) if measured else None,
                    'cold_by_gap': {k: {'runs': v[0], 'cold': v[1]} for k, v in sorted(buckets.items())},
                    'first_input_median': _median(first), 'first_read_median': _median(reads),
                    'first_write_median': _median(writes),
                    'first_read_share': round(sum(reads) / sum(first), 3) if sum(first) else None,
                    'run_read_share': round(sum(u['total_read'] for u in totals) / total_in, 3) if total_in else None,
                    'wall_seconds_median': _median([r['wall_seconds'] for r in items]),
                    'wall_seconds_p90': _p90([r['wall_seconds'] for r in items])})
    return out
