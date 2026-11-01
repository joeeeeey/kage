import contextlib
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from kage.adapters.slack import buttons
from kage.adapters.slack import client

OWNER = 'U0TEST0OWNER'


class ButtonsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = []
        hooks = {'load_config': lambda: self.b.load_config(), 'check': lambda *a, **k: self.calls.append(('check', a)) or {},
                 'record_refusal': lambda *a, **k: self.calls.append(('refusal', a)),
                 'private': lambda *a, **k: self.calls.append(('private', a)),
                 'default_model': lambda: self.b.card_model([])}
        self.b = buttons.DecisionButtons('en', tz=ZoneInfo('UTC'), tz_name='UTC', owner_user=OWNER, owner_key='owner',
                                         owner_name='Owner', config_env='KAGE_TEST_DECISIONS', default_config=self.tmp / 'd.json',
                                         refresh_command='kage card-destination-check --refresh', report_next='Fix it.',
                                         copy_texts={'ask_verb': 'the ask must start with what {owner} has to do: '},
                                         copy_active=lambda: True, hooks=hooks)

    def test_components_share_the_owner_and_the_hooks(self):
        self.assertIs(self.b.destinations.authority, self.b.authority)
        self.assertEqual(self.b.destinations.refresh_command, 'kage card-destination-check --refresh')
        self.assertEqual(self.b.destinations.im_not_owner, 'im_not_owner')
        self.b.destinations.hook_check('rt', 'db', 'C0TEST00001')
        self.assertEqual(self.calls, [('check', ('rt', 'db', 'C0TEST00001'))])
        self.assertEqual(self.b.copy.texts['ask_verb'], 'the ask must start with what {owner} has to do: ')

    def test_config_path_and_a_missing_config_is_the_owner_only(self):
        os.environ.pop('KAGE_TEST_DECISIONS', None)
        self.assertEqual(self.b.config_path(), self.tmp / 'd.json')
        with mock.patch.dict(os.environ, {'KAGE_TEST_DECISIONS': str(self.tmp / 'e.json')}):
            self.assertEqual(self.b.config_path(), self.tmp / 'e.json')
        self.assertEqual(self.b.load_config(), self.b.authority.load_config(self.tmp / 'd.json'))

    def test_left_out_arguments_are_the_cards_hint_and_the_owner(self):
        self.assertEqual(self.b.hint_block(), self.b.cards.hint_block(self.b.cards.hint))
        self.assertEqual(self.b.card_blocks('Decide', 'e1'), self.b.cards.card_blocks('Decide', 'e1', None, None,
                                                                                       self.b.cards.hint, None))
        draft = {'title': 'A rule', **{k: k + ' text' for k in buttons._cards.RULE_FIELDS}}
        self.assertEqual(self.b.rule_card_blocks(draft), self.b.cards.rule_card_blocks(draft, OWNER))
        self.assertEqual(self.b.card_model([]), buttons._decisions.card_model([], self.b.default_pair))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = []
        self.helper = self.tmp / 'poll_schedule.py'
        self.c = client.Client(guard_env='KAGE_TEST_OFFLINE', owner_name='Owner', expected_bot_user='examplebot',
                               user_token_env='KAGE_TEST_USER_TOKEN', user_token_file=str(self.tmp / 'token'),
                               bot_token_env='KAGE_TEST_BOT_TOKEN', call=lambda *a, **k: self.calls.append(a) or {'ok': True},
                               post_send=lambda r, p: self.calls.append(('post_send', r, p)), followup_helper=self.helper)

    def test_the_usage_example_is_the_deployments(self):
        self.assertEqual(self.c.transport.emoji_example, 'eyes')                 # N1: Kage's neutral default
        named = client.Client(guard_env='KAGE_TEST_OFFLINE', owner_name='Owner', expected_bot_user='examplebot',
                              user_token_env='KAGE_TEST_USER_TOKEN', user_token_file=str(self.tmp / 'token'),
                              bot_token_env='KAGE_TEST_BOT_TOKEN', call=lambda *a, **k: {'ok': True},
                              post_send=lambda r, p: None, followup_helper=self.helper, emoji_example='mira-mark')
        self.assertEqual(named.transport.emoji_example, 'mira-mark')
        with self.assertRaisesRegex(ValueError, r'for example thinking_face or mira-mark\.$'):
            named.add_reactions('xoxp-' + 'fake', 'D0TEST00001', '1.1', ['Not A Name'])

    def test_operations_go_through_the_transport_and_its_hooks(self):
        self.assertIs(self.c.transport.call.__class__, type(lambda: 0))
        self.c.transport.call('auth.test', 'xoxb-' + 'fake')
        self.c.transport.post_send({'ok': True}, {'channel': 'C0TEST00001'})
        self.assertEqual(self.calls, [('auth.test', 'xoxb-' + 'fake'), ('post_send', {'ok': True}, {'channel': 'C0TEST00001'})])
        with mock.patch.dict(os.environ, {'KAGE_TEST_OFFLINE': '1'}):
            with self.assertRaises(RuntimeError):
                self.c.require_real_slack('a test')

    def test_the_post_send_hook_reports_sends_and_mentions_to_the_helper(self):
        self.assertIsNone(self.c.notify_followup_activity({'ok': True}, {}))  # no helper: nothing
        log = self.tmp / 'calls.txt'
        self.helper.write_text(
            'def record_outbound(*a, metadata=None):\n'
            f'    open({str(log)!r}, "a").write(repr(("out", a, metadata)) + "\\n")\n'
            'def record_mentions(channel, ts, users, metadata=None):\n'
            f'    open({str(log)!r}, "a").write(repr(("mentions", channel, ts, users)) + "\\n")\n')
        self.c.notify_followup_activity({'channel': 'C0TEST00001', 'ts': '1700000000.000100', 'message': {'user': OWNER}},
                                        {'text': 'hi <@U0TEST0PEER> and <@U0TEST0EXTRA|x>', 'metadata': {'m': 1}})
        self.assertEqual(log.read_text().splitlines(), [
            repr(('out', ('C0TEST00001', '1700000000.000100', '1700000000.000100', OWNER), {'m': 1})),
            repr(('mentions', 'C0TEST00001', '1700000000.000100', ['U0TEST0PEER', 'U0TEST0EXTRA']))])
        self.helper.write_text('raise RuntimeError("broken")\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.c.notify_followup_activity({'ok': True}, {})
        self.assertIn('Slack send succeeded', err.getvalue())

    def test_payload_texts_reads_text_or_blocks(self):
        self.assertEqual(client._payload_texts({'text': 'a'}), ['a'])
        self.assertEqual(client._payload_texts({'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'b'}},
                                                           {'type': 'rich_text', 'elements': [{'type': 'user',
                                                                                               'user_id': 'U0TEST01'}]}]}),
                         ['b', '<@U0TEST01>'])


if __name__ == '__main__':
    unittest.main()
