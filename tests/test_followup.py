import io
import os
import sqlite3
import types
import unittest
from unittest import mock

from kage.core import followup as F

OWNER, BOT, APP = 'U0000TEST01', 'U0000TEST02', 'A0000TEST01'


def runtime(**over):
    """A fake runtime: only what the tested methods reach through `rt`."""
    buttons = types.SimpleNamespace(escape=lambda t: t.replace('<', '&lt;'), load_config=lambda: {},
                                    private_card_destination=lambda config, channel: channel == 'C0000TEST01')
    intake = types.SimpleNamespace(permalink=lambda channel, ts, root=None:
                                   f'https://example.slack.com/archives/{channel}/p{ts.replace(".", "")}')
    return types.SimpleNamespace(buttons=buttons, intake=intake, **over)


def core(language='en', rt=None):
    return F.FollowupCore(language, owner_key='owner', owner_name='Mira', workspace='T0000TEST01', owner_slack_user=OWNER,
                          bot_user=BOT, app_id=APP, agent_event_type='test_agent_event', dashboard_url='http://127.0.0.1:1',
                          run_context_env='TEST_RUN_CONTEXT', run_id_env='TEST_RUN_ID', tail_command='test-tail',
                          rt=rt or runtime())


def database():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE escalations (event_id TEXT PRIMARY KEY, thread_ts TEXT, watch_id TEXT, stage TEXT);
        CREATE TABLE escalation_cards (event_id TEXT PRIMARY KEY, channel TEXT, thread_root TEXT, dry INTEGER, created REAL);
        CREATE TABLE consumer_runs (run_id TEXT, task TEXT, dispatch_id TEXT, event_ids TEXT, started_at REAL,
                                    ended_at REAL, state TEXT, provider TEXT, session TEXT);
        CREATE TABLE events (id TEXT PRIMARY KEY, status TEXT, receipt TEXT);''')
    return db


class FollowupCoreTest(unittest.TestCase):
    def test_a_decision_typed_through_an_allowed_app_says_so(self):
        # 0.78.8 (F20): context.decision via=api for the owner's reply through their own API client
        model = [{'id': 'approve', 'label': 'Approve', 'kind': 'approve', 'recommended': True}]
        buttons = types.SimpleNamespace(card_model=lambda options: model, resolve_choice=lambda choice, m: m[0],
                                        public_option=lambda o: o, iso=lambda ts: str(ts))
        rt = runtime()
        rt.buttons = buttons
        c = core(rt=rt)
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE decisions (event_id TEXT, choice TEXT, actor TEXT, role TEXT, decided_at REAL, card_channel TEXT,
              card_ts TEXT, receipt_ts TEXT, delivered_event TEXT, stage TEXT, note TEXT, provenance TEXT, source_ts TEXT,
              proposal_id TEXT);
            CREATE TABLE escalation_cards (event_id TEXT, options TEXT);
            INSERT INTO escalation_cards VALUES ('e1', '[]');
            INSERT INTO decisions VALUES ('e1', 'approve', 'U0000TEST01', 'approver', 1.0, 'C0000TEST01', '1700000000.000100',
              NULL, 'ev1', 'recorded', NULL, 'thread_reply', '1700000001.000200', NULL);''')
        plain = c.button_decision(db, 'ev1')
        self.assertNotIn('via', plain)
        db.executescript("CREATE TABLE owner_api_replies (channel TEXT, ts TEXT, user TEXT, app TEXT, at REAL);"
                         "INSERT INTO owner_api_replies VALUES ('C0000TEST01', '1700000001.000200', 'U0000TEST01', "
                         "'API-Client', 1.0);")
        api = c.button_decision(db, 'ev1')
        self.assertEqual((api['via'], api['via_app'], api['provenance']), ('api', 'API-Client', 'thread_reply'))
        self.assertIn("allowed app 'API-Client'", api['verified_by'])

    def test_who_may_write_into_a_watch(self):
        c = core()
        human = {'user': 'U0000TEST09', 'ts': '1.0'}
        self.assertTrue(c.eligible({'peers': []}, human))
        self.assertFalse(c.eligible({'peers': []}, {'user': OWNER}))           # the owner never feeds a plain watch
        self.assertFalse(c.eligible({'peers': []}, {'user': BOT}))
        self.assertFalse(c.eligible({'peers': []}, {'user': 'U0000TEST09', 'app_id': APP}))
        decision = {'kind': 'decision', 'delegates': ['U0000TEST09'], 'delegate_window': (0, 5)}
        self.assertTrue(c.eligible(decision, {'user': OWNER}))                  # a decision watch takes the owner
        self.assertTrue(c.eligible(decision, human))                            # and a delegate in window
        self.assertFalse(c.eligible(decision, {'user': 'U0000TEST09', 'ts': '9.0'}))

    def test_owner_words_and_language(self):
        c, zh = core(), core('zh-CN')
        self.assertEqual(c.ask_owner_text('codex', 's', 't', 'Merge <it>?', lang='en', delegates=['U0000TEST09']),
                         f'<@{OWNER}> Merge &lt;it>?\nWhile Mira is away, <@U0000TEST09> can also decide with the buttons.')
        self.assertIn('Mira 休假期间', zh.ask_owner_text('codex', 's', 't', 'x', delegates=['U0000TEST09']))
        self.assertIn('ask-owner printed it', c.about_help)
        self.assertIn("Mira's decision channel", c.about_help)
        self.assertTrue(c.thread_reply_guidance.startswith('Mira typed this decision'))
        self.assertEqual(c.ask_owner_footer('claude', '0123456789', None, lang='en'), 'session 01234567 · Claude')
        # O1d: without a lang the card words follow the owner's language
        self.assertEqual(c.ask_owner_footer('claude', '0123456789', None), 'session 01234567 · Claude')
        self.assertEqual(zh.ask_owner_footer('claude', '0123456789', None), '会话 01234567 · Claude')
        self.assertIn('HIGH PRIORITY', c.ask_owner_text('codex', 's', 't', 'x', priority='high'))
        self.assertIn('高优先级', zh.ask_owner_text('codex', 's', 't', 'x', priority='high'))
        self.assertEqual(c.audience_identity([OWNER]), 'bot')
        self.assertEqual(c.audience_identity([OWNER, 'U0000TEST09']), 'user')
        self.assertTrue(c._external('T0000TEST09'))
        self.assertFalse(c._external('T0000TEST01'))

    def test_terminal_only_names_the_deployment_env(self):
        c = core()
        self.assertEqual(c.terminal_only_env[-2:], ('TEST_RUN_CONTEXT', 'TEST_RUN_ID'))
        with mock.patch.dict(os.environ, {'TEST_RUN_ID': 'r1'}, clear=True):
            with self.assertRaisesRegex(ValueError, r'^agent_session: only Mira can run this.*\(TEST_RUN_ID\)$'):
                c.owner_terminal_only()

    def test_about_refs_and_read_models(self):
        c, db = core(), database()
        db.execute("INSERT INTO escalations VALUES ('a'||printf('%023d', 1), '100.000001', NULL, 'watching')")
        db.execute("INSERT INTO escalation_cards VALUES ('a'||printf('%023d', 1), 'C0000TEST01', NULL, 0, 1)")
        ref = c.resolve_about(db, 'https://example.slack.com/archives/C0000TEST01/p100000001')
        self.assertEqual(ref['ts'], '100.000001')
        self.assertEqual(c.resolve_about(db, 'https://github.com/example/repo/pull/7/'),
                         {'pr': 'https://github.com/example/repo/pull/7', 'number': 7})
        with self.assertRaisesRegex(ValueError, 'about_not_recognized'):
            c.resolve_about(db, 'nonsense')
        db.execute("INSERT INTO consumer_runs VALUES ('r1','t','d1','[\"e1\",\"e2\"]',1,NULL,'running','codex','s')")
        db.execute("INSERT INTO events VALUES ('e1','handled',NULL), ('e2','dispatched','dispatch:d1')")
        run = F._latest_run(db, task='t')
        self.assertEqual(F._run_counts(db, run), (1, 2))
        self.assertEqual(F.dispatch_held(db, 'd1'), ['e2'])
        out = io.StringIO()
        F._tail_print(out, 'x')
        self.assertEqual(out.getvalue(), 'x\n')

    def test_the_runtime_is_the_seam(self):
        seen = []
        rt = runtime(blockers=types.SimpleNamespace(annotation=lambda rt_, db, eid: seen.append(rt_) or ''),
                     envelopes=types.SimpleNamespace(lookup=lambda db, eid: None, prompt_suffix=lambda env: ''))
        c, db = core(rt=rt), sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript('CREATE TABLE decisions (event_id, delivered_event, choice, options, proposal_id, provenance, card_channel, '
                         'source_ts); '
                         'CREATE TABLE escalation_cards (event_id, options); CREATE TABLE events (id, channel, ts); '
                         'CREATE TABLE decision_proposals (event_id, rejected_event); '
                         'CREATE TABLE ask_cards (answer_event, expired_event); CREATE TABLE ask_proposals (rejected_event)')
        self.assertEqual(c.decision_annotation(db, 'e1'), '')
        self.assertIs(seen[0], rt)                                              # RUNTIME -> rt


if __name__ == '__main__':
    unittest.main()
