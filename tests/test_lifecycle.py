import re
import unittest
import zoneinfo

from kage.core import lifecycle as lc

T0 = 1793400000.0


def lifecycle(language='en'):
    return lc.Lifecycle(language, tz=zoneinfo.ZoneInfo('Asia/Shanghai'), ticket_task_re=re.compile(r'^TEST-\d+(?:$|-)'),
                        ticket_id_re=re.compile(r'TEST-\d+'), owner_key='owner', owner_name='Mira',
                        owner_user=lambda rt: 'U0000TEST01')


class LifecycleTest(unittest.TestCase):
    def test_ticket_tasks_and_words(self):
        L = lifecycle()
        self.assertTrue(L.is_linear_task('TEST-12-move-the-store'))
        self.assertFalse(L.is_linear_task('local-task-20261031-x'))
        self.assertEqual(L._when(T0 - 7200, T0), '2 h ago')
        self.assertEqual(lifecycle('zh-CN')._when(T0 - 3 * 86400, T0), '3 天前')
        self.assertEqual(L._until(T0 + 3 * 3600, T0), '3 h left')
        self.assertEqual(L.orphan_options[1], 'agent=Let the agent keep going:approve')
        self.assertIn('Mira chose "Close it"', L.close_prompt)
        self.assertIn('{task}', L.close_prompt)                     # formatted later with the task id
        self.assertIn('Mira 在「任务停着没人推进」卡片上选了「关单」', lifecycle('zh-CN').close_prompt)

    def test_handoff_without_closure(self):
        zh = lifecycle('zh-CN')
        self.assertTrue(zh.handoff_without_closure('已经整理好。下一步：建议由你确认后再合并。'))
        self.assertFalse(zh.handoff_without_closure('任务已完成，Linear 已置 Done。'))
        self.assertFalse(zh.handoff_without_closure(''))
        self.assertTrue(lifecycle().handoff_without_closure('Next steps: someone should review the PR.'))

    def test_parse_due(self):
        L = lifecycle()
        self.assertEqual(L.parse_due('+2h', T0), T0 + 7200)
        with self.assertRaises(ValueError):
            L.parse_due('soon', T0)


if __name__ == '__main__':
    unittest.main()
