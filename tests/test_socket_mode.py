"""O0: the Socket Mode loop (kage.adapters.slack.socket_mode) over a deployment namespace. Offline: a scripted socket,
fake clients and a fake `websockets` module; nothing reaches Slack."""
from __future__ import annotations

from collections import OrderedDict
import json
import queue
import sys
import threading
import time
import types
import unittest
from unittest import mock

from kage.adapters.slack import socket_mode as S


class Socket:
    def __init__(self, *items):
        self.items, self.sent = list(items), []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout):
        if not self.items:
            time.sleep(min(timeout, 0.005))
            raise TimeoutError
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    def send(self, data):
        self.sent.append(json.loads(data)["envelope_id"])

    def ping(self):
        return mock.Mock(wait=lambda timeout: True)


def namespace(**overrides):
    ns = types.SimpleNamespace(**{name: getattr(S, name) for name in dir(S) if not name.startswith('__')})
    ns.STOP_CHECK_SECONDS, ns.STAGGER_SECONDS = 0.01, 0.0
    ns.is_decision_action = lambda payload: payload.get('type') == 'block_actions'
    ns.is_card_reply_event = lambda payload: False
    ns.decisions = []
    ns.handle_decision_action = lambda payload, **kw: ns.decisions.append(payload['n']) or {'event': 'decision_handled'}
    ns.FOLLOWUP_SCRIPT, ns.DECISION_TIMEOUT_SECONDS = '/nonexistent/cli.py', 20.0
    ns.decision_error_diagnostic = lambda payload, exc: {'event': 'decision_error'}
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


class Runner(S.SocketModeRunner):
    def __init__(self, ns, **args):
        self.ns = ns
        self.args = types.SimpleNamespace(**{'idle_timeout_seconds': 0, 'liveness_interval_seconds': 10.0,
                                             'pong_timeout_seconds': 5.0, 'socket_connections': 1, 'once': False,
                                             'followup_script': ns.FOLLOWUP_SCRIPT, 'followup_db': None,
                                             'decision_timeout_seconds': 20.0, 'expected_team_id': 'T0000TEST01', **args})
        self.app_client = mock.Mock()
        self.app_client.call.return_value = ({'ok': True, 'url': 'wss://socket.example/1'}, {})
        self.logged, self.other = [], []

    def preflight(self):
        return None

    def _log(self, record, stderr=False):
        self.logged.append(record)

    def handle_interactive(self, payload):
        self.other.append(payload)
        return bool(self.args.once)


def interactive(envelope_id, n, kind='block_actions'):
    return {'type': 'interactive', 'envelope_id': envelope_id, 'payload': {'type': kind, 'n': n}}


