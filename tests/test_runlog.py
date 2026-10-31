import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from kage.core import runlog

TZ = dt.timezone(dt.timedelta(hours=8))
ZH, EN = runlog.RunLog('zh-CN', tz=TZ), runlog.RunLog('en', tz=TZ)
T0 = dt.datetime(2026, 7, 1, 14, 0, tzinfo=TZ).timestamp()


def jsonl(*events):
    return ''.join(json.dumps(e) + '\n' for e in events)


CODEX = [{'type': 'thread.started', 'thread_id': '0199aaaa-bbbb'},
         {'type': 'item.started', 'item': {'id': 'c1', 'type': 'command_execution', 'command': 'git status'}},
         {'type': 'item.completed', 'item': {'id': 'c1', 'type': 'command_execution', 'command': 'git status',
                                             'exit_code': 1, 'aggregated_output': 'x\nfatal: no repo\n'}},
         {'type': 'item.completed', 'item': {'id': 'f1', 'type': 'file_change', 'status': 'failed',
                                             'changes': [{'kind': 'add', 'path': 'a.py'}]}},
         {'type': 'item.completed', 'item': {'id': 't1', 'type': 'todo_list',
                                             'items': [{'text': 'a', 'completed': True}, {'text': 'b'}]}},
         {'type': 'turn.completed'}]
CLAUDE = [{'type': 'system', 'subtype': 'init', 'session_id': 'S1234567890', 'model': 'opus'},
          {'type': 'system', 'subtype': 'api_retry', 'attempt': 2, 'error_status': 529, 'error': 'overloaded'},
          {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'u1', 'name': 'Bash',
                                                         'input': {'command': 'ls'}}]}},
          {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'u1', 'is_error': True,
                                                    'content': 'Exit code 2\nno such file'}]}},
          {'type': 'system', 'subtype': 'compact_boundary'},
          {'type': 'result', 'subtype': 'success', 'num_turns': 3, 'duration_ms': 125000, 'result': 'done'}]


class TextTest(unittest.TestCase):
    def test_durations_and_labels(self):
        self.assertEqual([ZH.duration(s) for s in (0, 59, 60, 61, 3600, 3725)],
                         ['0 秒', '59 秒', '1 分', '1 分 1 秒', '1 小时 0 分', '1 小时 2 分'])
        self.assertEqual(EN.duration(3725), '1h 2m')
        entry = {'at': T0, 'kind': 'cmd', 'text': '$ ls'}
        self.assertEqual(ZH.format_entry(entry), '14:00:00 · 命令 · $ ls')
        self.assertEqual(EN.summary_of(entry), 'command: $ ls')
        self.assertTrue(ZH.is_step_line('14:00:00 · 命令 · $ ls'))
        self.assertFalse(ZH.is_step_line('14:00:00 · 开始 · x'))

    def test_codex_and_claude_rendering(self):
        lines, renderer = ZH.render_text('codex', jsonl(*CODEX), seen_at=T0)
        self.assertEqual(lines, ['14:00:00 · 开始 · Codex 会话 0199aaaa 开始', '14:00:00 · 命令 · $ git status',
                                 '14:00:00 · 退出 · exit 1 · fatal: no repo', '14:00:00 · 改文件 · add a.py （失败）',
                                 '14:00:00 · 待办 · 1/2 完成 · 下一项：b', '14:00:00 · 结果 · 本轮完成'])
        self.assertEqual(renderer.snapshot()['steps'], 2)
        lines, renderer = EN.render_text('claude', jsonl(*CLAUDE), seen_at=T0)
        self.assertEqual(lines, ['14:00:00 · start · Claude session S1234567 started · opus',
                                 '14:00:00 · retry · API retry 2 · HTTP 529 · overloaded', '14:00:00 · command · $ ls',
                                 '14:00:00 · exit · exit 2 · no such file', '14:00:00 · note · context compacted',
                                 '14:00:00 · result · succeeded · 3 turns · 2m 5s · done'])
        self.assertEqual(renderer.markers, 1)
        self.assertEqual(ZH.render_text('codex', 'plain text\n{"broken', seen_at=T0)[0],
                         ['14:00:00 · 输出 · plain text', '14:00:00 · 输出 · {"broken'])


class MaskTest(unittest.TestCase):
    def test_masking(self):
        token = 'gh' + 'p_' + 'a1B2c3D4e5F6g7H8i9J0k'
        for text, gone in ((f'export GITHUB_TOKEN={token}', token), ('password: hunter2hunter', 'hunter2hunter'),
                           ('curl -H "Authorization: Bearer ' + 'abcd' * 4 + '"', 'abcd' * 4),
                           ('验证码 123456', '123456'), ('--token-code 654321', '654321'),
                           ('https://user:' + 'pa55word' + '@example.com', 'pa55word')):
            with self.subTest(text=text[:12]):
                self.assertNotIn(gone, runlog.mask(text))
        self.assertEqual(runlog.mask('keyboard monkey'), 'keyboard monkey')
        self.assertEqual(runlog.short('<@U0000TEST01> a\n\nb ' + 'x' * 300, 20), 'a b ' + 'x' * 15 + '…')


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp())
        os.chmod(self.run, 0o700)

    def tearDown(self):
        shutil.rmtree(self.run)

    def test_incremental_update_and_finish(self):
        events = self.run / runlog.EVENTS
        events.write_text(jsonl(*CODEX[:2]) + '{"type": "turn.')               # the last line is not complete yet
        state = ZH.update(self.run, 'codex', now=T0)
        self.assertEqual((state['lines'], state['last_summary']), (2, '命令：$ git status'))
        with events.open('a') as stream:
            stream.write('completed"}\n')
        state = ZH.update(self.run, now=T0)
        self.assertEqual((state['lines'], state['provider']), (3, 'codex'))
        done = ZH.finish(self.run, {'exit_code': 0, 'started_at': T0 - 90, 'ended_at': T0}, now=T0)
        again = ZH.finish(self.run, {'exit_code': 0}, now=T0)
        self.assertTrue(done['finished'] and again['finished'])
        lines = runlog.read_lines(self.run)
        self.assertEqual(lines[-1], '14:00:00 · 结束 · exit 0 · 用时 1 分 30 秒')
        self.assertEqual(len(lines), 4)                                          # the end line exactly once
        self.assertEqual(runlog.read_from(self.run / runlog.READABLE, 0)[0].count('\n'), 4)

    def test_an_oversized_line_is_skipped(self):
        (self.run / runlog.EVENTS).write_bytes(b'x' * runlog.MAX_CHUNK)
        state = EN.update(self.run, 'claude', now=T0)
        self.assertEqual(runlog.read_lines(self.run), ['14:00:00 · note · (skipped one oversized output line)'])
        self.assertEqual(state['offset'], runlog.MAX_CHUNK)


if __name__ == '__main__':
    unittest.main()
