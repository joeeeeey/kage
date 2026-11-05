"""The dashboard (kage.observe.dashboard) and its pages (kage.observe.web), bound to a synthetic owner."""
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
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
        self.assertEqual(W.languages(), ['en', 'zh-CN'])  # O1e: English pages
        for language in W.languages():
            self.assertEqual(set(W.sentinels(language)), set(VALUES), language)
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

    def test_the_english_pages_are_the_chinese_pages_in_english(self):
        """O1e: pages/en/ has the files of pages/zh-CN/ with the same structure (sentinels and their counts, HTML tags with
        their ids / classes / links / data-* attributes, the API paths, element lookups and data-* names the scripts
        use) and no Chinese text: a line with Han or full-width signs is only one the zh-CN page has verbatim (a
        character class in code: text width, Markdown punctuation)."""
        from html.parser import HTMLParser
        han = re.compile('[\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef\u3000-\u303f]')
        zh_root, en_root = W.ROOT / 'zh-CN', W.ROOT / 'en'
        files = sorted(p.relative_to(zh_root).as_posix() for p in zh_root.rglob('*') if p.is_file())
        self.assertEqual(files, sorted(p.relative_to(en_root).as_posix() for p in en_root.rglob('*') if p.is_file()))

        class Shape(HTMLParser):
            def __init__(self):
                super().__init__()
                self.items = []

            def handle_starttag(self, tag, attrs):
                keep = ('id', 'class', 'href', 'src', 'rel', 'type', 'name', 'for', 'role', 'hidden')
                self.items.append((tag, tuple(sorted((k, v if k in keep or k.startswith('data-') else None)
                                                     for k, v in attrs if k != 'lang'))))

        uses = re.compile(r"/api/[a-z0-9_/-]+|getElementById\(\s*['\"][^'\"]+|querySelector(?:All)?\(\s*['\"][^'\"]+"
                          r"|data-[a-z-]+|\{\{[a-z_A-Z]+\}\}")
        for rel in files:
            zh, en = zh_root / rel, en_root / rel
            if zh.suffix not in ('.js', '.html', '.css', '.json'):
                self.assertEqual(zh.read_bytes(), en.read_bytes(), rel)
                continue
            zt, et = zh.read_text(encoding='utf-8'), en.read_text(encoding='utf-8')
            zh_lines = set(zt.splitlines())
            left = [line for line in et.splitlines() if han.search(line) and line not in zh_lines]
            self.assertEqual(left, [], rel)
            self.assertEqual(sorted(uses.findall(zt)), sorted(uses.findall(et)), rel)
            if zh.suffix == '.html':
                shapes = []
                for text in (zt, et):
                    parser = Shape()
                    parser.feed(text)
                    shapes.append(parser.items)
                self.assertEqual(shapes[0], shapes[1], rel)
                self.assertIn('<html lang="en">', et)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_every_rendered_page_script_parses(self):
        for language in W.languages():
            pages = W.Pages.for_owner(language, VALUES)
            with tempfile.TemporaryDirectory() as tmp:
                for name in sorted(p.name for p in (W.ROOT / language).glob('*.js')):
                    path = Path(tmp) / name
                    path.write_bytes((pages / name).read_bytes())
                    run = subprocess.run(['node', '--check', str(path)], capture_output=True, text=True, timeout=60)
                    self.assertEqual(run.returncode, 0, (language, name, run.stderr[-600:]))

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


def get(pages, target, host='127.0.0.1:8776'):
    """GET through the handler without a socket (the tests are offline): (status, content type or Location, body)."""
    handler = D.Handler.__new__(D.Handler)
    handler.server = types.SimpleNamespace(web=pages, server_port=8776)
    handler.headers, handler.path, sent, headers = {'Host': host}, target, [], {}
    handler.respond = lambda status, payload, content_type=None, csp=None: sent.append((status, content_type, payload))
    handler.send_response = lambda status, message=None: sent.append((status, None, b''))
    handler.send_header = lambda key, value: headers.__setitem__(key, value)
    handler.end_headers = lambda: None
    handler.do_GET()
    status, ctype, body = sent[0]
    return (status, headers['Location'], headers) if status == 302 else (status, ctype, body)


