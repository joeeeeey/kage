import hashlib
import os
import types
import unittest
from unittest import mock

from kage import app
from kage.adapters.slack import client as slack_client
from tests.test_app import Deployment

LOCK = {'modules': {'registry': 'kage', 'liveness': 'kage', 'assembly': 'kage', 'slack_client': 'kage'}}


def deployment(**changes):
    return Deployment(lock=LOCK, modules={}, **changes)


class StandaloneTransportTest(unittest.TestCase):
    def test_an_undeclared_slack_transport_is_kages_own_leaf(self):
        with deployment() as d:
            a = app.App(d.root / 'deploy')
            a.verify()
            leaf = a.leaf('slack_transport')
            self.assertEqual(leaf.__name__, 'kage_assembled_slack_transport')
            self.assertIsInstance(leaf.CLIENT, slack_client.Client)
            self.assertIs(a.leaf('slack_transport'), leaf)
            t = leaf.TRANSPORT
            self.assertEqual((t.guard_env, t.user_token_env, t.bot_token_env, t.bot_token_file_env),
                             ('KAGE_NO_REAL_SLACK', 'KAGE_MIRA_SLACK_USER_TOKEN', 'KAGE_MIRA_SLACK_BOT_TOKEN',
                              'KAGE_MIRA_SLACK_BOT_TOKEN_FILE'))
            self.assertEqual((t.user_token_file, t.bot_token_file),
                             ('~/.config/kage/mira/slack-user-token', '~/.config/kage/mira/slack-bot-token'))
            for name in ('api_call', 'default_user_token', 'default_bot_token', 'add_reactions', 'paginated_list'):
                self.assertTrue(callable(getattr(leaf, name)), name)

    def test_a_declared_module_still_wins(self):
        body = b'ANSWER = "the deployment\'s own"\n'
        declared = {'slack_transport': {'path': 'scripts/own_slack.py', 'sha256': hashlib.sha256(body).hexdigest(),
                                        'note': 'test'}}
        with Deployment(lock=LOCK, modules=declared) as d:
            (d.root / 'scripts/own_slack.py').write_bytes(body)
            a = app.App(d.root / 'deploy')
            a.verify()
            self.assertEqual(a.leaf('slack_transport').ANSWER, "the deployment's own")

    def test_the_guard_refuses_before_any_request(self):
        with deployment() as d, mock.patch.dict(os.environ, {'KAGE_NO_REAL_SLACK': '1'}):
            a = app.App(d.root / 'deploy')
            a.verify()
            with self.assertRaisesRegex(RuntimeError, 'real_slack_disabled'):
                a.leaf('slack_transport').api_call('auth.test', 'xoxb-test')

    def test_operations_reach_a_replaced_api_call_and_no_bot_is_trusted_yet(self):
        with deployment() as d:
            a = app.App(d.root / 'deploy')
            a.verify()
            leaf = a.leaf('slack_transport')
            calls = []
            leaf.api_call = lambda endpoint, token, params=None, json_body=None: calls.append(endpoint) or {
                'ok': True, 'user': 'somebot', 'user_id': 'U0BOT', 'team_id': 'T0'}
            with self.assertRaisesRegex(RuntimeError, "Expected Slack bot user ''"):
                leaf.validate_bot_identity('xoxb-test')
            self.assertEqual(calls, ['auth.test'])

    def test_a_send_is_recorded_through_kages_own_poll_schedule(self):
        with deployment() as d:
            a = app.App(d.root / 'deploy')
            a.verify()
            recorded = []
            a._leaves['poll_schedule'] = types.SimpleNamespace(
                record_outbound=lambda *args, metadata=None: recorded.append(('out', args, metadata)),
                record_mentions=lambda channel, ts, users, metadata=None: recorded.append(('mentions', channel, ts, users)))
            leaf = a.leaf('slack_transport')
            leaf.notify_followup_activity({'channel': 'C1', 'ts': '1.5', 'message': {'user': 'U1'}},
                                          {'text': 'hello <@U2>', 'metadata': {'event_type': 'x'}})
            self.assertEqual(recorded, [('out', ('C1', '1.5', '1.5', 'U1'), {'event_type': 'x'}),
                                        ('mentions', 'C1', '1.5', ['U2'])])

    def test_the_client_refuses_unknown_token_options(self):
        with self.assertRaises(TypeError):
            slack_client.Client(guard_env='G', owner_name='Mira', expected_bot_user='', user_token_env='U',
                                user_token_file='~/u', bot_token_env='B', call=None, post_send=None,
                                followup_helper=None, token='xoxb-never')


if __name__ == '__main__':
    unittest.main()
