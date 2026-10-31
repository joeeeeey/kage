import contextlib
import sqlite3
import types
import unittest

from kage.adapters.slack import health_notices
from kage.core import provider_health as ph

PH = {'enabled': True, 'window_minutes': 30, 'min_runs': 2, 'degrade_threshold': 2, 'recover_checks': 2,
      'slow_run_minutes': 20}
CONFIG = {'dispatch': {'provider_health': PH}, 'routing': {'fallback_order': ['claude', 'codex']},
          'providers': {'codex': {'model': 'gpt-x'}, 'claude': {'model_default': 'opus'}}}


def store():
    db = sqlite3.connect(':memory:', isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE consumer_runs (task TEXT, provider TEXT, model TEXT, state TEXT, started_at REAL, ended_at REAL, '
               'outcome TEXT, error_class TEXT, trigger TEXT, health_markers INTEGER)')
    ph.initialize(db)
    return db


def runtime(posts):
    @contextlib.contextmanager
    def transaction(db):
        db.execute('BEGIN IMMEDIATE')
        try:
            yield
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    return types.SimpleNamespace(transaction=transaction, safe_error=lambda e: type(e).__name__,
                                 worker_config=types.SimpleNamespace(load=lambda: (CONFIG, None),
                                                                     slow_limits=lambda c: {}),
                                 _meta_error=lambda db, key, exc: None)


class HealthTest(unittest.TestCase):
    def test_degrade_notice_recover(self):
        db, posts = store(), []
        health = ph.ProviderHealth('zh-CN', post=lambda rt, db, p, kind, text: posts.append((p, kind, text)) or 'ts1')
        rt = runtime(posts)
        for i in range(3):
            db.execute("INSERT INTO consumer_runs VALUES ('t','codex','gpt-x','ended',?,?,'failed','transient','event',0)",
                       (900.0 + i, 950.0 + i))
        out = health.evaluate(rt, db, now=1000.0)
        self.assertEqual([o['event'] for o in out], ['degraded', 'notice_degraded'])
        self.assertEqual(posts[0][2], 'gpt-x（Codex） 过载不稳定，新的后台会话已临时改用 Claude，恢复后自动切回。\n'
                                      '近 30 分钟 3 次运行里 3 次出问题（过载 / 断流 3 次、失败 3 次）。')
        self.assertEqual(ph.status_map(db), {'codex': 'degraded'})            # a provider without a row is healthy
        for now in (4000.0, 4100.0):
            health.evaluate(rt, db, now=now)
        self.assertEqual(ph.status_map(db)['codex'], 'healthy')
        db.execute("INSERT INTO consumer_runs VALUES ('t','codex','gpt-x','ended',4150,4160,'success',NULL,'event',0)")
        health.evaluate(rt, db, now=4200.0)
        self.assertEqual(posts[-1][1:], ('healthy', 'gpt-x（Codex） 已恢复正常，后台任务已切回。'))

    def test_a_failed_post_keeps_the_state_and_retries(self):
        db = store()
        calls = []

        def post(rt, db, p, kind, text):
            calls.append(kind)
            if len(calls) == 1:
                raise RuntimeError('Slack API error: ratelimited')
            return 'ts'
        health = ph.ProviderHealth('en', post=post)
        for i in range(2):
            db.execute("INSERT INTO consumer_runs VALUES ('t','claude','opus','ended',900,950,'failed','quota','event',0)")
        health.evaluate(runtime([]), db, now=1000.0)
        row = db.execute("SELECT status, announced, notice_error FROM provider_health WHERE provider='claude'").fetchone()
        self.assertEqual(tuple(row), ('degraded', 'healthy', 'RuntimeError'))
        health.evaluate(runtime([]), db, now=1030.0)
        self.assertEqual(calls, ['degraded', 'degraded'])

    def test_english_notice_and_the_slack_post(self):
        health = ph.ProviderHealth('en', post=None)
        counts = {'runs': 4, 'adverse': 2, 'overload': 0, 'failed': 2, 'slow': 0, 'models': []}
        text = health.notice_text('claude', 'degraded', counts, CONFIG, {'codex': 'degraded'}, 30)
        self.assertEqual(text, 'opus (Claude) is overloaded and so is the other provider; background work queues and '
                               'retries as usual.\n2 of 4 runs in the last 30 minutes had problems (failures 2).')
        sent = []
        rt = types.SimpleNamespace(
            asks=types.SimpleNamespace(check_secret_free=lambda t: None),
            buttons=types.SimpleNamespace(load_config=lambda: {'decision_channel': 'C0000TEST01'},
                                          require_private_destination=lambda *a, **k: 'private',
                                          escape=lambda t: t.replace('<', '&lt;')),
            slack=types.SimpleNamespace(api_call=lambda method, token, json_body: sent.append(json_body) or
                                        {'channel': 'C0000TEST01', 'ts': '1.2'}),
            api_identity=lambda bot=False: 'bot-token', DASHBOARD_URL='http://127.0.0.1:8776',
            agent_metadata=lambda *a, **k: {'event_type': 'x'}, record_send=lambda *a: None)
        ts = health_notices.HealthNotices('zh-CN', owner_user='U0000TEST01').post(rt, None, 'codex', 'degraded', 'a<b')
        self.assertEqual(ts, '1.2')
        self.assertEqual(sent[0]['text'], '<@U0000TEST01> a&lt;b')
        self.assertEqual(sent[0]['blocks'][1]['elements'][0]['text'], 'provider 健康 · 详情：<http://127.0.0.1:8776/health>')


if __name__ == '__main__':
    unittest.main()
