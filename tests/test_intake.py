import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import types
import unittest
import zoneinfo

from kage.core import intake as I

OWNER, BOT, APP = 'U0000TEST01', 'U0000BOT001', 'A0000APP001'


def core(**over):
    args = dict(tz=zoneinfo.ZoneInfo('Asia/Shanghai'), owner_key='owner', owner_name='Mira', owner_slack_user=OWNER,
                bot_user=BOT, app_id=APP, archive_url='https://example.slack.com/archives',
                known_channels={'#ops': 'C0000OPS001'}, ticket_ref_re=re.compile(r'\b(TEST-\d{1,6})\b', re.I),
                agent_event_type='test_agent_post', ticket_ref_reason='ticket_ref')
    return I.IntakeCore('en', **{**args, **over})


class Registry:
    def __init__(self, active=True):
        self.on, self.refused = active, []

    def active(self, rule):
        return self.on

    def refuse(self, rule, why):
        self.refused.append(rule)
        raise ValueError(why)


def runtime(people, registry=None, users=None):
    calls = []

    def api_call(method, identity, params=None):
        calls.append(method)
        if method == 'users.info':
            return {'user': (users or {}).get(params['user'], {})}
        return {'messages': []}
    return types.SimpleNamespace(CJK_RE=re.compile('[一-鿿]'), rule_registry=registry or Registry(),
                                 envelopes=types.SimpleNamespace(people_path=lambda: people),
                                 slack=types.SimpleNamespace(api_call=api_call), api_identity=lambda: 'user',
                                 safe_error=str, calls=calls)


class IntakeCoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.people = self.tmp / 'people.json'
        self.people.write_text(json.dumps({'people': [
            {'id': 'ada', 'identities': [{'type': 'slack', 'value': 'U0000ADA001'}], 'name': {'display': 'Ada'},
             'handling': {'request_style': 'strategic_directive', 'auto_execute': False},
             'owner_view': {'summary_zh': '喜欢先看数据再做决定的工程负责人'}}]}))
        I._SLACK_PEOPLE.clear()
        I._NOTES_CACHE.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_owner_bound_helpers(self):
        c = core()
        self.assertEqual(c.resolve_channels(['#ops', 'C0000XYZ001', 'nope']), (['C0000OPS001', 'C0000XYZ001'], ['nope']))
        self.assertEqual(c.permalink('C1', '1.2', '0.5'), 'https://example.slack.com/archives/C1/p12?thread_ts=0.5&cid=C1')
        self.assertEqual(c.task_refs('see test-12 and TEST-3'), ['TEST-12', 'TEST-3'])
        self.assertEqual(c.local_iso(1793400000), '2026-10-31T06:40:00+08:00')
        self.assertIsNone(c.local_iso('x'))
        app = {'bot_id': 'B1', 'user': 'U0000ADA001', 'bot_profile': {'name': 'coder-ada'}}
        self.assertEqual(c.delegate_app(app, {'delegate_bot_names': ['coder-*']}), 'coder-ada')
        self.assertIsNone(c.delegate_app({**app, 'user': OWNER}, {'delegate_bot_names': ['coder-*']}))
        self.assertIsNone(c.delegate_app({**app, 'app_id': APP}, {'delegate_bot_names': ['coder-*']}))

    def test_the_notes_guard_uses_the_owner_key(self):
        c, rt = core(), runtime(self.people)
        self.assertEqual(c.owner_notes(rt, 'U0000ADA001'), ['喜欢先看数据再做决定的工程负责人'])
        self.assertTrue(c.notes_line(rt, 'U0000ADA001').startswith("Mira's notes (background only"))
        with self.assertRaisesRegex(ValueError, 'owner_notes_quoted'):
            c.require_language(rt, None, 'U0000ADA001', ['他喜欢先看数据再做决定的工程负责人，所以'])
        I._NOTES_CACHE.clear()  # the notes cache is per file: one owner per process
        self.assertEqual(core(owner_key='other').owner_notes(rt, 'U0000ADA001'), [])  # another owner's notes key

    def test_the_language_gate(self):
        rt = runtime(self.people, users={'U0000BOB001': {'tz': 'Europe/Paris'}})
        self.assertEqual(I.text_language(rt, 'please take a look at the deploy when you can')[0], 'en')
        self.assertEqual(I.text_language(rt, 'ok')[0], None)
        c = core()
        with self.assertRaisesRegex(ValueError, 'reply_in_their_language'):
            c.require_language(rt, None, 'U0000BOB001', ['好的，我来看一下这个部署的问题'])
        self.assertEqual(c.require_language(rt, None, 'U0000BOB001', ['Sure, I will look at the deploy now.']),
                         ({'en'}, None))
        self.assertEqual(c.require_language(rt, None, OWNER, ['好的，我来看一下这个部署的问题']), (None, None))
        self.assertEqual(I.default_ack({'fr'}, 0.0), "Je m'en occupe.")
        self.assertEqual(I.default_ack({'zh', 'en'}, None), I.DEFAULT_ACK)
        self.assertEqual(rt.calls.count('users.info'), 1)  # memoized per person

    def test_pure_helpers(self):
        a = I.alert_fingerprint({'text': '[Triggered] High error rate on service:api-1 (42%)'})
        b = I.alert_fingerprint({'text': '[Recovered] High error rate on service:api-1 (3%)'})
        self.assertEqual((a[0], a[1], a[2], b[2]), (b[0], 'api-1', 'triggered', 'recovered'))
        self.assertEqual(I.parse_permalink_thread('https://x/archives/C1/p2?thread_ts=1.5&cid=C1'), '1.5')
        self.assertEqual(I.match_conv_type({'channel': {'id': 'D1'}}), 'im')
        self.assertEqual(I.pr_refs('https://github.com/Org/Repo/pull/12'), ['org/repo#12'])
        self.assertEqual(I.load_handling(self.people)['U0000ADA001']['request_style'], 'strategic_directive')
        self.assertEqual(I.guards('strategic_directive', None)['may_reply'], False)
        out = I.people_set_field(self.people, 'ada', 'auto_execute', 'true')
        self.assertEqual((out['old'], out['new']), (False, True))
        with self.assertRaises(ValueError):
            I.people_set_field(self.people, 'ada', 'tone', 'x')


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.core = core()
        self.core.initialize(self.db)
        self.db.execute('CREATE TABLE agent_sends (channel TEXT, ts TEXT)')
        self.engine = I.Intake(self.db, ':memory:', types.SimpleNamespace(), core=self.core)

    def test_the_store_follows_the_owner_key(self):
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(triage_records)')}
        self.assertIn('needs_owner', columns)
        self.assertIn('horizon', {r[1] for r in self.db.execute('PRAGMA table_info(intake_sends)')})
        self.assertIn('AS MIRA', self.core.ddl)  # the owner's name in the schema comments

    def test_the_filter(self):
        ctx = {'oncall': {'C0000ONC001'}, 'cfg': {}}
        verdict = lambda m, ch='D0000DM0001', conv='im', root=None: self.engine.verdict(m, ch, conv, root, ctx)  # noqa: E731
        self.assertEqual(verdict({'user': OWNER, 'ts': '1.1'}), 'owner_authored')
        self.assertEqual(verdict({'user': BOT, 'ts': '1.2'}), 'our_agent')
        self.assertEqual(verdict({'user': 'U0000ADA001', 'ts': '1.3', 'metadata': {'event_type': 'test_agent_post'}}),
                         'our_agent')
        self.assertEqual(verdict({'user': 'USLACKBOT', 'ts': '1.4'}), 'system_noise')
        self.assertEqual(verdict({'bot_id': 'B1', 'ts': '1.5'}), 'bot_outside_oncall')
        self.assertEqual(verdict({'bot_id': 'B1', 'ts': '1.6'}, 'C0000ONC001', 'channel'), 'accept')

    def test_requests_and_budget(self):
        self.assertEqual(I.binding_key('im', None, '1.5', 'U1'), '1.5')
        self.assertEqual(I.binding_key('im', '1.2', '1.5', 'U1'), '1.2')
        self.assertTrue(I.legacy_whole_dm_key('u:U1') and I.legacy_whole_dm_key(''))
        budget = I.Budget(2)
        budget.take('search.messages')
        budget.take('search.messages')
        with self.assertRaises(I.BudgetExhausted):
            budget.take('conversations.history')
        self.assertEqual(self.core.ask_kind_zh['watch'], 'waiting for their reply')
        self.assertIn('Mira', self.core.window_why)


if __name__ == '__main__':
    unittest.main()
