import json
import os
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import patch

from kage.adapters.slack import receiver as r

TEAM, OWNER, OTHER = 'T0000TEST01', 'U0000TEST01', 'U0000TEST05'
SCRIPT = Path('/opt/kage/runtime/scripts/runtime.py')
ZH = r.ClickAnswers('zh-CN', owner_name='Mira', guard_env='KAGE_TEST_NO_SLACK')
EN = r.ClickAnswers('en', owner_name='Mira')


def click(action_id, value='ev1', team=TEAM, user_team=None, url='https://hooks.slack.com/actions/T/1/x'):
    return {'type': 'block_actions', 'team': {'id': team}, 'user': {'id': OWNER, 'team_id': user_team or team},
            'actions': [{'action_id': action_id, 'value': value}], 'response_url': url,
            'container': {'channel_id': 'C0000TEST01', 'message_ts': '1700000000.000100'}}


def completed(stdout='{}', returncode=0):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode)


class ClickTest(unittest.TestCase):
    def test_context_per_kind(self):
        kinds = {'sf_decision:approve': 'decide', 'sf_confirm:yes': 'confirm', 'sf_ask:c1:o2': 'ask',
                 'sf_askconfirm:yes': 'askconfirm', 'sf_dispatch:stop': 'dispatch', 'sf_breaker:continue': 'breaker'}
        for action, kind in kinds.items():
            context = r.decision_context(click(action, value='c1' if kind == 'ask' else 'ev1'), expected_team_id=TEAM)
            self.assertEqual(context['kind'], kind)
        ask = r.decision_context(click('sf_ask:c1:o2', value=''), expected_team_id=TEAM)
        self.assertEqual((ask['value'], ask['choice']), ('c1', 'o2'))

    def test_refusals(self):
        for payload, code in ((click('sf_decision:approve', team='T0000TEST02'), 'unexpected_team'),
                              (click('sf_dispatch:explode'), 'unsupported_action'), (click('other:x'), 'unsupported_action'),
                              (click('sf_ask:c1'), 'missing_decision_context'),
                              (click('sf_decision:approve', value=''), 'missing_decision_context')):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                r.decision_context(payload, expected_team_id=TEAM)
        external = r.decision_context(click('sf_ask:c1:o2', value='c1', user_team='T0000TEST02'), expected_team_id=TEAM)
        self.assertEqual(external['actor_team'], 'T0000TEST02')       # answered privately by the CLI, never dropped

    def test_commands(self):
        decide = r.decide_command(r.decision_context(click('sf_decision:approve'), expected_team_id=TEAM),
                                  python='py', script=SCRIPT, db='/tmp/db')
        self.assertEqual(decide[:6], ['py', '-I', str(SCRIPT), '--db', '/tmp/db', 'decide'])
        self.assertIn('--response-url', decide)
        retry = r.decide_command(r.decision_context(click('sf_dispatch:retry_codex'), expected_team_id=TEAM),
                                 python='py', script=SCRIPT)
        self.assertEqual(retry[3:7], ['dispatch-retry', '--dispatch-id', 'ev1', '--mode'])
        self.assertEqual(r.card_reply_push_command({'channel': 'C1', 'ts': '1.2', 'thread_ts': '1.1', 'user': 'U1'},
                                                   python='py', script=SCRIPT)[3:],
                         ['card-reply-push', '--channel', 'C1', '--ts', '1.2', '--thread-ts', '1.1', '--user', 'U1'])

    def test_running_the_runtime(self):
        context = r.decision_context(click('sf_decision:approve'), expected_team_id=TEAM)
        seen = {}

        def runner(command, **kw):
            seen.update(kw)
            return completed(json.dumps({'ok': True, 'choice': 'approve'}))
        with patch.dict(os.environ, {'CLAUDECODE': '1'}):
            self.assertEqual(r.run_decide(context, runner=runner, script=SCRIPT)['choice'], 'approve')
        self.assertNotIn('CLAUDECODE', seen['env'])                 # a click is never an agent action
        self.assertEqual(seen['cwd'], str(SCRIPT.resolve().parents[1]))

        def raising(exc):
            def run(*a, **kw):
                raise exc
            return run
        for run, reason in ((raising(subprocess.TimeoutExpired('x', 1)), 'decide_timeout'),
                            (raising(OSError()), 'decide_unavailable'), (lambda *a, **k: completed('', 2), 'decide_failed'),
                            (lambda *a, **k: completed('not json'), 'decide_invalid_output'),
                            (lambda *a, **k: completed('[1]'), 'decide_invalid_output')):
            self.assertEqual(r.run_decide(context, runner=run, script=SCRIPT)['reason'], reason)

    def test_answers(self):
        decide = {'kind': 'decide', 'choice': 'approve'}
        self.assertEqual(ZH.decision_ephemeral_text(decide, {'ok': False, 'reason': 'not_authorized'}),
                         '只有 Mira（或休假期间的代理人）可以处理这张卡片。')
        self.assertEqual(ZH.decision_ephemeral_text(decide, {'ok': False, 'reason': 'Bad Code!'}),
                         '没能记录这个选择（unknown）。请稍后再点一次，或直接在 thread 里回复。')
        self.assertEqual(ZH.decision_ephemeral_text(decide, {'ok': True, 'duplicate': True, 'choice': 'reject',
                                                             'label': '拒<绝', 'actor': OTHER}),
                         f'这张卡片已经处理过了：拒&lt;绝（<@{OTHER}>）。如需补充，请在 thread 里回复。')
        self.assertIsNone(ZH.decision_ephemeral_text(decide, {'ok': True}))
        self.assertEqual(EN.decision_ephemeral_text({'kind': 'confirm', 'choice': 'yes'},
                                                    {'ok': False, 'reason': 'already_decided'}), EN.stale)
        self.assertEqual(ZH.decision_ephemeral_text({'kind': 'dispatch', 'choice': 'view'}, {'ephemeral': 'x' * 3000}),
                         'x' * 3000)
        self.assertEqual(ZH.decision_ephemeral_text({'kind': 'breaker', 'choice': 'stop'}, {'ok': False, 'reason': 'x'}),
                         '没能处理这个按钮（x）。请稍后再点一次。')
        self.assertEqual(EN.decision_ephemeral_text({'kind': 'ask', 'choice': 'o'}, {'ok': False}), EN.ask_fallback)

    def test_private_answer_post(self):
        self.assertFalse(r.post_ephemeral('https://example.com/x', 't'))         # only hooks.slack.com
        with patch.dict(os.environ, {'KAGE_TEST_NO_SLACK': '1'}), self.assertRaisesRegex(RuntimeError, 'real_slack_disabled'):
            ZH.post_ephemeral('https://hooks.slack.com/actions/x', 't')
        sent = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b''
        self.assertTrue(ZH.post_ephemeral('https://hooks.slack.com/actions/x', '好', opener=lambda req, timeout: (
            sent.append(json.loads(req.data)), Response())[1]))
        self.assertEqual(sent, [{'response_type': 'ephemeral', 'replace_original': False, 'text': '好'}])

    def test_handle_a_click(self):
        posted = []
        out = ZH.handle_decision_action(click('sf_decision:approve'), expected_team_id=TEAM, script=SCRIPT,
                                        runner=lambda *a, **k: completed(json.dumps({'ok': False, 'reason': 'not_authorized'})),
                                        poster=lambda url, text: posted.append(text) or True)
        self.assertEqual((out['ok'], out['reason'], out['ephemeral']), (False, 'not_authorized', 'sent'))
        self.assertEqual(posted, [ZH.not_authorized])
        self.assertNotIn('response_url', json.dumps(out))


