// The console's control entry (总控, Phase 2 C1; the console's home since S2): conversations with the control task.
// Text is set with textContent; a control answer goes through md.js (a safe Markdown subset built with createElement,
// never parsed as HTML). Writes carry the page token and JSON (same-origin, kage.observe.dashboard post_control).
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const STATE_LABEL = { received: '已收到', queued: '排队中', working: '处理中', answered: '已回答', failed: '失败' };
  const OPEN = new Set(['received', 'queued', 'working']);
  // C1b: a relay / launch from the console waits for the owner's click on a confirm card in Slack
  const PROPOSAL_LABEL = { proposed: '待你在卡片上点确认', deciding: '处理中', confirmed: '已确认并执行', cancelled: '已取消',
    expired: '已过期（没有确认）', refused: '没有执行（不是你本人点的按钮）', failed: '失败' };
  const ERROR_LABEL = {
    control_off: '总控还没开启', rate_limited: '一小时内发得太多了，稍后再试', text_empty: '先写点什么',
    text_too_long: '太长了（最多 4000 字）', unknown_conversation: '这个对话不存在了',
    text_looks_like_it_contains_a_credential_use_a_one_time_share_link: '看起来里面有密钥，没有发送。密钥请用一次性分享链接',
    invalid_page_token: '页面过期了，已刷新令牌，请再发一次', same_origin_required: '请求来源不对',
    no_reply: '这次运行结束了但没有回答', timeout: '太久没回答', control_task_not_bootstrapped: '总控任务还没建好（control-bootstrap）',
    message_text_expired: '原文已过保留期', only_a_failed_message_can_be_retried: '只有失败的消息能重试',
    worker_config_changed_since_loaded_reload: '设置在别处改过了，已重新加载',
  };
  const UA_LABEL = { chrome: 'Chrome', safari: 'Safari', firefox: 'Firefox', edge: 'Edge', curl: 'curl', python: 'Python',
    wget: 'wget', node: 'Node', other: '其他程序', none: '无 User-Agent' };
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
    return d.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  };

  function toast(text, tone) {
    const t = h('div', { class: 'toast', 'data-tone': tone || 'info', text });
    $('toasts').append(t);
    setTimeout(() => t.remove(), 5000);
  }

  function chip(stateName, text) {
    $('ctl-chip').dataset.state = stateName;
    $('ctl-chip-label').textContent = text;
    $('ctl-sync').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
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
      return f.browser_like ? '控制台 · ' + who : '控制台 · ' + who + ' · 没有浏览器标头（可能不是你在页面上发的）';
    }
    if (f.source === 'slack') return 'Slack 卡片频道 · 你本人输入';
    return f.source || m.source;
  }

  // C3: what the page can show of a message's words (stored, re-read from Slack, or why not)
  const TEXT_NOTE = {
    expired: '原文已过保留期', in_slack: '原文在 Slack', unreadable: '无法读取原文',
  };
  function bodyFor(m, mine) {
    const body = h('div', { class: 'ctl-msg-body' });
    const ts = m.text_state;
    let placeholder = null;
    if (ts) {
      if (ts === 'busy') placeholder = '读取太频繁，' + (m.retry_after || 60) + ' 秒后再看';
      else if (TEXT_NOTE[ts]) placeholder = TEXT_NOTE[ts];
    } else if (m.slack && m.text === null) {
      placeholder = mine ? 'Slack 卡片频道里的消息，原文在 Slack' : '回答在 Slack 讨论串里';
    } else if (m.expired) {
      placeholder = TEXT_NOTE.expired;
    }
    if (placeholder !== null) {
      body.classList.add('is-placeholder');
      body.append(h('span', { text: placeholder }));
      const link = m.slack && m.slack.link;
      if (link && (ts === 'in_slack' || ts === 'unreadable' || !ts)) {
        body.append(' ', h('a', { href: link, target: '_blank', rel: 'noopener noreferrer', text: '在 Slack 里打开' }));
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
    const who = mine ? '你' : m.runtime ? '系统' : '总控';
    const li = h('li', { class: 'ctl-msg ' + (mine ? 'is-owner' : m.runtime ? 'is-system' : 'is-control'), 'data-state': m.state });
    const bubble = h('div', { class: 'ctl-bubble' });
    const head = h('div', { class: 'ctl-msg-head' },
      h('b', { class: 'ctl-who', text: who }),
      h('time', { datetime: m.created_at, text: clock(m.created_at) }));
    if (mine) {
      head.append(h('span', { class: 'pill', 'data-tone': m.state === 'failed' ? 'bad' : m.state === 'answered' ? 'good' : 'warn',
        text: STATE_LABEL[m.state] || m.state }));
    } else if (m.needs_answer) {
      head.append(h('span', { class: 'pill', 'data-tone': 'warn', text: '等你回答' }));
    }
    bubble.append(head, bodyFor(m, mine));
    if (m.text_state === 'slack_edited') {
      bubble.append(h('div', { class: 'ctl-msg-note', text: '已在 Slack 修改（未按修改后的内容执行）' }));
    }
    if (mine) {
      if (m.relays && m.relays.length) {
        bubble.append(h('ul', { class: 'ctl-relays' }, m.relays.map((r) => h('li', { class: 'ctl-relay' },
          h('span', { class: 'ctl-relay-kind', text: r.mode === 'launch' ? '新开任务' : '已转给' }),
          h('a', { class: 'ctl-relay-task', href: '/runs?task=' + encodeURIComponent(r.task_id) }, h('code', { text: r.task_id })),
          h('small', { text: clock(r.at) })))));
      }
      if (m.proposals && m.proposals.length) {
        bubble.append(h('ul', { class: 'ctl-relays ctl-proposals' }, m.proposals.map((p) => h('li', { class: 'ctl-relay', 'data-state': p.state },
          h('span', { class: 'ctl-relay-kind', text: p.kind === 'launch' ? '新开任务' : '转给' }),
          p.kind === 'launch' ? h('span', { class: 'ctl-relay-title', text: '「' + (p.title || '') + '」' + (p.linear ? '（建工单）' : '') })
            : h('a', { class: 'ctl-relay-task', href: '/runs?task=' + encodeURIComponent(p.task_id || '') }, h('code', { text: p.task_id || '' })),
          h('small', { text: (PROPOSAL_LABEL[p.state] || p.state) + (p.error ? '：' + label(p.error) : '') })))));
      }
      bubble.append(h('div', { class: 'ctl-msg-facts' + (m.facts && m.facts.source === 'console' && !m.facts.browser_like ? ' is-odd' : ''),
        text: '来源：' + factsLine(m) }));
      if (m.state === 'failed') {
        bubble.append(h('div', { class: 'ctl-msg-error' },
          h('span', { text: '没能完成：' + label(m.error || 'failed') }),
          h('button', { type: 'button', class: 'btn sm ghost', text: '重试', onclick: () => retry(m.id) })));
      } else if (m.state === 'received' && state.data && state.data.waiting) {
        bubble.append(h('div', { class: 'ctl-msg-error is-wait', text: '在等：' + label(state.data.waiting) }));
      }
      if (m.run_id) bubble.append(h('a', { class: 'ctl-run', href: '/runs/' + m.run_id, text: '看这次运行 →' }));
    }
    li.append(h('div', { class: 'ctl-avatar', 'aria-hidden': 'true', text: mine ? '你' : m.runtime ? '系' : '控' }), bubble);
    return li;
  }

  // C3: whether the owner's words of a Slack conversation were read this time
  function wordsLine(w) {
    if (!w || !w.state || w.state === 'none' || w.state === 'not_read') return null;
    if (w.state === 'read' || w.state === 'cached') {
      return (w.state === 'cached' ? 'Slack 原文（一分钟内的缓存）' : '已从 Slack 读取原文') + (w.partial ? '，有几条没读到' : '');
    }
    if (w.state === 'busy') return '读取太频繁，' + (w.retry_after || 60) + ' 秒后再看';
    if (w.state === 'unreadable') return '无法读取 Slack 原文' + (w.error ? '（' + label(w.error) + '）' : '');
    return null;
  }

  function threadHead(d) {
    const head = $('ctl-thread-head');
    if (!head) return;
    const conv = d.conversation || null;
    const title = conv ? (conv.title || (conv.source === 'slack' ? 'Slack 对话' : '对话')) : '新对话';
    const sub = conv ? (conv.source === 'slack' ? '来自 Slack 卡片频道' : '来自控制台') : '写下你想问的事或要交代的事';
    const kids = [h('div', { class: 'ctl-thread-title' }, h('b', { text: title }), h('small', { text: sub }))];
    const side = h('div', { class: 'ctl-thread-side' });
    const slack = conv && conv.slack;
    if (slack && slack.link) side.append(h('a', { class: 'btn sm ghost', href: slack.link, target: '_blank', rel: 'noopener noreferrer', text: '在 Slack 打开' }));
    const words = wordsLine(conv && conv.words);
    if (words) side.prepend(h('span', { class: 'ctl-words' + (conv.words.state === 'busy' || conv.words.state === 'unreadable' ? ' is-warn' : ''), text: words }));
    kids.push(side);
    head.replaceChildren(...kids);
  }

  const SUGGEST = ['今天有在做哪些事情？', '哪个任务在等我决定？', '把某个任务先停一下，等我明天看'];
  function emptyThread() {
    return h('li', { class: 'ctl-empty' },
      h('div', { class: 'ctl-empty-mark', 'aria-hidden': 'true' }),
      h('b', { text: state.conversation ? '这个对话还没有消息' : '问进度，或交代一件事' }),
      h('p', { text: '总控会找到对应的任务，看完情况再回答你；要交代的事，它会把你的原话转给那个任务。' }),
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
      }, h('span', { class: 'ctl-conv-title', text: c.title || (c.source === 'slack' ? 'Slack 对话' : '（无标题）') }),
        h('small', { text: clock(c.updated_at) + (c.last_state ? ' · ' + (STATE_LABEL[c.last_state] || c.last_state) : '') })));
      return li;
    }));
    if (!(d.conversations || []).length) list.append(h('li', { class: 'empty', text: '还没有对话' }));
  }

  function renderSettings(d) {
    const body = $('ctl-settings-body');
    const providers = (d.enums && d.enums.providers) || ['claude', 'codex'];
    const efforts = (d.enums && d.enums.efforts) || [];
    const sel = (name, options, value) => h('select', { name },
      options.map((o) => h('option', { value: o, selected: o === (value || '') ? true : null, text: o || '默认' })));
    const enabled = h('input', { type: 'checkbox', name: 'enabled', checked: d.enabled ? true : null });
    const model = h('input', { type: 'text', name: 'model', value: d.model || '', placeholder: '默认（跟随 provider 设置）' });
    const form = h('form', { class: 'ctl-settings-form' },
      h('label', { class: 'field' }, enabled, h('span', { text: ' 开启总控' })),
      h('label', { class: 'field' }, h('span', { text: 'Provider' }), sel('provider', providers, d.provider)),
      h('label', { class: 'field' }, h('span', { text: '模型' }), model),
      h('label', { class: 'field' }, h('span', { text: 'Effort' }), sel('effort', ['', ...efforts], d.effort)),
      h('p', { class: 'note', text: '总控任务：' + d.task }),
      h('button', { type: 'submit', class: 'btn sm primary', text: '保存' }));
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const changes = { enabled: enabled.checked, provider: form.provider.value,
        model: model.value.trim() || null, effort: form.effort.value || null };
      if (!window.confirm((changes.enabled ? '开启' : '关闭') + '总控并保存设置？')) return;
      try {
        const r = await api('/api/control/settings', { changes, digest: d.digest });
        if (r.page_token) state.token = r.page_token;
        toast('已保存', 'good');
        refresh();
      } catch (e) {
        toast('没保存：' + label(e.message), 'bad');
        if (e.message === 'invalid_page_token') await newToken();
        refresh();
      }
    });
    body.replaceChildren(form);
  }

  // 0.78.6 (F18): the host's disk / memory / CPU (kage.core.system_status, probed by the console at most every 10 s);
  // a level over a system.* threshold turns the panel and the health nav item amber / red.
  const SYS_LEVEL = { ok: '正常', warning: '偏高', critical: '告警' };
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
      nav.setAttribute('aria-label', nav.textContent.trim() + ' · 系统状态：' + SYS_LEVEL[level]);
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
    $('ctl-sys-badge').textContent = sys.enabled === false ? '检查已关闭' : (SYS_LEVEL[sys.alert] || sys.alert);
    const memDetail = fmt('{used} / {limit}', { used: bytes(mem.used), limit: bytes(mem.limit) }) + (mem.limit && !mem.limited ? '（无上限）' : '');
    const cpuDetail = [cpu.cores_used !== null && cpu.cores_used !== undefined ? fmt('{used} / {limit} 核', { used: cpu.cores_used, limit: cpu.limit_cores }) : null,
      cpu.loadavg ? fmt('负载 {l}', { l: cpu.loadavg[0] }) : null].filter(Boolean).join(' · ');
    $('ctl-sys-rows').replaceChildren(
      sysRow('磁盘', disk.pct, fmt('剩 {free} / 共 {total}', { free: bytes(disk.free), total: bytes(disk.total) }), lv.disk),
      sysRow('内存', mem.pct, memDetail, lv.memory),
      sysRow('CPU', cpu.pct, cpuDetail || '—', 'ok'));
    $('ctl-sys-foot').textContent = [pr.agent_runs !== null && pr.agent_runs !== undefined ? fmt('后台运行 {n}', { n: pr.agent_runs }) : null,
      pr.kage_children !== null && pr.kage_children !== undefined ? fmt('Kage 子进程 {n}', { n: pr.kage_children }) : null].filter(Boolean).join(' · ');
    navAlert(sys.alert);
  }

  function render(d) {
    state.data = d;
    const banner = $('ctl-banner');
    if (!d.enabled) {
      banner.hidden = false;
      banner.replaceChildren(h('b', { text: '总控还没开启。' }),
        h('span', { text: ' 在“总控设置”里打开；打开前，这里发的话不会被处理。' }));
      $('ctl-settings').open = true;
    } else if (d.config_error) {
      banner.hidden = false;
      banner.replaceChildren(h('b', { text: '配置读不出来：' }), h('code', { text: d.config_error }));
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
    chip(d.enabled ? 'ok' : 'warn', d.enabled ? '总控已开启' : '总控未开启');
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
      chip('bad', '连不上');
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
      toast('没发出去：' + label(e.message), 'bad');
    } finally {
      state.sending = false;
      refresh();
    }
  }

  async function retry(id) {
    try {
      await api('/api/control/retry', { message: id });
      toast('已重新排队', 'good');
    } catch (e) {
      if (e.message === 'invalid_page_token') await newToken();
      toast('没能重试：' + label(e.message), 'bad');
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
