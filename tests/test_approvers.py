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

OWNER, BOT, JAY, HAO, HR, GUEST = 'U0000OWNER1', 'U0000BOT001', 'U0000TEST01', 'U0000TEST02', 'U0000HR0001', 'U0000GUEST1'
TEAM = 'T0000TEST01'
USERS = {
    JAY: {'id': JAY, 'team_id': TEAM, 'profile': {'real_name': 'Jay Example'}},
    HAO: {'id': HAO, 'team_id': TEAM, 'profile': {'real_name': 'Hao Example'}},
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
            if params['email'] == 'hao@example.com':
                return {'user': USERS[HAO]}
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
        out = self.ed.save([OWNER, JAY, 'hao@example.com'], st['digest'], 'dashboard:jay@example.com', now=1_800_000_000.0)
        self.assertEqual(out['change']['added'], [JAY, HAO])
        written = json.loads(self.path.read_text())
        self.assertEqual(written, {'decision_channel': 'C0000CARDS1', 'approvers': [OWNER, JAY, HAO]})  # other keys kept
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), '0o600')
        new, who, note = self.rules.installed[0]
        self.assertEqual((new['version'], new['defaults']['approvers'], who), (4, [OWNER, JAY, HAO], 'dashboard:jay@example.com'))
        self.assertEqual(new['defaults']['approver_names_zh'], {OWNER: 'Owner', JAY: 'Jay Example', HAO: 'Hao Example'})
        self.assertEqual(new['rules'][0]['bounds']['approvers'], [OWNER, JAY, HAO])
        self.assertEqual(new['rules'][1]['bounds'], {'max_days': 7})
        log = A.changes(self.path)
        self.assertEqual((log[0]['who'], log[0]['old'], log[0]['new'], log[0]['rules_version']),
                         ('dashboard:jay@example.com', [OWNER], [OWNER, JAY, HAO], 4))
        self.assertEqual(json.loads(Path(log[0]['backup']).read_text())['approvers'], [OWNER])
        st = out['state']
        self.assertEqual([(a['id'], a['in_decisions'], a['in_rules']) for a in st['approvers']],
                         [(OWNER, True, True), (JAY, True, True), (HAO, True, True)])
        # remove one; the owner stays
        out = self.ed.save([OWNER, JAY], st['digest'], 'dashboard:hao@example.com')
        self.assertEqual((out['change']['removed'], self.rules.active['version']), ([HAO], 5))

    def test_only_active_people_of_this_workspace_and_the_owner_stays(self):
        d = self.ed.state()['digest']
        for entries, code in (([OWNER, 'U0000APPBOT'], 'approver_must_be_a_person_not_a_bot'),
                              ([OWNER, BOT], 'approver_must_be_a_person_not_a_bot'),
                              ([OWNER, 'U0000GONE01'], 'approver_account_is_deactivated'),
                              ([OWNER, GUEST], 'approver_must_be_a_full_member_of_this_workspace'),
                              ([OWNER, 'U0000OTHER1'], 'approver_must_be_a_full_member_of_this_workspace'),
                              ([OWNER, 'U0000NOBODY'], 'approver_not_found_in_slack:'),
                              ([OWNER, 'nobody@example.com'], 'approver_not_found_in_slack:'),
                              ([OWNER, 'Jay'], 'approver_must_be_a_slack_user_id_or_an_email'),
                              ([JAY], 'the_owner_stays_an_approver'),
                              ([OWNER, HR], 'an_hr_contact_cannot_be_an_approver'),
                              ([OWNER, JAY, HAO, HR, 'U0000TEST03', 'U0000TEST04'], 'at_most_5_approvers'),
                              ([OWNER], 'nothing_to_change'), ([], 'approvers_must_be_a_list')):
            with self.subTest(entries), self.assertRaises(ValueError) as ctx:
                self.ed.save(entries, d, 'dashboard')
            self.assertIn(code, str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.ed.save([OWNER, JAY], '0' * 16, 'dashboard')
        self.assertIn('approvers_changed_since_loaded_reload', str(ctx.exception))
        self.assertEqual(json.loads(self.path.read_text())['approvers'], [OWNER])
        self.assertEqual(self.rules.installed, [])

    def test_a_failed_rules_install_puts_the_decisions_file_back(self):
        self.rules.fail = 'rules_version_exists_with_different_content'
        with self.assertRaises(ValueError):
            self.ed.save([OWNER, JAY], self.ed.state()['digest'], 'dashboard')
        self.assertEqual(json.loads(self.path.read_text()), {'decision_channel': 'C0000CARDS1', 'approvers': [OWNER]})
        self.assertEqual(A.changes(self.path), [])

    def test_without_installed_rules_only_the_decisions_file(self):
        self.rules.load_active = lambda category='access': (_ for _ in ()).throw(ValueError('rules_not_installed'))
        st = self.ed.state()
        self.assertIsNone(st['rules'])
        out = self.ed.save([OWNER, JAY], st['digest'], 'dashboard')
        self.assertIsNone(out['rules'])
        self.assertEqual(json.loads(self.path.read_text())['approvers'], [OWNER, JAY])


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
            stream.write(json.dumps({'who': 'dashboard:jay@example.com', 'old': [OWNER], 'new': [OWNER, JAY],
                                     'added': [JAY], 'removed': [], 'names': {JAY: 'Jay Example'}}) + '\n')
        fail.append(1)
        self.assertEqual(notices.step(rt, db), [])
        self.assertEqual(errors, ['approvers_notice_error'])
        fail.clear()
        self.assertEqual(notices.step(rt, db), [1])                     # retried
        self.assertEqual(posts[0][2], '审批人已变更：jay@example.com 加了 Jay Example。现在的审批人：Owner、Jay Example。')
        self.assertEqual(posts[0][3:], ('approvers.notice_context', '/settings'))
        self.assertEqual(notices.step(rt, db), [])


if __name__ == '__main__':
    unittest.main()
