"""Kage tests never reach the network.

Importing this package (every test module is imported as `tests.<name>`; run them with
`python3 -m unittest discover -s tests -t .`) replaces socket connects and DNS lookups with a refusal, and
drops chat tokens from the environment. `tests/test_offline.py` fails when the suite runs without it.
"""
import os
import socket


class NetworkRefused(OSError):
    """A test tried to reach the network."""


def _refuse(*_args, **_kwargs):
    raise NetworkRefused('Kage tests are offline: no socket connects or DNS lookups')


for _name in [k for k in os.environ if k.startswith(('SLACK_', 'KAGE_LIVE'))]:
    del os.environ[_name]

socket.socket.connect = _refuse
socket.socket.connect_ex = _refuse
socket.create_connection = _refuse
socket.getaddrinfo = _refuse
socket.gethostbyname = _refuse
OFFLINE = True
