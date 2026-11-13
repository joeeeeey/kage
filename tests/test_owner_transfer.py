"""0.79.3 (F30): `owner-transfer` hands an existing deployment to another person of its workspace (DM-code proof by that
person only, every owner record rewritten with a backup, put back on a failure); F29: `init --apply` keeps the decisions
config and owner.json's `assistant`."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import types
import unittest
from unittest import mock

from kage import app, profile
from kage.core import approvers as A
from kage.core import authority
from kage.onboard import answers, dispatch_files, identity, init, transfer
from kage.ports import secrets
from tests.test_init import BOT_TOKEN, BOT_USER, OWNER, TEAM, Base, FakeSlack, raw_answers

NEW, OTHER, GONE = 'U0000TEST02', 'U0000TEST03', 'U0000GONE01'
CHANNEL = 'G0000TEST01'
START = 1800000000.0


class Slack(FakeSlack):
    users = {NEW: {'id': NEW, 'team_id': TEAM, 'profile': {'real_name': 'Lin Example'}},
             OWNER: {'id': OWNER, 'team_id': TEAM, 'profile': {'real_name': 'Mira Lind'}},
             GONE: {'id': GONE, 'team_id': TEAM, 'deleted': True},
             'U0000APPBOT': {'id': 'U0000APPBOT', 'team_id': TEAM, 'is_bot': True}}

    def __call__(self, method, token, params=None):
        if method == 'users.info':
            user = self.users.get(params['user'])
            return ({'ok': True, 'user': user} if user else {'ok': False, 'error': 'user_not_found'}), {}
        if method == 'conversations.list':
            payload, headers = super().__call__(method, token, params)
            payload['channels'].append({'id': 'D0000TEST02', 'user': NEW})
            return payload, headers
        return super().__call__(method, token, params)


def rules_spec(version=7, approvers=(OWNER, NEW)):
    return {'category': 'access', 'version': version, 'defaults': {
        'owner': OWNER, 'approvers': list(approvers), 'approver_names_zh': {OWNER: 'Mira', NEW: 'Lin'}, 'hr': []},
        'rules': [{'id': 'r1', 'bounds': {'approvers': list(approvers)}}, {'id': 'r2', 'bounds': {'max_days': 7}}]}


class Rules:
    def __init__(self):
        self.active, self.installed, self.fail = rules_spec(), [], None

    def load_active(self, category='access'):
        return copy.deepcopy(self.active), {'version': self.active['version'], 'dir': '/nonexistent-rules',
                                            'file': f"access-v{self.active['version']}.json"}

    def validate_spec(self, s):
        if s['defaults']['owner'] not in s['defaults']['approvers']:
            raise ValueError('invalid_rules_spec: owner must be one of the approvers')
        if len(s['defaults']['approvers']) > 5:
            raise ValueError('invalid_rules_spec: at most 5 approvers')

    def install_spec(self, s, directory=None, *, who, note=None, now=None):
        if self.fail:
            raise ValueError(self.fail)
        self.installed.append((copy.deepcopy(s), who, note))
        self.active = copy.deepcopy(s)
        return {'ok': True, 'file': f"access-v{s['version']}.json"}


class TransferTest(Base):
    def setUp(self):
        super().setUp()
        self.records()
        self.raw = raw_answers()
        self.raw['slack']['cards'] = CHANNEL
        self.init(self.raw)
        self.state = init.state_dir('mira', self.home)
        self.decisions = self.home / '.config/kage/mira/decisions.json'
        self.decisions.write_text(json.dumps({'decision_channel': CHANNEL, 'approvers': [NEW],
                                              'owners': [OWNER, OTHER, NEW],
                                              'owner_names': {OWNER: 'Mira Lind', OTHER: 'Sam Example'}}))
        self.slack, self.rules, self.now, self.said = Slack(), Rules(), START, []
        self.sender = NEW

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def say(self, item):
        self.said.append(item)
        if isinstance(item, dict) and 'owner_code' in item and self.sender:
            self.slack.dms.append({'user': self.sender, 'text': item['owner_code'], 'ts': f'{self.now + 3:.6f}'})

    def transfer(self, **kw):
        auth = authority.Authority(owner_user=NEW, owner_key='mira', user_re=re.compile(r'[UW][A-Z0-9]{2,}'),
                                   channel_re=re.compile(r'[CG][A-Z0-9]{2,}'), dm_re=re.compile(r'D[A-Z0-9]{2,}'))
        args = dict(deploy_dir=self.target / 'deploy', state_dir=self.state, to=NEW, bot_token=BOT_TOKEN,
                    decisions_path=self.decisions, rules=self.rules, authority=auth, call=self.slack, clock=self.clock,
                    sleep=self.sleep, say=self.say)
        args.update(kw)
        return transfer.Transfer(**args)

    def snapshot(self):
        files = [self.target / 'deploy/owner.json', self.target / 'deploy/org.json', self.target / 'README.md',
                 self.state / identity.RECORD, self.decisions]
        return {p: p.read_bytes() for p in files}

    def test_the_new_owner_proves_it_and_every_owner_record_moves(self):
        t = self.transfer()
        plan = t.plan()
        self.assertEqual(self.said, [])  # the plan makes no code and writes nothing
        audit = t.apply(plan)
        self.assertEqual(self.said[0]['to'], NEW)
        self.assertRegex(self.said[0]['owner_code'], r'^KAGE-[A-Z2-9]{5}-[A-Z2-9]{5}$')
        d = profile.load(self.target / 'deploy')
        self.assertEqual((d.owner.slack_user, d.owner.name, d.owner.short_name, d.owner.key), (NEW, 'Lin Example', 'Lin', 'mira'))
        self.assertEqual((d.owner.assistant_slack_bot_user, d.owner.slack_workspace), (BOT_USER, TEAM))
        self.assertEqual(d.org.name, 'Lin Example')
        record = identity.load(self.state)
        self.assertEqual((record['user_id'], record['method'], record['name'], record['transferred_from']),
                         (NEW, 'dm_code', 'Lin Example', OWNER))
        self.assertEqual(identity.status(d.owner, self.state), (True, 'verified (dm_code)'))
        self.assertFalse((self.state / identity.CHALLENGE).exists())
        dec = json.loads(self.decisions.read_text())
        self.assertEqual((dec['approvers'], dec['owners'], dec['decision_channel']), ([NEW], [NEW, OTHER], CHANNEL))
        self.assertEqual(dec['owner_names'], {OTHER: 'Sam Example', NEW: 'Lin Example'})
        spec, who, _note = self.rules.installed[-1]
        self.assertEqual((spec['version'], spec['defaults']['owner'], spec['defaults']['approvers']), (8, NEW, [NEW]))
        self.assertEqual(spec['rules'][0]['bounds']['approvers'], [NEW])
        self.assertEqual(spec['defaults']['approver_names_zh'], {NEW: 'Lin Example'})
        self.assertEqual(who, f'owner-transfer:{NEW}')
        change = json.loads(A.log_path(self.decisions).read_text().splitlines()[-1])
        self.assertEqual(change['deployment_owner'], {'old': OWNER, 'new': NEW})
        self.assertEqual((change['owners_old'], change['owners_new'], change['source']),
                         ([OWNER, OTHER, NEW], [NEW, OTHER], 'owner-transfer'))
        line = json.loads((self.state / transfer.AUDIT).read_text().splitlines()[-1])
        self.assertEqual((line['old'], line['new'], line['method'], line['rules']), (OWNER, NEW, 'dm_code', 'access-v8.json'))
        backup = Path(audit['backup'])
        manifest = json.loads((backup / 'manifest.json').read_text())
        saved = {Path(e['path']).name: json.loads((backup / e['copy']).read_text()) for e in manifest
                 if e['copy'] and e['path'].endswith('.json')}
        self.assertEqual(saved['owner.json']['identities']['slack']['user'], OWNER)
        self.assertEqual(saved[identity.RECORD]['user_id'], OWNER)
        self.assertEqual(saved['decisions.json']['owners'], [OWNER, OTHER, NEW])
        for p in [backup, *backup.iterdir()]:
            self.assertEqual(p.stat().st_mode & 0o077, 0, p)
        self.assertIn('Lin', (self.target / 'SKILL.md').read_text())
        if shutil.which('git'):
            log = subprocess.run(['git', '-C', str(self.target), 'log', '--format=%s'], capture_output=True, text=True).stdout
            self.assertTrue(log.startswith(f'kage owner-transfer: {OWNER} -> {NEW}'), log)
            self.assertEqual(audit['git'], subprocess.run(['git', '-C', str(self.target), 'rev-parse', '--short', 'HEAD'],
                                                          capture_output=True, text=True).stdout.strip())
            dirty = subprocess.run(['git', '-C', str(self.target), 'status', '--porcelain'], capture_output=True, text=True)
            self.assertEqual(dirty.stdout, '')
        # F29 + the record's names: the next `init --apply` changes none of it
        report = self.init(self.raw, apply=True)
        changed = [s['path'] for s in report['steps'] if s['action'] != 'same']
        self.assertEqual(changed, [])
        self.assertEqual(json.loads(self.decisions.read_text()), dec)
        text = A.Notices('en').text(types.SimpleNamespace(intake=types.SimpleNamespace(person_name=lambda rt, db, u: None)),
                                    None, change)
        self.assertIn(f'the deployment owner is now Lin Example (was {OWNER})', text)

    def test_keep_old_owner_adds_the_new_one(self):
        self.transfer(keep_old_owner=True).apply(self.transfer(keep_old_owner=True).plan())
        dec = json.loads(self.decisions.read_text())
        self.assertEqual((dec['approvers'], dec['owners']), ([NEW], [OWNER, NEW, OTHER]))
        self.assertEqual(dec['owner_names'][OWNER], 'Mira Lind')
        spec = self.rules.installed[-1][0]
        self.assertEqual((spec['defaults']['owner'], spec['defaults']['approvers']), (NEW, [OWNER, NEW]))

    def test_a_code_from_anyone_else_changes_nothing(self):
        before = self.snapshot()
        self.sender = OWNER
        t = self.transfer()
        with self.assertRaises(transfer.TransferRefused) as cm:
            t.apply(t.plan())
        self.assertIn('identity_code_sent_by_another_person', str(cm.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.rules.installed, [])
        self.assertFalse((self.state / identity.CHALLENGE).exists())
        self.assertFalse(A.log_path(self.decisions).exists())

    def test_no_code_within_the_wait_changes_nothing(self):
        before = self.snapshot()
        self.sender = None
        t = self.transfer(timeout=60)
        with self.assertRaises(transfer.TransferRefused) as cm:
            t.apply(t.plan())
        self.assertIn('new_owner_not_verified: identity_code_not_found', str(cm.exception))
        self.assertGreater(self.now - START, 60)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.state / identity.CHALLENGE).exists())

    def test_a_failure_after_the_proof_puts_every_file_back(self):
        before = self.snapshot()
        self.rules.fail = 'rules_version_exists_with_different_content'
        t = self.transfer()
        with self.assertRaises(transfer.TransferRefused) as cm:
            t.apply(t.plan())
        self.assertIn('owner_transfer_failed_and_restored', str(cm.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(identity.load(self.state)['user_id'], OWNER)

    def test_refusals_before_any_code(self):
        cases = {OWNER: 'already_the_owner', BOT_USER: 'the_new_owner_is_the_bot', GONE: 'owner_account_is_deactivated',
                 'U0000APPBOT': 'owner_must_be_a_person_not_a_bot', 'U0000NOBODY': 'owner_not_found_in_slack',
                 'not-an-id': 'to_must_be_a_slack_user_id'}
        for to, code in cases.items():
            with self.subTest(to=to), self.assertRaises(transfer.TransferRefused) as cm:
                self.transfer(to=to).plan()
            self.assertIn(code, str(cm.exception))
        with self.assertRaises(transfer.TransferRefused) as cm:
            self.transfer(bot_token=None).plan()
        self.assertIn('bot_token_not_in_the_secrets_store', str(cm.exception))
        self.rules.active['defaults']['hr'] = [NEW]
        with self.assertRaises(transfer.TransferRefused) as cm:
            self.transfer().plan()
        self.assertIn('hr_contact', str(cm.exception))
        self.assertEqual(self.said, [])

    def test_an_inconsistent_identity_refuses(self):
        identity.prove_by_user_token(self.state, {'team_id': TEAM, 'user_id': OTHER})
        with self.assertRaises(transfer.TransferRefused) as cm:
            self.transfer().plan()
        self.assertIn('owner_identity_not_consistent', str(cm.exception))

    def test_a_dm_only_deployment_moves_without_a_decisions_config(self):
        self.decisions.unlink()
        t = self.transfer()
        t.apply(t.plan())
        self.assertFalse(self.decisions.exists())
        self.assertEqual(profile.load(self.target / 'deploy').owner.slack_user, NEW)


class SwapTest(unittest.TestCase):
    def test_swap(self):
        self.assertEqual(transfer.swap(['a', 'b'], 'a', 'n'), ['n', 'b'])
        self.assertEqual(transfer.swap(['a', 'n'], 'a', 'n'), ['n'])
        self.assertEqual(transfer.swap(['b', 'a'], 'a', 'n', keep=True), ['b', 'a', 'n'])
        self.assertEqual(transfer.swap(['b'], 'a', 'n'), ['b'])


class InitKeepsTest(Base):
    """F29: a re-run keeps the decisions config (owners, approvers, names) and owner.json's re-pinned assistant."""

    def test_rerun_keeps_decisions_and_assistant(self):
        self.records()
        raw = raw_answers()
        raw['slack']['cards'] = CHANNEL
        self.init(raw)
        path = self.home / '.config/kage/mira/decisions.json'
        kept = {'decision_channel': CHANNEL, 'approvers': [OTHER], 'owners': [OWNER, OTHER],
                'owner_names': {OTHER: 'Sam Example'}}
        path.write_text(json.dumps(kept, indent=2) + '\n')
        owner_file = self.target / 'deploy/owner.json'
        raw_owner = json.loads(owner_file.read_text())
        raw_owner['assistant'] = {'display': 'Team agent', 'slack': {'bot_user': 'U0000BOT02', 'app': 'A0000TEST02'}}
        owner_file.write_text(json.dumps(raw_owner, indent=2, ensure_ascii=False) + '\n')
        report = self.init(raw, apply=True)
        self.assertEqual(json.loads(path.read_text()), kept)
        self.assertEqual(json.loads(owner_file.read_text())['assistant'], raw_owner['assistant'])
        self.assertNotIn(str(path), [s['path'] for s in report['steps'] if s['action'] == 'change'])
        raw['slack']['cards'] = 'G0000TEST09'  # the channel is still init's
        self.init(raw, apply=True)
        self.assertEqual(json.loads(path.read_text()), {**kept, 'decision_channel': 'G0000TEST09'})
        path.write_text(json.dumps({'decision_channel': CHANNEL, 'approvers': []}))
        self.init(raw, apply=True)
        self.assertEqual(json.loads(path.read_text())['approvers'], [OWNER])


