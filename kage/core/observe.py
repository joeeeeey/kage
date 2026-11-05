"""O2c: triage while observing (onboarding design §3.4). In observe mode the dispatcher starts no agent run; for the
intake tasks' held dispatches it asks the inbox model instead — with every tool off, the provider probe's way (Claude
`--tools '' --strict-mcp-config`, Codex `--ephemeral --sandbox read-only`), in a private scratch directory — what it
would have done with the burst: one JSON line {"route": task | reply | log | ignore, "why": "..."}. The answer becomes a
shadow `triage` row per burst (the route and the one-line why, never the message text); the held events are resolved
`observe` and the dispatch ends `observed`. One call at a time, killed after TRIAGE_TIMEOUT_SECONDS, its result read on
a later cycle; nothing at all while the mode is not `observe`.
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import shutil
import signal
import tempfile
import uuid

from kage.core import mode as _mode

META = 'observe_triage'              # runtime_meta: the running call {pid, pid_start, started_at, dir, dispatch_id, ...}
TRIAGE_TIMEOUT_SECONDS = 180
TRIAGE_DIR_PREFIX = 'kage-observe-'
ROUTES = ('task', 'reply', 'log', 'ignore')
MAX_TEXT = 6000
PROMPT = (
    "You are triaging messages for {name} while {name}'s assistant only observes: nothing you say is sent or done, "
    'and you have no tools. Decide what the assistant WOULD do with the conversation below and answer with exactly one '
    'JSON line and nothing else: {{"route": "task" | "reply" | "log" | "ignore", "why": "<one short sentence in '
    '{language}>"}}. task = it needs work (a new task), reply = a short answer in the conversation is enough, log = '
    'worth recording only, ignore = not for {name}.\n\n--- conversation ({kind}) ---\n{text}\n--- end ---')
JSON_LINE_RE = re.compile(r'\{[^{}]*"route"[^{}]*\}')


def _meta(db):
    row = db.execute('SELECT value FROM runtime_meta WHERE key=?', (META,)).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        return None


def _save(db, value):
    with db:
        if value is None:
            db.execute('DELETE FROM runtime_meta WHERE key=?', (META,))
        else:
            db.execute('INSERT OR REPLACE INTO runtime_meta (key, value) VALUES (?, ?)', (META, json.dumps(value)))


def burst_text(rt, b):
    """The burst's own messages, read from Slack now (reads are allowed while observing), oldest first, capped."""
    from kage.core.intake import message_text
    refs = set(json.loads(b['refs'] or '[]'))
    token = rt.api_identity()
    if b['thread_root']:
        found = rt.slack.api_call('conversations.replies', token, params={
            'channel': b['channel'], 'ts': b['thread_root'], 'limit': 60})
    else:
        found = rt.slack.api_call('conversations.history', token, params={
            'channel': b['channel'], 'oldest': b['first_ts'], 'latest': b['last_ts'], 'inclusive': 'true',
            'limit': min(100, len(refs) + 20)})
    msgs = sorted((m for m in found.get('messages') or [] if isinstance(m, dict) and m.get('ts') in refs),
                  key=lambda m: float(m['ts']))
    return '\n'.join(message_text(m) for m in msgs)[:MAX_TEXT]


def parse(text):
    """The model's JSON line -> {route, why}; None when there is none (counted as `unclear`)."""
    for candidate in reversed(JSON_LINE_RE.findall(text or '')):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if value.get('route') in ROUTES:
            return {'route': value['route'], 'why': ' '.join(str(value.get('why') or '').split())[:200]}
    return None


def step(dispatcher, waiting, config, now):
    """Called by the dispatcher's cycle while the mode is not active (after it recorded the shadow `dispatch` rows)."""
    db = dispatcher.db
    if _mode.read(db) != 'observe':
        return None
    running = _meta(db)
    if running:
        return _reap(dispatcher, running, now)
    wc = dispatcher.rt.worker_config
    for d in waiting:
        if wc.intake_role(config, d['task_key']) not in ('inbox', 'oncall'):
            continue
        events = dispatcher._held_events(d['dispatch_id'])
        bursts = [dict(r) for r in db.execute(
            'SELECT * FROM intake_bursts WHERE event_id IN (%s) ORDER BY created_at' % ','.join('?' * len(events)),
            events)] if events else []
        if not bursts:
            continue
        return _start(dispatcher, d, events, bursts, config, now)
    return None


