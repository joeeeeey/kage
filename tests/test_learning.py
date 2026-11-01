import os
import sqlite3
import tempfile
import unittest
from unittest import mock
import zoneinfo

from kage.core import learning as lr


def learning(language='en'):
    return lr.Learning(language, tz=zoneinfo.ZoneInfo('Asia/Shanghai'), owner_key='owner', owner_name='Mira',
                       owner_slack_user='U0000TEST01', owner_user=lambda rt: 'U0000TEST01',
                       corpus_dir_env='TEST_FEEDBACK_CORPUS_DIR', default_corpus_dir='~/.local/share/test-owner/corpus',
                       smoke_scope='pilot')


class LearningTest(unittest.TestCase):
    def test_words_in_the_owners_language(self):
        en, zh = learning(), learning('zh-CN')
        self.assertEqual(en.promote_options[0], 'apply=Make this a rule:approve')
        self.assertEqual(zh.promote_options[0], 'apply=就按这条固化:approve')
        self.assertIn('owner-inbox', zh.promote_reply['apply'])     # the owner's inbox task, by key
        self.assertIn('{days}', en.promote_reply['drop'])           # formatted later with the cooling days
        self.assertEqual(en.card_type_label['ask_owner'], 'Ask Mira')
        self.assertIn('{signal}', zh.draft_prompt)                 # formatted later per record
        self.assertIn('Mira', en.draft_prompt)
        # the smoke rule's scope is the deployment's word (K15d), not the catalog's
        self.assertEqual(zh.msg('learning.smoke.3', smoke_scope=zh.smoke_scope), '（限 pilot 范围）')
        self.assertEqual(en.msg('learning.smoke.3', smoke_scope=en.smoke_scope), ' (limited to the pilot scope)')

    def test_directives_and_redaction(self):
        for L in (learning(), learning('zh-CN')):
            self.assertTrue(L.directive_re.search('以后这类都别再建 Linear'))
            self.assertTrue(L.directive_re.search('from now on do not open tickets for this'))
            self.assertIsNone(L.directive_re.search('ok, done'))
            text = L.redact('password: hunter2hunter2 and token xoxb-1234567890abcdef')
            self.assertNotIn('hunter2hunter2', text)
            self.assertNotIn('xoxb-1234567890abcdef', text)
            self.assertIn('[REDACTED]', text)
        self.assertEqual(learning('zh-CN')._redraft_guidance('改一下：范围只限测试环境'), '范围只限测试环境')

    def test_pure_helpers(self):
        self.assertEqual(lr.compute_override({'recommended': 'a', 'card_type': 'decision'}, 'b', 'other'), (True, 'a'))
        self.assertEqual(lr.compute_override({'recommended': None, 'card_type': 'rule_check'}, 'x', 'reject'),
                         (True, '(approve)'))
        self.assertEqual(lr.compute_override({'recommended': None, 'card_type': 'decision'}, 'x', 'reject'), (False, None))
        self.assertEqual(lr.shape_key('rule_check', 'infra', 'x'), lr.shape_key('rule_check', 'infra', 'x'))
        self.assertEqual(len(lr.shape_key('rule_check', None, None)), 12)

    def test_store_location_and_schema(self):
        L = learning()
        with mock.patch.dict(os.environ, {'TEST_FEEDBACK_CORPUS_DIR': '/tmp/test-corpus'}):
            self.assertEqual(str(L.corpus_dir()), '/tmp/test-corpus')
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TEST_FEEDBACK_CORPUS_DIR', None)
            self.assertTrue(str(L.corpus_dir()).endswith('/.local/share/test-owner/corpus'))
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(os.path.join(tmp, 'state.sqlite'))
            lr.initialize(db)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('interaction_records', tables)
            lr.initialize(db)                                       # idempotent
            db.close()


if __name__ == '__main__':
    unittest.main()
