import sqlite3
import types
import unittest

from kage.adapters.slack import card_replies, card_threads

OWNER, DEPUTY, OTHER, BOT = 'U0000TEST01', 'U0000TEST04', 'U0000TEST05', 'U0000TEST09'
CHANNEL, ROOT = 'C0000TEST01', '1700000000.000100'
REPLIES = card_replies.CardReplies('mira', OWNER)
THREADS = card_threads.CardThreads('mira', OWNER)


def runtime(env=None, delegates=(), cards=None):
    """The runtime handle the modules call: envelopes, buttons, worker config, the sibling module."""
    env = env or {'author_kind': 'human', 'trust': 'mira', 'authority': 'mira_intent'}
    cards = cards or {'task_thread': True, 'realtime_replies': True}
    rt = types.SimpleNamespace(
        BOT=BOT, AGENT_EVENT_TYPE='kage_agent',
        envelopes=types.SimpleNamespace(classify_message=lambda m, **kw: env, load_people=lambda: {},
                                        active_delegates=lambda config, ts, buttons: list(delegates)),
        buttons=types.SimpleNamespace(load_config=lambda: {'decision_channel': CHANNEL, 'ooo': {
            'active': bool(delegates), 'delegates': list(delegates), 'channel': None}},
            decision_channels=lambda config: {CHANNEL}),
        worker_config=types.SimpleNamespace(load=lambda: ({'cards': cards}, None), DEFAULTS={'cards': cards}))
    rt.card_replies = types.SimpleNamespace(owner_typed=REPLIES.owner_typed)
    return rt


def store():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE agent_sends (channel TEXT, ts TEXT); CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, '
                     'value TEXT);' + card_replies.DDL + 'ALTER TABLE card_reply_routes ADD COLUMN reply_class TEXT;')
    return db


def reply(ts='1700000001.000200', user=OWNER, **extra):
    return {'ts': ts, 'thread_ts': ROOT, 'user': user, 'text': '批准', **extra}


class CardRepliesTest(unittest.TestCase):
    def test_owner_typed(self):
        db, rt = store(), runtime()
        self.assertTrue(REPLIES.owner_typed(rt, db, CHANNEL, reply(), ROOT))
        for m in (reply(user=OTHER), reply(ts=ROOT), reply(ts='1600000000.000100'), {**reply(), 'thread_ts': 'x'},
                  reply(ts='not-a-ts')):
            self.assertFalse(REPLIES.owner_typed(rt, db, CHANNEL, m, ROOT), m)
        for env in ({'author_kind': 'agent', 'trust': 'mira', 'authority': 'mira_intent'},
                    {'author_kind': 'human', 'trust': 'colleague', 'authority': 'mira_intent'},
                    {'author_kind': 'human', 'trust': 'mira', 'authority': 'none'}):
            self.assertFalse(REPLIES.owner_typed(runtime(env), db, CHANNEL, reply(), ROOT), env)

    def test_the_owners_reply_through_an_allowed_app_counts_on_a_bot_deployment(self):
        # 0.78.8 (F20): their own user token through an API client (bot_id / app_id set), as F14 for intake
        import contextlib
        replies = card_replies.CardReplies('mira', OWNER)
        intake = {'identity': 'bot', 'delegate_bot_names': ['api-*']}
        calls = []
        env = {'author_kind': 'agent', 'trust': 'agent', 'authority': 'none', 'rule': 'agent_foreign_app'}
        db, rt = store(), runtime(env)
        rt.worker_config = types.SimpleNamespace(load=lambda: ({'cards': {}, 'intake': intake}, None))
        person = {'is_bot': False}
        rt.slack = types.SimpleNamespace(api_call=lambda method, token, params=None: calls.append(params['user']) or
                                         {'user': dict(person)})
        rt.api_identity = lambda bot=False: 'bot-token'

        @contextlib.contextmanager
        def transaction(db):
            db.execute('BEGIN IMMEDIATE')
            yield
            db.commit()
        rt.transaction = transaction
        api = reply(bot_id='B0000TEST07', app_id='A0000TEST07', bot_profile={'app_id': 'A0000TEST07', 'name': 'API-Client'})
        self.assertTrue(replies.owner_typed(rt, db, CHANNEL, api, ROOT))
        self.assertEqual(db.execute('SELECT user, app FROM owner_api_replies').fetchall()[0][:], (OWNER, 'API-Client'))
        self.assertTrue(replies.owner_typed(rt, db, CHANNEL, {**api, 'ts': '1700000002.000200'}, ROOT))
        self.assertEqual(calls, [OWNER])                                                     # users.info cached
        self.assertFalse(replies.owner_typed(rt, db, CHANNEL, {**api, 'user': OTHER}, ROOT))  # not the owner
        self.assertFalse(replies.owner_typed(rt, db, CHANNEL, {**api, 'subtype': 'bot_message'}, ROOT))
        self.assertFalse(replies.owner_typed(rt, db, CHANNEL, {**api, 'bot_profile': {'name': 'deploy-bot'}}, ROOT))
        intake['identity'] = 'user'  # agents may post with the owner's token here: never theirs
        self.assertFalse(replies.owner_typed(rt, db, CHANNEL, api, ROOT))
        intake['identity'] = 'bot'
        fresh = card_replies.CardReplies('mira', OWNER)
        person['is_bot'] = True
        self.assertFalse(fresh.owner_typed(rt, db, CHANNEL, api, ROOT))                      # an app's own bot user
        for rule in ('agent_app_id', 'agent_metadata', 'agent_ledger'):                      # ours: never
            self.assertFalse(replies.owner_typed(runtime({**env, 'rule': rule}), db, CHANNEL, api, ROOT), rule)
        # the thread router takes it as the owner's reply (it no longer drops every bot_id before asking)
        rt.card_replies = types.SimpleNamespace(owner_typed=replies.owner_typed)
        self.assertEqual(THREADS.eligible_author(rt, db, CHANNEL, ROOT, {**api, 'ts': '1700000003.000200'}), 'mira')
        self.assertIsNone(THREADS.eligible_author(rt, db, CHANNEL, ROOT, {**api, 'ts': '1700000004.000200', 'user': OTHER}))

    def test_an_owner_like_approvers_reply_is_the_owners(self):
        # 0.77.0: a bot-only deployment's approvers with `intake.owner_like: approvers` (kage.core.authority)
        db, rt = store(), runtime()
        self.assertFalse(REPLIES.owner_typed(rt, db, CHANNEL, reply(user=DEPUTY), ROOT))
        self.assertEqual(THREADS.receiver_config(rt, db)['users'], [OWNER])
        rt.envelopes.ENVELOPES = types.SimpleNamespace(owner_like=lambda: frozenset({DEPUTY}))
        self.assertTrue(REPLIES.owner_typed(rt, db, CHANNEL, reply(user=DEPUTY), ROOT))
        self.assertFalse(REPLIES.owner_typed(rt, db, CHANNEL, reply(user=OTHER), ROOT))
        self.assertEqual(THREADS.receiver_config(rt, db)['users'], sorted({OWNER, DEPUTY}))

    def test_claims_and_poll_ladder(self):
        db = store()
        self.assertFalse(card_replies.claimed(db, CHANNEL, '1.0', now=1000.0))
        db.execute("INSERT INTO card_reply_routes (channel,reply_ts,root,outcome,at) VALUES (?,?,?,'routing',?)",
                   (CHANNEL, '1.0', ROOT, 1000.0))
        self.assertTrue(card_replies.claimed(db, CHANNEL, '1.0', now=1000.0 + card_replies.CLAIM_STALE))
        self.assertFalse(card_replies.claimed(db, CHANNEL, '1.0', now=1001.0 + card_replies.CLAIM_STALE))   # stale
        self.assertEqual([card_replies._poll_every(a) for a in (0, 3599, 3600, 86399, 86400, 10 ** 9)],
                         [30, 30, 120, 120, 600, 600])
        self.assertIsNone(card_replies.routed(db, CHANNEL, 'missing'))


