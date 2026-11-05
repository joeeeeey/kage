import sqlite3
import unittest
import zoneinfo

from kage.core import blockers as b

TZ = zoneinfo.ZoneInfo('Asia/Shanghai')


def blockers(language='en', key='owner', name='Mira'):
    return b.Blockers(language, tz=TZ, tz_name='Asia/Shanghai', owner_key=key, owner_name=name,
                      owner_user=lambda rt: 'U0000TEST01', ask_owner=lambda rt: rt.ask, worker_slug='test-worker',
                      run_id_env='TEST_RUN_ID')


class BlockersTest(unittest.TestCase):
    def test_gap_proposals_are_written_in_the_owners_language(self):
        # O1d (G2): the gate passes an English owner's English texts (the next check, the slug, refuses here) and keeps
        # a Chinese owner's Han requirement with the same error as before
        from unittest import mock

        def items(c, title, rationale, brief):
            doc = {'schema': c.proposals_schema_id, 'proposals': [{'system': 'wiki', 'operation': 'add a page',
                                                                    'title_zh': title, 'rationale_zh': rationale,
                                                                    'brief_zh': brief}]}
            c._proposal_items(mock.MagicMock(), None, doc, {'max_skill_proposals_per_run': 3}, 0)

        english = ('Wiki page skill', 'Pages are added by hand every week', 'Build a skill that adds a wiki page.')
        with self.assertRaisesRegex(ValueError, 'slug'):
            items(blockers('en'), *english)
        with self.assertRaisesRegex(ValueError, '^proposal_0_title_rationale_and_brief_must_be_in_the_owner_language$'):
            items(blockers('en'), '42', *english[1:])
        with self.assertRaisesRegex(ValueError, '^proposal_0_title_rationale_and_brief_must_be_chinese$'):
            items(blockers('zh-CN'), *english)
        with self.assertRaisesRegex(ValueError, 'slug'):
            items(blockers('zh-CN'), 'wiki 页面技能', '每周手工加页面', '做一个加 wiki 页面的技能。')

    def test_owner_bound_values(self):
        B = blockers()
        self.assertIn('owner_manual', B.resolutions)
        self.assertEqual(B.owner_manual, ('owner_manual', 'credential_added'))
        self.assertIn('ask-owner', B.internal_systems)
        self.assertIn('test-worker', B.internal_systems)
        self.assertNotIn('mira-runtime', B.internal_systems)
        named = b.Blockers('en', tz=TZ, tz_name='Asia/Shanghai', owner_key='owner', owner_name='Mira',
                           owner_user=lambda rt: 'U0000TEST01', ask_owner=lambda rt: rt.ask, worker_slug='test-worker',
                           run_id_env='TEST_RUN_ID', internal_names=('mira-runtime',))
        self.assertIn('mira-runtime', named.internal_systems)        # N1: the deployment's own names for the runtime
        self.assertTrue(named._is_internal_bookkeeping('mira-runtime', 'anything'))
        self.assertEqual(B.proposals_schema_id, 'test-worker.gap-proposals/v1')
        self.assertIn('owner_manual INTEGER NOT NULL DEFAULT 0', B.ddl)
        self.assertEqual(B.resolution_zh['owner_manual'], 'Mira did it by hand')
        self.assertTrue(B.stage1_next['manual'].startswith('Mira will do it himself'))
        db = sqlite3.connect(':memory:')
        B.initialize(db)
        self.assertIn('owner_manual', [r[1] for r in db.execute('PRAGMA table_info(automation_gaps)')])

    def test_internal_bookkeeping_and_patterns(self):
        zh = blockers('zh-CN')
        self.assertTrue(zh._is_internal_bookkeeping('ask-owner', 'anything'))
        self.assertTrue(zh._is_internal_bookkeeping('google', '取消我之前设的提醒卡'))
        self.assertFalse(zh._is_internal_bookkeeping('google', '取消 Google Calendar 里的会议'))
        self.assertTrue(zh._runtime_noun.search('ask-owner'))
        self.assertFalse(zh._runtime_noun.search('ask-someone'))
        self.assertEqual(zh.type_zh['mfa_code'], '需要验证码')

    def test_card_text(self):
        row = {'system': 'google-workspace', 'operation': 'reset a password', 'type': 'mfa_code', 'attempt': 2,
               'unblock': 'a code', 'id': 'bk0000000000000a', 'options': '[]', 'task_id': 'task-a', 'smoke': 0,
               'previous_id': None, 'tried': 'the API'}
        text = blockers('zh-CN').card_question(row, previous={'choice': 'manual', 'options': '[]'})
        self.assertTrue(text.startswith('agent 在 google-workspace 上做「reset a password」卡住了'), text)
        self.assertIn('验证码直接回在这条 thread 里', text)
        self.assertTrue(blockers().card_question(row).startswith('An agent is stuck on "reset a password" on google-workspace'))
        self.assertTrue(blockers().waiting_on_owner({'status': 'open', 'choice': None, 'stage2_state': None,
                                                     'resolution': None}))


if __name__ == '__main__':
    unittest.main()
