import os
import socket
import unittest
import urllib.request

import tests


class OfflineGuardTest(unittest.TestCase):
    def test_suite_runs_with_the_guard(self):
        self.assertTrue(getattr(tests, 'OFFLINE', False))

    def test_socket_connect_is_refused(self):
        with socket.socket() as s, self.assertRaises(tests.NetworkRefused):
            s.connect(('127.0.0.1', 9))

    def test_http_is_refused(self):
        with self.assertRaises(OSError):
            urllib.request.urlopen('https://example.com', timeout=1)

    def test_dns_is_refused(self):
        with self.assertRaises(tests.NetworkRefused):
            socket.getaddrinfo('example.com', 443)

    def test_chat_tokens_are_not_in_the_environment(self):
        self.assertEqual([k for k in os.environ if k.startswith('SLACK_')], [])


if __name__ == '__main__':
    unittest.main()
