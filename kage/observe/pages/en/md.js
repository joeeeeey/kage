/* Kage · a safe Markdown subset for text an agent wrote (the control answers). The text is untrusted: nothing is ever
   parsed as HTML. parse() turns it into a small tree ([tag, attrs, ...children], text as strings); render() builds DOM
   nodes from that tree with createElement / createTextNode only, for a fixed set of tags and attributes.
   Supported: headings, paragraphs (a single newline is a line break), bold / italic / strikethrough, inline code and
   fenced code blocks, ordered / unordered lists (nested), blockquotes, tables, horizontal rules, links. Links: http(s)
   and same-origin paths only (rel=noopener noreferrer; http(s) open in a new tab). Images are never loaded: ![alt](u)
   shows its alt text. Record ids (runs, gaps, rule checks / proposals, task workspaces) become links to their pages.
   Bounded: the text is cut at MAX_CHARS, lists / quotes nest at most MAX_DEPTH deep, tables keep MAX_COLS x MAX_ROWS. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KageMd = api;
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const MAX_CHARS = 40000, MAX_LINES = 3000, MAX_DEPTH = 4, MAX_COLS = 20, MAX_ROWS = 300, MAX_INLINE = 400;
  const TAGS = new Set(['p', 'h3', 'h4', 'h5', 'strong', 'em', 'del', 'code', 'pre', 'ul', 'ol', 'li', 'blockquote', 'hr',
    'br', 'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'div', 'span']);
  const ATTRS = new Set(['href', 'title', 'class', 'rel', 'target', 'start']);
  const TRUNCATED = '… (too long; the rest is not shown)';

  // ---- links ----
  function safeHref(raw) {
    const u = String(raw || '').trim();
    if (!u || u.length > 2000 || /[\s"'<>`\\\u0000-\u001f\u007f]/.test(u)) return null;
    if (/^https?:\/\//i.test(u)) {
      try {
        const p = new URL(u);
        return (p.protocol === 'http:' || p.protocol === 'https:') && p.hostname ? p.href : null;
      } catch (e) { return null; }
    }
    // same-origin: an absolute path, never a protocol-relative or backslash form
    if (/^\/(?![\/\\])[A-Za-z0-9\-._~!$&()*+,;=:@%\/?#]*$/.test(u)) return u;
    return null;
  }
  function link(href, kids, cls) {
    const ext = /^https?:/i.test(href);
    const a = { href, rel: 'noopener noreferrer' };
    if (ext) a.target = '_blank';
    if (cls) a.class = cls;
    return ['a', a, ...kids];
  }

  // ---- record ids -> their pages ----
  const IDS = [
    [/^r\d{8}T\d{6}Z-[0-9a-f]{6}$/, (id) => '/runs/' + id],
    [/^bk[0-9a-f]{14}$/, (id) => '/gaps/' + id],
    [/^rc[0-9a-f]{14}$/, (id) => '/rules?check=' + id],
    [/^rp[0-9a-f]{14}$/, (id) => '/rules?proposal=' + id],
    [/^local-task-\d{8}-[a-z0-9][a-z0-9-]{0,80}$/, (id) => '/runs?task=' + encodeURIComponent(id)],
    [/^[A-Z][A-Z0-9]{1,9}-\d{1,6}-[a-z0-9][a-z0-9-]{1,80}$/, (id) => '/runs?task=' + encodeURIComponent(id)],
  ];
  const ID_SCAN = /\b(?:r\d{8}T\d{6}Z-[0-9a-f]{6}|bk[0-9a-f]{14}|rc[0-9a-f]{14}|rp[0-9a-f]{14}|local-task-\d{8}-[a-z0-9][a-z0-9-]{0,80}|[A-Z][A-Z0-9]{1,9}-\d{1,6}-[a-z0-9][a-z0-9-]{1,80})/;
  function idHref(id) {
    const clean = id.replace(/-+$/, '');
    for (const [re, to] of IDS) if (re.test(clean)) return to(clean);
    return null;
  }

  // ---- inline ----
  const PATTERNS = [  // [name, regex]; the earliest match wins, ties go to the first listed
    ['code', /(`+)([^`\n](?:[\s\S]*?[^`\n])?)\1(?!`)/],
    ['image', /!\[([^\]\n]{0,300})\]\(([^()\s]{0,2000})(?:\s+"[^"\n]{0,200}")?\)/],
    ['link', /\[([^\]\n]{1,500})\]\(([^()\s]{0,2000})(?:\s+"([^"\n]{0,200})")?\)/],
    ['auto', /<(https?:\/\/[^\s<>"'`]{1,2000})>/i],
    ['url', /https?:\/\/[^\s<>()"'`　-〿！-｠]{1,2000}/i],
    ['strong', /\*\*(?=\S)([\s\S]{0,2000}?\S)\*\*|__(?=\S)([\s\S]{0,2000}?\S)__/],
    ['del', /~~(?=\S)([\s\S]{0,2000}?\S)~~/],
    ['em', /\*(?=[^\s*])([^*\n]{0,2000}?[^\s*])\*|(?:^|(?<=[\s(（「“]))_(?=[^\s_])([^_\n]{0,2000}?[^\s_])_(?![A-Za-z0-9_])/],
    ['id', ID_SCAN],
  ];
  const GLOBAL = new Map(PATTERNS.map(([name, re]) => [name, new RegExp(re.source, re.flags + 'g')]));
  function inline(text, depth, budget) {
    // one left-to-right pass: each pattern's next match is searched from the current position and kept until passed,
    // so the work stays linear in the text (no re-scan of the rest per token)
    const out = [];
    const s = String(text);
    const next = new Map();
    let pos = 0;
    while (pos < s.length) {
      if (budget.n++ > MAX_INLINE * 50) { out.push(s.slice(pos)); break; }  // pathological input: the rest stays plain
      let best = null;
      for (const [name] of PATTERNS) {
        if (depth > 0 && (name === 'link' || name === 'auto' || name === 'url' || name === 'image')) continue;  // no links in links
        let hit = next.get(name);
        if (hit === undefined || (hit && hit.index < pos)) {
          const re = GLOBAL.get(name);
          re.lastIndex = pos;
          const m = re.exec(s);
          hit = m ? { index: m.index, m } : null;
          next.set(name, hit);
        }
        if (hit && (!best || hit.index < best.index)) best = { name, index: hit.index, m: hit.m };
      }
      if (!best) { out.push(s.slice(pos)); break; }
      const { name, m } = best;
      if (m.index > pos) out.push(s.slice(pos, m.index));
      let node = null;
      if (name === 'code') {
        const body = m[2].replace(/^ (.*) $/, '$1');
        const href = idHref(body.trim());
        node = href ? link(href, [['code', {}, body]], 'md-id') : ['code', {}, body];
      } else if (name === 'image') {
        node = m[1] ? ['span', { class: 'md-alt', title: 'Image not loaded' }, m[1]] : '';
      } else if (name === 'link') {
        const href = safeHref(m[2]);
        const kids = inline(m[1], depth + 1, budget);
        node = href ? link(href, kids) : ['span', {}, ...kids];
        if (href && m[3]) node[1].title = m[3];
      } else if (name === 'auto' || name === 'url') {
        let u = name === 'auto' ? m[1] : m[0];
        let tail = '';
        if (name === 'url') { const t = /[.,;:!?)\]]+$/.exec(u); if (t) { tail = t[0]; u = u.slice(0, -tail.length); } }
        const href = safeHref(u);
        node = href ? link(href, [u]) : u;
        if (tail) { out.push(node); node = tail; }
      } else if (name === 'strong' || name === 'del' || name === 'em') {
        node = [name, {}, ...inline(m[1] !== undefined ? m[1] : m[2], depth + 1, budget)];
      } else if (name === 'id') {
        const id = m[0].replace(/-+$/, '');
        const href = idHref(id);
        node = href ? link(href, [id], 'md-id') : id;
        if (id.length < m[0].length) { out.push(node); node = m[0].slice(id.length); }
      }
      if (node !== '' && node !== null) out.push(node);
      pos = m.index + Math.max(1, m[0].length);
    }
    return out;
  }
  function inlineLines(text, budget) {  // a single newline inside a paragraph is a line break
    const out = [];
    String(text).split('\n').forEach((line, i) => { if (i) out.push(['br', {}]); out.push(...inline(line, 0, budget)); });
    return out;
  }

  // ---- blocks ----
  const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+#.-]{0,30})\s*$/;
  const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  const HR = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
  const QUOTE = /^ {0,3}> ?(.*)$/;
  const ITEM = /^( *)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;
  function cells(line) {
    let s = line.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
    return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|')).slice(0, MAX_COLS);
  }
  function startsBlock(line, next) {
    return FENCE.test(line) || HEADING.test(line) || HR.test(line) || QUOTE.test(line) || ITEM.test(line) ||
      (line.includes('|') && next !== undefined && TABLE_SEP.test(next));
  }
  function blocks(lines, depth, budget) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m;
      if ((m = FENCE.exec(line))) {
        const fence = m[1], body = [];
        i++;
        while (i < lines.length && !(lines[i].trim().startsWith(fence[0].repeat(fence.length)) && !lines[i].trim().replace(new RegExp('^\\' + fence[0] + '+'), ''))) body.push(lines[i++]);
        i++;  // the closing fence (or the end)
        out.push(['pre', { class: 'md-code' }, ['code', m[2] ? { class: 'lang-' + m[2].replace(/[^\w-]/g, '') } : {}, body.join('\n')]]);
        continue;
      }
      if ((m = HEADING.exec(line))) {
        const level = Math.min(5, m[1].length + 2);  // # -> h3: the answer sits inside the page's own headings
        out.push(['h' + level, {}, ...inline(m[2], 0, budget)]);
        i++;
        continue;
      }
      if (HR.test(line)) { out.push(['hr', {}]); i++; continue; }
      if (QUOTE.test(line)) {
        const body = [];
        while (i < lines.length && (m = QUOTE.exec(lines[i]))) { body.push(m[1]); i++; }
        out.push(depth < MAX_DEPTH ? ['blockquote', {}, ...blocks(body, depth + 1, budget)] : ['p', {}, ...inlineLines(body.join('\n'), budget)]);
        continue;
      }
      if (line.includes('|') && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
        const head = cells(line);
        const align = cells(lines[i + 1]).map((c) => c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : '');
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
          if (rows.length < MAX_ROWS) rows.push(cells(lines[i]));
          i++;
        }
        const cell = (tag, text, j) => [tag, align[j] ? { class: 'md-' + align[j] } : {}, ...inline(text, 0, budget)];
        out.push(['div', { class: 'md-table' }, ['table', {},
          ['thead', {}, ['tr', {}, ...head.map((c, j) => cell('th', c, j))]],
          ['tbody', {}, ...rows.map((r) => ['tr', {}, ...head.map((_, j) => cell('td', r[j] || '', j))])]]]);
        continue;
      }
      if ((m = ITEM.exec(line))) {
        const indent = m[1].length, ordered = /\d/.test(m[2]);
        const items = [];
        while (i < lines.length) {
          const im = ITEM.exec(lines[i]);
          if (!im || im[1].length !== indent || /\d/.test(im[2]) !== ordered) {
            if (im && im[1].length > indent && items.length) { items[items.length - 1].push(lines[i]); i++; continue; }
            if (!im && lines[i].trim() && /^\s+/.test(lines[i]) && items.length) { items[items.length - 1].push(lines[i]); i++; continue; }
            break;
          }
          items.push([im[3]]);
          i++;
        }
        const start = ordered ? parseInt(m[2], 10) : 1;
        const lis = items.map((it) => {
          const first = it[0], rest = it.slice(1);
          const kids = inline(first, 0, budget);
          if (rest.length) {
            const pad = Math.min(...rest.filter((l) => l.trim()).map((l) => /^ */.exec(l)[0].length));
            const sub = rest.map((l) => l.slice(pad));
            if (depth < MAX_DEPTH) kids.push(...blocks(sub, depth + 1, budget));
            else kids.push(['br', {}], ...inlineLines(sub.join('\n'), budget));
          }
          return ['li', {}, ...kids];
        });
        out.push(ordered ? ['ol', start !== 1 ? { start: String(start) } : {}, ...lis] : ['ul', {}, ...lis]);
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && !(para.length && startsBlock(lines[i], lines[i + 1]))) para.push(lines[i++]);
      out.push(['p', {}, ...inlineLines(para.join('\n'), budget)]);
    }
    return out;
  }

  function parse(text) {
    let s = String(text == null ? '' : text).replace(/\r\n?/g, '\n');
    let cut = false;
    if (s.length > MAX_CHARS) { s = s.slice(0, MAX_CHARS); cut = true; }
    let lines = s.split('\n');
    if (lines.length > MAX_LINES) { lines = lines.slice(0, MAX_LINES); cut = true; }
    const tree = blocks(lines, 0, { n: 0 });
    if (cut) tree.push(['p', { class: 'md-cut' }, TRUNCATED]);
    return tree;
  }

  function render(tree, doc) {
    doc = doc || document;
    const frag = doc.createDocumentFragment();
    const build = (node) => {
      if (typeof node === 'string') return doc.createTextNode(node);
      const [tag, attrs, ...kids] = node;
      if (!TAGS.has(tag)) return doc.createTextNode('');
      const el = doc.createElement(tag);
      for (const [k, v] of Object.entries(attrs || {})) {
        if (!ATTRS.has(k)) continue;
        if (k === 'href') { const h = safeHref(v); if (!h) continue; el.setAttribute('href', h); continue; }
        el.setAttribute(k, String(v));
      }
      for (const k of kids) el.appendChild(build(k));
      return el;
    };
    for (const node of tree) frag.appendChild(build(node));
    return frag;
  }

  return { parse, render, safeHref, idHref, MAX_CHARS };
}));
