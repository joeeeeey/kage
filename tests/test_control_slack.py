"""Phase 2 C2: the card-channel source of the control entry — the owner's own hand-typed top-level messages in the
decision channels (and the owner's replies in their threads) become control messages; answers go into the thread (bot).

A runtime stand-in over the C1 test runtime (tests/test_control.py) with a fake Slack: history / replies hold the
messages the tests post; every call is recorded. Offline: nothing reaches Slack."""
import json
from pathlib import Path
import shutil
import tempfile
import time
import types
import unittest
import urllib.error
import unittest.mock

from kage.adapters.slack import envelope as SE
from kage.adapters.slack import receiver as R
from kage.core import control as C
from kage.core import envelope as E
from kage.core.store import transaction
from tests.test_control import RUN_SESSION, SESSION, TARGET_SESSION, Runtime, write_config

OWNER, BOT, APP, MARKER, TEAM = 'U0000OWNER1', 'U0000BOT001', 'A0000APP001', 'kage_test_agent', 'T0000TEAM01'
CHANNEL = 'C0000CARDS1'


class FakeSlack:
    def __init__(self):
        self.top, self.threads, self.calls, self.posted = {}, {}, [], []

    def say(self, ts, text, **fields):
        m = {'type': 'message', 'ts': ts, 'user': OWNER, 'text': text, 'client_msg_id': 'x', 'team': TEAM, **fields}
        if m.get('thread_ts') and m['thread_ts'] != ts:
            self.threads.setdefault(m['thread_ts'], []).append(m)
        else:
            self.top[ts] = m
        return m

    def api_call(self, endpoint, token, params=None, json_body=None):
        self.calls.append((endpoint, token, dict(params or {}), json_body))
        p = params or {}
        if endpoint == 'conversations.history':  # Slack's order: newest first; pages of `limit` going back in time
            msgs = sorted(self.top.values(), key=lambda m: float(m['ts']), reverse=True)
            if 'latest' in p:
                msgs = [m for m in msgs if float(m['ts']) <= float(p['latest'])]
            if 'oldest' in p:
                msgs = [m for m in msgs if float(m['ts']) > float(p['oldest'])]
            return self.page(msgs, p)
        if endpoint == 'conversations.replies':  # oldest first, the parent on the first page; pages going forward
            root = self.top.get(p['ts'])
            msgs = ([root] if root else []) + sorted(self.threads.get(p['ts'], []), key=lambda m: float(m['ts']))
            if 'oldest' in p:
                msgs = [m for m in msgs if float(m['ts']) >= float(p['oldest'])]
            if 'latest' in p:
                msgs = [m for m in msgs if float(m['ts']) <= float(p['latest'])]
            return self.page(msgs, p)
        if endpoint == 'chat.postMessage':
            ts = f'{float(json_body["thread_ts"]) + 1 + len(self.posted):.6f}' if json_body.get('thread_ts') else \
                f'{9000 + len(self.posted)}.000001'  # a reply comes after its thread's root, as in Slack
            self.posted.append({**json_body, 'ts': ts})
            if json_body.get('thread_ts'):
                self.threads.setdefault(json_body['thread_ts'], []).append(
                    {'ts': ts, 'user': BOT, 'bot_id': 'B1', 'app_id': APP, 'thread_ts': json_body['thread_ts'],
                     'text': json_body['text'], 'metadata': json_body.get('metadata')})
            return {'ok': True, 'ts': ts, 'channel': json_body['channel']}
        raise AssertionError(f'unexpected Slack call {endpoint}')

    @staticmethod
    def page(msgs, p):
        start, limit = int(p.get('cursor') or 0), int(p.get('limit', 100))
        out = {'ok': True, 'messages': msgs[start:start + limit], 'has_more': start + limit < len(msgs)}
        if out['has_more']:
            out['response_metadata'] = {'next_cursor': str(start + limit)}
        return out

    def reads(self, endpoint=None):
        return [c for c in self.calls if c[0] in ((endpoint,) if endpoint else ('conversations.history',
                                                                                  'conversations.replies'))]


