import re
import string
import unittest

from kage import locale


def fields(template):
    return sorted({name for _, name, _, _ in string.Formatter().parse(template) if name})


class CatalogTest(unittest.TestCase):
    def test_catalogs_exist(self):
        self.assertEqual(locale.languages(), ['en', 'zh-CN'])

    def test_every_catalog_has_the_same_keys_and_placeholders(self):
        base = locale.catalog('en')
        for language in locale.languages():
            cat = locale.catalog(language)
            with self.subTest(language=language):
                self.assertEqual(cat['language'], language)
                self.assertEqual(sorted(cat['patterns']), sorted(base['patterns']))
                self.assertEqual(sorted(cat['messages']), sorted(base['messages']))
                self.assertEqual(sorted(cat['words']), sorted(base['words']))
                self.assertTrue(all(isinstance(w, str) and w for ws in cat['words'].values() for w in ws))
                for key, template in cat['messages'].items():
                    self.assertEqual(fields(template), fields(base['messages'][key]), key)

    def test_no_field_takes_a_parameter_name_of_message(self):
        # locale.message(language, key, **values) and every core's msg(key, **values): a field named like one of their
        # own parameters could never be passed (TypeError: got multiple values for argument 'key').
        for language in locale.languages():
            for key, template in locale.catalog(language)['messages'].items():
                self.assertFalse(set(fields(template)) & {'language', 'key', 'values', 'self'}, (language, key))

    def test_every_pattern_compiles(self):
        for language in locale.languages():
            for key, source in locale.catalog(language)['patterns'].items():
                with self.subTest(language=language, key=key):
                    re.compile(source)

    def test_message_formatting(self):
        self.assertEqual(locale.message('en', 'card.line.situation', text='waiting since 09:00'),
                         'Situation: waiting since 09:00')
        self.assertEqual(locale.message('zh-CN', 'card.line.field', label='原因', text='x'), '原因：x')
        self.assertEqual(locale.message('en', 'you'), 'you')

    def test_unknown_language_or_key(self):
        self.assertIn('批准', locale.words('zh-CN', 'decision.approve_phrases'))
        for call in (lambda: locale.catalog('fr'), lambda: locale.catalog('../en'), lambda: locale.words('en', 'nope'),
                     lambda: locale.pattern('en', 'nope'), lambda: locale.message('zh-CN', 'nope')):
            with self.assertRaises(locale.LocaleError):
                call()

    def test_catalog_is_read_only(self):
        with self.assertRaises(TypeError):
            locale.catalog('en')['messages']['you'] = 'me'


class PatternBehaviourTest(unittest.TestCase):
    def search(self, language, key, text):
        return bool(re.search(locale.pattern(language, key), text))

    def test_decision_words(self):
        self.assertTrue(self.search('zh-CN', 'card.decision', '要你决定：是否合并 PR'))
        self.assertTrue(self.search('en', 'card.decision', 'Decide whether to merge the PR'))
        self.assertFalse(self.search('en', 'card.decision', 'Status update'))

    def test_waiting_since(self):
        self.assertTrue(self.search('zh-CN', 'card.wait', '等 Mira 回话') and
                        self.search('zh-CN', 'card.since', '从昨天 18:00 开始'))
        self.assertTrue(self.search('en', 'card.wait', 'waiting on Ola') and
                        self.search('en', 'card.since', 'since 09:30'))

    def test_failure_words(self):
        for language, text in (('en', 'HTTP 403 Forbidden'), ('zh-CN', '认证失败'), ('en', 'request timed out')):
            self.assertTrue(self.search(language, 'card.failure', text), text)


if __name__ == '__main__':
    unittest.main()
