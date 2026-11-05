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


class StartCardTest(unittest.TestCase):
    """O2d: the daily start card of an observe-only instance; only the owner's own click starts it."""
    OWNER = 'U0000OWNER1'

    def setUp(self):
        import types
        from kage.core import decisions as Dec
        from kage.core import store
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = sqlite3.connect(str(self.tmp / 'state.sqlite'))
        self.db.executescript(store.CORE_DDL)  # escalations, escalation_cards, decisions, runtime_meta
        self.addCleanup(self.db.close)
        self.cards, self.posts = [], []

        def supervisor_card(db, question, options, **kw):
            eid = f'ev{len(self.cards)}'
            self.cards.append({'event_id': eid, 'question': question, 'options': options, **kw})
            db.execute('INSERT INTO escalation_cards (event_id, channel, options, created) VALUES (?, ?, ?, ?)',
                       (eid, 'D0000OWNER', json.dumps([{'id': o.split('=')[0], 'label': o.split('=')[1].split(':')[0],
                                                        'kind': o.split(':')[-1]} for o in options]), 1.0))
            db.execute("INSERT INTO escalations (event_id, stage, thread_ts, client_msg_id) VALUES (?, 'card', ?, 'x')",
                       (eid, f'{len(self.cards)}.0'))
            db.commit()
            return {'event_id': eid}

        self.rt = types.SimpleNamespace(
            supervisor_card=supervisor_card, DEPLOYMENT=types.SimpleNamespace(owner=types.SimpleNamespace(slack_user=self.OWNER)),
            buttons=types.SimpleNamespace(resolve_choice=Dec.resolve_choice, card_model=lambda stored: Dec.card_model(stored, [])),
            slack=types.SimpleNamespace(api_call=lambda *a, **kw: self.posts.append(kw.get('json_body')) or {'ts': '9.0'}),
            api_identity=lambda bot=False: 'xoxb-test')
        self.cards_core = M.StartCards('en', owner_key='mira', owner_name='Mira')

    def click(self, eid, choice, actor=OWNER, provenance='slack_button', role='approver'):
        self.db.execute('INSERT INTO decisions (event_id, choice, actor, role, decided_at, card_channel, card_ts, '
                        "stage, provenance) VALUES (?, ?, ?, ?, 5.0, ?, ?, 'decided', ?)",
                        (eid, choice, actor, role, 'D0000OWNER', '1.0', provenance))
        self.db.commit()

    def test_nothing_while_active_one_card_a_day_while_observing_and_only_the_owners_click_starts(self):
        self.assertEqual(self.cards_core.step(self.rt, self.db, now=100.0), {})  # no row: active, nothing posted
        M.write(self.db, 'installed', 'init', now=1.0)
        M.write(self.db, 'observe', 'services', now=2.0)
        M.shadow(self.db, 'slack_write', 'C0000PUBLIC', 'reply "on it" in #general', 'mode_observe', now=50.0)
        M.shadow(self.db, 'dispatch', 'release-task', 'start a run (event: events)', 'mode_observe', now=60.0)
        self.cards_core.step(self.rt, self.db, now=100.0)
        self.assertEqual(len(self.cards), 1)
        card = self.cards[0]
        self.assertIn(f'<@{self.OWNER}> Your Kage is still in observe-only mode: 2 things', card['question'])
        self.assertIn('1 agent runs', card['question'])
        self.assertNotIn('/onboarding', card['question'])  # no console address configured: no link
        self.assertIn('reply "on it" in #general', card['question'])
        self.assertEqual(self.cards_core.line('triage', json.dumps({'burst': 'b1', 'route': 'task', 'why': 'Needs work.'})),
                         'would start a task: Needs work.')  # O2c rows read as their route and why
        self.assertEqual([o.split(':')[-1] for o in card['options']], ['approve', 'other'])
        self.assertEqual((card['kind'], card['never_auto']), ('mode_start', True))
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM button_only_cards WHERE event_id='ev0'").fetchone())
        self.cards_core.step(self.rt, self.db, now=200.0)
        self.assertEqual(len(self.cards), 1)  # one open card at a time
        self.click('ev0', 'opt:start', actor='U0000OTHER9')  # someone else's click starts nothing
        self.cards_core.step(self.rt, self.db, now=300.0)
        self.assertEqual(M.read(self.db), 'observe')
        self.assertIn('Only your own button click counts', self.posts[-1]['text'])
        self.cards_core.step(self.rt, self.db, now=100.0 + M.DIGEST_SECONDS)  # the next day's card
        self.assertEqual(len(self.cards), 2)
        self.click('ev1', 'opt:later')
        self.cards_core.step(self.rt, self.db, now=110.0 + M.DIGEST_SECONDS)
        self.assertEqual(M.read(self.db), 'observe')
        self.cards_core.step(self.rt, self.db, now=100.0 + 2 * M.DIGEST_SECONDS)
        self.click('ev2', 'opt:start')
        self.cards_core.step(self.rt, self.db, now=110.0 + 2 * M.DIGEST_SECONDS)
        self.assertEqual(M.read(self.db), 'active')
        self.assertEqual(M.overview(self.db)['log'][0]['actor'], 'owner_button')
        self.assertIn('Started', self.posts[-1]['text'])
        self.cards_core.step(self.rt, self.db, now=100.0 + 9 * M.DIGEST_SECONDS)
        self.assertEqual(len(self.cards), 3)  # active: no more cards

    def test_the_card_links_the_console_list_when_the_console_has_an_address(self):
        M.write(self.db, 'installed', 'init', now=1.0)
        M.write(self.db, 'observe', 'services', now=2.0)
        self.rt.DASHBOARD_URL = 'http://127.0.0.1:8776/'
        self.cards_core.step(self.rt, self.db, now=100.0)
        self.assertIn('Everything recorded: http://127.0.0.1:8776/onboarding', self.cards[0]['question'])

    def test_an_unanswered_card_expires_and_a_typed_decision_does_not_start(self):
        M.write(self.db, 'installed', 'init')
        M.write(self.db, 'observe', 'services')
        self.cards_core.step(self.rt, self.db, now=100.0)
        self.click('ev0', 'opt:start', provenance='thread_reply')
        self.cards_core.step(self.rt, self.db, now=101.0)
        self.assertEqual(M.read(self.db), 'observe')
        self.cards_core.step(self.rt, self.db, now=100.0 + M.DIGEST_SECONDS)
        self.cards_core.step(self.rt, self.db, now=100.0 + M.DIGEST_SECONDS + M.CARD_TTL + 1)
        self.assertEqual(self.db.execute("SELECT state FROM mode_cards WHERE event_id='ev1'").fetchone()[0], 'expired')


if __name__ == '__main__':
    unittest.main()
