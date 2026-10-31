import unittest

from kage.core import copy

ZH = copy.CopyStandard('zh-CN', owner_name='Mira', owner_key='mira')
EN = copy.CopyStandard('en', owner_name='Mira', owner_key='mira')


class CopyStandardTest(unittest.TestCase):
    def test_ids_and_jargon(self):
        for text, what in (('see AR-12', 'rule id'), ('Q-07 open', 'open-question'), ('rc0123456789abcd', 'record id'),
                           ('never-auto card', 'never-auto'), ('12345678-1234-1234-1234-123456789abc', 'uuid'),
                           ('deadbeefdeadbeef', 'hash')):
            with self.subTest(text=text):
                self.assertIn(what, ' '.join(ZH.problems(text)))
        self.assertEqual(ZH.problems('要你决定：是否合并这个改动'), [])
        self.assertEqual(ZH.problems('Q-123 and xAR-1'), [])                      # not an id shape

    def test_card_references_need_a_link(self):
        self.assertTrue(ZH.problems('去原审批卡上确认', refs=True))
        self.assertEqual(ZH.problems('去原审批卡上确认 https://example.test/x', refs=True), [])
        self.assertEqual(ZH.problems('任务卡住了，等你回复', refs=True), [])       # 卡住 is not a card
        self.assertEqual(ZH.problems('去原审批卡上确认'), [])                      # refs off: relayed text
        self.assertIn('points Mira to another card', ZH.problems('去那张卡片看看', refs=True)[0])
        self.assertTrue(EN.problems('Confirm it on the approval card', refs=True))
        self.assertEqual(EN.problems('Confirm it on the approval card: https://example.test/x', refs=True), [])

    def test_first_line(self):
        self.assertIn('first line is 121 characters', ZH.problems('x' * 121)[0])
        self.assertEqual(ZH.problems('x' * 121, first_line_max=0), [])
        self.assertEqual(ZH.problems('short\n' + 'x' * 300), [])

    def test_check_refuses_with_the_standard(self):
        with self.assertRaisesRegex(ValueError, r'^mira_facing_question_breaks_the_card_copy_standard: contains .*Mira'):
            ZH.check('AR-1 要你决定')
        with self.assertRaisesRegex(ValueError, r'^mira_facing_ask_breaks.*must start with what Mira has to do: 要你 / 请你'):
            ZH.check('看一下这个', field='ask', ask=True)
        self.assertEqual(ZH.check('要你决定：是否合并', ask=True), '要你决定：是否合并')
        self.assertEqual(EN.check('Please decide whether to merge', ask=True), 'Please decide whether to merge')
        own = copy.CopyStandard('en', owner_name='Mira', owner_key='mira', texts={'standard': 'Ask {owner} plainly.'})
        with self.assertRaisesRegex(ValueError, r'Ask Mira plainly\.$'):
            own.check('AR-1')

    def test_off_while_inactive(self):
        off = copy.CopyStandard('zh-CN', owner_name='Mira', owner_key='mira', active=lambda: False)
        self.assertEqual(off.check('AR-1 ' + 'x' * 300, ask=True), 'AR-1 ' + 'x' * 300)

    def test_scrub(self):
        self.assertEqual(copy.CopyStandard.scrub('AR-12 rc0123456789abcd done'), '… … done')
        self.assertEqual(copy.CopyStandard.scrub('y' * 130 + '\nnext'), 'y' * 119 + '…\nnext')


if __name__ == '__main__':
    unittest.main()
