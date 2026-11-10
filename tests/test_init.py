"""O1c4: `kage init` — the answers, the verified records, the generated deployment, re-runs (diff / --apply), refusals,
adopt, the wizard with a fake Slack and a fake terminal, and the deployment's program on a verified release."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import kage
from kage import app, profile
from kage.onboard import answers, identity, init, manifest, tokens, wizard
from kage.ports import secrets

KAGE_ROOT = Path(kage.__file__).resolve().parents[1]
TEAM, OWNER, BOT_USER, BOT_ID, APP_ID = 'T0000TEST01', 'U0000TEST01', 'U0000BOT01', 'B0000TEST01', 'A0000TEST01'
URL = 'https://northwind-example.slack.com'
BOT_TOKEN, APP_TOKEN, USER_TOKEN = 'xoxb-' + '0000-INIT', 'xapp-' + '1-A0000INIT-0000-abc', 'xoxp-' + '0000-INIT'
THIN_TOKEN = 'xoxb-' + '0000-THIN'  # F7: a bot of an app not made from the manifest


def make_release(versions, commit='c0ffee' + '0' * 34):
    """A verified release of the working tree's Kage under `versions` (BUNDLE.json, MANIFEST.sha256, kage/)."""
    rel = Path(versions) / f'{kage.__version__}-{commit[:12]}'
    shutil.copytree(KAGE_ROOT / 'kage', rel / 'kage', ignore=shutil.ignore_patterns('__pycache__'))
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(rel).as_posix()}"
             for p in sorted((rel / 'kage').rglob('*')) if p.is_file()]
    manifest_bytes = ('\n'.join(lines) + '\n').encode()
    (rel / 'MANIFEST.sha256').write_bytes(manifest_bytes)
    (rel / 'BUNDLE.json').write_text(json.dumps({'name': 'kage', 'version': kage.__version__, 'commit': commit,
                                                 'ref': 'test', 'tree_sha256': hashlib.sha256(manifest_bytes).hexdigest()}))
    return rel


def raw_answers(**over):
    raw = {'schema': answers.SCHEMA,
           'owner': {'name': 'Mira Lind', 'key': 'mira', 'language': 'en', 'timezone': 'Europe/Berlin'},
           'slack': {'voice': 'assistant', 'cards': 'dm',
                     'secrets': {'slack-bot-token': 'kage-secret://mira/slack-bot-token',
                                 'slack-app-token': 'kage-secret://mira/slack-app-token'}}}
    raw.update(over)
    return raw


class FakeSlack:
    def __init__(self):
        self.dms, self.calls = [], []

    def __call__(self, method, token, params=None):
        self.calls.append(method)
        if method == 'auth.test' and token in (BOT_TOKEN, THIN_TOKEN):
            scopes = manifest.BOT_SCOPES if token == BOT_TOKEN else ('chat:write', 'im:history')
            return ({'ok': True, 'team_id': TEAM, 'team': 'Northwind', 'user_id': BOT_USER, 'bot_id': BOT_ID,
                     'url': URL + '/'}, {'x-oauth-scopes': ','.join(scopes)})
        if method == 'auth.test' and token == USER_TOKEN:
            return {'ok': True, 'team_id': TEAM, 'user_id': OWNER, 'url': URL + '/'}, {}
        if method == 'auth.test':
            return {'ok': False, 'error': 'invalid_auth'}, {}
        if method == 'bots.info' and token == BOT_TOKEN:
            return {'ok': True, 'bot': {'id': params['bot'], 'app_id': APP_ID, 'user_id': BOT_USER}}, {}
        if method == 'apps.connections.open' and token == APP_TOKEN:
            return {'ok': True, 'url': 'wss://example.invalid/socket'}, {}
        if method == 'conversations.list':  # F5: as Slack lists a new bot's DMs, Slackbot's first
            return {'ok': True, 'channels': [{'id': 'D0000SLACKBOT', 'user': 'USLACKBOT'},
                                             {'id': 'D0000GONE01', 'user': 'U0000GONE01', 'is_user_deleted': True},
                                             {'id': 'D0000HIDDEN', 'user': 'U0000HIDDEN'},
                                             {'id': 'D0000TEST01', 'user': OWNER}]}, {}
        if method == 'conversations.history':
            if params['channel'] in ('D0000SLACKBOT', 'D0000GONE01'):
                return {'ok': False, 'error': 'channel_not_found'}, {}
            if params['channel'] == 'D0000HIDDEN':
                return {'ok': False, 'error': 'not_in_channel'}, {}
            return {'ok': True, 'messages': [m for m in self.dms if float(m['ts']) > float(params['oldest'])]}, {}
        raise AssertionError(method)


