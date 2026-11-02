// The safe Markdown subset (kage/observe/pages/zh-CN/md.js) against hostile and ordinary inputs; prints JSON for
// tests/test_observe_dashboard.py (MarkdownTest). Runs under node; md.js has no DOM dependency except render().
'use strict';
const md = require(process.argv[2]);
const walk = (t, f) => { for (const n of t) if (Array.isArray(n)) { f(n); walk(n.slice(2), f); } };
const summary = (t) => {
  const tags = [], hrefs = [], attrs = new Set();
  walk(t, (n) => { tags.push(n[0]); for (const k of Object.keys(n[1] || {})) attrs.add(k); if (n[1] && n[1].href) hrefs.push(n[1].href); });
  return { tags, hrefs, attrs: [...attrs].sort(), text: JSON.stringify(t) };
};
const cases = {
  script: '<script>alert(1)</script> <img src=x onerror=alert(1)> <a href="javascript:alert(1)">x</a>',
  js_link: '[a](javascript:alert(1)) [b](JaVaScRiPt:alert) [c](vbscript:x) [d](data:text/html;base64,PHNjcmlwdD4=)',
  host_relative: '[a](//evil.example/x) [b](/\\evil.example) [c](\\\\evil) [d](http:evil)',
  quote_injection: '[a](https://ok.example/"onmouseover="alert(1)) [b](https://ok.example/x\'y)',
  image: '![alt](https://x.example/p.png) ![](https://x.example/q.png)',
  ok_links: '[docs](https://example.com/a?b=1#c) <https://example.com/z> see https://example.com/q. and [run](/runs/r20260101T000000Z-0a1b2c)',
  ids: 'r20260101T000000Z-0a1b2c bk0123456789abcd rc0123456789abcd rp0123456789abcd local-task-20260101-sample-task TEAM-12-some-task UTF-8 TEAM-12',
  rich: '# T\n\n**b** *i* ~~d~~ `c`\n\n- one\n- two\n  - sub\n\n1. x\n2. y\n\n> q\n\n| A | B |\n|---|--:|\n| 1 | 2 |\n\n```\n<b>raw</b>\n```\n\n---',
};
const out = {};
for (const [k, v] of Object.entries(cases)) out[k] = summary(md.parse(v));
// bounds: huge, deep, and adversarial inputs finish fast and are cut
const time = (s) => { const t0 = process.hrtime.bigint(); const tree = md.parse(s); return [Number(process.hrtime.bigint() - t0) / 1e6, tree]; };
const [hugeMs, huge] = time('x'.repeat(3000000));
out.huge = { ms: hugeMs, cut: JSON.stringify(huge).includes('md-cut') };
out.deep = { ms: time('>'.repeat(20000) + ' q\n' + '  '.repeat(5000) + '- x')[0] };
out.adversarial = { ms: Math.max(...['a`'.repeat(20000), '*a '.repeat(13000), '[x]('.repeat(10000), 'r20260101T000000Z-0a1b2c '.repeat(1600),
  'https://a.example/x '.repeat(2000), '**a *b ~~c `d` e~~ f* g** '.repeat(1500)].map((s) => time(s)[0])) };
// render(): a stand-in document records every element and attribute it is asked to create
const made = [];
const doc = {
  createDocumentFragment: () => ({ kids: [], appendChild(c) { this.kids.push(c); return c; } }),
  createTextNode: (t) => ({ text: t }),
  createElement: (tag) => { const el = { tag, attrs: {}, kids: [], setAttribute(k, v) { this.attrs[k] = v; }, appendChild(c) { this.kids.push(c); return c; } }; made.push(el); return el; },
};
md.render([['p', { onclick: 'x', href: 'javascript:alert(1)', class: 'ok' }, 'a'], ['script', {}, 'b'], ['a', { href: '//evil.example' }, 'c'],
  ['a', { href: '/runs/x', rel: 'noopener noreferrer' }, 'd']], doc);
out.render = { tags: made.map((e) => e.tag), attrs: made.map((e) => e.attrs) };
console.log(JSON.stringify(out));
