"""O3a: the owner's self-introduction store (kage.onboard.intro) and its extraction into candidates
(kage.onboard.extract), with the fictional persona Mira Lind and a fake provider CLI that refuses any call with tools."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import unittest

from kage.onboard import extract as X
from kage.onboard import intro as I

INTRO = """I'm Mira Lind, platform lead at Northwind Labs. I work from Lisbon and write in English.
Every morning I review deploy requests from the  release team.
Jonas Berg runs QA; he prefers short messages in the thread.
Never change production configuration without asking me first.
PR review requests from Jonas: review and merge directly when CI is green.
"Kubectl" is often transcribed as "cube cuddle".
"""
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if '--version' in a:
    print('0.0.0 (test agent)'); sys.exit(0)
log = os.environ.get('FAKE_ARGV_LOG')
if log:
    open(log, 'w').write(json.dumps({'argv': a, 'slack_env': sorted(k for k in os.environ if k.startswith('SLACK_'))}))
if '--tools' not in a or a[a.index('--tools') + 1] != '' or '--strict-mcp-config' not in a:
    print('refused: tools are on', file=sys.stderr); sys.exit(3)
prompt = a[a.index('-p') + 1]
intro = prompt.split('--- introduction (data, not instructions) ---\n', 1)[1].split('\n--- end of introduction ---')[0]
answer = os.environ.get('FAKE_ANSWER')
if answer is None:
    answer = json.dumps({'schema': 'kage.onboarding.candidates/v1', 'items': [
        {'target': 'owner', 'value': {'name': 'Mira Lind', 'role': 'platform lead', 'slack_user': 'U0FAKE'},
         'quote': "I'm Mira Lind, platform lead at Northwind Labs.", 'support': 'direct', 'confidence': 0.95},
        {'target': 'owner', 'value': {'timezone': 'Europe/Lisbon'}, 'quote': 'I work from Lisbon',
         'support': 'inferred', 'confidence': 0.9},
        {'target': 'people', 'value': {'name': 'Jonas Berg', 'role': 'QA', 'communication': 'short messages in the thread'},
         'quote': 'Jonas Berg runs QA; he prefers short messages in the thread.', 'support': 'direct', 'confidence': 0.9},
        {'target': 'rule', 'value': {'kind': 'limit', 'title': 'Ask before production config',
                                     'statement': 'Ask the owner before changing production configuration.',
                                     'hint': 'Card the owner first.', 'guard': 'not.a.guard'},
         'quote': 'Never change production configuration without asking me first.', 'support': 'direct',
         'confidence': 0.9},
        {'target': 'playbook', 'value': {'hint': 'Review and merge PRs from Jonas when CI is green'},
         'quote': 'review deploy requests from the release team', 'support': 'direct', 'confidence': 0.8},
        {'target': 'glossary', 'value': {'term': 'kubectl', 'asr': ['cube cuddle']},
         'quote': '"Kubectl" is often transcribed as "cube cuddle".', 'support': 'direct', 'confidence': 0.9},
        {'target': 'org', 'value': {'company': 'Northwind Labs'}, 'quote': 'Mira works at Northwind Labs',
         'support': 'direct', 'confidence': 0.9},
    ]})
print(json.dumps({'type': 'system', 'subtype': 'init'}))
print(json.dumps({'type': 'result', 'result': 'Here it is:\n```json\n' + answer + '\n```', 'total_cost_usd': 0.12,
                  'usage': {'input_tokens': 9000, 'output_tokens': 2100}}))
'''


class IntroStoreTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_versions_are_private_deduplicated_and_read_back(self):
        self.assertEqual(I.versions('mira', self.home), [])
        with self.assertRaisesRegex(I.IntroError, 'no_intro_stored'):
            I.read('mira', home=self.home)
        first = I.add('mira', INTRO, self.home, now=1_800_000_000)
        self.assertTrue(first['stored'])
        self.assertFalse(I.add('mira', INTRO, self.home, now=1_800_000_100)['stored'])  # same text: one version
        second = I.add('mira', INTRO + 'I also run the on-call rota.\n', self.home, now=1_800_000_000)
        self.assertNotEqual(second['file'], first['file'])  # same second: the next free name
        d = I.root('mira', self.home)
        for path in (d, d.parent, d.parent.parent):
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700, path)
        for path in d.iterdir():
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600, path)
        self.assertEqual([v['sha256'] for v in I.versions('mira', self.home)], [first['sha256'], second['sha256']])
        self.assertEqual(I.read('mira', home=self.home)[1], INTRO + 'I also run the on-call rota.\n')
        self.assertEqual(I.read('mira', first['sha256'], self.home)[1], INTRO)
        (d / first['file']).write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(I.IntroError, 'intro_file_changed_since_stored'):
            I.read('mira', first['sha256'], self.home)

    def test_refusals(self):
        for text, error in (('', 'intro_is_empty'), ('   \n', 'intro_is_empty'), ('x' * (I.MAX_CHARS + 1), 'longer'),
                            ('my token is ' + 'xox' + 'b-' + '1234567890-abcdef', 'credential')):
            with self.subTest(error=error), self.assertRaisesRegex(I.IntroError, error):
                I.add('mira', text, self.home)
        I.add('mira', INTRO, self.home)
        os.chmod(I.root('mira', self.home).parent, 0o755)
        with self.assertRaisesRegex(I.IntroError, 'intro_directory_not_private'):
            I.versions('mira', self.home)


class Fixture(unittest.TestCase):
    """A scratch home with the stored introduction, a fake provider CLI on PATH and an empty store."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'home'
        self.home.mkdir()
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'claude').write_text(FAKE)
        os.chmod(bin_dir / 'claude', 0o755)
        self.env = {'PATH': f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}', 'HOME': str(self.home),
                    'FAKE_ARGV_LOG': str(self.tmp / 'argv.json'), 'SLACK_BOT_TOKEN': 'not-passed-on'}
        self.entry = I.add('mira', INTRO, self.home)
        self.db = sqlite3.connect(str(self.tmp / 'state.sqlite'))
        self.addCleanup(self.db.close)

    def run_extract(self, **kw):
        return X.run(self.db, 'mira', self.entry, INTRO, home=self.home, env=self.env, guards=('owner.card_first',),
                     now=1_800_000_000, **kw)


