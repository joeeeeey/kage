"""0.78.7 (F19): role facts in the people registry — whitelisted patches, HR's confirmation read back from Slack, the
approver card and its settle, the new revision (backup, provenance, change log) and the read view."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest

from kage.core import people as P

NOW = 1_800_000_000.0
HR, OWNER, APPROVER, PERSON, OTHER = 'U0000HR0001', 'U0000OWNER1', 'U0000APPR01', 'U0000TEST01', 'U0000TEST02'


def registry():
    return {'schema': 'example.people/v1', 'people': [
        {'id': 'slack:' + PERSON, 'name': {'display': 'Mira Example'}, 'identities': [
            {'type': 'slack', 'value': PERSON, 'primary': True}, {'type': 'email', 'value': 'mira@example.com'}],
         'employment': {'status': 'work_trial', 'as_of': '2026-10-01', 'contract': 'unknown'},
         'role': {'function': 'unknown', 'disciplines': [], 'title': None, 'team': {'name': None, 'lead_person_id': None}},
         'owner_view': {'note': 'PRIVATE'}, 'authority': {'approver_scope': 'none'}, 'flags': [],
         'provenance': {'employment.status': 'task_history', 'owner_view': 'owner_note'}},
        {'id': 'slack:' + OTHER, 'name': {'display': 'Lead Example'}, 'identities': [{'type': 'slack', 'value': OTHER, 'primary': True}],
         'employment': {'status': 'active'}, 'role': {'function': 'engineering'}}]}


def store():
    db = sqlite3.connect(':memory:', isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE decisions (event_id TEXT PRIMARY KEY, choice TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL,
        decided_at REAL NOT NULL, card_channel TEXT, card_ts TEXT);
      CREATE TABLE escalation_cards (event_id TEXT PRIMARY KEY, channel TEXT, options TEXT);''')
    return db


class Runtime(types.SimpleNamespace):
    pass


def runtime(path, thread=None, hr=(HR,)):
    cards, replies, reads = [], [], []

    @contextlib.contextmanager
    def transaction(db):
        db.execute('BEGIN IMMEDIATE')
        try:
            yield
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise

    def supervisor_card(db, question, options, **kw):
        cards.append((question, options, kw))
        eid = f'{len(cards):024x}'
        db.execute('INSERT INTO escalation_cards VALUES (?,?,?)', (eid, 'C0000CARDS1', json.dumps(options)))
        return {'event_id': eid, 'channel': 'C0000CARDS1', 'thread_ts': '1800000000.000100'}

    def api_call(method, token, params=None, json_body=None):
        reads.append((method, token, params))
        if token == 'bot-token-unreadable':
            raise RuntimeError('not_in_channel')
        return {'messages': thread or []}

    def check_secret_free(text):
        if 'xoxb-' in text:
            raise ValueError('text_looks_like_it_contains_a_credential')

    access = types.SimpleNamespace(pack=types.SimpleNamespace(FUNCTIONS=('engineering', 'non_engineering', 'unknown')))
    return Runtime(
        transaction=transaction, safe_error=lambda e: type(e).__name__, supervisor_card=supervisor_card,
        envelopes=types.SimpleNamespace(people_path=lambda: path), slack=types.SimpleNamespace(api_call=api_call),
        api_identity=lambda bot=False: 'bot-token' if bot else 'user-token', asks=types.SimpleNamespace(
            check_secret_free=check_secret_free),
        rules=types.SimpleNamespace(ACCESS_POLICY=access, load_active=lambda category: ({'defaults': {'hr': list(hr)}}, {}),
                                    _card_reply=lambda rt, db, eid, text, pid, kind: replies.append((eid, text, kind))),
        buttons=types.SimpleNamespace(card_model=lambda options: json.loads(options),
                                      resolve_choice=lambda choice, model: {'id': choice}),
        cards=cards, replies=replies, reads=reads)


def hr_thread(text='是的，确认', author=HR, ts='1799999000.000200', mention=True, **extra):
    root = {'ts': '1799998000.000100', 'user': 'U0000BOT001', 'bot_id': 'B0000BOT001',
            'text': (f'<@{author}> 请确认 <@{PERSON}> 的职位是后端工程师吗？' if mention else '请确认一下职位')}
    return [root, {'ts': ts, 'user': author, 'text': text, **extra}]


class PeopleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'people.json'
        self.path.write_text(json.dumps(registry(), ensure_ascii=False, indent=2))
        os.chmod(self.path, 0o600)
        self.db = store()
        self.roles = P.PeopleRoles('zh-CN')
        self.patch = {'role': {'title': '后端工程师', 'function': 'engineering', 'disciplines': ['backend'],
                               'team': {'name': 'Platform', 'lead_person_id': 'slack:' + OTHER}},
                      'employment': {'status': 'active', 'since': '2026-11-01'}}

    def person(self):
        return P.find(json.loads(self.path.read_text()), PERSON)

    def link(self, ts='1799999000.000200', thread='1799998000.000100'):
        return f'https://example.slack.com/archives/C0000HRCHAN/p{ts.replace(".", "")}?thread_ts={thread}&cid=C0000HRCHAN'

    # ---- the patch -------------------------------------------------------------------------------------------------
    def test_only_whitelisted_fields_and_valid_values(self):
        raw = registry()
        target = P.find(raw, PERSON)
        out = P.validate_patch(raw, target, self.patch, ('engineering', 'non_engineering'))
        self.assertEqual(out, {'role.title': '后端工程师', 'role.function': 'engineering', 'role.disciplines': ['backend'],
                               'role.team.name': 'Platform', 'role.team.lead_person_id': 'slack:' + OTHER,
                               'employment.status': 'active', 'employment.as_of': '2026-11-01'})
        for bad, code in (({'owner_view': {'note': 'x'}}, 'field_not_allowed_owner_view'),
                          ({'authority': {'approver_scope': 'all'}}, 'field_not_allowed_authority'),
                          ({'flags': ['privileged_access']}, 'field_not_allowed_flags'),
                          ({'identities': []}, 'field_not_allowed_identities'),
                          ({'role': {'seniority': 'staff'}}, 'field_not_allowed_role_seniority'),
                          ({'employment': {'status': 'contractor'}}, 'employment_status_must_be_one_of'),
                          ({'employment': {'as_of': '2026-13-01'}}, 'employment_as_of_must_be_a_date'),
                          ({'role': {'function': 'sales'}}, 'role_function_must_be_one_of_engineering'),
                          ({'role': {'team': {'lead_person_id': 'slack:U0000NOBODY'}}}, 'lead_person_id_must_be_another'),
                          ({'role': {'team': {'lead_person_id': 'slack:' + PERSON}}}, 'lead_person_id_must_be_another'),
                          ({'role': {'title': 'x\ny'}}, 'role_title_must_be_text'),
                          ({'employment': {'status': 'work_trial'}}, 'nothing_to_change'), ({}, 'patch_must_be')):
            with self.subTest(bad), self.assertRaises(ValueError) as ctx:
                P.validate_patch(raw, target, bad, ('engineering', 'non_engineering'))
            self.assertIn(code, str(ctx.exception))

    def test_show_never_prints_the_private_view(self):
        out = P.show(self.path, PERSON)
        self.assertEqual((out['person'], out['name'], out['slack']), ('slack:' + PERSON, 'Mira Example', PERSON))
        self.assertEqual(out['employment']['status'], 'work_trial')
        self.assertEqual(out['provenance'], {'employment.status': 'task_history'})
        text = json.dumps(out)
        for private in ('owner_view', 'PRIVATE', 'authority', 'flags', 'mira@example.com'):
            self.assertNotIn(private, text)
        self.assertNotIn('role', P.show(self.path, PERSON, ('employment',)))
        with self.assertRaises(ValueError):
            P.show(self.path, PERSON, ('owner_view',))

    # ---- HR's confirmation ---------------------------------------------------------------------------------------
    def test_hr_confirmation_writes_a_new_revision_with_provenance(self):
        rt = runtime(self.path, hr_thread())
        out = self.roles.propose(rt, self.db, PERSON, self.patch, hr_link=self.link(), now=NOW)
        self.assertEqual((out['action'], out['revision'], out['source']), ('written', 1, 'hr_confirmed'))
        self.assertEqual(rt.reads[0][:2], ('conversations.replies', 'bot-token'))
        self.assertEqual(rt.reads[0][2], {'channel': 'C0000HRCHAN', 'ts': '1799998000.000100', 'limit': 200})
        person = self.person()
        self.assertEqual((person['role']['title'], person['role']['team']['lead_person_id'], person['employment']['status'],
                          person['employment']['as_of']), ('后端工程师', 'slack:' + OTHER, 'active', '2026-11-01'))
        self.assertEqual(person['owner_view'], {'note': 'PRIVATE'})                       # untouched
        self.assertEqual(person['provenance']['role.title'], 'hr_confirmed')
        self.assertEqual(person['provenance']['owner_view'], 'owner_note')
        record = person['provenance_records']['employment.status']
        self.assertEqual((record['source'], record['by'], record['proposal']), ('hr_confirmed', HR, out['proposal']))
        self.assertEqual(record['source_link'], 'https://slack.com/archives/C0000HRCHAN/p1799999000000200'
                                                '?thread_ts=1799998000.000100')
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), '0o600')
        backup = Path(out['backup'])
        self.assertEqual(json.loads(backup.read_text()), registry())                     # the previous bytes
        self.assertEqual(oct(backup.stat().st_mode & 0o777), '0o600')
        log = [json.loads(line) for line in (self.path.parent / 'people.changes.jsonl').read_text().splitlines()]
        self.assertEqual(len(log), 7)
        self.assertEqual({(r['field'], r['old'], r['new']) for r in log if r['field'] == 'employment.status'},
                         {('employment.status', 'work_trial', 'active')})
        self.assertEqual(self.db.execute('SELECT state, hr_user FROM people_proposals').fetchone()[:], ('written', HR))
        with self.assertRaises(ValueError) as ctx:                                        # one message, one write
            self.roles.propose(rt, self.db, PERSON, {'role': {'title': 'Staff'}}, hr_link=self.link(), now=NOW)
        self.assertIn('already_used', str(ctx.exception))
        self.assertEqual(rt.cards, [])

    def test_hr_confirmation_is_refused_unless_it_is_hr_s_own_clear_yes_about_this_person(self):
        cases = ((hr_thread(author=OTHER), 'author_is_not_hr'),
                 (hr_thread(bot_id='B0000BOT002'), 'not_a_person_s_own_message'),
                 (hr_thread(subtype='message_changed'), 'not_a_person_s_own_message'),
                 (hr_thread(text='不是，他是前端'), 'not_a_clear_confirmation'),
                 (hr_thread(text='收到，我问一下'), 'not_a_clear_confirmation'),
                 (hr_thread(text='Yes, but not the team'), 'not_a_clear_confirmation'),
                 (hr_thread(mention=False), 'does_not_mention_the_person'),
                 (hr_thread(ts='1798000000.000200'), 'older_than_14_days'))
        for thread, code in cases:
            with self.subTest(code=code, text=thread[1].get('text')):
                rt = runtime(self.path, thread)
                with self.assertRaises(ValueError) as ctx:
                    self.roles.propose(rt, self.db, PERSON, self.patch, hr_link=self.link(ts=thread[1]['ts']), now=NOW)
                self.assertIn(code, str(ctx.exception))
        self.assertEqual(self.person()['employment']['status'], 'work_trial')
        with self.assertRaises(ValueError) as ctx:
            self.roles.propose(runtime(self.path, hr_thread()), self.db, PERSON, self.patch, hr_link='not a link', now=NOW)
        self.assertIn('must_be_a_slack_message_link', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.roles.propose(runtime(self.path, hr_thread(), hr=()), self.db, PERSON, self.patch, hr_link=self.link(),
                               now=NOW)
        self.assertIn('access_rules_name_no_hr', str(ctx.exception))

    def test_english_yes_counts(self):
        rt = runtime(self.path, hr_thread(text='Yes, confirmed.'))
        self.assertEqual(self.roles.propose(rt, self.db, PERSON, {'role': {'title': 'Backend engineer'}},
                                            hr_link=self.link(), now=NOW)['action'], 'written')

    # ---- the approver card -----------------------------------------------------------------------------------------
    def decide(self, eid, choice, actor=APPROVER, role='approver'):
        self.db.execute('INSERT INTO decisions VALUES (?,?,?,?,?,?,?)', (eid, choice, actor, role, NOW + 60, 'C0000CARDS1',
                                                                         '1800000000.000100'))

    def test_card_then_an_approver_click_writes(self):
        rt = runtime(self.path)
        out = self.roles.propose(rt, self.db, PERSON, self.patch, note='HR 在周会上口头确认', now=NOW)
        self.assertEqual(out['action'], 'carded')
        question, options, kw = rt.cards[0]
        self.assertEqual(options, ['write=写入:primary', 'skip=不写'])
        self.assertEqual(kw['kind'], 'people_proposal_card')
        self.assertEqual(question.splitlines()[0], '要你决定：是否把 Mira Example 的 7 项角色信息写进人员表')
        self.assertIn('· 在职状态：试岗 → 在职', question)
        self.assertIn('· 团队负责人：（空） → Lead Example', question)
        self.assertIn('依据：HR 在周会上口头确认', question)
        self.assertEqual(self.roles.settle(rt, self.db, now=NOW + 30), [])               # no click yet
        self.decide(out['card'], 'write', actor='U0000DELEG1', role='ooo_delegate')
        self.assertEqual(self.roles.settle(rt, self.db, now=NOW + 90), [])               # a delegate's click: not enough
        self.db.execute('DELETE FROM decisions')
        self.decide(out['card'], 'write')
        done = self.roles.settle(rt, self.db, now=NOW + 120)
        self.assertEqual((done[0]['choice'], done[0]['revision']), ('write', 1))
        person = self.person()
        self.assertEqual(person['provenance_records']['role.function']['source'], 'owner_approved')
        self.assertEqual(person['provenance_records']['role.function']['by'], APPROVER)
        self.assertEqual(person['provenance_records']['role.function']['source_link'],
                         'https://slack.com/archives/C0000CARDS1/p1800000000000100')
        self.assertEqual(rt.replies[-1][1:], ('✅ 已写入人员表：7 项，第 1 版（旧版本已备份）。', 'people_proposal_result'))
        self.assertEqual(self.roles.settle(rt, self.db, now=NOW + 200), [])              # settled once

    def test_skip_and_stale(self):
        rt = runtime(self.path)
        first = self.roles.propose(rt, self.db, PERSON, {'role': {'title': 'A'}}, now=NOW)
        second = self.roles.propose(rt, self.db, PERSON, {'role': {'title': 'B'}}, now=NOW)
        self.decide(first['card'], 'skip')
        self.roles.settle(rt, self.db, now=NOW + 60)
        self.assertEqual(rt.replies[-1][1], '好的，没有写入。')
        self.assertIsNone(self.person()['role']['title'])
        # the registry changes under the second proposal (another write): stale, nothing written
        rt3 = runtime(self.path, hr_thread())
        self.roles.propose(rt3, self.db, PERSON, {'role': {'title': 'C'}}, hr_link=self.link(), now=NOW)
        self.decide(second['card'], 'write')
        done = self.roles.settle(rt, self.db, now=NOW + 120)
        self.assertIn('stale_registry_changed_since_the_proposal', done[0]['error'])
        self.assertEqual(self.person()['role']['title'], 'C')
        self.assertIn('提议之后人员表里这几项已经被改过了', rt.replies[-1][1])
        states = dict(self.db.execute('SELECT id, state FROM people_proposals').fetchall())
        self.assertEqual((states[first['proposal']], states[second['proposal']]), ('declined', 'stale'))

    def test_a_secret_in_a_patch_is_refused(self):
        with self.assertRaises(ValueError):
            self.roles.propose(runtime(self.path), self.db, PERSON, {'role': {'title': 'xoxb-1234567890-abcdef'}}, now=NOW)

    def test_the_access_rules_read_the_new_role(self):
        from kage.policy import access
        policy = access.AccessPolicy.__new__(access.AccessPolicy)
        policy.key = 'owner'
        policy.pack = types.SimpleNamespace(FUNCTIONS=('engineering', 'non_engineering', 'unknown'), PRIVILEGED_SCOPES=('all',))
        self.assertEqual(policy.people_index(self.path)['slack'][PERSON]['function'], 'unknown')
        time.sleep(0.01)  # a new mtime for the index cache
        self.roles.propose(runtime(self.path, hr_thread()), self.db, PERSON, self.patch, hr_link=self.link(), now=NOW)
        facts = policy.people_index(self.path)['slack'][PERSON]
        self.assertEqual((facts['function'], facts['status'], facts['team_lead']), ('engineering', 'active', OTHER))


class PeopleProgramTest(unittest.TestCase):
    """The deployment's program: people-show reads the registry it is configured with; people-propose refuses an HR
    confirmation while the access rules (the HR list) are not installed, and a card cannot leave an offline install."""

    def test_the_program_commands(self):
        import subprocess
        import sys
        from tests.test_init import Base

        class Deploy(Base):
            def runTest(self):
                pass
        case = Deploy()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.records()
        case.init()
        people = case.home / 'people.json'
        people.write_text(json.dumps(registry(), ensure_ascii=False))
        os.chmod(people, 0o600)
        patch = case.home / 'patch.json'
        patch.write_text(json.dumps({'role': {'title': 'Backend engineer'}}))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('SLACK_', 'KAGE_')) and k != 'PYTHONPATH'}
        env.update({'HOME': str(case.home), 'KAGE_NO_REAL_SLACK': '1', 'KAGE_NO_AUTOSTART': '1', 'KAGE_SECRETS_BACKEND': 'file',
                    'KAGE_VERSIONS_DIR': str(case.release.parent), 'PYTHONDONTWRITEBYTECODE': '1', 'KAGE_PEOPLE': str(people)})
        run = lambda *args: subprocess.run([sys.executable, str(case.target / 'scripts/kage.py'), *args], capture_output=True,
                                           text=True, env=env, timeout=120, stdin=subprocess.DEVNULL, cwd=str(case.tmp))
        shown = run('people-show', '--person', PERSON, '--fields', 'role,employment')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        data = json.loads(shown.stdout)
        self.assertEqual((data['name'], data['employment']['status']), ('Mira Example', 'work_trial'))
        self.assertNotIn('PRIVATE', shown.stdout)
        hr = run('people-propose', '--person', PERSON, '--file', str(patch), '--hr-confirmation',
                 'https://example.slack.com/archives/C0000HRCHAN/p1799999000000200')
        self.assertNotEqual(hr.returncode, 0)
        self.assertIn('access_rules_not_installed_no_hr_list', hr.stdout + hr.stderr)
        carded = run('people-propose', '--person', PERSON, '--file', str(patch))
        self.assertNotEqual(carded.returncode, 0)
        self.assertIn('people_card_failed', carded.stdout + carded.stderr)
        self.assertEqual(json.loads(people.read_text()), registry())  # nothing written


if __name__ == '__main__':
    unittest.main()