class CardReplyTest(unittest.TestCase):
    def event(self, **event):
        base = {'type': 'message', 'channel': 'C0000TEST01', 'ts': '1700000001.000200', 'thread_ts': '1700000000.000100',
                'user': OWNER}
        return {'type': 'event_callback', 'team_id': TEAM, 'event': {**base, **event}}

    def test_refs_and_drops(self):
        refs, why = r.card_reply_refs(self.event(), expected_team_id=TEAM)
        self.assertEqual((sorted(refs), why), (['channel', 'thread_ts', 'ts', 'user'], ''))
        for event, code in (({'subtype': 'message_changed'}, 'has_subtype'), ({'bot_id': 'B1'}, 'has_bot_id'),
                            ({'channel': 'D1'}, 'bad_channel'), ({'thread_ts': None}, 'not_thread_reply'),
                            ({'thread_ts': '1700000001.000200'}, 'not_thread_reply'), ({'user': 'W1'}, 'bad_user')):
            self.assertEqual(r.card_reply_refs(self.event(**event), expected_team_id=TEAM), (None, code))
        self.assertEqual(r.card_reply_refs(self.event(), expected_team_id='T0000TEST02'), (None, 'unexpected_team'))
        self.assertEqual(r.card_reply_drop_diagnostic(self.event(), 'Bad!')['reason'], '')

    def test_config(self):
        self.assertIsNone(r.parse_card_reply_config({'ok': True, 'enabled': 'yes'}))
        cfg = r.parse_card_reply_config({'ok': True, 'enabled': True, 'channels': ['C0000TEST01', 'bad'],
                                         'users': [OWNER, 7], 'ttl_seconds': 99999})
        self.assertEqual((cfg['channels'], cfg['users'], cfg['ttl_seconds']),
                         (frozenset({'C0000TEST01'}), frozenset({OWNER}), 3600.0))
        self.assertEqual([r.card_reply_ttl(v) for v in (True, float('nan'), 1, 600)], [300.0, 300.0, 30.0, 600.0])



