import ast
import json
from pathlib import Path
import unittest

from kage import app, defaults, profile
from kage.core.worker_config import WorkerConfig
from tests.test_app import Deployment


class DefaultsTest(unittest.TestCase):
    def test_every_generated_value_has_a_default_and_unknown_keys_still_refuse(self):
        requested = set()
        for path in app.UNIT_DIR.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.Call) and ast.unparse(node.func) == '_app.value':
                    requested.add(node.args[0].value)
        with Deployment(values={}) as deployment:
            assembly = app.App(deployment.root / 'deploy')
            self.assertEqual(requested, set(assembly.values))
            with self.assertRaisesRegex(app.AssemblyError, 'value_missing: unknown.key'):
                assembly.value('unknown.key')

    def test_a_minimal_instance_loads_a_unit_without_copying_wiring(self):
        with Deployment(values=None) as deployment:
            assembly = app.App(deployment.root / 'deploy')
            assembly.verify()
            registry = assembly.leaf('task_registry')
            self.assertEqual(registry.ROOT_ENV, 'KAGE_TASKS_ROOT')
            self.assertEqual(str(registry.DEFAULT_SMOKE_ROOT), '/tmp/kage-mira-dispatch-smoke')

    def test_explicit_values_replace_even_objects_wholesale(self):
        overrides = {'worker_config.DEFAULTS': {'only': 'explicit'}, 'hub.DEFAULT_DB': 'existing.sqlite',
                     'custom.value': None, 'hub.RECEIPT_EMOJI.codex': ''}
        with Deployment(values=overrides) as deployment:
            assembly = app.App(deployment.root / 'deploy')
            self.assertEqual({key: assembly.value(key) for key in overrides}, overrides)
            self.assertNotIn('providers', assembly.value('worker_config.DEFAULTS'))

    def test_defaults_are_private_to_each_owner_and_assembly(self):
        with Deployment(values={}) as deployment:
            first = app.App(deployment.root / 'deploy')
            first.value('worker_config.DEFAULTS')['providers']['codex']['model'] = 'test-mutated'
            owner_path = deployment.root / 'deploy/owner.json'
            owner = json.loads(owner_path.read_text())
            owner['owner'].update(key='alex', name='Alex Example', short_name='Alex')
            owner_path.write_text(json.dumps(owner))
            second = app.App(deployment.root / 'deploy')
            self.assertNotEqual(first.value('hub.DEFAULT_DB'), second.value('hub.DEFAULT_DB'))
            self.assertIn('Alex', second.value('card_gate.help'))
            worker = second.value('worker_config.DEFAULTS')
            self.assertNotEqual(worker['providers']['codex']['model'], 'test-mutated')
            self.assertEqual(worker['intake']['inbox_task'], 'alex-inbox')
            self.assertIn('state-alex/', second.value('hub.DEFAULT_DB'))

    def test_new_worker_config_is_valid_and_inert_until_configured(self):
        with Deployment(values={}) as deployment:
            loaded = profile.load(deployment.root / 'deploy')
            worker = defaults.assembly_values(loaded)['worker_config.DEFAULTS']
            config = WorkerConfig('en', defaults=worker, config_env='KAGE_TEST_CONFIG',
                                  default_path=deployment.root / 'missing.json', owner_key='mira', owner_name='Mira')
            self.assertEqual(config.validate(worker), worker)
            self.assertFalse(worker['dispatch']['enabled'])
            self.assertFalse(worker['intake']['enabled'])
            self.assertFalse(worker['intake']['dm_request_routing'])
            self.assertEqual(worker['intake']['channels'], [])
            self.assertFalse(worker['feedback_learning']['enabled'])
            self.assertEqual(worker['feedback_learning']['max_promote_cards_per_day'], 0)

    def test_defaults_do_not_bypass_missing_policy_packs(self):
        with Deployment(values={}) as deployment:
            assembly = app.App(deployment.root / 'deploy')
            with self.assertRaisesRegex(app.AssemblyError, 'pack_default.access_not_pinned'):
                assembly.bridge.pack(assembly.value('rule_engine._p'))


if __name__ == '__main__':
    unittest.main()