def slack_runtime(tmp):
    rt = Runtime(tmp)
    rt.slack = FakeSlack()
    rt.api_identity = lambda bot=False: 'bot-token' if bot else 'user-token'
    core = E.Envelopes(owner_key='owner', owner_user=OWNER)
    slack_env = SE.SlackEnvelopes(core, workspace=TEAM, assistant_app=APP, assistant_bot_user=BOT, agent_marker=MARKER)
    rt.envelopes = types.SimpleNamespace(envelope=E.envelope, ENVELOPES=core, classify_message=slack_env.classify_message,
                                         load_people=lambda: {})
    rt.OWNER = OWNER
    rt.DEPLOYMENT = types.SimpleNamespace(owner=types.SimpleNamespace(slack_user=OWNER,
                                                                      slack_workspace_url='https://example.slack.com'))
    rt.card_threads = types.SimpleNamespace(decision_channels=lambda rt_: [CHANNEL])
    rt.guarded, rt.receipts, rt.sends = [], [], []
    rt.buttons.require_private_destination = lambda rt_, db, channel, kind, **kw: rt.guarded.append((channel, kind))
    rt.agent_metadata = lambda kind, w=None, **refs: {'event_type': MARKER, 'event_payload': {'kind': kind, **refs}}
    rt.record_send = lambda db, channel, ts, kind, ref=None, **kw: rt.sends.append((channel, ts, kind))
    rt.ingest_receipt = lambda db, refs, provider, now=None, decided=None: rt.receipts.append((tuple(refs), provider))
    rt.db.execute('CREATE TABLE agent_sends (channel TEXT, ts TEXT, kind TEXT, ref TEXT, session TEXT, created REAL, '
                  'mentions TEXT)')
    rt.db.execute('CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    rt.db.commit()
    return rt


class SlackSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        write_config(self.tmp, enabled=True)
        self.rt = slack_runtime(self.tmp)
        self.addCleanup(self.rt.db.close)
        self.db, self.slack = self.rt.db, self.rt.slack
        self.c = C.Control('en', owner_key='owner', owner_name='Mira')
        self.now = time.time()
        self.t = lambda k: f'{self.now + k:.6f}'  # Slack ts after the entry first saw the channel
        self.rt.add_task('owner-control', session=SESSION)
        self.c.bootstrap  # noqa: B018 - the control task exists (add_task); tables come with initialize
        C.initialize(self.db)

    def push(self, m):
        return self.c.slack_push(self.rt, self.db, CHANNEL, m['ts'], m['user'], now=self.now)

    def hold(self):
        """pump, then the control run holds the events (the dispatcher binds its session)."""
        self.c.pump(self.rt, self.db, now=self.now)
        self.rt.running['owner-control'] = ('claude', RUN_SESSION)
        with transaction(self.db):
            self.db.execute("UPDATE events SET status='dispatched'")
            for wid, data in self.db.execute('SELECT id, data FROM watches').fetchall():
                self.db.execute('UPDATE watches SET data=? WHERE id=?', (json.dumps({**json.loads(data),
                                                                                     'session': RUN_SESSION}), wid))
        self.rt.actor = ('claude', RUN_SESSION)

    # ---- who is the owner, typing
    def test_only_the_owners_hand_typed_message_counts(self):
        typed = self.slack.say(self.t(10), '今天有在做哪些事情？')
        self.assertTrue(self.c.typed(self.rt, self.db, CHANNEL, typed))
        cases = {
            'agent marker (our runtime posting with the owner token)': {'metadata': {'event_type': MARKER, 'event_payload': {}}},
            'our app id (any post with a token of our app)': {'app_id': APP},
            'another app (a tool on the owner token)': {'app_id': 'A0000OTHER1'},
            'a bot id / bot profile': {'bot_id': 'B0000001'},
            'a bot profile only': {'bot_profile': {'id': 'B1'}},
            'a subtype': {'subtype': 'message_changed'},
            'someone else': {'user': 'U0000OTHER1'},
            'a thread reply': {'thread_ts': self.t(1)},
        }
        for label, fields in cases.items():
            m = {**typed, **fields}
            self.assertFalse(self.c.typed(self.rt, self.db, CHANNEL, m), label)
        with transaction(self.db):
            self.db.execute('INSERT INTO agent_sends (channel, ts, kind) VALUES (?,?,?)', (CHANNEL, typed['ts'], 'x'))
        self.assertFalse(self.c.typed(self.rt, self.db, CHANNEL, typed), 'in our send ledger')

    def test_the_explicit_checks_hold_even_if_the_classification_were_wrong(self):
        """Defense in depth: with a classifier that would call anything the owner's own words, an app id, a bot, a
        subtype or another author still never counts."""
        e = self.rt.envelopes.ENVELOPES
        self.rt.envelopes.classify_message = lambda m, **kw: E.envelope('slack_message', 'human', e.owner_trust,
                                                                        e.owner_intent, 'stub')
        typed = self.slack.say(self.t(10), 'x')
        self.assertTrue(self.c.typed(self.rt, self.db, CHANNEL, typed))
        for fields in ({'app_id': APP}, {'app_id': 'A0000OTHER1'}, {'bot_id': 'B1'}, {'bot_profile': {'id': 'B1'}},
                       {'subtype': 'bot_message'}, {'user': 'U0000OTHER1'}):
            self.assertFalse(self.c.typed(self.rt, self.db, CHANNEL, {**typed, **fields}), fields)

    # ---- the receiver's push
    def test_push_records_refs_and_a_digest_never_the_text(self):
        m = self.slack.say(self.t(10), '发布那件事进度如何？')
        out = self.push(m)
        self.assertEqual(out['outcome'], 'recorded')
        row = dict(self.db.execute('SELECT * FROM control_messages WHERE message_id=?', (out['message_id'],)).fetchone())
        self.assertEqual((row['source'], row['channel'], row['ts'], row['text']), ('slack', CHANNEL, m['ts'], None))
        self.assertEqual(row['digest'], C.digest('发布那件事进度如何？'))
        self.assertEqual(json.loads(row['facts'])['root'], m['ts'])
        thread = self.db.execute('SELECT * FROM control_threads').fetchone()
        self.assertEqual((thread['channel'], thread['root_ts'], thread['conversation_id']), (CHANNEL, m['ts'],
                                                                                              row['conversation_id']))
        self.assertEqual(self.rt.receipts, [(((CHANNEL, m['ts']),), 'claude')])
        self.assertEqual(self.push(m)['outcome'], 'duplicate')
        self.assertNotIn('发布那件事', json.dumps([dict(r) for r in self.db.execute('SELECT * FROM control_messages')],
                                               ensure_ascii=False))

    def test_push_refusals(self):
        m = self.slack.say(self.t(10), 'x')
        self.assertEqual(self.c.slack_push(self.rt, self.db, 'C0000OTHER1', m['ts'], OWNER, now=self.now)['reason'],
                         'not_a_decision_channel')
        self.assertEqual(self.c.slack_push(self.rt, self.db, CHANNEL, m['ts'], 'U0000OTHER1', now=self.now)['reason'],
                         'not_the_owner')
        self.assertEqual(self.c.slack_push(self.rt, self.db, CHANNEL, 'bad', OWNER, now=self.now)['reason'], 'invalid_refs')
        old = self.slack.say(f'{self.now - 3600:.6f}', 'before the entry')
        self.assertEqual(self.push(old)['reason'], 'before_the_entry')
        self.assertEqual(self.c.slack_push(self.rt, self.db, CHANNEL, self.t(99), OWNER, now=self.now)['reason'],
                         'message_not_found')
        agent = self.slack.say(self.t(20), 'an agent with the owner token', metadata={'event_type': MARKER})
        self.assertEqual(self.push(agent)['reason'], 'not_hand_typed')
        app = self.slack.say(self.t(21), 'curl with the owner xoxp', app_id=APP, bot_id='B1', bot_profile={'id': 'B1'})
        self.assertEqual(self.push(app)['reason'], 'not_hand_typed')
        write_config(self.tmp, enabled=True, card_channel=False)
        self.assertEqual(self.push(m)['reason'], 'control_off')
        write_config(self.tmp, enabled=False)
        self.assertEqual(self.push(m)['reason'], 'control_off')
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_messages').fetchone()[0], 0)

    # ---- thread replies, and the fallback poll
    def test_a_reply_in_the_thread_continues_the_conversation_and_a_card_thread_is_not_touched(self):
        top = self.slack.say(self.t(10), '先看一下发布')
        conversation = self.c._row(self.db, self.push(top)['message_id'])['conversation_id']
        reply = self.slack.say(self.t(30), '那第二个呢？', thread_ts=top['ts'])
        out = self.c.thread_reply(self.rt, self.db, CHANNEL, top['ts'], reply, now=self.now)
        self.assertEqual(out['outcome'], 'control')
        self.assertEqual(self.c._row(self.db, out['message_id'])['conversation_id'], conversation)
        self.assertIsNone(self.c.thread_reply(self.rt, self.db, CHANNEL, self.t(5), {**reply, 'thread_ts': self.t(5)},
                                              now=self.now))  # a card's thread: card routing goes on
        bot = {**reply, 'ts': self.t(31), 'user': BOT, 'bot_id': 'B1', 'app_id': APP}
        self.assertEqual(self.c.thread_reply(self.rt, self.db, CHANNEL, top['ts'], bot, now=self.now)['reason'],
                         'not_hand_typed_control_thread')
        write_config(self.tmp, enabled=False)
        self.assertIsNone(self.c.thread_reply(self.rt, self.db, CHANNEL, top['ts'], {**reply, 'ts': self.t(32)},
                                              now=self.now))  # off: the old routing

    def test_the_fallback_poll_finds_what_the_push_missed(self):
        before = self.slack.say(f'{self.now - 600:.6f}', 'typed before the entry saw the channel')
        self.assertEqual(self.c.slack_poll(self.rt, self.db, now=self.now)['recorded'], 0)  # no backfill
        a = self.slack.say(self.t(10), '第一件')
        self.slack.say(self.t(11), 'not mine', user='U0000OTHER1')
        self.slack.say(self.t(12), 'agent', metadata={'event_type': MARKER})
        out = self.c.slack_poll(self.rt, self.db, now=self.now + 100)
        self.assertEqual(out['recorded'], 1)
        self.assertIsNone(self.c.slack_poll(self.rt, self.db, now=self.now + 110))  # inside channel_poll_seconds
        reply = self.slack.say(self.t(40), '再补充一句', thread_ts=a['ts'])
        out = self.c.slack_poll(self.rt, self.db, now=self.now + 200)
        self.assertEqual((out['recorded'], out['threads']), (1, 1))
        self.assertEqual(self.c.slack_poll(self.rt, self.db, now=self.now + 300)['recorded'], 0)  # cursors moved on
        self.assertEqual(self.db.execute("SELECT count(*) FROM control_messages WHERE source='slack'").fetchone()[0], 2)
        self.assertEqual(self.c.thread_reply(self.rt, self.db, CHANNEL, a['ts'], reply, now=self.now)['outcome'], 'duplicate')

    def noise(self, first, n, thread=None):
        """n bot posts (cards) at t(first)..t(first+n-1), top-level or in `thread`."""
        for k in range(first, first + n):
            self.slack.say(self.t(k), 'card', user=BOT, bot_id='B1', app_id=APP, **({'thread_ts': thread} if thread else {}))

    def cursor(self):
        return json.loads(self.db.execute('SELECT value FROM runtime_meta WHERE key=?',
                                          (f'control_cursor:{CHANNEL}',)).fetchone()[0])

    def test_the_poll_pages_through_a_busy_channel_and_a_capped_walk_keeps_its_cursor(self):
        self.c.slack_poll(self.rt, self.db, now=self.now)  # the entry first saw the channel
        start = self.cursor()['cursor']
        old = self.slack.say(self.t(1), '最早的一条')  # the oldest in the gap, behind 450 cards
        self.noise(2, 449)
        new = self.slack.say(self.t(451), '最新的一条')
        with unittest.mock.patch.object(C, 'HISTORY_PAGES', 1):  # one page per poll: the walk spans three polls
            self.slack.calls.clear()
            out = self.c.slack_poll(self.rt, self.db, now=self.now + 500)
            self.assertEqual((out['recorded'], out['error']), (1, 'history_page_cap'))
            self.assertEqual(len(self.slack.reads('conversations.history')), 1)
            self.assertEqual(self.cursor()['cursor'], start)  # not moved past what is unread
            later = self.slack.say(self.t(510), '走读期间新来的')  # after the walk's end: read once the walk is done
            out = self.c.slack_poll(self.rt, self.db, now=self.now + 600)
            self.assertEqual((out['recorded'], out['error']), (0, 'history_page_cap'))
            self.assertEqual(self.cursor()['cursor'], start)
            out = self.c.slack_poll(self.rt, self.db, now=self.now + 700)
            self.assertEqual((out['recorded'], out['error']), (1, None))  # the oldest one, on the walk's last page
            self.assertEqual(self.cursor(), {'cursor': new['ts'], 'read_at': self.now + 700})
        self.slack.calls.clear()
        out = self.c.slack_poll(self.rt, self.db, now=self.now + 800)
        self.assertEqual((out['recorded'], len(self.slack.reads('conversations.history'))), (1, 1))
        recorded = {r[0] for r in self.db.execute("SELECT ts FROM control_messages WHERE role='owner'")}
        self.assertEqual(recorded, {old['ts'], new['ts'], later['ts']})
        # the whole gap in one poll with the default page cap; a page cursor Slack no longer accepts restarts the walk
        self.noise(800, 450)
        self.slack.say(self.t(799), '又一条')
        self.slack.calls.clear()
        out = self.c.slack_poll(self.rt, self.db, now=self.now + 1300)
        self.assertEqual((out['recorded'], len(self.slack.reads('conversations.history')), out['error']), (1, 3, None))

    def test_a_failed_page_keeps_the_cursor_and_drops_the_page_cursor(self):
        self.c.slack_poll(self.rt, self.db, now=self.now)
        start = self.cursor()['cursor']
        self.slack.say(self.t(1), '在缺口最早处')
        self.noise(2, 300)
        with unittest.mock.patch.object(C, 'HISTORY_PAGES', 1):  # a walk that goes on next poll, from a page cursor
            self.assertEqual(self.c.slack_poll(self.rt, self.db, now=self.now + 350)['error'], 'history_page_cap')
        self.assertTrue(self.cursor()['walk']['page'])
        real = self.slack.api_call

        def failing(endpoint, token, params=None, json_body=None):
            if (params or {}).get('cursor'):
                raise RuntimeError('invalid_cursor')
            return real(endpoint, token, params, json_body)
        self.slack.api_call = failing
        self.rt.slack = self.slack
        out = self.c.slack_poll(self.rt, self.db, now=self.now + 420)
        self.assertEqual((out['recorded'], out['error']), (0, 'RuntimeError'))
        self.assertEqual(self.cursor().get('cursor'), start)
        self.assertNotIn('walk', self.cursor())  # the next walk starts again from the cursor
        self.slack.api_call = real
        self.assertEqual(self.c.slack_poll(self.rt, self.db, now=self.now + 500)['recorded'], 1)

    def test_a_long_thread_is_read_to_its_end_in_one_poll(self):
        top = self.slack.say(self.t(1), '先看一下发布')
        self.push(top)
        self.noise(10, 250, thread=top['ts'])  # a page holds the parent and 199 replies: the owner's reply is on page two
        reply = self.slack.say(self.t(300), '那第二个呢？', thread_ts=top['ts'])
        self.slack.calls.clear()
        out = self.c.slack_poll(self.rt, self.db, now=self.now + 400, force=True)
        self.assertEqual((out['recorded'], out['error']), (1, None))
        calls = self.slack.reads('conversations.replies')
        self.assertEqual([c[2].get('cursor') for c in calls], [None, '200'])
        self.assertEqual({c[2]['oldest'] for c in calls}, {top['ts']})  # one query, paged
        self.assertEqual(self.db.execute('SELECT cursor FROM control_threads').fetchone()[0], reply['ts'])

    def test_replies_are_paged_and_a_capped_thread_goes_on_from_its_cursor(self):
        top = self.slack.say(self.t(1), '先看一下发布')
        self.push(top)
        self.noise(10, 250, thread=top['ts'])
        reply = self.slack.say(self.t(300), '那第二个呢？', thread_ts=top['ts'])
        with unittest.mock.patch.object(C, 'REPLY_PAGES', 1):
            out = self.c.slack_poll(self.rt, self.db, now=self.now + 400, force=True)
            self.assertEqual((out['recorded'], out['error']), (0, 'replies_page_cap'))
            self.assertEqual(self.db.execute('SELECT cursor FROM control_threads').fetchone()[0], self.t(208))
            out = self.c.slack_poll(self.rt, self.db, now=self.now + 500, force=True)
        self.assertEqual((out['recorded'], out['error']), (1, None))
        self.assertEqual(self.db.execute('SELECT cursor FROM control_threads').fetchone()[0], reply['ts'])

    def test_the_poll_spares_the_owners_token(self):
        cfg = C.control_settings({}, 'owner')
        self.assertEqual(cfg['channel_poll_seconds'], 60)
        roots = []
        for k in range(8):  # eight control threads, turns from 10 minutes to 2 days ago
            top = self.slack.say(self.t(k), f'第 {k} 件')
            mid = self.push(top)['message_id']
            roots.append(top['ts'])
            age = (600, 1200, 1800, 2400, 2 * 3600, 3 * 3600, 23 * 3600, 2 * 86400)[k]
            with transaction(self.db):
                self.db.execute('UPDATE control_threads SET updated_at=? WHERE root_ts=?', (self.now - age, top['ts']))
            self.assertTrue(mid)
        self.c.slack_poll(self.rt, self.db, now=self.now + 1)  # a history read
        # the receiver pushed within the interval: no history read (but at least every HISTORY_MAX_SKIP)
        with transaction(self.db):
            self.db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (C.PUSH_KEY, f'{self.now + 50:.6f}'))
        self.slack.calls.clear()
        out = self.c.slack_poll(self.rt, self.db, now=self.now + 61)
        self.assertEqual(out['history'], {CHANNEL: 'skipped_receiver_pushed'})
        self.assertEqual(len(self.slack.reads('conversations.history')), 0)
        polled = [c[2]['ts'] for c in self.slack.reads('conversations.replies')]
        self.assertLessEqual(len(polled), C.THREAD_POLL_MAX)  # at most 5 threads a poll
        self.assertNotIn(roots[7], polled)  # 2 days quiet: never polled
        per_poll, polls = [], {}
        for i in range(1, 21):  # 20 minutes of polls, one a minute, the receiver quiet
            self.slack.calls.clear()
            out = self.c.slack_poll(self.rt, self.db, now=self.now + 61 + 60 * i)
            per_poll.append(len(self.slack.reads()))
            for c in self.slack.reads('conversations.replies'):
                polls.setdefault(c[2]['ts'], []).append(i)
        self.assertLessEqual(max(per_poll), 1 + C.THREAD_POLL_MAX)
        self.assertNotIn(roots[7], polls)
        for k in (4, 5, 6):  # quiet for over an hour: at most every 5 minutes
            gaps = [b - a for a, b in zip(polls[roots[k]], polls[roots[k]][1:])]
            self.assertTrue(polls[roots[k]] and min(gaps or [5]) >= 5, (k, polls[roots[k]]))
        for k in (0, 1, 2, 3):  # within an hour of their last turn: every poll (while they stay within it)
            self.assertIn(1, polls[roots[k]])
        # a push now: the history read pauses, then comes back within HISTORY_MAX_SKIP even while pushes go on
        reads = []
        for i in range(1, 16):
            at = self.now + 2000 + 60 * i
            with transaction(self.db):
                self.db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)', (C.PUSH_KEY, f'{at - 5:.6f}'))
            self.slack.calls.clear()
            self.c.slack_poll(self.rt, self.db, now=at)
            reads.append(len(self.slack.reads('conversations.history')))
        # the first poll reads (the last read is over 10 minutes old); then one read every HISTORY_MAX_SKIP only
        self.assertEqual([i for i, n in enumerate(reads) if n], [0, C.HISTORY_MAX_SKIP // 60])

    def test_the_answer_is_a_turn_of_the_thread(self):
        top = self.slack.say(self.t(1), '进度？')
        mid = self.push(top)['message_id']
        with transaction(self.db):
            self.db.execute('UPDATE control_threads SET updated_at=?', (self.now - 5 * 3600,))
        self.hold()
        self.c.reply(self.rt, self.db, 'db', mid, '在做', now=self.now + 10)
        self.assertEqual(self.db.execute('SELECT updated_at FROM control_threads').fetchone()[0], self.now + 10)

    def test_the_poll_does_nothing_while_off_or_never_set_up(self):
        write_config(self.tmp, enabled=False)
        self.slack.say(self.t(10), 'x')
        self.assertIsNone(self.c.slack_poll(self.rt, self.db, now=self.now + 100))
        self.assertEqual(self.slack.calls, [])

    # ---- the run: the words re-read, the answer in the thread, a relay
    def test_the_run_reads_the_words_from_slack_answers_in_the_thread_and_relays_directly(self):
        target = self.rt.add_task('release-task', session=TARGET_SESSION)
        m = self.slack.say(self.t(10), '让发布先停一下，等我明天看。')
        mid = self.push(m)['message_id']
        self.hold()
        body = Path(self.db.execute('SELECT path FROM local_messages').fetchone()[0]).read_text()
        self.assertNotIn('让发布先停一下', body)
        self.assertIn(f'control-show --message {mid}', body)
        self.assertIn(f"https://example.slack.com/archives/{CHANNEL}/p{m['ts'].replace('.', '')}", body)
        shown = self.c.show(self.rt, self.db, mid)
        self.assertEqual(shown['message']['text'], '让发布先停一下，等我明天看。')
        out = self.c.relay(self.rt, self.db, 'db', mid, 'release-task', now=self.now)
        self.assertEqual(out['mode'], 'event')  # Slack: no confirm card (control.confirm_slack false)
        env = E.lookup(self.db, out['event'])
        self.assertEqual((env['kind'], env['authority']), ('owner_relay', 'owner_intent'))
        text = Path(out['file']).read_text()
        self.assertTrue(Path(out['file']).is_relative_to(target / 'history' / 'messages'))
        self.assertIn('让发布先停一下，等我明天看。', text)
        self.assertIn("slack card channel · the owner's own hand-typed message (push)", text)
        self.c.reply(self.rt, self.db, 'db', mid, '已转给 release-task。', now=self.now)
        post = self.slack.posted[-1]
        self.assertEqual((post['channel'], post['thread_ts'], post['text']), (CHANNEL, m['ts'], '已转给 release-task。'))
        self.assertEqual(post['metadata']['event_type'], MARKER)
        self.assertIn((CHANNEL, 'control_reply'), self.rt.guarded)
        self.assertEqual(self.rt.sends[-1][2], 'control_reply')
        stored = self.db.execute("SELECT text, facts FROM control_messages WHERE role='control'").fetchone()
        self.assertEqual(stored['text'], '已转给 release-task。')  # C3: the run's own answer is stored
        self.assertEqual(json.loads(stored['facts'])['slack_ts'], post['ts'])
        self.assertIsNone(self.db.execute("SELECT text FROM control_messages WHERE role='owner'").fetchone()[0])

    def test_an_edited_message_is_never_relayed_and_confirm_slack_asks_first(self):
        self.rt.add_task('release-task', session=TARGET_SESSION)
        m = self.slack.say(self.t(10), 'Hold the release.')
        mid = self.push(m)['message_id']
        self.hold()
        self.slack.top[m['ts']]['text'] = 'Delete the release branch.'  # edited after it was recorded
        with self.assertRaises(ValueError) as caught:
            self.c.relay(self.rt, self.db, 'db', mid, 'release-task', now=self.now)
        self.assertEqual(str(caught.exception), 'stored_text_does_not_match_its_digest')
        self.slack.top[m['ts']]['text'] = 'Hold the release.'
        write_config(self.tmp, enabled=True, confirm_slack=True)
        out = self.c.relay(self.rt, self.db, 'db', mid, 'release-task', now=self.now)
        self.assertEqual(out['state'], 'proposed')
        self.assertIn('Hold the release.', self.rt.cards[-1]['question'])

    def test_a_failed_slack_message_gets_one_note_in_its_thread(self):
        m = self.slack.say(self.t(10), 'x')
        mid = self.push(m)['message_id']
        self.c.pump(self.rt, self.db, now=self.now)
        with transaction(self.db):
            self.db.execute("UPDATE events SET status='handled'")
        self.c.sweep(self.rt, self.db, now=self.now + 5)
        self.assertEqual(self.c._row(self.db, mid)['error'], 'no_reply')
        self.assertEqual((self.slack.posted[-1]['thread_ts'], self.rt.sends[-1][2]), (m['ts'], 'control_failed'))
        self.assertIn('no_reply', self.slack.posted[-1]['text'])

    def test_the_console_read_model_links_the_thread_and_holds_no_slack_text(self):
        m = self.slack.say(self.t(10), '秘密以外的普通问题')
        mid = self.push(m)['message_id']
        row = self.c._row(self.db, mid)
        view = self.c.overview(self.db, json.loads((Path(self.tmp) / 'config.json').read_text()), row['conversation_id'])
        self.assertEqual(view['conversation']['slack'], {'channel': CHANNEL, 'root': m['ts']})
        self.assertEqual(view['messages'][0]['slack'], {'channel': CHANNEL, 'ts': m['ts']})
        self.assertIsNone(view['messages'][0]['text'])
        self.assertFalse(view['messages'][0]['expired'])


class Reader:
    """The dashboard's preview service as SlackWords sees it: authorize() and request(); every read recorded."""

    def __init__(self, slack, fail=None):
        self.slack, self.fail, self.reads, self.authorized = slack, fail, [], 0

    def authorize(self):
        self.authorized += 1

    def request(self, method, params):
        self.reads.append((method, dict(params)))
        if self.fail is not None:
            raise self.fail
        return self.slack.api_call(method, 'user-token', params)


class ConsoleWordsTest(unittest.TestCase):
    """C3: /control shows a Slack conversation: the run's own answers are stored; the owner's words are re-read from
    Slack on view (memory only, a short TTL, a read budget), with a clear state when they cannot be read."""
    setUp, push, hold = SlackSourceTest.setUp, SlackSourceTest.push, SlackSourceTest.hold

    def config(self):
        return json.loads((Path(self.tmp) / 'config.json').read_text())

    def conversation(self, text='发布那件事进度如何？', at=10):
        m = self.slack.say(self.t(at), text)
        mid = self.push(m)['message_id']
        return m, mid, self.c._row(self.db, mid)['conversation_id']

    def view(self, conversation, words):
        return self.c.overview(self.db, self.config(), conversation, words=words)

    def dump(self):
        return [tuple(r) for r in self.db.execute('SELECT * FROM control_messages ORDER BY rowid')]

    def test_the_owners_words_are_read_from_slack_on_view_and_held_in_memory_only(self):
        m, mid, cv = self.conversation()
        self.hold()
        self.c.reply(self.rt, self.db, 'db', mid, '明天上线。', now=self.now)
        clock = [self.now]
        reader = Reader(self.slack)
        words = C.SlackWords(reader, clock=lambda: clock[0])
        before = self.dump()
        view = self.view(cv, words)
        owner, answer = view['messages']
        self.assertEqual((owner['text'], owner['text_state']), ('发布那件事进度如何？', 'slack'))
        self.assertEqual((answer['text'], answer['text_state']), ('明天上线。', 'stored'))
        self.assertEqual(view['conversation']['words']['state'], 'read')
        self.assertEqual(reader.reads, [('conversations.replies', {'channel': CHANNEL, 'ts': m['ts'], 'oldest': m['ts'],
                                                                   'inclusive': True, 'limit': C.WORDS_PAGE})])
        self.assertEqual(self.dump(), before)  # nothing written: the words live in the reader's memory only
        self.assertEqual(reader.authorized, 1)  # the owner's token, its identity checked by the preview service
        clock[0] += C.WORDS_TTL - 1
        self.assertEqual(self.view(cv, words)['conversation']['words']['state'], 'cached')
        self.assertEqual(len(reader.reads), 1)
        clock[0] += 2
        self.assertEqual(self.view(cv, words)['messages'][0]['text_state'], 'slack')
        self.assertEqual(len(reader.reads), 2)  # after the TTL: read again
        # without a reader (another site's request, or none configured): the words stay in Slack
        view = self.view(cv, None)
        self.assertEqual((view['messages'][0]['text'], view['messages'][0]['text_state']), (None, 'in_slack'))
        self.assertEqual(view['conversation']['words'], {'state': 'not_read'})

    def test_an_edited_or_deleted_message_and_a_failed_read_say_so(self):
        m, mid, cv = self.conversation('Hold the release.')
        reader = Reader(self.slack)
        clock = [self.now]
        words = C.SlackWords(reader, clock=lambda: clock[0])
        self.slack.top[m['ts']]['text'] = 'Delete the release branch.'
        owner = self.view(cv, words)['messages'][0]
        self.assertEqual((owner['text'], owner['text_state']), ('Delete the release branch.', 'slack_edited'))
        del self.slack.top[m['ts']]  # deleted in Slack
        clock[0] += C.WORDS_TTL + 1
        view = self.view(cv, words)
        self.assertEqual((view['messages'][0]['text'], view['messages'][0]['text_state']), (None, 'unreadable'))
        self.assertEqual(view['conversation']['words']['error'], 'message_not_found')
        _, _, cv2 = self.conversation('第二件', at=20)
        failing = C.SlackWords(Reader(self.slack, fail=RuntimeError('Slack API error: channel_not_found')),
                               clock=lambda: clock[0])
        view = self.view(cv2, failing)
        self.assertEqual(view['messages'][0]['text_state'], 'unreadable')
        self.assertEqual(view['conversation']['words']['state'], 'unreadable')

    def test_reads_are_rate_limited_and_a_429_pauses_them(self):
        clock = [self.now]
        reader = Reader(self.slack)
        words = C.SlackWords(reader, clock=lambda: clock[0])
        conversations = []
        for k in range(C.WORDS_READS_PER_MINUTE + 1):
            m = self.slack.say(self.t(100 + k), f'第 {k} 件')
            conversations.append(self.c._row(self.db, self.push(m)['message_id'])['conversation_id'])
        for cv in conversations[:-1]:
            self.assertEqual(self.view(cv, words)['messages'][0]['text_state'], 'slack')
        view = self.view(conversations[-1], words)
        self.assertEqual(view['messages'][0]['text_state'], 'busy')
        self.assertEqual(view['conversation']['words']['error'], 'read_budget')
        self.assertTrue(1 <= view['messages'][0]['retry_after'] <= 60)
        self.assertEqual(len(reader.reads), C.WORDS_READS_PER_MINUTE)
        clock[0] += 61
        self.assertEqual(self.view(conversations[-1], words)['messages'][0]['text_state'], 'slack')
        error = urllib.error.HTTPError('https://slack.com/api', 429, 'rate limited', {'Retry-After': '120'}, None)
        limited = C.SlackWords(Reader(self.slack, fail=error), clock=lambda: clock[0])
        view = self.view(conversations[0], limited)
        self.assertEqual((view['messages'][0]['text_state'], view['messages'][0]['retry_after']), ('busy', 120))
        limited.reader.fail = None
        clock[0] += 60
        self.assertEqual(self.view(conversations[1], limited)['messages'][0]['text_state'], 'busy')  # paused for all
        clock[0] += 61
        self.assertEqual(self.view(conversations[1], limited)['messages'][0]['text_state'], 'slack')

    def test_answers_from_before_c3_or_past_retention_are_read_back_from_the_thread(self):
        m, mid, cv = self.conversation()
        self.hold()
        self.c.reply(self.rt, self.db, 'db', mid, '在做。', now=self.now)
        with transaction(self.db):  # an answer recorded by C2 (its text not stored)
            self.db.execute("UPDATE control_messages SET text=NULL WHERE role='control'")
        view = self.view(cv, C.SlackWords(Reader(self.slack)))
        self.assertEqual((view['messages'][1]['text'], view['messages'][1]['text_state']), ('在做。', 'slack'))
        # retention: the stored answer text goes like a console answer's after retention_days
        with transaction(self.db):
            self.db.execute("UPDATE control_messages SET text='在做。' WHERE role='control'")
        self.c.sweep(self.rt, self.db, now=self.now + 31 * 86400)
        self.assertIsNone(self.db.execute("SELECT text FROM control_messages WHERE role='control'").fetchone()[0])

    def test_the_endpoint_reads_slack_only_for_the_page_itself(self):
        from tests.test_control import ConsoleEndpointTest
        m, mid, cv = self.conversation()
        endpoint = ConsoleEndpointTest('test_get_without_a_store_and_a_token_only_on_request')  # its server and call()
        endpoint.setUp()
        self.addCleanup(endpoint.doCleanups)
        reader = Reader(self.slack)
        endpoint.server.control_words = C.SlackWords(reader)
        endpoint.server.db_path = Path(self.db.execute('PRAGMA database_list').fetchone()[2])  # this test's store
        status, data = endpoint.call('GET', f'/api/control?conversation={cv}', headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(status, 200)
        self.assertEqual((data['messages'][0]['text_state'], reader.reads), ('in_slack', []))
        status, data = endpoint.call('GET', f'/api/control?conversation={cv}', headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual((data['messages'][0]['text'], data['messages'][0]['text_state']), ('发布那件事进度如何？', 'slack'))
        self.assertEqual(len(reader.reads), 1)


class ReceiverRefsTest(unittest.TestCase):
    def payload(self, **event):
        return {'type': 'event_callback', 'team_id': TEAM, 'event': {'type': 'message', 'channel': CHANNEL,
                                                                      'ts': '1800000000.000100', 'user': OWNER, **event}}

    def test_top_level_refs_and_drops(self):
        self.assertEqual(R.control_refs(self.payload(), expected_team_id=TEAM),
                         ({'channel': CHANNEL, 'ts': '1800000000.000100', 'user': OWNER}, ''))
        self.assertEqual(R.control_refs(self.payload(thread_ts='1800000000.000100'), expected_team_id=TEAM)[1], '')
        for fields, code in (({'thread_ts': '1799999999.000001'}, 'thread_reply'), ({'app_id': APP}, 'has_app_id'),
                             ({'bot_id': 'B1'}, 'has_bot_id'), ({'subtype': 'message_changed'}, 'has_subtype'),
                             ({'user': 'bad'}, 'bad_user')):
            self.assertEqual(R.control_refs(self.payload(**fields), expected_team_id=TEAM), (None, code), fields)
        self.assertEqual(R.control_refs(self.payload(), expected_team_id='T0000OTHER1'), (None, 'unexpected_team'))
        self.assertEqual(R.control_push_command({'channel': CHANNEL, 'ts': '1.2', 'user': OWNER}, python='py',
                                                script=Path('/x/p.py'), db='/x/db')[-7:],
                         ['control-push', '--channel', CHANNEL, '--ts', '1.2', '--user', OWNER])

    def test_the_config_answer_carries_the_control_flag_only_when_set(self):
        base = {'ok': True, 'enabled': True, 'channels': [CHANNEL], 'users': [OWNER], 'ttl_seconds': 300}
        self.assertFalse(R.parse_card_reply_config(base)['control'])
        self.assertTrue(R.parse_card_reply_config({**base, 'control': True})['control'])
        self.assertFalse(R.parse_card_reply_config({**base, 'control': 'yes'})['control'])


if __name__ == '__main__':
    unittest.main()


class CardThreadHooksTest(unittest.TestCase):
    """The two places card routing meets the control entry: the receiver's gate and route_reply."""

    def setUp(self):
        from kage.adapters.slack import card_threads as CT
        self.CT = CT
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        write_config(self.tmp, enabled=True)
        self.rt = slack_runtime(self.tmp)
        self.addCleanup(self.rt.db.close)
        self.c = C.Control('en', owner_key='owner', owner_name='Mira')
        self.rt.LOOP_CORE = types.SimpleNamespace(control=self.c)
        self.rt.buttons.load_config = lambda: {'decision_channel': CHANNEL, 'ooo': {'active': False}}
        self.rt.buttons.decision_channels = lambda config: {CHANNEL}
        self.threads = CT.CardThreads('owner', OWNER)

    def test_the_gate_says_control_only_while_the_entry_takes_card_channel_messages(self):
        self.assertNotIn('control', self.threads.receiver_config(self.rt, self.rt.db))  # never set up: no tables
        C.initialize(self.rt.db)
        self.assertTrue(self.threads.receiver_config(self.rt, self.rt.db)['control'])
        write_config(self.tmp, enabled=True, card_channel=False)
        self.assertNotIn('control', self.threads.receiver_config(self.rt, self.rt.db))
        write_config(self.tmp, enabled=False)
        self.assertNotIn('control', self.threads.receiver_config(self.rt, self.rt.db))

    def test_route_reply_hands_only_a_control_thread_to_the_entry(self):
        C.initialize(self.rt.db)
        now = time.time()
        root = self.rt.slack.say(f'{now + 1:.6f}', 'a control conversation')
        self.c.slack_push(self.rt, self.rt.db, CHANNEL, root['ts'], OWNER, now=now)
        reply = self.rt.slack.say(f'{now + 5:.6f}', 'and then?', thread_ts=root['ts'])

        class Continued(Exception):
            pass

        def secret(db, channel, r):
            raise Continued(r)
        self.rt._secret_thread = secret
        self.threads.eligible_author = lambda rt, db, channel, r, m, config=None: 'owner'
        out = self.threads.route_reply(self.rt, self.rt.db, CHANNEL, root['ts'], reply, now=now, source='push')
        self.assertEqual(out['outcome'], 'control')
        card_reply = {**reply, 'ts': f'{now + 6:.6f}', 'thread_ts': f'{now - 100:.6f}'}
        with self.assertRaises(Continued):  # a card's thread: the card routing goes on
            self.threads.route_reply(self.rt, self.rt.db, CHANNEL, card_reply['thread_ts'], card_reply, now=now)
        self.threads.eligible_author = lambda rt, db, channel, r, m, config=None: 'delegate'
        with self.assertRaises(Continued):  # a delegate's reply never becomes a control message
            self.threads.route_reply(self.rt, self.rt.db, CHANNEL, root['ts'], {**reply, 'ts': f'{now + 7:.6f}'}, now=now)
