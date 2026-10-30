import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import types
import unittest

from kage.policy import card_gate, registry

SEED = Path(__file__).resolve().parent / 'fixtures/registry-seed'
PR = 'https://github.com/northwind-example/app/pull/7'


class FakeRuntime:
    """What the gate reads from its runtime handle: the registry, the copy lint and (open_limit) config and replies."""

    def __init__(self, live):
        self.rule_registry = registry.Registry(live_dir=lambda: live, seed=SEED, guards=[
            json.loads(p.read_text())['enforcement']['guard'] for p in SEED.glob('R-*.json')], language='en')
        self.linted = []
        self.buttons = types.SimpleNamespace(check_copy=lambda v, k, first_line_max=None: self.linted.append(k))
        self.worker_config = types.SimpleNamespace(load=lambda: ({'intake': {}},), DEFAULTS={},
                                                   intake_role=lambda config, task: task == 'mira-inbox')
        self.card_replies = types.SimpleNamespace(config=lambda rt: {'max_open_per_task': 2}, OPEN_CARD_DAYS=7,
                                                  continued=lambda rt, db, event_id: event_id == 'continued')
        self.task_lifecycle = types.SimpleNamespace(config=lambda rt: {'stale_card_days': 0})


def gate(**kw):
    base = {'decide': 'Decide whether to merge the fix', 'waiting': 'Mira waits on you since 09:30',
            'waiting_on': 'mira', 'checked': ['CI is green on the PR']}
    return {**base, **kw}


class CardGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rt = FakeRuntime(self.tmp / 'live')
        self.gate = card_gate.CardGate(language='en', owner_key='mira', owner_name='Mira')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def refused(self, fragment, g, **kw):
        with self.assertRaises(ValueError) as ctx:
            self.gate.check(self.rt, g, **kw)
        self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def test_a_complete_card_renders_its_lines(self):
        shown = self.gate.check(self.rt, gate())
        self.assertEqual(shown, {'decide': 'Decide whether to merge the fix',
                                 'lines': ['Situation: Mira waits on you since 09:30',
                                           'Checked: CI is green on the PR']})
        self.assertEqual(self.rt.linted, ['decide', 'waiting', 'checked'])
        self.assertEqual(card_gate.compose(shown, 'Details below.'),
                         'Decide whether to merge the fix\nSituation: Mira waits on you since 09:30\n'
                         'Checked: CI is green on the PR\nDetails below.')

    def test_decision_line(self):
        self.refused('card_needs_a_decision_line', gate(decide=''))
        message = self.refused('decision_line_is_vague', gate(decide='blocked, please help'))
        self.assertIn('does not say what Mira must decide. Fix: Fix the decision_line field. [R-0001', message)

    def test_situation_line(self):
        self.refused('card_needs_the_situation', gate(waiting_on='joe'))
        self.refused('situation_line_is_incomplete', gate(waiting='Mira waits on you'))
        message = self.refused('situation_does_not_name_mira', gate(waiting='Ola waits since 09:30'))
        self.assertIn('--waiting-on mira, but --waiting does not say that Mira (you) is the one waited on', message)
        self.gate.check(self.rt, gate(waiting='Ola waits on mira since 09:30'))   # the owner key names the owner too

    def test_waiting_on_a_person_needs_a_reason(self):
        g = gate(waiting='Mira waits on Ola since 09:30', waiting_on='person')
        self.refused('waiting_on_a_person_is_not_a_card_to_mira: --waiting-on person without --why-mira', g)
        self.gate.check(self.rt, {**g, 'why_owner': 'spend'})

    def test_failures_need_two_checks(self):
        self.refused('card_needs_what_you_checked: 0 --checked, 1 needed', gate(checked=[]))
        self.refused('2 needed (a failure card', gate(), text='the API answers 403')
        self.refused('2 needed', gate(), failure=True)
        self.gate.check(self.rt, gate(checked=['token valid', 'second route also 403']), failure=True)

    def test_pr_cards_have_four_parts(self):
        self.refused('pr_card_needs_four_parts: missing --pr-cause (Cause)', gate(), text=PR)
        parts = {'pr_cause': 'A retry loop', 'pr_why': 'Smallest fix', 'pr_change': 'Retries stop after three',
                 'pr_verify': 'Error rate drops'}
        self.refused('pr_change_names_files', gate(**{**parts, 'pr_change': 'edit worker.py'}), text=PR)
        self.refused('pr_card_needs_the_pr_link', gate(**parts))
        shown = self.gate.check(self.rt, gate(**parts), text=PR)
        self.assertEqual(shown['lines'][2:], ['Cause: A retry loop', 'Why this way: Smallest fix',
                                              'What changes: Retries stop after three',
                                              'How to verify after merge: Error rate drops'])

    def test_infra_apply_cards_name_the_plan(self):
        self.refused('infra_card_needs_the_plan_scope: missing --plan-pr', gate(), text='run terraform apply')
        plan = {'plan_pr': 'infra#12', 'plan_root': 'dns', 'plan_resources': 'one record', 'plan_counts': '1/2'}
        self.refused('--plan-counts must be C/U/D/R', gate(**plan))
        shown = self.gate.check(self.rt, gate(**{**plan, 'plan_counts': '1/0/0/0'}))
        self.assertEqual(shown['lines'][-5:], ['Plan: infra#12 · root dns', 'Resources: one record',
                                               'Create 1 · Update 0', 'Delete 0 · Replace 0', self.gate.apply_line])

    def test_field_lengths(self):
        with self.assertRaisesRegex(ValueError, 'decide_must_be_at_most_80_characters'):
            self.gate.check(self.rt, gate(decide='Decide whether ' + 'x' * 80))

    def test_a_retired_rule_turns_its_check_off(self):
        self.rt.rule_registry.seed()
        live = self.tmp / 'live/R-0001.json'
        rule = json.loads(live.read_text())
        live.write_text(json.dumps({**rule, 'status': 'retired', 'retired': '2026-02-01'}))
        self.assertEqual(self.gate.check(self.rt, gate(decide=''))['decide'], '')

    def test_chinese_owner_language(self):
        gate_zh = card_gate.CardGate(language='zh-CN', owner_key='mira', owner_name='Mira')
        self.assertEqual(gate_zh.owner_named_re.pattern, '(?i)你|mira')
        shown = gate_zh.check(self.rt, {'decide': '要你决定：是否合并这个修复', 'waiting': '等你批准，从 09:30 开始',
                                        'waiting_on': 'mira', 'checked': ['CI 绿了', '本地复现过']})
        self.assertEqual(shown['lines'], ['局面：等你批准，从 09:30 开始', '查过：CI 绿了；本地复现过'])


class OpenLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rt = FakeRuntime(self.tmp / 'live')
        self.gate = card_gate.CardGate(language='en', owner_key='mira', owner_name='Mira')
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''
          CREATE TABLE escalations (event_id TEXT, watch_id TEXT, stage TEXT);
          CREATE TABLE escalation_cards (event_id TEXT, task_id TEXT, dry INT, smoke INT, created REAL);
          CREATE TABLE events (id TEXT, disposition TEXT);
          CREATE TABLE decisions (event_id TEXT);
          CREATE TABLE watches (id TEXT, data TEXT);
          CREATE TABLE card_replies (event_id TEXT, stage TEXT);''')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def card(self, event_id, task='t1', disposition='ask_mira', created=1000.0, stage='watching'):
        self.db.execute('INSERT INTO escalations VALUES (?,?,?)', (event_id, 'w1', stage))
        self.db.execute('INSERT INTO escalation_cards VALUES (?,?,0,0,?)', (event_id, task, created))
        self.db.execute('INSERT INTO events VALUES (?,?)', (event_id, disposition))

    def test_the_cap_counts_open_owner_cards_of_the_task(self):
        self.db.execute("INSERT INTO watches VALUES ('w1', '{\"state\": \"running\"}')")
        self.card('e1')
        self.gate.open_limit(self.rt, self.db, 't1', now=2000.0)
        self.card('e2')
        with self.assertRaisesRegex(ValueError, r'too_many_open_cards: task t1 already has 2 cards from the last 7 days '
                                                r'waiting for Mira \(cards.max_open_per_task=2\)'):
            self.gate.open_limit(self.rt, self.db, 't1', now=2000.0)
        self.gate.open_limit(self.rt, self.db, 't1', replaced='e2', now=2000.0)   # the card being taken over
        self.gate.open_limit(self.rt, self.db, 'mira-inbox', now=2000.0)          # intake tasks are exempt

    def test_only_cards_that_still_wait_count(self):
        self.db.execute("INSERT INTO watches VALUES ('w1', '{\"state\": \"running\"}')")
        recent = 690000.0                                # now 700000, 7-day window: created after 95200
        self.card('e1', created=recent)
        self.card('other', disposition='brief', created=recent)   # not an owner card
        self.card('old', created=1.0)                             # older than the stale window
        self.card('continued', created=recent)                    # a newer card continues it
        self.card('decided', created=recent); self.db.execute("INSERT INTO decisions VALUES ('decided')")
        self.card('retired', created=recent); self.db.execute("INSERT INTO card_replies VALUES ('retired', 'retired')")
        self.gate.open_limit(self.rt, self.db, 't1', now=700000.0)
        self.card('e2', created=recent)
        with self.assertRaisesRegex(ValueError, 'already has 2 cards'):
            self.gate.open_limit(self.rt, self.db, 't1', now=700000.0)


if __name__ == '__main__':
    unittest.main()
