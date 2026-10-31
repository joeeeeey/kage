import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from kage.adapters.slack import transport

GUARD = 'KAGE_TEST_OFFLINE'
BOT_TOKEN = 'xoxb-' + 'fake'
USER_TOKEN = 'xoxp-' + 'fake'


class FakeResponse(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(json.dumps(body).encode() if not isinstance(body, bytes) else body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSlack:
    """Records every request and answers from a table: endpoint -> response body (or a callable)."""

    def __init__(self, answers):
        self.answers, self.requests = answers, []

    def __call__(self, req, timeout=None):
        endpoint = req.full_url.split('/api/')[-1].split('?')[0] if '/api/' in req.full_url else req.full_url
        self.requests.append((req.get_method(), req.full_url, dict(req.header_items()), req.data, timeout))
        answer = self.answers.get(endpoint, {'ok': True})
        return FakeResponse(answer(req) if callable(answer) else answer)


def make(**kw):
    return transport.Transport(guard_env=GUARD, owner_name='Mira', expected_bot_user='mirasbot', **kw)


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {GUARD: '0'})
        self.env.start()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp)

    def fake(self, answers=None):
        slack = FakeSlack(answers or {})
        patcher = mock.patch('urllib.request.urlopen', slack)
        patcher.start()
        self.addCleanup(patcher.stop)
        return slack

    def test_guard_refuses_every_call_and_loads_no_token(self):
        t = make()
        slack = self.fake()
        with mock.patch.dict(os.environ, {GUARD: '1', 'SLACK_BOT_TOKEN': BOT_TOKEN}):
            for call in (lambda: t.api_call('auth.test', BOT_TOKEN), lambda: t.api_call_form('x', BOT_TOKEN, {}),
                         lambda: t.upload_file_bytes('https://files.example/up', __file__)):
                with self.assertRaisesRegex(RuntimeError, f'real_slack_disabled: {GUARD}=1 refuses'):
                    call()
            self.assertEqual((t.default_bot_token(), t.default_user_token()), ('', ''))
        self.assertEqual(slack.requests, [])

    def test_get_post_and_errors(self):
        posted = []
        t = make(post_send=lambda response, payload: posted.append((response['ts'], payload)))
        slack = self.fake({'conversations.history': {'ok': True, 'messages': []},
                           'chat.postMessage': {'ok': True, 'ts': '1.0'}, 'auth.test': {'ok': False, 'error': 'invalid_auth'}})
        self.assertEqual(t.api_call('conversations.history', BOT_TOKEN, params={'channel': 'C1', 'limit': 2}),
                         {'ok': True, 'messages': []})
        method, url, headers, data, timeout = slack.requests[0]
        self.assertEqual((method, url, headers['Authorization'], data, timeout),
                         ('GET', 'https://slack.com/api/conversations.history?channel=C1&limit=2', f'Bearer {BOT_TOKEN}',
                          None, 30))
        t.api_call('chat.postMessage', BOT_TOKEN, json_body={'channel': 'C1', 'text': 'hi'})
        method, url, headers, data, _ = slack.requests[1]
        self.assertEqual((method, headers['Content-type'], json.loads(data)), ('POST', 'application/json; charset=utf-8',
                                                                               {'channel': 'C1', 'text': 'hi'}))
        self.assertEqual(posted, [('1.0', {'channel': 'C1', 'text': 'hi'})])
        with self.assertRaisesRegex(RuntimeError, '^Slack API error: invalid_auth$'):
            t.api_call('auth.test', BOT_TOKEN)

    def test_form_call_drops_empty_fields(self):
        t = make()
        slack = self.fake({'files.getUploadURLExternal': {'ok': True, 'upload_url': 'u'}})
        t.api_call_form('files.getUploadURLExternal', BOT_TOKEN, {'filename': 'a.txt', 'alt': '', 'x': None, 'length': 3})
        self.assertEqual(slack.requests[0][3], b'filename=a.txt&length=3')

    def test_identity_checks(self):
        t = make()
        self.fake({'auth.test': {'ok': True, 'user': 'mirasbot', 'user_id': 'U0000TEST02', 'bot_id': 'B0000TEST01'}})
        t.validate_bot_identity(BOT_TOKEN)
        with self.assertRaisesRegex(RuntimeError, "starting with 'xoxb-'"):
            t.validate_bot_identity(USER_TOKEN)
        with self.assertRaisesRegex(RuntimeError, "^Expected Mira's Slack user token for this operation, but got a bot"):
            t.validate_user_identity(BOT_TOKEN)
        with self.assertRaisesRegex(RuntimeError, 'bot-like identity'):
            t.validate_user_identity(USER_TOKEN)
        self.fake({'auth.test': {'ok': True, 'user': 'someone', 'user_id': 'U0000TEST09'}})
        with self.assertRaisesRegex(RuntimeError, "Expected Slack bot user 'mirasbot'"):
            t.validate_bot_identity(BOT_TOKEN)
        self.assertEqual(t.validate_user_identity(USER_TOKEN)['user_id'], 'U0000TEST09')

    def test_tokens_from_env_then_file(self):
        t = make()
        user_file, bot_file = self.tmp / 'u', self.tmp / 'b'
        user_file.write_text(' from-file \n')
        with mock.patch.dict(os.environ, {'SLACK_USER_TOKEN': '', 'SLACK_USER_TOKEN_FILE': str(user_file),
                                          'SLACK_BOT_TOKEN': ' env-bot ', 'SLACK_BOT_TOKEN_FILE': str(bot_file)}):
            self.assertEqual((t.default_user_token(), t.default_bot_token()), ('from-file', 'env-bot'))
        with mock.patch.dict(os.environ, {'SLACK_BOT_TOKEN': '', 'SLACK_BOT_TOKEN_FILE': str(self.tmp / 'none')}):
            self.assertEqual(t.default_bot_token(), '')

    def test_reactions_go_through_the_adapter_call(self):
        calls = []

        def call(endpoint, token, params=None, json_body=None):
            calls.append((endpoint, params, json_body))
            if endpoint == 'auth.test':
                return {'user_id': 'U0000TEST01'}
            if endpoint == 'reactions.add' and json_body['name'] == 'eyes':
                raise RuntimeError('Slack API error: already_reacted')
            return {'message': {'reactions': [{'name': 'eyes', 'users': ['U0000TEST01']},
                                              {'name': 'white_check_mark', 'users': ['U0000TEST01']}]}}
        t = make(call=call)
        result = t.add_reactions(USER_TOKEN, 'C1', '1.0', [':eyes:', 'white_check_mark', 'eyes'])
        self.assertEqual(result['verified'], {'eyes': True, 'white_check_mark': True})
        self.assertEqual([c[0] for c in calls], ['auth.test', 'reactions.add', 'reactions.add', 'reactions.get'])
        with self.assertRaises(ValueError):
            t.add_reactions(USER_TOKEN, 'C1', '1.0', ['Bad Name'])
        t2 = make(call=lambda e, tok, params=None, json_body=None: {'user_id': 'U1', 'message': {'reactions': []}})
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            t2.add_reactions(USER_TOKEN, 'C1', '1.0', ['eyes'])

    def test_pagination(self):
        pages = iter([{'members': [1, 2], 'response_metadata': {'next_cursor': 'c1'}}, {'members': [3]}])
        seen = []
        t = make(call=lambda e, tok, params=None, json_body=None: (seen.append(dict(params)), next(pages))[1])
        self.assertEqual(t.paginated_list('users.list', BOT_TOKEN, {'limit': 2}, 'members'), [1, 2, 3])
        self.assertEqual(seen, [{'limit': 2}, {'limit': 2, 'cursor': 'c1'}])

    def test_upload(self):
        t = make()
        slack = self.fake({'https://files.example/up': {'ok': True}})
        path = self.tmp / 'notes.txt'
        path.write_text('hello')
        with mock.patch('uuid.uuid4', return_value=mock.Mock(hex='f' * 32)):
            t.upload_file_bytes('https://files.example/up', path)
        method, url, headers, data, _ = slack.requests[0]
        self.assertEqual(headers['Content-type'], 'multipart/form-data; boundary=----codex-slack-upload-' + 'f' * 32)
        self.assertIn(b'filename="notes.txt"\r\nContent-Type: text/plain\r\n\r\nhello\r\n', data)


if __name__ == '__main__':
    unittest.main()
