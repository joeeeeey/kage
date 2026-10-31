import contextlib
import io
import json
import sqlite3
import types
import unittest
import unittest.mock

from kage.core import commands as C


def core(language='en', owner_key='owner', **rt):
    runtime = types.SimpleNamespace(transaction=contextlib.nullcontext, **rt)
    return C.CommandsCore(language, owner_key=owner_key, owner_name='Mira', owner_slack_user='U0000TEST01',
                          bot_user='U0000TEST02', dashboard_url='http://127.0.0.1:9999', hops_env='TEST_HOPS',
                          run_id_env='TEST_RUN_ID', no_iterm_env='TEST_NO_ITERM', tail_command='run-tail', rt=runtime)


class CommandsTest(unittest.TestCase):
    def test_run_view_in_the_owner_language_with_the_deployment_tail_command(self):
        c = core(buttons=types.SimpleNamespace(escape=lambda s: s), _latest_run=lambda db, dispatch_id: None,
                 dispatch=types.SimpleNamespace(PROVIDER_NAME={'codex': 'Codex'}), dispatch_held=lambda db, d: [1, 2])
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE tasks (task_id, title)')
        db.execute("INSERT INTO tasks VALUES ('task-a', 'Fix it')")
        text = c.run_view_text(db, {'task_id': 'task-a', 'task_key': 'task-a', 'dispatch_id': 'd1', 'state': 'waiting',
                                    'plan': json.dumps([{'provider': 'codex'}]), 'step': 0})
        self.assertEqual(text, '*Run overview* · task `task-a` (Fix it)\nNo run has started yet (state waiting, plan: Codex).'
                               '\nPending events: 2')
        self.assertEqual(c.msg('commands.run_view_text.9', tail_command=c.tail_command, run_id='r1',
                               dashboard_url=c.dashboard_url, run_id2='r1'),
                         'Full log: `run-tail r1` · http://127.0.0.1:9999/runs/r1')
        self.assertTrue(core('zh-CN').smoke_step1.startswith('🧪 TEST smoke-dispatch 第 1 步'))

    def test_the_owner_key_names_the_hook_ask_and_the_terminal_check(self):
        out, called = io.StringIO(), []
        c = core(owner_key='mira', dispatch=types.SimpleNamespace(KEEP_WARM_ID_RE=__import__('re').compile('kw-.*')),
                 _meta_error=lambda db, what, exc: called.append(what))
        with contextlib.redirect_stdout(out):
            c.dispatch_hook(None, '/tmp/db', 'r1', stdin=io.StringIO('not json'))
        reason = json.loads(out.getvalue())['hookSpecificOutput']['permissionDecisionReason']
        self.assertIn('ask-mira', reason)
        self.assertEqual(called, ['dispatch_hook_error'])
        c = core(owner_key='mira', mira_terminal_only=lambda: (_ for _ in ()).throw(PermissionError('terminal_only')))
        with self.assertRaises(PermissionError):
            c.takeover(None, '/tmp/db', 'task-a')

    def test_a_task_key_in_the_takeover_words(self):
        c = core()
        self.assertEqual(c.msg('commands.takeover.1', key_value='task-a'),
                         'no_session: task task-a has no session to take over yet')
        self.assertIn('handback --task task-a', c.msg('commands.takeover.2', key_value='task-a', key_value2='task-a'))

    def test_iterm_is_refused_under_the_deployment_test_env(self):
        c = core()
        with unittest.mock.patch.dict('os.environ', {'TEST_NO_ITERM': '1'}):
            with self.assertRaises(OSError):
                c.open_iterm_tab('/tmp', 'true')


if __name__ == '__main__':
    unittest.main()