class ScriptIO:
    def __init__(self, replies, secrets_=(), on_say=None):
        self.replies, self.secrets, self.out, self.on_say = list(replies), list(secrets_), [], on_say

    def ask(self, prompt, default=''):
        value = self.replies.pop(0) if self.replies else ''
        return value or default

    def secret(self, prompt):
        return self.secrets.pop(0)

    def say(self, text=''):
        self.out.append(text)
        if self.on_say:
            self.on_say(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.release = make_release(self.tmp / 'versions')
        self.env = mock.patch.dict(os.environ, {'KAGE_SECRETS_BACKEND': 'file', 'KAGE_NO_REAL_SLACK': '1',
                                                    'KAGE_NO_AUTOSTART': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('KAGE_LOCK', None)

    def assert_no_supervisor(self):
        """No process of this test's deployment is left running (a CLI command must never start a daemon here)."""
        ps = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout
        left = [line for line in ps.splitlines() if str(self.tmp.resolve()) in line or str(self.tmp) in line]
        self.assertEqual(left, [])

    def records(self, key='mira', team=TEAM, user=OWNER):
        state = init.state_dir(key, self.home)
        init._private_mkdir(state)
        bot = {'team_id': team, 'url': URL + '/', 'bot_user_id': BOT_USER, 'bot_id': BOT_ID}
        identity.record_bot(state, bot, APP_ID)
        identity.prove_by_user_token(state, {'team_id': team, 'user_id': user})
        return state

    def init(self, raw=None, **kw):
        kw.setdefault('release', self.release)
        return init.run(answers.validate(raw or raw_answers()), home=self.home, **kw)

    @property
    def target(self):
        return self.home / '.local/share/kage/deploy-mira'


class AnswersTest(unittest.TestCase):
    def test_defaults_and_normalization(self):
        a = answers.validate(raw_answers())
        self.assertEqual(a['owner']['short_name'], 'Mira')
        self.assertEqual((a['channel'], a['tracker'], a['workspace_root']), ('slack', 'none', '~/kage-tasks'))
        self.assertEqual(a['_fallback_offset'], '+01:00')
        self.assertNotIn('_fallback_offset', answers.public(a))

    def test_standard_time_offsets(self):
        for tz, offset in (('Asia/Shanghai', '+08:00'), ('Australia/Sydney', '+10:00'), ('America/New_York', '-05:00'),
                           ('Asia/Kolkata', '+05:30'), ('UTC', '+00:00')):
            self.assertEqual(answers.fallback_offset(tz), offset, tz)

    def test_refusals(self):
        bad = [raw_answers(extra=1), raw_answers(channel='none'), raw_answers(tracker='jira'),
               raw_answers(schema='kage.answers/v0'), raw_answers(workspace_root='relative/path'),
               raw_answers(owner={'name': 'Mira', 'key': 'Mira', 'language': 'en', 'timezone': 'UTC'}),
               raw_answers(owner={'name': 'Mira', 'language': 'fr', 'timezone': 'UTC'}),
               raw_answers(owner={'name': 'Mira', 'language': 'en', 'timezone': 'Mars/Olympus'}),
               raw_answers(owner={'name': 'Mira', 'language': 'en', 'timezone': 'UTC', 'slack_user': OWNER}),
               raw_answers(slack={'voice': 'assistant', 'cards': '#ops', 'secrets': {}}),
               raw_answers(slack={'voice': 'as_owner', 'cards': 'dm', 'secrets': raw_answers()['slack']['secrets']}),
               raw_answers(slack={'voice': 'assistant', 'cards': 'dm',
                                  'secrets': {'slack-bot-token': 'kage-secret://other/slack-bot-token',
                                              'slack-app-token': 'kage-secret://mira/slack-app-token'}})]
        for raw in bad:
            with self.assertRaises(answers.AnswersError, msg=json.dumps(raw)):
                answers.validate(raw)

    def test_a_token_anywhere_refuses_the_file(self):
        for raw in (raw_answers(workspace_root='~/' + BOT_TOKEN),
                    raw_answers(owner={'name': APP_TOKEN, 'language': 'en', 'timezone': 'UTC'})):
            with self.assertRaises(answers.AnswersError) as cm:
                answers.validate(raw)
            self.assertNotIn('xox', str(cm.exception))
            self.assertNotIn('xapp', str(cm.exception))

    def test_write_is_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            path = answers.write(Path(d) / 'a/answers.json', answers.validate(raw_answers()))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(answers.load(path), answers.validate(raw_answers()))

    def test_default_key(self):
        self.assertEqual(answers.default_key('Mira Lind'), 'mira')
        self.assertEqual(answers.default_key('  '), 'owner')
        self.assertEqual(answers.default_key('李雷'), 'owner')


class GenerateTest(Base):
    def test_a_new_deployment(self):
        self.records()
        report = self.init()
        self.assertTrue(report['applied'] and report['new'])
        t = self.target
        self.assertEqual(stat.S_IMODE(t.stat().st_mode), 0o700)
        for f in t.rglob('*'):
            if '.git' in f.relative_to(t).parts:
                continue
            mode = stat.S_IMODE(f.stat().st_mode)
            self.assertEqual(mode, 0o700 if f.is_dir() or f.name in ('kage.py', 'dispatch_runner.py') else 0o600, f)
        d = profile.load(t / 'deploy')
        self.assertEqual((d.owner.key, d.owner.slack_workspace, d.owner.slack_user), ('mira', TEAM, OWNER))
        self.assertEqual((d.owner.assistant_slack_bot_user, d.owner.assistant_slack_app), (BOT_USER, APP_ID))
        self.assertEqual((d.owner.slack_workspace_url, d.org.tracker), (URL, 'none'))
        self.assertIsNone(d.org.ticket_ref_re())  # O1c5a: tracker none writes no pattern; the units match nothing
        self.assertNotIn('ref', json.loads((t / 'deploy/org.json').read_text())['tickets'])
        lock = json.loads((t / 'kage.lock').read_text())
        self.assertEqual(lock['kage']['version'], kage.__version__)
        self.assertEqual(sorted(lock['packs']), ['default.access', 'mira.playbook'])
        instance = json.loads((t / 'deploy/instance.json').read_text())
        self.assertNotIn('values', instance['assembly'])
        self.assertNotIn('modules', instance['assembly'])
        a = app.App(t / 'deploy')
        a.verify()
        a.bridge.pack('default.access')
        a.bridge.pack('mira.playbook')
        with mock.patch.dict(os.environ, {'HOME': str(self.home)}):
            units = {name: a.leaf(name) for name in ('intake', 'task_lifecycle')}
        for rx in (units['intake'].TASK_REF_RE, units['task_lifecycle'].TICKET_TASK_RE,
                   units['task_lifecycle'].TICKET_ID_RE):
            self.assertFalse(any(rx.search(text) for text in ('', 'PM-1', 'OPS-12', 'pm-1-task', 'none', '(?!)')), rx)
        self.assertEqual(stat.S_IMODE(init.state_dir('mira', self.home).stat().st_mode), 0o700)
        self.assertTrue((self.home / 'kage-tasks').is_dir())
        with sqlite3.connect(init.state_dir('mira', self.home) / 'state.sqlite') as db:
            self.assertEqual(db.execute('SELECT step, status FROM onboarding_steps').fetchall(), [('init', 'done')])
        if shutil.which('git'):
            log = subprocess.run(['git', '-C', str(t), 'log', '--oneline'], capture_output=True, text=True).stdout
            self.assertEqual(len(log.splitlines()), 1, log)
            self.assertEqual(report['git'], log.split()[0])
            self.assertEqual(subprocess.run(['git', '-C', str(t), 'remote'], capture_output=True, text=True).stdout, '')
        self.assertFalse(list(t.parent.glob('.kage-init-*')))
        self.assertFalse((self.home / '.config/kage/mira/decisions.json').exists())

    def test_every_switch_a_unit_reads_is_pinned(self):
        used = set()
        for unit in (KAGE_ROOT / 'kage/assembly').rglob('*.py'):
            used |= set(re.findall(r"module\('([a-z_]+)'", unit.read_text()))
        self.assertTrue(used)
        self.assertLessEqual(used, set(init.SWITCHES))

    def test_identity_only_from_the_records(self):
        with self.assertRaises(init.InitError) as cm:
            self.init()
        self.assertIn('owner_identity_not_verified', str(cm.exception))
        state = init.state_dir('mira', self.home)
        init._private_mkdir(state)
        identity.prove_by_user_token(state, {'team_id': TEAM, 'user_id': OWNER})
        with self.assertRaises(init.InitError) as cm:
            self.init()
        self.assertIn('bot_not_verified', str(cm.exception))
        self.assertFalse(self.target.exists())

    def test_records_of_two_workspaces_refuse(self):
        state = self.records()
        identity.prove_by_user_token(state, {'team_id': 'T0000OTHER1', 'user_id': OWNER})
        with self.assertRaises(init.InitError) as cm:
            self.init()
        self.assertIn('different_workspaces', str(cm.exception))

    def test_needs_a_verified_release_of_this_version(self):
        self.records()
        with self.assertRaises(init.InitError):
            self.init(release=self.tmp / 'nowhere')
        (self.release / 'kage/defaults.py').write_text('# altered\n')
        with self.assertRaises(init.InitError) as cm:
            self.init()
        self.assertIn('release_does_not_verify', str(cm.exception))
        self.assertFalse(self.target.exists())

    def test_a_card_channel_writes_the_private_decisions_config(self):
        self.records()
        raw = raw_answers()
        raw['slack']['cards'] = 'G0000TEST01'
        self.init(raw)
        path = self.home / '.config/kage/mira/decisions.json'
        self.assertEqual(json.loads(path.read_text()), {'decision_channel': 'G0000TEST01', 'approvers': [OWNER]})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class RerunTest(Base):
    def setUp(self):
        super().setUp()
        self.records()
        self.first = self.init()

    def head(self):
        return subprocess.run(['git', '-C', str(self.target), 'rev-parse', 'HEAD'], capture_output=True,
                              text=True).stdout.strip() if shutil.which('git') else None

    def test_same_answers_change_nothing(self):
        before = {p: p.read_bytes() for p in self.target.rglob('*') if p.is_file()}
        head = self.head()
        report = self.init(apply=True)
        self.assertFalse(report['applied'])
        self.assertEqual({s['action'] for s in report['steps']}, {'same'})
        self.assertEqual({p: p.read_bytes() for p in self.target.rglob('*') if p.is_file()}, before)
        self.assertEqual(self.head(), head)

    def test_a_changed_answer_is_shown_then_written_only_with_apply(self):
        (self.target / 'notes.txt').write_text('the owner\'s own file\n')
        raw = raw_answers()
        raw['owner']['timezone'] = 'Asia/Shanghai'
        report = self.init(raw)
        self.assertFalse(report['applied'])
        self.assertEqual([s['path'] for s in report['steps'] if s['action'] == 'change'], ['deploy/owner.json'])
        self.assertIn('+    "timezone": "Asia/Shanghai"', report['diff'])
        self.assertIn('Europe/Berlin', (self.target / 'deploy/owner.json').read_text())
        head = self.head()
        report = self.init(raw, apply=True)
        self.assertTrue(report['applied'])
        self.assertIn('Asia/Shanghai', (self.target / 'deploy/owner.json').read_text())
        self.assertEqual((self.target / 'notes.txt').read_text(), 'the owner\'s own file\n')
        if head:
            self.assertNotEqual(self.head(), head)
        self.assertFalse(list(self.target.parent.glob('*.kage-old-*')))

    def test_a_pack_is_never_replaced(self):
        pack = self.target / 'deploy/packs/mira/playbook.py'
        data = pack.read_bytes() + b'\n# the owner edited and re-pinned this pack\n'
        pack.write_bytes(data)
        lock = json.loads((self.target / 'kage.lock').read_text())
        lock['packs']['mira.playbook']['sha256'] = hashlib.sha256(data).hexdigest()
        (self.target / 'kage.lock').write_text(json.dumps(lock, indent=2, ensure_ascii=False) + '\n')
        report = self.init(apply=True)
        actions = {s['path']: s['action'] for s in report['steps']}
        self.assertEqual(actions['deploy/packs/mira/playbook.py'], 'keep')
        self.assertEqual(actions['kage.lock'], 'same')
        self.assertEqual(pack.read_bytes(), data)

    def test_a_rerun_keeps_what_the_deployment_added(self):
        # F11 (0.78.1): a re-pin by `init --answers --apply` dropped the console origins, assembly values, an extra pack
        # and the dashboard port
        instance = json.loads((self.target / 'deploy/instance.json').read_text())
        instance['console'] = {'public_origins': ['https://console.example.com']}
        instance['paths']['extra_notes'] = '~/notes'
        instance['assembly']['values'] = {'rule_engine._p': 'org.access'}
        (self.target / 'deploy/instance.json').write_text(json.dumps(instance, indent=2) + '\n')
        extra = self.target / 'deploy/packs/org/access.py'
        extra.parent.mkdir(mode=0o700)
        extra.write_bytes(b'# an organisation access pack\n')
        lock = json.loads((self.target / 'kage.lock').read_text())
        lock['packs']['org.access'] = {'path': 'deploy/packs/org/access.py',
                                       'sha256': hashlib.sha256(extra.read_bytes()).hexdigest()}
        (self.target / 'kage.lock').write_text(json.dumps(lock, indent=2) + '\n')
        services = json.loads((self.target / 'services.json').read_text())
        services['services']['dashboard']['args'][-1] = '8776'
        (self.target / 'services.json').write_text(json.dumps(services, indent=2) + '\n')
        raw = raw_answers()
        raw['owner']['timezone'] = 'Asia/Shanghai'
        self.assertTrue(self.init(raw, apply=True)['applied'])
        instance = json.loads((self.target / 'deploy/instance.json').read_text())
        self.assertEqual(instance['console'], {'public_origins': ['https://console.example.com']})
        self.assertEqual((instance['paths']['extra_notes'], instance['assembly']['values']),
                         ('~/notes', {'rule_engine._p': 'org.access'}))
        self.assertEqual(json.loads((self.target / 'kage.lock').read_text())['packs']['org.access']['path'],
                         'deploy/packs/org/access.py')
        self.assertEqual(json.loads((self.target / 'services.json').read_text())['services']['dashboard']['args'][-1],
                         '8776')
        self.assertIn('Asia/Shanghai', (self.target / 'deploy/owner.json').read_text())  # init's own files still follow
        self.assertTrue(extra.is_file())

    def test_refusals_leave_the_deployment_as_it_was(self):
        before = {p: p.read_bytes() for p in self.target.rglob('*') if p.is_file()}
        pack = self.target / 'deploy/packs/default/access.py'
        original = pack.read_bytes()
        pack.write_bytes(original + b'# altered, pin not updated\n')
        with self.assertRaises(init.InitError) as cm:
            self.init(apply=True)
        self.assertIn('pack_differs_from_its_pin', str(cm.exception))
        pack.write_bytes(original)
        org = self.target / 'deploy/org.json'
        org_bytes = org.read_bytes()
        org.unlink()
        org.symlink_to(self.tmp / 'elsewhere.json')
        with self.assertRaises(init.InitError) as cm:
            self.init(apply=True)
        self.assertIn('not_a_plain_file', str(cm.exception))
        org.unlink()
        org.write_bytes(org_bytes)
        os.chmod(org, 0o600)
        identity.prove_by_user_token(init.state_dir('mira', self.home), {'team_id': TEAM, 'user_id': 'U0000OTHER1'})
        with self.assertRaises(init.InitError) as cm:
            self.init(apply=True)
        self.assertIn('verified_identity_differs', str(cm.exception))
        identity.prove_by_user_token(init.state_dir('mira', self.home), {'team_id': TEAM, 'user_id': OWNER})
        self.assertEqual({p: p.read_bytes() for p in self.target.rglob('*') if p.is_file()}, before)

    def test_an_unpinned_pack_is_never_pinned_by_a_rerun(self):
        lock = json.loads((self.target / 'kage.lock').read_text())
        del lock['packs']['mira.playbook']
        (self.target / 'kage.lock').write_text(json.dumps(lock))
        self.assertEqual(self.init(apply=True)['applied'], True)  # the template's own bytes: pinned again
        pack = self.target / 'deploy/packs/mira/playbook.py'
        pack.write_bytes(pack.read_bytes() + b'# not pinned anywhere\n')
        (self.target / 'kage.lock').write_text(json.dumps(lock))  # the lock again without that pack's pin
        with self.assertRaises(init.InitError) as cm:
            self.init(apply=True)
        self.assertIn('pack_differs_from_its_pin', str(cm.exception))

    def test_another_owners_deployment_at_the_target_refuses(self):
        state = init.state_dir('lena', self.home)
        init._private_mkdir(state)
        identity.record_bot(state, {'team_id': TEAM, 'url': URL, 'bot_user_id': BOT_USER, 'bot_id': BOT_ID}, APP_ID)
        identity.prove_by_user_token(state, {'team_id': TEAM, 'user_id': OWNER})
        raw = raw_answers()
        raw['owner']['key'] = 'lena'
        raw['slack']['secrets'] = {n: f'kage-secret://lena/{n}' for n in raw['slack']['secrets']}
        with self.assertRaises(init.InitError) as cm:
            init.run(answers.validate(raw), home=self.home, target=self.target, apply=True, release=self.release)
        self.assertIn('another_owner_key', str(cm.exception))

    def test_an_interrupted_publish_names_itself(self):
        leftover = self.target.with_name(self.target.name + '.kage-old-1')
        leftover.mkdir()
        with self.assertRaises(init.InitError) as cm:
            self.init(apply=True)
        self.assertIn('interrupted_earlier_init', str(cm.exception))

    def test_a_failed_check_publishes_nothing(self):
        raw = raw_answers()
        raw['owner']['timezone'] = 'Asia/Shanghai'
        before = (self.target / 'deploy/owner.json').read_bytes()
        with mock.patch.object(init, '_check_staged', side_effect=init.InitError('generated_deployment_refused: x')):
            with self.assertRaises(init.InitError):
                self.init(raw, apply=True)
        self.assertEqual((self.target / 'deploy/owner.json').read_bytes(), before)
        self.assertFalse(list(self.target.parent.glob('.kage-init-*')))


class AdoptTest(Base):
    def test_adopt_writes_only_the_onboarding_row(self):
        self.records()
        self.init()
        state = init.state_dir('mira', self.home)
        (state / 'state.sqlite').unlink()
        before = {p: p.read_bytes() for p in self.target.rglob('*') if p.is_file()}
        with mock.patch.dict(os.environ, {'HOME': str(self.home)}):
            result = init.adopt(self.target / 'deploy', home=self.home)
        self.assertEqual(result['owner'], 'mira')
        self.assertEqual({p: p.read_bytes() for p in self.target.rglob('*') if p.is_file()}, before)
        with sqlite3.connect(state / 'state.sqlite') as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                             [('onboarding_steps',)])
            self.assertEqual(db.execute('SELECT step, status FROM onboarding_steps').fetchall(), [('init', 'adopted')])

    def test_adopt_refuses_a_deployment_that_does_not_assemble(self):
        with self.assertRaises(init.InitError):
            init.adopt(self.tmp / 'missing/deploy', home=self.home)


class WizardTest(Base):
    REPLIES = ['Mira Lind', '', '', 'en', 'Europe/Berlin', 'assistant', 'dm', '']

    def test_assistant_voice_end_to_end(self):
        slack = FakeSlack()

        def on_say(text):  # the owner sends the code shown to them, as a DM to the bot
            m = re.search(r'KAGE-[A-Z2-9]{5}-[A-Z2-9]{5}', text)
            if m:
                slack.dms.append({'type': 'message', 'user': OWNER, 'team': TEAM, 'text': m.group(0),
                                  'ts': f'{time.time() + 1:.6f}'})

        io = ScriptIO(self.REPLIES, ['xoxb-wrong', BOT_TOKEN, 'xoxe.xapp-1-TESTROTATING', APP_TOKEN], on_say)
        a, path = wizard.run(io, home=self.home, call=slack, sleep=lambda s: None)
        self.assertEqual((a['owner']['key'], a['slack']['voice']), ('mira', 'assistant'))
        self.assertTrue(any('refused: slack_auth.test_refused_invalid_auth' in line for line in io.out))
        self.assertTrue(any('refused: rotating_token_not_supported' in line for line in io.out))  # F9
        self.assertTrue(any('token rotation off' in line for line in io.out))
        store = secrets.for_owner('mira', home=self.home)
        self.assertEqual((store.get(tokens.BOT), store.get(tokens.APP)), (BOT_TOKEN, APP_TOKEN))
        state = init.state_dir('mira', self.home)
        self.assertEqual(identity.load(state)['method'], 'dm_code')
        self.assertEqual(identity.load_bot(state)['app_id'], APP_ID)
        self.assertEqual(answers.load(path), a)
        report = init.run(a, home=self.home, release=self.release)
        self.assertTrue(report['applied'])
        for base in (self.home,):
            for f in base.rglob('*'):
                if f.is_file() and store.root not in f.parents:
                    for secret in (BOT_TOKEN, APP_TOKEN):
                        self.assertNotIn(secret.encode(), f.read_bytes(), f)
        self.assertFalse(any(BOT_TOKEN in line or APP_TOKEN in line for line in io.out))

    def test_missing_bot_scopes_stop_at_the_manifest_step(self):
        """F7: the wizard does not go on with a bot that lacks scopes; it shows the manifest update + reinstall and
        reads the bot token again."""
        slack = FakeSlack()

        def on_say(text):
            m = re.search(r'KAGE-[A-Z2-9]{5}-[A-Z2-9]{5}', text)
            if m:
                slack.dms.append({'type': 'message', 'user': OWNER, 'team': TEAM, 'text': m.group(0),
                                  'ts': f'{time.time() + 1:.6f}'})

        io = ScriptIO(self.REPLIES, [THIN_TOKEN, BOT_TOKEN, APP_TOKEN], on_say)
        wizard.run(io, home=self.home, call=slack, sleep=lambda s: None)
        step = next(i for i, line in enumerate(io.out) if 'The bot lacks scopes:' in line)
        self.assertIn('groups:read', io.out[step])
        self.assertIn('App Manifest', io.out[step])
        self.assertIn('Reinstall to Northwind', io.out[step])
        self.assertLess(slack.calls.index('bots.info'), slack.calls.index('apps.connections.open'))
        self.assertEqual(secrets.for_owner('mira', home=self.home).get(tokens.BOT), BOT_TOKEN)
        self.assertEqual(identity.load(init.state_dir('mira', self.home))['method'], 'dm_code')

    def test_scopes_still_missing_stop_without_answers(self):
        slack = FakeSlack()
        io = ScriptIO(self.REPLIES, [THIN_TOKEN] * (wizard.TRIES + 1))
        with self.assertRaises(wizard.WizardError) as cm:
            wizard.run(io, home=self.home, call=slack, sleep=lambda s: None)
        self.assertTrue(str(cm.exception).startswith('bot_scopes_missing: '), cm.exception)
        self.assertEqual(sum('The bot lacks scopes:' in line for line in io.out), wizard.TRIES)
        self.assertNotIn('apps.connections.open', slack.calls)  # stopped before the next token
        self.assertNotIn('bots.info', slack.calls)
        self.assertFalse((self.home / '.config/kage/mira/answers.json').exists())

    def test_as_owner_voice_proves_by_the_user_token(self):
        replies = list(self.REPLIES)
        replies[5] = 'as_owner'
        io = ScriptIO(replies, [BOT_TOKEN, APP_TOKEN, USER_TOKEN])
        a, _path = wizard.run(io, home=self.home, call=FakeSlack(), sleep=lambda s: None)
        self.assertEqual(sorted(a['slack']['secrets']), ['slack-app-token', 'slack-bot-token', 'slack-user-token'])
        self.assertEqual(identity.load(init.state_dir('mira', self.home))['method'], 'user_token')

    def test_no_code_in_time_stops_without_answers(self):
        clock = iter(range(0, 10000, 100))
        io = ScriptIO(self.REPLIES, [BOT_TOKEN, APP_TOKEN])
        with self.assertRaises(wizard.WizardError) as cm:
            wizard.run(io, home=self.home, call=FakeSlack(), sleep=lambda s: None, clock=lambda: next(clock) + 1e9)
        self.assertIn('owner_not_verified', str(cm.exception))
        self.assertFalse((self.home / '.config/kage/mira/answers.json').exists())

    def test_invalid_replies_are_asked_again(self):
        replies = ['Mira Lind', 'Mira!', 'mira', '', 'fr', 'en', 'Mars/Olympus', 'UTC', 'loud', 'assistant', '#ops',
                   'dm', 'relative']
        io = ScriptIO(replies)
        raw = wizard.questions(io)
        self.assertEqual((raw['owner']['key'], raw['owner']['timezone'], raw['slack']['cards'], raw['workspace_root']),
                         ('mira', 'UTC', 'dm', '~/kage-tasks'))


class ProgramTest(Base):
    def test_the_program_runs_the_pinned_release_and_refuses_an_altered_one(self):
        self.records()
        self.init()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('SLACK_', 'KAGE_')) and '_DISPATCH_' not in k
               and 'RUN_CONTEXT' not in k and k not in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'PYTHONPATH')}
        env.update({'HOME': str(self.home), 'KAGE_NO_REAL_SLACK': '1', 'KAGE_NO_AUTOSTART': '1', 'KAGE_SECRETS_BACKEND': 'file',
                    'KAGE_VERSIONS_DIR': str(self.release.parent), 'PYTHONDONTWRITEBYTECODE': '1'})
        program = self.target / 'scripts/kage.py'
        out = subprocess.run([sys.executable, str(program), '--help'], capture_output=True, text=True, env=env,
                             timeout=120, stdin=subprocess.DEVNULL, cwd=str(self.tmp))
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn('ask-mira', out.stdout)
        (self.release / 'kage/defaults.py').write_text('# altered\n')
        out = subprocess.run([sys.executable, str(program), '--help'], capture_output=True, text=True, env=env,
                             timeout=120, stdin=subprocess.DEVNULL, cwd=str(self.tmp))
        self.assertEqual(out.returncode, 78)
        self.assertIn('file_hash_mismatch:kage/defaults.py', out.stderr)


