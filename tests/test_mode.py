"""O2a / O2b: the instance mode and the observe-only guard (kage.core.mode)."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from kage.adapters.slack import transport as T
from kage.core import mode as M


class ModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / 'state.sqlite'
        self.db = sqlite3.connect(str(self.path))
        self.addCleanup(self.db.close)

    def test_a_store_without_the_row_is_active_and_init_writes_installed_once(self):
        self.assertEqual(M.read(self.db), 'active')  # no table at all: predates O2
        self.db.execute('CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.assertEqual(M.read(self.db), 'active')
        self.assertEqual(M.read(self.db, default='installed'), 'installed')
        self.assertEqual(M.write(self.db, 'installed', 'init', now=1.0), {'old': None, 'new': 'installed'})
        self.assertEqual(M.write(self.db, 'installed', 'init', now=2.0), {'old': 'installed', 'new': 'installed'})
        self.assertEqual(M.read(self.db), 'installed')
        self.assertEqual(len(M.overview(self.db)['log']), 1)

    def test_only_the_allowed_actor_moves_the_mode(self):
        M.write(self.db, 'installed', 'init')
        for new, actor in (('active', 'owner'), ('active', 'services'), ('paused', 'owner')):
            with self.subTest(new=new, actor=actor), self.assertRaises(ValueError):
                M.write(self.db, new, actor)
        M.write(self.db, 'observe', 'services')
        with self.assertRaisesRegex(ValueError, 'observe->active:owner'):  # the start card's button only
            M.write(self.db, 'active', 'owner')
        M.write(self.db, 'active', 'owner_button')
        M.write(self.db, 'paused', 'breaker')
        M.write(self.db, 'active', 'owner')
        M.write(self.db, 'observe', 'owner')
        with self.assertRaises(ValueError):
            M.write(self.db, 'bogus', 'owner')
        self.assertEqual([r['new'] for r in reversed(M.overview(self.db)['log'])],
                         ['installed', 'observe', 'active', 'paused', 'active', 'observe'])

    def test_the_guard_records_writes_instead_of_sending_them_and_lets_the_owners_card_through(self):
        M.write(self.db, 'installed', 'init')
        M.write(self.db, 'observe', 'services')
        self.db.execute('INSERT INTO runtime_meta VALUES (?, ?)', ('card_destination:D0000OWNER', json.dumps(
            {'verdict': 'im', 'error': None, 'checked_at': 1.0})))
        self.db.commit()
        guard = M.WriteGuard(self.path)
        guard('conversations.history', {'channel': 'C0000PUBLIC'})  # reads never reach the store
        guard('chat.postMessage', {'channel': 'D0000OWNER', 'text': 'the daily digest'})
        for endpoint, payload in (('chat.postMessage', {'channel': 'C0000PUBLIC', 'text': 'Hi team'}),
                                  ('reactions.add', {'channel': 'C0000PUBLIC', 'name': 'eyes'}),
                                  ('reactions.add', {'channel': 'D0000OWNER', 'name': 'eyes'}),
                                  ('files.getUploadURLExternal', {'filename': 'x.txt'})):
            with self.subTest(endpoint=endpoint), self.assertRaises(M.ShadowedWrite):
                guard(endpoint, payload)
        shadow = M.overview(self.db)['shadow']
        self.assertEqual(shadow['counts'], {'slack_write': 4})
        self.assertIn('Hi team', ' '.join(r['what'] for r in shadow['recent']))
        with self.assertRaises(M.ShadowedWrite):  # the same action again is one row
            guard('chat.postMessage', {'channel': 'C0000PUBLIC', 'text': 'Hi team'})
        self.assertEqual(M.overview(self.db)['shadow']['counts'], {'slack_write': 4})

    def test_the_guard_is_open_while_active_and_closed_when_it_cannot_tell(self):
        M.write(self.db, 'installed', 'init')
        M.write(self.db, 'observe', 'services')
        M.write(self.db, 'active', 'owner_button')
        M.WriteGuard(self.path)('chat.postMessage', {'channel': 'C0000PUBLIC', 'text': 'Hi'})
        empty = self.tmp / 'empty.sqlite'
        sqlite3.connect(str(empty)).close()
        with self.assertRaises(M.ShadowedWrite):  # a generated deployment's store without the row: installed
            M.WriteGuard(empty)('chat.postMessage', {'channel': 'C0000PUBLIC', 'text': 'Hi'})
        with self.assertRaises(M.ShadowedWrite):  # a store that is not there is never created
            M.WriteGuard(self.tmp / 'none.sqlite')('chat.postMessage', {'channel': 'C1', 'text': 'x'})
        self.assertFalse((self.tmp / 'none.sqlite').exists())
        with self.assertRaises(M.ShadowedWrite):
            M.WriteGuard(self.tmp / 'missing' / 'state.sqlite')('chat.update', {'channel': 'C1', 'text': 'x'})

    def test_the_transport_asks_the_guard_before_any_request(self):
        calls = []

        def guard(endpoint, payload):
            calls.append((endpoint, payload))
            raise M.ShadowedWrite('observe_mode:observe:' + endpoint)

        t = T.Transport(guard_env='KAGE_TEST_NEVER_SET', owner_name='Mira', expected_bot_user='aide', write_guard=guard)
        os.environ.pop('KAGE_TEST_NEVER_SET', None)
        with self.assertRaises(M.ShadowedWrite):
            t.api_call('chat.postMessage', 'xoxb-test', json_body={'channel': 'C1', 'text': 'hi'})
        with self.assertRaises(M.ShadowedWrite):
            t.api_call_form('chat.update', 'xoxb-test', {'channel': 'C1', 'ts': '1.0', 'text': 'hi'})
        self.assertEqual([c[0] for c in calls], ['chat.postMessage', 'chat.update'])


if __name__ == '__main__':
    unittest.main()
