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