class SocketModeTest(unittest.TestCase):
    def runner(self, **args):
        r = Runner(namespace(), **args)
        r._stop, r._inbox, r._seen = threading.Event(), queue.Queue(), OrderedDict()
        return r

    def test_acks_every_envelope_queues_payloads_and_returns_on_disconnect(self):
        r = self.runner()
        sock = Socket({'type': 'hello', 'num_connections': 2}, interactive('e1', 1), interactive('e2', 2, 'view_submission'),
                      {'type': 'disconnect', 'reason': 'refresh_requested'})
        self.assertEqual(r._socket_session(0, lambda url, **kw: sock, 10.0, 5.0, lambda: None), 'disconnect')
        self.assertEqual(sock.sent, ['e1', 'e2'])
        self.assertEqual([r._handle_envelope(r._inbox.get()) for _ in range(2)], [False, False])
        self.assertEqual((r.ns.decisions, [p['n'] for p in r.other]), ([1], [2]))
        self.assertEqual(r.logged[0], {'event': 'receiver_ready', 'slot': 0, 'liveness_interval_seconds': 10.0,
                                       'pong_timeout_seconds': 5.0})

    def test_a_duplicate_envelope_is_processed_once(self):
        r = self.runner()
        for _ in range(2):
            r._handle_envelope(interactive('e1', 1))
        self.assertEqual(r.ns.decisions, [1])
        self.assertEqual(r.logged[-1], {'event': 'envelope_duplicate', 'envelope_type': 'interactive'})

    def test_link_disabled_raises_and_the_base_handler_ignores_other_payloads(self):
        r = self.runner()
        with self.assertRaises(S.SocketModeDisabled):
            r._socket_session(1, lambda url, **kw: Socket({'type': 'disconnect', 'reason': 'link_disabled'}),
                              10.0, 5.0, lambda: None)
        self.assertFalse(S.SocketModeRunner.handle_interactive(r, {'type': 'shortcut'}))
        with self.assertRaises(NotImplementedError):
            S.SocketModeRunner.preflight(r)

    def test_the_backoff_and_error_codes(self):
        self.assertEqual([S.next_backoff(x) for x in (0.0, 1.0, 16.0, 30.0)], [1.0, 2.0, 30.0, 30.0])
        self.assertTrue(S.is_transient_transport_error(RuntimeError('Slack API HTTP 503 for apps.connections.open: x')))
        self.assertFalse(S.is_transient_transport_error(RuntimeError('Slack API error for auth.test: invalid_auth')))
        self.assertEqual(S.transport_error_code(RuntimeError('Slack API error for auth.test: invalid_auth')),
                         'slack:invalid_auth')

    def test_a_mention_gets_the_receipt_at_once_while_the_dispatch_waits(self):
        # 0.78.3 (F12: 👀 within 3 s, every time): the receipt goes out from its own thread right after the ack,
        # whatever the dispatch thread is doing (here it never runs); once per message; never on the bot's own post
        from kage.adapters.slack import receiver as R
        r = Runner(namespace(receipt_refs=R.receipt_refs, _safe=R._safe, SAFE_CODE_RE=R.SAFE_CODE_RE))
        r._stop, r._inbox, r._seen = threading.Event(), queue.Queue(), OrderedDict()
        r._receipts, r._receipt_lock, r._receipted = queue.Queue(), threading.Lock(), OrderedDict()
        r.own_bot_user, r._card_reply_cache = 'U0000TEST09', (float('inf'), {'intake_ack': True})
        calls = []
        r.bot_client = mock.Mock()
        r.bot_client.call.side_effect = lambda method, json_body=None: calls.append((time.monotonic(), method, json_body))

        def mention(envelope_id, user='U0000TEST01', ts='1700000001.000200'):
            return {'type': 'events_api', 'envelope_id': envelope_id, 'payload': {
                'type': 'event_callback', 'team_id': 'T0000TEST01',
                'event': {'type': 'app_mention', 'channel': 'C0000TEST01', 'ts': ts, 'user': user}}}
        sock = Socket(mention('e1'), mention('e2'), mention('e3', user='U0000TEST09', ts='1700000002.000300'),
                      {'type': 'disconnect', 'reason': 'refresh_requested'})
        thread = threading.Thread(target=r._receipt_loop, daemon=True)
        thread.start()
        started = time.monotonic()
        self.assertEqual(r._socket_session(0, lambda url, **kw: sock, 10.0, 5.0, lambda: None), 'disconnect')
        deadline = time.monotonic() + 2.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(0.05)
        r._stop.set()
        thread.join(1.0)
        self.assertEqual(sock.sent, ['e1', 'e2', 'e3'])  # every envelope acked first
        self.assertEqual([(m, b) for _, m, b in calls], [('reactions.add', {'channel': 'C0000TEST01',
                                                                          'timestamp': '1700000001.000200',
                                                                          'name': 'eyes'})])
        self.assertLess(calls[0][0] - started, 1.0)  # the end-to-end bound the pod measures is 3 s
        self.assertEqual(r._inbox.qsize(), 3)  # the dispatch thread still gets every envelope
        self.assertEqual(r.logged[-1]['outcome'], 'added')
        r._receipted.clear()
        r._card_reply_cache = (float('inf'), {'intake_ack': False})  # the gate off: no receipt
        r._receipt_now(mention('e4'))
        self.assertTrue(r._receipts.empty())

    def receipt_runner(self, tmp=None):
        from kage.adapters.slack import receiver as R
        r = Runner(namespace(receipt_refs=R.receipt_refs, _safe=R._safe, SAFE_CODE_RE=R.SAFE_CODE_RE))
        r._stop, r._inbox, r._seen = threading.Event(), queue.Queue(), OrderedDict()
        r._receipts, r._receipt_lock, r._receipted = queue.Queue(), threading.Lock(), OrderedDict()
        r.own_bot_user, r.own_app_id = 'U0000TEST09', 'A0000TEST01'
        r.calls = []

        def call(method, params=None, json_body=None):
            r.calls.append((method, params, json_body))
            if method == 'users.info':
                return {'user': {'id': params['user'], 'is_bot': params['user'] == 'U0000APBOT1', 'deleted': False}}, {}
            return {'ok': True}, {}
        r.bot_client = mock.Mock()
        r.bot_client.call.side_effect = call
        return r

    @staticmethod
    def app_mention(envelope_id, user, ts):
        return {'type': 'events_api', 'envelope_id': envelope_id, 'payload': {
            'type': 'event_callback', 'team_id': 'T0000TEST01',
            'event': {'type': 'app_mention', 'channel': 'C0000TEST01', 'ts': ts, 'user': user, 'bot_id': 'B0000TEST05',
                      'app_id': 'A0000TEST05', 'bot_profile': {'app_id': 'A0000TEST05', 'name': 'api-client'}}}}

    def drain(self, r):
        r._stop.clear()
        thread = threading.Thread(target=r._receipt_loop, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2.0
        while not r._receipts.empty() and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(0.05)
        r._stop.set()
        thread.join(1.0)

    def test_an_api_post_by_a_person_gets_the_receipt_after_a_cached_check(self):
        # 0.78.4 (F15): users.info once per user (cached), in the receipt thread; a bot user's post gets none
        r = self.receipt_runner()
        r._card_reply_cache = (float('inf'), {'intake_ack': True, 'delegate_bot_names': ('*',)})
        r._receipt_now(self.app_mention('e1', 'U0000TEST01', '1700000001.000200'))
        r._receipt_now(self.app_mention('e2', 'U0000TEST01', '1700000002.000300'))
        r._receipt_now(self.app_mention('e3', 'U0000APBOT1', '1700000003.000400'))
        self.drain(r)
        self.assertEqual([c[0] for c in r.calls], ['users.info', 'reactions.add', 'reactions.add', 'users.info'])
        self.assertEqual([rec['outcome'] for rec in r.logged if rec.get('event') == 'receipt'],
                         ['added', 'added', 'not_a_person'])
        r._card_reply_cache = (float('inf'), {'intake_ack': True, 'delegate_bot_names': ()})  # apps not allowed: none
        r._receipt_now(self.app_mention('e4', 'U0000TEST01', '1700000004.000500'))
        self.assertTrue(r._receipts.empty())

    def test_a_failed_push_is_tried_once_more_and_its_cause_logged(self):
        # 0.78.4 (F16): the first push after a restart failed with no cause in the log
        from kage.adapters.slack import receiver as R
        ns = namespace(**{name: getattr(R, name) for name in ('is_intake_event', 'intake_refs', 'intake_push_command',
                                                              'run_followup_json', '_safe', 'SAFE_CODE_RE',
                                                              'SAFE_ERROR_RE')})
        r = Runner(ns)
        r._card_reply_cache = (float('inf'), {'intake_push': True})
        answers = [types.SimpleNamespace(returncode=1, stdout='', stderr='{"error": "OperationalError", "detail": "x"}'),
                   types.SimpleNamespace(returncode=0, stdout='{"ok": true, "outcome": "accepted"}', stderr='')]
        r.followup_runner = lambda *a, **kw: answers.pop(0)
        payload = {'type': 'event_callback', 'team_id': 'T0000TEST01', 'event': {
            'type': 'app_mention', 'channel': 'C0000TEST01', 'ts': '1700000001.000200', 'user': 'U0000TEST01'}}
        with mock.patch.object(S.time, 'sleep'):
            record = r.process_intake(payload)
        self.assertEqual((record['ok'], record['outcome'], record['attempts']), (True, 'accepted', 2))
        retry = next(rec for rec in r.logged if rec.get('event') == 'intake_push_retry')
        self.assertEqual((retry['reason'], retry['returncode'], retry['error']), ('intake_push_failed', 1, 'OperationalError'))

    def test_the_owners_card_reply_through_an_allowed_app_is_pushed(self):
        # 0.78.8 (F20): no longer dropped as has_bot_id; the runtime re-reads it and checks the person
        from kage.adapters.slack import receiver as R
        ns = namespace(**{name: getattr(R, name) for name in (
            'is_card_reply_event', 'card_reply_refs', 'card_reply_app_refs', 'card_reply_drop_diagnostic',
            'card_reply_push_command', 'run_followup_json', '_safe', 'SAFE_CODE_RE', 'SAFE_ERROR_RE')})
        r = Runner(ns)
        r.own_bot_user, r.own_app_id = 'U0000TEST09', 'A0000TEST01'
        gate = {'enabled': True, 'channels': frozenset({'C0000TEST01'}), 'users': frozenset({'U0000TEST01'}),
                'error': False, 'delegate_bot_names': ('api-*',)}
        r._card_reply_cache = (float('inf'), gate)
        commands = []
        r.followup_runner = lambda command, **kw: commands.append(command) or types.SimpleNamespace(
            returncode=0, stdout='{"ok": true, "outcome": "routed"}', stderr='')
        event = {'type': 'message', 'channel': 'C0000TEST01', 'ts': '1700000001.000200', 'thread_ts': '1700000000.000100',
                 'user': 'U0000TEST01', 'bot_id': 'B0000TEST05', 'app_id': 'A0000TEST05',
                 'bot_profile': {'app_id': 'A0000TEST05', 'name': 'API-Client'}}
        payload = {'type': 'event_callback', 'team_id': 'T0000TEST01', 'event': event}
        record = r.process_card_reply(payload)
        self.assertEqual((record['event'], record['outcome']), ('card_reply_pushed', 'routed'))
        self.assertIn('card-reply-push', commands[0])
        r._card_reply_cache = (float('inf'), {**gate, 'delegate_bot_names': ()})
        self.assertEqual(r.process_card_reply(payload)['reason'], 'app_not_delegated')
        r._card_reply_cache = (float('inf'), gate)
        other = {**payload, 'event': {**event, 'user': 'U0000TEST05'}}
        self.assertEqual(r.process_card_reply(other)['reason'], 'user_not_allowed')
        self.assertEqual(len(commands), 1)

    def test_after_a_restart_the_last_good_gate_is_used(self):
        # 0.78.4 (F16): no good cached gate yet (cold, or the first fetch failed): the gate saved on disk
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            r = self.receipt_runner()
            r.gate_file = Path(tmp) / 'receiver-gate.json'
            r._save_gate({'intake_ack': True, 'delegate_bot_names': ('*',)})
            self.assertEqual(json.loads(r.gate_file.read_text()), {'intake_ack': True, 'delegate_bot_names': ['*']})
            fresh = self.receipt_runner()
            fresh.gate_file = r.gate_file
            fresh._card_reply_cache = (float('inf'), {'intake_ack': False, 'error': True})  # the first fetch failed
            fresh._receipt_now({'type': 'events_api', 'envelope_id': 'e1', 'payload': {
                'type': 'event_callback', 'team_id': 'T0000TEST01',
                'event': {'type': 'app_mention', 'channel': 'C0000TEST01', 'ts': '1700000001.000200', 'user': 'U0000TEST01'}}})
            self.assertEqual(fresh._receipts.get_nowait()[0], ('C0000TEST01', '1700000001.000200', None))

    def test_run_imports_websockets_only_when_it_runs_and_honours_once(self):
        self.assertNotIn('websockets', sys.modules.get(S.__name__).__dict__)
        r = Runner(namespace(), once=True)
        sock = Socket(interactive('e1', 7, 'message_action'))
        client = types.ModuleType('websockets.sync.client')
        client.connect = lambda url, **kw: sock
        fake = {'websockets': types.ModuleType('websockets'), 'websockets.sync': types.ModuleType('websockets.sync'),
                'websockets.sync.client': client}
        with mock.patch.dict(sys.modules, fake):
            self.assertEqual(r.run(), 0)
        self.assertEqual(([p['n'] for p in r.other], sock.sent), ([7], ['e1']))


if __name__ == '__main__':
    unittest.main()