class CommandTest(Base):
    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('SLACK_', 'KAGE_')) and '_DISPATCH_' not in k}
        env.update({'HOME': str(self.home), 'KAGE_NO_REAL_SLACK': '1', 'KAGE_NO_AUTOSTART': '1', 'KAGE_SECRETS_BACKEND': 'file',
                    'PYTHONPATH': str(KAGE_ROOT), 'PYTHONDONTWRITEBYTECODE': '1'})
        return subprocess.run([sys.executable, '-m', 'kage', 'init', *args], capture_output=True, text=True, env=env,
                              timeout=120, stdin=subprocess.DEVNULL)

    def test_answers_mode_needs_the_referenced_secrets_and_a_terminal_for_the_wizard(self):
        self.records()
        path = answers.write(self.tmp / 'answers.json', answers.validate(raw_answers()))
        out = self.run_cli('--answers', str(path), '--release', str(self.release))
        self.assertEqual(out.returncode, 1)
        self.assertIn('secret_missing: slack-bot-token, slack-app-token', out.stderr)
        store = secrets.for_owner('mira', home=self.home)
        store.put(secrets.SLACK_BOT, BOT_TOKEN)
        store.put(secrets.SLACK_APP, APP_TOKEN)
        out = self.run_cli('--answers', str(path), '--release', str(self.release))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('init: created', out.stdout)
        out = self.run_cli('--answers', str(path), '--release', str(self.release))
        self.assertIn('is up to date', out.stdout)
        out = self.run_cli()
        self.assertEqual(out.returncode, 1)
        self.assertIn('init_needs_a_terminal', out.stderr)
        for text in (out.stdout, out.stderr):
            self.assertNotIn(BOT_TOKEN, text)



