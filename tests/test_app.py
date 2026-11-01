import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import kage
from kage import __main__ as program
from kage import app

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/northwind-mira/deploy'


class Deployment:
    """A scratch deployment root: deploy/ (the example profile, owner key `mira`), kage.lock, the program and one
    transitional module, with instance.json's `assembly` section."""

    def __init__(self, **changes):
        self.changes = changes

    def __enter__(self):
        self.root = Path(tempfile.mkdtemp())
        shutil.copytree(EXAMPLE, self.root / 'deploy')
        owner = json.loads((self.root / 'deploy/owner.json').read_text())
        owner.setdefault('owner', owner)['key'] = 'mira'
        (self.root / 'deploy/owner.json').write_text(json.dumps(owner))
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/program.py').write_text('# the deployment program\n')
        (self.root / 'scripts/legacy_mod.py').write_text('ANSWER = 42\n')
        modules = {'legacy_mod': {'path': 'scripts/legacy_mod.py',
                                  'sha256': hashlib.sha256(b'ANSWER = 42\n').hexdigest(), 'note': 'test'}}
        lock = {'schema': 'kage.lock/v1', 'kage': {'version': kage.__version__},
                'modules': {'registry': 'kage', 'liveness': 'kage', 'assembly': 'kage'}}
        lock.update(self.changes.pop('lock', {}))
        (self.root / 'kage.lock').write_text(json.dumps(lock))
        instance = json.loads((self.root / 'deploy/instance.json').read_text())
        instance['assembly'] = {'program': 'scripts/program.py', 'lock': 'kage.lock', 'runtime_version': 'test-1',
                                'values': {'task_registry.ROOT_ENV': 'TEST_TASKS_ROOT',
                                           'task_registry.SMOKE_ROOT_ENV': 'TEST_SMOKE_ROOT',
                                           'task_registry.DEFAULT_SMOKE_ROOT': '/tmp/test-smoke'},
                                'modules': modules}
        for key, value in self.changes.items():
            if value is None:
                instance['assembly'].pop(key, None)
            else:
                instance['assembly'][key] = value
        (self.root / 'deploy/instance.json').write_text(json.dumps(instance))
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)


class AppTest(unittest.TestCase):
    def test_units_assemble_from_kage_and_the_deployment_values(self):
        with Deployment() as d:
            a = app.App(d.root / 'deploy')
            a.verify()
            registry = a.leaf('task_registry')
            self.assertEqual((registry.ROOT_ENV, registry.SMOKE_ROOT_ENV), ('TEST_TASKS_ROOT', 'TEST_SMOKE_ROOT'))
            self.assertEqual(registry.REGISTRY.__class__.__module__, 'kage.core.task_registry')
            self.assertIs(a.leaf('task_registry'), registry)  # once per assembly
            self.assertEqual(a.leaf('liveness').Probe.__module__, 'kage.adapters.providers.liveness')
            self.assertEqual(a.leaf('legacy_mod').ANSWER, 42)  # a declared transitional module, verified
            self.assertEqual((a.version, a.program, a.root), ('test-1', d.root / 'scripts/program.py', d.root))

    def test_owner_keyed_names_and_values(self):
        with Deployment() as d:
            a = app.App(d.root / 'deploy')
            self.assertEqual(a.key_name('ask_owner'), 'ask_mira')
            self.assertEqual(a.key_name('WHY_OWNER'), 'WHY_MIRA')
            self.assertEqual(a.key_name('--why-owner'), '--why-mira')
            with self.assertRaisesRegex(app.AssemblyError, 'value_missing: nope.x'):
                a.value('nope.x')
            with self.assertRaisesRegex(app.AssemblyError, 'unknown_module: elsewhere'):
                a.leaf('elsewhere')

    def test_refusals_before_anything_loads(self):
        cases = [({'lock': {'modules': {'registry': 'legacy'}}}, 'switches_not_on_kage: registry'),
                 ({'lock': {'kage': {'version': '0.0.1'}}}, 'release_mismatch: the lock pins 0.0.1'),
                 ({'modules': {'legacy_mod': {'path': 'scripts/gone.py', 'sha256': '0' * 64}}},
                  r'legacy_mod: missing \(scripts/gone.py\)'),
                 ({'modules': {'legacy_mod': {'path': 'scripts/legacy_mod.py', 'sha256': '1' * 64}}},
                  r'legacy_mod: sha256 differs'),
                 ({'modules': {'legacy_mod': {'path': 'scripts/legacy_mod.py'}}}, 'legacy_mod: no sha256 pin')]
        for changes, reason in cases:
            with self.subTest(reason=reason), Deployment(**changes) as d:
                with self.assertRaisesRegex(app.AssemblyError, reason):
                    app.App(d.root / 'deploy').assemble()
        with Deployment() as d:
            (d.root / 'deploy/instance.json').write_text(json.dumps({'schema': 'kage.instance/v1', 'paths': {}}))
            with self.assertRaisesRegex(app.AssemblyError, 'instance_json_has_no_assembly_section'):
                app.App(d.root / 'deploy')
            a = None
        with Deployment() as d:
            a = app.App(d.root / 'deploy')
            with self.assertRaisesRegex(app.AssemblyError, 'transitional_modules_not_verified'):
                a.leaf('legacy_mod')

    def test_python_m_kage_refuses_cleanly(self):
        with Deployment(modules={'legacy_mod': {'path': 'scripts/gone.py', 'sha256': '0' * 64}}) as d:
            before = sorted(str(p.relative_to(d.root)) for p in d.root.rglob('*'))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = program.main(['--deployment', str(d.root / 'deploy'), 'status'])
            self.assertEqual(code, 3)
            line = json.loads(err.getvalue())
            self.assertEqual(line['error'], 'AssemblyError')
            self.assertIn('legacy_mod: missing', line['detail'])
            self.assertEqual(sorted(str(p.relative_to(d.root)) for p in d.root.rglob('*')), before)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(program.main(['status']), 2)
        self.assertIn('usage: python3 -m kage --deployment DIR', err.getvalue())


if __name__ == '__main__':
    unittest.main()