class ConsoleShellTest(unittest.TestCase):
    """S2: the console is Kage. / is the control entry (总控) and the scene moved to /scene (an old scene URL redirects
    there, its query kept); one global nav on every page, 总控 first; the shared shell script on every page; the
    control answers through the safe Markdown subset (md.js); no page script writes HTML."""
    NAV = ('control', 'scene', 'runs', 'intake', 'rules', 'routines', 'gaps', 'learning', 'health', 'design',
           'backlog', 'settings')
    PAGES = {'control.html': 'control', 'index.html': 'scene', 'ops.html': None}

    def setUp(self):
        self.pages = W.Pages.for_owner('zh-CN', VALUES)

    def page(self, name):
        return (self.pages / name).read_bytes()

    def test_home_is_the_control_entry_and_the_scene_moved(self):
        control, scene = self.page('control.html'), self.page('index.html')
        for target in ('/', '/control', '/?conversation=cv1', '/control?conversation=cv1', '/control?scope=all'):
            self.assertEqual(get(self.pages, target), (200, 'text/html; charset=utf-8', control), target)
        self.assertEqual(get(self.pages, '/scene'), (200, 'text/html; charset=utf-8', scene))
        self.assertEqual(get(self.pages, '/scene?scope=all'), (200, 'text/html; charset=utf-8', scene))
        # an old scene URL keeps working: the scene, with its own query
        for target, location in (('/index.html', '/scene'), ('/index.html?scope=all', '/scene?scope=all'),
                                 ('/?scope=all&q=x', '/scene?scope=all&q=x'), ('/?q=', '/scene?q='),
                                 ('/?conversation=cv1&scope=all', '/scene?conversation=cv1&scope=all')):
            status, where, headers = get(self.pages, target)
            self.assertEqual((status, where), (302, location), target)
            self.assertEqual((headers['Content-Length'], headers['Cache-Control']), ('0', 'no-store'))
            self.assertEqual(headers['Content-Security-Policy'], D.PAGE_CSP)
        for target in ('/?a=b\r\nSet-Cookie:x', '/?a=b c', '/?a=b\\c', '/?q=' + 'x' * 2001):
            self.assertEqual(get(self.pages, target)[0], 400, target[:20])
        self.assertEqual(get(self.pages, '/', host='evil.example:8776')[0], 403)
        self.assertEqual(get(self.pages, '/scene', host='evil.example:8776')[0], 403)

    def test_brand_and_one_nav_on_every_page(self):
        for name, current in self.PAGES.items():
            html = self.page(name).decode()
            self.assertIn('<title>Kage · ', html, name)
            self.assertIn('<a class="brand" href="/" aria-label="Kage · 总控">', html, name)
            self.assertIn('<h1>Kage</h1>', html, name)
            self.assertIn('<p>影分身</p>', html, name)
            nav = html[html.index('<nav class="ops-nav site-nav"'):html.index('</nav>', html.index('site-nav'))]
            items = re.findall(r'<a class="scope-btn" href="([^"]+)" data-nav="([a-z]+)"', nav)
            self.assertEqual([key for _, key in items], list(self.NAV), name)
            self.assertEqual(dict((key, href) for href, key in items)['control'], '/')
            self.assertEqual(dict((key, href) for href, key in items)['scene'], '/scene')
            marked = re.findall(r'data-nav="([a-z]+)"[^>]*aria-current="page"', nav)
            self.assertEqual(marked, [current] if current else [], name)  # ops.html: shell.js marks it from the path
            scripts = re.findall(r'<script src="([^"]+)"></script>', html)
            self.assertEqual(scripts[0], '/shell.js', name)
            if name == 'control.html':
                self.assertEqual(scripts, ['/shell.js', '/md.js', '/control.js'])
            self.assertNotRegex(html, r'(?i)follow-?up observatory|slack followup')
        for asset in ('/shell.js', '/md.js'):
            status, ctype, body = get(self.pages, asset)
            self.assertEqual((status, ctype, body), (200, 'text/javascript; charset=utf-8', self.page(asset[1:])))

    def test_no_page_script_writes_html(self):
        """Data reaches the page only as text nodes and checked attributes (createElement / textContent)."""
        for path in sorted((Path(W.__file__).parent / 'pages').glob('*/*.js')):
            text = path.read_text(encoding='utf-8')
            for sink in (r'\.innerHTML\b', r'\.outerHTML\b', r'insertAdjacentHTML', r'document\.write',
                         r'\beval\s*\(', r'new\s+Function\b', r'DOMParser', r'createContextualFragment'):
                self.assertIsNone(re.search(sink, text), f'{path.name}: {sink}')

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_markdown_answers_are_a_safe_subset(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / 'md.js'
            md.write_bytes(self.page('md.js'))
            run = subprocess.run(['node', str(Path(__file__).with_name('md_cases.js')), str(md)],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr[-800:])
        out = json.loads(run.stdout)
        # raw HTML is text; script / image / handler never become elements or attributes
        self.assertEqual(out['script']['tags'], ['p'])
        self.assertIn('<script>alert(1)</script>', json.loads(out['script']['text'])[0][2])
        for case in ('script', 'js_link', 'host_relative', 'quote_injection', 'image', 'ok_links', 'ids', 'rich'):
            self.assertFalse(set(out[case]['tags']) & {'script', 'img', 'iframe', 'style', 'object', 'svg'}, case)
            self.assertTrue(set(out[case]['attrs']) <= {'href', 'rel', 'target', 'class', 'title'}, case)
            for href in out[case]['hrefs']:
                self.assertRegex(href, r'^(https?://[^/\s"\'<>]|/(?![/\\]))', (case, href))
        self.assertEqual(out['js_link']['hrefs'], [])
        self.assertEqual(out['host_relative']['hrefs'], [])
        self.assertEqual(out['image']['hrefs'], [])
        self.assertEqual(out['quote_injection']['hrefs'], ['https://ok.example/'])
        self.assertEqual(out['ok_links']['hrefs'], ['https://example.com/a?b=1#c', 'https://example.com/z',
                                                    'https://example.com/q', '/runs/r20260101T000000Z-0a1b2c'])
        tree = json.loads(out['ok_links']['text'])[0]
        for node in tree[2:]:
            if isinstance(node, list) and node[1]['href'].startswith('http'):
                self.assertEqual((node[1]['rel'], node[1]['target']), ('noopener noreferrer', '_blank'))
        # record ids become links to their own pages; plain words that only look similar stay text
        self.assertEqual(out['ids']['hrefs'], ['/runs/r20260101T000000Z-0a1b2c', '/gaps/bk0123456789abcd',
                                               '/rules?check=rc0123456789abcd', '/rules?proposal=rp0123456789abcd',
                                               '/runs?task=local-task-20260101-sample-task',
                                               '/runs?task=TEAM-12-some-task'])
        self.assertTrue(json.loads(out['ids']['text'])[0][-1].endswith('UTF-8 TEAM-12'))
        self.assertEqual(out['rich']['tags'], ['h3', 'p', 'strong', 'em', 'del', 'code', 'ul', 'li', 'li', 'ul', 'li',
                                               'ol', 'li', 'li', 'blockquote', 'p', 'div', 'table', 'thead', 'tr', 'th',
                                               'th', 'tbody', 'tr', 'td', 'td', 'pre', 'code', 'hr'])
        self.assertIn('"<b>raw</b>"', out['rich']['text'])  # a code block keeps its text, never markup
        # bounded: a huge input is cut, deep and adversarial inputs stay fast (linear)
        self.assertTrue(out['huge']['cut'])
        for case in ('huge', 'deep', 'adversarial'):
            self.assertLess(out[case]['ms'], 1500, case)
        # render() builds only allowed elements and attributes, whatever tree it is handed
        self.assertEqual(out['render']['tags'], ['p', 'a', 'a'])
        self.assertEqual(out['render']['attrs'], [{'class': 'ok'}, {}, {'href': '/runs/x', 'rel': 'noopener noreferrer'}])


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