def _start(dispatcher, d, events, bursts, config, now):
    rt, db = dispatcher.rt, dispatcher.db
    provider = (json.loads(d['plan'] or '[]') or [{'provider': 'claude'}])[0].get('provider') or 'claude'
    model, effort, _ = rt.worker_config.model_for_task(config, provider, False, d['task_id'])
    texts = []
    for b in bursts:
        try:
            texts.append(burst_text(rt, b))
        except Exception as exc:  # noqa: BLE001 - an unreadable burst is recorded, never retried in a loop
            texts.append(f'(could not be read: {type(exc).__name__})')
    owner = rt.DEPLOYMENT.owner
    prompt = PROMPT.format(name=owner.short_name, language=owner.language,
                           kind=bursts[0]['conv_type'] or 'conversation', text='\n\n'.join(texts)[:MAX_TEXT])
    run_dir = Path(tempfile.mkdtemp(prefix=TRIAGE_DIR_PREFIX))
    if provider == 'claude':
        from kage.core.dispatch import PROBE_CLAUDE_ARGS
        cmd = dispatcher.core.build_command('claude', 'fresh', str(uuid.uuid4()), prompt, model, effort, (), run_dir, None,
                                            PROBE_CLAUDE_ARGS)
    else:
        from kage.core.dispatch import PROBE_CODEX_ARGS
        cmd = dispatcher.core.build_command('codex', 'fresh', None, prompt, model, effort, PROBE_CODEX_ARGS, run_dir)
    try:
        pid = dispatcher.spawn(cmd, str(run_dir), dispatcher._env('o' + uuid.uuid4().hex[:12], 'observe-triage', 0,
                                                                   provider, config), run_dir)
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    _save(db, {'pid': pid, 'pid_start': dispatcher.pid_start(pid), 'started_at': now, 'dir': str(run_dir),
               'dispatch_id': d['dispatch_id'], 'task_key': d['task_key'], 'provider': provider, 'events': events,
               'bursts': [b['burst_id'] for b in bursts]})
    return {'started': d['dispatch_id']}


def _reap(dispatcher, running, now):
    """The running call's answer once it exited (or was killed after TRIAGE_TIMEOUT_SECONDS); None while it runs."""
    db, run_dir = dispatcher.db, Path(running['dir'])
    exit_info = dispatcher._exit_info({'log_dir': run_dir})
    alive = exit_info is None and dispatcher.alive(running['pid'], running.get('pid_start'))
    if alive and now - running['started_at'] <= TRIAGE_TIMEOUT_SECONDS:
        return None
    if alive:
        try:
            dispatcher.killer(running['pid'], signal.SIGKILL)
        except OSError:
            pass
    child = dispatcher.children.pop(running['pid'], None) if hasattr(dispatcher, 'children') else None
    if child is not None:
        child.poll()
    text = ''
    for name in ('last.md', 'events.jsonl', 'stdout.log'):
        path = run_dir / name
        if path.is_file():
            text += path.read_text(encoding='utf-8', errors='replace')[-20000:] + '\n'
    answer = None if alive else parse(text)
    route, why = (answer['route'], answer['why']) if answer else ('unclear', 'timeout' if alive else 'no JSON answer')
    for burst_id in running['bursts']:
        _mode.shadow(db, 'triage', running['task_key'], json.dumps({'burst': burst_id, 'route': route, 'why': why},
                                                                   ensure_ascii=False), 'mode_observe', now=now)
    with db:
        if running['events']:
            db.execute("UPDATE events SET status='handled', disposition='observe' WHERE id IN (%s) AND status!='handled'"
                       % ','.join('?' * len(running['events'])), running['events'])
        db.execute("UPDATE dispatches SET state='done', outcome='observed', ended_at=?, updated_at=? WHERE dispatch_id=? "
                   "AND state='waiting'", (now, now, running['dispatch_id']))
    _save(db, None)
    if run_dir.name.startswith(TRIAGE_DIR_PREFIX) and run_dir.is_dir() and not run_dir.is_symlink():
        shutil.rmtree(run_dir, ignore_errors=True)
    return {'triaged': running['dispatch_id'], 'route': route}