class CardThreadsTest(unittest.TestCase):
    def test_eligible_author(self):
        db = store()
        self.assertEqual(THREADS.eligible_author(runtime(), db, CHANNEL, ROOT, reply()), 'mira')
        other = runtime({'author_kind': 'human', 'trust': 'colleague', 'authority': 'none'}, delegates=[DEPUTY])
        self.assertEqual(THREADS.eligible_author(other, db, CHANNEL, ROOT, reply(user=DEPUTY)), 'delegate')
        self.assertIsNone(THREADS.eligible_author(other, db, CHANNEL, ROOT, reply(user=OTHER)))
        self.assertIsNone(THREADS.eligible_author(other, db, CHANNEL, ROOT, reply(user=DEPUTY, bot_id='B1')))
        self.assertIsNone(THREADS.eligible_author(other, db, CHANNEL, ROOT, reply(user=DEPUTY, app_id='A1')))
        self.assertIsNone(THREADS.eligible_author(other, db, CHANNEL, ROOT, reply(
            user=DEPUTY, metadata={'event_type': 'kage_agent'})))                 # an agent's post as the delegate
        self.assertIsNone(THREADS.eligible_author(other, db, CHANNEL, ROOT, reply(user=OWNER)))  # owner, not typed

    def test_route_reply_skips(self):
        db = store()
        off = runtime(cards={'task_thread': False})
        self.assertEqual(THREADS.route_reply(off, db, CHANNEL, ROOT, reply())['reason'], 'task_thread_off')
        nobody = runtime({'author_kind': 'human', 'trust': 'colleague', 'authority': 'none'})
        self.assertEqual(THREADS.route_reply(nobody, db, CHANNEL, ROOT, reply(user=OTHER))['reason'],
                         'not_mira_or_delegate')

    def test_receiver_config_and_push_refs(self):
        db = store()
        cfg = THREADS.receiver_config(runtime(delegates=[DEPUTY]), db, now=1.0)
        self.assertEqual((cfg['enabled'], cfg['channels'], cfg['users']), (True, [CHANNEL], sorted([OWNER, DEPUTY])))
        self.assertEqual(THREADS.receiver_config(runtime(), db)['users'], [OWNER])
        for args in (('bad', '1.0', ROOT, OWNER), (CHANNEL, ROOT, ROOT, OWNER), (CHANNEL, '1.0', ROOT, 'nobody')):
            self.assertEqual(THREADS.push(runtime(), db, *args)['reason'], 'invalid_refs')
        off = runtime(cards={'task_thread': True, 'realtime_replies': False})
        self.assertEqual(THREADS.push(off, db, CHANNEL, '1.0', ROOT, OWNER)['reason'], 'realtime_replies_off')
        self.assertEqual(THREADS.push(runtime(), db, 'C0000TEST02', '1.0', ROOT, OWNER)['reason'],
                         'not_a_decision_channel')


if __name__ == '__main__':
    unittest.main()
