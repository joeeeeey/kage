import datetime as dt
import unittest

from kage.adapters.slack import asks as slack_asks
from kage.core import asks as core
from kage.core import secrets

OWNER, OLA, NILS = 'U0000TEST01', 'U0000TEST03', 'U0000TEST05'
BERLIN = dt.timezone(dt.timedelta(hours=1))


def make(language='en'):
    asks = core.Asks(owner_language=language, owner_key='mira', owner_name='Mira', tz=BERLIN)
    return asks, slack_asks.SlackAsks(asks, owner_user=OWNER)


def parse_option(spec):
    kind, label = spec.split(':', 1)
    return kind, label, None


class SecretsTest(unittest.TestCase):
    def test_credential_shapes_are_refused(self):
        token = 'xox' + 'b-' + '1234567890-abcdef'
        # Probes are assembled at run time, so this file carries no credential-shaped literal.
        for text in (token, 'AKIA' + 'ABCDEFGHIJKLMNOP', 'pass' + 'word: ' + 'hunter22hunter', 'api_' + 'key=' + 'abcdef123456',
                     '-----BEGIN RSA ' + 'PRIVATE KEY-----', '密码' + '：abc123456', '口令' + '=abcdefgh',
                     'eyJ' + 'a' * 12 + '.' + 'b' * 12 + '.'):
            with self.subTest(text=text[:12]), self.assertRaisesRegex(ValueError, secrets.ERROR):
                secrets.check_secret_free('fine', text)

    def test_ordinary_text_passes(self):
        secrets.check_secret_free(None, '', 'the token expired yesterday', 'password reset link sent',
                                  'see https://share.northwind.example/one-time/abc', '密码已重置')

    def test_every_catalog_contributes_its_labelled_form(self):
        sources = [p.pattern for p in secrets.PATTERNS]
        self.assertEqual(sources[:len(secrets.SHAPES)], list(secrets.SHAPES))
        self.assertEqual(len(sources), len(secrets.SHAPES) + 2)
        self.assertIsInstance(secrets.PATTERNS, tuple)


class AsksTest(unittest.TestCase):
    def test_owner_language_and_vias(self):
        asks, _ = make()
        self.assertEqual((asks.default_lang, asks.override_via), ('en', 'mira_override'))
        self.assertEqual(make('zh-CN')[0].default_lang, 'zh')
        with self.assertRaises(ValueError):
            core.Asks(owner_language='fr', owner_key='mira', owner_name='Mira', tz=BERLIN)

    def test_phrases_in_both_languages(self):
        asks, _ = make()
        self.assertEqual(asks.phrase('en', 'expired'), '(Expired, no reply)')
        self.assertEqual(asks.phrase('zh', 'handled'), '这个已经处理过了')
        self.assertEqual(asks.phrase('fr', 'handled'), "This one's already been handled")   # owner's language
        self.assertEqual(asks.phrase('en', 'override', icon='✅', label='Yes', note='', clock='09:00'),
                         '✅ Mira answered: Yes · 09:00')
        self.assertEqual(asks.phrases()['zh']['override'], '{icon} Mira 代选：{label}{note} · {clock}')
        with self.assertRaises(KeyError):
            asks.phrase('en', 'chose', icon='✅')   # a missing value is an error, never a silent blank

    def test_durations(self):
        self.assertEqual(core.parse_duration('10h'), 36000)
        self.assertEqual(core.parse_duration(' 2 D '), 172800)
        self.assertEqual(core.parse_duration('5m', smoke=True), 300)
        for bad, kw in (('30m', {}), ('8d', {}), ('soon', {}), ('30s', {'smoke': True})):
            with self.assertRaises(ValueError):
                core.parse_duration(bad, **kw)

    def test_clock_and_confirmation(self):
        asks, _ = make()
        at = dt.datetime(2026, 1, 5, 9, 30, tzinfo=BERLIN).timestamp()
        self.assertEqual((asks.clock(at), asks.iso(at), asks.iso(0)), ('09:30', '2026-01-05T09:30:00+01:00', None))
        self.assertTrue(core.needs_confirmation('auto', {'kind': 'approve'}))
        self.assertFalse(core.needs_confirmation('auto', {'kind': 'reject'}))
        self.assertTrue(core.needs_confirmation('always', {'kind': 'other'}))


class SlackAsksTest(unittest.TestCase):
    def test_text_mentions_only_responders_and_never_broadcasts(self):
        self.assertEqual(slack_asks.prepare_text('Can you review?', [OLA, NILS]), f'<@{OLA}> <@{NILS}> Can you review?')
        self.assertEqual(slack_asks.prepare_text(f'<@{OLA}> can you?', [OLA]), f'<@{OLA}> can you?')
        for text, code in ((f'<@{OWNER}> hi', 'may_only_mention_responders'), ('<!here> hi', 'must_not_broadcast'),
                           ('@channel hi', 'must_not_broadcast'), ('', '1_to_1500'), ('x' * 1501, '1_to_1500')):
            with self.assertRaisesRegex(ValueError, code):
                slack_asks.prepare_text(text, [OLA])

    def test_responders_and_labels(self):
        self.assertEqual(slack_asks.validate_responders([OLA, OLA, NILS]), [OLA, NILS])
        for bad in ([], [OLA] * 1 + ['C0000TEST01'], [f'U000000000{i}' for i in range(6)]):
            with self.assertRaises(ValueError):
                slack_asks.validate_responders(bad)
        slack_asks.validate_labels(['approve:Yes', 'reject:No'], parse_option)
        for bad in ('approve:see https://x.example', 'other:ask <@U0000TEST03>', 'other:a@b'):
            with self.assertRaises(ValueError):
                slack_asks.validate_labels([bad], parse_option)

    def test_blocks_and_lines(self):
        asks, slack = make()
        options = [{'id': 'a', 'label': 'Ship it', 'kind': 'approve'}, {'id': 'b', 'label': 'Wait', 'kind': 'other'}]
        blocks = slack.card_blocks('c' * 20, 'Text', options)
        self.assertEqual([b['type'] for b in blocks], ['section', 'actions', 'context'])
        self.assertEqual(blocks[1]['elements'][0]['style'], 'primary')
        self.assertNotIn('style', blocks[1]['elements'][1])
        self.assertEqual(blocks[2]['elements'][0]['text'], 'Anything else, just reply in the thread')
        self.assertEqual(len(slack.card_blocks('c' * 20, 'Text', options, hint=False)), 2)
        at = dt.datetime(2026, 1, 5, 9, 30, tzinfo=BERLIN).timestamp()
        self.assertEqual(slack.result_line('en', options[0], OLA, 'button', at, note='<b>'),
                         f'✅ <@{OLA}> chose: Ship it（&lt;b&gt;） · 09:30')
        self.assertEqual(slack.result_line('zh', options[1], OWNER, 'mira_override', at), '🔹 Mira 代选：Wait · 09:30')
        self.assertEqual(slack.confirm_text('en', OWNER, options[0], None), 'Just to confirm: Ship it?')
        self.assertEqual(slack.confirm_text('en', OLA, options[0], None), f'<@{OLA}> Just to confirm: Ship it?')
        confirm = slack.confirm_blocks('p1', 'Sure?', 'zh')
        self.assertEqual([e['text']['text'] for e in confirm[1]['elements']], ['✅ 对', '✏️ 不对'])
        self.assertEqual(slack_asks.closed_blocks('T', 'L')[1]['block_id'], slack_asks.RESULT_BLOCK_ID)


if __name__ == '__main__':
    unittest.main()
