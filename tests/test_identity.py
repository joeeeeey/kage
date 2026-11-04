import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest

from kage.onboard import identity
from kage.onboard.tokens import TokenRefused

BOT_TOKEN = 'xoxb-' + '0000-TESTBOT'
BOT = {'team_id': 'T0000TEST', 'bot_user_id': 'U0000BOT', 'bot_id': 'B0000TEST'}
T0 = 1_800_000_000.0


class FakeSlack:
    """conversations.list / conversations.history (honouring `oldest`, two per page) / users.info over fixtures."""

    def __init__(self, ims, users=None):
        self.ims, self.users, self.calls = ims, users or {}, []

    def __call__(self, method, token, params=None):
        assert token == BOT_TOKEN
        params = params or {}
        self.calls.append((method, dict(params)))
        if method == 'conversations.list':
            assert params['types'] == 'im'
            return {'ok': True, 'channels': [{'id': c, 'user': 'U0000X'} for c in self.ims]}, {}
        if method == 'conversations.history':
            msgs = [m for m in self.ims[params['channel']] if float(m['ts']) > float(params['oldest'])]
            start = int(params.get('cursor') or 0)
            page, more = msgs[start:start + 2], start + 2 < len(msgs)
            meta = {'next_cursor': str(start + 2) if more else ''}
            return {'ok': True, 'messages': page, 'response_metadata': meta}, {}
        if method == 'users.info':
            user = self.users.get(params['user'])
            return ({'ok': True, 'user': user} if user else {'ok': False, 'error': 'user_not_found'}), {}
        raise AssertionError(method)


