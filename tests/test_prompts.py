import sqlite3
import types
import unittest
from pathlib import Path

from kage.core import prompts as P


def runtime(**over):
    """A fake runtime: only what the tested methods reach through `rt`."""
    claude = types.SimpleNamespace(provider=lambda w: w.get('provider') or 'codex')
    worker_config = types.SimpleNamespace(load=lambda: ({'intake': {'inbox_task': 'owner-inbox'}}, None),
                                          DEFAULTS={'intake': {'inbox_task': 'owner-inbox'}})
    return types.SimpleNamespace(
        __file__='/nonexistent/runtime.py', SKILL=Path('/nonexistent/skill'), INBOX_KIND='task_inbox', claude=claude,
        worker_config=worker_config, local_message=lambda db, eid: None, decision_annotation=lambda db, eid: '',
        envelope_annotation=lambda db, eid: '', card_replies=types.SimpleNamespace(routed=lambda db, c, t: None),
        load=lambda db, wid: {'channel': 'C0000TEST01', 'thread': None, 'mode': 'waiting', 'cwd': '/tmp/x'}, **over)


def playbook(seen):
    def dispatch_rules(**values):
        seen.append(values)
        return ['1. A rule of the owner.', f'2. Ask with `{values["cli"]} ask-owner`.']
    return types.SimpleNamespace(dispatch_rules=dispatch_rules)


def core(language='en', seen=None, rt=None):
    return P.PromptsCore(language, owner_key='owner', owner_name='Mira', workspace='T0000TEST01',
                         owner_slack_user='U0000TEST01', run_context_env='TEST_RUN_CONTEXT', rt=rt or runtime(),
                         pack=playbook([] if seen is None else seen))


class PromptsTest(unittest.TestCase):
    def test_the_dispatch_prompt_takes_the_owner_rules_from_the_pack(self):
        seen = []
        c = core(seen=seen)
        dispatch = {'task_id': 'task-a', 'task_key': 'task-a', 'task_path': '/tmp/a', 'trigger': 'event',
                    'dispatch_id': 'd1'}
        step = {'provider': 'claude', 'session': None, 'mode': 'fresh'}
        instructions, context = c.dispatch_prompt_parts(None, '/tmp/db', task=None, dispatch=dispatch, step=step,
                                                        run_id='r1', events=[], previous=[], context_file=True)
        lines = instructions.splitlines()
        self.assertEqual(lines[4:6], ['1. A rule of the owner.',
                                      '2. Ask with `python3 /nonexistent/runtime.py --db /tmp/db ask-owner`.'])
        self.assertEqual(seen[0]['inbox'], 'owner-inbox')
        self.assertEqual(seen[0]['sid'], '"$CLAUDE_CODE_SESSION_ID"')
        self.assertIn('`cat "$TEST_RUN_CONTEXT"`', instructions)                 # the deployment's env name
        self.assertTrue(context.startswith(P.RUN_CONTEXT_HEAD))
        self.assertIn('Taking over from: no live consumer.', context)            # run-dependent text in the file
        self.assertIn('trust=owner with authority=owner_intent is Mira hand-typed', instructions)

    def test_queued_and_local_prompts_in_the_owner_words(self):
        c, zh = core(), core('zh-CN')
        w = {'id': 'w1', 'session': 's1', 'cwd': '/tmp/x', 'channel': 'C0000TEST01', 'thread': None, 'mode': 'waiting',
             'provider': 'codex'}
        e = {'id': 'e1', 'channel': 'C0000TEST01', 'ts': '1.000001', 'author': 'U0000TEST02'}
        text = c.event_prompt(w, [e], '/tmp/db')
        self.assertIn("Mira's reply that starts with a clear approve / no", text)
        self.assertIn('- event=e1 ref=T0000TEST01/C0000TEST01/1.000001 author=U0000TEST02', text)
        self.assertIn('批准 / 不行', zh.event_prompt(w, [e], '/tmp/db'))
        db = sqlite3.connect(':memory:')
        local = {'path': '/nonexistent/note.md', 'kind': 'intake', 'sender': 'x', 'task_id': 't', 'created_at': 0}
        rt = runtime(envelopes=types.SimpleNamespace(lookup=lambda db, eid: None), intake_burst_ref=lambda db, eid: None,
                     task_memory=types.SimpleNamespace(MAX_TEXT=10), routines=types.SimpleNamespace(ROUTINE_INSTRUCTION=''))
        out = core(rt=rt).local_event_context(db, e, w, local, 5)
        self.assertIn('(owner-inbox / owner-oncall: triage policy', out['instruction'])
        self.assertIsNone(out['text'])


if __name__ == '__main__':
    unittest.main()
