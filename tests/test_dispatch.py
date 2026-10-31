import unittest
import zoneinfo

from kage.core import dispatch as D


def core(language='en', **over):
    args = dict(tz=zoneinfo.ZoneInfo('Asia/Shanghai'), tz_name='Asia/Shanghai', owner_key='owner', owner_name='Mira',
                run_id_env='TEST_RUN_ID', task_env='TEST_TASK', hops_env='TEST_HOPS', run_context_env='TEST_RUN_CONTEXT')
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


if __name__ == '__main__':
    unittest.main()
