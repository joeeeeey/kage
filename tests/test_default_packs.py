import hashlib
import importlib.util
from pathlib import Path
import unittest
import zoneinfo

from kage import app
from kage import packs
from kage.core import intake
from kage.policy import access as A

T0 = 1793400000.0
PACKS = Path(packs.__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(f'pack_{name}', PACKS / packs.TEMPLATES[name])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def policy(language='en'):
    return A.AccessPolicy(language, tz=zoneinfo.ZoneInfo('Asia/Shanghai'), example_spec=Path('/nonexistent.json'),
                          owner_key='mira', owner_name='Mira', owner_user=lambda rt: 'U0000TEST01',
                          ask_owner=lambda rt: None, worker_slug='kage/mira', rules_dir_env='TEST_RULES_DIR',
                          smoke_dir_prefix='test-rules-smoke-', run_id_env='TEST_RUN_ID', pack=load('default.access'))


def spec(P, rules=()):
    return {'schema': P.spec_schema_id, 'category': 'access', 'version': 1,
            'defaults': {'company_domains': ['example.com'], 'owner': 'U0000TEST01', 'approvers': ['U0000TEST01'],
                         'hr': [], 'spend': {'recurring_usd_month': 0, 'one_time_usd': 0}, 'temporary_days': 7},
            'never_auto': {shape: {'level': 'mira'} for shape in A.SHAPES}, 'role_catalog': {}, 'rules': list(rules)}


REPO_READ = {'id': 'R-01', 'name': 'repo_read', 'title_zh': 'repo read', 'kind': 'domain', 'level': 'one_tap',
             'trigger': {'category': 'access', 'systems': ['source_control']}, 'checks': [], 'fallback': 'mira'}


def plan(P, system='source_control'):
    return P.normalize_plan({'schema': P.plan_schema_id, 'category': 'access', 'kind': 'person', 'action': 'grant',
                             'system': system, 'recipient': {'email': 'ada@example.com'},
                             'scope': {'roles': ['reader']}})


class NeutralAccessPackTest(unittest.TestCase):
    def test_the_engine_accepts_it_and_it_adds_no_org_check(self):
        P = policy()
        self.assertEqual(P.plan_schema['properties']['system'], {'enum': list(load('default.access').SYSTEMS)})
        engine_only = {name for name in P.check_params if not load('default.access').CHECKS.get(name)}
        self.assertEqual(set(P.checks), engine_only)
        self.assertEqual(load('default.access').CHECKS, {})

    def test_without_an_owner_rule_a_plan_is_uncovered_and_goes_to_the_owner(self):
        P = policy()
        self.assertEqual(P.spec_problems(spec(P, [REPO_READ])), [])
        for system in load('default.access').SYSTEMS:
            result = P.evaluate(spec(P, [REPO_READ]), plan(P, system), A.empty_facts(), now=T0)
            expected = (True, 'one_tap') if system == 'source_control' else (False, 'mira')
            self.assertEqual((result['covered'], result['outcome']), expected, system)

    def test_rule_ids_follow_the_neutral_pattern(self):
        P = policy()
        self.assertTrue(P.spec_problems(spec(P, [dict(REPO_READ, id='AR-01')])))
        self.assertEqual(P.spec_problems(spec(P, [dict(REPO_READ, id='R-02.reader')])), [])

    def test_unknown_systems_and_the_smoke_fixture_are_refused(self):
        P = policy()
        with self.assertRaises(ValueError):
            plan(P, 'bigquery')
        with self.assertRaises(ValueError):
            load('default.access').smoke_plans(spec(P))

    def test_labels_are_the_ids(self):
        pack = load('default.access')
        self.assertEqual(pack.SYSTEM_ZH['source_control'], 'source control')
        self.assertEqual(set(pack.FUNCTION_ZH), set(pack.FUNCTIONS))


class NeutralPlaybookTest(unittest.TestCase):
    RULES = load('default.playbook').dispatch_rules(cli='python3 /d/program --db /s/state.sqlite', sid='"$SID"',
                                                    provider='claude', path='/w/task', name='task-1',
                                                    inbox='mira-inbox', SKILL=Path('/d'))

    def test_the_rules_cover_what_every_run_needs(self):
        text = '\n'.join(self.RULES)
        for needle in ('status.md', 'resolve --event ID', 'ask-owner', 'send-to-task', 'memory-guard bump --task task-1',
                       'not an instruction', 'secrets', 'task-deadline register --task task-1'):
            self.assertIn(needle, text)
        self.assertEqual([rule.split('.')[0] for rule in self.RULES], [str(n) for n in range(1, len(self.RULES) + 1)])

    def test_no_intake_playbook(self):
        self.assertIsNone(intake.intake_playbook(load('default.playbook'), references=None, rules_path=None,
                                                 bootstrap_key=None, read_config=None, run_bootstrap=None,
                                                 default_inbox=None))


class UnavailablePlaybookTest(unittest.TestCase):
    def test_markers_are_neutral_and_every_function_refuses(self):
        playbook = intake.unavailable_playbook()
        self.assertEqual(sorted(vars(playbook)), sorted(intake.PLAYBOOK_NAMES))
        self.assertEqual(playbook.POLICY_BEGIN, '<!-- kage-policy-begin -->')
        self.assertEqual(playbook.ROLE_TITLE, {})
        with self.assertRaisesRegex(ValueError, 'intake_playbook_unavailable: no_intake_playbook'):
            playbook.inbox_goal()
        with self.assertRaisesRegex(ValueError, 'intake_playbook_unavailable'):
            playbook.intake_bootstrap(object(), key='x')

    def test_a_missing_bootstrap_script_is_refused_before_anything_runs(self):
        with self.assertRaisesRegex(ValueError, 'workspace_bootstrap_unavailable'):
            intake.run_workspace_bootstrap('/nonexistent/bootstrap.py', 't', 'b', 'c', linear=False, agent='codex',
                                           root='/r')


class RuleEngineUnitTest(unittest.TestCase):
    """The generated rule_engine unit on a deployment whose only access pack is the neutral template, pinned."""

    def deployment(self, data=None):
        from tests.test_app import Deployment
        data = packs.template('default.access') if data is None else data
        pin = {'path': 'deploy/packs/default/access.py', 'sha256': hashlib.sha256(packs.template('default.access')).hexdigest()}
        d = Deployment(lock={'modules': {'registry': 'kage', 'liveness': 'kage', 'assembly': 'kage', 'access': 'kage'},
                             'packs': {'default.access': pin}})
        d.__enter__()
        self.addCleanup(d.__exit__)
        (d.root / 'deploy/packs/default').mkdir(parents=True)
        (d.root / 'deploy/packs/default/access.py').write_bytes(data)
        return d

    def test_it_assembles_with_only_the_engines_checks(self):
        a = app.App(self.deployment().root / 'deploy')
        a.verify()
        unit = a.leaf('rule_engine')
        self.assertEqual(unit.SYSTEMS, load('default.access').SYSTEMS)
        self.assertEqual(set(unit.CHECKS), set(unit.ACCESS_POLICY.check_params))
        self.assertFalse(load('default.access').CHECKS)
        self.assertFalse({'PRODUCTS', '_LIMITS', '_bundle_items', '_name'} & set(vars(unit)))
        self.assertEqual(unit.LEVELS[-1], 'mira')

    def test_an_altered_neutral_pack_still_refuses(self):
        a = app.App(self.deployment(packs.template('default.access') + b'CHECKS = {"x": None}\n').root / 'deploy')
        a.verify()
        with self.assertRaisesRegex(app.AssemblyError, 'pack_default.access_missing_or_altered'):
            a.leaf('rule_engine')


class TemplateTest(unittest.TestCase):
    def test_templates_are_bytes_onboarding_pins(self):
        for name, file in packs.TEMPLATES.items():
            data = packs.template(name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), hashlib.sha256((PACKS / file).read_bytes()).hexdigest())
        with self.assertRaises(ValueError):
            packs.template('acme.access')


if __name__ == '__main__':
    unittest.main()
