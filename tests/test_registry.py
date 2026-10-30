import json
from pathlib import Path
import shutil
import tempfile
import unittest

from kage.policy import registry

SEED = Path(__file__).resolve().parent / 'fixtures/registry-seed'
GUARDS = ('card.decision_line', 'card.situation_line', 'card.checked_first', 'card.not_waiting_on_person',
          'card.pr_four_parts', 'card.infra_plan_scope', 'card.open_limit', 'send.reply_language')


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.live = self.tmp / 'rules/registry'
        self.reg = self.make('en')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def make(self, language):
        return registry.Registry(live_dir=lambda: self.live, seed=SEED, guards=GUARDS, language=language)

    def rule(self, rid='R-0001'):
        return json.loads((SEED / f'{rid}.json').read_text())

    def test_seed_is_used_until_a_live_registry_exists(self):
        loaded = self.reg.load()
        self.assertEqual((loaded['source'], len(loaded['files']), len(loaded['cases'])), ('seed', 7, 7))
        self.reg.seed()
        self.assertEqual(self.reg.load()['source'], 'live')
        self.assertEqual(oct(self.live.stat().st_mode & 0o777), '0o700')
        self.assertEqual(oct((self.live / 'R-0001.json').stat().st_mode & 0o777), '0o600')
        self.assertFalse(self.reg.seed()['seeded'])  # never touches an existing directory

    def test_unreadable_live_file_falls_back_to_the_seed_file(self):
        self.reg.seed()
        (self.live / 'R-0002.json').write_text('{')
        loaded = self.reg.load()
        self.assertEqual(loaded['problems'], ['R-0002.json: not readable JSON'])
        self.assertEqual(dict(loaded['files'])['R-0002.json']['id'], 'R-0002')

    def test_active_and_retired(self):
        self.reg.seed()
        self.assertEqual(self.reg.active('card.decision_line')['id'], 'R-0001')
        rule = self.rule()
        rule.update(status='retired', retired='2026-02-01')
        (self.live / 'R-0001.json').write_text(json.dumps(rule))
        self.assertIsNone(self.reg.active('card.decision_line'))
        self.assertIsNone(self.reg.active('send.reply_language'))  # known guard, no rule: off

    def test_refusal_names_the_fix_and_the_rule(self):
        with self.assertRaisesRegex(ValueError, r'^bad_card\. Fix: Fix the decision_line field\. '
                                                r'\[R-0001 Lead with the decision\]$'):
            self.reg.refuse('card.decision_line', 'bad_card')
        with self.assertRaisesRegex(ValueError, r'\[card.unknown \]$'):
            self.reg.refuse('card.unknown', 'x')

    def test_lint(self):
        self.assertTrue(self.reg.lint()['ok'])
        self.reg.seed()
        dup = self.rule('R-0002')
        (self.live / 'R-0009.json').write_text(json.dumps({**dup, 'id': 'R-0009', 'cases': ['C-missing']}))
        bad = {**self.rule('R-0003'), 'enforcement': {'level': 'L9', 'guard': 'card.nope', 'hint': ''}}
        (self.live / 'R-0003.json').write_text(json.dumps(bad))
        problems = self.reg.lint()['problems']
        self.assertIn('R-0009.json: duplicate guard card.situation_line (also R-0002.json)', problems)
        self.assertIn('R-0009.json: case C-missing is not in cases.json', problems)
        self.assertTrue(any("unknown guard 'card.nope'" in p for p in problems))
        self.assertIn('R-0003.json: enforcement.level must be one of L0/L1/L2/L3/L4', problems)
        self.assertIn('R-0003.json: enforcement.hint is empty', problems)

    def test_title_follows_the_owner_language(self):
        rule = self.rule()
        self.assertEqual(self.reg.rule_problems(rule, 'R-0001.json'), [])
        self.assertEqual(self.make('zh-CN').rule_problems(rule), ['title must be one Chinese line'])
        self.assertEqual(self.make('zh-CN').rule_problems({**rule, 'title': '先说要决定什么'}), [])

    def test_rule_problems(self):
        rule = {**self.rule(), 'statement': 'One. Two. Three.', 'why': [], 'created': '01/01/2026',
                'status': 'retired', 'retired': None, 'extra': 1}
        problems = self.reg.rule_problems(rule, 'R-0002.json')
        for expected in ('file name must be R-0001.json', 'statement is longer than 2 sentences',
                         'why must list an incident or decision', 'created must be YYYY-MM-DD',
                         'a retired rule needs its retired date', "missing or extra fields: ['extra']"):
            self.assertIn(expected, problems)
        self.assertEqual(self.reg.rule_problems([]), ['not a JSON object'])

    def test_overview_and_show_text_in_both_languages(self):
        en = self.reg.show_text().splitlines()
        self.assertEqual(en[0], f'Rules (the shipped seed: {SEED})')
        self.assertEqual(en[1], 'R-0001 Lead with the decision · active')
        self.assertEqual(en[2], '  How it is checked: refused by code (card.decision_line): ask-owner')
        zh = self.make('zh-CN').overview()['rules'][0]
        self.assertEqual((zh['how'], zh['status']), ('代码拒绝（card.decision_line）：ask-owner', '生效中'))

    def test_install_validates_lints_and_logs(self):
        rule = {**self.rule('R-0004'), 'status': 'retired', 'retired': '2026-03-01'}
        path = self.tmp / 'R-0004.json'
        path.write_text(json.dumps(rule))
        result = self.reg.install(path, who='mira', now=100.0)
        self.assertEqual((result['id'], result['status'], result['replaced']), ('R-0004', 'retired', True))
        log = [json.loads(x) for x in (self.live / 'changelog.jsonl').read_text().splitlines()]
        self.assertEqual(log, [{'ts': 100.0, 'kind': 'registry_install', 'id': 'R-0004', 'status': 'retired',
                                'replaced': True, 'who': 'mira'}])
        clash = {**self.rule('R-0005'), 'id': 'R-0010'}   # a second rule for the same guard
        path.write_text(json.dumps(clash))
        with self.assertRaisesRegex(ValueError, 'registry_lint_failed_nothing_changed'):
            self.reg.install(path, who='mira')
        self.assertFalse((self.live / 'R-0010.json').exists())
        path.write_text(json.dumps({**clash, 'kind': 'wish'}))
        with self.assertRaisesRegex(ValueError, 'registry_rule_invalid: kind must be one of'):
            self.reg.install(path, who='mira')

    def test_each_registry_has_its_own_cache(self):
        a, b = self.make('en'), self.make('en')
        self.assertIsNot(a.load(), b.load())
        self.assertIs(a.load(), a.load())


if __name__ == '__main__':
    unittest.main()
