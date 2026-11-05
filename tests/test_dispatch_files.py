"""O1c5b: the files a generated deployment's dispatched runs read — SKILL.md, references/ and the dispatch runner —
exist where the assembled units look for them, carry only the owner's own values, and work: the example spec is
valid for the rule engine, the registry seed holds no rule, the runner runs one command on the pinned release and
refuses an altered one."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import unittest
from unittest import mock

from kage import app
from kage.onboard import dispatch_files

from tests.test_init import Base, OWNER


class DispatchFilesTest(Base):
    def setUp(self):
        super().setUp()
        self.records()
        self.init()

    def units(self, *names):
        a = app.App(self.target / 'deploy')
        with mock.patch.dict(os.environ, {'HOME': str(self.home)}):
            return {name: a.leaf(name) for name in names}

    def test_where_the_units_look(self):
        t = self.target
        u = self.units('dispatcher', 'rule_engine', 'rule_registry')
        for found, rel in ((u['dispatcher'].RUNNER, dispatch_files.RUNNER),
                           (u['rule_engine'].EXAMPLE_SPEC, dispatch_files.EXAMPLE_SPEC),
                           (u['rule_registry'].SEED, 'references/rule-registry-seed')):
            self.assertEqual(Path(found).resolve(), (t / rel).resolve())
        for rel in (dispatch_files.SKILL, dispatch_files.RULES, dispatch_files.EXAMPLE_SPEC, dispatch_files.SEED_README,
                    dispatch_files.RUNNER):
            self.assertTrue((t / rel).is_file(), rel)
        self.assertEqual(stat.S_IMODE((t / dispatch_files.RUNNER).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((t / dispatch_files.SKILL).stat().st_mode), 0o600)

    def test_only_the_owners_values(self):
        skill = (self.target / 'SKILL.md').read_text()
        self.assertIn("Mira's Kage", skill)
        self.assertIn('ask-mira', skill)
        self.assertIn('trust=mira', skill)
        self.assertIn('python3 scripts/kage.py <command>', skill)
        rules = (self.target / dispatch_files.RULES).read_text()
        self.assertIn('kage/mira.rules-plan/v1', rules)
        for text in (skill, rules):
            self.assertFalse(dispatch_files.MARKER_RE.search(text))
        with self.assertRaisesRegex(ValueError, 'template_marker_left'):
            dispatch_files._render('rules.md', {'key': 'mira'})

    def test_the_example_spec_is_valid_and_asks_the_owner(self):
        spec = json.loads((self.target / dispatch_files.EXAMPLE_SPEC).read_text())
        self.assertEqual(spec['defaults']['owner'], OWNER)
        policy = self.units('rule_engine')['rule_engine'].ACCESS_POLICY
        self.assertEqual(policy.spec_problems(spec), [])
        self.assertEqual(policy.plan_schema_id, 'kage/mira.rules-plan/v1')
        self.assertEqual({rule['level'] for rule in spec['rules']}, {'mira'})
        self.assertEqual({shape['level'] for shape in spec['never_auto'].values()}, {'mira'})

    def test_the_registry_seed_holds_no_rule(self):
        registry = self.units('rule_registry')['rule_registry'].REGISTRY
        view = registry.load(self.target / 'references/rule-registry-seed')
        self.assertEqual((view['files'], view['problems']), ([], []))

    def runner(self, run_dir, *command, versions=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('SLACK_', 'KAGE_')) and k != 'PYTHONPATH'}
        env.update({'HOME': str(self.home), 'KAGE_VERSIONS_DIR': str(versions or self.release.parent),
                    'PYTHONDONTWRITEBYTECODE': '1'})
        return subprocess.run([sys.executable, '-I', str(self.target / dispatch_files.RUNNER), '--run-dir',
                               str(run_dir), '--', *command], capture_output=True, text=True, env=env, timeout=60,
                              stdin=subprocess.DEVNULL, cwd=str(self.tmp))

    def test_the_runner_runs_one_command_on_the_pinned_release(self):
        run_dir = self.tmp / 'run1'
        run_dir.mkdir(mode=0o700)
        done = self.runner(run_dir, sys.executable, '-c', 'print("hello from the agent")')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('hello from the agent', (run_dir / 'events.jsonl').read_text())
        self.assertEqual(json.loads((run_dir / 'exit.json').read_text())['exit_code'], 0)
        for name in ('events.jsonl', 'stderr.log', 'exit.json'):
            self.assertEqual(stat.S_IMODE((run_dir / name).stat().st_mode), 0o600, name)

    def test_the_runner_refuses_an_altered_release(self):
        (self.release / 'kage/core/runner.py').write_text('raise SystemExit(0)\n')
        run_dir = self.tmp / 'run2'
        run_dir.mkdir(mode=0o700)
        refused = self.runner(run_dir, sys.executable, '-c', 'print(1)')
        self.assertEqual(refused.returncode, 78, refused.stderr)
        self.assertIn('file_hash_mismatch:kage/core/runner.py', refused.stderr)
        self.assertFalse((run_dir / 'events.jsonl').exists())
        missing = self.runner(run_dir, sys.executable, '-c', 'print(1)', versions=self.tmp / 'nowhere')
        self.assertEqual(missing.returncode, 78)
        self.assertIn('release_unreadable', missing.stderr)

    def test_a_rerun_with_the_same_answers_changes_no_dispatch_file(self):
        before = {rel: (self.target / rel).read_bytes() for rel in dispatch_files.files(
            key='mira', name='Mira', owner_user=OWNER, version='x', program='scripts/kage.py')}
        report = self.init()
        self.assertFalse(report['applied'])
        self.assertEqual({rel: (self.target / rel).read_bytes() for rel in before}, before)
        self.assertTrue(all(re.fullmatch(r'[A-Za-z0-9_./-]+', rel) for rel in before))


if __name__ == '__main__':
    unittest.main()
