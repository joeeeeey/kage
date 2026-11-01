// The console's control entry (总控, Phase 2 C1): conversations with the control task. Text is set with textContent
// only; writes carry the page token and JSON (same-origin, kage.observe.dashboard post_control).
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
    return f.source || m.source;
  }

  function messageItem(m) {
    const mine = m.role === 'owner';
    const li = h('li', { class: 'ctl-msg ' + (mine ? 'is-owner' : 'is-control'), 'data-state': m.state });
    const head = h('div', { class: 'ctl-msg-head' },
      h('b', { text: mine ? '你' : m.runtime ? '系统' : '总控' }),
      h('time', { datetime: m.created_at, text: clock(m.created_at) }));
    if (mine) {
      head.append(h('span', { class: 'pill', 'data-tone': m.state === 'failed' ? 'bad' : m.state === 'answered' ? 'good' : 'warn',
        text: STATE_LABEL[m.state] || m.state }));
    } else if (m.needs_answer) {
      head.append(h('span', { class: 'pill', 'data-tone': 'warn', text: '等你回答' }));
    }
    li.append(head);
    const body = h('div', { class: 'ctl-msg-body' });
    body.textContent = m.expired ? '（原文已过保留期）' : (m.text || '');
    li.append(body);
    if (mine) {
      const facts = h('div', { class: 'ctl-msg-facts' + (m.facts && m.facts.source === 'console' && !m.facts.browser_like ? ' is-odd' : ''),
        text: '来源：' + factsLine(m) });
      li.append(facts);
      if (m.relays && m.relays.length) {
        li.append(h('ul', { class: 'ctl-relays' }, m.relays.map((r) => h('li', {},
          h('span', { text: r.mode === 'launch' ? '新开任务 ' : '已转给 ' }), h('code', { text: r.task_id }),
          h('small', { text: ' · ' + clock(r.at) })))));
      }
      if (m.proposals && m.proposals.length) {
        li.append(h('ul', { class: 'ctl-relays ctl-proposals' }, m.proposals.map((p) => h('li', { 'data-state': p.state },
          h('span', { text: (p.kind === 'launch' ? '新开任务「' + (p.title || '') + '」' + (p.linear ? '（建工单）' : '') : '转给 ')
            + (p.kind === 'launch' ? '' : '') }),
          p.kind === 'launch' ? '' : h('code', { text: p.task_id || '' }),
          h('small', { text: ' · ' + (PROPOSAL_LABEL[p.state] || p.state) + (p.error ? '：' + label(p.error) : '') })))));
      }
      if (m.state === 'failed') {
        li.append(h('div', { class: 'ctl-msg-error' },
          h('span', { text: '没能完成：' + label(m.error || 'failed') + ' ' }),
          h('button', { type: 'button', class: 'btn sm ghost', text: '重试', onclick: () => retry(m.id) })));
      } else if (m.state === 'received' && state.data && state.data.waiting) {
        li.append(h('div', { class: 'ctl-msg-error', text: '在等：' + label(state.data.waiting) }));
      }
      if (m.run_id) li.append(h('a', { class: 'ctl-run', href: '/runs/' + m.run_id, text: '看这次运行' }));
    }
    return li;
  }

  function renderConversations(d) {
    const list = $('ctl-convs');
    list.replaceChildren(...(d.conversations || []).map((c) => {
      const li = h('li', {}, h('button', {
        type: 'button', class: 'ctl-conv' + (c.conversation_id === state.conversation ? ' is-on' : ''),
        'aria-pressed': String(c.conversation_id === state.conversation),
        onclick: () => { state.conversation = c.conversation_id; refresh(); },
      }, h('span', { class: 'ctl-conv-title', text: c.title || '（无标题）' }),
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

  function render(d) {
    state.data = d;
    const banner = $('ctl-banner');
    if (!d.enabled) {
      banner.hidden = false;
      banner.replaceChildren(h('b', { text: '总控还没开启。' }),
        h('span', { text: ' 在左下角“总控设置”里打开；打开前，这里发的话不会被处理。' }));
      $('ctl-settings').open = true;
    } else if (d.config_error) {
      banner.hidden = false;
      banner.replaceChildren(h('b', { text: '配置读不出来：' }), h('code', { text: d.config_error }));
    } else {
      banner.hidden = true;
    }
    renderConversations(d);
    renderSettings(d);
    const list = $('ctl-messages');
    const msgs = d.messages || [];
    list.replaceChildren(...msgs.map(messageItem));
    if (!msgs.length) list.append(h('li', { class: 'empty', text: state.conversation ? '这个对话还没有消息' :
      '新对话：在下面写下你想问的事或要交代的事。' }));
    $('ctl-send').disabled = !d.enabled || state.sending;
    chip(d.enabled ? 'ok' : 'warn', d.enabled ? '总控已开启' : '总控未开启');
    const open = msgs.some((m) => m.role === 'owner' && OPEN.has(m.state));
    schedule(open ? 2000 : 15000);
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
