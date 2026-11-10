// The console's control entry (Control, Phase 2 C1; the console's home since S2): conversations with the control task.
// Text is set with textContent; a control answer goes through md.js (a safe Markdown subset built with createElement,
// never parsed as HTML). Writes carry the page token and JSON (same-origin, kage.observe.dashboard post_control).
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const STATE_LABEL = { received: 'Received', queued: 'Queued', working: 'Working', answered: 'Answered', failed: 'Failed' };
  const OPEN = new Set(['received', 'queued', 'working']);
  // C1b: a relay / launch from the console waits for the owner's click on a confirm card in Slack
  const PROPOSAL_LABEL = { proposed: 'Waiting for you to confirm on the card', deciding: 'Working', confirmed: 'Confirmed and carried out', cancelled: 'Cancelled',
    expired: 'Expired (not confirmed)', refused: 'Not carried out (the button was not clicked by you)', failed: 'Failed' };
  const ERROR_LABEL = {
    control_off: 'Control is not turned on yet', rate_limited: 'Too many messages in the last hour; try again later', text_empty: 'Write something first',
    text_too_long: 'Too long (4000 characters at most)', unknown_conversation: 'This conversation no longer exists',
    text_looks_like_it_contains_a_credential_use_a_one_time_share_link: 'This looks like it contains a secret, so it was not sent. Share secrets with a one-time share link',
    invalid_page_token: 'The page had expired; its token is refreshed, please send again', same_origin_required: 'The request came from the wrong origin',
    no_reply: 'The run ended without an answer', timeout: 'No answer for too long', control_task_not_bootstrapped: 'The Control task is not set up yet (control-bootstrap)',
    message_text_expired: 'The original message is past its retention period', only_a_failed_message_can_be_retried: 'Only a failed message can be retried',
    worker_config_changed_since_loaded_reload: 'The settings were changed elsewhere; reloaded',
  };
  const UA_LABEL = { chrome: 'Chrome', safari: 'Safari', firefox: 'Firefox', edge: 'Edge', curl: 'curl', python: 'Python',
    wget: 'wget', node: 'Node', other: 'Other program', none: 'No User-Agent' };
  const state = { token: null, conversation: null, data: null, timer: null, sending: false, pendingClient: null };

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const label = (code) => ERROR_LABEL[code] || code;
  const clock = (iso) => {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleString('en-GB', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  };

  function toast(text, tone) {
    const t = h('div', { class: 'toast', 'data-tone': tone || 'info', text });
    $('toasts').append(t);
    setTimeout(() => t.remove(), 5000);
  }

  function chip(stateName, text) {
    $('ctl-chip').dataset.state = stateName;
    $('ctl-chip-label').textContent = text;
    $('ctl-sync').textContent = new Date().toLocaleTimeString('en-GB', { hour12: false });
  }

  async function api(path, body) {
    const opts = body === undefined ? { cache: 'no-store' } : {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Page-Token': state.token || '' },
      body: JSON.stringify(body),
    };
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const err = new Error((data && data.error) || ('http_' + res.status));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function factsLine(m) {
    const f = m.facts || {};
    if (f.source === 'console') {
      const who = UA_LABEL[f.ua] || f.ua || '?';
      return f.browser_like ? 'Console · ' + who : 'Console · ' + who + ' · no browser headers (may not have been sent by you from the page)';
    }
    if (f.source === 'slack') return 'Slack card channel · typed by you';
    return f.source || m.source;
  }

  // C3: what the page can show of a message's words (stored, re-read from Slack, or why not)
  const TEXT_NOTE = {
    expired: 'The original message is past its retention period', in_slack: 'The original message is in Slack', unreadable: 'Could not read the original message',
  };
  function bodyFor(m, mine) {
    const body = h('div', { class: 'ctl-msg-body' });
    const ts = m.text_state;
    let placeholder = null;
    if (ts) {
      if (ts === 'busy') placeholder = 'Read too often; check again in ' + (m.retry_after || 60) + ' seconds';
      else if (TEXT_NOTE[ts]) placeholder = TEXT_NOTE[ts];
    } else if (m.slack && m.text === null) {
      placeholder = mine ? 'A message in the Slack card channel; the original is in Slack' : 'The answer is in the Slack thread';
    } else if (m.expired) {
      placeholder = TEXT_NOTE.expired;
    }
    if (placeholder !== null) {
      body.classList.add('is-placeholder');
      body.append(h('span', { text: placeholder }));
      const link = m.slack && m.slack.link;
      if (link && (ts === 'in_slack' || ts === 'unreadable' || !ts)) {
        body.append(' ', h('a', { href: link, target: '_blank', rel: 'noopener noreferrer', text: 'Open in Slack' }));
      }
      return body;
    }
    const text = m.text || '';
    if (mine || m.runtime || !window.KageMd) {  // the owner's own words (and system lines) stay plain text
      body.classList.add('is-plain');
      body.textContent = text;
    } else {  // a control answer: the safe Markdown subset (md.js; never parsed as HTML)
      body.classList.add('md');
      body.append(window.KageMd.render(window.KageMd.parse(text)));
    }
    return body;
  }

  function messageItem(m) {
    const mine = m.role === 'owner';
    const who = mine ? 'You' : m.runtime ? 'System' : 'Control';
    const li = h('li', { class: 'ctl-msg ' + (mine ? 'is-owner' : m.runtime ? 'is-system' : 'is-control'), 'data-state': m.state });
    const bubble = h('div', { class: 'ctl-bubble' });
    const head = h('div', { class: 'ctl-msg-head' },
      h('b', { class: 'ctl-who', text: who }),
      h('time', { datetime: m.created_at, text: clock(m.created_at) }));
    if (mine) {
      head.append(h('span', { class: 'pill', 'data-tone': m.state === 'failed' ? 'bad' : m.state === 'answered' ? 'good' : 'warn',
        text: STATE_LABEL[m.state] || m.state }));
    } else if (m.needs_answer) {
      head.append(h('span', { class: 'pill', 'data-tone': 'warn', text: 'Waiting for your answer' }));
    }
    bubble.append(head, bodyFor(m, mine));
    if (m.text_state === 'slack_edited') {
      bubble.append(h('div', { class: 'ctl-msg-note', text: 'Edited in Slack (the edited text was not acted on)' }));
    }
    if (mine) {
      if (m.relays && m.relays.length) {
        bubble.append(h('ul', { class: 'ctl-relays' }, m.relays.map((r) => h('li', { class: 'ctl-relay' },
          h('span', { class: 'ctl-relay-kind', text: r.mode === 'launch' ? 'New task' : 'Relayed to' }),
          h('a', { class: 'ctl-relay-task', href: '/runs?task=' + encodeURIComponent(r.task_id) }, h('code', { text: r.task_id })),
          h('small', { text: clock(r.at) })))));
      }
      if (m.proposals && m.proposals.length) {
        bubble.append(h('ul', { class: 'ctl-relays ctl-proposals' }, m.proposals.map((p) => h('li', { class: 'ctl-relay', 'data-state': p.state },
          h('span', { class: 'ctl-relay-kind', text: p.kind === 'launch' ? 'New task' : 'Relay to' }),
          p.kind === 'launch' ? h('span', { class: 'ctl-relay-title', text: '“' + (p.title || '') + '”' + (p.linear ? ' (create a ticket)' : '') })
            : h('a', { class: 'ctl-relay-task', href: '/runs?task=' + encodeURIComponent(p.task_id || '') }, h('code', { text: p.task_id || '' })),
          h('small', { text: (PROPOSAL_LABEL[p.state] || p.state) + (p.error ? ': ' + label(p.error) : '') })))));
      }
      bubble.append(h('div', { class: 'ctl-msg-facts' + (m.facts && m.facts.source === 'console' && !m.facts.browser_like ? ' is-odd' : ''),
        text: 'Source: ' + factsLine(m) }));
      if (m.state === 'failed') {
        bubble.append(h('div', { class: 'ctl-msg-error' },
          h('span', { text: 'Could not finish: ' + label(m.error || 'failed') }),
          h('button', { type: 'button', class: 'btn sm ghost', text: 'Retry', onclick: () => retry(m.id) })));
      } else if (m.state === 'received' && state.data && state.data.waiting) {
        bubble.append(h('div', { class: 'ctl-msg-error is-wait', text: 'Waiting on: ' + label(state.data.waiting) }));
      }
      if (m.run_id) bubble.append(h('a', { class: 'ctl-run', href: '/runs/' + m.run_id, text: 'View this run →' }));
    }
    li.append(h('div', { class: 'ctl-avatar', 'aria-hidden': 'true', text: mine ? 'You' : m.runtime ? 'Sys' : 'Ctl' }), bubble);
    return li;
  }

  // C3: whether the owner's words of a Slack conversation were read this time
  function wordsLine(w) {
    if (!w || !w.state || w.state === 'none' || w.state === 'not_read') return null;
    if (w.state === 'read' || w.state === 'cached') {
      return (w.state === 'cached' ? 'Slack original (from a cache under a minute old)' : 'Original read from Slack') + (w.partial ? '; a few could not be read' : '');
    }
    if (w.state === 'busy') return 'Read too often; check again in ' + (w.retry_after || 60) + ' seconds';
    if (w.state === 'unreadable') return 'Could not read the Slack original' + (w.error ? ' (' + label(w.error) + ')' : '');
    return null;
  }

  function threadHead(d) {
    const head = $('ctl-thread-head');
    if (!head) return;
    const conv = d.conversation || null;
    const title = conv ? (conv.title || (conv.source === 'slack' ? 'Slack conversation' : 'Conversation')) : 'New conversation';
    const sub = conv ? (conv.source === 'slack' ? 'From the Slack card channel' : 'From the console') : 'Ask how something stands, or say what needs doing';
    const kids = [h('div', { class: 'ctl-thread-title' }, h('b', { text: title }), h('small', { text: sub }))];
    const side = h('div', { class: 'ctl-thread-side' });
    const slack = conv && conv.slack;
    if (slack && slack.link) side.append(h('a', { class: 'btn sm ghost', href: slack.link, target: '_blank', rel: 'noopener noreferrer', text: 'Open in Slack' }));
    const words = wordsLine(conv && conv.words);
    if (words) side.prepend(h('span', { class: 'ctl-words' + (conv.words.state === 'busy' || conv.words.state === 'unreadable' ? ' is-warn' : ''), text: words }));
    kids.push(side);
    head.replaceChildren(...kids);
  }

  const SUGGEST = ['What is being worked on today?', 'Which task is waiting for my decision?', 'Pause a task until I look at it tomorrow'];
  function emptyThread() {
    return h('li', { class: 'ctl-empty' },
      h('div', { class: 'ctl-empty-mark', 'aria-hidden': 'true' }),
      h('b', { text: state.conversation ? 'No messages in this conversation yet' : 'Ask how things stand, or hand over a job' }),
      h('p', { text: 'Control finds the matching task and answers once it has looked; for a job to be done, it relays your exact words to that task.' }),
      state.conversation ? null : h('div', { class: 'ctl-suggest' }, SUGGEST.map((t) => h('button', {
        type: 'button', class: 'ctl-chip', text: t, onclick: () => { const ta = $('ctl-text'); ta.value = t; ta.focus(); } }))));
  }

  function renderConversations(d) {
    const list = $('ctl-convs');
    list.replaceChildren(...(d.conversations || []).map((c) => {
      const li = h('li', {}, h('button', {
        type: 'button', class: 'ctl-conv' + (c.conversation_id === state.conversation ? ' is-on' : ''),
        'aria-pressed': String(c.conversation_id === state.conversation),
        onclick: () => { state.conversation = c.conversation_id; refresh(); },
      }, h('span', { class: 'ctl-conv-title', text: c.title || (c.source === 'slack' ? 'Slack conversation' : '(untitled)') }),
        h('small', { text: clock(c.updated_at) + (c.last_state ? ' · ' + (STATE_LABEL[c.last_state] || c.last_state) : '') })));
      return li;
    }));
    if (!(d.conversations || []).length) list.append(h('li', { class: 'empty', text: 'No conversations yet' }));
  }

  function renderSettings(d) {
    const body = $('ctl-settings-body');
    const providers = (d.enums && d.enums.providers) || ['claude', 'codex'];
    const efforts = (d.enums && d.enums.efforts) || [];
    const sel = (name, options, value) => h('select', { name },
      options.map((o) => h('option', { value: o, selected: o === (value || '') ? true : null, text: o || 'Default' })));
    const enabled = h('input', { type: 'checkbox', name: 'enabled', checked: d.enabled ? true : null });
    const model = h('input', { type: 'text', name: 'model', value: d.model || '', placeholder: 'Default (follows the provider setting)' });
    const form = h('form', { class: 'ctl-settings-form' },
      h('label', { class: 'field' }, enabled, h('span', { text: ' Turn on Control' })),
      h('label', { class: 'field' }, h('span', { text: 'Provider' }), sel('provider', providers, d.provider)),
      h('label', { class: 'field' }, h('span', { text: 'Model' }), model),
      h('label', { class: 'field' }, h('span', { text: 'Effort' }), sel('effort', ['', ...efforts], d.effort)),
      h('p', { class: 'note', text: 'Control task: ' + d.task }),
      h('button', { type: 'submit', class: 'btn sm primary', text: 'Save' }));
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const changes = { enabled: enabled.checked, provider: form.provider.value,
        model: model.value.trim() || null, effort: form.effort.value || null };
      if (!window.confirm((changes.enabled ? 'Turn on' : 'Turn off') + ' Control and save the settings?')) return;
      try {
        const r = await api('/api/control/settings', { changes, digest: d.digest });
        if (r.page_token) state.token = r.page_token;
        toast('Saved', 'good');
        refresh();
      } catch (e) {
        toast('Not saved: ' + label(e.message), 'bad');
        if (e.message === 'invalid_page_token') await newToken();
        refresh();
      }
    });
    body.replaceChildren(form);
  }

  // 0.78.6 (F18): the host's disk / memory / CPU (kage.core.system_status, probed by the console at most every 10 s);
  // a level over a system.* threshold turns the panel and the health nav item amber / red.
  const SYS_LEVEL = { ok: 'Normal', warning: 'High', critical: 'Critical' };
  const fmt = (t, v) => t.replace(/\{(\w+)\}/g, (_, k) => (v[k] === undefined || v[k] === null ? '—' : v[k]));
  function bytes(n) {
    if (n === null || n === undefined) return '—';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let i = 0;
    while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
    return (i ? n.toFixed(n >= 100 ? 0 : 1) : String(Math.round(n))) + ' ' + u[i];
  }
  function navAlert(level) {
    const nav = document.querySelector('.site-nav [data-nav="health"]');
    if (!nav) return;
    if (level === 'warning' || level === 'critical') {
      nav.dataset.alert = level;
      nav.setAttribute('aria-label', nav.textContent.trim() + ' · System status: ' + SYS_LEVEL[level]);
    } else {
      delete nav.dataset.alert;
      nav.removeAttribute('aria-label');
    }
  }
  function sysRow(name, pct, detail, level) {
    const bar = h('i', {});
    bar.style.width = Math.max(0, Math.min(100, pct || 0)) + '%';
    return h('span', { class: 'ctl-sys-row', 'data-level': level || 'ok' },
      h('span', { class: 'ctl-sys-name', text: name }),
      h('b', { class: 'ctl-sys-pct', text: pct === null || pct === undefined ? '—' : Math.round(pct) + '%' }),
      h('span', { class: 'ctl-sys-bar', 'aria-hidden': 'true' }, bar),
      h('small', { class: 'ctl-sys-detail', text: detail }));
  }
  function renderSystem(sys) {
    const box = $('ctl-sys');
    if (!box) return;
    if (!sys || sys.error) {
      box.dataset.level = 'unknown';
      $('ctl-sys-badge').textContent = '—';
      $('ctl-sys-rows').replaceChildren();
      $('ctl-sys-foot').textContent = sys && sys.error ? sys.error : '';
      navAlert(null);
      return;
    }
    const lv = sys.levels || {};
    const disk = sys.disk || {};
    const mem = sys.memory || {};
    const cpu = sys.cpu || {};
    const pr = sys.processes || {};
    box.dataset.level = sys.alert || 'ok';
    $('ctl-sys-badge').textContent = sys.enabled === false ? 'Checks off' : (SYS_LEVEL[sys.alert] || sys.alert);
    const memDetail = fmt('{used} of {limit}', { used: bytes(mem.used), limit: bytes(mem.limit) }) + (mem.limit && !mem.limited ? ' (no limit)' : '');
    const cpuDetail = [cpu.cores_used !== null && cpu.cores_used !== undefined ? fmt('{used} of {limit} cores', { used: cpu.cores_used, limit: cpu.limit_cores }) : null,
      cpu.loadavg ? fmt('load {l}', { l: cpu.loadavg[0] }) : null].filter(Boolean).join(' · ');
    $('ctl-sys-rows').replaceChildren(
      sysRow('Disk', disk.pct, fmt('{free} free of {total}', { free: bytes(disk.free), total: bytes(disk.total) }), lv.disk),
      sysRow('Memory', mem.pct, memDetail, lv.memory),
      sysRow('CPU', cpu.pct, cpuDetail || '—', 'ok'));
    $('ctl-sys-foot').textContent = [pr.agent_runs !== null && pr.agent_runs !== undefined ? fmt('{n} background runs', { n: pr.agent_runs }) : null,
      pr.kage_children !== null && pr.kage_children !== undefined ? fmt('{n} Kage child processes', { n: pr.kage_children }) : null].filter(Boolean).join(' · ');
    navAlert(sys.alert);
  }

  function render(d) {
    state.data = d;
    const banner = $('ctl-banner');
    if (!d.enabled) {
      banner.hidden = false;
      banner.replaceChildren(h('b', { text: 'Control is not turned on yet.' }),
        h('span', { text: ' Turn it on in “Control settings”; until then, messages sent here are not processed.' }));
      $('ctl-settings').open = true;
    } else if (d.config_error) {
      banner.hidden = false;
      banner.replaceChildren(h('b', { text: 'Could not read the config: ' }), h('code', { text: d.config_error }));
    } else {
      banner.hidden = true;
    }
    renderConversations(d);
    renderSettings(d);
    renderSystem(d.system);
    const list = $('ctl-messages');
    const msgs = d.messages || [];
    const atEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    list.replaceChildren(...msgs.map(messageItem));
    threadHead(d);
    if (!msgs.length) list.append(emptyThread());
    if (atEnd) list.scrollTop = list.scrollHeight;
    $('ctl-send').disabled = !d.enabled || state.sending;
    chip(d.enabled ? 'ok' : 'warn', d.enabled ? 'Control is on' : 'Control is off');
    const open = msgs.some((m) => m.role === 'owner' && OPEN.has(m.state));
    // C3: words not readable this time come back after retry_after seconds
    const waits = [...msgs.filter((m) => m.text_state === 'busy').map((m) => m.retry_after || 60),
      ...(d.conversation && d.conversation.words && d.conversation.words.state === 'busy' ? [d.conversation.words.retry_after || 60] : [])];
    const again = open ? 2000 : 15000;
    schedule(waits.length ? Math.min(again, Math.max(1, Math.min(...waits)) * 1000) : again);
  }

  function schedule(ms) {
    clearTimeout(state.timer);
    state.timer = setTimeout(refresh, ms);
  }

  async function newToken() {
    const d = await api('/api/control?token=new' + (state.conversation ? '&conversation=' + encodeURIComponent(state.conversation) : ''));
    state.token = d.page_token || state.token;
    return d;
  }

  async function refresh() {
    try {
      const q = state.conversation ? '?conversation=' + encodeURIComponent(state.conversation) : '';
      render(await api('/api/control' + q));
    } catch (e) {
      if (e.message === 'unknown_conversation') { state.conversation = null; return refresh(); }
      chip('bad', 'Cannot connect');
      schedule(5000);
    }
  }

  function clientId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }

  async function send(ev) {
    if (ev) ev.preventDefault();
    const text = $('ctl-text').value.trim();
    if (!text || state.sending) return;
    state.sending = true;
    $('ctl-send').disabled = true;
    state.pendingClient = state.pendingClient || clientId();  // the same id on a retry after a network error
    try {
      const r = await api('/api/control/messages', { text, client_id: state.pendingClient,
        conversation: state.conversation });
      state.conversation = r.message.conversation;
      state.pendingClient = null;
      $('ctl-text').value = '';
    } catch (e) {
      if (e.message === 'invalid_page_token') { await newToken(); }
      if (e.status && e.status < 500) state.pendingClient = null;  // refused: a new message next time
      toast('Not sent: ' + label(e.message), 'bad');
    } finally {
      state.sending = false;
      refresh();
    }
  }

  async function retry(id) {
    try {
      await api('/api/control/retry', { message: id });
      toast('Queued again', 'good');
    } catch (e) {
      if (e.message === 'invalid_page_token') await newToken();
      toast('Could not retry: ' + label(e.message), 'bad');
    }
    refresh();
  }

  $('ctl-form').addEventListener('submit', send);
  $('ctl-text').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) send(ev);
  });
  $('ctl-new').addEventListener('click', () => { state.conversation = null; refresh(); $('ctl-text').focus(); });
  const fromUrl = new URLSearchParams(location.search).get('conversation');
  if (fromUrl && /^cv[0-9a-f]{14}$/.test(fromUrl)) state.conversation = fromUrl;
  newToken().then(render).catch(() => refresh());
})();
