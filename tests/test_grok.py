import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from kage.adapters.providers import claude_delivery, grok, liveness, cache_report
from kage.core import dispatch, runlog, worker_config
from kage.ports.provider import Providers
from tests import test_worker_config, test_dispatch

SID = '00000000-0000-4000-8000-000000000123'


class GrokTest(unittest.TestCase):
    def test_command_and_results(self):
        provider = Providers(claude=lambda: None, codex=lambda: None)
        for mode, flag in [('fresh', '-s'), ('resume', '-r')]:
            cmd = provider.build_command('grok', mode, SID, 'hello', 'grok-4.7', 'xhigh', ['--always-approve'], '/r')
            self.assertEqual(cmd[cmd.index(flag) + 1], SID)
            self.assertEqual(cmd[cmd.index('--reasoning-effort') + 1], 'xhigh')
            self.assertIn('--no-auto-update', cmd)
        result = {'text': 'done', 'sessionId': SID, 'stopReason': 'EndTurn',
                  'usage': {'inputTokens': 30, 'outputTokens': 5, 'cacheReadInputTokens': 10}}
        for text in [json.dumps(result), json.dumps(result, indent=2), 'noise\n' + json.dumps(result) + '\n{']:
            info = provider.parse_run_log('grok', text)
            self.assertEqual((info['session'], info['final'], info['cli_ok']), (SID, 'done', True))
            self.assertEqual(info['usage'], result['usage'])
        self.assertIsNone(provider.parse_run_log('grok', '{')['cli_ok'])
        for error in [{'type': 'error', 'message': 'Not signed in'}, {'error': 'HTTP 429'}, {**result, 'stopReason': 'MaxTurns'}]:
            self.assertFalse(provider.parse_run_log('grok', json.dumps(error))['cli_ok'])
        info = provider.parse_run_log('grok', '{"error":"HTTP 503"}')
        self.assertEqual(dispatch.run_failure(info, {'code': 1})[0], 'transient')
        info = provider.parse_run_log('grok', '{"type":"error","message":"Not signed in"}')
        self.assertEqual(dispatch.run_failure(info, {'code': 1})[0], 'auth')
        with self.assertRaises(KeyError):
            provider._provider('unknown')

    def test_actor_and_environment_are_provider_scoped(self):
        with mock.patch.dict(os.environ, {'GROK_SESSION_ID': SID, 'CLAUDE_CODE_SESSION_ID': 'stale',
                                         'CODEX_THREAD_ID': 'stale'}, clear=True):
            self.assertEqual(claude_delivery.actor(), ('grok', SID))
        env = test_dispatch.core().strip_agent_env({'GROK_SESSION_ID': SID, 'GROK_AGENT': 'x',
                    'GROK_MODELS_BASE_URL': 'https://example.invalid/v1', 'XAI_API_KEY': 'test'})
        self.assertEqual(env, {'GROK_MODELS_BASE_URL': 'https://example.invalid/v1', 'XAI_API_KEY': 'test'})

    def test_config_preserves_old_routes_and_can_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            wc = test_worker_config.config(tmp)
            config, _ = wc.load()
            self.assertNotIn('grok', worker_config.fallback_after(config, 'claude'))
            self.assertEqual(worker_config.model_for(config, 'grok'), ('grok-4.7', 'high', ['--always-approve']))
            config['providers']['grok'] = copy.deepcopy(grok.DEFAULTS)
            self.assertNotIn('grok', worker_config.fallback_after(config, 'claude'))
            config['routing']['categories']['triage'] = 'grok'
            config['routing']['fallback_order'] = ['grok', 'claude', 'codex']
            wc.validate(config)
            self.assertEqual(worker_config.provider_order(config, 'triage'), ['grok', 'claude', 'codex'])
            self.assertEqual(worker_config.fallback_after(config, 'grok'), ['claude', 'codex'])
            config['providers']['grok']['effort'] = 'invalid'
            with self.assertRaises(ValueError):
                wc.validate(config)

    def test_usage_and_readable_log(self):
        result = {'text': 'finished', 'sessionId': SID, 'usage': {'inputTokens': 30, 'outputTokens': 5,
                                                               'cacheReadInputTokens': 10}}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'events.jsonl'; p.write_text(json.dumps(result))
            usage = cache_report.grok_usage(p)
            self.assertEqual((usage['input_tokens'], usage['cache_read_input_tokens'], usage['output_tokens']), (30, 10, 5))
        self.assertEqual(runlog.Renderer('grok').feed(result, 0)[0]['text'], 'finished')

    def test_liveness_missing_corrupt_live_dead_and_unknown(self):
        other = '00000000-0000-4000-8000-000000000456'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'active_sessions.json'
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, ''))
            probe = liveness.Probe(grok_home=tmp, runner=runner, ttl=0, proc_root=Path(tmp) / 'no-proc')
            self.assertEqual(probe.check('grok', SID)['state'], liveness.DEAD)
            path.write_text('{')
            self.assertEqual(probe.check('grok', SID)['state'], liveness.UNKNOWN)
            path.write_text(json.dumps([{'session_id': SID, 'pid': 777}]))
            runner.return_value.stdout = '777 /opt/bin/grok\n'
            self.assertEqual(probe.check('grok', SID)['state'], liveness.LIVE_BUSY)
            path.write_text('[]')
            self.assertEqual(probe.check('grok', SID)['state'], liveness.UNKNOWN)  # a TUI may still be starting
            runner.return_value.stdout = f'777 /opt/bin/grok -p work -s {other} --output-format json\n'
            self.assertEqual(probe.check('grok', SID)['state'], liveness.DEAD)  # another task's headless run
            runner.return_value.stdout += f'778 grok -p work -r {SID}\n'
            self.assertEqual(probe.check('grok', SID)['state'], liveness.LIVE_BUSY)
            runner.return_value.stdout = '777 /usr/bin/python grok\n'
            self.assertEqual(probe.check('grok', SID)['state'], liveness.DEAD)
            runner.return_value.returncode = 1
            self.assertEqual(probe.check('grok', SID)['state'], liveness.UNKNOWN)
            proc = Path(tmp) / 'proc'
            for pid, argv in (('self', ['python3']), ('41', ['/usr/local/bin/grok', '-p', 'x', '-s', other]),
                              ('42', ['/usr/local/bin/grok', '-p', 'x', '-r', SID])):
                (proc / pid).mkdir(parents=True)
                (proc / pid / 'cmdline').write_bytes('\0'.join(argv).encode() + b'\0')
            probe = liveness.Probe(grok_home=tmp, runner=runner, ttl=0, proc_root=proc)
            result = probe.check('grok', SID)
            self.assertEqual((result['state'], result['pid']), (liveness.LIVE_BUSY, 42))
            (proc / '42' / 'cmdline').unlink()
            self.assertEqual(probe.check('grok', SID)['state'], liveness.DEAD)

    def test_headless_only_delivery_never_uses_codex_queue(self):
        from tests.test_loop import core
        c = core(claude=claude_delivery, load=lambda db, wid: {'provider': 'grok'})
        runner = mock.Mock(side_effect=AssertionError('must not deliver to Codex'))
        self.assertIsNone(c.deliver(None, 'w', '/db', runner=runner))
        runner.assert_not_called()

    def test_health_tracks_grok_and_recovers(self):
        from tests.test_provider_health import store, runtime
        from kage.core import provider_health as ph
        db = store(); self.addCleanup(db.close)
        posts = []
        health = ph.ProviderHealth('en', post=lambda rt, db, p, kind, text: posts.append((p, kind)) or 'ts')
        for _ in range(2):
            db.execute("INSERT INTO consumer_runs VALUES ('t','grok','grok-4.7','ended',900,950,'failed','transient','event',0)")
        health.evaluate(runtime(posts), db, now=1000)
        self.assertEqual(ph.status_map(db)['grok'], 'degraded')
        self.assertEqual(posts, [('grok', 'degraded')])
        for now in (4000, 4100):
            health.evaluate(runtime(posts), db, now=now)
        self.assertEqual(ph.status_map(db)['grok'], 'healthy')
