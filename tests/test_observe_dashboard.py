"""The dashboard (kage.observe.dashboard) and its pages (kage.observe.web), bound to a synthetic owner."""
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import types
import unittest
import zoneinfo

from kage.observe import dashboard as D
from kage.observe import web as W

VALUES = {'owner_name': 'Mira', 'OWNER_NAME': 'MIRA', 'owner_key': 'mira', 'tz_name': 'Europe/Lisbon',
          'receipt_emoji': 'eyes',
          'tail_command': 'test-tail', 'ticket_ref_reason': 'ticket_ref', 'ticket_team': 'TEST',
          'feedback_corpus': '~/.local/share/test/feedback-corpus', 'service_label': 'org.example.followup',
          'trace_global': 'TestTrace', 'channel_example': 'C0000TEST01', 'oncall_channel_example': 'C0000TEST02',
          'channel_names': '#ops / #oncall'}


def module(**attrs):
    return types.SimpleNamespace(**attrs)


def core(language='en', web=None):
    trace = module(LIST_DAYS=3, LIST_MAX_DAYS=14, VIEWS=('all', 'mira'), INBOX_TASKS=('mira-inbox',),
                   Store=lambda *a, **kw: None, inbox=lambda store, days: [], in_view=lambda item, view: True)
    avatars = module(initialize=lambda db: None, seed_events=lambda db: None, enrich=lambda db, state: state,
                     CacheWorker=None)
    return D.DashboardCore(language, tz=zoneinfo.ZoneInfo('Europe/Lisbon'), owner_key='mira', owner_name='Mira',
                           web=web or Path('/nonexistent'), default_design_html='/nonexistent/design.html',
                           design_workspace='/nonexistent/workspace', known_channels={'#ops': 'C0000TEST01'},
                           tail_command='test-tail', avatars=avatars, poll_schedule=module(DELAYS=[60, 120, 300]),
                           previews=module(PreviewService=lambda path: None, PreviewError=Exception),
                           claude_delivery=None, run_log=None, task_registry=None,
                           worker_config=module(load=lambda: (_ for _ in ()).throw(ValueError('no config'))),
                           rule_engine=None, routines=None, decision_buttons=None, blockers=None,
                           feedback_learning=None, provider_health=None, stuck_claims=None, dispatcher=None,
                           cache_report=None, trace=trace, process_alive=lambda pid, birth: False)


class PagesTest(unittest.TestCase):
    def test_every_sentinel_is_filled_once(self):
        self.assertEqual(W.languages(), ['zh-CN'])
        self.assertEqual(set(W.sentinels('zh-CN')), set(VALUES))
        pages = W.Pages.for_owner('zh-CN', VALUES)
        ops = (pages / 'ops.js').read_bytes().decode()
        self.assertNotIn('{{', ops)
        self.assertIn("const TZ = 'Europe/Lisbon';", ops)
        self.assertIn('test-tail', ops)
        self.assertTrue((pages / 'assets' / 'codex-agent.png').read_bytes().startswith(b'\x89PNG'))
        with self.assertRaises(FileNotFoundError):
            (pages / 'assets' / '../ops.js').read_bytes()
        once = W.Pages.for_owner('zh-CN', {**VALUES, 'owner_name': '{{owner_key}}'})
        self.assertIn('{{owner_key}}', (once / 'index.html').read_bytes().decode())  # a value is never re-read

    def test_missing_values_and_languages(self):
        with self.assertRaises(W.PagesError):
            W.render('zh-CN', {k: v for k, v in VALUES.items() if k != 'tz_name'})
        for language in ('fr', '../zh-CN', ''):
            with self.assertRaises(W.PagesError):
                W.render(language, VALUES)


