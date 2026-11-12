"""0.79.1: Grok as a fallback. The intake triage task's own fallback (`triage.fallback`, first after its provider, with its
model and effort) and the executors' last fallback (`routing.fallback_order` ending in grok, no code needed)."""
import contextlib
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import types
import unittest

from kage.core import dispatch as D
from kage.core import worker_config as W
from tests import test_dispatch
from tests.test_worker_config import DEFAULTS, config as make_config

TRIAGE = {'provider': 'grok', 'model': 'grok-4.7', 'effort': 'high'}


class Assembled:
    """What a deployment's `rt.worker_config` is: the assembled module, i.e. the bound WorkerConfig methods plus the
    module-level functions (`fallback_after`, `PROVIDERS`, ...)."""

    def __init__(self, wc):
        self.wc = wc

    def __getattr__(self, name):
        return getattr(self.wc, name) if hasattr(self.wc, name) else getattr(W, name)


class TriageConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wc = make_config(self.tmp)

    def test_without_the_section_a_config_reads_and_routes_as_before(self):
        config, _ = self.wc.load(self.tmp / 'none.json')
        self.assertNotIn('triage', config)
        self.assertIsNone(W.triage_fallback(config, 'inbox'))
        self.assertEqual(self.wc.model_for_task(config, 'codex', task_id='example-inbox'),
                         W.model_for(config, 'codex'))

    def test_the_section_is_validated_and_set_like_control(self):
        path = self.tmp / 'c.json'
        self.wc.set_value('triage.fallback', json.dumps(TRIAGE), 'test', path=path, now=1700000000.0)
        config, _ = self.wc.load(path)
        self.assertEqual(config['triage'], {'fallback': TRIAGE})  # sparse: the default roles are not written
        self.assertEqual(W.triage_fallback(config, 'inbox'), TRIAGE)
        self.assertIsNone(W.triage_fallback(config, 'oncall'))   # the inbox only, unless the roles say so
        self.assertIsNone(W.triage_fallback(config, None))
        self.wc.set_value('triage.roles', '["inbox", "oncall"]', 'test', path=path, now=1700000001.0)
        self.assertEqual(W.triage_fallback(self.wc.load(path)[0], 'oncall'), TRIAGE)
        self.assertEqual({c['key'] for c in self.wc.change_log(path)}, {'triage.fallback', 'triage.roles'})
        self.wc.set_value('triage.fallback', '{"provider": "grok"}', 'test', path=path, now=1700000002.0)
        self.assertEqual(W.triage_fallback(self.wc.load(path)[0], 'inbox'),
                         {'provider': 'grok', 'model': None, 'effort': None})
        self.wc.set_value('triage.fallback', 'null', 'test', path=path, now=1700000003.0)
        self.assertIsNone(W.triage_fallback(self.wc.load(path)[0], 'inbox'))
        for key, value in (('triage.fallback', '{"provider": "gemini"}'), ('triage.fallback', '{"model": "grok-4.7"}'),
                           ('triage.fallback', '{"provider": "grok", "effort": "extreme"}'),
                           ('triage.fallback', '{"provider": "grok", "model": "bad model"}'),
                           ('triage.fallback', '{"provider": "grok", "retries": 2}'), ('triage.fallback', '"grok"'),
                           ('triage.roles', '[]'), ('triage.roles', '["inbox", "inbox"]'), ('triage.roles', '["control"]'),
                           ('triage.surprise', '1')):
            with self.assertRaises(ValueError, msg=(key, value)):
                self.wc.set_value(key, value, 'test', path=path, now=1700000004.0)

    def test_the_fallback_runs_with_its_own_model_and_effort(self):
        config = copy.deepcopy(DEFAULTS)
        config['triage'] = {'fallback': TRIAGE}
        self.wc.validate(config)
        model = self.wc.model_for_task
        self.assertEqual(model(config, 'grok', task_id='example-inbox'), ('grok-4.7', 'high', ['--always-approve']))
        intake = DEFAULTS['intake']
        self.assertEqual(model(config, 'claude', task_id='example-inbox')[:2],  # its own provider: as before
                         (intake['inbox_model'], intake['inbox_effort']))
        self.assertEqual(model(config, 'codex', task_id='example-inbox'), W.model_for(config, 'codex'))
        config['triage']['fallback'] = {'provider': 'grok', 'model': 'grok-test-triage', 'effort': 'low'}
        config['providers']['grok'] = {'model': 'grok-test-default', 'effort': 'max', 'yolo_args': ['--always-approve']}
        self.assertEqual(model(config, 'grok', task_id='example-inbox')[:2], ('grok-test-triage', 'low'))
        self.assertEqual(model(config, 'grok', task_id='example-oncall')[:2], ('grok-test-default', 'max'))
        self.assertEqual(model(config, 'grok', task_id='release-task')[:2], ('grok-test-default', 'max'))
        config['triage']['fallback'] = {'provider': 'grok'}
        self.assertEqual(model(config, 'grok', task_id='example-inbox')[:2], ('grok-test-default', 'max'))


