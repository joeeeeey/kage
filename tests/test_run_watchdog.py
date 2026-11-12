"""0.79.2 (F26): the run watchdog. A Claude run with no first token `dispatch.ttft_seconds` after its CLI is ready, or
a Claude / Codex run whose retryable API errors do not recover within `dispatch.error_grace_seconds` (at any point of
the run), is stopped and its batch moves to the next provider of the plan at once; provider health counts the stop."""
import contextlib
import copy
import json
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest

from kage.adapters.providers.claude import Claude
from kage.core import dispatch as D
from kage.core import provider_health as H
from kage.core import runlog as R
from kage.core import worker_config as W
from kage.onboard import doctor
from tests import test_dispatch, test_grok_dispatch
from tests.test_triage_fallback import Assembled
from tests.test_worker_config import DEFAULTS, config as make_config


class WatchdogConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wc = make_config(self.tmp)

    def test_engine_defaults_apply_without_the_keys_and_the_keys_are_validated(self):
        config, _ = self.wc.load(self.tmp / 'none.json')
        self.assertNotIn('ttft_seconds', config['dispatch'])  # sparse: a deployment's DEFAULTS need not list them
        self.assertEqual(W.watchdog_settings(config), {'ttft_seconds': 10, 'error_grace_seconds': 10})
        path = self.tmp / 'c.json'
        for key in ('dispatch.ttft_seconds', 'dispatch.error_grace_seconds'):
            for good in ('0', '5', '30', '120'):
                self.wc.set_value(key, good, 'test', path=path, now=1700000000.0)
                self.assertEqual(W.watchdog_settings(self.wc.load(path)[0])[key.split('.')[1]], int(good))
            for bad in ('4', '121', '-1', 'true', '"10"', '10.5', 'null'):
                with self.assertRaises(ValueError, msg=(key, bad)):
                    self.wc.set_value(key, bad, 'test', path=path, now=1700000001.0)
        config = copy.deepcopy(DEFAULTS)
        config['dispatch']['ttft_surprise'] = 10
        with self.assertRaises(ValueError):
            self.wc.validate(config)


def feed(provider, events, start=1000.0):
    """The renderer's watchdog inputs after `events` (one per second from `start`)."""
    renderer = R.Renderer(provider)
    for i, event in enumerate(events):
        renderer.feed(event, start + i)
    return {k: renderer.snapshot()[k] for k in R.WATCH_KEYS}