class AssembledTest(Base):
    """main() on the assembled runtime of a generated deployment: the real rule engine, decisions config and checks."""

    def test_main_on_the_assembled_runtime(self):
        self.records()
        raw = raw_answers()
        raw['slack']['cards'] = CHANNEL
        self.init(raw)
        patch = mock.patch.dict(os.environ, {'HOME': str(self.home), 'KAGE_VERSIONS_DIR': str(self.release.parent)})
        patch.start()
        self.addCleanup(patch.stop)
        assembly = app.App(self.target / 'deploy')
        runtime = assembly.assemble()
        rt = runtime['RUNTIME']
        spec = dispatch_files.example_spec('mira', OWNER)
        spec['defaults']['approvers'] = [OWNER, OTHER]
        rt.rules.install_spec(spec, who='test')
        decisions = rt.buttons.config_path()
        self.assertEqual(decisions, self.home / '.config/kage/mira/decisions.json')
        decisions.write_text(json.dumps({'decision_channel': CHANNEL, 'approvers': [OTHER], 'owners': [OWNER, OTHER]}))
        store = secrets.MemoryStore('mira')
        store.put(secrets.SLACK_BOT, BOT_TOKEN)
        slack, clock = Slack(), [START]
        original = identity.new_challenge

        def challenge(state_dir, *, clock=None):
            code = original(state_dir, clock=clock)
            slack.dms.append({'user': NEW, 'text': code, 'ts': f'{clock() + 2:.6f}'})
            return code
        kw = dict(call=slack, store=store, sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0])
        with mock.patch('sys.stdout') as out:
            self.assertEqual(transfer.main(assembly, runtime, ['--to', NEW, '--dry-run', '--json'], **kw), 0)
        first = json.loads(out.write.call_args_list[0][0][0])
        self.assertEqual((first['dry_run'], first['new'], first['rules_version']), (True, NEW, 2))
        self.assertIn(str(decisions), first['files'])
        self.assertFalse((init.state_dir('mira', self.home) / identity.CHALLENGE).exists())
        with mock.patch.object(identity, 'new_challenge', challenge), mock.patch('sys.stdout') as out:
            self.assertEqual(transfer.main(assembly, runtime, ['--to', NEW, '--json'], **kw), 0)
        lines = [json.loads(c[0][0]) for c in out.write.call_args_list if c[0][0].strip()]
        self.assertIn('owner_code', lines[0])
        self.assertTrue(lines[-1]['done'])
        active, info = rt.rules.load_active('access')
        self.assertEqual((info['version'], active['defaults']['owner'], active['defaults']['approvers']),
                         (2, NEW, [NEW, OTHER]))
        self.assertEqual(json.loads(decisions.read_text())['owners'], [NEW, OTHER])
        self.assertEqual(profile.load(self.target / 'deploy').owner.slack_user, NEW)
        fresh = app.App(self.target / 'deploy')
        self.assertEqual(fresh.assemble()['RUNTIME'].buttons.load_config()['owners'], [NEW, OTHER])


