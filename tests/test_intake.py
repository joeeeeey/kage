import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import types
import unittest
import unittest.mock
import zoneinfo

from kage.core import intake as I

OWNER, BOT, APP = 'U0000TEST01', 'U0000BOT001', 'A0000APP001'


def core(**over):
    args = dict(tz=zoneinfo.ZoneInfo('Asia/Shanghai'), owner_key='owner', owner_name='Mira', owner_slack_user=OWNER,
                bot_user=BOT, app_id=APP, archive_url='https://example.slack.com/archives',
                known_channels={'#ops': 'C0000OPS001'}, ticket_ref_re=re.compile(r'\b(TEST-\d{1,6})\b', re.I),
                agent_event_type='test_agent_post', ticket_ref_reason='ticket_ref', run_id_env='TEST_RUN_ID',
                workspace_bootstrap=lambda *a, **kw: {'workspace': '/nonexistent'}, owner_user=lambda rt: OWNER,
                ask_owner=lambda rt: None)
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
        # 0.78.3 (F14): bot-only, the owner's own user token through any allowed app is the owner
        bot_only = {'delegate_bot_names': ['*'], 'identity': 'bot'}
        self.assertEqual(c.delegate_app({**app, 'user': OWNER}, bot_only), 'coder-ada')
        self.assertIsNone(c.delegate_app({**app, 'user': BOT}, bot_only))
        self.assertIsNone(c.delegate_app({**app, 'user': OWNER}, {**bot_only, 'delegate_bot_names': []}))

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

    def test_a_bot_only_deployment_answers_what_is_said_to_it(self):
        # 0.77.0: with intake.identity bot, a channel message must mention the bot (DMs and on-call alerts as before)
        self.db.execute('CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, data TEXT)')
        ctx = {'oncall': {'C0000ONC001'}, 'cfg': {'identity': 'bot'}}
        verdict = lambda m, ch='C0000OPS001', conv='channel': self.engine.verdict(m, ch, conv, None, ctx)  # noqa: E731
        self.assertEqual(verdict({'user': 'U0000ADA001', 'ts': '2.1', 'text': 'the deploy is stuck'}), 'no_bot_mention')
        self.assertEqual(verdict({'user': 'U0000ADA001', 'ts': '2.2', 'text': f'<@{BOT}> the deploy is stuck'}), 'accept')
        self.assertEqual(verdict({'user': 'U0000ADA001', 'ts': '2.3', 'text': 'hi'}, 'D0000DM0001', 'im'), 'accept')
        self.assertEqual(verdict({'bot_id': 'B1', 'ts': '2.4'}, 'C0000ONC001'), 'accept')
        # the owner's words said to the bot are an instruction to it; elsewhere they stay the owner's own
        self.assertEqual(verdict({'user': OWNER, 'ts': '2.6', 'text': f'<@{BOT}> ship it'}), 'accept')
        self.assertEqual(verdict({'user': OWNER, 'ts': '2.7', 'text': 'ship it'}, 'D0000DM0001', 'im'), 'accept')
        self.assertEqual(verdict({'user': OWNER, 'ts': '2.8', 'text': 'ship it'}), 'owner_authored')
        self.assertEqual(verdict({'user': OWNER, 'ts': '2.9', 'text': 'ship it'}, 'C0000ONC001'), 'owner_authored')
        ctx['cfg'] = {}
        self.assertEqual(verdict({'user': OWNER, 'ts': '2.10', 'text': f'<@{BOT}> ship it'}), 'owner_authored')
        self.assertEqual(verdict({'user': 'U0000ADA001', 'ts': '2.5', 'text': 'the deploy is stuck'}), 'accept')

    def test_intake_push_reads_back_with_the_bot_token(self):
        self.db.execute('CREATE TABLE IF NOT EXISTS runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        cfg = {'intake': {'enabled': True, 'identity': 'bot', 'oncall_channels': []}}
        reads, idents = [], []
        message = {'ts': '1700000001.000200', 'user': 'U0000ADA001', 'text': f'<@{BOT}> help'}

        def api_call(method, token, params=None):
            reads.append((method, token, params.get('ts')))
            return {'messages': [message]}

        def api_identity(bot=False):
            idents.append(bot)
            return 'bot-token' if bot else 'user-token'
        rt = types.SimpleNamespace(worker_config=types.SimpleNamespace(load=lambda: (cfg, None)), api_identity=api_identity,
                                   slack=types.SimpleNamespace(api_call=api_call))
        push = lambda **kw: self.core.intake_push(rt, self.db, ':memory:', **{  # noqa: E731
            'channel': 'C0000OPS001', 'ts': '1700000001.000200', 'user': 'U0000ADA001', **kw})
        self.assertEqual(push(channel='bad')['reason'], 'invalid_refs')
        cfg['intake']['identity'] = 'user'
        self.assertEqual(push()['reason'], 'bot_intake_off')
        cfg['intake']['identity'] = 'bot'
        self.assertEqual(push()['reason'], 'intake_not_started')  # no baseline yet: the first poll sets it
        self.db.execute("INSERT INTO runtime_meta VALUES (?, ?)", (I.STATE_KEY, json.dumps({'baseline': 1.0})))
        seen = []
        with unittest.mock.patch.object(I.Intake, 'ingest', lambda engine, ms, ch, conv, source, ctx: seen.append(
                (ms[0]['ts'], ch, conv, source, ctx['baseline']))):
            out = push(thread_ts='1700000000.000100')
        self.assertEqual(seen, [('1700000001.000200', 'C0000OPS001', 'channel', 'push', 1.0)])
        self.assertEqual(reads[-1], ('conversations.replies', 'bot-token', '1700000000.000100'))
        self.assertEqual(idents[-1], True)
        self.assertEqual(out['reason'], 'before_intake_baseline')  # the patched ingest wrote nothing
        rt.LOOP_CORE = types.SimpleNamespace(control=types.SimpleNamespace(card_channel_on=lambda rt_: (True, {})))
        rt.card_threads = types.SimpleNamespace(decision_channels=lambda rt_: ['C0000OPS001'])
        rt.OWNER = 'U0000ADA001'  # the owner's top-level message in the card channel: the control entry takes it
        self.assertEqual(push()['reason'], 'control_entry')
        message['thread_ts'] = '1700000000.000100'  # in a thread: intake's, as before
        with unittest.mock.patch.object(I.Intake, 'ingest', lambda *a: None):
            self.assertEqual(push(thread_ts='1700000000.000100')['reason'], 'before_intake_baseline')
        del rt.OWNER, message['thread_ts']
        message['user'] = 'U0000OTHER1'
        self.assertEqual(push()['reason'], 'author_mismatch')
        message['user'] = 'U0000ADA001'

    def test_the_owners_api_post_to_the_bot_is_the_owners(self):
        # 0.78.3 (F14): a user-token post (bot_id / app_id + the owner's user) passes as the owner once users.info says
        # a person; a bot user's post through an app stays a bot
        self.db.execute('CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, data TEXT)')
        cfg = {'identity': 'bot', 'delegate_bot_names': ['*']}
        post = {'user': OWNER, 'ts': '3.1', 'text': f'<@{BOT}> ship it', 'bot_id': 'B0000TEST05', 'app_id': 'A0000TEST05',
                'bot_profile': {'name': 'api-client', 'app_id': 'A0000TEST05'}}
        ctx = {'oncall': set(), 'cfg': cfg, 'state': {'app_people': {OWNER: True, 'U0000APBOT1': False}}}
        self.assertEqual(self.engine.verdict(post, 'C0000OPS001', 'channel', None, ctx), 'accept')
        self.assertEqual(self.engine.verdict({**post, 'user': 'U0000APBOT1'}, 'C0000OPS001', 'channel', None, ctx),
                         'bot_outside_oncall')
        self.assertEqual(self.engine.verdict({**post, 'text': 'ship it'}, 'C0000OPS001', 'channel', None, ctx),
                         'owner_authored')  # not said to the bot

    def test_an_accepted_push_gets_the_bots_receipt_at_once(self):
        # 0.78.2 (F12): 👀 right after intake accepts the message, before the burst settles; config-gated
        self.db.execute('CREATE TABLE IF NOT EXISTS runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, data TEXT)')
        self.db.execute("INSERT INTO runtime_meta VALUES (?, ?)", (I.STATE_KEY, json.dumps({'baseline': 1.0})))
        cfg = {'intake': {'enabled': True, 'identity': 'bot', 'oncall_channels': [], 'burst_seconds': 30}}
        message = {'ts': '1700000001.000200', 'user': 'U0000ADA001', 'text': f'<@{BOT}> help'}
        calls = []

        def api_call(method, token, params=None, json_body=None):
            calls.append((method, token, json_body))
            if method == 'reactions.add' and len(calls) > 3:
                raise RuntimeError('Slack API error: already_reacted')
            return {'messages': [message]}
        rt = types.SimpleNamespace(worker_config=types.SimpleNamespace(load=lambda: (cfg, None)),
                                   api_identity=lambda bot=False: 'bot-token' if bot else 'user-token',
                                   slack=types.SimpleNamespace(api_call=api_call))

        def accept(engine, ms, ch, conv, source, ctx):
            self.db.execute("INSERT OR REPLACE INTO intake_seen (channel,ts,source,conv_type,user,sender_kind,digest,"
                            "reason,burst_id,seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (ch, ms[0]['ts'], source, conv, ms[0]['user'], 'human', 'd', 'accept', 'in00000000000002', 1.0))
        push = lambda: self.core.intake_push(rt, self.db, ':memory:', channel='C0000OPS001',  # noqa: E731
                                             ts='1700000001.000200', user='U0000ADA001')
        with unittest.mock.patch.object(I.Intake, 'ingest', accept):
            out = push()
        self.assertEqual((out['outcome'], out['receipt']), ('accepted', 'added'))
        self.assertEqual(calls[-1], ('reactions.add', 'bot-token',
                                     {'channel': 'C0000OPS001', 'timestamp': '1700000001.000200', 'name': 'eyes'}))
        with unittest.mock.patch.object(I.Intake, 'ingest', accept):
            self.assertEqual(push()['receipt'], 'already')  # idempotent
            cfg['intake']['ack_on_receipt'] = False
            self.assertNotIn('receipt', push())
        message['ts'] = '1.0'
        self.assertEqual(push()['reason'], 'message_not_found')
        rt.slack.api_call = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('Slack API error: ratelimited'))
        self.assertEqual({k: push()[k] for k in ('reason', 'error')},  # 0.78.4 (F16): a coded result, not a crash
                         {'reason': 'slack_read_failed', 'error': 'ratelimited'})

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


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.core = core()
        self.core.initialize(self.db)
        self.db.execute("INSERT INTO intake_bursts (burst_id, conv_key, channel, conv_type, sender, first_ts, last_ts, "
                        "msg_count, refs, state, event_id, created_at, updated_at) VALUES "
                        "('in00000000000001','D1','D0000DM0001','im','U0000ADA001','1.0','1.0',1,'[\"1.0\"]','routed',"
                        "'e1',0,0)")
        rt = types.SimpleNamespace(transaction=lambda db: db, asks=types.SimpleNamespace(check_secret_free=lambda *t: None),
                                   envelopes=types.SimpleNamespace(people_path=lambda: Path('/nonexistent')))
        self.rt = rt

    def test_an_owner_keyed_keyword_and_the_owner_default(self):
        b = dict(self.db.execute('SELECT * FROM intake_bursts').fetchone())
        with unittest.mock.patch.object(I, 'time', types.SimpleNamespace(time=lambda: 100.0)):
            rid = self.core.record_triage(self.rt, self.db, b, intent='request', clarity='clear', disposition='log',
                                          needs_owner=True, enforce=False)
        row = self.db.execute('SELECT needs_owner, recorded_by FROM triage_records WHERE id=?', (rid,)).fetchone()
        self.assertEqual(tuple(row), (1, 'owner'))  # the owner's key as the default author
        rid2 = self.core.record_triage(self.rt, self.db, b, intent='request', clarity='clear', disposition='log',
                                       recorded_by='inbox-task', enforce=False, now=101.0)
        self.assertEqual(self.db.execute('SELECT recorded_by FROM triage_records WHERE id=?', (rid2,)).fetchone()[0],
                         'inbox-task')
        mira = core(owner_key='mira')  # callers name the flag with the owner's key: needs_mira
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        mira.initialize(db)
        db.execute("INSERT INTO intake_bursts SELECT * FROM (SELECT 'in00000000000002','D1','D0000DM0001','im',NULL,"
                   "'U0000ADA001','person','1.0','1.0',1,'[]',NULL,NULL,NULL,NULL,NULL,0,NULL,NULL,NULL,NULL,'routed',NULL,"
                   "NULL,NULL,NULL,'e2',NULL,0,NULL,0,NULL,NULL,0,NULL)")
        b2 = dict(db.execute('SELECT * FROM intake_bursts').fetchone())
        rid3 = mira.record_triage(self.rt, db, b2, intent='request', clarity='clear', disposition='log', needs_mira=True,
                                  enforce=False, now=102.0)
        self.assertEqual(tuple(db.execute('SELECT needs_mira, recorded_by FROM triage_records WHERE id=?', (rid3,)).fetchone()),
                         (1, 'mira'))

    def test_option_labels_and_the_brief_in_the_owner_language(self):
        self.assertEqual([o.split('=')[0] for o in self.core.brief_options], ['self', 'draft', 'research', 'ignore'])
        zh = I.IntakeCore('zh-CN', **{k: getattr(self.core, k) for k in (
            'tz', 'owner_slack_user', 'bot_user', 'app_id', 'archive_url', 'known_channels', 'ticket_ref_re',
            'agent_event_type', 'ticket_ref_reason', 'run_id_env', 'workspace_bootstrap', 'owner_user', 'ask_owner')},
            owner_key='owner', owner_name='Mira')
        self.assertEqual(zh.brief_options[0], 'self=我来回:other')
        self.assertTrue(all(len(o.split('=')[1].rsplit(':', 1)[0]) <= 30 for o in self.core.brief_options))


class PlaybookSeamTest(unittest.TestCase):
    CONTEXT = dict(references=Path('/nonexistent/references'), rules_path='/nonexistent/rules.md',
                   bootstrap_key=I.BOOTSTRAP_KEY, read_config=lambda rt: {}, run_bootstrap=lambda *a, **kw: {},
                   default_inbox=lambda: 'owner-inbox')

    def pack(self, drop=()):
        def intake_playbook(**context):
            values = {name: (lambda *a, _name=name, **kw: _name) for name in I.PLAYBOOK_NAMES if name not in drop}
            return types.SimpleNamespace(**values, context=context)
        return types.SimpleNamespace(intake_playbook=intake_playbook)

    def test_a_pack_provides_the_playbook(self):
        playbook = I.intake_playbook(self.pack(), **self.CONTEXT)
        self.assertEqual(playbook.inbox_goal('cli', {}), 'inbox_goal')
        self.assertIs(playbook.context['read_config'], self.CONTEXT['read_config'])

    def test_no_pack_or_no_playbook_and_refusals(self):
        self.assertIsNone(I.intake_playbook(None, **self.CONTEXT))
        self.assertIsNone(I.intake_playbook(types.SimpleNamespace(dispatch_rules=None), **self.CONTEXT))
        with self.assertRaisesRegex(ValueError, 'intake_playbook_incomplete: oncall_goal'):
            I.intake_playbook(self.pack(drop=('oncall_goal',)), **self.CONTEXT)
        with self.assertRaises(TypeError):
            I.intake_playbook(self.pack(), references='/x')


if __name__ == '__main__':
    unittest.main()