class WatchInputsTest(unittest.TestCase):
    def test_claude_ready_first_token_and_retries(self):
        init = {'type': 'system', 'subtype': 'init', 'session_id': 's'}
        retry = {'type': 'system', 'subtype': 'api_retry', 'attempt': 1, 'error_status': 429, 'error': 'rate_limit'}
        token = {'type': 'stream_event', 'event': {'type': 'message_start'}}
        hooks = [{'type': 'system', 'subtype': 'hook_started'}, {'type': 'system', 'subtype': 'hook_response'}]
        self.assertEqual(feed('claude', hooks + [init]), {'ready_at': 1002.0, 'model_at': None, 'retry_at': None,
                                                          'retry_marker': None})
        self.assertEqual(feed('claude', [init, retry, retry]),
                         {'ready_at': 1000.0, 'model_at': None, 'retry_at': 1001.0, 'retry_marker': 'api_retry 429'})
        self.assertEqual(feed('claude', [init, retry, token]),
                         {'ready_at': 1000.0, 'model_at': 1002.0, 'retry_at': None, 'retry_marker': None})
        mid_run = feed('claude', [init, token, {'type': 'assistant', 'message': {'content': []}}, retry])
        self.assertEqual((mid_run['model_at'], mid_run['retry_at']), (1001.0, 1003.0))
        api_error = {'type': 'assistant', 'error': 'rate_limit', 'message': {'content': [{'type': 'text', 'text': 'x'}]}}
        self.assertIsNone(feed('claude', [init, api_error])['model_at'])

    def test_codex_reconnects_count_and_warnings_do_not(self):
        warning = {'type': 'item.completed', 'item': {'type': 'error', 'message': 'Model metadata for `m` not found.'}}
        reconnect = {'type': 'error', 'message': 'Reconnecting... 1/5 (unexpected status 503 Service Unavailable)'}
        item = {'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'ls'}}
        self.assertEqual(feed('codex', [{'type': 'thread.started'}, warning, {'type': 'turn.started'}]),
                         {'ready_at': 1002.0, 'model_at': None, 'retry_at': None, 'retry_marker': None})
        state = feed('codex', [{'type': 'turn.started'}, reconnect, reconnect])
        self.assertEqual((state['retry_at'], state['retry_marker']), (1001.0, 'reconnect'))
        self.assertEqual(feed('codex', [{'type': 'turn.started'}, reconnect, item])['retry_at'], None)
        self.assertIsNone(feed('codex', [{'type': 'error', 'message': 'This session was recorded with model x'}])['retry_at'])

    def test_grok_has_nothing_to_watch(self):
        self.assertEqual(feed('grok', [{'text': 'ok', 'sessionId': 's', 'stopReason': 'EndTurn'}]),
                         dict.fromkeys(R.WATCH_KEYS))


class WatchdogDispatcherTest(unittest.TestCase):
    """The decision (`_watchdog`) and the move (`_after_failure`) on a dispatcher over an in-memory store."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('CREATE TABLE events (id TEXT PRIMARY KEY, watch_id TEXT, status TEXT, receipt TEXT, '
                              'received REAL); CREATE TABLE tasks (task_id TEXT PRIMARY KEY, current_source TEXT);')
        D.initialize(self.db)
        self.raw = copy.deepcopy(DEFAULTS)
        self.raw['dispatch'].update(enabled=True)
        (self.tmp / 'config.json').write_text(json.dumps(self.raw))
        wc = W.WorkerConfig('en', defaults=DEFAULTS, config_env='KAGE_TEST_WORKER_CONFIG',
                            default_path=self.tmp / 'config.json', owner_key='owner', owner_name='Mira')
        self.rt = types.SimpleNamespace(__file__='/nonexistent/runtime.py', liveness=None, worker_config=Assembled(wc),
                                        provider_health=types.SimpleNamespace(status_map=lambda db: {}),
                                        run_log=R.RunLog(), _meta_error=lambda *a: None, _meta_note=lambda *a: None,
                                        transaction=lambda db: contextlib.nullcontext())
        claude = Claude()
        self.d = D.Dispatcher(self.db, '/tmp/kage-test.sqlite', self.rt, probe=object(), spawn=lambda *a: 1,
                              pid_start=lambda pid: None, killer=lambda pid, sig: None, clock=lambda: 100.0,
                              core=test_dispatch.core(parse_run_log=lambda provider, *a: claude.parse_run_log(*a)))
        self.d._card_status = lambda *a, **kw: None
        self.runs = 0

    def run_row(self, provider, events, plan, step=0, partial=True):
        self.runs += 1
        run_dir = self.tmp / f'run-{self.runs}'
        run_dir.mkdir(mode=0o700)
        (run_dir / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
        argv = [provider, '-p', '<prompt.md>'] + ([D.PARTIAL_MESSAGES_ARG] if partial else [])
        (run_dir / 'meta.json').write_text(json.dumps({'argv_without_prompt': argv}))
        self.db.execute("INSERT INTO dispatches (dispatch_id, task_key, task_id, task_path, state, trigger, plan, step, "
                        "run_id, created_at, updated_at) VALUES ('d1', 't', 't', '/x', 'running', 'event', ?, ?, 'r1', 0, 0)",
                        (json.dumps([{'provider': p, 'mode': 'fresh', 'session': None} for p in plan]), step))
        self.db.execute("INSERT INTO consumer_runs (run_id, dispatch_id, task, provider, mode, trigger, event_ids, state, "
                        "started_at, log_dir) VALUES ('r1', 'd1', 't', ?, 'fresh', 'event', '[]', 'running', 0, ?)",
                        (provider, str(run_dir)))
        return dict(self.db.execute("SELECT * FROM consumer_runs WHERE run_id='r1'").fetchone())

    def decide(self, run, seconds_after):
        state = self.rt.run_log.update(run['log_dir'], run['provider'], now=1000.0)
        return self.d._watchdog(run, state, 1000.0 + seconds_after)

    INIT = {'type': 'system', 'subtype': 'init', 'session_id': 's'}
    RETRY = {'type': 'system', 'subtype': 'api_retry', 'attempt': 1, 'error_status': 429, 'error': 'rate_limit'}

    def test_a_claude_run_without_a_first_token_gives_way_after_ttft(self):
        run = self.run_row('claude', [self.INIT], ['claude', 'grok', 'codex'])
        self.assertIsNone(self.decide(run, 9))
        self.assertEqual(self.decide(run, 10), 'ttft_timeout')

    def test_a_first_token_ends_the_ttft_clock(self):
        run = self.run_row('claude', [self.INIT, {'type': 'stream_event', 'event': {'type': 'message_start'}}],
                           ['claude', 'grok'])
        self.assertIsNone(self.decide(run, 300))

    def test_ttft_needs_the_stream_events_flag_and_never_applies_to_codex(self):
        run = self.run_row('claude', [self.INIT], ['claude', 'grok'], partial=False)
        self.assertIsNone(self.decide(run, 300))  # a run launched without stream events cannot show its first token
        self.db.execute('DELETE FROM dispatches'), self.db.execute('DELETE FROM consumer_runs')
        run = self.run_row('codex', [{'type': 'turn.started'}], ['codex', 'claude'])
        self.assertIsNone(self.decide(run, 300))  # Codex reports a step only once it is complete

    def test_retries_that_do_not_recover_give_way_after_the_grace_mid_run_too(self):
        token = {'type': 'stream_event', 'event': {'type': 'message_start'}}
        run = self.run_row('claude', [self.INIT, token, {'type': 'user'}, self.RETRY], ['claude', 'grok'])
        self.assertIsNone(self.decide(run, 9))
        self.assertEqual(self.decide(run, 10), 'error_grace')
        self.db.execute('DELETE FROM dispatches'), self.db.execute('DELETE FROM consumer_runs')
        reconnect = {'type': 'error', 'message': 'Reconnecting... 2/5 (unexpected status 502 Bad Gateway)'}
        run = self.run_row('codex', [{'type': 'turn.started'}, reconnect], ['codex', 'claude', 'grok'])
        self.assertEqual(self.decide(run, 11), 'error_grace')

    def test_off_and_the_last_provider_are_never_stopped(self):
        run = self.run_row('claude', [self.INIT, self.RETRY], ['claude', 'claude'])
        self.assertIsNone(self.decide(run, 600))  # no later step on another provider
        self.db.execute('DELETE FROM dispatches'), self.db.execute('DELETE FROM consumer_runs')
        run = self.run_row('claude', [self.INIT, self.RETRY], ['codex', 'claude'], step=1)
        self.assertIsNone(self.decide(run, 600))
        self.db.execute('DELETE FROM dispatches'), self.db.execute('DELETE FROM consumer_runs')
        self.raw['dispatch'].update(ttft_seconds=0, error_grace_seconds=0)
        (self.tmp / 'config.json').write_text(json.dumps(self.raw))
        run = self.run_row('claude', [self.INIT, self.RETRY], ['claude', 'grok'])
        self.assertIsNone(self.decide(run, 600))

    def test_a_stopped_run_moves_its_batch_to_the_next_provider_at_once(self):
        self.raw['dispatch']['retry_backoff_seconds'] = [600, 600]
        (self.tmp / 'config.json').write_text(json.dumps(self.raw))
        run = self.run_row('claude', [self.INIT, self.RETRY], ['claude', 'claude', 'grok', 'codex'])
        self.assertEqual(self.d._after_failure(self.d._dispatch('d1'), run, 'error_grace', 100.0), 'fallback')
        d = self.d._dispatch('d1')
        plan = json.loads(d['plan'])
        self.assertEqual((d['state'], d['step'], d['next_at'], d['retries']), ('waiting', 2, 100.0, 0))
        self.assertEqual(plan[2]['why'], 'watchdog_takeover')  # the takeover's session note
        self.assertNotIn('why', plan[1])
        self.assertEqual(json.loads(d['failures'])[-1]['error_class'], 'error_grace')
        self.assertIn('watchdog_takeover', __import__('kage.core.prompts', fromlist=['x']).SESSION_NOTES)

    def test_without_another_provider_a_stop_is_handled_as_an_overload(self):
        run = self.run_row('claude', [self.INIT], ['claude'])
        self.assertEqual(self.d._after_failure(self.d._dispatch('d1'), run, 'ttft_timeout', 100.0), 'retry_same')

    def test_finalize_records_the_watchdog_class_and_marker(self):
        run = self.run_row('claude', [self.INIT, self.RETRY], ['claude', 'grok'])
        self.d.release_lease = lambda *a: None
        self.d._signal(run, signal.SIGTERM, 5, 100.0)
        Path(run['log_dir'], 'exit.json').write_text(json.dumps({'exit_code': -15, 'signal': 15, 'stopped': True}))
        self.assertEqual(self.d.finalize('r1', now=101.0), 'failed')
        row = self.db.execute("SELECT outcome, error_class, error_marker FROM consumer_runs WHERE run_id='r1'").fetchone()
        self.assertEqual(tuple(row), ('failed', 'error_grace', 'api_retry 429'))
        self.assertEqual(self.d._dispatch('d1')['step'], 1)

    def test_provider_health_counts_the_stops_and_the_overview_lists_them(self):
        self.assertIn('ttft_timeout', H.PROVIDER_FAILURES)
        self.assertIn('error_grace', H.PROVIDER_FAILURES)
        self.assertIn('error_grace', H.OVERLOAD_FAILURES)
        self.assertIn('ttft_timeout', D.PROVIDER_WAIT)
        for i, cls in enumerate(('ttft_timeout', 'error_grace', 'error_grace')):
            self.db.execute("INSERT INTO consumer_runs (run_id, task, provider, mode, trigger, event_ids, state, ended_at, "
                            "outcome, error_class) VALUES (?, 't', 'claude', 'fresh', 'event', '[]', 'ended', ?, 'failed', ?)",
                            (f'w{i}', 5000.0 + i, cls))
        ph = dict(DEFAULTS['dispatch']['provider_health'])
        counts = H.window(self.db, 'claude', 5100.0, ph)
        self.assertEqual((counts['adverse'], counts['failed'], counts['overload']), (3, 3, 2))
        overview = D.watchdog_overview(self.db, self.raw, 5100.0)
        self.assertEqual(overview['stops_24h'], 3)
        self.assertEqual((overview['ttft_seconds'], overview['error_grace_seconds']), (10, 10))
        line = next(c for c in doctor.runtime_checks(None, {'watchdog': overview}, doctor.Probe())
                    if c['id'] == 'runtime.watchdog')
        self.assertIn('first token within 10 s', line['detail'])
        self.assertIn('3 runs moved', line['detail'])


# The fake Claude of test_dispatch_run, slowed down per task: `.no-first-token` = the CLI is ready, then nothing comes;
# `.retry-forever` = a relay answering 429 to every attempt (the CLI keeps retrying); `.retry-then-ok` = one 429, then
# the answer within the grace. Every line is flushed, as the real CLI streams them.
SLOW_CLAUDE = test_grok_dispatch.test_dispatch_run.FAKE_CLAUDE.replace(
    '''args = sys.argv[1:]
''', '''args = sys.argv[1:]
import time
from pathlib import Path
if '-p' in args and args[:1] != ['agents'] and '--version' not in args:
    with open(Path.home() / 'claude-argv.jsonl', 'a') as log:
        log.write(json.dumps([a for a in args if not a.startswith('[') and len(a) < 200]) + '\\n')
    sid = args[args.index('--resume') + 1] if '--resume' in args else args[args.index('--session-id') + 1]
    retry = {'type': 'system', 'subtype': 'api_retry', 'attempt': 1, 'max_retries': 10, 'retry_delay_ms': 1000,
             'error_status': 429, 'error': 'rate_limit'}
    if Path('.no-first-token').exists() or Path('.retry-forever').exists():
        print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)
        while True:
            if Path('.retry-forever').exists():
                print(json.dumps(retry), flush=True)
            time.sleep(1)
    if Path('.retry-then-ok').exists():
        print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)
        print(json.dumps(retry), flush=True)
        time.sleep(2)
        print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_start'}}), flush=True)
''', 1)


class WatchdogRunTest(test_grok_dispatch.GrokDispatchTest):
    """Generated CLI, supervisor, runner and fake binaries: the stop and the takeover end to end."""
    test_a_launch_is_dispatched_through_the_generated_runner = None  # the inherited tests run in their own modules
    test_b_provider_failures_fall_back_to_grok_and_task_failures_do_not = None

    def test_a_watchdog_stops_slow_claude_runs_and_grok_takes_over(self):
        self.assertIn('.retry-forever', SLOW_CLAUDE)
        (self.bin / 'claude').write_text(SLOW_CLAUDE)
        (self.bin / 'claude').chmod(0o755)
        self.ok('config', 'set', 'dispatch.enabled', 'true')
        self.ok('config', 'set', 'dispatch.retry_backoff_seconds', '[600, 600]')
        self.ok('config', 'set', 'routing.fallback_order', '["claude", "grok"]')
        self.ok('config', 'set', 'dispatch.ttft_seconds', '5')
        self.ok('config', 'set', 'dispatch.error_grace_seconds', '5')
        prompt = self.tmp / 'prompt.md'; prompt.write_text('Answer the question.\n')
        tasks = {}
        for name, marker in (('t-retry', '.retry-forever'), ('t-silent', '.no-first-token'), ('t-recovers', '.retry-then-ok')):
            tasks[name] = task = self.home / 'kage-tasks' / name
            task.mkdir(parents=True)
            (task / 'goal.md').write_text('# Goal\nWatchdog acceptance.\n')
            (task / 'status.md').write_text('<!-- rev: 1 -->\n# Status\n')
            (task / marker).touch()
        log = open(self.tmp / 'supervisor.log', 'w'); self.addCleanup(log.close)
        supervisor = subprocess.Popen([sys.executable, str(self.target / 'scripts/kage.py'), 'run'], env=self.env,
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=str(self.tmp))
        self.addCleanup(lambda: (supervisor.terminate(), supervisor.wait(20)))
        self.wait_for_supervisor(supervisor)
        from kage.core.mode import KEY
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE runtime_meta SET value=? WHERE key=?', ('active', KEY))
        for name in tasks:
            self.ok('launch-task', '--task', str(tasks[name]), '--provider', 'claude', '--prompt-file', str(prompt),
                    '--wait-seconds', '60')

        for name, error_class in (('t-retry', 'error_grace'), ('t-silent', 'ttft_timeout')):
            runs = self.wait_runs(supervisor, name, lambda rows: len(rows) >= 2 and rows[-1]['state'] == 'ended')
            self.assertEqual([(r['provider'], r['outcome'], r['error_class']) for r in runs],
                             [('claude', 'failed', error_class), ('grok', 'success', None)], self.diag())
            claude, grok = runs
            self.assertLess(claude['ended_at'] - claude['started_at'], 30, self.diag())   # not the CLI's own retries
            self.assertLess(grok['started_at'] - claude['ended_at'], 10, self.diag())     # no same-provider backoff
            self.assertEqual(grok['route_reason'], 'watchdog_takeover')
            context = ''.join(p.read_text() for p in Path(grok['log_dir']).glob('*.md'))
            self.assertIn('stopped by the dispatcher because its provider was too slow', context)
        runs = self.wait_runs(supervisor, 't-recovers', lambda rows: rows and rows[-1]['state'] == 'ended')
        self.assertEqual([(r['provider'], r['outcome']) for r in runs], [('claude', 'success')], self.diag())
        argv = [json.loads(line) for line in (self.home / 'claude-argv.jsonl').read_text().splitlines()]
        self.assertTrue(argv and all(D.PARTIAL_MESSAGES_ARG in a for a in argv), argv)
        health = json.loads(self.ok('health').stdout)
        self.assertEqual(health['watchdog']['stops_24h'], 2, health.get('watchdog'))


if __name__ == '__main__':
    unittest.main()
