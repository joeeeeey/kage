import datetime as dt
import unittest

from kage.adapters.slack import cards
from kage.core import decisions

OWNER, OTHER = 'U0000TEST01', 'U0000TEST05'
TZ = dt.timezone(dt.timedelta(hours=8))
T = dt.datetime(2026, 7, 1, 20, 5, tzinfo=TZ).timestamp()


def make(language):
    pair = decisions.default_pair(language)
    return cards.DecisionCards(language, tz=TZ, tz_name='Asia/Shanghai', owner_user=OWNER,
                               default_model=lambda: decisions.card_model([], pair))


ZH, EN = make('zh-CN'), make('en')
OPTIONS = decisions.card_model([{'id': 'a', 'label': '先回滚', 'kind': 'approve', 'recommended': True},
                                {'id': 'b', 'label': '再等等', 'kind': 'other'},
                                {'id': 'c', 'label': '不做', 'kind': 'reject'}], decisions.default_pair('zh-CN'))


class CardTest(unittest.TestCase):
    def test_default_card(self):
        blocks = ZH.card_blocks('要你决定：是否批准', 'ev1')
        self.assertEqual([b['type'] for b in blocks], ['section', 'actions', 'context'])
        approve, reject = blocks[1]['elements']
        self.assertEqual((approve['text']['text'], approve['action_id'], approve['value'], approve['style']),
                         ('✅ 批准', 'sf_decision:approve', 'ev1', 'primary'))
        self.assertEqual((reject['text']['text'], reject['style']), ('❌ 拒绝', 'danger'))
        self.assertEqual(blocks[2]['block_id'], cards.HINT_BLOCK_ID)
        self.assertTrue(blocks[2]['elements'][0]['text'].startswith('💬'))

    def test_options_footer_hint_and_body(self):
        blocks = ZH.card_blocks('x', 'ev2', OPTIONS, footer='任务 · 面板', hint='h',
                                body_blocks=[{'type': 'header', 'text': {'type': 'plain_text', 'text': 'H'}}])
        self.assertEqual([b.get('block_id') for b in blocks],
                         [None, cards.ACTIONS_BLOCK_ID, cards.FOOTER_BLOCK_ID, cards.HINT_BLOCK_ID])
        texts = [e['text']['text'] for e in blocks[1]['elements']]
        self.assertEqual(texts, ['✅ 先回滚（推荐）', '🔹 再等等', '❌ 不做'])
        self.assertEqual([e.get('style') for e in blocks[1]['elements']], ['primary', None, 'danger'])
        self.assertEqual(blocks[3]['elements'][0]['text'], 'h')
        self.assertIsNone(ZH.card_blocks('x', 'e', hint=None)[-1]['elements'][0]['text'])   # passed as given
        self.assertEqual(ZH.code_owned_hint(OPTIONS), '💬 也可以在 thread 里直接回复“先回滚”或“再等等”或“不做”')
        self.assertEqual(EN.code_owned_hint(OPTIONS), '💬 You can also reply in the thread with "先回滚" or "再等等" or "不做"')

    def test_outcomes_and_receipts(self):
        approve = ZH.default_model()[0]
        self.assertEqual(ZH.outcome_text(approve, OTHER, T), f'✅ 已批准 · <@{OTHER}> · 20:05 (Asia/Shanghai)')
        self.assertEqual(ZH.outcome_text(OPTIONS[2], OTHER, T, note='a<b', provenance='thread_reply', excerpt='不做'),
                         f'❌ 已选择：不做（你在 thread 里回复了「不做」）（备注：a&lt;b） · <@{OTHER}> · 20:05 (Asia/Shanghai)')
        self.assertEqual(ZH.receipt_text(OTHER, OPTIONS[0], note='今晚'), f'<@{OTHER}> 选择了：先回滚（附条件：今晚）')
        self.assertEqual(ZH.receipt_text(OTHER, OPTIONS[0], provenance='thread_reply', guidance=True),
                         f'已按 <@{OTHER}> 在 thread 里的回复记为：✅ 先回滚；后面补充的话已交给 agent 照着办')
        self.assertEqual(EN.outcome_text(EN.default_model()[1], OTHER, T), f'❌ Rejected · <@{OTHER}> · 20:05 (Asia/Shanghai)')
        meta = cards.receipt_metadata('ev', 'opt:a', OTHER, option=OPTIONS[0], proposal_id='p1')
        self.assertEqual(meta['event_payload'], {'event_id': 'ev', 'choice': 'opt:a', 'actor': OTHER,
                                                 'provenance': 'slack_button', 'option_id': 'a',
                                                 'option_kind': 'approve', 'proposal_id': 'p1'})
        self.assertEqual(ZH.iso(T), '2026-07-01T20:05:00+08:00')

    def test_decided_and_retired(self):
        blocks = ZH.card_blocks('x', 'e', footer='f')
        decided = cards.decided_blocks(blocks, ZH.outcome_block(OPTIONS[0], OTHER, T))
        self.assertEqual([b.get('block_id') for b in decided], [None, cards.FOOTER_BLOCK_ID, cards.OUTCOME_BLOCK_ID])
        line = ZH.reply_line('好的', plan_change=True)
        self.assertEqual(line, '💬 你在 thread 里回复了：「好的」。你改了方案，agent 会按新意思重新提案')
        retired = cards.retired_blocks(blocks, line)
        self.assertEqual([b.get('block_id') for b in retired], [None, cards.FOOTER_BLOCK_ID, cards.REPLY_BLOCK_ID])
        kept = cards.retired_blocks(retired, ZH.reply_line('x', secret=True), keep_buttons=True)
        self.assertEqual(kept[-1]['elements'][0]['text'], '💬 你在 thread 里回复了（内容不在这里显示），按你的回复处理')
        self.assertEqual(ZH.reply_line('x', code_owned=True, labels=('是', '否')),
                         '💬 你在 thread 里回复了：「x」。要决定请点按钮或回复（“是”或“否”）；其他内容已转给负责这件事的会话')

    def test_rule_card(self):
        draft = {'title': '  回滚  前先问 ', 'scenario': 's', 'default': 'd', 'override': 'o', 'rule': 'r<', 'impact': 'i'}
        blocks = ZH.rule_card_blocks(draft)
        self.assertEqual(blocks[0]['text']['text'], '📐 规则提炼 · 回滚 前先问')
        self.assertTrue(blocks[1]['text']['text'].startswith(f'<@{OWNER}> 我想把'))
        self.assertEqual(blocks[4]['text']['text'], '*建议的规则（人话）*\nr&lt;')
        self.assertEqual(ZH.rule_card_blocks(draft, mention=None)[1]['text']['text'][:7], '<@None>')
        self.assertEqual(ZH.rule_card_fallback(draft), '规则提炼 · 回滚 前先问 — 要不要按这条固化成规则？')
        for bad, code in (({**draft, 'rule': ''}, 'rule_rule_must_be_1_to_700'), ({**draft, 'impact': 'a\x00'}, 'plain_text')):
            with self.assertRaisesRegex(ValueError, code):
                ZH.rule_card_blocks(bad)
        self.assertLessEqual(len(ZH.rule_card_header('x' * 300)['text']['text']), 150)

    def test_proposals(self):
        self.assertEqual(ZH.proposal_text(OPTIONS[1]), '我理解你的意思是：🔹 再等等。')
        self.assertEqual(ZH.proposal_text(OPTIONS[0], note='n', meaning='先回滚<再说>'),
                         '我理解你的意思是：先回滚&lt;再说&gt;。\n确认后按「✅ 先回滚（附条件：n）」处理。')
        blocks = ZH.proposal_blocks('p1', 't')
        self.assertEqual([(e['text']['text'], e['action_id']) for e in blocks[1]['elements']],
                         [('✅ 对，就这样', 'sf_confirm:yes'), ('✏️ 不是', 'sf_confirm:no')])
        self.assertEqual(ZH.confirmed_line(OTHER, T), f'✅ 已确认 · <@{OTHER}> · 20:05 (Asia/Shanghai)')
        self.assertEqual(cards.proposal_closed_blocks('t', 's')[1]['elements'][0]['text'], 's')
        self.assertEqual(set(ZH.superseded), set(cards.SUPERSEDED_REASONS))


if __name__ == '__main__':
    unittest.main()
