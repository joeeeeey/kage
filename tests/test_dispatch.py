import types
import unittest
import zoneinfo

from kage.core import dispatch as D


def core(language='en', **over):
    args = dict(tz=zoneinfo.ZoneInfo('Asia/Shanghai'), tz_name='Asia/Shanghai', owner_key='owner', owner_name='Mira',
                run_id_env='TEST_RUN_ID', task_env='TEST_TASK', hops_env='TEST_HOPS', run_context_env='TEST_RUN_CONTEXT',
                probe_dir_prefix='test-probe-', runner='/nonexistent/runner.py', build_command=lambda *a, **kw: ['true'],
                parse_run_log=lambda *a, **kw: {}, prompt_layout=lambda *a, **kw: {}, lean_claude=lambda *a, **kw: None,
                claude_settings=lambda *a, **kw: {}, rc_exports=lambda *a, **kw: {}, owner_user=lambda rt: 'U0000TEST01',
                ask_owner=lambda rt: None)
    return D.DispatchCore(language, **{**args, **over})


class DispatchCoreTest(unittest.TestCase):
    def test_failure_classes(self):
        self.assertEqual(D.classify_failure('HTTP 429 Too Many Requests')[0], 'rate_limited')
        self.assertEqual(D.classify_failure('You have hit your weekly limit')[0], 'quota')
        self.assertEqual(D.classify_failure('rmcp::transport 401 unauthorized\nstream disconnected')[0], 'transient')
        self.assertEqual(D.classify_failure('unexpected status 502; 401 unauthorized')[0], 'transient')
        self.assertEqual(D.classify_failure('nothing useful'), ('unknown', None))
        self.assertEqual(D.run_failure({'errors': [], 'retry_categories': ['overloaded']}, {'code': 1})[0], 'transient')
        self.assertEqual(D.run_failure({'errors': [], 'retry_categories': []}, None)[0], 'crashed')

    def test_questions_in_the_owner_language(self):
        c = core()
        self.assertIsNotNone(c.final_question('Done with the draft.\n\nShould I merge it now?'))
        self.assertIsNotNone(c.final_question('All set; waiting for owner to sign off'))  # the owner's key
        self.assertIsNone(c.final_question('Merged and deployed.'))
        question, options = c.hook_question('ExitPlanMode', {'plan': 'Step 1'})
        self.assertTrue(question.startswith('A background session wants to carry out the plan'))
        self.assertEqual([o.split('=')[0] for o in options], ['approve', 'reject'])
        zh_question, _ = core('zh-CN').hook_question('ExitPlanMode', {'plan': 'Step 1'})
        self.assertTrue(zh_question.startswith('后台会话想按下面这个计划执行'))
        _, options = c.hook_question('AskUserQuestion', {'questions': [{'question': 'Which?', 'options': ['a']}]})
        self.assertEqual(options, ['ok=Go on as you suggest:approve', "reply=I'll reply in the thread"])

    def test_cards_labels_and_env(self):
        c = core()
        blocks = c.run_card_blocks('text', 'status', 'd1')
        labels = [e['text']['text'] for b in blocks if b['type'] == 'actions' for e in b['elements']]
        self.assertEqual(labels, ['⏹ Stop', '📺 Watch in iTerm', 'View'])
        self.assertEqual(c.trigger_summary(['slack_message', 'slack_message', 'note']), 'Slack message × 2, task note × 1')
        self.assertEqual(c._waited(7200), 'waited 2 h')
        env = c.strip_agent_env({'TEST_RUN_ID': 'r1', 'CLAUDE_CODE_X': '1', 'CODEX_THREAD_ID': 't', 'PATH': '/bin'})
        self.assertEqual(env, {'PATH': '/bin'})


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('CREATE TABLE events (id TEXT PRIMARY KEY, watch_id TEXT, status TEXT, receipt TEXT, '
                              'received REAL);')  # the runtime's table the dispatcher indexes
        self.core = core()
        D.initialize(self.db)

    def test_the_store_follows_the_owner_key(self):
        self.assertIn("'needs_owner'", self.core.holding.__repr__())
        self.assertEqual((self.core.takeover_actor, self.core.takeover_reason), ('owner-terminal', 'owner_takeover'))
        self.assertTrue({'dispatches', 'consumer_runs', 'consumer_leases'} <= {
            r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")})

    def test_a_dispatcher_holds_the_core_and_its_seams(self):
        rt = types.SimpleNamespace(__file__='/nonexistent/runtime.py', liveness=None)
        d = D.Dispatcher(self.db, '/tmp/kage-test.sqlite', rt, probe=object(), spawn=lambda *a: 1,
                         pid_start=lambda pid: None, killer=lambda pid, sig: None, clock=lambda: 1.0, core=self.core)
        self.assertIs(d.core, self.core)
        self.assertEqual(d.runs_root.name, 'runs')
        self.assertFalse(d.alive(None, None))
        self.assertTrue(isinstance(D.Dispatcher.__dict__['_step'], staticmethod))

    def test_an_install_hold_starts_only_the_control_entrys_runs(self):
        """C4: dispatch.install_hold — an install's quiet moment: waiting dispatches stay waiting and nothing is revived,
        except the control task's (the owner's control messages keep being answered)."""
        import json
        import tempfile
        from pathlib import Path
        from kage.core import worker_config as W
        defaults = json.loads((Path(__file__).with_name('fixtures') / 'worker_defaults.json').read_text())
        tmp = tempfile.mkdtemp()
        raw = json.loads(json.dumps(defaults))
        raw['dispatch'].update(enabled=True, install_hold=True)
        raw['control'] = {'enabled': True}
        (Path(tmp) / 'config.json').write_text(json.dumps(raw))
        wc = W.WorkerConfig('en', defaults=defaults, config_env='KAGE_TEST_WORKER_CONFIG',
                            default_path=Path(tmp) / 'config.json', owner_key='owner', owner_name='Mira')
        import contextlib
        rt = types.SimpleNamespace(__file__='/nonexistent/runtime.py', liveness=None, worker_config=wc,
                                   _meta_error=lambda *a: None, transaction=lambda db: contextlib.nullcontext())
        d = D.Dispatcher(self.db, '/tmp/kage-test.sqlite', rt, probe=object(), spawn=lambda *a: 1,
                         pid_start=lambda pid: None, killer=lambda pid, sig: None, clock=lambda: 100.0, core=self.core)
        config = d.config()
        self.assertEqual((d.held_for_install(config, 'owner-inbox'), d.held_for_install(config, 'release-task'),
                          d.held_for_install(config, 'owner-control')), (True, True, False))
        for i, task in enumerate(('release-task', 'owner-control')):
            self.db.execute("INSERT INTO dispatches (dispatch_id, task_key, task_id, task_path, state, trigger, plan, "
                            "created_at, updated_at) VALUES (?, ?, ?, '/x', 'waiting', 'event', '[]', ?, ?)",
                            (f'd{i}', task, task, i, i))
        started = []
        d.start = lambda dispatch_id, now=None: started.append(dispatch_id)
        d.cycle(now=100.0)
        self.assertEqual(started, ['d1'])
        self.assertEqual(d.sweep_stranded(now=100.0), [])  # nothing revived while held
        raw['dispatch']['install_hold'] = False
        (Path(tmp) / 'config.json').write_text(json.dumps(raw))
        self.assertFalse(d.held_for_install(d.config(), 'release-task'))
        started.clear()
        d.cycle(now=101.0)
        self.assertEqual(started, ['d0', 'd1'])

    def test_outside_active_mode_no_run_starts_and_what_would_have_is_recorded(self):
        """O2b: the instance mode gates every start; a store without the row (predates O2) starts as before."""
        import contextlib
        import json
        import tempfile
        from pathlib import Path
        from kage.core import mode as M
        from kage.core import worker_config as W
        defaults = json.loads((Path(__file__).with_name('fixtures') / 'worker_defaults.json').read_text())
        tmp = tempfile.mkdtemp()
        raw = json.loads(json.dumps(defaults))
        raw['dispatch'].update(enabled=True)
        (Path(tmp) / 'config.json').write_text(json.dumps(raw))
        wc = W.WorkerConfig('en', defaults=defaults, config_env='KAGE_TEST_WORKER_CONFIG',
                            default_path=Path(tmp) / 'config.json', owner_key='owner', owner_name='Mira')
        rt = types.SimpleNamespace(__file__='/nonexistent/runtime.py', liveness=None, worker_config=wc,
                                   _meta_error=lambda *a: None, transaction=lambda db: contextlib.nullcontext())
        d = D.Dispatcher(self.db, '/tmp/kage-test.sqlite', rt, probe=object(), spawn=lambda *a: 1,
                         pid_start=lambda pid: None, killer=lambda pid, sig: None, clock=lambda: 100.0, core=self.core)
        self.db.execute("INSERT INTO dispatches (dispatch_id, task_key, task_id, task_path, state, trigger, reason, plan, "
                        "created_at, updated_at) VALUES ('d0', 'release-task', 'release-task', '/x', 'waiting', 'event', "
                        "'events', '[]', 1, 1)")
        started = []
        d.start = lambda dispatch_id, now=None: started.append(dispatch_id)
        self.db.execute('CREATE TABLE IF NOT EXISTS runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        M.write(self.db, 'installed', 'init')
        M.write(self.db, 'observe', 'services')
        d.cycle(now=100.0)
        d.cycle(now=101.0)
        self.assertEqual(started, [])
        shadow = M.overview(self.db)['shadow']
        self.assertEqual((shadow['counts'], shadow['recent'][0]['target']), ({'dispatch': 1}, 'release-task'))
        M.write(self.db, 'active', 'owner_button')
        d.cycle(now=102.0)
        self.assertEqual(started, ['d0'])
        self.db.execute("DELETE FROM runtime_meta WHERE key='instance.mode'")
        started.clear()
        d.cycle(now=103.0)
        self.assertEqual(started, ['d0'])  # no row: as before O2


if __name__ == '__main__':
    unittest.main()
