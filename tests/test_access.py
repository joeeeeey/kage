import re
import types
import unittest
from pathlib import Path
import zoneinfo

from kage.policy import access as A

T0 = 1793400000.0


def check_seat_limit(ctx, p, rule):  # an org check: the pack's own code, calling the engine's helpers
    if len(ctx.scope.get('seats') or ()) > p.get('max', 1):
        return A.fail('too_many_seats', 'too many seats', 'owner')
    return None


PACK = types.SimpleNamespace(  # a synthetic org pack: what an org adds to the engine
    SYSTEMS=('wiki', 'tracker', 'other'), PRODUCTS=(), CREDENTIAL_TYPES=('api_key',), DELIVERIES=('email_link',),
    BUNDLES=('starter',), FUNCTIONS=('engineering', 'design'), PRIVILEGED_SCOPES=('org',),
    SYSTEM_ZH={'wiki': 'Wiki'}, FUNCTION_ZH={'engineering': 'Engineering'}, AR_RE=re.compile(r'\bOR-\d{2}\b'),
    PLAN_SCOPE={'seats': A._list(A._str(40), 20)}, SPEC_DEFAULTS={},
    RULE_ID_PATTERN=r'^OR-\d{2}$', RULE_REF_PATTERN=r'(?<![A-Za-z0-9])OR-\d+',
    REASON_WHY={'too_many_seats': 'more seats than the rule allows'},
    CHECKS={'seat_limit': check_seat_limit},
    check_params=lambda level, relations: {'seat_limit': {'max': A._INT}},
    SMOKE_SUMMARY={}, SMOKE_PROPOSAL_PATH='/rules/OR-01/checks/seat_limit/params/max', SMOKE_RULE='OR-01',
    SMOKE_CHECK='seat_limit', smoke_plans=lambda spec: {})


def policy(language='en'):
    return A.AccessPolicy(language, tz=zoneinfo.ZoneInfo('Asia/Shanghai'), example_spec=Path('/nonexistent.json'),
                          owner_key='owner', owner_name='Mira', owner_user=lambda rt: 'U0000TEST01',
                          ask_owner=lambda rt: None, worker_slug='test-worker', rules_dir_env='TEST_RULES_DIR',
                          smoke_dir_prefix='test-rules-smoke-', run_id_env='TEST_RUN_ID', pack=PACK)


def spec(P):
    return {'schema': P.spec_schema_id, 'category': 'access', 'version': 1,
            'defaults': {'company_domains': ['example.com'], 'owner': 'U0000TEST01',
                         'approvers': ['U0000TEST01', 'U0000TEST02'], 'hr': ['U0000TEST03'],
                         'spend': {'recurring_usd_month': 10, 'one_time_usd': 25}, 'temporary_days': 7},
            'never_auto': {shape: {'level': 'owner'} for shape in A.SHAPES},
            'role_catalog': {'wiki': {'reader': 'read', 'editor': 'write'}},
            'rules': [{'id': 'OR-01', 'name': 'wiki_seats', 'title_zh': 'wiki seats', 'kind': 'domain', 'level': 'auto',
                       'trigger': {'category': 'access', 'systems': ['wiki']},
                       'checks': [{'name': 'seat_limit', 'params': {'max': 2}}], 'fallback': 'owner'}]}


def plan(P, seats):
    return P.normalize_plan({'schema': P.plan_schema_id, 'category': 'access', 'kind': 'person', 'action': 'grant',
                             'system': 'wiki', 'recipient': {'email': 'Ada@Example.com'},
                             'scope': {'roles': ['reader'], 'seats': seats}})


class AccessPolicyTest(unittest.TestCase):
    def test_the_pack_extends_the_engine(self):
        P = policy()
        self.assertIn('seats', P.plan_schema['properties']['scope']['properties'])
        self.assertIn('resources', P.plan_schema['properties']['scope']['properties'])   # the engine's own fields stay
        self.assertEqual(P.plan_schema['properties']['system'], {'enum': ['wiki', 'tracker', 'other']})
        self.assertEqual(P.spec_schema['$defs']['rule']['properties']['id']['pattern'], r'^OR-\d{2}$')
        self.assertIs(P.checks['seat_limit'], check_seat_limit)
        self.assertIn('registry_status', P.checks)                                       # and its checks
        self.assertEqual(set(P.checks), set(P.check_params))
        self.assertEqual(P.reason_why['too_many_seats'], 'more seats than the rule allows')
        self.assertEqual((P.spec_schema_id, P.plan_schema_id), ('test-worker.rules/v1', 'test-worker.rules-plan/v1'))

    def test_owner_bound_levels(self):
        P = policy()
        self.assertEqual(P.levels, ('auto', 'one_tap', 'owner'))
        self.assertEqual(P.outcomes[-1], 'owner')
        self.assertEqual(P.strictest(['auto', 'owner', 'route']), 'owner')
        self.assertTrue(str(P.default_rules_dir).endswith('.config/test-worker/rules'))

    def test_a_pack_check_decides_the_outcome(self):
        P = policy()
        self.assertEqual(P.spec_problems(spec(P)), [])
        ok = P.evaluate(spec(P), plan(P, ['a', 'b']), A.empty_facts(), now=T0)
        self.assertEqual((ok['covered'], ok['matched_rules'], ok['failed_checks']), (True, ['OR-01'], []))
        over = P.evaluate(spec(P), plan(P, ['a', 'b', 'c']), A.empty_facts(), now=T0)
        self.assertEqual((over['outcome'], over['failed_checks'][0]['reason']), ('owner', 'too_many_seats'))
        self.assertEqual(policy('zh-CN').why_zh(over), 'more seats than the rule allows。')
        self.assertEqual(P.why_zh(over), 'more seats than the rule allows.')

    def test_plain_reason_drops_brackets_and_rule_refs(self):
        zh = policy('zh-CN')
        self.assertEqual(zh._plain_reason('席位超过上限 OR-01（见规则）：多了一个'), '席位超过上限')
        self.assertEqual(zh._plain_reason(''), '规则要你确认这一步')
        self.assertNotEqual(policy()._plain_reason(''), '规则要你确认这一步')

    def test_an_invalid_plan_is_refused(self):
        P = policy()
        with self.assertRaises(ValueError):
            plan(P, ['a'] * 21)
        with self.assertRaises(ValueError):
            P.normalize_plan({'schema': P.plan_schema_id, 'category': 'access', 'kind': 'person', 'action': 'grant',
                              'system': 'unknown_system'})

    def test_the_card_summary_is_written_in_the_owners_language(self):
        # O1d (G2): an English owner's summary passes; a Chinese owner's still needs Han, with the same error as before
        from unittest import mock
        rt = mock.MagicMock()
        self.assertEqual(policy('en').validate_summary(rt, ' Remove  Ann from the wiki? '), 'Remove Ann from the wiki?')
        self.assertEqual(policy('zh-CN').validate_summary(rt, '移除 Ann 的 wiki 账号'), '移除 Ann 的 wiki 账号')
        with self.assertRaisesRegex(ValueError, '^summary_must_be_one_chinese_sentence_of_at_most_'):
            policy('zh-CN').validate_summary(rt, 'Remove Ann from the wiki?')
        with self.assertRaisesRegex(ValueError, '^summary_must_be_one_in_the_owner_language_sentence_of_at_most_'):
            policy('en').validate_summary(rt, '42 / 7')
        self.assertEqual((policy('zh-CN').script, policy('en').script), ('chinese', 'in_the_owner_language'))


if __name__ == '__main__':
    unittest.main()
