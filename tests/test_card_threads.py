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