class ProgramTest(Base):
    """The deployment's program reaches the command (assembled runtime) and refuses agents and dispatched runs."""

    def run_program(self, *args, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('SLACK_', 'KAGE_')) and '_DISPATCH_' not in k
               and 'RUN_CONTEXT' not in k and k not in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID',
                                                         'PYTHONPATH')}
        env.update({'HOME': str(self.home), 'KAGE_NO_REAL_SLACK': '1', 'KAGE_NO_AUTOSTART': '1',
                    'KAGE_SECRETS_BACKEND': 'file', 'KAGE_VERSIONS_DIR': str(self.release.parent),
                    'PYTHONDONTWRITEBYTECODE': '1', **extra})
        return subprocess.run([sys.executable, str(self.target / 'scripts/kage.py'), 'owner-transfer', *args],
                              capture_output=True, text=True, env=env, timeout=120, stdin=subprocess.DEVNULL,
                              cwd=str(self.tmp))

    def test_program(self):
        self.records()
        self.init()
        out = self.run_program('--to', NEW, '--dry-run')
        self.assertEqual(out.returncode, 1, out.stderr[-2000:])
        self.assertEqual(json.loads(out.stderr.strip().splitlines()[-1]),
                         {'error': 'TransferRefused', 'detail': 'bot_token_not_in_the_secrets_store'})
        out = self.run_program('--to', NEW, CLAUDECODE='1')
        self.assertEqual(out.returncode, 1)
        self.assertIn('never_in_an_agent_session_or_a_dispatched_run', out.stderr)
        out = self.run_program('--help')
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn('--keep-old-owner', out.stdout)
        self.assertEqual(profile.load(self.target / 'deploy').owner.slack_user, OWNER)


if __name__ == '__main__':
    unittest.main()
