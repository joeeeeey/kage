/* Kage · the console shell, on every page: marks the current page in the global nav (and scrolls it into view on a
   narrow screen), aligns the header of a numeric table column with its numbers, mutes a table cell that holds no
   value, names each cell after its column (on a phone a table's rows show as small cards), shows the full text of a
   cell cut by an ellipsis as its tooltip, and a slim progress line while a request is slow. It reads the page; it
   never changes data or what a page fetches. */
(function () {
  'use strict';
  const path = (document.body.dataset.path || location.pathname).replace(/\/+$/, '') || '/';
  const key = path === '/' || path === '/control' ? 'control' : path === '/scene' ? 'scene' : path.split('/')[1];

  function markNav() {
    for (const a of document.querySelectorAll('.site-nav [data-nav]')) {
      const on = a.dataset.nav === key;
      if (on) { a.setAttribute('aria-current', 'page'); a.setAttribute('aria-pressed', 'true'); }
      else { a.removeAttribute('aria-current'); a.setAttribute('aria-pressed', 'false'); }
    }
    const cur = document.querySelector('.site-nav [aria-current="page"]');
    const nav = cur && cur.closest('.site-nav');
    if (cur && nav && nav.scrollWidth > nav.clientWidth) {
      nav.scrollLeft = Math.max(0, cur.offsetLeft - (nav.clientWidth - cur.offsetWidth) / 2);
    }
  }

  // a column of plain numbers (counts, ratios, percentages; not times or dates) is right-aligned, its header too
  const NUMERIC = /^[\s\d.,/%+\-—–]*\d[\s\d.,/%+\-—–]*$|^[—–-]$/;
  // no value: a dash, or only a note in brackets such as （没有日志）
  const NIL = /^(?:[—–-]|（[^（）]*）)$/;
  // a text's width in Latin letters: a CJK character or a full-width sign counts two
  const width = (t) => t.length + (t.match(/[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]/g) || []).length;
  function alignTables(rootEl) {
    for (const table of rootEl.querySelectorAll('table.ops-table')) {  // again after each re-render (rows come and go)
      const head = table.tHead && table.tHead.rows[0];
      const body = table.tBodies[0];
      if (!head || !body || !body.rows.length) continue;
      // rows with a cell per column (not a heading row that spans the table)
      const rows = [...body.rows].filter((r) => r.cells.length === head.cells.length);
      const labels = [...head.cells].map((th) => th.textContent.trim());
      for (const r of rows) {
        [...r.cells].forEach((td, j) => {
          const text = td.textContent.trim();
          td.classList.toggle('is-nil', NIL.test(text));
          // on a phone the rows become small cards: each value under its column's name, a long one across the card
          if (td.dataset.label !== labels[j]) td.dataset.label = labels[j];
          td.classList.toggle('is-long', width(text) > 24 || !!td.querySelector('table, pre, ul, ol'));
        });
      }
      table.classList.add('is-stackable');
      [...head.cells].forEach((th, j) => {
        const cells = rows.map((r) => r.cells[j]).filter(Boolean);
        const texts = cells.map((td) => td.textContent.trim());
        if (!cells.length) return;
        // the first column names the row (a date, a task): it stays left-aligned
        if (j > 0 && texts.some((t) => /\d/.test(t)) && texts.every((t) => NUMERIC.test(t))) {
          th.classList.add('is-numeric');
          for (const td of cells) td.classList.add('is-numeric');
        }
        // a column of short values (a state, a count) never breaks inside a value
        if (texts.every((t) => t.length <= 8)) for (const td of cells) td.classList.add('is-short');
      });
    }
  }
  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; alignTables(document); });
  });

  // the full text of a cell or label cut by an ellipsis, as its tooltip
  function tip(ev) {
    const el = ev.target && ev.target.closest && ev.target.closest('td, .mono, .pd-title, .chip-text b, .ctl-conv-title');
    if (!el || el.hasAttribute('title')) return;
    if (el.scrollWidth > el.clientWidth + 1) el.setAttribute('title', el.textContent.trim().slice(0, 500));
  }

  // a slim progress line under the top bar while any request takes longer than a moment
  let inflight = 0, timer = 0;
  const bar = document.createElement('div');
  bar.className = 'shell-progress';
  bar.setAttribute('aria-hidden', 'true');
  function busy(delta) {
    inflight = Math.max(0, inflight + delta);
    clearTimeout(timer);
    if (inflight) timer = setTimeout(() => bar.classList.add('on'), 350);
    else bar.classList.remove('on');
  }
  if (window.fetch) {
    const fetch0 = window.fetch.bind(window);
    window.fetch = function (...args) {
      busy(1);
      return fetch0(...args).finally(() => busy(-1));
    };
  }

  function start() {
    markNav();
    const top = document.querySelector('.site-bar');
    if (top) top.appendChild(bar);
    alignTables(document);
    observer.observe(document.body, { childList: true, subtree: true });
    document.addEventListener('mouseover', tip, { passive: true });
    document.addEventListener('focusin', tip);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