class IntakePushTest(unittest.TestCase):
    """0.77.0 bot intake: @mentions of the bot and DMs to it -> `intake-push` refs (ids only)."""

    def event(self, **event):
        base = {'type': 'app_mention', 'channel': 'C0000TEST01', 'ts': '1700000001.000200', 'user': OWNER}
        return {'type': 'event_callback', 'team_id': TEAM, 'event': {**base, **event}}

    def test_which_events_are_intake(self):
        self.assertTrue(r.is_intake_event(self.event()))
        dm = self.event(type='message', channel='D0000TEST01', channel_type='im')
        self.assertTrue(r.is_intake_event(dm))
        self.assertFalse(r.is_intake_event(self.event(type='message', channel_type='channel')))  # a plain channel post
        self.assertFalse(r.is_intake_event(self.event(type='message', channel='D0000TEST01', channel_type='im',
                                                      thread_ts='1700000000.000100')))  # a DM thread reply: card path
        self.assertFalse(r.is_intake_event({'type': 'event_callback', 'event': 'x'}))

    def test_refs_drops_and_command(self):
        refs, why = r.intake_refs(self.event(), expected_team_id=TEAM)
        self.assertEqual((refs, why), ({'channel': 'C0000TEST01', 'ts': '1700000001.000200', 'user': OWNER}, ''))
        refs, _ = r.intake_refs(self.event(thread_ts='1700000000.000100'), expected_team_id=TEAM)
        self.assertEqual(refs['thread_ts'], '1700000000.000100')  # a mention inside a thread
        for event, code in (({'subtype': 'bot_message'}, 'has_subtype'),
                            ({'channel': 'X1'}, 'bad_channel'), ({'ts': 'now'}, 'bad_ts'), ({'user': 'W1'}, 'bad_user'),
                            ({'thread_ts': 'later'}, 'bad_thread_ts')):
            self.assertEqual(r.intake_refs(self.event(**event), expected_team_id=TEAM), (None, code))
        self.assertEqual(r.intake_refs(self.event(), expected_team_id='T0000TEST02'), (None, 'unexpected_team'))
        cmd = r.intake_push_command({**refs}, python='py', script=Path('/x/kage.py'))
        self.assertEqual(cmd[-9:], ['intake-push', '--channel', 'C0000TEST01', '--ts', '1700000001.000200', '--user',
                                    OWNER, '--thread-ts', '1700000000.000100'])
        self.assertNotIn('--thread-ts', r.intake_push_command(r.intake_refs(self.event(), expected_team_id=TEAM)[0],
                                                              python='py', script=Path('/x/kage.py')))

    def test_the_gate_comes_from_the_runtime(self):
        base = {'ok': True, 'enabled': True, 'channels': [], 'users': []}
        self.assertFalse(r.parse_card_reply_config(base)['intake_push'])
        self.assertTrue(r.parse_card_reply_config({**base, 'intake_push': True})['intake_push'])
        self.assertEqual((r.parse_card_reply_config(base)['intake_ack'],
                          r.parse_card_reply_config({**base, 'intake_ack': True})['intake_ack']), (False, True))
        self.assertEqual(r.parse_card_reply_config({**base, 'delegate_bot_names': ['*', 3, 'x' * 81]})['delegate_bot_names'],
                         ('*',))

    def test_a_failed_command_says_why_as_a_code(self):
        # 0.78.4 (F16): the error type, never the text
        self.assertEqual(r.failure_code('{"error": "ValueError", "detail": "the secret words"}'), 'ValueError')
        self.assertEqual(r.failure_code('Traceback (most recent call last):\n  ...\nRuntimeError: Slack API error: x'),
                         'RuntimeError')
        self.assertEqual(r.failure_code('subprocess.TimeoutExpired: Command timed out'), 'TimeoutExpired')
        self.assertEqual(r.failure_code(''), 'unknown')
        failed = types.SimpleNamespace(returncode=1, stdout='', stderr='{"error": "OperationalError", "detail": "x"}')
        out = r.run_followup_json(['x'], runner=lambda *a, **kw: failed, script=Path('/x/scripts/kage.py'), prefix='intake_push')
        self.assertEqual(out, {'ok': False, 'reason': 'intake_push_failed', 'returncode': 1, 'error': 'OperationalError'})

    def test_a_post_through_an_app_passes_unless_it_is_ours(self):
        # 0.78.3 (F14): a person's own user token through an API client carries bot_id / app_id; the runtime decides
        app = {'bot_id': 'B0000TEST05', 'app_id': 'A0000TEST05', 'bot_profile': {'app_id': 'A0000TEST05', 'name': 'cli'}}
        refs, why = r.intake_refs(self.event(**app), expected_team_id=TEAM, own_bot_user='U0000TEST09',
                                  own_app_id='A0000TEST01')
        self.assertEqual((refs['user'], why), (OWNER, ''))
        self.assertEqual(r.intake_refs(self.event(user='U0000TEST09'), expected_team_id=TEAM,
                                       own_bot_user='U0000TEST09'), (None, 'our_bot'))
        ours = {**app, 'app_id': 'A0000TEST01'}
        self.assertEqual(r.intake_refs(self.event(**ours), expected_team_id=TEAM, own_app_id='A0000TEST01'),
                         (None, 'our_app'))

    def test_which_messages_get_the_receivers_receipt(self):
        # 0.78.3 (F12): decided from the payload alone; never the bot's own message, never a post through an app
        kw = {'expected_team_id': TEAM, 'own_bot_user': 'U0000TEST09'}
        self.assertEqual(r.receipt_refs(self.event(), **kw), ('C0000TEST01', '1700000001.000200', None))
        dm = self.event(type='message', channel='D0000TEST01', channel_type='im')
        self.assertEqual(r.receipt_refs(dm, **kw), ('D0000TEST01', '1700000001.000200', None))
        for event in ({'bot_id': 'B0000TEST05'}, {'app_id': 'A0000TEST05'}, {'bot_profile': {'name': 'x'}},
                      {'user': 'U0000TEST09'}, {'subtype': 'bot_message'}, {'type': 'message', 'channel_type': 'channel'}):
            self.assertIsNone(r.receipt_refs(self.event(**event), **kw), event)
        self.assertIsNone(r.receipt_refs(self.event(), expected_team_id=TEAM, own_bot_user=None))  # unknown bot: none
        # 0.78.4 (F15): a post through an app intake allows -> the person check (its user) in the receipt thread
        app = {'bot_id': 'B0000TEST05', 'app_id': 'A0000TEST05', 'bot_profile': {'app_id': 'A0000TEST05', 'name': 'API-Client'}}
        self.assertEqual(r.receipt_refs(self.event(**app), delegate_names=['*'], **kw),
                         ('C0000TEST01', '1700000001.000200', OWNER))
        self.assertEqual(r.receipt_refs(self.event(**app), delegate_names=['api-*'], **kw)[2], OWNER)  # any case
        self.assertIsNone(r.receipt_refs(self.event(**app), delegate_names=['coder-*'], **kw))
        self.assertIsNone(r.receipt_refs(self.event(**app), **kw))  # [] = off
        self.assertIsNone(r.receipt_refs(self.event(**app), delegate_names=['*'], own_app_id='A0000TEST05', **kw))
        self.assertIsNone(r.receipt_refs(self.event(), expected_team_id='T0000TEST02', own_bot_user='U0000TEST09'))


if __name__ == '__main__':
    unittest.main()
