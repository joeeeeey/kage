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


if __name__ == '__main__':
    unittest.main()
