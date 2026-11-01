import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from kage import app, service


def release(root, version='9.9.0', commit='c' * 40):
    rel = root / 'versions' / f'{version}-{commit[:12]}'
    (rel / 'kage').mkdir(parents=True)
    (rel / 'kage' / '__init__.py').write_text("__version__ = '9.9.0'\n")
    (rel / 'kage' / 'service.py').write_text('# the launcher\n')
    lines = ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(rel).as_posix()}\n'
                    for p in sorted((rel / 'kage').rglob('*')) if p.is_file())
    (rel / 'MANIFEST.sha256').write_text(lines)
    tree = hashlib.sha256(lines.encode()).hexdigest()
    (rel / 'BUNDLE.json').write_text(json.dumps({'version': version, 'commit': commit, 'tree_sha256': tree}))
    return rel, {'version': version, 'commit': commit, 'tree_sha256': tree}


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rel, pin = release(self.tmp)
        self.repo = self.tmp / 'deploy-repo'
        (self.repo / 'deploy').mkdir(parents=True)
        (self.repo / 'kage.lock').write_text(json.dumps({'kage': pin, 'modules': {}}))
        (self.repo / 'services.json').write_text(json.dumps({'schema': 'kage.services/v1', 'skills': '/skills', 'services': {
            'followup': {'python': '/usr/bin/python3', 'script': 'a/run.py', 'args': ['run'], 'workdir': '/skills/a'}}}))

    def plan(self, name='followup', environ=None, rel=None):
        return service.plan(self.repo / 'deploy', name, environ or {'HOME': '/h'}, release=rel or self.rel)

    def test_the_service_runs_with_where_the_deployment_lives(self):
        argv, env, cwd = self.plan()
        self.assertEqual(argv, ['/usr/bin/python3', '/skills/a/run.py', 'run'])
        self.assertEqual(cwd, '/skills/a')
        self.assertEqual({k: env[k] for k in ('KAGE_LOCK', 'KAGE_DEPLOYMENT_DIR', 'KAGE_VERSIONS_DIR', 'HOME')},
                         {'KAGE_LOCK': str(self.repo / 'kage.lock'), 'KAGE_DEPLOYMENT_DIR': str(self.repo / 'deploy'),
                          'KAGE_VERSIONS_DIR': str(self.tmp / 'versions'), 'HOME': '/h'})

    def test_refused_when_the_release_is_not_the_pinned_one_or_does_not_verify(self):
        with self.assertRaisesRegex(service.Refused, 'no_service: nope'):
            self.plan('nope')
        other, _ = release(self.tmp, version='9.9.1', commit='d' * 40)
        with self.assertRaisesRegex(service.Refused, 'release_not_pinned'):
            self.plan(rel=other)
        (self.rel / 'kage' / 'service.py').write_text('# altered\n')
        with self.assertRaisesRegex(service.Refused, 'file_hash_mismatch:kage/service.py'):
            self.plan()

    def test_dry_run_and_exit_codes(self):
        out = io.StringIO()
        main = lambda argv, env: service.main(argv, env)  # noqa: E731
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['--deployment', str(self.repo / 'deploy'), 'followup'],
                                  {'KAGE_SERVICE_DRY_RUN': '1', 'KAGE_LOCK': str(self.repo / 'kage.lock')}), 78)  # not from a release
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(['followup'], {}), 64)
        self.assertIn('usage', err.getvalue())


class OwnerAliasTest(unittest.TestCase):
    def test_the_generic_names_become_the_owners(self):
        self.assertEqual(app.owner_alias(['ask-owner', '--about', 'x'], 'joe'), ['ask-joe', '--about', 'x'])
        self.assertEqual(app.owner_alias(['--db', '/x', 'ask-owner-close', 'c1'], 'joe'), ['--db', '/x', 'ask-joe-close', 'c1'])
        self.assertEqual(app.owner_alias(['status', 'ask-owner'], 'joe'), ['status', 'ask-owner'])
        self.assertEqual(app.owner_alias(['--db'], 'joe'), ['--db'])
        self.assertEqual(app.owner_alias([], 'joe'), [])


if __name__ == '__main__':
    unittest.main()