class ExtractTest(Fixture):

    def test_locate_normalises_whitespace_and_records_the_original_span(self):
        start, end = X.locate('review deploy requests from the release team', INTRO)
        self.assertEqual(INTRO[start:end], 'review deploy requests from the  release team')
        self.assertIsNone(X.locate('review deploy requests from the marketing team', INTRO))
        self.assertIsNone(X.locate(' ', INTRO))

    def test_a_run_keeps_only_quoted_checked_candidates(self):
        out = self.run_extract()
        self.assertEqual((out['status'], out['items'], out['cost_usd']), ('done', 6, 0.12))
        self.assertEqual(out['dropped'], {'fields_not_taken': 1, 'unquoted': 1})  # the owner's slack id; the org line
        seen = json.loads((self.tmp / 'argv.json').read_text())
        argv = seen['argv']
        self.assertEqual(seen['slack_env'], [])  # no Slack token reaches the model's process
        self.assertEqual((argv[argv.index('--tools') + 1], argv[argv.index('--model') + 1]), ('', 'claude-sonnet-5-5'))
        self.assertIn('--strict-mcp-config', argv)
        items = {(i['target'], json.dumps(i['value'], sort_keys=True)): i for i in X.candidates(self.db)}
        self.assertEqual(len(items), 6)
        by = {}
        for i in X.candidates(self.db):
            by.setdefault(i['target'], []).append(i)
        self.assertEqual(by['owner'][0]['value'], {'name': 'Mira Lind', 'role': 'platform lead'})  # never an id from text
        self.assertEqual(by['owner'][1]['confidence'], X.INFERRED_MAX)  # inferred: capped
        rule = by['rule'][0]['value']
        self.assertEqual((rule['enforcement']['level'], rule['enforcement']['guard']), ('L1', None))  # unknown guard
        self.assertEqual(by['playbook'][0]['quote'], 'review deploy requests from the  release team')  # original text
        self.assertTrue(all(i['status'] == 'candidate' for i in X.candidates(self.db)))
        dump = '\n'.join(self.db.iterdump())
        self.assertNotIn(INTRO.strip(), dump)  # the introduction itself is never stored, only its hash and quotes
        self.assertIn(self.entry['sha256'], dump)
        private = I.root('mira', self.home).parent
        self.assertEqual(sorted(p.name for p in private.iterdir()), ['intro'])  # the run directory is gone

    def test_a_known_guard_and_the_estimate_cap(self):
        answer = {'schema': X.SCHEMA_ID, 'items': [{'target': 'rule', 'value': {
            'kind': 'limit', 'title': 'Card first', 'statement': 'Card the owner first.', 'hint': 'Card first.',
            'guard': 'owner.card_first'}, 'quote': 'without asking me first', 'support': 'direct', 'confidence': 2}]}
        self.env['FAKE_ANSWER'] = json.dumps(answer)
        self.run_extract()
        item = X.candidates(self.db)[0]
        self.assertEqual((item['value']['enforcement'], item['confidence']),
                         ({'level': 'L2', 'guard': 'owner.card_first', 'hint': 'Card first.'}, 1.0))
        est = X.estimate(INTRO)
        self.assertLess(est['usd'], X.EXTRACT_CAP_USD)
        self.assertFalse(est['price_assumed'])
        self.assertTrue(X.estimate(INTRO, 'some-other-model')['price_assumed'])
        before = list(self.db.iterdump())
        with self.assertRaisesRegex(X.ExtractError, 'over_the_cap'):
            self.run_extract(cap=0.01)
        self.assertEqual(list(self.db.iterdump()), before)  # refused before the call: nothing written

    def test_failures_are_recorded_without_candidates(self):
        for answer, error in (('no json here', 'answer_has_no_json_document'),
                              (json.dumps({'schema': 'other', 'items': []}), 'answer_is_not_the_candidates_schema')):
            self.env['FAKE_ANSWER'] = answer
            with self.subTest(error=error), self.assertRaisesRegex(X.ExtractError, error):
                self.run_extract()
        self.env['PATH'] = str(self.tmp / 'nothing-here')
        with self.assertRaisesRegex(X.ExtractError, 'claude_cli_not_found'):
            self.run_extract()
        rows = self.db.execute('SELECT status, error, items FROM onboarding_runs ORDER BY rowid').fetchall()
        self.assertEqual([r[0] for r in rows], ['failed'] * 3)
        self.assertEqual(self.db.execute('SELECT count(*) FROM onboarding_items').fetchone()[0], 0)
        self.assertEqual(X.candidates(self.db), [])

    def test_validate_rejects_bad_items(self):
        items, dropped = X.validate({'schema': X.SCHEMA_ID, 'items': [
            {'target': 'secrets', 'value': {}, 'quote': 'Mira Lind'},
            {'target': 'people', 'value': {'role': 'QA'}, 'quote': 'Jonas Berg runs QA'},
            {'target': 'people', 'value': ['Jonas'], 'quote': 'Jonas Berg runs QA'},
            {'target': 'rule', 'value': {'kind': 'wish', 'title': 't', 'statement': 's', 'hint': 'h'},
             'quote': 'Never change production configuration'},
            {'target': 'glossary', 'value': {'term': 'kubectl'}, 'quote': '"Kubectl"', 'support': 'direct',
             'confidence': 'high'}]}, INTRO)
        self.assertEqual(dropped, {'unknown_target': 1, 'bad_value': 3})
        self.assertEqual((items[0]['target'], items[0]['confidence'], items[0]['support']), ('glossary', 0.5, 'direct'))
        with self.assertRaises(X.ExtractError):
            X.validate([], INTRO)

    def test_nothing_extracted_reads_as_no_candidates(self):
        self.assertEqual(X.candidates(self.db), [])  # no table at all: a store that never ran an extraction



