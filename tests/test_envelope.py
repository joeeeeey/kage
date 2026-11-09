import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import types
import unittest

from kage.adapters.slack import envelope as slack_envelope
from kage.core import envelope as core

OWNER, OLA, DEPUTY, BOT, APP, TEAM = 'U0000TEST01', 'U0000TEST03', 'U0000TEST04', 'U0000TEST02', 'A0000TEST01', 'T0000TEST01'


def make():
    env = core.Envelopes(owner_key='mira', owner_user=OWNER)
    return env, slack_envelope.SlackEnvelopes(env, workspace=TEAM, assistant_app=APP, assistant_bot_user=BOT,
                                              agent_marker='mira_kage_sent')


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.env, self.slack = make()

    def classify(self, **m):
        return self.slack.classify_message(m, thread_root='1.0')

    def test_owner_hand_typed_carries_intent_named_by_the_key(self):
        e = self.classify(user=OWNER)
        self.assertEqual((e['author_kind'], e['trust'], e['authority'], e['rule']),
                         ('human', 'mira', 'mira_intent', 'mira_hand_typed'))
        e = self.classify(user=OWNER, subtype='message_changed')
        self.assertEqual((e['authority'], e['rule']), ('none', 'mira_non_message'))
        self.assertEqual(self.classify(user=OWNER, subtype='thread_broadcast')['authority'], 'mira_intent')

    def test_an_owner_like_approver_carries_the_owners_intent_and_says_so(self):
        # 0.77.0: in a bot-identity deployment with intake.owner_like approvers, kage.app wires this to the approvers
        self.assertEqual(self.classify(user=OLA)['trust'], 'colleague')
        self.env.owner_like = lambda: frozenset({OLA})
        e = self.classify(user=OLA)
        self.assertEqual((e['trust'], e['authority'], e['rule']), ('mira', 'mira_intent', 'owner_like_approver'))
        e = self.classify(user=OLA, subtype='message_changed')
        self.assertEqual((e['authority'], e['rule']), ('none', 'owner_like_approver_non_message'))
        self.assertEqual(self.classify(user=OWNER)['rule'], 'mira_hand_typed')  # the owner is unchanged
        self.assertEqual(self.classify(user=DEPUTY)['trust'], 'colleague')     # everyone else too

    def test_our_agents_are_agents_even_with_the_owner_token(self):
        meta = {'event_type': 'mira_kage_sent', 'event_payload': {'kind': 'reply', 'provider': 'claude',
                                                                   'session': 's1', 'task_cwd': '/w/t', 'hops': '2'}}
        e = self.classify(user=OWNER, metadata=meta)
        self.assertEqual((e['author_kind'], e['trust'], e['authority'], e['rule'], e['hops']),
                         ('agent', 'agent', 'none', 'agent_metadata', 3))
        self.assertEqual((e['sender_provider'], e['sender_session'], e['sender_task_cwd']), ('claude', 's1', '/w/t'))
        self.assertEqual(self.classify(user=OWNER, app_id=APP)['rule'], 'agent_app_id')
        self.assertEqual(self.slack.classify_message({'user': OWNER}, ledger_hit=True)['rule'], 'agent_ledger')
        other_marker = {'event_type': 'someone_else_sent', 'event_payload': {}}
        self.assertEqual(self.classify(user=OWNER, metadata=other_marker)['trust'], 'mira')

    def test_another_persons_agent_never_gets_the_owner_trust(self):
        self.assertEqual(self.classify(user=OWNER, app_id='A0000TEST09')['trust'], 'agent')
        e = self.classify(user=OLA, app_id='A0000TEST09')
        self.assertEqual((e['author_kind'], e['trust'], e['rule']), ('agent', 'colleague', 'agent_foreign_app'))

    def test_bots_and_systems(self):
        for m in ({'user': OLA, 'bot_id': 'B1'}, {'user': OLA, 'subtype': 'bot_message'}, {'user': BOT}, {}):
            self.assertEqual(self.classify(**m)['trust'], 'system')

    def test_people(self):
        self.assertEqual(self.classify(user=OLA)['trust'], 'colleague')
        self.assertEqual(self.classify(user=OLA, user_team='T0000TEST09')['rule'], 'external_team')
        self.assertEqual(self.classify(user=OLA, user_profile={'is_restricted': True})['rule'], 'external_guest')
        e = self.slack.classify_message({'user': DEPUTY}, delegates={DEPUTY})
        self.assertEqual((e['trust'], e['authority']), ('delegate', 'none'))
        people = {OLA: {'employment_status': 'left'}}
        self.assertEqual(self.slack.classify_message({'user': OLA}, people=people)['rule'], 'external_registry')


