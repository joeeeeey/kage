import json
from pathlib import Path
import shutil
import tempfile
import unittest

from kage.adapters.providers import claude, codex
from kage.ports import provider as port

CLAUDE, CODEX = claude.Claude(), codex.Codex()
PROFILE = {'class': 'inbox', 'enabled': True, 'skills': 'names', 'keep_skills': ['b'], 'git_context': False,
           'cache_ttl': '1h', 'auto_memory': False, 'mcp': 'none', 'drop_tools': ['WebSearch'],
           'rules_in_system_prompt': True}


def jsonl(*events):
    return '\n'.join(json.dumps(e) for e in events)


class CommandTest(unittest.TestCase):
    def test_claude(self):
        cmd = CLAUDE.build_command('resume', 'S1', 'do it', 'opus', 'high', ('--dangerously-skip-permissions',), '/r',
                                   settings={'a': 1}, extra_args=('--strict-mcp-config',), system_prompt='rules',
                                   disallowed=('WebSearch', 'AskUserQuestion'))
        self.assertEqual(cmd, ['claude', '-p', 'do it', '--resume', 'S1', '--output-format', 'stream-json', '--verbose',
                               '--model', 'opus', '--effort', 'high', '--dangerously-skip-permissions',
                               '--strict-mcp-config', '--append-system-prompt', 'rules', '--settings', '{"a":1}',
                               '--disallowedTools', 'AskUserQuestion', 'ExitPlanMode', 'WebSearch'])
        self.assertEqual(CLAUDE.build_command('fresh', 'S2', 'p', 'm', 'e', (), '/r')[3:5], ['--session-id', 'S2'])

    def test_codex(self):
        self.assertEqual(CODEX.build_command('fresh', None, 'go', 'gpt', 'xhigh', ('--yolo',), '/runs/r1'),
                         ['codex', 'exec', '--yolo', '--skip-git-repo-check', '--json', '-o', '/runs/r1/last.md', '-m',
                          'gpt', '-c', 'model_reasoning_effort="xhigh"', 'go'])
        self.assertEqual(CODEX.build_command('resume', 'T1', 'go', 'gpt', 'low', (), '/r')[-2:], ['T1', 'go'])

    def test_hook_settings_and_layout(self):
        hook = claude.claude_settings('/py', '/x/runtime.py', '/db/state.sqlite', 'r1')['hooks']['PreToolUse'][0]
        self.assertEqual(hook['matcher'], 'AskUserQuestion|ExitPlanMode')
        self.assertTrue(hook['hooks'][0]['command'].endswith('dispatch-hook --run-id r1'))
        self.assertEqual([CLAUDE.prompt_layout(PROFILE, m) for m in ('fresh', 'resume')], ['split', 'single'])
        self.assertEqual(CLAUDE.prompt_layout({**PROFILE, 'first_message': 'constant'}, 'fresh'), 'constant')
        self.assertEqual(CODEX.prompt_layout(PROFILE, 'fresh'), 'single')


class LeanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        home = self.tmp / 'home' / '.claude' / 'skills'
        for name, front in (('a', 'name: a\n'), ('dir-b', 'name: "b"\n'), ('synced', 'name: s\n'), ('c', 'title: x\n')):
            (home / name).mkdir(parents=True)
            (home / name / 'SKILL.md').write_text(f'---\n{front}---\nbody\n')
        (self.tmp / 'home' / '.claude' / 'commands').mkdir()
        (self.tmp / 'home' / '.claude' / 'commands' / 'cmd.md').write_text('x')
        self.cwd = self.tmp / 'work'
        self.cwd.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_discover_and_lean(self):
        self.assertEqual(claude.discover_skills(self.cwd, self.tmp / 'home'), ['a', 'b', 'c', 'cmd'])
        settings, args, summary = CLAUDE.lean(PROFILE, self.cwd, home=self.tmp / 'home')
        self.assertEqual(settings, {'skillOverrides': {'a': 'name-only', 'b': 'on', 'c': 'name-only', 'cmd': 'name-only'},
                                    'includeGitInstructions': False, 'promptCacheTtl': '1h', 'autoMemoryEnabled': False})
        self.assertEqual((args, summary['skill_overrides'], summary['drop_tools']), (['--strict-mcp-config'], 4, 1))
        self.assertEqual(CLAUDE.lean({**PROFILE, 'skills': 'none', 'mcp': 'all'}, self.cwd)[1],
                         ['--disable-slash-commands'])

    def test_an_unreadable_skill_tree_only_costs_the_trim(self):
        def broken(cwd, home=None):
            raise RuntimeError('symlink loop')
        settings, _, summary = claude.Claude(discover=broken).lean(PROFILE, self.cwd)
        self.assertEqual((settings['skillOverrides'], summary['skills_error']), ({'b': 'on'}, 'RuntimeError'))


class ParseTest(unittest.TestCase):
    def test_claude_stream(self):
        text = jsonl({'type': 'system', 'subtype': 'init', 'session_id': 'S1'},
                     {'type': 'system', 'subtype': 'api_retry', 'error': 'overloaded', 'error_status': 529},
                     {'type': 'result', 'subtype': 'success', 'session_id': 'S1', 'result': 'done'}) + '\n{"partial'
        info = CLAUDE.parse_run_log(text, 'warn')
        self.assertEqual(info, {'session': 'S1', 'final': 'done', 'cli_ok': True, 'errors': ['HTTP 529', 'warn'],
                                'retry_categories': ['overloaded']})
        failed = CLAUDE.parse_run_log(jsonl({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True,
                                             'result': 'stopped', 'errors': ['x'], 'api_error_status': 500}))
        self.assertEqual((failed['cli_ok'], failed['errors']), (False, ['stopped', 'x', 'HTTP 500']))
        self.assertIsNone(CLAUDE.parse_run_log('')['cli_ok'])

    def test_codex_stream(self):
        text = jsonl({'type': 'thread.started', 'thread_id': 'T1'},
                     {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'hi'}},
                     {'type': 'turn.completed'})
        self.assertEqual(CODEX.parse_run_log(text)['final'], 'hi')
        self.assertEqual(CODEX.parse_run_log(text, last_message='from file')['final'], 'from file')
        failed = CODEX.parse_run_log(jsonl({'type': 'turn.failed', 'error': {'message': 'quota'}},
                                           {'type': 'error', 'message': 'boom'}))
        self.assertEqual((failed['cli_ok'], failed['errors']), (False, ['quota', 'boom']))
        self.assertEqual(list(port.events('x\n{"a": 1}\n{bad\n')), [{'a': 1}])


if __name__ == '__main__':
    unittest.main()
