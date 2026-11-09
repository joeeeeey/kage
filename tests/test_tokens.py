import os
import unittest
from unittest import mock

from kage.onboard import manifest, tokens
from kage.ports import secrets

BOT = 'xoxb-' + '0000-TESTBOT'
APP = 'xapp-' + '1-A0000TEST-0000-abc'
USER = 'xoxp-' + '0000-TESTUSER'
TEAM, OTHER = 'T0000TEST', 'T0000OTHER'
SCOPES = {'x-oauth-scopes': ','.join(manifest.BOT_SCOPES)}


class FakeSlack:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, method, token):
        self.calls.append((method, token))
        return self.answers[(method, token)]


def slack(over=None):
    answers = {('auth.test', BOT): ({'ok': True, 'team_id': TEAM, 'team': 'Test', 'user_id': 'U0000BOT',
                                     'bot_id': 'B0000TEST'}, SCOPES),
               ('apps.connections.open', APP): ({'ok': True, 'url': 'wss://example.invalid/socket'}, {}),
               ('auth.test', USER): ({'ok': True, 'team_id': TEAM, 'user_id': 'U0000OWNER'},
                                     {'x-oauth-scopes': 'chat:write'})}
    answers.update(over or {})
    return FakeSlack(answers)


class TokenChecksTest(unittest.TestCase):
    def test_bot_app_user_admitted_in_order(self):
        store, fake = secrets.MemoryStore('mira'), slack()
        bot = tokens.admit(store, tokens.BOT, BOT, call=fake)
        self.assertEqual((bot['team_id'], bot['bot_user_id'], bot['missing_scopes']), (TEAM, 'U0000BOT', []))
        self.assertEqual(bot['ref'], 'kage-secret://mira/slack-bot-token')
        app = tokens.admit(store, tokens.APP, APP, call=fake)
        self.assertNotIn('url', app)
        user = tokens.admit(store, tokens.USER, USER, call=fake, bot=bot)
        self.assertEqual(user['user_id'], 'U0000OWNER')
        self.assertIn('search:read', user['missing_scopes'])
        self.assertEqual((store.get(tokens.BOT), store.get(tokens.APP), store.get(tokens.USER)), (BOT, APP, USER))
        for result in (bot, app, user):
            self.assertFalse(any(t in repr(result) for t in (BOT, APP, USER)))

    def test_refusals_store_nothing_and_never_name_the_token(self):
        bot = tokens.check_bot(BOT, call=slack())
        cases = [
            (tokens.BOT, APP, {}, 'not_a_slack_bot_token'),
            (tokens.BOT, BOT + ' x', {}, 'not_a_slack_bot_token'),
            (tokens.BOT, BOT, {('auth.test', BOT): ({'ok': False, 'error': 'invalid_auth'}, {})},
             'slack_auth.test_refused_invalid_auth'),
            (tokens.BOT, BOT, {('auth.test', BOT): ({'ok': True, 'team_id': TEAM, 'user_id': 'U0000OWNER'}, {})},
             'bot_token_is_not_a_bot'),
            (tokens.APP, APP, {('apps.connections.open', APP): ({'ok': False, 'error': 'invalid_auth'}, {})},
             'slack_apps.connections.open_refused_invalid_auth'),
            (tokens.USER, USER, {('auth.test', USER): ({'ok': True, 'team_id': OTHER, 'user_id': 'U0000OWNER'}, {})},
             'user_token_of_another_workspace'),
            (tokens.USER, USER, {('auth.test', USER): ({'ok': True, 'team_id': TEAM, 'user_id': 'U0000BOT',
                                                        'bot_id': 'B0000TEST'}, {})}, 'user_token_is_the_bot'),
            (tokens.USER, BOT, {}, 'not_a_slack_user_token'),
            (tokens.APP, 'xoxe.' + APP, {}, 'rotating_token_not_supported'),  # F9: rotating tokens, said plainly
            (tokens.BOT, 'xoxe.' + BOT, {}, 'rotating_token_not_supported'),
            (tokens.APP, 'xoxe-1-' + 'TESTREFRESH', {}, 'rotating_token_not_supported'),
        ]
        for name, token, over, code in cases:
            store = secrets.MemoryStore('mira')
            with self.assertRaises(tokens.TokenRefused) as e:
                tokens.admit(store, name, token, call=slack(over), bot=bot)
            self.assertEqual(e.exception.code, code, (name, code))
            self.assertIsNone(store.get(name))
            self.assertNotIn(token, str(e.exception))

    def test_a_bot_of_another_workspace_is_refused_before_storage(self):
        store = secrets.MemoryStore('mira')
        with self.assertRaises(tokens.TokenRefused) as e:
            tokens.admit(store, tokens.BOT, BOT, call=slack(), team_id=OTHER)
        self.assertEqual(e.exception.code, 'bot_token_of_another_workspace')
        self.assertIsNone(store.get(tokens.BOT))
        with self.assertRaises(tokens.TokenRefused) as e:
            tokens.admit(store, tokens.USER, USER, call=slack())
        self.assertEqual(e.exception.code, 'user_token_needs_the_checked_bot_first')

    def test_unknown_scopes_are_none_not_all_missing(self):
        fake = slack({('auth.test', BOT): ({'ok': True, 'team_id': TEAM, 'user_id': 'U0000BOT', 'bot_id': 'B0'}, {})})
        self.assertIsNone(tokens.check_bot(BOT, call=fake)['missing_scopes'])

    def test_the_live_call_honours_the_offline_guard(self):
        with mock.patch.dict(os.environ, {'KAGE_NO_REAL_SLACK': '1'}), \
                mock.patch('urllib.request.urlopen', side_effect=AssertionError('no network')):
            with self.assertRaises(tokens.TokenRefused) as e:
                tokens.check_bot(BOT)
        self.assertEqual(e.exception.code, 'slack_offline_guard_set')


if __name__ == '__main__':
    unittest.main()
