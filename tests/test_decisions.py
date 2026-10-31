import json
import unittest

from kage.adapters.slack import decisions as slack_decisions
from kage.core import decisions as core

EN, ZH = core.ReplyReader('en'), core.ReplyReader('zh-CN')


def model(language='en', specs=None, recommend=None):
    stored = core.validate_options(specs or [], recommend, slack_decisions.clean_label)
    return core.card_model(stored, core.default_pair(language))


class OptionsTest(unittest.TestCase):
    def test_default_pair_in_the_owner_language(self):
        self.assertEqual([o['label'] for o in model('en')], ['Approve', 'Reject'])
        self.assertEqual([o['label'] for o in model('zh-CN')], ['批准', '拒绝'])
        self.assertEqual([o['choice'] for o in model('en')], ['approve', 'reject'])

    def test_specs(self):
        options = model(specs=['ship=Ship it:approve', 'wait=Wait a day', 'drop=Drop it:reject'], recommend='wait')
        self.assertEqual([(o['id'], o['kind'], o['recommended'], o['choice']) for o in options],
                         [('ship', 'approve', False, 'opt:ship'), ('wait', 'other', True, 'opt:wait'),
                          ('drop', 'reject', False, 'opt:drop')])
        self.assertEqual(core.parse_option('a=Label: with colon'), ('a', 'Label: with colon', 'other'))
        for specs, recommend, code in ((['a=A'], None, '2_to_4'), (['a=A', 'a=B'], None, 'ids_must_be_unique'),
                                       (['a=A', 'b=A'], None, 'labels_must_be_unique'), (['A=x', 'b=y'], None, 'option_id'),
                                       (['a=A', 'b=B'], 'c', 'recommend_must_match'), ([], 'a', 'requires_scenario'),
                                       (['a=<@U0000TEST01>', 'b=B'], None, '1_to_30'), (['nolabel', 'b=B'], None, 'ID=LABEL')):
            with self.assertRaisesRegex(ValueError, code):
                core.validate_options(specs, recommend, slack_decisions.clean_label)

    def test_stored_formats_and_choices(self):
        legacy = core.card_model(json.dumps(['Later']), core.default_pair('en'))
        self.assertEqual([o['choice'] for o in legacy], ['approve', 'reject', 'opt:1'])
        self.assertEqual(core.normalize_choice('reject', legacy), 'reject')
        self.assertEqual(core.normalize_choice('1', legacy), 'opt:1')
        self.assertEqual(core.resolve_choice('opt:1', legacy)['label'], 'Later')
        with self.assertRaises(ValueError):
            core.resolve_choice('opt:9', legacy)
        self.assertEqual(core.public_option(legacy[0]), {'id': 'approve', 'label': 'Approve', 'kind': 'approve',
                                                         'recommended': False})
        self.assertEqual(core.validate_note('  a \n b '), 'a b')
        self.assertIsNone(core.validate_note('   '))
        with self.assertRaises(ValueError):
            core.validate_note('x' * 301)


class ReplyTest(unittest.TestCase):
    def read(self, reader, text, options, strict=False):
        result = reader.parse_reply(text, options, strict)
        return (result['option'] or {}).get('id'), result['reason']

    def test_english_replies(self):
        m = model('en')
        self.assertEqual(self.read(EN, 'approve', m), ('approve', 'decided'))
        self.assertEqual(self.read(EN, 'Not approved', m), ('reject', 'decided'))   # longest phrase first
        self.assertEqual(self.read(EN, 'approve? not sure', m), (None, 'question'))
        self.assertEqual(self.read(EN, 'approve but only read access', m), (None, 'conditional'))
        self.assertEqual(self.read(EN, 'approve, no', m), (None, 'contradiction'))
        self.assertEqual(self.read(EN, 'approve, and tell Ola', m), ('approve', 'decided'))
        self.assertEqual(self.read(EN, 'approve, and tell Ola', m, strict=True), (None, 'verbatim_card_extra_text'))
        self.assertEqual(self.read(EN, 'approvement', m), (None, 'no_keyword'))

    def test_chinese_replies(self):
        m = model('zh-CN', ['a=先灰度:approve', 'b=全量:approve', 'c=不做:reject'], recommend='a')
        self.assertEqual(self.read(ZH, '按建议', m), ('a', 'decided'))
        self.assertEqual(self.read(ZH, '批准', m), ('a', 'decided'))       # several approve options: the recommended one
        self.assertEqual(self.read(ZH, '全量，今晚上', m), ('b', 'decided'))  # an exact label wins
        self.assertEqual(self.read(ZH, '不批准', m), ('c', 'decided'))
        self.assertEqual(self.read(ZH, '批准，可以吗', m), (None, 'question'))
        self.assertEqual(self.read(ZH, '批准吗', m), (None, 'no_keyword'))   # 吗 is not a separator
        self.assertEqual(self.read(ZH, '批准，但是只给只读', m), (None, 'conditional'))

    def test_slack_markup(self):
        replies = slack_decisions.SlackReplies(EN)
        result = replies.parse_reply('<@U0000TEST01> approve', model('en'))
        self.assertEqual((result['option']['id'], result['reason']), ('approve', 'decided'))
        self.assertEqual(replies.reply_class('see <https://x.example/?a=1|the doc>, cancel it'), 'plan_change')
        self.assertEqual(replies.reply_class('should we cancel it?'), 'kept')
        self.assertEqual(slack_decisions.SlackReplies(ZH).reply_class('改成只读'), 'plan_change')
        self.assertEqual(slack_decisions.SlackReplies(ZH).reply_class('要不要改成只读？'), 'kept')

    def test_excerpt(self):
        self.assertEqual(slack_decisions.excerpt('<@U0000TEST01> see <https://x.example|doc> &amp; ok'),
                         '@… see doc &amp; ok')
        self.assertEqual(slack_decisions.excerpt('x' * 50, limit=10), 'x' * 10 + '…')
        self.assertEqual(slack_decisions.excerpt('&lt;b&gt;'), '&lt;b&gt;')   # Slack sends text escaped
        self.assertEqual(slack_decisions.excerpt('<b>'), '…')                # a raw <…> is Slack markup


if __name__ == '__main__':
    unittest.main()
