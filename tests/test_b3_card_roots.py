"""B3: an event recorded under a threaded card's own ts is found under the card's thread root, and a Codex thread made
under another model provider moves the dispatch on at once (classified like a missing session)."""
import sqlite3
import unittest

from kage.core import dispatch as D
from kage.core import escalation as E
from kage.core import loop as L

from tests.test_escalation import core as escalation_core
from tests.test_loop import core as loop_core

CHANNEL, ROOT, CARD, RECEIPT = 'C0000TEST09', '100.000001', '200.000001', '300.000001'


class ThreadingSlack:
    """Slack as it threads: a reply to a reply lands under the root; replies(ts=<a reply>) returns only that reply."""

    def __init__(self):
        self.calls = []
        self.messages = {ROOT: {'ts': ROOT, 'user': 'U0000TEST01', 'text': 'root'},
                         CARD: {'ts': CARD, 'thread_ts': ROOT, 'user': 'U0000TEST02', 'text': 'card'},
                         RECEIPT: {'ts': RECEIPT, 'thread_ts': ROOT, 'user': 'U0000TEST02', 'text': 'receipt'}}

    def read(self, endpoint, token, params):
        self.calls.append((endpoint, dict(params)))
        if endpoint != 'conversations.replies':
            return {'messages': []}
        head = self.messages.get(params['ts'])
        if head is None:
            return {'messages': []}
        if head.get('thread_ts', head['ts']) != head['ts']:  # a reply: only itself
            return {'messages': [head]}
        thread = sorted((m for m in self.messages.values() if m.get('thread_ts', m['ts']) == head['ts']),
                        key=lambda m: float(m['ts']))
        lo, hi = float(params.get('oldest', 0)), float(params.get('latest', 1e12))
        found = [m for m in thread if lo <= float(m['ts']) <= hi]
        return {'messages': found[:params.get('limit', 100)]}


def store(thread_root=ROOT):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE escalations (event_id, stage, thread_ts, x, client_id)')
    db.execute('CREATE TABLE escalation_cards (event_id, channel, thread_root)')
    db.execute("INSERT INTO escalations VALUES ('card1', 'sent', ?, NULL, NULL)", (CARD,))
    db.execute("INSERT INTO escalation_cards VALUES ('card1', ?, ?)", (CHANNEL, thread_root))
    return db


class CardThreadTests(unittest.TestCase):
    def test_a_threaded_card_answers_its_thread_root_a_top_level_card_its_own_ts(self):
        c, db = escalation_core(), store()
        self.assertEqual(c._card_thread(db, 'card1', CARD), ROOT)
        db.execute("UPDATE escalation_cards SET thread_root=NULL")
        self.assertEqual(c._card_thread(db, 'card1', CARD), CARD)
        self.assertEqual(c._card_thread(db, 'unknown', CARD), CARD)


class SourceFallbackTests(unittest.TestCase):
    def setUp(self):
        self.slack = ThreadingSlack()
        self.loop = loop_core(read_api=self.slack.read)
        self.event = {'id': 'e1', 'channel': CHANNEL, 'ts': RECEIPT}

    def test_the_receipt_is_not_under_the_cards_own_ts(self):
        found = self.slack.read('conversations.replies', 't', {'channel': CHANNEL, 'ts': CARD, 'oldest': RECEIPT,
                                                              'latest': RECEIPT, 'inclusive': True, 'limit': 1})
        self.assertNotIn(RECEIPT, [m['ts'] for m in found['messages']])  # the shape that raised exact_context_source_not_found

    def test_found_under_the_cards_recorded_thread_root_with_one_call(self):
        source, fallback = self.loop._source_under_card_root(store(), self.event, CARD, 't')
        self.assertEqual(source['ts'], RECEIPT)
        self.assertEqual(fallback, {'stored_root': CARD, 'root': ROOT, 'via': 'card_thread_root', 'api_calls': 1})
        self.assertEqual(len(self.slack.calls), 1)

    def test_without_a_card_row_slacks_own_thread_of_the_stored_root(self):
        db = store()
        db.execute('DELETE FROM escalation_cards')
        source, fallback = self.loop._source_under_card_root(db, self.event, CARD, 't')
        self.assertEqual((source['ts'], fallback['via'], fallback['root'], fallback['api_calls']), (RECEIPT, 'slack_thread', ROOT, 2))

    def test_a_store_without_card_tables_and_a_message_that_is_gone(self):
        source, fallback = self.loop._source_under_card_root(sqlite3.connect(':memory:'), self.event, CARD, 't')
        self.assertEqual(source['ts'], RECEIPT)
        del self.slack.messages[RECEIPT]
        self.assertEqual(self.loop._source_under_card_root(store(), self.event, CARD, 't'), (None, None))

    def test_a_wrong_recorded_root_still_falls_through_to_slack(self):
        source, fallback = self.loop._source_under_card_root(store('150.000001'), self.event, CARD, 't')
        self.assertEqual((source['ts'], fallback['via'], fallback['api_calls']), (RECEIPT, 'slack_thread', 3))


class ProviderMismatchTests(unittest.TestCase):
    LINE = ('Error: thread/resume: thread/resume failed: This chat uses {a}, but your current provider is {b}. Switch to '
            '{a} to continue. (code -32600), data: {{"type":"modelProviderMismatch"}}')

    def test_both_directions_move_on_at_once(self):
        for a, b in (('OpenAI', 'proxy'), ('proxy', 'OpenAI')):
            error_class, marker = D.classify_failure(self.LINE.format(a=a, b=b))
            self.assertEqual((error_class, marker), ('session_missing', 'but your current provider is'))
            self.assertIn(error_class, D.SWITCH_NOW)
            self.assertNotIn(error_class, D.RETRY_SAME)
        self.assertEqual(D.classify_failure('{"type":"modelProviderMismatch"}')[0], 'session_missing')

    def test_other_failures_keep_their_class(self):
        self.assertEqual(D.classify_failure('Error: 503 Service Unavailable')[0], 'transient')
        self.assertEqual(D.classify_failure('thread not found')[0], 'session_missing')
        self.assertEqual(D.classify_failure('something else')[0], 'unknown')


if __name__ == '__main__':
    unittest.main()
