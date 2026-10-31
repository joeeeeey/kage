import contextlib
import os
import types
import unittest
from unittest import mock
import urllib.error

from kage.core import loop as L

OWNER, BOT = 'U0000TEST01', 'U0000TEST02'


class Slack:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def api_call(self, endpoint, token, params=None, json_body=None):
        self.calls.append((endpoint, token, params or json_body))
        if self.fail:
            raise self.fail
        if endpoint == 'auth.test':
            return {'team_id': 'T0000TEST01', 'user_id': BOT if token == 'bot-token' else OWNER}
        return {'messages': [{'ts': '1.000001', 'user': 'U0000TEST03', 'text': 'x'}]}

    default_bot_token = staticmethod(lambda: 'bot-token')
    default_user_token = staticmethod(lambda: 'user-token')


def core(slack=None, **rt):
    runtime = types.SimpleNamespace(slack=slack or Slack(), transaction=contextlib.nullcontext, **rt)
    return L.LoopCore('en', owner_key='owner', owner_name='Mira', workspace='T0000TEST01', owner_slack_user=OWNER,
                      bot_user=BOT, read_cache={}, rate_limits={}, identity_cache={},
                      receipt_emoji={'claude': 'test-claude', 'codex': 'test-codex'},
                      no_autostart_env='TEST_NO_AUTOSTART', rt=runtime)


class LoopTest(unittest.TestCase):
    def test_reads_use_the_deployment_caches(self):
        c = core()
        first = c.read_api('conversations.history', 'user-token', {'channel': 'C0000TEST01'})
        self.assertEqual(c.read_api('conversations.history', 'user-token', {'channel': 'C0000TEST01'}), first)
        self.assertEqual(len(c.rt.slack.calls), 1)                              # the second read came from the cache
        self.assertEqual(len(c.read_cache), 1)
        limited = core(Slack(urllib.error.HTTPError('slack', 429, 'x', {'Retry-After': '30'}, None)))
        with self.assertRaises(urllib.error.HTTPError):
            limited.read_api('conversations.history', 't', {})
        self.assertIn('conversations.history', limited.rate_limits)

    def test_identity_is_checked_against_the_owner_and_cached(self):
        c = core()
        self.assertEqual(c.api_identity(), 'user-token')
        self.assertEqual(c.api_identity(bot=True), 'bot-token')
        self.assertEqual(set(c.identity_cache), {False, True})
        wrong = core()
        wrong.owner_slack_user = 'U0000TEST09'
        with self.assertRaisesRegex(RuntimeError, 'unexpected_slack_identity'):
            wrong.api_identity()

    def test_the_autostart_guard_uses_the_deployment_env_name(self):
        c = core()
        with mock.patch.dict(os.environ, {'TEST_NO_AUTOSTART': '1'}):
            self.assertIsNone(c.start('/nonexistent/db.sqlite'))

    def test_replaced_runtime_names_are_called_through_the_runtime(self):
        seen = []
        watch = {'provider': 'codex', 'owner_pid': 42, 'owner_birth': 'birth', 'session': 's', 'cwd': '/tmp'}
        c = core(pid_birth=lambda pid: seen.append(pid) or 'birth', load=lambda db, wid: dict(watch),
                 claude=types.SimpleNamespace(provider=lambda w: w.get('provider') or 'codex'))
        with self.assertRaisesRegex(ValueError, 'owner_still_alive_use_queue_not_resume'):
            c.recover(None, 'w1', '/tmp/db')
        self.assertEqual(seen, [42])                                             # rt.pid_birth, not the module's


if __name__ == '__main__':
    unittest.main()