class AcceptanceTest(Base):
    """The whole first day on a scratch HOME with a fake Slack: wizard -> `python3 -m kage init --answers` -> the
    deployment's own program: --help, doctor --offline (every required check passes), note -> context -> resolve;
    a re-run changes nothing; no token outside the store."""
    SESSION = '00000000-0000-4000-8000-0000000acc04'

    def program(self, *args, session=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('SLACK_', 'KAGE_')) and '_DISPATCH_' not in k
               and 'RUN_CONTEXT' not in k and k not in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'PYTHONPATH')}
        bin_dir = self.tmp / 'bin'
        if not bin_dir.exists():  # an agent CLI the doctor can find on any machine (CI has none)
            bin_dir.mkdir()
            (bin_dir / 'claude').write_text('#!/bin/sh\necho "0.0.0 (test agent)"\n')
            os.chmod(bin_dir / 'claude', 0o755)
            from tests.test_services import FAKE_LAUNCHCTL  # O1c5e: never the machine's launchd
            (bin_dir / 'launchctl').write_text(FAKE_LAUNCHCTL)
            os.chmod(bin_dir / 'launchctl', 0o755)
            (self.tmp / 'site/websockets').mkdir(parents=True)  # 0.77.1: the receiver's package, for doctor (CI has none)
            (self.tmp / 'site/websockets/__init__.py').write_text("__version__ = '13.1'\n")
        env.update({'FAKE_LAUNCHCTL_STATE': str(self.tmp / 'launchd.json'), 'FAKE_LAUNCHCTL_LOG': str(self.tmp / 'launchd.log')})
        env.update({'HOME': str(self.home), 'KAGE_NO_REAL_SLACK': '1', 'KAGE_NO_AUTOSTART': '1', 'KAGE_SECRETS_BACKEND': 'file',
                    'KAGE_VERSIONS_DIR': str(self.release.parent), 'PYTHONDONTWRITEBYTECODE': '1',
                    'PATH': f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}', 'PYTHONPATH': str(self.tmp / 'site'),
                    **({'CLAUDE_CODE_SESSION_ID': session} if session else {})})
        return subprocess.run([sys.executable, str(self.target / 'scripts/kage.py'), *args], capture_output=True,
                              text=True, env=env, timeout=120, stdin=subprocess.DEVNULL, cwd=str(self.tmp))

    def test_first_day(self):
        slack = FakeSlack()

        def on_say(text):
            m = re.search(r'KAGE-[A-Z2-9]{5}-[A-Z2-9]{5}', text)
            if m:
                slack.dms.append({'type': 'message', 'user': OWNER, 'team': TEAM, 'text': m.group(0),
                                  'ts': f'{time.time() + 1:.6f}'})

        io = ScriptIO(WizardTest.REPLIES, [BOT_TOKEN, APP_TOKEN], on_say)
        _answers, path = wizard.run(io, home=self.home, call=slack, sleep=lambda s: None)
        cli = CommandTest.run_cli(self, '--answers', str(path), '--release', str(self.release))
        self.assertEqual(cli.returncode, 0, cli.stderr[-2000:])
        out = self.program('--help')
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn('ask-mira', out.stdout)
        doctor = self.program('doctor', '--offline')
        text = doctor.stdout + doctor.stderr
        self.assertIn('ok    owner.identity: verified (dm_code)', text)
        self.assertIn('ok    secrets.port: file store: slack-bot-token present, slack-app-token present', text)
        self.assertIn('doctor: failed: services.running', text, text[-3000:])  # O1c5e: init loads no service
        self.assertIn('`services start`', text)
        self.assertIn('ok    receiver.websockets: websockets 13.1', text)
        refused = self.program('mode', 'observe', '--offline')  # 0.77.1: the way out without launchd, same gate
        self.assertEqual((refused.returncode, json.loads(refused.stderr)['detail']),
                         (1, 'doctor_required_checks_fail: services.running'))
        started = self.program('services', 'start', '--offline')
        self.assertEqual(started.returncode, 0, started.stderr[-2000:])
        self.assertEqual(set(json.loads(started.stdout)['jobs'].values()), {'running'})
        doctor = self.program('doctor', '--offline')
        text = doctor.stdout + doctor.stderr
        self.assertIn('ok    services.running: 3 / 3 running', text)
        # O2a: init wrote `installed`, `services start` (the required checks pass) moved it to observe-only
        mode = json.loads(self.program('mode').stdout)
        self.assertEqual((mode['mode'], [r['new'] for r in reversed(mode['log'])]), ('observe', ['installed', 'observe']))
        refused = self.program('mode', 'resume')
        self.assertEqual((refused.returncode, json.loads(refused.stderr)['detail']), (1, 'resume_is_only_from_paused'))
        self.assertIn('doctor: all required checks pass', text, text[-3000:])
        task = self.home / 'kage-tasks/t1'
        task.mkdir()
        (task / 'goal.md').write_text('# Goal\n\nAcceptance task.\n')
        (task / 'status.md').write_text('<!-- rev: 1 -->\n# Status\n')
        self.assertEqual(json.loads(self.program('note', '--task', 't1', '--text', 'registers the task').stdout)[
            'reason'], 'task_without_current_session_note_kept_in_history')
        done = self.program('tasks', 'set-current', '--task', 't1', '--provider', 'claude', '--session', self.SESSION,
                            '--reason', 'acceptance')
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        event = json.loads(self.program('note', '--task', 't1', '--text', 'one local event').stdout)['event']
        context = self.program('context', '--event', event, session=self.SESSION)
        self.assertEqual(context.returncode, 0, context.stderr[-2000:])
        resolve = self.program('resolve', '--event', event, '--disposition', 'observe', session=self.SESSION)
        self.assertEqual(resolve.returncode, 0, resolve.stderr[-2000:])
        with sqlite3.connect(init.state_dir('mira', self.home) / 'state.sqlite') as db:
            self.assertEqual(db.execute('SELECT status, disposition FROM events WHERE id=?', (event,)).fetchone(),
                             ('handled', 'observe'))
            self.assertEqual(db.execute('SELECT step, status FROM onboarding_steps').fetchall(), [('init', 'done')])
        again = CommandTest.run_cli(self, '--answers', str(path), '--release', str(self.release))
        self.assertIn('is up to date', again.stdout)
        self.assert_no_supervisor()
        store = secrets.for_owner('mira', home=self.home)
        for f in self.home.rglob('*'):
            if f.is_file() and store.root not in f.parents:
                for secret in (BOT_TOKEN, APP_TOKEN):
                    self.assertNotIn(secret.encode(), f.read_bytes(), f)


if __name__ == '__main__':
    unittest.main()
