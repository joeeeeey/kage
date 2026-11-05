"""O1c5c: `serve dashboard` — a generated deployment's console from Kage's own entry: every hook the read models use
exists on the assembled units, the pages carry only the owner's words, every read-only API answers on loopback, and
the deployment's program serves it as a service would (`scripts/kage.py serve dashboard --port 0`)."""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from kage import app
from kage.observe import dashboard as observe
from kage.serve import dashboard

from tests.test_init import Base

HOOK_ATTRS = re.compile(r'(?:self|core)\.(' + '|'.join(
    ['trace', 'previews', 'avatars', *dashboard.HOOKS]) + r')\.([A-Za-z_]+)')
LEAF_OF = {'avatars': 'avatar_cache'}
READ_APIS = ('/api/state', '/api/runs', '/api/backlog', '/api/intake', '/api/inbox', '/api/rules', '/api/routines',
             '/api/gaps', '/api/learning', '/api/cache', '/api/health', '/api/settings', '/api/design', '/api/control')


def get(server, target):
    """GET through the server's handler without a socket (the suite is offline): (status, body)."""
    handler = observe.Handler.__new__(observe.Handler)
    handler.server = server
    handler.headers, handler.path, sent, headers = {'Host': f'127.0.0.1:{server.server_port}'}, target, [], {}
    handler.respond = lambda status, payload, content_type=None, csp=None: sent.append((status, payload))
    handler.send_response = lambda status, message=None: sent.append((status, b''))
    handler.send_header = lambda key, value: headers.__setitem__(key, value)
    handler.end_headers = lambda: None
    handler.do_GET()
    return sent[0]


class ServeDashboardTest(Base):
    def setUp(self):
        super().setUp()
        self.records()
        self.init()
        self.home_env = mock.patch.dict(os.environ, {'HOME': str(self.home)})
        self.home_env.start()
        self.addCleanup(self.home_env.stop)
        self.assembly = app.App(self.target / 'deploy')
        self.runtime = self.assembly.assemble()

    def test_every_hook_the_read_models_use_exists(self):
        core, _server = dashboard.build(self.assembly)
        used = set(HOOK_ATTRS.findall(Path(observe.__file__).read_text()))
        self.assertGreater(len(used), 40)
        missing = sorted(f'{hook}.{attr}' for hook, attr in used
                         if not hasattr(getattr(core, hook), attr))
        self.assertEqual(missing, [])

    def test_the_pages_carry_only_the_owners_words(self):
        values = dashboard.page_values(self.assembly)
        self.assertEqual((values['owner_name'], values['owner_key'], values['tz_name']), ('Mira', 'mira', 'Europe/Berlin'))
        self.assertEqual(values['service_label'], 'pro.kage.mira.followup')
        self.assertEqual(values['tail_command'], 'scripts/kage.py tail')
        self.assertEqual(dashboard.page_language('en'), dashboard.FALLBACK_PAGES)

    def test_every_read_api_answers_on_loopback(self):
        _core, server_class = dashboard.build(self.assembly)
        db = self.home / '.local/share/kage/state-mira/state.sqlite'
        self.runtime['connect'](db).close()  # the store as the followup service leaves it once it has run
        server = server_class(('127.0.0.1', 0), db, enrich_profiles=False)
        self.addCleanup(server.server_close)
        status, body = get(server, '/')
        self.assertEqual(status, 200)
        self.assertIn(b'Mira', body)
        for path in READ_APIS:
            status, body = get(server, path)
            data = json.loads(body) if isinstance(body, (bytes, str)) else body
            self.assertEqual(status, 200, (path, str(data)[:300]))
            self.assertIsInstance(data, (dict, list), path)
        with self.assertRaises(ValueError):
            server_class(('0.0.0.0', 0), db)
        with sqlite3.connect(db) as conn:  # the console only read the store
            self.assertEqual(conn.execute('SELECT step FROM onboarding_steps').fetchall(), [('init',)])


class ServeDashboardProgramTest(Base):
    def test_the_program_serves_the_console(self):
        self.records()
        self.init()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('SLACK_', 'KAGE_')) and k != 'PYTHONPATH'}
        env.update({'HOME': str(self.home), 'KAGE_NO_REAL_SLACK': '1', 'KAGE_SECRETS_BACKEND': 'file',
                    'KAGE_VERSIONS_DIR': str(self.release.parent), 'PYTHONDONTWRITEBYTECODE': '1'})
        proc = subprocess.Popen([sys.executable, str(self.target / 'scripts/kage.py'), 'serve', 'dashboard', '--port',
                                 '0', '--no-enrichment'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=env, stdin=subprocess.DEVNULL, cwd=str(self.tmp))
        self.addCleanup(lambda: (proc.terminate(), proc.wait(10), proc.stdout.close(), proc.stderr.close()))
        line = proc.stdout.readline()  # printed once the console is bound (the suite makes no connection to it)
        m = re.search(r'Kage console: http://127\.0\.0\.1:(\d+)', line)
        self.assertTrue(m, line + proc.stderr.read() if proc.poll() is not None else line)
        self.assertIsNone(proc.poll())
        bad = subprocess.run([sys.executable, str(self.target / 'scripts/kage.py'), 'serve', 'nothing'],
                             capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(bad.returncode, 2, bad.stderr)


if __name__ == '__main__':
    unittest.main()