class StylesTest(unittest.TestCase):
    """S1: one shared stylesheet (base.css: the design tokens and the shared components), linked first by every page,
    served under the same allow-list as the other sheets; local only; no text under the 10 px floor."""
    TOKENS = ('--accent', '--accent-d', '--accent-soft', '--surface', '--surface-2', '--surface-sunken',
              '--ok', '--ok-d', '--ok-soft', '--warn', '--warn-d', '--warn-soft', '--bad', '--bad-d', '--bad-soft',
              *(f'--sp-{i}' for i in range(1, 9)), '--r-xs', '--r-sm', '--r-md', '--r-lg', '--r-pill', '--radius',
              '--fs-2xs', '--fs-xs', '--fs-sm', '--fs-base', '--fs-md', '--fs-lg', '--fs-xl', '--fs-2xl',
              '--lh-tight', '--lh', '--lh-loose', '--sh-sm', '--sh-card', '--sh-raised', '--sh-overlay',
              '--control-h', '--page-max', '--page-pad')

    def setUp(self):
        self.pages = W.Pages.for_owner('zh-CN', VALUES)

    def sheet(self, name):
        return (self.pages / name).read_bytes().decode()

    def test_base_holds_the_tokens_and_every_page_links_it_first(self):
        base = self.sheet('base.css')
        root = base[base.index(':root {'):base.index('}', base.index(':root {'))]
        for token in self.TOKENS:
            self.assertIn(f'  {token}:', root, token)
        for page in ('index.html', 'ops.html', 'control.html'):
            html = self.sheet(page)
            links = [line.strip() for line in html.splitlines() if 'rel="stylesheet"' in line]
            self.assertEqual(links[0], '<link rel="stylesheet" href="/base.css">', page)

    def test_control_css_is_on_the_tokens(self):
        """C1's page sheet reads the shared tokens without fallbacks (base.css is always linked first)."""
        import re
        css = self.sheet('control.css')
        self.assertIn('var(--accent)', css)
        self.assertIsNone(re.search(r'var\(--[a-z0-9-]+,', css))

    def test_sheets_are_local_and_readable(self):
        import re
        for name in ('base.css', 'app.css', 'ops.css', 'trace.css', 'control.css'):
            css = self.sheet(name)
            self.assertNotIn('@import', css, name)
            self.assertIsNone(re.search(r'url\(\s*["\']?(https?:)?//', css), name)
            sizes = [float(m) for m in re.findall(r'(?:font-size:\s*|font:[^;]*?\s)(\d+(?:\.\d+)?)px', css)]
            self.assertGreaterEqual(min(sizes, default=10), 10, f'{name}: text under 10 px')

    def test_the_handler_serves_base_css_under_the_allow_list(self):
        def get(target, host='127.0.0.1:8776'):  # the handler without a socket (the tests are offline)
            handler = D.Handler.__new__(D.Handler)
            handler.server = types.SimpleNamespace(web=self.pages, server_port=8776)
            handler.headers, handler.path, sent = {'Host': host}, target, []
            handler.respond = lambda status, payload, content_type=None, csp=None: sent.append((status, content_type, payload))
            handler.do_GET()
            return sent[0]
        status, ctype, body = get('/base.css')
        self.assertEqual((status, ctype), (200, 'text/css; charset=utf-8'))
        self.assertEqual(body, (self.pages / 'base.css').read_bytes())
        self.assertEqual(get('/base.css', host='evil.example:8776')[0], 403)
        self.assertEqual(get('/tokens.css')[0], 404)
        self.assertEqual(get('/../base.css')[0], 404)


class DashboardCoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_words_and_owner_values(self):
        c = core()
        visuals = c.watch_visuals({'state': 'running', 'step': 0}, 100.0)
        self.assertEqual(visuals['freshness']['label'], 'fresh · 1 min')
        self.assertEqual(c.watch_visuals({'state': 'stopped'}, 100.0)['freshness']['label'], 'closed')
        self.assertEqual(core('zh-CN').watch_visuals({'state': 'stopped'}, 100.0)['freshness']['label'], '已关闭')
        self.assertEqual(c._inbox_tasks(), ('mira-inbox',))

    def test_the_server_binds_loopback_only_and_reads_state(self):
        path = self.tmp / 'state.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE watches (id TEXT PRIMARY KEY, data TEXT); CREATE TABLE events (id TEXT, '
                             'watch_id TEXT, channel TEXT, ts TEXT, author TEXT, digest TEXT, received REAL, status TEXT, '
                             'receipt TEXT, disposition TEXT, action_ts TEXT);')
        db.close()
        c = core()
        with self.assertRaisesRegex(ValueError, 'dashboard_must_bind_loopback'):
            D.DashboardServer(('0.0.0.0', 0), path, core=c)
        state = c.snapshot(path, now=100.0)
        self.assertEqual((state['summary']['watches'], state['limits']['watches']), (0, D.WATCH_LIMIT))
        self.assertEqual(c.snapshot(self.tmp / 'missing.sqlite', now=1.0)['notice'], 'state_database_missing')

if __name__ == '__main__':
    unittest.main()