def msg(user, text, ts, **extra):
    return {'type': 'message', 'user': user, 'text': text, 'ts': f'{ts:.6f}', **extra}


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir(mode=0o700)
        self.now = T0

    def tearDown(self):
        self.tmp.cleanup()

    def clock(self):
        return self.now

    def challenge(self):
        code = identity.new_challenge(self.state, clock=self.clock)
        saved = (self.state / identity.CHALLENGE).read_text()
        self.assertNotIn(code, saved)
        self.assertRegex(code, r'^KAGE-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$')
        return code

    def test_dm_code_proves_the_one_sender(self):
        code = self.challenge()
        fake = FakeSlack({'D1': [msg('U0000OLD', code, T0 - 5),  # before the challenge: not read
                                 msg('U0000BOT', code, T0 + 1, bot_id='B0000TEST'),  # the bot itself
                                 msg('U0000OWNER', 'hi', T0 + 2), msg('U0000OWNER', code.lower() + '  ', T0 + 3,
                                                                      team='T0000TEST'),
                                 msg('U0000OWNER', code, T0 + 4, team='T0000TEST'),
                                 msg('U0000ELSE', code, T0 + 5, subtype='message_changed')]})
        self.now = T0 + 60
        record = identity.prove_by_dm(self.state, BOT_TOKEN, BOT, call=fake, clock=self.clock)
        self.assertEqual((record['method'], record['team_id'], record['user_id']),
                         ('dm_code', 'T0000TEST', 'U0000OWNER'))
        self.assertEqual(record['evidence'], {'channel': 'D1', 'ts': f'{T0 + 3:.6f}'})
        self.assertEqual(identity.load(self.state), record)
        self.assertEqual(stat.S_IMODE(os.stat(self.state / identity.RECORD).st_mode), 0o600)
        self.assertFalse((self.state / identity.CHALLENGE).exists())  # used once
        self.assertGreater(sum(1 for c in fake.calls if c[0] == 'conversations.history'), 1)  # paged
        with self.assertRaises(TokenRefused) as e:
            identity.prove_by_dm(self.state, BOT_TOKEN, BOT, call=fake, clock=self.clock)
        self.assertEqual(e.exception.code, 'no_identity_challenge')

    def test_not_found_keeps_the_challenge_for_a_retry(self):
        code = self.challenge()
        ims = {'D1': []}
        with self.assertRaises(TokenRefused) as e:
            identity.prove_by_dm(self.state, BOT_TOKEN, BOT, call=FakeSlack(ims), clock=self.clock)
        self.assertEqual(e.exception.code, 'identity_code_not_found_in_the_bots_direct_messages')
        ims['D1'].append(msg('U0000OWNER', code, T0 + 30))
        fake = FakeSlack(ims, users={'U0000OWNER': {'id': 'U0000OWNER', 'team_id': 'T0000TEST'}})
        self.assertEqual(identity.prove_by_dm(self.state, BOT_TOKEN, BOT, call=fake, clock=self.clock)['user_id'],
                         'U0000OWNER')
        self.assertIn(('users.info', {'user': 'U0000OWNER'}), fake.calls)  # no team on the message: asked

    def test_refusals(self):
        cases = [
            ({'D1': [msg('U0000A', 'X', T0 + 1), msg('U0000B', 'X', T0 + 2)]}, {},
             'identity_code_sent_by_more_than_one_person', False),
            ({'D1': [msg('U0000EXT', 'X', T0 + 1, team='T0000OTHER')]}, {},
             'identity_sender_of_another_workspace', False),
            ({'D1': [msg('U0000EXT', 'X', T0 + 1)]}, {'U0000EXT': {'team_id': 'T0000OTHER'}},
             'identity_sender_of_another_workspace', False),
            ({'D1': [msg('U0000APP', 'X', T0 + 1)]}, {'U0000APP': {'team_id': 'T0000TEST', 'is_bot': True}},
             'identity_sender_not_a_person', True),
        ]
        for ims, users, code_expected, kept in cases:
            code = self.challenge()
            for m in ims['D1']:
                m['text'] = code
            with self.assertRaises(TokenRefused) as e:
                identity.prove_by_dm(self.state, BOT_TOKEN, BOT, call=FakeSlack(ims, users), clock=self.clock)
            self.assertEqual(e.exception.code, code_expected)
            self.assertEqual((self.state / identity.CHALLENGE).exists(), kept, code_expected)
            self.assertIsNone(identity.load(self.state))

    def test_an_expired_code_is_dropped(self):
        code = self.challenge()
        self.now = T0 + identity.CODE_TTL + 1
        with self.assertRaises(TokenRefused) as e:
            identity.prove_by_dm(self.state, BOT_TOKEN, BOT, call=FakeSlack({'D1': [msg('U0000OWNER', code, T0 + 1)]}),
                                 clock=self.clock)
        self.assertEqual(e.exception.code, 'identity_challenge_expired')
        self.assertFalse((self.state / identity.CHALLENGE).exists())

    def test_user_token_proof_and_status(self):
        owner = types.SimpleNamespace(slack_workspace=None, slack_user=None)
        self.assertEqual(identity.status(owner, self.state), (False, 'owner identity not verified'))
        owner = types.SimpleNamespace(slack_workspace='T0000TEST', slack_user='U0000OWNER')
        self.assertEqual(identity.status(owner, self.state)[0], None)  # adopted
        identity.prove_by_user_token(self.state, {'team_id': 'T0000TEST', 'user_id': 'U0000OWNER'}, clock=self.clock)
        self.assertEqual(identity.status(owner, self.state), (True, 'verified (user_token)'))
        other = types.SimpleNamespace(slack_workspace='T0000TEST', slack_user='U0000ELSE')
        self.assertEqual(identity.status(other, self.state)[0], False)

    def test_private_state_and_records_only(self):
        os.chmod(self.state, 0o755)
        with self.assertRaises(TokenRefused):
            identity.new_challenge(self.state)
        os.chmod(self.state, 0o700)
        (self.state / identity.RECORD).write_text(json.dumps({'schema': identity.SCHEMA, 'method': 'typed',
                                                              'team_id': 'T0000TEST', 'user_id': 'U0000OWNER'}))
        os.chmod(self.state / identity.RECORD, 0o600)
        with self.assertRaises(TokenRefused) as e:
            identity.load(self.state)
        self.assertEqual(e.exception.code, 'identity_record_malformed')
        os.chmod(self.state / identity.RECORD, 0o644)
        with self.assertRaises(TokenRefused) as e:
            identity.load(self.state)
        self.assertEqual(e.exception.code, 'identity_record_not_private')


if __name__ == '__main__':
    unittest.main()
