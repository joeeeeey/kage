import contextlib
import io
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

from kage import cli


def core(key='mira'):
    deployment = types.SimpleNamespace(owner=types.SimpleNamespace(timezone='Europe/Lisbon', slack_workspace='T0000TEST01',
                                                                   assistant_slack_app='A0000TEST01', assistant_name='Aide'),
                                       instance=types.SimpleNamespace(agent_marker='test_agent_sent',
                                                                      workspace_root=Path('/srv/work/desk')))
    return cli.CliCore('en', owner_key=key, owner_name='Mira', owner_slack_user='U0000TEST01', bot_user='U0000TEST02',
                       dashboard_url='http://127.0.0.1:9999', deployment=deployment, description='The test program.',
                       tail_command='run-tail', tasks_root_env='TEST_TASKS_ROOT', worker_config_path='~/.config/test.json',
                       issue_tracker='Test tracker', rt=mock.MagicMock())


def run(c, argv):
    out = io.StringIO()
    with mock.patch.object(sys, 'argv', ['program', *argv]), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            c.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue()


class CliTest(unittest.TestCase):
    def test_owner_keyed_commands_and_the_deployment_words(self):
        code, out = run(core(), ['--help'])
        self.assertEqual(code, 0)
        self.assertIn('The test program.', out)
        self.assertIn('ask-mira', out)
        self.assertIn('ask-mira-close', out)
        self.assertIn('post an Aide decision card to Mira', ' '.join(out.split()))  # N1: the assistant's name
        self.assertIn('run-tail: print/follow the readable log', ' '.join(out.split()))
        code, out = run(core(), ['ask-mira', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('everything addressed to Mira', out)
        self.assertIn('en (default) for everything', ' '.join(out.split()))   # O1d: the owner's language
        code, out = run(core(), ['task-rename', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('or the desk root', ' '.join(out.split()))       # the deployment's workspace folder (K15d)

    def test_a_command_runs_through_the_runtime(self):
        c = core()
        c.rt.handback.return_value = {'ok': True}
        code, out = run(c, ['--db', '/tmp/test.sqlite', 'handback', '--task', 'task-a'])
        self.assertEqual(code, 0)
        self.assertEqual(c.rt.handback.call_args.args[2], 'task-a')


if __name__ == '__main__':
    unittest.main()
