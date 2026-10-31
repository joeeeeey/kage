import contextlib
import sqlite3
import types
import unittest

from kage.core import escalation as E


def deployment(tz='Europe/Lisbon'):
    owner = types.SimpleNamespace(timezone=tz, slack_workspace='T0000TEST01', assistant_slack_app='A0000TEST01')
    return types.SimpleNamespace(owner=owner)


def core(language='en', **rt):
    runtime = types.SimpleNamespace(transaction=contextlib.nullcontext, **rt)
    return E.EscalationCore(language, owner_key='owner', owner_name='Mira', owner_slack_user='U0000TEST01',
                            bot_user='U0000TEST02', deployment=deployment(), agent_event_type='test_agent_event',
                            retire_checked={}, rt=runtime)


class EscalationTest(unittest.TestCase):
    def test_card_lines_in_the_owner_language_and_time_zone(self):
        c, zh = core(), core('zh-CN')
        self.assertEqual(c.ask_owner_closed_line.format(clock='10:00'), '⏹ Closed, nothing to do · 10:00 (Europe/Lisbon)')
        self.assertTrue(zh.ask_owner_closed_line.startswith('⏹ 已关闭'))
        self.assertEqual(c.superseded_line, '⏭ Taken over by a newer card')
        self.assertEqual(c.team, 'T0000TEST01')

    def test_roots_and_stops_through_the_runtime(self):
        ensured, saved, resolved = [], [], []
        watch = {'id': 'w1', 'state': 'running', 'provider': 'claude', 'session': 's1'}
        c = core(registry=types.SimpleNamespace(ensure_task=lambda db, task, now: ensured.append(task)),
                 load=lambda db, wid: dict(watch), save=lambda db, w: saved.append(w),
                 claude=types.SimpleNamespace(provider=lambda w: w['provider'], resolved=lambda db, s: resolved.append(s)))
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE approvals_roots (channel, root_ts, task_id, home, created, PRIMARY KEY (channel, root_ts))')
        c._register_root(db, 'C0000TEST01', '1.000001', 'task-a', 5.0)
        c._register_root(db, 'C0000TEST01', '1.000001', 'task-a', 6.0, home=True)
        self.assertEqual(db.execute('SELECT task_id, home, created FROM approvals_roots').fetchall(), [('task-a', 1, 5.0)])
        self.assertEqual(ensured, ['task-a', 'task-a'])
        self.assertTrue(c._stop_approvals_watch(db, 'w1', 'kill_switch', 7.0))
        self.assertEqual((saved[0]['state'], saved[0]['stop_reason'], resolved), ('stopped', 'kill_switch', ['s1']))


    def test_owner_keyed_keywords_reach_the_generic_parameter(self):
        c = E.EscalationCore('en', owner_key='mira', owner_name='Mira', owner_slack_user='U0000TEST01',
                             bot_user='U0000TEST02', deployment=deployment(), agent_event_type='x', retire_checked={},
                             rt=types.SimpleNamespace())
        with self.assertRaises(ValueError):  # not a TypeError: mira_only= is the owner_only parameter
            c.ask_owner(None, '/tmp/db', 'not-a-uuid', 'codex', '/tmp', 'q', mira_only=True)


if __name__ == '__main__':
    unittest.main()