class FallbackPlanTest(unittest.TestCase):
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
        self.health = {}
        wc = W.WorkerConfig('en', defaults=DEFAULTS, config_env='KAGE_TEST_WORKER_CONFIG',
                            default_path=self.tmp / 'config.json', owner_key='owner', owner_name='Mira')
        rt = types.SimpleNamespace(__file__='/nonexistent/runtime.py', liveness=None, worker_config=Assembled(wc),
                                   provider_health=types.SimpleNamespace(status_map=lambda db: self.health),
                                   _meta_error=lambda *a: None, transaction=lambda db: contextlib.nullcontext())
        self.d = D.Dispatcher(self.db, '/tmp/kage-test.sqlite', rt, probe=object(), spawn=lambda *a: 1,
                              pid_start=lambda pid: None, killer=lambda pid, sig: None, clock=lambda: 100.0,
                              core=test_dispatch.core())

    def plan(self, task_id, category=None, current=None):
        (self.tmp / 'config.json').write_text(json.dumps(self.raw))
        self.db.execute('DELETE FROM dispatch_prefs')
        if category:
            self.db.execute("INSERT INTO dispatch_prefs (task_key, provider, important, category, source, updated_at) "
                            "VALUES (?, NULL, 0, ?, 'test', 1)", (task_id, category))
        steps = self.d.plan_for({'key': task_id, 'task_id': task_id}, current, self.d.config())
        return [(s['provider'], s['mode']) for s in steps]

    def providers(self, *args, **kw):
        return [p for p, _ in self.plan(*args, **kw)]

    def test_executors_end_on_grok_once_the_order_lists_it(self):
        self.assertEqual(self.providers('release-task', 'infra'), ['codex', 'claude'])  # 0.79.0 config: as before
        self.raw['routing']['fallback_order'] = ['codex', 'claude', 'grok']
        self.assertEqual(self.providers('release-task', 'infra'), ['codex', 'claude', 'grok'])
        self.assertEqual(self.providers('docs-task', 'docs'), ['claude', 'codex', 'grok'])
        self.assertEqual(self.plan('release-task', 'infra', current=('codex', 'thread-1')),
                         [('codex', 'resume'), ('claude', 'fresh'), ('grok', 'fresh')])
        self.assertEqual(self.providers('example-inbox'), ['claude', 'codex', 'grok'])  # no triage section

    def test_the_triage_task_tries_its_fallback_first(self):
        self.assertEqual(self.providers('example-inbox'), ['claude', 'codex'])
        self.raw['triage'] = {'fallback': TRIAGE}
        self.assertEqual(self.providers('example-inbox'), ['claude', 'grok', 'codex'])
        self.assertEqual(self.providers('example-oncall'), ['codex', 'claude'])  # not one of the roles
        self.assertEqual(self.providers('release-task', 'infra'), ['codex', 'claude'])
        self.raw['triage']['roles'] = ['inbox', 'oncall']
        self.assertEqual(self.providers('example-oncall'), ['codex', 'grok', 'claude'])
        self.raw['routing']['fallback_order'] = ['codex', 'claude', 'grok']
        self.assertEqual(self.providers('example-inbox'), ['claude', 'grok', 'codex'])  # never twice
        self.raw['triage']['fallback'] = {'provider': 'claude'}
        self.assertEqual(self.providers('example-inbox'), ['claude', 'codex', 'grok'])  # its own provider: no change

    def test_a_degraded_fallback_moves_last_and_a_degraded_triage_provider_reroutes_to_it(self):
        self.raw['triage'] = {'fallback': TRIAGE}
        self.health = {'grok': D.DEGRADED}
        self.assertEqual(self.providers('example-inbox'), ['claude', 'codex', 'grok'])
        self.health = {'claude': D.DEGRADED}
        self.assertEqual(self.providers('example-inbox'), ['grok', 'codex', 'claude'])

    def test_a_task_failure_never_falls_back_and_provider_failures_do(self):
        """The classification is unchanged: provider-side classes move on to the next step (Grok last), a run that
        ended cleanly with its events unresolved (tests failing, a refused action) does not."""
        self.assertEqual(D.classify_failure('API Error: 529 overloaded')[0], 'transient')
        self.assertEqual(D.classify_failure('unexpected status 503 Service Unavailable')[0], 'transient')
        self.assertEqual(D.classify_failure('HTTP 429 Too Many Requests')[0], 'rate_limited')
        self.assertIn('transient', D.RETRY_SAME)
        self.assertIn('rate_limited', D.RETRY_SAME)
        self.assertIn('quota', D.SWITCH_NOW)
        self.assertNotIn('task_unresolved', D.RETRY_SAME + D.SWITCH_NOW)
        self.assertEqual(D.NO_SWITCH, ('auth', 'model_unsupported', 'spawn_failed', 'run_timeout'))


if __name__ == '__main__':
    unittest.main()
