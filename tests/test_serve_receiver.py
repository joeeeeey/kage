"""O1c5d: `serve receiver` — a generated deployment's Socket Mode receiver from Kage's own entry. Offline: a fake
transport, a scripted socket and a fake `websockets` module. Tokens only from the owner's store, the workspace and
bot only from the verified profile, every command through the deployment's own program."""
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest import mock

from kage import app
from kage.ports import secrets
from kage.serve import receiver

from tests.test_init import APP_TOKEN, BOT_TOKEN, BOT_USER, Base, OWNER, TEAM
from tests.test_socket_mode import Socket

CARDS = 'C0000CARDS1'


class FakeTransport:
    def __init__(self, team=TEAM, bot_user=BOT_USER):
        self.team, self.bot_user, self.calls = team, bot_user, []

    def api_call(self, method, token, params=None, json_body=None):
        kind = {BOT_TOKEN: 'bot', APP_TOKEN: 'app'}.get(token, 'other')
        self.calls.append((method, kind))
        if method == 'auth.test' and kind == 'bot':
            return {'ok': True, 'team_id': self.team, 'user_id': self.bot_user}
        if method == 'apps.connections.open' and kind == 'app':
            return {'ok': True, 'url': 'wss://example.invalid/socket'}
        raise RuntimeError('Slack API error: invalid_auth')


class FakeRunner:
    """The program's command runs: records each command line, answers like the runtime would."""

    def __init__(self):
        self.commands = []

    def __call__(self, command, **kw):
        self.commands.append(command)
        verb = next(a for a in command[3:] if not a.startswith('-') and a not in ('--db',))
        answer = {'decide': {'ok': True, 'choice': 'approve'},
                  'card-reply-config': {'ok': True, 'enabled': True, 'channels': [CARDS], 'users': [OWNER],
                                        'ttl_seconds': 300, 'control': True},
                  'card-reply-push': {'ok': True, 'outcome': 'pushed'},
                  'control-push': {'ok': True, 'outcome': 'pushed'}}[verb]
        return subprocess.CompletedProcess(command, 0, json.dumps(answer), '')


def click(n=1):
    return {'type': 'interactive', 'envelope_id': f'click{n}', 'payload': {
        'type': 'block_actions', 'team': {'id': TEAM}, 'user': {'id': OWNER, 'team_id': TEAM},
        'actions': [{'action_id': 'sf_decision:approve', 'value': 'a' * 24}],
        'container': {'channel_id': CARDS, 'message_ts': '1790000000.000100'}, 'response_url': ''}}


def message(envelope_id, ts, thread_ts=None, user=OWNER):
    event = {'type': 'message', 'channel': CARDS, 'ts': ts, 'user': user, **({'thread_ts': thread_ts} if thread_ts else {})}
    return {'type': 'events_api', 'envelope_id': envelope_id,
            'payload': {'type': 'event_callback', 'team_id': TEAM, 'event': event}}


def fake_websockets(sock):
    client = types.ModuleType('websockets.sync.client')
    client.connect = lambda url, **kw: sock
    return {'websockets': types.ModuleType('websockets'), 'websockets.sync': types.ModuleType('websockets.sync'),
            'websockets.sync.client': client}


class ServeReceiverTest(Base):
    def setUp(self):
        super().setUp()
        self.records()
        self.init()
        patch = mock.patch.dict(os.environ, {'HOME': str(self.home), 'KAGE_VERSIONS_DIR': str(self.release.parent)})
        patch.start()
        self.addCleanup(patch.stop)
        self.assembly = app.App(self.target / 'deploy')
        self.runtime = self.assembly.assemble()
        self.store = secrets.for_owner('mira', home=self.home)

    def tokens(self):
        self.store.put(secrets.SLACK_BOT, BOT_TOKEN)
        self.store.put(secrets.SLACK_APP, APP_TOKEN)

    def make(self, *argv, transport=None, runner=None):
        args = receiver.build_parser().parse_args(list(argv))
        r = receiver.Receiver(self.assembly, args, store=self.store, transport=transport or FakeTransport(),
                              poster=lambda url, text: True, runner=runner)
        r.logged = []
        r._log = lambda record, stderr=False: r.logged.append(record)
        return r

    def test_tokens_only_from_the_store(self):
        with self.assertRaisesRegex(receiver.ReceiverRefused, 'slack-app-token, slack-bot-token'):
            self.make()
        self.tokens()
        r = self.make()
        self.assertNotIn(APP_TOKEN, repr(r.app_client) + repr(r.bot_client) + repr(vars(r.args)))
        self.assertEqual((r.args.expected_team_id, r.args.followup_script), (TEAM, str(self.assembly.program)))

    def test_the_preflight_refuses_another_workspace_or_bot(self):
        self.tokens()
        self.make().preflight()
        for transport, code in ((FakeTransport(team='T0000OTHER1'), 'another_workspace'),
                                (FakeTransport(bot_user='U0000OTHER1'), 'another_bot_user')):
            with self.assertRaisesRegex(receiver.ReceiverRefused, code):
                self.make(transport=transport).preflight()
            with mock.patch.dict(sys.modules, fake_websockets(Socket())):
                out = receiver.main(self.assembly, self.runtime, ['--once'], store=self.store, transport=transport)
            self.assertEqual(out, 78)

    def test_buttons_replies_and_control_messages_go_to_the_program(self):
        self.tokens()
        runner, transport = FakeRunner(), FakeTransport()
        r = self.make('--socket-connections', '1', '--idle-timeout-seconds', '2', transport=transport, runner=runner)
        r.ns.STOP_CHECK_SECONDS, r.ns.STAGGER_SECONDS = 0.01, 0.0
        sock = Socket({'type': 'hello', 'num_connections': 1}, click(1),
                      message('reply1', '1790000001.000200', thread_ts='1790000000.000100'),
                      message('top1', '1790000002.000300'), message('reply1', '1790000001.000200', '1790000000.000100'))
        with mock.patch.dict(sys.modules, fake_websockets(sock)):
            self.assertEqual(r.run(), 0)
        self.assertEqual(sock.sent, ['click1', 'reply1', 'top1', 'reply1'])  # every envelope acked, once each
        self.assertIn(('apps.connections.open', 'app'), transport.calls)
        self.assertIn(('auth.test', 'bot'), transport.calls)
        program = str(self.target / 'scripts/kage.py')
        verbs = [c[3] for c in runner.commands]
        # 0.78.3: the gate is fetched once at start (the receipt thread needs it warm), then cached
        self.assertEqual(verbs, ['card-reply-config', 'decide', 'card-reply-push', 'control-push'])
        self.assertTrue(all(c[:3] == [sys.executable, '-I', program] for c in runner.commands))
        events = [rec.get('event') for rec in r.logged]
        self.assertIn('decision_handled', events)
        self.assertIn('envelope_duplicate', events)
        self.assertNotIn(APP_TOKEN, json.dumps(r.logged))
        self.assertNotIn(BOT_TOKEN, json.dumps(r.logged))

    def test_the_real_program_answers_the_receivers_commands(self):
        self.tokens()
        r = self.make()
        config = r.card_reply_settings()
        self.assertFalse(config['error'], r.logged)
        r._seen = {}
        result = r.process_decision(click(2)['payload'])
        self.assertEqual(result['event'], 'decision_handled', r.logged)
        self.assertFalse(result['ok'])  # no such card in a new store; the program answered, nothing was recorded


if __name__ == '__main__':
    unittest.main()
