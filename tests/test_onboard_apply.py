"""O3c: verification of candidates (read-only Slack reads) and applying the owner's confirmed items
(kage.onboard.apply): a generated deployment gets its owner.json / people / draft rules written, an adopted one only
the owner's private confirmed profile and a proposal."""
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import os

from kage.core import mode as M
from kage.onboard import apply as A
from kage.onboard import extract as X
from kage.onboard import intro as I
from tests.test_onboard_intro import Fixture

OWNER = {}


def slack(calls, *, tz='Europe/Lisbon', members=None):
    def read(method, params):
        calls.append(method)
        if method == 'users.info':
            return {'user': {'id': params['user'], 'real_name': 'Mira Lind', 'tz': tz, 'profile': {'display_name': 'mira'}}}
        if method == 'users.list':
            return {'members': members if members is not None else [
                {'id': 'U0000JONAS', 'real_name': 'Jonas Berg'}, {'id': 'U0000BOT01', 'real_name': 'Jonas Berg',
                                                                  'is_bot': True}]}
        raise AssertionError(method)
    return read


class Registry:
    def __init__(self, live):
        self.live = live

    def live_dir(self):
        return self.live

    def rule_problems(self, rule, name=None):
        return ['title is empty'] * (not rule['title'])


class ApplyTest(Fixture):
    def setUp(self):
        super().setUp()
        self.run_extract()
        self.items = {}
        for i in X.candidates(self.db):
            self.items.setdefault(i['target'], []).append(i)
        self.deploy = self.tmp / 'deployment'
        (self.deploy / 'deploy').mkdir(parents=True)
        (self.deploy / 'deploy/owner.json').write_text(json.dumps(
            {'schema': 'kage.owner/v1', 'owner': {'key': 'mira', 'language': 'en', 'name': 'Mira', 'short_name': 'Mira',
                                                  'timezone': 'UTC', 'timezone_fallback_offset': None}}))
        for args in (['init', '-q'], ['add', '-A'], ['-c', 'user.name=t', '-c', 'user.email=t@localhost', 'commit', '-q',
                                                     '-m', 'init']):
            subprocess.run(['git', '-C', str(self.deploy), *args], check=True, capture_output=True)

    def apply(self, **kw):
        return A.apply(self.db, 'mira', deployment_dir=self.deploy / 'deploy', people_path=self.tmp / 'people.json',
                       registry=Registry(self.tmp / 'rules'), home=self.home, env=OWNER, now=1_800_000_000, **kw)

    def test_verification_reads_slack_and_resolves_ids_by_code(self):
        calls = []
        out = A.verify(self.db, slack(calls), 'U0000MIRA1', env=OWNER, now=5.0)
        self.assertEqual(out['results'], {'verified': 3, 'unverifiable': 3})  # 2 owner items, Jonas; 3 other targets
        self.assertEqual(sorted(calls), ['users.info', 'users.list'])  # one read each, whatever the item count
        checked = A.results(self.db)
        jonas = self.items['people'][0]['id']
        self.assertEqual((checked[jonas]['result'], checked[jonas]['slack_id']), ('verified', 'U0000JONAS'))  # not the bot
        A.verify(self.db, slack([], tz='Asia/Shanghai', members=[]), 'U0000MIRA1', env=OWNER, now=6.0)
        checked = A.results(self.db)
        tz_item = next(i['id'] for i in self.items['owner'] if 'timezone' in i['value'])
        self.assertEqual((checked[tz_item]['result'], checked[tz_item]['detail']),
                         ('contradicted', 'slack says timezone=Asia/Shanghai'))
        self.assertEqual(checked[jonas]['result'], 'not_found')
        with self.assertRaisesRegex(ValueError, 'only_the_owner'):
            A.verify(self.db, slack([]), 'U0000MIRA1', env={'CLAUDECODE': '1'})

    def test_only_verified_facts_are_accepted_in_bulk(self):
        A.verify(self.db, slack([]), 'U0000MIRA1', env=OWNER, now=5.0)
        out = A.accept_verified(self.db, env=OWNER, now=6.0)
        self.assertEqual(out['accepted'], 3)
        states = {i['target']: i['status'] for i in X.candidates(self.db) if i['target'] in ('rule', 'glossary')}
        self.assertEqual(states, {'rule': 'candidate', 'glossary': 'candidate'})  # rules are always looked at one by one

    def test_a_generated_deployment_gets_its_files_written(self):
        M.write(self.db, 'installed', 'init', now=1.0)  # kage init made this store
        A.verify(self.db, slack([]), 'U0000MIRA1', env=OWNER, now=5.0)
        A.accept_verified(self.db, env=OWNER, now=6.0)
        rule = self.items['rule'][0]
        X.decide(self.db, rule['id'], 'accept', env=OWNER, now=7.0)
        out = self.apply()
        self.assertEqual((out['applied'], out['deployment'], out['skipped']), (4, 'generated', []))
        owner = json.loads((self.deploy / 'deploy/owner.json').read_text())['owner']
        self.assertEqual((owner['name'], owner['timezone'], owner['key']), ('Mira Lind', 'Europe/Lisbon', 'mira'))
        self.assertEqual(out['written']['owner_fields'], ['name', 'timezone'])
        log = subprocess.run(['git', '-C', str(self.deploy), 'log', '--oneline'], capture_output=True, text=True).stdout
        self.assertIn('onboarding: owner fields confirmed by the owner', log)
        self.assertEqual(out['written']['commit'][:7], log.split()[0][:7])
        people = json.loads((self.tmp / 'people.json').read_text())['people']
        self.assertEqual(people, [{'name': 'Jonas Berg', 'role': 'QA', 'communication': 'short messages in the thread',
                                   'identities': [{'type': 'slack', 'value': 'U0000JONAS'}]}])
        written = json.loads((self.tmp / 'rules/R-0001.json').read_text())
        self.assertEqual((written['status'], written['enforcement']['level'], written['cases']), ('draft', 'L1', []))
        self.assertIn('Never change production configuration', written['why'][0])
        self.assertTrue(all(i['status'] == 'applied' for i in X.candidates(self.db) if i['id'] in
                            {x['id'] for x in json.loads(Path(out['confirmed']).read_text())['items']}))
        self.assertEqual(stat.S_IMODE(os.stat(out['confirmed']).st_mode), 0o600)
        with self.assertRaisesRegex(ValueError, 'candidate_is_applied'):
            X.decide(self.db, rule['id'], 'reject', env=OWNER)
        self.assertEqual(self.apply(), {'applied': 0})  # applied items are final

    def test_an_adopted_deployment_is_never_written(self):
        """An adopted deployment: no mode row. Only the owner's private files appear."""
        A.verify(self.db, slack([]), 'U0000MIRA1', env=OWNER, now=5.0)
        A.accept_verified(self.db, env=OWNER, now=6.0)
        rule = self.items['rule'][0]
        X.decide(self.db, rule['id'], 'edit', {'kind': 'limit', 'title': 'Ask first', 'statement': 'Ask first.',
                                               'hint': 'Card me.'}, env=OWNER, now=7.0)
        before = (self.deploy / 'deploy/owner.json').read_bytes()
        out = self.apply()
        self.assertEqual((out['applied'], out['deployment']), (4, 'adopted'))
        self.assertEqual((self.deploy / 'deploy/owner.json').read_bytes(), before)
        self.assertFalse((self.tmp / 'people.json').exists())
        self.assertFalse((self.tmp / 'rules').exists())
        proposal = json.loads(Path(out['proposal']).read_text())
        self.assertEqual(proposal['owner_fields'], {'name': 'Mira Lind', 'timezone': 'Europe/Lisbon'})
        self.assertEqual((proposal['rules'][0]['title'], proposal['rules'][0]['status']), ('Ask first', 'draft'))
        private = I.root('mira', self.home).parent
        self.assertEqual(sorted(p.name for p in private.iterdir()), ['confirmed.json', 'intro', 'proposal.json'])

    def test_an_invalid_rule_is_skipped_and_nothing_without_decisions(self):
        self.assertEqual(self.apply(), {'applied': 0})
        M.write(self.db, 'installed', 'init', now=1.0)
        rule = self.items['rule'][0]
        self.db.execute('UPDATE onboarding_items SET status=?, decision=? WHERE id=?', ('edited', json.dumps(
            {'decision': 'edit', 'value': {**rule['value'], 'title': ''}}), rule['id']))
        self.db.commit()
        out = self.apply()
        self.assertEqual((out['written']['rules'], out['skipped']), ([], [{'id': rule['id'], 'why': 'title is empty'}]))
        with self.assertRaisesRegex(ValueError, 'only_the_owner'):
            A.apply(self.db, 'mira', deployment_dir=self.deploy, env={'CODEX_THREAD_ID': 't'})

    def test_a_store_that_never_extracted(self):
        empty = sqlite3.connect(str(self.tmp / 'empty.sqlite'))
        self.addCleanup(empty.close)
        self.assertEqual(A.results(empty), {})
        self.assertFalse(A.generated(empty))
        self.assertEqual(A.apply(empty, 'mira', deployment_dir=self.deploy, home=self.home, env=OWNER), {'applied': 0})