class InteractionTest(unittest.TestCase):
    def test_interactions(self):
        env, _ = make()
        self.assertEqual(env.interaction('decision_answer', OWNER, verified_decision=True)['authority'],
                         'decision_verified')
        self.assertEqual(env.interaction('decision_answer', OWNER)['rule'], 'mira')
        self.assertEqual(env.interaction('decision_answer', DEPUTY, role='ooo_delegate')['trust'], 'delegate')
        self.assertEqual(env.interaction('job_result', None, system=True)['rule'], 'system_runtime')
        self.assertEqual(env.trusts[0], 'mira')
        self.assertEqual(env.authorities[1], 'mira_intent')


class PeopleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / 'people.json'

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_index_reads_only_status_fields(self):
        doc = {'people': [{'employment': {'status': 'active', 'entity': 'external:vendor'}, 'private': 'never read',
                           'identities': [{'type': 'slack', 'value': OLA, 'status': 'guest', 'guest': True},
                                          {'type': 'email', 'value': 'ola@northwind.example'},
                                          {'type': 'slack', 'value': 'not-an-id'}]}, 'junk', {'identities': None}]}
        self.path.write_text(json.dumps(doc))
        people = core.People(lambda: self.path)
        self.assertEqual(people.load(), {OLA: {'employment_status': 'active', 'entity': 'external:vendor',
                                               'slack_status': 'guest', 'guest': True}})
        self.assertTrue(core.registry_external(people.load()[OLA]))

    def test_missing_or_broken_file_means_no_facts(self):
        people = core.People(lambda: self.path)
        self.assertEqual(people.load(), {})
        self.path.write_text('{')
        self.assertEqual(people.load(), {})

    def test_reload_on_change(self):
        people = core.People(lambda: self.path)
        self.path.write_text(json.dumps({'people': []}))
        self.assertEqual(people.load(), {})
        self.path.write_text(json.dumps({'people': [{'identities': [{'type': 'slack', 'value': OLA}]}]}))
        self.assertIn(OLA, people.load())


class StoreTest(unittest.TestCase):
    def test_store_lookup_and_prompt_suffix(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        core.initialize(db)
        env, slack = make()
        e = slack.classify_message({'user': OWNER}, thread_root='9.9')
        core.store(db, 'ev1', e, 1.0)
        core.store(db, 'ev1', {**e, 'trust': 'changed'}, 2.0)   # first write wins
        core.store(db, 'ev2', None, 1.0)
        core.store(None, 'ev3', e, 1.0)                           # never raises
        self.assertEqual(core.lookup(db, 'ev1'), {'kind': 'slack_message', 'author_kind': 'human', 'trust': 'mira',
                                                  'authority': 'mira_intent', 'thread_root': '9.9', 'burst_id': '9.9',
                                                  'hops': 0, 'rule': 'mira_hand_typed'})
        self.assertIsNone(core.lookup(db, 'ev2'))
        self.assertIsNone(core.lookup(None, 'ev1'))
        self.assertEqual(core.prompt_suffix(e), ' envelope=kind:slack_message,author:human,trust:mira,'
                                                'authority:mira_intent,hops:0')
        self.assertEqual(core.prompt_suffix(None), '')


class SelfWakeTest(unittest.TestCase):
    def test_self_wake(self):
        agent = {'author_kind': 'agent', 'sender_session': 's1', 'sender_provider': 'claude', 'sender_task_cwd': '/w/a'}
        task_of = lambda cwd: {'/w/a': 'a', '/w/b': 'b'}.get(cwd)  # noqa: E731
        self.assertTrue(core.self_wake(agent, {'session': 's1'}, 'claude', task_of))
        self.assertFalse(core.self_wake(agent, {'session': 's1', 'cwd': '/w/b'}, 'codex', task_of))
        self.assertTrue(core.self_wake(agent, {'session': 's9', 'cwd': '/w/a'}, 'codex', task_of))
        self.assertFalse(core.self_wake({**agent, 'author_kind': 'human'}, {'session': 's1'}, 'claude', task_of))


class DelegatesTest(unittest.TestCase):
    def test_active_delegates(self):
        buttons = types.SimpleNamespace(ooo_in_window=lambda config, ts: 10 <= ts <= 20)
        config = {'ooo': {'delegates': [DEPUTY]}}
        self.assertEqual(core.active_delegates(config, 15, buttons), {DEPUTY})
        self.assertEqual(core.active_delegates(config, 25, buttons), set())
        self.assertEqual(core.active_delegates({}, 15, buttons), set())
        self.assertEqual(core.active_delegates(config, 'x', buttons), set())


if __name__ == '__main__':
    unittest.main()
