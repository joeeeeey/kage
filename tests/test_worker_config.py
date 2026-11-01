import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from kage.core import worker_config

DEFAULTS = json.loads((Path(__file__).with_name('fixtures') / 'worker_defaults.json').read_text())  # a deployment's


def config(tmp, defaults=DEFAULTS):
    return worker_config.WorkerConfig('en', defaults=defaults, config_env='KAGE_TEST_WORKER_CONFIG',
                                      default_path=Path(tmp) / 'missing.json', owner_key='owner', owner_name='Owner')


class WorkerConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wc = config(self.tmp)

    def test_the_path_comes_from_the_deployment_variable_or_its_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KAGE_TEST_WORKER_CONFIG', None)
            self.assertEqual(self.wc.config_path(), self.tmp / 'missing.json')
            os.environ['KAGE_TEST_WORKER_CONFIG'] = str(self.tmp / 'c.json')
            self.assertEqual(self.wc.config_path(), self.tmp / 'c.json')
            self.assertEqual(self.wc.log_path(), self.tmp / 'c.changes.jsonl')

    def test_missing_file_is_the_deployment_defaults_and_unknown_keys_fail_closed(self):
        loaded, source = self.wc.load(self.tmp / 'none.json')
        self.assertEqual(source, 'default')
        self.assertEqual(loaded['providers'], DEFAULTS['providers'])
        self.assertIsNot(loaded, DEFAULTS)
        with self.assertRaises(ValueError):
            self.wc.validate({**DEFAULTS, 'surprise': 1})

    def test_a_set_writes_privately_and_logs_the_change(self):
        path = self.tmp / 'c.json'
        result = self.wc.set_value('routing.categories.infra', '"claude"', 'test', path=path, now=1700000000.0)
        self.assertTrue(result['ok'])
        self.assertEqual(json.loads(path.read_text())['routing']['categories']['infra'], 'claude')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.wc.change_log(path)), 1)
        with self.assertRaises(ValueError):
            self.wc.set_value('routing.categories.infra', '"nobody"', 'test', path=path, now=1700000001.0)

    def test_defaults_are_the_instance_value_not_a_module_global(self):
        other = config(self.tmp, {**DEFAULTS, 'circuit_breaker': {'round_trips_per_hour': 3}})  # another deployment
        self.assertEqual(other.load(self.tmp / 'none.json')[0]['circuit_breaker'], {'round_trips_per_hour': 3})
        self.assertEqual(self.wc.load(self.tmp / 'none.json')[0]['circuit_breaker'], {'round_trips_per_hour': 10})


if __name__ == '__main__':
    unittest.main()
