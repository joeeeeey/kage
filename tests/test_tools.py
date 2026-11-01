import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f'kage_tool_{name}', ROOT / 'tools' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


leak_scan = load_tool('leak_scan')
bundle = load_tool('bundle')

# Probe values are assembled at run time so this file passes the scan it tests.
REAL_ID = 'U7Q2' + 'K9X4M1'
REAL_CHANNEL = 'C08A' + 'BCD12EF'
HOME_MAC = '/Users/' + 'quillon/repos/x'
HOME_LINUX = '/home/' + 'quillon/x'
MAIL = 'quillon' + '@' + 'northwind.io'


class LeakScanTest(unittest.TestCase):
    def findings(self, text, denied=frozenset()):
        return [(rule, hint) for _, rule, hint in leak_scan.scan_text(text, set(denied))]

    def test_denylisted_word_is_found_by_hash_and_masked(self):
        found = self.findings('owner: Quillon Varga', {leak_scan.digest('quillon')})
        self.assertEqual(found, [('denylist', 'Q…(7 chars)')])

    def test_denylist_is_case_insensitive_and_splits_identifiers(self):
        denied = {leak_scan.digest('northwind')}
        self.assertEqual(len(self.findings('NORTHWIND_TOKEN_FILE and northwind-infra', denied)), 2)

    def test_compound_project_names(self):
        # K15d: a project name is a compound of ordinary words; it is found however it is joined, its parts alone pass
        denied = {leak_scan.digest('north-wind')}
        text = 'the north-wind rollout, NORTH_WIND_ROOT, north.wind.example, ex-north-wind-ops'
        self.assertEqual([(r, h[1:]) for r, h in self.findings(text, denied)], [('denylist', '…(10 chars)')] * 4)
        self.assertEqual(self.findings('north wind, north-west wind, wind-north', denied), [])

    def test_known_history_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'known.txt'
            path.write_text('# released\n0123456789ab ' + leak_scan.digest('north-wind') + '\n\n')
            self.assertEqual(leak_scan.load_known(path), {('0123456789ab', leak_scan.digest('north-wind'))})
            path.write_text('0123456789ab north-wind\n')                 # a word, not its hash: refused
            with self.assertRaises(ValueError):
                leak_scan.load_known(path)
            self.assertEqual(leak_scan.load_known(Path(tmp) / 'none.txt'), set())

    def test_real_looking_chat_ids(self):
        self.assertEqual(self.findings('user ' + REAL_ID), [('chat-id', 'U…(10 chars)')])
        self.assertEqual(self.findings('channel ' + REAL_CHANNEL), [('chat-id', 'C…(11 chars)')])

    def test_synthetic_chat_ids_pass(self):
        for value in ('U0000TEST01', 'T0000TEST01', 'UFAKE12345', 'C00000000AB', 'UEXAMPLE12'):
            self.assertEqual(self.findings(value), [], value)

    def test_words_that_only_look_like_ids_pass(self):
        self.assertEqual(self.findings('CONSTANTS UNDERSCORE ABCDEFGHIJ SHA256SUM'), [])

    def test_home_paths(self):
        self.assertEqual(self.findings(HOME_MAC), [('home-path', 'q…(7 chars)')])
        self.assertEqual(self.findings(HOME_LINUX), [('home-path', 'q…(7 chars)')])
        self.assertEqual(self.findings('/Users/example/x and /home/runner/work'), [])

    def test_email_addresses(self):
        self.assertEqual([r for r, _ in self.findings('mail ' + MAIL)], ['email'])
        self.assertEqual(self.findings('mira@northwind.example and a@b.test and x@example.com'), [])
        self.assertEqual(self.findings('Co-Authored-By: Bot <noreply@vendor.io>'), [])

    def test_matched_text_is_never_printed(self):
        hints = ' '.join(h for _, h in self.findings(f'{MAIL} {REAL_ID} {HOME_MAC}'))
        for secret in ('quillon', 'northwind', REAL_ID):
            self.assertNotIn(secret, hints)

    def test_denylist_file_holds_only_hashes(self):
        hashes = leak_scan.load_denylist()
        self.assertGreater(len(hashes), 0)
        self.assertTrue(all(len(h) == 64 for h in hashes))

    def test_the_repository_itself_is_clean(self):
        if not (ROOT / '.git').exists():
            self.skipTest('not a git checkout')
        out = subprocess.run(['python3', str(ROOT / 'tools/leak_scan.py'), '--tree'], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_usage_errors(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(leak_scan.main([]), 2)
            self.assertEqual(leak_scan.main(['--tree', 'some/path']), 2)


class BundleTest(unittest.TestCase):
    def setUp(self):
        if shutil.which('git') is None:
            self.skipTest('git is not installed')
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / 'repo'
        (self.repo / 'kage').mkdir(parents=True)
        (self.repo / 'kage/__init__.py').write_text("__version__ = '9.9.9'\n", encoding='utf-8')
        (self.repo / 'kage/core.py').write_text('X = 1\n', encoding='utf-8')
        (self.repo / 'README.md').write_text('not bundled\n', encoding='utf-8')
        env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}
        for cmd in (['init', '-q'], ['add', '-A'], ['commit', '-qm', 'x'], ['tag', 'v9.9.9']):
            subprocess.run(['git', '-C', str(self.repo), *cmd], check=True, env=env, capture_output=True)
        self.saved_root = bundle.ROOT
        bundle.ROOT = self.repo

    def tearDown(self):
        bundle.ROOT = self.saved_root
        shutil.rmtree(self.tmp)

    def build(self):
        out = self.tmp / 'out'
        with contextlib.redirect_stdout(io.StringIO()):
            bundle.build('v9.9.9', out)
        [tarball] = sorted(out.glob('*.tar.gz'))
        return tarball

    def extract(self, tarball):
        dest = self.tmp / 'x'
        with tarfile.open(tarball) as tar:
            bundle.extract(tar, dest)  # the tool's own extraction: the 'data' filter, or its 3.9 equivalent
        [root] = list(dest.iterdir())
        return root

    def test_build_is_deterministic_and_verifies(self):
        first = self.build().read_bytes()
        second = self.build().read_bytes()
        self.assertEqual(first, second)

    def test_bundle_layout(self):
        root = self.extract(self.build())
        self.assertTrue(root.name.startswith('kage-9.9.9-'))
        self.assertEqual(sorted(p.name for p in root.iterdir()), ['BUNDLE.json', 'MANIFEST.sha256', 'kage'])
        meta = json.loads((root / 'BUNDLE.json').read_text())
        self.assertEqual(meta['version'], '9.9.9')
        self.assertEqual(meta['ref'], 'v9.9.9')
        self.assertEqual(len(meta['commit']), 40)
        self.assertEqual(bundle.verify(root), [])

    def test_verify_catches_a_changed_file(self):
        root = self.extract(self.build())
        (root / 'kage/core.py').write_text('X = 2\n', encoding='utf-8')
        self.assertTrue(bundle.verify(root))

    def test_verify_catches_an_extra_file(self):
        root = self.extract(self.build())
        (root / 'kage/extra.py').write_text('', encoding='utf-8')
        self.assertTrue(bundle.verify(root))

    def test_verify_catches_a_changed_manifest(self):
        root = self.extract(self.build())
        manifest = root / 'MANIFEST.sha256'
        manifest.write_text(manifest.read_text().replace('kage/core.py', 'kage/core2.py'))
        self.assertTrue(bundle.verify(root))

    def test_verify_ignores_bytecode_caches(self):
        root = self.extract(self.build())
        (root / 'kage/__pycache__').mkdir()
        (root / 'kage/__pycache__/core.cpython-310.pyc').write_bytes(b'x')
        self.assertEqual(bundle.verify(root), [])


if __name__ == '__main__':
    unittest.main()
