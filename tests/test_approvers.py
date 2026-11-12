"""0.78.9 (F21): the approvers edited from the console — Slack-resolved people only, the owner stays, one save writes the
decisions file and a new access-rules version (rolled back together), an audit line, and the supervisor's notice."""
import contextlib
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import types
import unittest

from kage.core import approvers as A
from kage.core import authority

OWNER, BOT, LIN, SAM, HR, GUEST = 'U0000OWNER1', 'U0000BOT001', 'U0000TEST01', 'U0000TEST02', 'U0000HR0001', 'U0000GUEST1'
TEAM = 'T0000TEST01'
USERS = {
    LIN: {'id': LIN, 'team_id': TEAM, 'profile': {'real_name': 'Lin Example'}},
    SAM: {'id': SAM, 'team_id': TEAM, 'profile': {'real_name': 'Sam Example'}},
    HR: {'id': HR, 'team_id': TEAM, 'profile': {'real_name': 'HR Example'}},
    GUEST: {'id': GUEST, 'team_id': TEAM, 'is_restricted': True},
    'U0000APPBOT': {'id': 'U0000APPBOT', 'team_id': TEAM, 'is_bot': True},
    'U0000GONE01': {'id': 'U0000GONE01', 'team_id': TEAM, 'deleted': True},
    'U0000OTHER1': {'id': 'U0000OTHER1', 'team_id': 'T0000OTHER1'},
    BOT: {'id': BOT, 'team_id': TEAM},
}


def spec(version=3, approvers=(OWNER,)):
    return {'category': 'access', 'version': version, 'defaults': {
        'owner': OWNER, 'approvers': list(approvers), 'approver_names_zh': {OWNER: 'Owner'}, 'hr': [HR]},
        'rules': [{'id': 'r1', 'bounds': {'approvers': list(approvers)}}, {'id': 'r2', 'bounds': {'max_days': 7}}]}


class Rules:
    def __init__(self):
        self.active, self.installed, self.fail = spec(), [], None

    def load_active(self, category='access'):
        return copy.deepcopy(self.active), {'version': self.active['version'], 'file': f'access-v{self.active["version"]}.json'}

    def validate_spec(self, s):
        if s['defaults']['owner'] not in s['defaults']['approvers']:
            raise ValueError('invalid_rules_spec: owner must be one of the approvers')

    def install_spec(self, s, directory=None, *, who, note=None, now=None):
        if self.fail:
            raise ValueError(self.fail)
        self.installed.append((copy.deepcopy(s), who, note))
        self.active = copy.deepcopy(s)
        return {'ok': True, 'file': f'access-v{s["version"]}.json'}


def lookup(calls):
    def slack(method, params):
        calls.append((method, params))
        if method == 'users.lookupByEmail':
            if params['email'] == 'sam@example.com':
                return {'user': USERS[SAM]}
            raise RuntimeError('Slack API error: users_not_found')
        if params['user'] not in USERS:
            raise RuntimeError('Slack API error: user_not_found')
        return {'user': USERS[params['user']]}
    return slack


class ApproversTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'cfg' / 'decisions.json'
        self.path.parent.mkdir(mode=0o700)
        self.path.write_text(json.dumps({'decision_channel': 'C0000CARDS1', 'approvers': [OWNER]}))
        os.chmod(self.path, 0o600)
        auth = authority.Authority(owner_user=OWNER, owner_key='owner', user_re=re.compile(r'[UW][A-Z0-9]{2,}'),
                                   channel_re=re.compile(r'[CG][A-Z0-9]{2,}'), dm_re=re.compile(r'D[A-Z0-9]{2,}'))
        self.rules, self.calls = Rules(), []
        self.ed = A.Approvers(owner_user=OWNER, bot_user=BOT, team=TEAM, decisions_path=lambda: self.path,
                              load_decisions=lambda: auth.load_config(self.path), rules=self.rules, slack=lookup(self.calls))

    def test_state_and_a_save_through_both_places(self):
        st = self.ed.state()
        self.assertEqual([a['id'] for a in st['approvers']], [OWNER])
        self.assertEqual((st['approvers'][0]['owner'], st['rules']['version'], st['max']), (True, 3, 5))
        out = self.ed.save([OWNER, LIN, 'sam@example.com'], st['digest'], 'dashboard:lin@example.com', now=1_800_000_000.0)
        self.assertEqual(out['change']['added'], [LIN, SAM])
        written = json.loads(self.path.read_text())
        self.assertEqual(written, {'decision_channel': 'C0000CARDS1', 'approvers': [OWNER, LIN, SAM]})  # other keys kept
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), '0o600')
        new, who, note = self.rules.installed[0]
        self.assertEqual((new['version'], new['defaults']['approvers'], who), (4, [OWNER, LIN, SAM], 'dashboard:lin@example.com'))
        self.assertEqual(new['defaults']['approver_names_zh'], {OWNER: 'Owner', LIN: 'Lin Example', SAM: 'Sam Example'})
        self.assertEqual(new['rules'][0]['bounds']['approvers'], [OWNER, LIN, SAM])
        self.assertEqual(new['rules'][1]['bounds'], {'max_days': 7})
        log = A.changes(self.path)
        self.assertEqual((log[0]['who'], log[0]['old'], log[0]['new'], log[0]['rules_version']),
                         ('dashboard:lin@example.com', [OWNER], [OWNER, LIN, SAM], 4))
        self.assertEqual(json.loads(Path(log[0]['backup']).read_text())['approvers'], [OWNER])
        st = out['state']
        self.assertEqual([(a['id'], a['in_decisions'], a['in_rules']) for a in st['approvers']],
                         [(OWNER, True, True), (LIN, True, True), (SAM, True, True)])
        # remove one; the owner stays
        out = self.ed.save([OWNER, LIN], st['digest'], 'dashboard:sam@example.com')
        self.assertEqual((out['change']['removed'], self.rules.active['version']), ([SAM], 5))

    def test_only_active_people_of_this_workspace_and_the_owner_stays(self):
        d = self.ed.state()['digest']
        for entries, code in (([OWNER, 'U0000APPBOT'], 'approver_must_be_a_person_not_a_bot'),
                              ([OWNER, BOT], 'approver_must_be_a_person_not_a_bot'),
                              ([OWNER, 'U0000GONE01'], 'approver_account_is_deactivated'),
                              ([OWNER, GUEST], 'approver_must_be_a_full_member_of_this_workspace'),
                              ([OWNER, 'U0000OTHER1'], 'approver_must_be_a_full_member_of_this_workspace'),
                              ([OWNER, 'U0000NOBODY'], 'approver_not_found_in_slack:'),
                              ([OWNER, 'nobody@example.com'], 'approver_not_found_in_slack:'),
                              ([OWNER, 'Lin'], 'approver_must_be_a_slack_user_id_or_an_email'),
                              ([LIN], 'the_owner_stays_an_approver'),
                              ([OWNER, HR], 'an_hr_contact_cannot_be_an_approver'),
                              ([OWNER, LIN, SAM, HR, 'U0000TEST03', 'U0000TEST04'], 'at_most_5_approvers'),
                              ([OWNER], 'nothing_to_change'), ([], 'approvers_must_be_a_list')):
            with self.subTest(entries), self.assertRaises(ValueError) as ctx:
                self.ed.save(entries, d, 'dashboard')
            self.assertIn(code, str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.ed.save([OWNER, LIN], '0' * 16, 'dashboard')
        self.assertIn('approvers_changed_since_loaded_reload', str(ctx.exception))
        self.assertEqual(json.loads(self.path.read_text())['approvers'], [OWNER])
        self.assertEqual(self.rules.installed, [])

    def test_a_failed_rules_install_puts_the_decisions_file_back(self):
        self.rules.fail = 'rules_version_exists_with_different_content'
        with self.assertRaises(ValueError):
            self.ed.save([OWNER, LIN], self.ed.state()['digest'], 'dashboard')
        self.assertEqual(json.loads(self.path.read_text()), {'decision_channel': 'C0000CARDS1', 'approvers': [OWNER]})
        self.assertEqual(A.changes(self.path), [])

    def test_without_installed_rules_only_the_decisions_file(self):
        self.rules.load_active = lambda category='access': (_ for _ in ()).throw(ValueError('rules_not_installed'))
        st = self.ed.state()
        self.assertIsNone(st['rules'])
        out = self.ed.save([OWNER, LIN], st['digest'], 'dashboard')
        self.assertIsNone(out['rules'])
        self.assertEqual(json.loads(self.path.read_text())['approvers'], [OWNER, LIN])


class OwnersTest(ApproversTest):
    """0.78.10 (F22): owners apart from the active approvers."""

    def test_owners_and_a_subset_that_approves(self):
        st = self.ed.state()
        out = self.ed.save([SAM], st['digest'], 'dashboard:lin@example.com', owners=[LIN, SAM], now=1_800_000_000.0)
        written = json.loads(self.path.read_text())
        self.assertEqual((written['owners'], written['approvers']), ([OWNER, LIN, SAM], [SAM]))
        new = self.rules.installed[-1][0]
        self.assertEqual(new['defaults']['approvers'], [OWNER, SAM])                 # the rules keep the deployment owner
        st = out['state']
        self.assertEqual(([p['id'] for p in st['owners']], [p['id'] for p in st['approvers']], st['split']),
                         ([OWNER, LIN, SAM], [SAM], True))
        with self.assertRaises(ValueError) as ctx:                                    # an approver must be an owner
            self.ed.save([HR], st['digest'], 'dashboard', owners=[LIN, SAM])
        self.assertIn('an_active_approver_must_be_one_of_the_owners', str(ctx.exception))
        # an old-style save (approvers only) keeps the owners and adds a new approver to them
        out = self.ed.save([LIN], st['digest'], 'dashboard')
        self.assertEqual(json.loads(self.path.read_text())['owners'], [OWNER, LIN, SAM])

    def test_switch_by_name_with_a_date_and_the_switch_back(self):
        self.ed.save([SAM], self.ed.state()['digest'], 'dashboard', owners=[LIN, SAM])
        until = 1_800_000_000.0 + 3 * 86400
        out = self.ed.switch(['lin'], 'slack:' + SAM, until=until, source='https://slack.com/archives/C1/p1', now=1_800_000_000.0)
        self.assertEqual((out['change']['new'], out['change']['until']), ([LIN], until))
        self.assertEqual(A.read_schedule(self.path)['then'], [SAM])
        for bad, code in ((['nobody'], 'approver_name_is_not_an_owner'), (['example'], 'approver_name_matches_several_owners'),
                          (['lin'], 'nothing_to_change')):
            with self.subTest(bad), self.assertRaises(ValueError) as ctx:
                self.ed.switch(bad, 'slack:' + SAM, now=1_800_000_000.0)
            self.assertIn(code, str(ctx.exception))
        with self.assertRaises(ValueError):
            self.ed.switch(['sam'], 'slack:' + SAM, until=1_800_000_000.0 + 100 * 86400, now=1_800_000_000.0)
        # the supervisor switches back once the date is over
        rt = types.SimpleNamespace(buttons=types.SimpleNamespace(config_path=lambda: self.path, load_config=self.ed.load_decisions),
                                   rules=self.rules, slack=types.SimpleNamespace(api_call=lambda m, t, params=None: lookup([])(m, params)),
                                   api_identity=lambda bot=False: 'bot-token', BOT=BOT, TEAM=TEAM,
                                   DEPLOYMENT=types.SimpleNamespace(owner=types.SimpleNamespace(slack_user=OWNER)))
        notices = A.Notices('zh-CN')
        self.assertIsNone(notices.revert(rt, None, now=until - 1))
        self.assertEqual(notices.revert(rt, None, now=until + 1), {'reverted': True, 'approvers': [SAM]})
        self.assertIsNone(A.read_schedule(self.path))
        self.assertEqual(A.changes(self.path)[0]['who'], 'schedule')

    def test_parse_and_match(self):
        import datetime as dt
        tz = dt.timezone(dt.timedelta(hours=8))
        now = dt.datetime(2026, 11, 12, 12, 0, tzinfo=tz).timestamp()
        self.assertEqual(A.parse_switch('审批人换成 lin', tz, now), (['lin'], None))
        self.assertEqual(A.parse_switch('<@U0000BOT001> approver -> Sam', tz, now), (['Sam'], None))
        self.assertEqual(A.parse_switch('审批人换成 <@U0000TEST01|lin> 和 sam', tz, now), ([LIN, 'sam'], None))
        names, until = A.parse_switch('我请假到 11-20，审批人换成 lin', tz, now)
        self.assertEqual((names, dt.datetime.fromtimestamp(until, tz).isoformat()), (['lin'], '2026-11-21T00:00:00+08:00'))
        self.assertEqual(A.parse_switch('approvers to lin until 11/20', tz, now)[0], ['lin'])
        for text in ('今天谁是审批人？', '帮我看下审批流程', 'lin 你好', ''):
            self.assertIsNone(A.parse_switch(text, tz, now), text)
        owners = [{'id': LIN, 'name': 'Lin Example'}, {'id': SAM, 'name': 'Sam Example'}]
        self.assertEqual(A.match_owners(['LIN', SAM, '<@U0000TEST01>'], owners), [LIN, SAM])


class SlackSwitchTest(unittest.TestCase):
    def test_an_owners_message_switches_once_and_others_are_ignored(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'decisions.json'
        path.write_text(json.dumps({'decision_channel': 'C0000CARDS1', 'owners': [OWNER, LIN, SAM], 'approvers': [SAM]}))
        os.chmod(path, 0o600)
        auth = authority.Authority(owner_user=OWNER, owner_key='owner', user_re=re.compile(r'[UW][A-Z0-9]{2,}'),
                                   channel_re=re.compile(r'[CG][A-Z0-9]{2,}'), dm_re=re.compile(r'D[A-Z0-9]{2,}'))
        rules, posts, sends = Rules(), [], []
        rules.active = spec(approvers=(OWNER, SAM))

        @contextlib.contextmanager
        def transaction(db):
            db.execute('BEGIN IMMEDIATE')
            yield
            db.commit()

        def api_call(method, token, params=None, json_body=None):
            if method == 'chat.postMessage':
                posts.append(json_body)
                return {'ts': '1800000001.000100'}
            return lookup([])(method, params)
        owners = {OWNER, LIN, SAM}
        rt = types.SimpleNamespace(
            buttons=types.SimpleNamespace(config_path=lambda: path, load_config=lambda: auth.load_config(path)), rules=rules,
            slack=types.SimpleNamespace(api_call=api_call), api_identity=lambda bot=False: 'bot-token', BOT=BOT, TEAM=TEAM,
            DEPLOYMENT=types.SimpleNamespace(owner=types.SimpleNamespace(slack_user=OWNER, language='zh-CN',
                                                                          tz=lambda: __import__('datetime').timezone.utc)),
            envelopes=types.SimpleNamespace(ENVELOPES=types.SimpleNamespace(owner_like=lambda: frozenset(owners))),
            transaction=transaction, agent_metadata=lambda kind, eid, **kw: {'event_type': 'x'},
            record_send=lambda db, channel, ts, kind, ref: sends.append(ts))
        db = sqlite3.connect(':memory:')
        m = {'ts': '1800000000.000200', 'user': SAM, 'text': '我请假到 11-20，审批人换成 lin'}
        out = A.handle_switch(rt, db, 'C0000CARDS1', m, now=1_793_000_000.0)
        self.assertEqual((out['outcome'], out['approvers']), ('approvers_switched', [LIN]))
        self.assertEqual(json.loads(path.read_text())['approvers'], [LIN])
        self.assertTrue(posts[0]['text'].startswith('✅ 审批人已换成 Lin Example'))
        self.assertEqual(posts[0]['thread_ts'], '1800000000.000200')
        self.assertEqual(A.handle_switch(rt, db, 'C0000CARDS1', m, now=1_793_000_000.0)['outcome'], 'duplicate')
        self.assertEqual(len(posts), 1)
        for other in ({**m, 'ts': '1800000000.000300', 'user': 'U0000TEST09'},          # not an owner
                      {**m, 'ts': '1800000000.000400', 'bot_id': 'B0000TEST07'},        # through an app
                      {**m, 'ts': '1800000000.000500', 'text': '今天天气不错'}):        # not a switch
            self.assertIsNone(A.handle_switch(rt, db, 'C0000CARDS1', other, now=1_793_000_000.0), other)
        refused = A.handle_switch(rt, db, 'C0000CARDS1', {**m, 'ts': '1800000000.000600', 'text': '审批人换成 nobody'},
                                  now=1_793_000_000.0)
        self.assertEqual(refused['outcome'], 'approvers_switch_refused')
        self.assertIn('没有换审批人', posts[-1]['text'])


class AuthorityOwnersTest(unittest.TestCase):
    def test_owners_in_the_decisions_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'decisions.json'
            auth = authority.Authority(owner_user=OWNER, owner_key='owner', user_re=re.compile(r'[UW][A-Z0-9]{2,}'),
                                       channel_re=re.compile(r'[CG][A-Z0-9]{2,}'), dm_re=re.compile(r'D[A-Z0-9]{2,}'))
            path.write_text(json.dumps({'decision_channel': 'C0000CARDS1', 'owners': [LIN, SAM], 'approvers': [SAM]}))
            config = auth.load_config(path)
            self.assertEqual((config['owners'], config['approvers']), ([LIN, SAM], [SAM]))
            self.assertEqual(authority.card_mentions(config, OWNER), f'<@{SAM}>')
            self.assertTrue(authority.card_mentioned(config, OWNER, f'<@{SAM}> 要你决定'))
            self.assertEqual(authority.card_mentions({**config, 'owners': None}, OWNER), f'<@{OWNER}>')
            self.assertIsNone(auth.load_config(Path(tmp) / 'missing.json')['owners'])
            path.write_text(json.dumps({'decision_channel': 'C0000CARDS1', 'owners': [LIN], 'approvers': [SAM]}))
            with self.assertRaises(ValueError):
                auth.load_config(path)
            path.write_text(json.dumps({'decision_channel': 'C0000CARDS1', 'owners': [LIN, SAM], 'approvers': [SAM]}))
            worker = types.SimpleNamespace(load=lambda: ({'intake': {'identity': 'bot', 'owner_like': 'approvers'}}, None))
            buttons = types.SimpleNamespace(load_config=lambda: auth.load_config(path))
            self.assertEqual(authority.owner_like_users(worker, buttons, _cache={}), frozenset({LIN, SAM}))


class NoticesTest(unittest.TestCase):
    def test_one_notice_per_change_after_the_cursor(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'decisions.json'
        log = A.log_path(path)
        db = sqlite3.connect(':memory:', isolation_level=None)
        db.execute('CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT)')
        posts, errors = [], []

        @contextlib.contextmanager
        def transaction(db):
            db.execute('BEGIN IMMEDIATE')
            yield
            db.execute('COMMIT')
        fail = []

        def post(rt, db, provider, kind, text, context_key=None, page=None):
            if fail:
                raise RuntimeError('ratelimited')
            posts.append((provider, kind, text, context_key, page))
            return 'ts'
        rt = types.SimpleNamespace(buttons=types.SimpleNamespace(config_path=lambda: path), transaction=transaction,
                                   provider_health=types.SimpleNamespace(post_notice=post),
                                   intake=types.SimpleNamespace(person_name=lambda rt, db, uid: {OWNER: 'Owner'}.get(uid)),
                                   _meta_error=lambda db, key, exc: errors.append(key))
        notices = A.Notices('zh-CN')
        log.write_text(json.dumps({'who': 'dashboard', 'old': [OWNER], 'new': [OWNER], 'added': [], 'removed': []}) + '\n')
        self.assertEqual(notices.step(rt, db), [])                      # the first run only sets the cursor
        with log.open('a') as stream:
            stream.write(json.dumps({'who': 'dashboard:lin@example.com', 'old': [OWNER], 'new': [OWNER, LIN],
                                     'added': [LIN], 'removed': [], 'names': {LIN: 'Lin Example'}}) + '\n')
        fail.append(1)
        self.assertEqual(notices.step(rt, db), [])
        self.assertEqual(errors, ['approvers_notice_error'])
        fail.clear()
        self.assertEqual(notices.step(rt, db), [1])                     # retried
        self.assertEqual(posts[0][2], '审批人已变更：lin@example.com 加了 Lin Example。现在的审批人：Owner、Lin Example。')
        self.assertEqual(posts[0][3:], ('approvers.notice_context', '/settings'))
        self.assertEqual(notices.step(rt, db), [])


if __name__ == '__main__':
    unittest.main()