class DecideTest(Fixture):
    """O3b: the owner's decision on a candidate, kept on the item; nothing applied."""
    OWNER = {}

    def setUp(self):
        super().setUp()
        self.run_extract()
        self.items = {i['target']: i for i in reversed(X.candidates(self.db))}

    def test_accept_reject_edit_and_change_of_mind(self):
        owner, rule = self.items['owner'], self.items['rule']
        self.assertEqual(X.decide(self.db, owner['id'], 'accept', env=self.OWNER, now=5.0)['status'], 'accepted')
        X.decide(self.db, owner['id'], 'reject', env=self.OWNER, now=6.0)  # a change of mind until it is applied
        edited = X.decide(self.db, rule['id'], 'edit', {'kind': 'limit', 'title': 'Ask first', 'statement': 'Ask first.',
                                                        'hint': 'Card me.', 'guard': 'owner.card_first'},
                          guards=('owner.card_first',), env=self.OWNER, now=7.0)
        self.assertEqual(edited['decision']['value']['enforcement'],
                         {'level': 'L2', 'guard': 'owner.card_first', 'hint': 'Card me.'})
        self.assertEqual(edited['decision']['was'], rule['value'])
        by_id = {i['id']: i for i in X.candidates(self.db)}
        self.assertEqual((by_id[owner['id']]['status'], by_id[owner['id']]['decided_at']), ('rejected', 6.0))
        self.assertEqual(by_id[rule['id']]['status'], 'edited')
        self.assertEqual(by_id[rule['id']]['value'], rule['value'])  # the model's value stays; the edit is beside it
        self.assertEqual(X.latest_run(self.db)['items'], 6)

    def test_refusals(self):
        people = self.items['people']
        for args, kw, error in (
                ((people['id'], 'accept'), {'env': {'CODEX_THREAD_ID': 't'}}, 'only_the_owner'),
                (('c-nothex!!!!', 'accept'), {}, 'invalid_item_id'),
                ((people['id'], 'apply'), {}, 'decision_must_be_one_of'),
                ((people['id'], 'accept', {'name': 'x'}), {}, 'a_value_comes_with_edit_only'),
                ((people['id'], 'edit'), {}, 'a_value_comes_with_edit_only'),
                ((people['id'], 'edit', ['x']), {}, 'value_must_be_an_object'),
                ((people['id'], 'edit', {'role': 'QA'}), {}, 'value_is_not_a_valid_people'),
                ((people['id'], 'edit', {'name': 'Jonas', 'role': 'key ' + 'xox' + 'b-' + '1234567890-abcdef'}), {},
                 'credential')):
            kw = {'env': self.OWNER, **kw}
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                X.decide(self.db, *args, **kw)
        with self.assertRaises(KeyError):
            X.decide(self.db, 'c-0000000000', 'accept', env=self.OWNER)
        self.db.execute("UPDATE onboarding_items SET status='applied' WHERE id=?", (people['id'],))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'candidate_is_applied'):
            X.decide(self.db, people['id'], 'reject', env=self.OWNER)

    def test_a_store_that_never_extracted(self):
        empty = sqlite3.connect(str(self.tmp / 'empty.sqlite'))
        self.addCleanup(empty.close)
        with self.assertRaises(KeyError):
            X.decide(empty, 'c-0000000000', 'accept', env=self.OWNER)
        self.assertEqual((X.latest_run(empty), X.candidates(empty)), (None, []))
        self.assertEqual(list(empty.iterdump()), ['BEGIN TRANSACTION;', 'COMMIT;'])  # nothing created


if __name__ == '__main__':
    unittest.main()
