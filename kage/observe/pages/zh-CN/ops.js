/* Kage · 数字员工运行页 / 旧积压 / 设置 (P1b) / 收件 (P2; P48 收件轨迹: trace.js) / 规则 · 定时 (P3) / 缺口 (P3.1) / 健康 (P38) / 设计 (P39). Vanilla JS, same-origin only, no innerHTML for data. */
'use strict';

(() => {
  const main = document.getElementById('ops-main');
  const chip = document.getElementById('ops-chip');
  const chipLabel = document.getElementById('ops-chip-label');
  const syncLabel = document.getElementById('ops-sync');
  const toasts = document.getElementById('toasts');
  const TZ = '{{tz_name}}';

  const PROVIDER = { codex: 'Codex', claude: 'Claude' };
  const RUN_STATE = { running: '运行中', ended: '已结束' };
  const OUTCOME = {
    success: ['成功', 'good'], failed: ['失败', 'bad'], partial: ['部分处理', 'warn'],
    unresolved: ['未处理完', 'bad'], stopped: ['已停止', 'warn'],
  };
  const DISPATCH_STATE = {
    waiting: ['等待开始', 'warn'], running: ['运行中', 'live'], paused: ['已暂停', 'warn'], failed: ['失败 · 等你处理', 'bad'],
    needs_{{owner_key}}: ['等你回答', 'warn'], done: ['已完成', 'good'], superseded: ['已合并', ''],
  };
  const ERROR_LABEL = {
    transient: '临时故障（5xx / 超时 / 网络）', rate_limited: '限流（429）', quota: '额度用完', auth: '认证失败（401/403）',
    model_unsupported: '模型不可用（relay 404）', session_missing: '找不到要恢复的会话', session_live: '会话仍在别处运行',
    unknown: '未知错误', task_unresolved: '事件没有处理完', run_timeout: '运行超时', spawn_failed: '无法启动 CLI',
    stopped: '已被停止', crashed: '进程异常退出', provider_degraded: 'provider 过载，换健康的那个',
  };
  const TRIGGER = {
    slack_message: 'Slack 消息', decision_answer: '决策卡片回复', ask_answer: '询问卡片回复', agent_message: 'agent 消息',
    note: '任务留言', routine: '定时任务', launch: '启动任务', transient_retry: '自动重试', partial_retry: '补处理',
    fallback: '换 provider', {{owner_key}}_retry: '{{owner_name}} 点了重试', event: '事件',
  };
  const EVENT_STATUS = {
    handled: ['已处理', 'good'], dispatched: ['后台处理中', null], pending: ['待投递', 'warn'], queued: ['已排队（待会话确认）', 'warn'],
    claimed: ['已领取（待处理完成）', 'warn'], delivery_unknown: ['投递结果未知', 'bad'], sending: ['发送中', 'warn'],
  };
  const statusPill = (st) => { const [label, tone] = EVENT_STATUS[st] || [st, null]; return pill(label, tone); };
  const KIND_TONE = { '命令': 'cmd', '退出': 'exit', '改文件': 'edit', '工具': 'tool', '错误': 'error', '重试': 'retry',
    '结果': 'result', '说明': 'say', '开始': 'start', '结束': 'end', '待办': 'todo', '提示': 'note', '输出': 'raw' };

  // ---------- small DOM helpers ----------
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const pill = (text, tone, live) => h('span', { class: 'pill' + (live ? ' live' : ''), 'data-tone': tone || null }, text);
  const provChip = (p) => h('span', { class: 'prov-chip', 'data-prov': PROVIDER[p] ? p : 'other' }, PROVIDER[p] || p || '?');
  const ext = (href, text) => href ? h('a', { class: 'ext', href, target: '_blank', rel: 'noopener noreferrer' }, text || 'Slack ↗') : null;
  function clear(el) { while (el.firstChild) el.firstChild.remove(); return el; }
  // Replace an element's children; null / undefined / false are skipped (Element.append would print "null").
  function fill(el, ...kids) {
    clear(el);
    for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
    return el;
  }
  // no value: nothing, an empty string, or an element with no text (a list that came back empty)
  const nil = (v) => v === null || v === undefined || v === '' || (v instanceof Element && !v.textContent.trim()) ? '—' : v;
  // label / value rows: [label, value, class, tooltip]; a null row is skipped
  function kv(rows) {
    return h('dl', { class: 'kv' }, rows.filter(Boolean).map(([k, v, cls, tip]) => [h('dt', {}, k), h('dd', { class: cls || null, title: tip || null }, nil(v))]));
  }
  // a detail page's key facts at a glance: the same rows as a grid, each label over its value
  function factGrid(rows) {
    return h('dl', { class: 'facts' }, rows.filter(Boolean).map(([k, v, cls, tip]) => h('div', { class: 'fact' },
      h('dt', {}, k), h('dd', { class: cls || null, title: tip || null }, nil(v)))));
  }

  function toast(text, tone) {
    const t = h('div', { class: 'toast', 'data-tone': tone || null }, text);
    toasts.append(t);
    setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 400); }, tone === 'bad' ? 6000 : 3200);
  }

  // ---------- formatting ----------
  const fmtDT = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const fmtClockF = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const fmtHM = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, hour: '2-digit', minute: '2-digit', hour12: false });
  const fmtDayKey = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' });
  const fmtWeekday = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, weekday: 'short' });
  const when = (ts) => ts ? fmtDT.format(new Date(ts * 1000)) : '—';
  const clock = (ts) => ts ? fmtClockF.format(new Date(ts * 1000)) : '—';
  const hm = (ts) => ts ? fmtHM.format(new Date(ts * 1000)) : '—';
  // the owner's calendar day of a time: its key (YYYY-MM-DD), and a heading for a list grouped by day
  const dayKey = (ts) => fmtDayKey.format(new Date(ts * 1000));
  function dayLabel(ts) {
    const key = dayKey(ts), now = Date.now() / 1000;
    const date = `${key.slice(5)} ${fmtWeekday.format(new Date(ts * 1000))}`;
    if (key === dayKey(now)) return ['今天', date];
    if (key === dayKey(now - 86400)) return ['昨天', date];
    return [date, null];
  }
  function dur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    if (sec < 60) return `${sec} 秒`;
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return s ? `${m} 分 ${s} 秒` : `${m} 分`;
    const hh = Math.floor(m / 60);
    if (hh < 48) return `${hh} 小时 ${m % 60} 分`;
    return `${Math.floor(hh / 24)} 天 ${hh % 24} 小时`;
  }
  const short = (s, n = 8) => (s || '').slice(0, n);
  function outcomePill(run) {
    if (run.state === 'running') return pill('运行中', null, true);
    const [label, tone] = OUTCOME[run.outcome] || [run.outcome || '已结束', null];
    return pill(label, tone);
  }
  function stable(v) {
    if (Array.isArray(v)) return '[' + v.map(stable).join(',') + ']';
    if (v && typeof v === 'object') return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + stable(v[k])).join(',') + '}';
    return JSON.stringify(v);
  }
  const same = (a, b) => stable(a) === stable(b);
  const show = (v) => typeof v === 'string' ? v : JSON.stringify(v);
  const showDefault = (v) => Array.isArray(v) ? v.join(', ') || '（空）'
    : v && typeof v === 'object' ? Object.entries(v).map(([k, x]) => `${k}→${x}`).join(', ') : show(v);

  // ---------- fetch ----------
  async function api(path, opts) {
    const res = await fetch(path, { cache: 'no-store', credentials: 'same-origin', ...(opts || {}) });
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) {
      const err = new Error((body && body.error) || `HTTP ${res.status}`);
      err.status = res.status; err.body = body;
      throw err;
    }
    return body;
  }
  function online(ok, label) {
    chip.dataset.state = ok ? 'live' : 'error';
    chipLabel.textContent = label || (ok ? '本机已连接' : '读取失败');
    if (ok) syncLabel.textContent = clock(Date.now() / 1000);
  }

  let timer = null;
  function every(ms, fn) {
    if (timer) clearInterval(timer);
    const tick = () => { if (!document.hidden) fn(); };
    timer = setInterval(tick, ms);
    fn();
  }

  function pageHead(title, eyebrow, note, extra) {
    return h('section', { class: 'ops-head' },
      h('div', {}, h('p', { class: 'eyebrow' }, eyebrow), h('h2', {}, title), note ? h('p', { class: 'ops-note' }, note) : null),
      extra || null);
  }
  // a detail page's head: the record's id as the title and its state beside it (the page fills the slot once it loads)
  function detailHead(title, eyebrow, note) {
    const status = h('span', { class: 'head-status' });
    const head = h('section', { class: 'ops-head' },
      h('div', {}, h('p', { class: 'eyebrow' }, eyebrow), h('div', { class: 'title-row' }, h('h2', {}, title), status),
        note ? h('p', { class: 'ops-note' }, note) : null));
    return [head, status];
  }

  // =====================================================================
  // Runs list
  // =====================================================================
  function Runs() {
    const params = new URLSearchParams(location.search);
    const state = { filter: ['all', 'running', 'failed', 'today'].includes(params.get('filter')) ? params.get('filter') : 'all',
      task: params.get('task') || '' };
    const filterBox = h('div', { class: 'scope', role: 'group', 'aria-label': '筛选' });
    const taskSel = h('select', { 'aria-label': '按任务筛选', id: 'task-filter' }, h('option', { value: '' }, '全部任务'));
    const queue = h('section', { class: 'ops-card', id: 'dispatch-queue', hidden: true });
    const list = h('section', { class: 'ops-card', id: 'run-list' });
    const filters = [['all', '全部', 'ALL'], ['running', '运行中', 'RUNNING'], ['failed', '失败', 'FAILED'], ['today', '今天', 'TODAY']];
    const buttons = {};
    for (const [key, label, en] of filters) {
      const b = h('button', { type: 'button', class: 'scope-btn', 'aria-pressed': String(state.filter === key),
        onclick: () => { state.filter = key; sync(); load(); } },
        key === 'running' ? h('i', { class: 'scope-led', 'aria-hidden': 'true' }) : null, h('span', {}, label),
        h('small', {}, en), h('b', { class: 'count', 'data-count': key }, '0'));
      buttons[key] = b; filterBox.append(b);
    }
    taskSel.addEventListener('change', () => { state.task = taskSel.value; sync(); load(); });
    function sync() {
      for (const [k, b] of Object.entries(buttons)) b.setAttribute('aria-pressed', String(state.filter === k));
      const q = new URLSearchParams();
      if (state.filter !== 'all') q.set('filter', state.filter);
      if (state.task) q.set('task', state.task);
      history.replaceState(null, '', '/runs' + (q.toString() ? '?' + q : ''));
    }
    clear(main).append(
      pageHead('运行记录', 'RUNS · 后台会话', '数字员工每次在后台处理事件都是一次运行。日志已脱敏，只在本机读取；停止 / 恢复请用 Slack 卡片上的按钮。',
        h('div', { class: 'ops-filters' }, filterBox, h('label', { class: 'field compact' }, h('span', { class: 'sr' }, '任务'), taskSel))),
      queue, list);

    async function load() {
      const q = new URLSearchParams({ filter: state.filter });
      if (state.task) q.set('task', state.task);
      let data;
      try { data = await api('/api/runs?' + q); online(true); } catch (e) { online(false); return; }
      for (const [k, n] of Object.entries(data.counts || {})) {
        const el = filterBox.querySelector(`[data-count="${k}"]`);
        if (el) el.textContent = n;
      }
      const tasks = data.tasks || [];
      const current = taskSel.value || state.task;
      if (taskSel.options.length - 1 !== tasks.length || tasks.some((t, i) => taskSel.options[i + 1].value !== t)) {
        clear(taskSel).append(h('option', { value: '' }, '全部任务'), ...tasks.map((t) => h('option', { value: t }, t)));
        if (state.task && !tasks.includes(state.task)) taskSel.append(h('option', { value: state.task }, state.task));
      }
      taskSel.value = current;
      renderQueue(data.dispatches || []);
      renderRuns(data);
    }

    function renderQueue(items) {
      queue.hidden = !items.length;
      if (!items.length) return;
      clear(queue).append(h('h3', {}, '调度队列 ', h('span', { class: 'count' }, items.length)),
        h('p', { class: 'note' }, '还没结束的调度：等待开始、已暂停、失败待处理或等你回答。'),
        h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['任务', '状态', '待处理事件', '下一步', '原因', '卡片'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((d) => {
            const [label, tone] = DISPATCH_STATE[d.state] || [d.state, null];
            return h('tr', {},
              h('td', { class: 'mono' }, d.task),
              h('td', {}, pill(label, tone === 'live' ? null : tone, tone === 'live')),
              h('td', { class: 'num' }, d.held_events),
              h('td', {}, d.next_provider ? `${PROVIDER[d.next_provider] || d.next_provider} · ${d.next_mode === 'resume' ? '恢复会话' : '新会话'}` : '—'),
              h('td', {}, ERROR_LABEL[d.error_class] || d.error_class || '—'),
              h('td', { class: 'links' }, ext(d.card_url, '运行卡片 ↗'), ext(d.fail_card_url, '失败卡片 ↗')));
          }))));
    }

    function renderRuns(data) {
      const runs = data.runs || [];
      clear(list);
      if (data.notice === 'state_database_missing' || data.notice === 'dispatcher_tables_missing') {
        list.append(h('div', { class: 'empty' }, h('b', {}, '还没有调度数据'), '守护进程还没有创建运行记录。'));
        return;
      }
      if (!runs.length) {
        list.append(h('div', { class: 'empty' }, h('b', {}, '没有符合条件的运行'), state.filter === 'running' ? '现在没有正在后台运行的会话。' : '换个筛选试试。'));
        return;
      }
      const COLS = ['状态', '任务', '执行', '触发', '开始', '用时', '步骤', '事件', '最新一步', ''];
      const rows = [];
      const keyOf = (r) => r.started_at ? dayKey(r.started_at) : '';
      const perDay = {};
      for (const r of runs) perDay[keyOf(r)] = (perDay[keyOf(r)] || 0) + 1;
      let day = null;
      for (const r of runs) {
        // the list is grouped by the owner's day: a heading row where the day changes
        const key = keyOf(r);
        if (key !== day) {
          day = key;
          const [label, date] = r.started_at ? dayLabel(r.started_at) : ['时间未知', null];
          const n = perDay[key];
          rows.push(h('tr', { class: 'day-sep' }, h('th', { scope: 'rowgroup', colspan: COLS.length },
            h('span', {}, label), date ? h('small', {}, date) : null, h('small', { class: 'day-n' }, `${n} 次`))));
        }
        const href = '/runs/' + encodeURIComponent(r.run_id);
        const model = [r.model, r.effort].filter(Boolean).join(' · ');
        rows.push(h('tr', { class: 'run-row', 'data-state': r.state, tabindex: '0',
          onclick: (ev) => { if (!ev.target.closest('a')) location.href = href; },
          onkeydown: (ev) => { if (ev.key === 'Enter') location.href = href; } },
        h('td', { class: 'state' }, outcomePill(r), r.error_class && r.outcome !== 'success' ? h('div', { class: 'sub' }, ERROR_LABEL[r.error_class] || r.error_class) : null),
        h('td', { class: 'mono task' }, r.task),
        // one line: provider, the model (cut; model and effort in the tooltip), then new or resumed session
        h('td', { class: 'exec' }, h('div', { class: 'exec-line' }, provChip(r.provider),
          model ? h('span', { class: 'exec-model', title: model }, r.model || r.effort) : null,
          h('span', { class: 'tag', title: r.mode === 'resume' ? '恢复会话 ' + (r.session || '') : '新会话' }, r.mode === 'resume' ? '恢复' : '新会话'))),
        h('td', { class: 'nowrap meta trig' }, TRIGGER[r.trigger] || r.trigger || '—', r.attempt > 1 ? h('div', { class: 'sub' }, `第 ${r.attempt} 次尝试`) : null),
        h('td', { class: 'num meta', title: when(r.started_at) }, hm(r.started_at)),
        h('td', { class: 'num meta' }, dur(r.elapsed_seconds)),
        h('td', { class: 'num meta counted' }, r.steps || '—'),
        h('td', { class: 'num meta counted' }, `${r.handled}/${r.events}`),
        h('td', { class: 'summary' }, r.last_summary || (r.has_log ? '—' : '（没有日志）')),
        h('td', { class: 'links' }, h('a', { href }, '详情'), ext(r.card_url, '卡片 ↗'))));
      }
      list.append(h('table', { class: 'ops-table runs' },
        h('thead', {}, h('tr', {}, COLS.map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, rows)));
    }
    sync();
    every(3000, load);
  }

  // =====================================================================
  // Run detail + live log
  // =====================================================================
  function RunDetail(runId) {
    const [head, headStatus] = detailHead('run ' + runId, 'RUN · 运行详情', null);
    const summary = h('section', { class: 'ops-card summary', id: 'run-summary' });
    const facts = h('section', { class: 'ops-card', id: 'run-facts' });
    const events = h('section', { class: 'ops-card', id: 'run-events' });
    const logList = h('ol', { class: 'log', id: 'run-log', 'aria-label': '可读日志（已脱敏）', tabindex: '0' });
    const follow = h('input', { type: 'checkbox', id: 'log-follow', checked: true });
    const live = h('span', { id: 'log-live' });
    const count = h('span', { class: 'count', id: 'log-count' }, '0');
    const source = h('span', { class: 'note', id: 'log-source' });
    const copyBtn = h('button', { type: 'button', class: 'btn sm', onclick: () => copy(`{{tail_command}} ${runId}`) }, '复制 {{tail_command}} 命令');
    const logCard = h('section', { class: 'ops-card log-card' },
      h('div', { class: 'log-bar' }, h('h3', {}, '可读日志 ', count), live, source,
        h('label', { class: 'toggle' }, follow, h('span', {}, '跟随最新')), copyBtn),
      h('div', { class: 'log-wrap' }, logList));
    clear(main).append(
      h('nav', { class: 'crumbs' }, h('a', { href: '/runs' }, '← 运行记录')), head, summary,
      h('div', { class: 'detail-grid' }, h('div', { class: 'detail-side' }, facts, events), logCard));

    let cursor = null, done = false, detail = null, lines = 0;
    function addLines(arr) {
      const frag = document.createDocumentFragment();
      for (const line of arr) {
        const m = /^(\d\d:\d\d:\d\d) · ([^·]+?) · ([\s\S]*)$/.exec(line);
        const li = m ? h('li', { 'data-kind': KIND_TONE[m[2]] || 'raw' }, h('time', {}, m[1]), h('b', {}, m[2]), h('span', {}, m[3]))
          : h('li', { 'data-kind': 'raw' }, h('span', {}, line));
        frag.append(li);
      }
      logList.append(frag);
      lines += arr.length;
      while (logList.children.length > 5000) logList.firstChild.remove();
      count.textContent = lines;
      if (follow.checked && arr.length) logList.lastElementChild.scrollIntoView({ block: 'end' });
    }
    async function pollLog() {
      try {
        const data = await api(`/api/runs/${encodeURIComponent(runId)}/log` + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''));
        online(true);
        if (data.reset) { clear(logList); lines = 0; }
        addLines(data.lines || []);
        cursor = data.cursor;
        source.textContent = data.source === 'rendered' ? '（旧运行：从原始输出临时渲染）' : data.source === 'missing' ? '（没有日志目录）' : '';
        clear(live).append(data.complete ? pill('已结束', 'good') : pill('实时', null, true));
        if (data.complete) {
          if (done) return true;
          done = true;  // one more read after the end, then stop
        }
      } catch (e) {
        online(false);
        if (e.status === 404) { clear(logList).append(h('li', { 'data-kind': 'error' }, h('span', {}, '找不到这次运行。'))); return true; }
      }
      return false;
    }
    async function loadDetail() {
      try { detail = await api(`/api/runs/${encodeURIComponent(runId)}`); } catch (e) { return; }
      renderFacts(detail);
    }
    function renderFacts(d) {
      fill(headStatus, outcomePill(d), d.error_class && d.outcome !== 'success' ? pill(ERROR_LABEL[d.error_class] || d.error_class, 'bad') : null);
      const model = [d.model, d.effort].filter(Boolean).join(' · ');
      fill(summary, factGrid([
        ['任务', d.task, 'mono'],
        ['执行', h('span', { class: 'exec-line' }, provChip(d.provider), model ? h('span', { class: 'exec-model', title: model }, model) : null)],
        ['模式', d.mode === 'resume' ? '恢复会话' : '新会话'],
        ['触发', (TRIGGER[d.trigger] || d.trigger || '—') + (d.attempt > 1 ? ` · 第 ${d.attempt} 次尝试` : '')],
        ['开始', when(d.started_at)],
        ['用时', dur(d.elapsed_seconds)],
        ['步骤', d.steps ? String(d.steps) : '—'],
        ['事件', `已处理 ${d.handled} / 共 ${d.events}`],
      ]));
      fill(facts, h('h3', {}, '运行信息'), kv([
        ['会话', d.session, 'mono'],
        ['结束', d.ended_at ? when(d.ended_at) : (d.state === 'running' ? '仍在运行' : '—')],
        ['退出码', d.exit ? String(d.exit.exit_code) + (d.exit.stopped ? '（被停止）' : '') : '—'],
        ['工作目录', d.cwd, 'mono'],
        d.env_injected && d.env_injected.length ? ['注入变量名', d.env_injected.join(', '), 'mono'] : null,
        ['Slack', h('span', {}, ext(d.card_url, '运行卡片 ↗') || '—', ' ', ext(d.fail_card_url, '失败卡片 ↗'))],
        d.dispatch ? ['调度', h('span', {}, `${(DISPATCH_STATE[d.dispatch.state] || [d.dispatch.state])[0]} · `,
          ...d.dispatch.runs.map((r, i) => [i ? ' · ' : '', r === d.run_id ? h('b', {}, short(r.split('-').pop(), 6)) : h('a', { href: '/runs/' + r }, short(r.split('-').pop(), 6))]))] : null,
        d.progress ? ['进度行', d.progress.stage ? `${d.progress.stage}${d.progress.posted_at ? ' · ' + clock(d.progress.posted_at) : ''}` : '—'] : null,
      ]));
      fill(events, h('h3', {}, '事件 ', h('span', { class: 'count' }, (d.event_list || []).length)),
        (d.event_list || []).length ? h('ul', { class: 'ev-list' }, d.event_list.map((e) => h('li', {},
          h('code', {}, short(e.id, 10)), ' ', statusPill(e.status),
          e.kind ? h('span', { class: 'sub' }, ' ' + (TRIGGER[e.kind] || e.kind)) : null, ' ', ext(e.slack_url, '原消息 ↗')))) : h('p', { class: 'note' }, '这次运行没有事件（例如启动任务）。'),
        (d.actions || []).length ? h('details', { class: 'hist' }, h('summary', {}, `卡片按钮记录（${d.actions.length}）`),
          h('ul', { class: 'ev-list' }, d.actions.map((a) => h('li', {}, `${when(a.at)} · ${a.action} · ${a.result || ''}`)))) : null);
    }
    async function copy(text) {
      try { await navigator.clipboard.writeText(text); toast('已复制：' + text); } catch (e) { toast('复制失败，请手动输入：' + text, 'bad'); }
    }
    loadDetail();
    let ticks = 0;
    const loop = async () => {
      const finished = await pollLog();
      ticks += 1;
      if (ticks % 4 === 0 || finished) loadDetail();
      if (!finished) setTimeout(loop, done ? 300 : 1500);
    };
    loop();
  }

  // =====================================================================
  // Stale backlog (read-only)
  // =====================================================================
  function Backlog() {
    const head = pageHead('旧积压', 'BACKLOG · 只读', null);
    const body = h('section', { class: 'ops-card', id: 'backlog' });
    clear(main).append(head, body);
    async function load() {
      let data;
      try { data = await api('/api/backlog'); online(true); } catch (e) { online(false); return; }
      const hours = data.max_event_age_hours;
      clear(head.querySelector('div')).append(h('p', { class: 'eyebrow' }, 'BACKLOG · 只读'), h('h2', {}, '旧积压'),
        h('p', { class: 'ops-note' }, `调度器不会为这些事件唤醒会话：监听已经过期，或者事件早于 ${hours} 小时（dispatch.max_event_age_hours）。这里只读，方便你检查；需要跟进时请到 Slack 原消息里手动处理。`));
      clear(body).append(h('div', { class: 'counters' },
        h('div', { class: 'counter' }, h('b', {}, data.total || 0), h('span', {}, '总数')),
        h('div', { class: 'counter' }, h('b', {}, data.counts.expired_watch), h('span', {}, '监听已过期')),
        h('div', { class: 'counter' }, h('b', {}, data.counts.older_than_max_age), h('span', {}, `超过 ${hours} 小时`)),
        h('div', { class: 'counter' }, h('b', {}, (data.tasks || []).length), h('span', {}, '涉及任务'))));
      if (!data.total) { body.append(h('div', { class: 'empty' }, h('b', {}, '没有旧积压'), '所有未处理的事件都还在正常投递范围内。')); return; }
      if (data.truncated) body.append(h('p', { class: 'warn-box' }, `只显示最早的 ${data.items.length} 条。`));
      const byTask = new Map();
      for (const item of data.items) { if (!byTask.has(item.task)) byTask.set(item.task, []); byTask.get(item.task).push(item); }
      for (const t of data.tasks) {
        const items = byTask.get(t.task) || [];
        body.append(h('div', { class: 'bl-task' },
          h('h3', {}, h('span', { class: 'mono' }, t.task), ' ', h('span', { class: 'count' }, t.count),
            h('span', { class: 'sub' }, ` · 最早 ${dur(data.generated_at - t.oldest)} 前`)),
          h('table', { class: 'ops-table' },
            h('thead', {}, h('tr', {}, ['收到', '已过去', '状态', '原因', '监听', '链接'].map((x) => h('th', { scope: 'col' }, x)))),
            h('tbody', {}, items.map((it) => h('tr', {},
              h('td', { class: 'num' }, when(it.received)),
              h('td', { class: 'num' }, dur(it.age_seconds)),
              h('td', {}, statusPill(it.status)),
              h('td', {}, pill(it.reason === 'expired_watch' ? '监听已过期' : `超过 ${hours} 小时`, it.reason === 'expired_watch' ? null : 'bad')),
              h('td', {}, h('span', { class: 'mono' }, short(it.watch.id, 8)), ' ', provChip(it.watch.provider),
                h('div', { class: 'sub' }, [{ expired: '已过期', running: '运行中', inbox: '收件箱' }[it.watch.state] || it.watch.state, it.watch.channel, it.watch.mode, '会话 ' + it.watch.session].filter(Boolean).join(' · '))),
              h('td', { class: 'links' }, ext(it.slack_url, '原消息 ↗'), ext(it.watch.slack_url, '线程 ↗'))))))));
      }
    }
    every(15000, load);
  }

  // =====================================================================
  // Onboarding (O2e) — 实例模式，以及只观察模式记下而没去做的事；本人对分拣的纠正（O2f）
  // =====================================================================
  const MODE_LABEL = { installed: ['已安装', 'warn'], observe: ['只观察', 'warn'], active: ['干活中', 'good'], paused: ['已暂停', 'bad'] };
  const SHADOW_KIND = { triage: '分拣', slack_write: 'Slack 发送', dispatch: '派活' };
  const ROUTE_LABEL = { task: '会建任务', reply: '会回复', log: '只记录', ignore: '不管', unclear: '没看清' };
  const MODE_ACTOR = { services: '体检', owner_button: '本人（开工卡片）', owner: '本人（终端）', breaker: '安全熔断', init: 'kage init' };
  const HELD_WHY = { mode_observe: '只观察中，压下', mode_installed: '还没启动，压下', mode_paused: '已暂停，压下' };
  const CARD_STATE = { open: ['等你点', 'warn'], deciding: ['处理中', 'warn'], started: ['已开工', 'good'], later: ['再看一天', null],
    refused: ['不认这次点击', 'bad'], expired: ['已过期', null], stale: ['已过时', null] };
  // O2f：分拣本该是什么（kage.core.observe.ROUTES），由本人选
  const CORRECT_ROUTE = { task: ['建任务', '要动手干活：新开一个任务。'], reply: ['回复', '在对话里简短回一句就够。'],
    log: ['只记录', '值得记一笔，不用做什么。'], ignore: ['不管', '跟你无关。'] };
  const CORRECT_ERROR = { shadow_record_not_found: '这条记录已经不在了，请刷新页面。', only_triage_records_are_corrected: '只有分拣可以纠正。',
    only_the_owner_corrects_shadow_records: '纠正只能由你本人来做，不能来自 agent 会话。', note_must_be_at_most_300_characters: '备注超过 300 字。',
    text_looks_like_it_contains_a_credential_use_a_one_time_share_link: '备注里像是有凭据，请删掉。',
    invalid_csrf_token: '页面已过期，请刷新后再试。', local_state_unavailable: '本地数据库正忙，请稍后再试。' };
  const MODE_NOTE = {
    installed: '已安装：体检通过后启动服务，实例会先只观察，再干活。',
    observe: '只观察：不会代你发任何消息、也不会派活。下面是它本来会做的事。用私信里每天那张卡片上的按钮让它开始干活。',
    active: '干活中：会替你回复和派活。只观察期间记下的事仍列在下面。',
    paused: '已暂停：恢复前不会开始新的运行（在终端里运行 `mode resume`）。',
  };
  // O3b：Kage 从本人的自我介绍里读懂的东西（kage.onboard.extract），逐条确认
  const INTRO_TARGET = { owner: '关于你', org: '你的组织', people: '常合作的人', rule: '规则',
    playbook: '你处理事情的习惯', glossary: '术语和名字' };
  const INTRO_FIELD = { name: '名字', short_name: '简称', role: '角色', company: '公司', timezone: '时区',
    language: '语言', voice: '说话身份', tracker: '工单系统', channels: '频道', systems: '系统', team: '团队',
    relationship: '关系', communication: '怎么跟他说话', hint: '该怎么做', applies_to: '适用于',
    term: '术语', meaning: '意思', asr: '常被听成', kind: '类型', title: '标题', statement: '内容' };
  const INTRO_STATE = { candidate: ['待确认', 'warn'], accepted: ['已接受', 'good'], rejected: ['不要', null],
    edited: ['已改', 'good'], applied: ['已生效', 'good'] };
  const INTRO_RUN = { done: ['已读完', 'good'], over_cap: ['超出上限', 'warn'], failed: ['失败', 'bad'] };
  const RULE_KIND = { fact: '事实', limit: '限制', preference: '偏好', principle: '原则', gotcha: '易错点' };
  const INTRO_EDIT = { owner: ['name', 'short_name', 'role', 'company', 'timezone', 'language', 'voice'],
    org: ['company', 'tracker', 'channels', 'systems', 'team'], people: ['name', 'role', 'relationship', 'communication'],
    rule: ['title', 'statement', 'hint'], playbook: ['hint', 'applies_to'], glossary: ['term', 'meaning', 'asr'] };
  const INTRO_LISTS = ['channels', 'systems', 'asr'];
  const INTRO_VERIFY = { verified: ['已核实', 'good'], contradicted: ['和 Slack 不一致', 'bad'], ambiguous: ['有重名', 'warn'],
    not_found: ['Slack 里没找到', null], unverifiable: null, error: ['核实失败', null] };
  const INTRO_ERROR = { candidate_not_found: '这一条已经不在了，请刷新页面。',
    only_the_owner_decides_candidates: '确认只能由你本人来做，不能来自 agent 会话。',
    text_looks_like_it_contains_a_credential_use_a_one_time_share_link: '这里像是有凭据，请删掉。',
    invalid_csrf_token: '页面已过期，请刷新后再试。', local_state_unavailable: '本地数据库正忙，请稍后再试。' };
  function Onboarding() {
    const head = pageHead('上手', 'ONBOARDING · 观察与纠正', null);
    const body = h('section', { class: 'ops-card', id: 'onboarding' });
    clear(main).append(head, body);
    let csrf = '';
    const sheet = document.getElementById('ops-confirm');
    const sheetBody = document.getElementById('ops-confirm-body');
    const sheetOk = document.getElementById('ops-confirm-ok');
    document.getElementById('ops-confirm-cancel').onclick = () => sheet.close();
    function correctTriage(r) {
      const c = r.correction || {};
      const choices = h('div', { class: 'oc-routes', role: 'radiogroup', 'aria-label': '它本该怎么做' },
        Object.entries(CORRECT_ROUTE).map(([route, [label, hint]]) => h('label', { class: 'oc-route' },
          h('input', { type: 'radio', name: 'oc-route', value: route, checked: (c.route || '') === route }),
          h('span', { class: 'oc-route-text' }, h('span', { class: 'oc-route-title' }, h('b', {}, label), route === r.route ? h('span', { class: 'oc-same' }, '它的判断') : null),
            h('span', { class: 'sub' }, hint)))));
      const note = h('textarea', { id: 'oc-note', rows: 3, maxlength: 300, placeholder: '选填：为什么（谁发的、你本来会怎么处理）' });
      note.value = c.note || '';
      const count = h('span', { class: 'oc-count' }, `${note.value.length} / 300`);
      note.addEventListener('input', () => { count.textContent = `${note.value.length} / 300`; });
      const err = h('p', { class: 'err-box', role: 'alert', hidden: true });
      const pick = () => { const el = choices.querySelector('input:checked'); return el ? el.value : ''; };
      choices.addEventListener('change', () => { sheetOk.disabled = !pick(); });
      sheet.querySelector('.sh-title .eyebrow').textContent = 'CORRECT · 纠正分拣';
      document.getElementById('ops-confirm-title').textContent = '它本该怎么做？';
      clear(sheetBody).append(
        h('div', { class: 'oc-was' }, h('span', { class: 'sub' }, `${when(r.at)} · ${r.target || '—'}`),
          h('div', {}, '它', h('b', {}, ROUTE_LABEL[r.route] || r.route || '—'), r.text ? h('span', { class: 'sub' }, ' — ' + r.text) : null)),
        choices,
        h('div', { class: 'oc-note' }, h('label', { for: 'oc-note' }, '备注', h('span', { class: 'sub' }, '（选填）')), note, count),
        h('p', { class: 'note' }, '记在这条记录旁边，作为上手的依据；只观察期间不会改变任何行为。'),
        err);
      sheetOk.textContent = r.correction ? '保存新的纠正' : '保存纠正';
      sheetOk.disabled = !pick();
      sheetOk.onclick = async () => {
        err.hidden = true;
        sheetOk.disabled = true;
        try {
          await api('/api/onboarding/correct', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: JSON.stringify({ id: r.id, route: pick(), note: note.value.trim() || null }) });
        } catch (e) {
          const code = e.body && e.body.error;
          err.textContent = CORRECT_ERROR[code] || `没保存（${code || e.message}）。`;
          err.hidden = false;
          sheetOk.disabled = false;
          return;
        }
        sheet.close();
        toast('纠正已保存', 'good');
        load();
      };
      sheet.showModal();
      (choices.querySelector('input:checked') || choices.querySelector('input')).focus();
    }
    function yourCall(r) {
      if (r.kind !== 'triage' || !r.id) return h('span', { class: 'sub' }, '—');
      const c = r.correction;
      if (!c) return h('button', { type: 'button', class: 'btn sm', onclick: () => correctTriage(r) }, '纠正…');
      const agrees = c.route === r.route;
      return h('div', { class: 'oc-call' },
        h('div', {}, pill(agrees ? '同意' : '应该：' + (CORRECT_ROUTE[c.route] || [c.route])[0], agrees ? 'good' : 'warn'),
          h('button', { type: 'button', class: 'btn sm ghost', 'aria-label': '修改这条纠正', onclick: () => correctTriage(r) }, '修改')),
        c.note ? h('div', { class: 'sub' }, c.note) : null);
    }
    function flatValue(target, value) {
      const v = value || {};
      if (target === 'rule') return [['kind', RULE_KIND[v.kind] || v.kind], ['title', v.title], ['statement', v.statement],
        ['hint', (v.enforcement || {}).hint]].filter(([, x]) => x);
      return Object.entries(v).map(([k, x]) => [k, Array.isArray(x) ? x.join(', ') : String(x)]);
    }
    async function decideItem(i, decision, value) {
      try {
        await api('/api/onboarding/decide', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify(value ? { id: i.id, decision, value } : { id: i.id, decision }) });
      } catch (e) {
        const code = e.body && e.body.error;
        toast(INTRO_ERROR[code] || `没保存 (${code || e.message})`, 'bad');
        return false;
      }
      toast('已保存', 'good');
      load();
      return true;
    }
    function editItem(i) {
      const v = (i.decision && i.decision.value) || i.value || {};
      const inputs = {};
      const fields = INTRO_EDIT[i.target] || [];
      const kind = i.target === 'rule' ? h('select', { id: 'ob-kind' }, Object.entries(RULE_KIND).map(([k, label]) =>
        h('option', { value: k, selected: v.kind === k }, label))) : null;
      const rows = fields.map((f) => {
        const raw = f === 'hint' && i.target === 'rule' ? (v.enforcement || {}).hint : v[f];
        const long = ['statement', 'hint', 'communication', 'meaning'].includes(f);
        const el = h(long ? 'textarea' : 'input', long ? { rows: 2, maxlength: 600 } : { type: 'text', maxlength: 600 });
        el.value = Array.isArray(raw) ? raw.join(', ') : (raw || '');
        inputs[f] = el;
        return h('label', { class: 'ob-field' }, h('span', {}, INTRO_FIELD[f] || f,
          INTRO_LISTS.includes(f) ? h('span', { class: 'sub' }, ' (用逗号分隔)') : null), el);
      });
      const err = h('p', { class: 'err-box', role: 'alert', hidden: true });
      sheet.querySelector('.sh-title .eyebrow').textContent = 'EDIT · 改候选';
      document.getElementById('ops-confirm-title').textContent = '改正我理解的';
      clear(sheetBody).append(
        h('div', { class: 'oc-was' }, h('span', { class: 'sub' }, '你的原话'), h('div', {}, h('mark', {}, i.quote))),
        kind ? h('label', { class: 'ob-field' }, h('span', {}, '类型'), kind) : null,
        h('div', { class: 'ob-fields' }, rows),
        h('p', { class: 'note' }, '和这一条记在一起；生效之前不会被用上。'), err);
      sheetOk.textContent = '保存修改';
      sheetOk.disabled = false;
      sheetOk.onclick = async () => {
        const value = {};
        for (const [f, el] of Object.entries(inputs)) {
          const text = el.value.trim();
          if (!text) continue;
          value[f] = INTRO_LISTS.includes(f) ? text.split(',').map((x) => x.trim()).filter(Boolean) : text;
        }
        if (kind) value.kind = kind.value;
        err.hidden = true;
        sheetOk.disabled = true;
        try {
          await api('/api/onboarding/decide', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: JSON.stringify({ id: i.id, decision: 'edit', value }) });
        } catch (e) {
          const code = e.body && e.body.error;
          err.textContent = INTRO_ERROR[code] || `没保存 (${code || e.message})`;
          err.hidden = false;
          sheetOk.disabled = false;
          return;
        }
        sheet.close();
        toast('已保存', 'good');
        load();
      };
      sheet.showModal();
      (kind || Object.values(inputs)[0]).focus();
    }
    function verifyPill(i) {
      const shown = i.verify && INTRO_VERIFY[i.verify.result];
      if (!shown) return null;
      const el = pill(shown[0], shown[1]);
      if (i.verify.detail) el.title = i.verify.detail;
      return el;
    }
    async function acceptVerified(button) {
      button.disabled = true;
      let res;
      try {
        res = await api('/api/onboarding/accept-verified', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: '{}' });
      } catch (e) {
        const code = e.body && e.body.error;
        toast(INTRO_ERROR[code] || `${code || e.message}`, 'bad');
        button.disabled = false;
        return;
      }
      toast(`已接受 ${res.accepted}`, 'good');
      load();
    }
    function itemCard(i) {
      const [label, tone] = INTRO_STATE[i.status] || [i.status, null];
      return h('article', { class: 'ob-item', 'data-state': i.status },
        h('div', { class: 'ob-item-head' }, pill(label, tone), verifyPill(i), i.support === 'inferred' ? pill('推断', 'warn') : null,
          h('span', { class: 'sub' }, `置信度 ${Math.round((i.confidence || 0) * 100)}%`)),
        h('dl', { class: 'ob-value' }, flatValue(i.target, (i.decision && i.decision.value) || i.value).map(([k, x]) =>
          [h('dt', {}, INTRO_FIELD[k] || k), h('dd', {}, x)])),
        h('blockquote', { class: 'ob-quote' }, h('mark', {}, i.quote)),
        h('div', { class: 'ob-actions' },
          h('button', { type: 'button', class: 'btn sm primary', disabled: i.status === 'accepted', onclick: () => decideItem(i, 'accept') }, '接受'),
          h('button', { type: 'button', class: 'btn sm', onclick: () => editItem(i) }, '改…'),
          h('button', { type: 'button', class: 'btn sm ghost', disabled: i.status === 'rejected', onclick: () => decideItem(i, 'reject') }, '不要')));
    }
    function introSection(intro) {
      const sec = h('div', { class: 'ob-intro', id: 'intro' }, h('h3', {}, '我理解的你'));
      const run = intro.run;
      if (!run) {
        sec.append(h('div', { class: 'empty' }, h('b', {}, '还没有自我介绍'), '在终端里运行 onboard intro add FILE，再运行 onboard extract（会先给费用估算）。这里的内容在你接受之前都不生效。'));
        return sec;
      }
      const [label, tone] = INTRO_RUN[run.status] || [run.status, null];
      const dropped = Object.values(run.dropped || {}).reduce((a, b) => a + b, 0);
      sec.append(h('p', { class: 'ops-note ob-run' }, pill(label, tone), ` ${when(run.at)} · ${run.items} 条候选 · `,
        run.cost_usd != null ? `$${run.cost_usd.toFixed(2)} / $${(run.cap_usd || 0).toFixed(2)}` : '没有报告费用',
        dropped ? ` · ${dropped} 条被校验丢弃` : null, run.error ? ` · ${run.error}` : null));
      const open = intro.items.filter((i) => i.status === 'candidate').length;
      if (intro.items.length) sec.append(h('p', { class: 'ops-note' }, `${open} 条待确认。接受只记下你的选择；在终端里运行 onboard apply 之后才生效。`));
      const ready = intro.items.filter((i) => i.status === 'candidate' && i.verify && i.verify.result === 'verified').length;
      if (ready) sec.append(h('div', { class: 'ob-bulk' }, h('button', { type: 'button', class: 'btn sm primary', onclick: (e) => acceptVerified(e.currentTarget) },
        `接受全部已核实 (${ready})`)));
      else if (intro.items.length && !intro.items.some((i) => i.verify)) sec.append(h('p', { class: 'note' }, '在终端里运行 onboard verify 会拿 Slack 核实这些事实；只有已核实的可以一起接受。'));
      for (const [target, name] of Object.entries(INTRO_TARGET)) {
        const items = intro.items.filter((i) => i.target === target);
        if (!items.length) continue;
        sec.append(h('h4', { class: 'ob-group' }, name, ' ', h('span', { class: 'count' }, items.length)),
          h('div', { class: 'ob-items' }, items.map(itemCard)));
      }
      return sec;
    }
    async function load() {
      let data;
      try { data = await api('/api/onboarding'); online(true); } catch (e) { online(false); return; }
      csrf = data.csrf_token || '';
      const [label, tone] = MODE_LABEL[data.mode] || [data.mode, null];
      clear(head.querySelector('div')).append(h('p', { class: 'eyebrow' }, 'ONBOARDING · 观察与纠正'), h('h2', {}, '上手'),
        h('p', { class: 'ops-note' }, MODE_NOTE[data.mode] || ''));
      const counts = data.counts || {};
      const total = Object.values(counts).reduce((a, b) => a + b, 0);
      const corrected = data.recent.filter((r) => r.correction).length;
      clear(body).append(h('div', { class: 'counters' },
        h('div', { class: 'counter' }, h('b', {}, pill(label, tone)), h('span', {}, '模式')),
        h('div', { class: 'counter' }, h('b', {}, total), h('span', {}, '只记下、没去做')),
        h('div', { class: 'counter' }, h('b', {}, counts.triage || 0), h('span', {}, '分拣过的消息')),
        h('div', { class: 'counter' }, h('b', {}, corrected), h('span', {}, '你纠正过的分拣')),
        h('div', { class: 'counter' }, h('b', {}, counts.dispatch || 0), h('span', {}, '压下的派活')),
        h('div', { class: 'counter' }, h('b', {}, counts.slack_write || 0), h('span', {}, '压下的 Slack 发送'))));
      if (!data.row) body.append(h('p', { class: 'ops-note' }, '这个实例早于上手流程：它一直是干活状态。'));
      body.append(introSection(data.intro || { run: null, items: [] }));
      body.append(h('h3', {}, '本来会做的事'));
      if (!data.recent.length) body.append(h('div', { class: 'empty' }, h('b', {}, '还没有记录'), '只观察模式还没有压下任何事。'));
      else {
        if (counts.triage) body.append(h('p', { class: 'ops-note' }, '不认同哪条分拣，就纠正它。纠正会作为依据留下，只观察期间不改变任何行为。'));
        body.append(h('div', { class: 'oc-scroll' }, h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['时间', '类型', '在哪', '本来会', '理由', '你的判断'].map((x) => h('th', { scope: 'col' }, x)))),
          h('tbody', {}, data.recent.map((r) => h('tr', {},
            h('td', { class: 'num' }, when(r.at)),
            h('td', {}, pill(SHADOW_KIND[r.kind] || r.kind, null)),
            h('td', {}, h('span', { class: 'mono' }, r.target || '—')),
            h('td', {}, r.kind === 'triage' ? (ROUTE_LABEL[r.route] || r.route || '—') : (r.endpoint ? r.endpoint + '：' : '') + (r.text || '')),
            h('td', { class: 'sub' }, r.kind === 'triage' ? (r.text || '') : (HELD_WHY[r.why] || r.why)),
            h('td', { class: 'oc-cell' }, yourCall(r))))))));
      }
      body.append(h('h3', {}, '模式变化'));
      if (!data.log.length) body.append(h('p', { class: 'ops-note' }, '还没有变化。'));
      else body.append(h('table', { class: 'ops-table' },
        h('thead', {}, h('tr', {}, ['时间', '从', '到', '谁', '原因'].map((x) => h('th', { scope: 'col' }, x)))),
        h('tbody', {}, data.log.map((r) => h('tr', {},
          h('td', { class: 'num' }, when(r.at)), h('td', {}, r.old ? (MODE_LABEL[r.old] || [r.old])[0] : '—'),
          h('td', {}, (MODE_LABEL[r.new] || [r.new])[0]), h('td', {}, MODE_ACTOR[r.actor] || r.actor), h('td', { class: 'sub' }, r.reason || ''))))));
      if (data.cards.length) {
        body.append(h('h3', {}, '开始卡片'));
        body.append(h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['发出', '状态', '结束'].map((x) => h('th', { scope: 'col' }, x)))),
          h('tbody', {}, data.cards.map((c) => h('tr', {},
            h('td', { class: 'num' }, when(c.posted_at)), h('td', {}, pill(...(CARD_STATE[c.state] || [c.state, null]))), h('td', { class: 'num' }, when(c.settled_at)))))));
      }
    }
    every(15000, load);
  }

  // =====================================================================
  // Intake (收件, P2) — read-only, metadata only
  // =====================================================================
  const CONV = { im: '私信', mpim: '群私信', channel: '频道' };
  const ROUTE = { inbox: '收件处', oncall: 'oncall', bound: '绑定任务', ref: '编号 / PR' };
  const ROUTE_REASON = { default_inbox: '默认去收件处', oncall_alert: 'oncall 告警', intake_binding: '会话已绑定', {{ticket_ref_reason}}: '{{ticket_team}} 编号',
    pr_link: 'PR 链接' };
  const BURST_STATE = { open: ['合并中', null, true], sealed: ['待路由', 'warn'], routed: ['已投递', 'good'],
    waiting: ['等任务会话', 'warn'], covered: ['已并入任务', ''], expired: ['已过期', 'bad'] };
  const FILTER_REASON = { burst: '进入连发', alert_duplicate: '告警去重', {{owner_key}}_authored: '{{owner_name}} 本人', our_agent: '我们的 agent',
    system_noise: '系统噪音', bot_outside_oncall: '非 oncall 的 bot', covered_by_watch: '已有监听', pending: '处理中' };
  const DISPOSITION = { forward: '转给任务', launch: '新建任务', answer: '直接回复', ask_requester: '问请求人', brief: '简报给 {{owner_name}}',
    card_{{owner_key}}: '卡片给 {{owner_name}}', log: '只记录', ignore: '忽略', investigate: '排查', escalate: '升级事故' };
  const INTENT = { request: '请求', question: '问题', directive: '方向性', follow_up: '跟进', fyi: '通知', chit_chat: '闲聊',
    alert: '告警', other: '其他' };
  const CLARITY = { clear: '清楚', partial: '部分清楚', vague: '模糊' };
  const INTAKE_STATUS = { ok: ['正常', 'good'], backoff: ['限流退避中', 'warn'], error: ['出错，退避中', 'bad'],
    auth_error: ['认证失败', 'bad'] };
  const tsWhen = (ts) => ts ? when(Number(ts)) : '—';

  function Intake() {
    const head = pageHead('收件', 'INTAKE · 收件处',
      '守护进程从私信、@{{owner_name}} 和订阅频道收消息，合并成连发后按规则投给任务（不调模型）。下面每条消息都能点开看它在系统里的完整轨迹。只显示元数据，消息正文从不落盘。');
    const status = h('section', { class: 'ops-card', id: 'intake-status' });
    // P48: the raw tables stay, folded under the trace list (built once, so an opened <details> survives the refresh)
    const raw = (id, title) => {
      const body = h('div', { class: 'raw-body' });
      const count = h('span', { class: 'count' }, '0');
      const sec = h('section', { class: 'ops-card', id }, h('details', { class: 'hist' }, h('summary', {}, title, ' ', count), body));
      return { sec, body, count };
    };
    const burstsBox = raw('intake-bursts', '原始记录：最近的连发');
    const triageBox = raw('intake-triage', '原始记录：分诊');
    const bursts = burstsBox.body, triage = triageBox.body;
    const bindingsBox = raw('intake-bindings', '会话绑定');
    const bindings = bindingsBox.body;
    const trace = window.{{trace_global}} ? window.{{trace_global}}.mount({ h, clear, fill, api, online, toast }).section : null;
    clear(main).append(head, status, trace, burstsBox.sec, triageBox.sec, bindingsBox.sec);

    function routeCell(b) {
      const reason = b.route_reason && b.route_reason.startsWith('watch:') ? '任务监听' : (ROUTE_REASON[b.route_reason] || b.route_reason);
      return h('td', {}, b.route ? (ROUTE[b.route] || b.route) : '—',
        b.target_task ? h('div', { class: 'sub mono' }, b.target_task) : null,
        reason ? h('div', { class: 'sub' }, reason) : null,
        b.handled_task && b.handled_task !== b.target_task ? h('div', { class: 'sub mono' }, '→ ' + b.handled_task) : null);
    }
    function renderStatus(d) {
      const st = d.state || {};
      const cfg = d.config || {};
      const last = st.last_cycle || {};
      const [label, tone] = !cfg.enabled ? ['已关闭', 'warn'] : (INTAKE_STATUS[st.status] || [st.status ? st.status : '还没开始', null]);
      const warns = [];
      if (d.config_error) warns.push(`配置无效：${d.config_error}`);
      for (const c of st.unresolved_channels || []) warns.push(`频道 ${c} 无法识别，请改成频道 ID（C…）`);
      for (const [ep, until] of Object.entries(st.backoff || {})) if (until * 1000 > Date.now()) warns.push(`${ep} 限流退避到 ${clock(until)}`);
      if (st.last_error) warns.push(`最近错误：${st.last_error}${st.last_error_at ? ' · ' + when(st.last_error_at) : ''}`);
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, pill(label, tone)), h('span', {}, '状态')),
          h('div', { class: 'counter' }, h('b', {}, last.at ? clock(last.at) : '—'), h('span', {}, '最近一轮')),
          h('div', { class: 'counter' }, h('b', {}, last.at ? `${last.api_calls}/${cfg.max_api_calls_per_cycle || '?'}` : '—'),
            h('span', {}, '本轮 API 调用 / 上限')),
          h('div', { class: 'counter' }, h('b', {}, st.api_calls_last_hour || 0), h('span', {}, '近 1 小时调用')),
          h('div', { class: 'counter' }, h('b', {}, st.next_cycle_at ? clock(st.next_cycle_at) : '—'), h('span', {}, '下一轮')),
          h('div', { class: 'counter' }, h('b', {}, (d.counts || {}).waiting || 0), h('span', {}, '等任务会话'))),
        h('dl', { class: 'kv' },
          h('dt', {}, '频道'), h('dd', {}, (d.channels || []).map((c) => h('span', { class: 'pill', 'data-tone': c.id ? null : 'bad' },
            `${c.entry}${c.id && c.id !== c.entry ? ' · ' + c.id : ''}${c.oncall ? ' · oncall' : ''}`))),
          h('dt', {}, '收件任务'), h('dd', {}, ['inbox', 'oncall'].map((r) => {
            const t = (d.tasks || {})[r];
            return t ? h('div', { class: 'mono' }, `${t.task_id} · ${t.current_provider ? (PROVIDER[t.current_provider] || t.current_provider) + ' ' + t.current_session : (t.registered ? '没有当前会话' : '未初始化（intake-bootstrap）')}`) : null;
          })),
          h('dt', {}, '节奏'), h('dd', {}, `每 ${cfg.poll_seconds || '?'} 秒一轮 · 连发窗口 ${cfg.burst_seconds || '?'} 秒 · 告警去重 ${cfg.alert_dedupe_minutes || '?'} 分钟`),
          h('dt', {}, '24 小时过滤'), h('dd', {}, Object.entries(d.filtered_24h || {}).map(([k, n]) => h('span', { class: 'pill' }, `${FILTER_REASON[k] || k} ${n}`)))),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderBursts(d) {
      const items = d.bursts || [];
      burstsBox.count.textContent = items.length;
      fill(bursts,
        items.length ? h('table', { class: 'ops-table', id: 'intake-burst-table' },
          h('thead', {}, h('tr', {}, ['时间', '会话', '发送人', '条数', '去向', '状态', '分诊', '告警'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((b) => {
            const [label, tone, live] = BURST_STATE[b.state] || [b.state, null];
            return h('tr', { 'data-burst': b.burst_id },
              h('td', { class: 'num' }, tsWhen(b.first_ts), b.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', {}, CONV[b.conv_type] || b.conv_type, ' ', h('span', { class: 'mono' }, b.channel), b.thread_root ? h('div', { class: 'sub' }, 'thread') : null,
                ' ', ext(b.slack_url, '原消息 ↗')),
              h('td', { class: 'mono' }, b.sender || '—', b.person_style === 'strategic_directive' ? h('div', {}, pill('方向性', 'warn')) : null),
              h('td', { class: 'num' }, b.msg_count),
              routeCell(b),
              h('td', {}, pill(label, tone === '' ? null : tone, live), b.event_status ? h('div', { class: 'sub' }, (EVENT_STATUS[b.event_status] || [b.event_status])[0]) : null,
                b.error ? h('div', { class: 'sub' }, b.error) : null),
              h('td', {}, (b.triage || []).map((x) => pill(DISPOSITION[x] || x))),
              h('td', {}, b.alert_fp ? [h('span', { class: 'mono' }, b.alert_service || '?'), h('div', { class: 'sub mono' }, b.alert_fp.slice(0, 8)),
                b.dup_count ? h('div', { class: 'sub' }, `重复 ${b.dup_count} 次`) : null, b.alert_state ? h('div', { class: 'sub' }, b.alert_state) : null] : '—'));
          }))) : h('div', { class: 'empty' }, h('b', {}, '还没有连发'), d.notice === 'intake_tables_missing' ? '守护进程还没有创建收件表。' : '收到消息后会出现在这里。'));
    }
    function renderTriage(d) {
      const items = d.triage || [];
      triageBox.count.textContent = items.length;
      fill(triage,
        h('p', { class: 'note' }, '每次分诊一条；要纠正请运行 slack_followup.py triage-correct --record ID --disposition … --note …'),
        items.length ? h('table', { class: 'ops-table', id: 'intake-triage-table' },
          h('thead', {}, h('tr', {}, ['时间', 'burst', '意图', '清晰度', '去向', '目标任务', '置信度', '依据', '纠正'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((r) => h('tr', { 'data-record': r.id },
            h('td', { class: 'num' }, when(r.created_at), r.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
            h('td', { class: 'mono' }, short(r.burst_id, 16)),
            h('td', {}, INTENT[r.intent] || r.intent),
            h('td', {}, CLARITY[r.clarity] || r.clarity),
            h('td', {}, pill(DISPOSITION[r.disposition] || r.disposition, r.needs_{{owner_key}} ? 'warn' : null)),
            h('td', { class: 'mono' }, r.target_task || '—'),
            h('td', { class: 'num' }, r.confidence === null || r.confidence === undefined ? '—' : Number(r.confidence).toFixed(2)),
            h('td', {}, r.basis || '—', r.note ? h('div', { class: 'sub' }, r.note) : null),
            h('td', {}, r.correction ? `${DISPOSITION[r.correction.disposition] || r.correction.disposition || ''} ${r.correction.note || ''}` : '—'))))) :
          h('div', { class: 'empty' }, h('b', {}, '还没有分诊记录'), '收件处处理事件时会写在这里。'));
    }
    function renderBindings(d) {
      const items = d.bindings || [];
      bindingsBox.count.textContent = items.length;
      fill(bindings,
        items.length ? h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['会话', '任务', '监听', '到期', ''].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => h('tr', {},
            h('td', { class: 'mono' }, x.channel, x.thread_root ? h('div', { class: 'sub' }, 'thread ' + x.thread_root) : h('div', { class: 'sub' }, '整个会话')),
            h('td', { class: 'mono' }, x.task_id),
            h('td', { class: 'mono' }, x.watch_id ? short(x.watch_id, 8) : (x.error || '—')),
            h('td', {}, x.expires_at ? when(x.expires_at) : '长期', x.active ? null : h('div', {}, pill('已失效'))),
            h('td', {}, ext(x.slack_url, 'Slack ↗')))))) : h('p', { class: 'note' }, '还没有绑定。'));
    }
    async function load() {
      let data;
      try { data = await api('/api/intake'); online(true); } catch (e) { online(false); return; }
      renderStatus(data);
      renderBursts(data);
      renderTriage(data);
      renderBindings(data);
    }
    every(5000, load);
  }

  // =====================================================================
  // Rules (规则, P3) — read-only: outcomes, approvals, overrides, proposals, backtest. Ids only, never plans.
  // =====================================================================
  const RULE_OUTCOME = { auto: ['自动执行', 'good'], one_tap: ['一键批准', 'warn'], clarify: ['先补信息', null], route: ['转交', null],
    {{owner_key}}: ['交 {{owner_name}}', 'bad'] };
  const APPROVAL = { not_required: ['不需要', null], clarify_first: ['先补信息', null], not_requested: ['未发卡片', 'warn'],
    pending: ['等批准', 'warn'], approved: ['已批准', 'good'], approved_with_condition: ['附条件（需重提）', 'warn'],
    rejected: ['已拒绝', 'bad'], self_approval_refused: ['本人不能批准', 'bad'], approver_not_in_rules: ['批准人不在规则里', 'bad'],
    unknown: ['未知', null] };
  const CHECK_RESULT = { executed: '已执行', failed: '执行失败', abandoned: '已放弃' };
  const PROPOSAL_STATUS = { posting: ['发卡片中', null], pending: ['等 {{owner_name}}', 'warn'], applied: ['已生效', 'good'],
    rejected: ['不采纳（28 天不再提）', null], needs_revision: ['附条件（需重提）', 'warn'], conflict: ['没生效（冲突）', 'bad'],
    failed: ['发卡片失败', 'bad'], written: ['已写入', 'good'] };
  const SHAPE = { unknown_role: '未知角色', admin_or_owner: 'admin / owner 级', prod_destructive: '生产破坏性 / 不可逆',
    spend_over_threshold: '支出超阈值', group_wide_expansion: '群组级扩展', personal_prod_access: '个人生产 Writer / exec / Secret',
    workload_admin_identity: 'admin workload 身份', auth_entry_change: '认证入口变更', account_wide_secret_read: '全账号 Secret 读取',
    unregistered_principal: '未登记的个人主体' };
  const LEVEL = { auto: '自动', one_tap: '一键批准', {{owner_key}}: '交 {{owner_name}}' };
  const pctText = (v) => v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`;
  const fmtDay = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, month: '2-digit', day: '2-digit' });
  const outcomePillRule = (o) => { const [label, tone] = RULE_OUTCOME[o] || [o, null]; return pill(label, tone); };

  function Rules() {
    const head = pageHead('规则', 'RULES · 经验法则引擎',
      '权限 / 账号类变更先过规则引擎：auto 直接做，one_tap / {{owner_key}} 发决策卡片，clarify 先问清楚，route 转交。列表只显示元数据；点检查编号看那一条的完整记录（含计划）。');
    const registry = h('section', { class: 'ops-card', id: 'rules-registry' });
    const status = h('section', { class: 'ops-card', id: 'rules-status' });
    const weeks = h('section', { class: 'ops-card', id: 'rules-weeks' });
    const backtest = h('section', { class: 'ops-card', id: 'rules-backtest' });
    const recent = h('section', { class: 'ops-card', id: 'rules-recent' });
    const proposals = h('section', { class: 'ops-card', id: 'rules-proposals' });
    clear(main).append(head, registry, status, weeks, backtest, recent, proposals);

    function renderRegistry(d) {  // P47: one row per rule of the registry (the switch and the text of each code check)
      const r = d.registry || { rules: [] };
      fill(registry, h('h3', {}, '规则清单'),
        h('p', { class: 'note' }, (r.source === 'live' ? '线上目录 ' : 'skill 自带的初始版本 ') + (r.dir || '') + '；改规则只能用 rules install（你本人，agent 不行）。'),
        h('table', { class: 'ops-table', id: 'rules-registry-table' },
          h('thead', {}, h('tr', {}, ['规则', '怎么检查', '来由', '状态'].map((x) => h('th', {}, x)))),
          h('tbody', {}, r.rules.map((x) => h('tr', {}, h('td', {}, h('span', { class: 'mono' }, x.id + ' '), x.title),
            h('td', {}, x.how), h('td', {}, x.why), h('td', {}, x.status))))));
    }

    function renderStatus(d) {
      const spec = d.spec || {};
      const cfg = d.config || {};
      const week = (d.weeks || []).slice(-1)[0] || { checks: 0, shares: {}, approval_latency: {} };
      const warns = [];
      if (d.spec_error) warns.push(`规则文件不可用：${d.spec_error}（rule-check 会报错，agent 需要问 {{owner_name}}）`);
      if (cfg.enabled === false) warns.push('规则引擎已关闭（rules.enabled=false）：每条检查都交 {{owner_name}}');
      if (cfg.proposals_enabled === false) warns.push('规则提议已关闭（rules.proposals_enabled=false）');
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, spec.version ? `v${spec.version}` : '—'), h('span', {}, '当前版本（access）')),
          h('div', { class: 'counter' }, h('b', {}, spec.rules || '—'), h('span', {}, '规则数')),
          h('div', { class: 'counter' }, h('b', {}, week.checks), h('span', {}, '本周检查')),
          h('div', { class: 'counter' }, h('b', {}, pctText(week.shares.auto_tier)), h('span', {}, '本周无需 {{owner_name}}')),
          h('div', { class: 'counter' }, h('b', {}, pctText(week.shares.auto_or_one_tap)), h('span', {}, '含一键批准')),
          h('div', { class: 'counter' }, h('b', {}, week.approval_latency.median === null || week.approval_latency.median === undefined ? '—' : dur(week.approval_latency.median)), h('span', {}, '批准耗时（中位）')),
          h('div', { class: 'counter' }, h('b', {}, (d.totals || {}).overrides || 0), h('span', {}, '{{owner_name}} 推翻次数'))),
        h('dl', { class: 'kv' },
          h('dt', {}, '规则文件'), h('dd', { class: 'mono' }, spec.file ? `${spec.file} · ${spec.digest}` : '—'),
          h('dt', {}, '级别'), h('dd', {}, Object.entries(spec.levels || {}).map(([k, n]) => h('span', { class: 'pill' }, `${LEVEL[k] || k} ${n}`))),
          h('dt', {}, 'never-auto'), h('dd', {}, Object.entries(spec.never_auto || {}).map(([k, lvl]) => h('span', { class: 'pill', 'data-tone': lvl === '{{owner_key}}' ? 'bad' : 'warn' }, `${SHAPE[k] || k} → ${LEVEL[lvl] || lvl}`))),
          h('dt', {}, '一键批准例外'), h('dd', {}, Object.entries(spec.claims || {}).map(([rid, c]) => h('span', { class: 'pill' }, `${rid}：${Object.keys(c).map((x) => SHAPE[x] || x).join('、')}`))),
          h('dt', {}, '开关'), h('dd', {}, pill(cfg.enabled === false ? '引擎关' : '引擎开', cfg.enabled === false ? 'bad' : 'good'), ' ',
            pill(cfg.proposals_enabled === false ? '提议关' : '提议开', cfg.proposals_enabled === false ? 'warn' : 'good'))),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderWeeks(d) {
      const items = (d.weeks || []).slice().reverse();
      fill(weeks, h('h3', {}, '每周结论分布'),
        h('p', { class: 'note' }, '自动占比 = auto + clarify + route（不需要 {{owner_name}} 出手，和 v1 估计口径一致）；含一键批准再加 one_tap。'),
        h('table', { class: 'ops-table', id: 'rules-week-table' },
          h('thead', {}, h('tr', {}, ['周（起）', '检查', '自动', '一键批准', '先补信息', '转交', '交 {{owner_name}}', '无需 {{owner_name}}', '含一键批准', '批准耗时 中位 / P90', '推翻', '已执行'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((w) => h('tr', {},
            h('td', { class: 'num' }, fmtDay.format(new Date(w.start * 1000))),
            h('td', { class: 'num' }, w.checks),
            ...['auto', 'one_tap', 'clarify', 'route', '{{owner_key}}'].map((o) => h('td', { class: 'num' }, w.outcomes[o] || 0)),
            h('td', { class: 'num' }, pctText(w.shares.auto_tier)), h('td', { class: 'num' }, pctText(w.shares.auto_or_one_tap)),
            h('td', { class: 'num' }, w.approval_latency.n ? `${dur(w.approval_latency.median)} / ${dur(w.approval_latency.p90)}` : '—'),
            h('td', { class: 'num' }, w.overrides), h('td', { class: 'num' }, w.executed))))));
    }
    function renderBacktest(d) {
      const b = d.backtest;
      if (!b) { fill(backtest, h('h3', {}, '回测'), h('p', { class: 'note' }, '还没有记录回测（rules backtest --record）。')); return; }
      const sh = b.shares || {};
      const ok = b.pass && b.pass.overall;
      fill(backtest, h('h3', {}, '回测 ', pill(ok ? '通过' : '未通过', ok ? 'good' : 'bad')),
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, b.cases), h('span', {}, '案例')),
          h('div', { class: 'counter' }, h('b', {}, `${pctText(sh.engine_auto)} / ${pctText(sh.v1_auto)}`), h('span', {}, '无需 {{owner_name}}：引擎 / v1')),
          h('div', { class: 'counter' }, h('b', {}, `${pctText(sh.engine_auto_or_one_tap)} / ${pctText(sh.v1_auto_or_one_tap)}`), h('span', {}, '含一键批准：引擎 / v1')),
          h('div', { class: 'counter' }, h('b', {}, `${sh.delta_auto_pp} / ${sh.delta_auto_or_one_tap_pp} pp`), h('span', {}, `偏差（容差 ±${b.tolerance_pp} pp）`)),
          h('div', { class: 'counter' }, h('b', {}, `${(b.agreement || {}).cases || 0}/${b.cases}`), h('span', {}, '逐案一致'))),
        h('p', { class: 'note' }, `规则 ${b.spec ? 'v' + b.spec.version + ' · ' + b.spec.digest : ''} · 生成于 ${b.generated_at || '—'} · 逐案分歧只在私密目录。`));
    }
    function renderRecent(d) {
      const items = d.recent || [];
      fill(recent, h('h3', {}, '最近的检查 ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'rules-recent-table' },
          h('thead', {}, h('tr', {}, ['时间', '检查', '系统 · 动作', '命中规则', '结论', 'never-auto', '批准', '结果', '任务'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((c) => {
            const [alabel, atone] = APPROVAL[c.approval] || [c.approval, null];
            return h('tr', { 'data-check': c.id },
              h('td', { class: 'num' }, when(c.created_at), c.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, h('a', { href: '/rules?check=' + c.id }, c.id)),
              h('td', {}, `${c.system} · ${c.action}`, h('div', { class: 'sub' }, c.kind)),
              h('td', { class: 'mono' }, (c.rules || []).join(' ') || '—'),
              h('td', {}, outcomePillRule(c.outcome)),
              h('td', {}, (c.never_auto || []).map((x) => pill(SHAPE[x] || x, 'bad'))),
              h('td', {}, pill(alabel, atone)),
              h('td', {}, CHECK_RESULT[c.result] || '—', c.corrected ? h('div', {}, pill('{{owner_name}} 已纠正', 'warn')) : null),
              h('td', { class: 'mono' }, c.task_id || '—'));
          }))) : h('div', { class: 'empty' }, h('b', {}, '还没有规则检查'), d.notice === 'rules_tables_missing' ? '守护进程还没有创建规则表。' : 'agent 做权限 / 账号变更前运行 rule-check 后会出现在这里。'));
    }
    function renderProposals(d) {
      const items = d.proposals || [];
      fill(proposals, h('h3', {}, '规则提议 ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'rules-proposal-table' },
          h('thead', {}, h('tr', {}, ['时间', '提议', '类型', '标题', '状态', '版本', ''].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [label, tone] = PROPOSAL_STATUS[x.status] || [x.status, null];
            return h('tr', {},
              h('td', { class: 'num' }, when(x.created_at), x.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, h('a', { href: '/rules?proposal=' + x.id }, x.id)),
              h('td', {}, x.kind === 'rule_patch' ? '规则补丁' : '分诊补充规则'),
              h('td', {}, x.title_zh),
              h('td', {}, pill(label, tone), x.error ? h('div', { class: 'sub' }, x.error) : null),
              h('td', { class: 'num' }, x.applied_version ? `v${x.base_version} → v${x.applied_version}` : (x.base_version ? `基于 v${x.base_version}` : '—')),
              h('td', {}, x.suppress_until ? h('div', { class: 'sub' }, '到 ' + when(x.suppress_until) + ' 前不再提') : null));
          }))) : h('p', { class: 'note' }, '还没有规则提议：每周一 10:07 由 {{owner_key}}-inbox 的定时任务复盘后提出。'),
        (d.changelog || []).length ? h('details', { class: 'hist' }, h('summary', {}, `变更记录（${d.changelog.length}）`),
          h('table', { class: 'ops-table' }, h('tbody', {}, d.changelog.map((c) => h('tr', {},
            h('td', { class: 'num' }, c.at || '—'), h('td', {}, c.kind), h('td', { class: 'mono' }, c.to || (c.to_version ? 'v' + c.to_version : '—')),
            h('td', {}, c.title_zh || c.who || '—')))))) : null);
    }
    async function load() {
      let data;
      try { data = await api('/api/rules'); online(true); } catch (e) { online(false); return; }
      renderRegistry(data); renderStatus(data); renderWeeks(data); renderBacktest(data); renderRecent(data); renderProposals(data);
    }
    every(8000, load);
  }

  // Per-check / per-proposal detail (P3.1): the technical record a Slack card links to instead of showing it.
  const CHECK_STATUS = { fail: '不通过', missing: '缺信息', escalate: '要批准', route: '转交' };
  const RESULT_LABEL = { executed: '已执行', failed: '执行失败', abandoned: '已放弃' };
  const PROVENANCE_LABEL = { slack_button: '点按钮', thread_reply: 'thread 回复', text_confirmed: '文字 + 确认按钮', smoke_synthetic: 'TEST 合成',
    {{owner_key}}_typed: '你在卡片 thread 里亲手打的指示' };
  function RuleCheckDetail(id) {
    const crumbs = h('nav', { class: 'crumbs' }, h('a', { href: '/rules' }, '← 规则'));
    const [head, headStatus] = detailHead('检查 ' + id, 'RULE CHECK · 检查详情',
      'Slack 卡片只写一句话；命中的规则、没通过的检查、事实、批准人和计划都在这里。');
    const facts = h('section', { class: 'ops-card summary', id: 'check-detail' });
    const why = h('section', { class: 'ops-card', id: 'check-why' });
    const planCard = h('section', { class: 'ops-card', id: 'check-plan' });
    clear(main).append(crumbs, head, facts, why, planCard);
    async function load() {
      let d;
      try { d = await api('/api/rules/checks/' + encodeURIComponent(id)); online(true); } catch (e) {
        online(e.status === 404);
        fill(facts, h('div', { class: 'empty' }, h('b', {}, e.status === 404 ? '没有这条检查' : '读取失败'), h('a', { href: '/rules' }, '回到规则')));
        return;
      }
      const c = d.check;
      const a = c.approval || {};
      const [alabel, atone] = APPROVAL[a.status] || [a.status || '—', null];
      const nameOf = (uid) => ((c.approvers || []).find((x) => x.id === uid) || {}).name || uid;
      fill(headStatus, outcomePillRule(c.outcome), pill(alabel, atone), c.smoke ? pill('TEST', 'warn') : null);
      fill(facts, h('div', { class: 'lead' }, h('span', { class: 'lead-label' }, 'agent 的一句话'),
        h('p', { class: 'gap-text' }, c.summary || '（没写：这条检查没有发卡片）')),
      factGrid([
        // the codes behind the words stay in the tooltip
        ['做什么', `${c.what.system_zh} · ${c.what.action_zh} · ${c.what.kind_zh}`, null, `${c.what.system} · ${c.what.action} · ${c.what.kind}`],
        ['结论', c.outcome_zh],
        ['批准', h('span', {}, a.actor ? `${nameOf(a.actor)} · ${when(a.decided_at)}` : alabel,
          a.provenance ? h('div', { class: 'sub' }, '方式：' + (PROVENANCE_LABEL[a.provenance] || a.provenance), a.source ? [' · ', ext(a.source, '那条消息 ↗')] : null) : null,
          a.note ? h('div', { class: 'sub' }, '附条件：' + a.note) : null)],
        ['结果', h('span', {}, c.result ? `${RESULT_LABEL[c.result.result] || c.result.result} · ${when(c.result.at)}` : '—',
          c.result && c.result.by ? h('div', { class: 'sub mono' }, c.result.by) : null,
          c.correction ? h('div', {}, pill('{{owner_name}} 已纠正：' + (c.correction.outcome || ''), 'warn')) : null)],
        ['任务', h('span', {}, h('span', { class: 'mono' }, c.task_id || '—'), h('div', { class: 'sub' }, `${when(c.created_at)} · ${c.caller}`))],
        ['卡片', c.card ? h('span', {}, ext(c.card.slack_url, 'Slack ↗'), ' · ', when(c.card.posted_at)) : '没发卡片'],
        ['规则版本', h('span', {}, `${c.category} v${c.spec_version || '?'}`, c.spec_digest ? h('div', { class: 'sub' }, c.spec_digest) : null), 'mono'],
        ['批准人', h('span', {}, (c.approvers || []).map((x) => pill(x.name || x.id, x.id === c.recipient_slack ? 'bad' : null)),
          c.recipient_slack && (c.approvers || []).some((x) => x.id === c.recipient_slack) ? h('div', { class: 'sub' }, '当事人是批准人：不能自己批准这一项') : null)],
      ]),
      c.result && c.result.note ? h('div', { class: 'lead' }, h('span', { class: 'lead-label' }, '执行结果说明'), h('p', { class: 'gap-text' }, c.result.note)) : null);
      fill(why, h('h3', {}, '为什么是这个结论'),
        h('table', { class: 'ops-table', id: 'check-rules' },
          h('thead', {}, h('tr', {}, ['规则', '说明'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, (c.matched_rules || []).map((r) => h('tr', {}, h('td', { class: 'mono' }, r.id), h('td', {}, r.title_zh || '—'))))),
        (c.failed_checks || []).length ? h('table', { class: 'ops-table', id: 'check-failed' },
          h('thead', {}, h('tr', {}, ['规则 · 检查', '状态', '人话', '原因码', '去向'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, c.failed_checks.map((f) => h('tr', {},
            h('td', { class: 'mono' }, `${f.rule} · ${f.check}`), h('td', {}, CHECK_STATUS[f.status] || f.status),
            h('td', {}, f.why_zh || '—'), h('td', { class: 'mono' }, f.reason), h('td', {}, RULE_OUTCOME[f.outcome] ? RULE_OUTCOME[f.outcome][0] : f.outcome))))) : h('p', { class: 'note' }, '所有检查都通过了。'),
        (c.never_auto || []).length ? h('div', { class: 'sub' }, 'never-auto：', c.never_auto.map((x) => pill(`${SHAPE[x.shape] || x.shape}${x.zh ? '（' + x.zh + '）' : ''} → ${LEVEL[x.effective] || x.effective}${x.claimed_by ? ' · ' + x.claimed_by : ''}`, 'bad'))) : null,
        h('details', { class: 'hist' }, h('summary', {}, '事实（代码查到的）'), h('pre', { class: 'json' }, JSON.stringify(c.facts, null, 2))));
      fill(planCard, h('h3', {}, '计划 ', h('span', { class: 'sub mono' }, c.plan_hash)),
        h('p', { class: 'note' }, '批准只对这份计划有效；计划有任何变化都要重新检查。'),
        h('pre', { class: 'json' }, JSON.stringify(c.plan, null, 2)));
    }
    every(15000, load);
  }

  function RuleProposalDetail(id) {
    const crumbs = h('nav', { class: 'crumbs' }, h('a', { href: '/rules' }, '← 规则'));
    const [head, headStatus] = detailHead('提议 ' + id, 'RULE PROPOSAL · 规则提议详情', '卡片上只写一句话和理由；改动的具体内容在这里。');
    const card = h('section', { class: 'ops-card summary', id: 'proposal-detail' });
    const body = h('section', { class: 'ops-card', id: 'proposal-body' });
    clear(main).append(crumbs, head, card, body);
    async function load() {
      let d;
      try { d = await api('/api/rules/proposals/' + encodeURIComponent(id)); online(true); } catch (e) {
        online(e.status === 404);
        fill(card, h('div', { class: 'empty' }, h('b', {}, e.status === 404 ? '没有这条提议' : '读取失败'), h('a', { href: '/rules' }, '回到规则')));
        return;
      }
      const x = d.proposal;
      const [label, tone] = PROPOSAL_STATUS[x.status] || [x.status, null];
      const ev = x.evidence || {};
      fill(headStatus, pill(label, tone), x.smoke ? pill('TEST', 'warn') : null);
      fill(card, h('div', { class: 'lead' }, h('span', { class: 'lead-label' }, '标题'), h('p', { class: 'lead-title' }, x.title_zh)),
        factGrid([
          ['类型', x.kind === 'rule_patch' ? '规则补丁' : '分诊补充规则'],
          ['状态', h('span', {}, label, x.error ? h('div', { class: 'sub' }, x.error) : null)],
          ['版本', x.applied_version ? `v${x.base_version} → v${x.applied_version}` : (x.base_version ? `基于 v${x.base_version}` : '—'), 'mono'],
          ['提出', h('span', {}, when(x.created_at), x.proposer ? h('div', { class: 'sub' }, x.proposer) : null)],
          ['决定', x.decided_at ? h('span', {}, when(x.decided_at), x.decided_by ? h('div', { class: 'sub' }, x.decided_by) : null) : '还没决定'],
        ]));
      fill(body, h('h3', {}, '内容'), kv([
        ['理由', x.rationale_zh, 'gap-text'],
        x.text_zh ? ['要加的这句', x.text_zh, 'gap-text'] : null,
        x.kind === 'rule_patch' ? ['改动', (x.lines || []).length ? h('ul', { class: 'patch-lines' }, x.lines.map((l) => h('li', { class: 'mono' }, l))) : h('pre', { class: 'json' }, JSON.stringify(x.patch, null, 2))] : null,
        ['依据', h('span', {}, Object.entries(ev.counts || {}).map(([k, v]) => pill(`${k} ${v}`, null)),
          (ev.check_ids || []).map((cid) => [' ', h('a', { class: 'mono', href: '/rules?check=' + cid }, cid)]))],
      ]));
    }
    every(15000, load);
  }

  // =====================================================================
  // Routines (定时, P3) — read-only
  // =====================================================================
  const ROUTINE_STATE = { active: ['启用', 'good'], disabled: ['已停用', 'warn'], expired: ['已过期', null], unapproved: ['未批准（不执行）', 'bad'],
    awaiting_confirmation: ['等 {{owner_name}} 确认（不执行）', 'warn'], rejected: ['{{owner_name}} 未启用（不执行）', 'bad'],
    invalid: ['文件无效（不执行）', 'bad'], removed: ['已删除', null] };
  const PROVENANCE = { {{owner_key}}_authored: '{{owner_name}} 编写', {{owner_key}}_approved: '卡片批准', unapproved: '未批准' };
  const CONFIRM_VIA = { card: '卡片确认', cli: '终端 routines enable 确认', smoke: 'TEST 自动确认' };
  const CONFIRM_STATUS = { confirmed: ['已确认', 'good'], pending: ['确认卡片已发，等 {{owner_name}}', 'warn'], posting: ['正在发确认卡片', 'warn'],
    failed: ['确认卡片没发出去', 'bad'], rejected: ['已点不启用', 'bad'], needs_revision: ['带条件：改文件或 routines enable', 'warn'],
    not_{{owner_key}}: ['别人点的不算：需要 {{owner_name}} 运行 routines enable', 'warn'] };
  function confirmLine(c) {
    if (!c) return '还没确认';
    if (c.status === 'confirmed') return `${CONFIRM_VIA[c.via] || c.via}${c.decided_at ? ' · ' + when(c.decided_at) : ''}`;
    return (CONFIRM_STATUS[c.status] || [c.status])[0] + (c.error ? `：${c.error}` : '');
  }
  const ROUTINE_RUN_STATE = { fired: ['已触发', 'good'], skipped_in_flight: ['跳过：上次还没跑完', 'warn'], skipped_daily_cap: ['跳过：今日次数用完', 'warn'],
    failed: ['失败', 'bad'] };
  const RUN_TRIGGER = { schedule: '按时', catch_up: '补跑（错过的只补一次）', run_now: '手动', smoke: '测试' };

  function Routines() {
    const head = pageHead('定时任务', 'ROUTINES · 定时任务',
      '每个任务目录下的 routines.yaml：手写的条目要 {{owner_name}} 确认一次（卡片 [启用] 或终端 routines enable，改动后重新确认），之后按 {{owner_name}} 意图在 scope 内执行；agent 提议的经卡片批准后由代码写入。错过的最多补跑一次，上次还没跑完就跳过。');
    const status = h('section', { class: 'ops-card', id: 'routines-status' });
    const table = h('section', { class: 'ops-card', id: 'routines-list' });
    const runs = h('section', { class: 'ops-card', id: 'routines-runs' });
    const props = h('section', { class: 'ops-card', id: 'routines-proposals' });
    clear(main).append(head, status, table, runs, props);

    function renderStatus(d) {
      const cfg = d.config || {};
      const c = d.counts || {};
      const day = (d.runs || []).filter((r) => r.state === 'fired' && r.fired_at * 1000 > Date.now() - 86400000).length;
      const warns = [];
      if (cfg.enabled === false) warns.push('定时任务总开关已关闭（routines.enabled=false）：所有定时任务暂停');
      if (d.config_error) warns.push(`配置无效：${d.config_error}`);
      for (const e of d.errors || []) warns.push(`${e.task_id} 的 routines.yaml 无效（这个任务的定时任务都不执行）：${e.error}`);
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, pill(cfg.enabled === false ? '已关闭' : '运行中', cfg.enabled === false ? 'bad' : 'good')), h('span', {}, '总开关')),
          h('div', { class: 'counter' }, h('b', {}, c.active || 0), h('span', {}, '启用')),
          h('div', { class: 'counter' }, h('b', {}, c.disabled || 0), h('span', {}, '已停用')),
          h('div', { class: 'counter' }, h('b', {}, c.awaiting_confirmation || 0), h('span', {}, '等 {{owner_name}} 确认')),
          h('div', { class: 'counter' }, h('b', {}, (c.unapproved || 0) + (c.rejected || 0) + (c.invalid || 0)), h('span', {}, '未批准 / 未启用 / 无效')),
          h('div', { class: 'counter' }, h('b', {}, day), h('span', {}, '近 24 小时触发'))),
        h('dl', { class: 'kv' },
          h('dt', {}, '限制'), h('dd', {}, `间隔不少于 ${cfg.min_interval_minutes || 15} 分钟 · 每任务最多 ${cfg.max_per_task || 20} 条 · 每任务每天最多 ${cfg.max_runs_per_day || 48} 次 · 每 ${cfg.scan_seconds || 60} 秒扫描文件`),
          h('dt', {}, '命令'), h('dd', { class: 'mono' }, 'routines list | validate | run-now | disable | enable（{{owner_name}}）· routine-propose（agent）')),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderList(d) {
      const items = d.routines || [];
      fill(table, h('h3', {}, '定时任务 ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'routines-table' },
          h('thead', {}, h('tr', {}, ['任务', 'id', '时间', '下次', '上次', '状态', '来源', '范围 / 内容'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((r) => {
            const [label, tone] = ROUTINE_STATE[r.state] || [r.state, null];
            return h('tr', { 'data-routine': `${r.task_id}/${r.routine_id}` },
              h('td', { class: 'mono' }, r.task_id, r.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, r.routine_id),
              h('td', {}, r.when || '—', h('div', { class: 'sub mono' }, r.schedule ? `cron ${r.schedule}` : (r.every ? `every ${r.every}` : '')),
                r.until ? h('div', { class: 'sub' }, '截止 ' + r.until) : null),
              h('td', { class: 'num' }, r.next_at ? when(r.next_at) : '—'),
              h('td', { class: 'num' }, r.last_fired_at ? when(r.last_fired_at) : '—'),
              h('td', {}, pill(label, tone), r.error ? h('div', { class: 'sub' }, r.error) : null),
              h('td', {}, PROVENANCE[r.provenance] || r.provenance, r.approved_by ? h('div', { class: 'sub mono' }, r.approved_by) : null,
                r.provenance === '{{owner_key}}_authored' ? h('div', { class: 'sub' }, confirmLine(r.confirmation)) : null),
              h('td', {}, r.scope || '—', h('div', { class: 'sub' }, r.prompt || '')));
          }))) : h('div', { class: 'empty' }, h('b', {}, '还没有定时任务'), d.notice === 'routines_tables_missing' ? '守护进程还没有创建定时任务表。' : '在任务目录写 routines.yaml，或让 agent 用 routine-propose 提议。'));
    }
    function renderRuns(d) {
      const items = d.runs || [];
      fill(runs, h('h3', {}, '最近的触发 ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'routines-run-table' },
          h('thead', {}, h('tr', {}, ['时间', '任务', 'id', '触发', '结果', '投递'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [label, tone] = ROUTINE_RUN_STATE[x.state] || [x.state, null];
            const delivered = x.delivery === 'event' ? ['投给当前会话', x.event_status ? (EVENT_STATUS[x.event_status] || [x.event_status])[0] : null]
              : x.delivery === 'launch' ? ['新开会话', x.dispatch_state ? ((DISPATCH_STATE[x.dispatch_state] || [x.dispatch_state])[0]) : null] : ['—', null];
            return h('tr', {},
              h('td', { class: 'num' }, when(x.fired_at), x.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, x.task_id), h('td', { class: 'mono' }, x.routine_id),
              h('td', {}, RUN_TRIGGER[x.trigger] || x.trigger),
              h('td', {}, pill(label, tone), x.error ? h('div', { class: 'sub' }, x.error) : null),
              h('td', {}, delivered[0], delivered[1] ? h('div', { class: 'sub' }, delivered[1]) : null));
          }))) : h('p', { class: 'note' }, '还没有触发记录。'));
    }
    function renderProposals(d) {
      const items = d.proposals || [];
      const confs = d.confirmations || [];
      fill(props, h('details', { class: 'hist', id: 'routines-confirmations' }, h('summary', {}, `{{owner_name}} 的确认（${confs.length}）`),
        confs.length ? h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['时间', '任务', 'id', '定义', '状态', '方式'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, confs.map((x) => {
            const [label, tone] = CONFIRM_STATUS[x.status] || [x.status, null];
            return h('tr', {}, h('td', { class: 'num' }, when(x.decided_at || x.created_at), x.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, x.task_id), h('td', { class: 'mono' }, x.routine_id), h('td', { class: 'mono' }, x.digest),
              h('td', {}, pill(label, tone), x.error ? h('div', { class: 'sub' }, x.error) : null),
              h('td', {}, x.via ? (CONFIRM_VIA[x.via] || x.via) : (x.card ? '卡片' : '—')));
          }))) : h('p', { class: 'note' }, '还没有需要确认的手写定时任务。')),
        h('details', { class: 'hist', id: 'routines-proposal-list' }, h('summary', {}, `定时任务提议（${items.length}）`),
        items.length ? h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['时间', '提议', '任务', 'id', '状态', '提议人'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [label, tone] = PROPOSAL_STATUS[x.status] || [x.status, null];
            return h('tr', {}, h('td', { class: 'num' }, when(x.created_at)), h('td', { class: 'mono' }, x.id),
              h('td', { class: 'mono' }, x.task_id), h('td', { class: 'mono' }, x.routine_id),
              h('td', {}, pill(label, tone), x.error ? h('div', { class: 'sub' }, x.error) : null),
              h('td', { class: 'mono' }, short(x.proposer, 20)));
          }))) : h('p', { class: 'note' }, '还没有提议。')));
    }
    async function load() {
      let data;
      try { data = await api('/api/routines'); online(true); } catch (e) { online(false); return; }
      renderStatus(data); renderList(data); renderRuns(data); renderProposals(data);
    }
    every(8000, load);
  }

  // =====================================================================
  // Gaps (缺口, P3.1) — capability blockers + automation gap ledger, read-only. Labels and ids only on the list;
  // tried / unblock text on the detail page; verification codes are never stored anywhere.
  // =====================================================================
  const GAP_TYPE = { no_api: 'API 不支持', no_skill: '没有 skill', auth_missing: '缺凭证', auth_expired: '凭证失效',
    mfa_code: '要验证码', permission: '权限不够', ui_only: '只能网页', external_owner: '等外部负责人' };
  const GAP_STATUS = { open: ['等 {{owner_name}} 选', 'warn'], in_progress: ['处理中', null], resolved: ['已解决', 'good'],
    superseded: ['已被新报告取代', null] };
  const GAP_RESOLUTION = { {{owner_key}}_manual: '{{owner_name}} 人工处理', credential_added: '{{owner_name}} 补了凭证', agent_browser: 'agent 自己登录',
    skill_built: '做成脚本 / skill', skipped: '跳过' };
  const GAP_CHOICE = { manual: '{{owner_name}} 自己做', credential: '{{owner_name}} 补凭证', agent_login: 'agent 自己登录',
    agent_build: 'agent 写脚本 / skill', skip: '跳过' };
  const GAP_PROPOSAL = { posting: ['发卡片中', null], pending: ['等 {{owner_name}}', 'warn'], launching: ['建任务中', 'warn'],
    launched: ['已建任务', 'good'], launch_failed: ['没建成', 'bad'], rejected: ['先不做', null],
    needs_revision: ['附条件（需重提）', 'warn'], failed: ['发卡片失败', 'bad'] };
  const typePill = (t) => pill(GAP_TYPE[t] || t, t === 'mfa_code' ? 'warn' : null);
  function gapStatus(g) {
    const [label, tone] = GAP_STATUS[g.status] || [g.status, null];
    const sub = g.status === 'in_progress' && g.waiting_on_{{owner_key}} ? '等 {{owner_name}} 做完点“已完成”'
      : g.status === 'in_progress' && g.choice ? `{{owner_name}} 选了：${GAP_CHOICE[g.choice] || g.choice}`
        : g.resolution ? GAP_RESOLUTION[g.resolution] || g.resolution : null;
    return [pill(label, tone), sub ? h('div', { class: 'sub' }, sub) : null, g.smoke ? h('div', {}, pill('TEST', 'warn')) : null];
  }
  const gapLink = (g) => h('a', { class: 'mono', href: '/gaps/' + g.id }, g.id);

  function Gaps() {
    const head = pageHead('缺口', 'GAPS · 能力阻塞与自动化缺口',
      'agent 做不到的事（没有 API / skill、缺凭证或凭证失效、要验证码、权限不够、只能网页、等外部负责人）用 report-blocker 发卡片给 {{owner_name}}，并在这里记一条缺口；每周复盘把高频路径提议成 skill 任务。列表只显示系统 / 操作 / 类型 / 状态，“试过什么 / 怎样能解开”在详情页；验证码从不存储。');
    const status = h('section', { class: 'ops-card', id: 'gaps-status' });
    const weeks = h('section', { class: 'ops-card', id: 'gaps-weeks' });
    const systems = h('section', { class: 'ops-card', id: 'gaps-systems' });
    const open = h('section', { class: 'ops-card', id: 'gaps-open' });
    const paths = h('section', { class: 'ops-card', id: 'gaps-paths' });
    const recent = h('section', { class: 'ops-card', id: 'gaps-recent' });
    const props = h('section', { class: 'ops-card', id: 'gaps-proposals' });
    clear(main).append(head, status, open, paths, weeks, systems, recent, props);

    function renderStatus(d) {
      const c = d.counts || {};
      const cfg = d.config || {};
      const warns = [];
      if (cfg.enabled === false) warns.push('能力阻塞卡片已关闭（blockers.enabled=false）：report-blocker 只记缺口、不发卡片，也不发“已完成”卡片和提醒');
      if (cfg.skill_proposals_enabled === false) warns.push('skill 提议已关闭（blockers.skill_proposals_enabled=false）：已决定的卡片暂不建任务');
      if (d.config_error) warns.push(`配置无效：${d.config_error}`);
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, (c.open || 0) + (c.in_progress || 0)), h('span', {}, '未解决')),
          h('div', { class: 'counter' }, h('b', {}, c.waiting_on_{{owner_key}} || 0), h('span', {}, '等 {{owner_name}}')),
          h('div', { class: 'counter' }, h('b', {}, c.resolved || 0), h('span', {}, '已解决')),
          h('div', { class: 'counter' }, h('b', {}, c.{{owner_key}}_manual || 0), h('span', {}, '{{owner_name}} 人工接手')),
          h('div', { class: 'counter' }, h('b', {}, c.superseded || 0), h('span', {}, '被新报告取代')),
          h('div', { class: 'counter' }, h('b', {}, c.total || 0), h('span', {}, '总缺口（不含 TEST）'))),
        h('dl', { class: 'kv' },
          h('dt', {}, '开关'), h('dd', {}, pill(cfg.enabled === false ? '卡片关' : '卡片开', cfg.enabled === false ? 'bad' : 'good'), ' ',
            pill(cfg.skill_proposals_enabled === false ? 'skill 提议关' : 'skill 提议开', cfg.skill_proposals_enabled === false ? 'warn' : 'good')),
          h('dt', {}, '提醒'), h('dd', {}, `卡片等了 ${cfg.reminder_hours || 24} 小时还没人点，就在 thread 里提醒一次（每张卡片一次）`),
          h('dt', {}, '命令'), h('dd', { class: 'mono' }, 'report-blocker · resolve-blocker · blocker-code · gaps list | show · gap-propose · smoke-blocker')),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderOpen(d) {
      const items = d.open || [];
      fill(open, h('h3', {}, '未解决的阻塞 ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'gaps-open-table' },
          h('thead', {}, h('tr', {}, ['报告', '编号', '任务', '系统 · 操作', '类型', '状态', '已等'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((g) => h('tr', { 'data-gap': g.id },
            h('td', { class: 'num' }, when(g.created_at), g.occurrences > 1 ? h('div', { class: 'sub' }, `报告 ${g.occurrences} 次`) : null),
            h('td', {}, gapLink(g), g.attempt > 1 ? h('div', { class: 'sub' }, `第 ${g.attempt} 次`) : null),
            h('td', { class: 'mono task' }, g.task_id),
            h('td', {}, h('span', { class: 'mono' }, g.system), ' · ', g.operation),
            h('td', {}, typePill(g.type)),
            h('td', {}, gapStatus(g)),
            h('td', { class: 'num' }, g.age_hours === null || g.age_hours === undefined ? '—' : dur(g.age_hours * 3600),
              g.reminded ? h('div', { class: 'sub' }, '已提醒') : null))))) :
          h('div', { class: 'empty' }, h('b', {}, '没有未解决的阻塞'), d.notice === 'gaps_tables_missing' ? '守护进程还没有创建缺口表。' : 'agent 用 report-blocker 报告后会出现在这里。'));
    }
    function renderPaths(d) {
      const items = d.top_paths || [];
      fill(paths, h('h3', {}, '最常见的路径 ', h('span', { class: 'count' }, items.length)),
        h('p', { class: 'note' }, '同一系统 + 同一操作算一条路径；每周复盘对反复出现或 {{owner_name}} 人工接手过的路径提议做成 skill。'),
        items.length ? h('table', { class: 'ops-table', id: 'gaps-path-table' },
          h('thead', {}, h('tr', {}, ['系统 · 操作', '报告', '出现次数', '{{owner_name}} 人工', '未解决', '最近'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((p) => h('tr', {},
            h('td', {}, h('span', { class: 'mono' }, p.system), ' · ', p.operation),
            h('td', { class: 'num' }, p.reports), h('td', { class: 'num' }, p.occurrences),
            h('td', { class: 'num' }, p.{{owner_key}}_manual), h('td', { class: 'num' }, p.open),
            h('td', { class: 'num' }, when(p.last_at)))))) : h('p', { class: 'note' }, '还没有缺口记录。'));
    }
    function renderWeeks(d) {
      const items = (d.weeks || []).slice().reverse();
      fill(weeks, h('h3', {}, '每周'),
        h('table', { class: 'ops-table', id: 'gaps-week-table' },
          h('thead', {}, h('tr', {}, ['周（起）', '报告', '解决', '{{owner_name}} 人工', '按类型'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((w) => h('tr', {},
            h('td', { class: 'num' }, fmtDay.format(new Date(w.start * 1000))),
            h('td', { class: 'num' }, w.reported), h('td', { class: 'num' }, w.resolved), h('td', { class: 'num' }, w.{{owner_key}}_manual),
            h('td', {}, Object.entries(w.by_type || {}).map(([t, n]) => pill(`${GAP_TYPE[t] || t} ${n}`, null))))))));
    }
    function renderSystems(d) {
      const items = d.by_system || [];
      fill(systems, h('h3', {}, '按系统 ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'gaps-system-table' },
          h('thead', {}, h('tr', {}, ['系统', '缺口', '未解决', '{{owner_name}} 人工'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((s) => h('tr', {}, h('td', { class: 'mono' }, s.system), h('td', { class: 'num' }, s.total),
            h('td', { class: 'num' }, s.open), h('td', { class: 'num' }, s.{{owner_key}}_manual))))) : null,
        h('dl', { class: 'kv' },
          h('dt', {}, '按类型'), h('dd', {}, Object.keys(d.by_type || {}).length ? Object.entries(d.by_type).map(([t, n]) => pill(`${GAP_TYPE[t] || t} ${n}`, t === 'mfa_code' ? 'warn' : null)) : '—'),
          h('dt', {}, '解决方式'), h('dd', {}, Object.keys(d.by_resolution || {}).length ? Object.entries(d.by_resolution).map(([r, n]) => pill(`${GAP_RESOLUTION[r] || r} ${n}`, r === '{{owner_key}}_manual' || r === 'credential_added' ? 'warn' : 'good')) : '—')));
    }
    function renderRecent(d) {
      const items = d.recent || [];
      fill(recent, h('h3', {}, '最近的阻塞 ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'gaps-recent-table' },
          h('thead', {}, h('tr', {}, ['报告', '编号', '任务', '系统 · 操作', '类型', '状态 / 解决方式'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((g) => h('tr', { 'data-gap': g.id },
            h('td', { class: 'num' }, when(g.created_at)), h('td', {}, gapLink(g)), h('td', { class: 'mono task' }, g.task_id),
            h('td', {}, h('span', { class: 'mono' }, g.system), ' · ', g.operation), h('td', {}, typePill(g.type)),
            h('td', {}, gapStatus(g)))))) : h('p', { class: 'note' }, '还没有阻塞记录。'));
    }
    function renderProposals(d) {
      const items = d.proposals || [];
      fill(props, h('h3', {}, 'skill 提议 ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'gaps-proposal-table' },
          h('thead', {}, h('tr', {}, ['时间', '提议', '系统 · 操作', '状态', '任务'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [label, tone] = GAP_PROPOSAL[x.status] || [x.status, null];
            return h('tr', {}, h('td', { class: 'num' }, when(x.created_at), x.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, x.id), h('td', {}, h('span', { class: 'mono' }, x.system), ' · ', x.operation),
              h('td', {}, pill(label, tone), x.error ? h('div', { class: 'sub' }, x.error) : null,
                x.suppress_until ? h('div', { class: 'sub' }, '到 ' + when(x.suppress_until) + ' 前不再提') : null),
              h('td', { class: 'mono' }, x.task_id || '—'));
          }))) : h('p', { class: 'note' }, '还没有 skill 提议：每周一由 {{owner_key}}-inbox 的定时复盘根据这里的缺口提出。'));
    }
    async function load() {
      let data;
      try { data = await api('/api/gaps'); online(true); } catch (e) { online(false); return; }
      renderStatus(data); renderOpen(data); renderPaths(data); renderWeeks(data); renderSystems(data); renderRecent(data);
      renderProposals(data);
    }
    every(8000, load);
  }

  function GapDetail(id) {
    const crumbs = h('nav', { class: 'crumbs' }, h('a', { href: '/gaps' }, '← 缺口'));
    const [head, headStatus] = detailHead('阻塞 ' + id, 'GAP · 能力阻塞详情', '“试过什么 / 怎样能解开”是 agent 写的说明（发卡片前查过不含凭据）；验证码从不存储。');
    const facts = h('section', { class: 'ops-card summary', id: 'gap-detail' });
    const texts = h('section', { class: 'ops-card', id: 'gap-texts' });
    const more = h('section', { class: 'ops-card', id: 'gap-more' });
    clear(main).append(crumbs, head, facts, texts, more);
    async function load() {
      let d;
      try { d = await api('/api/gaps/' + encodeURIComponent(id)); online(true); } catch (e) {
        online(e.status === 404);
        fill(facts, h('div', { class: 'empty' }, h('b', {}, e.status === 404 ? '没有这条阻塞' : '读取失败'), h('a', { href: '/gaps' }, '回到缺口')));
        return;
      }
      const g = d.gap;
      const optionLabel = (oid) => ((g.options || []).find((o) => o.id === oid) || {}).label || oid;
      const [slabel, stone] = GAP_STATUS[g.status] || [g.status, null];
      fill(headStatus, pill(slabel, stone), typePill(g.type), g.smoke ? pill('TEST', 'warn') : null);
      fill(facts, factGrid([
        ['状态', h('span', {}, slabel, gapStatus(g)[1])],
        ['任务', g.task_id, 'mono'],
        ['系统 · 操作', h('span', {}, h('span', { class: 'mono' }, g.system), ' · ', g.operation)],
        ['报告', h('span', {}, `${when(g.created_at)} · ${g.reporter}`, g.run_id ? h('div', { class: 'sub' }, h('a', { href: '/runs/' + g.run_id }, 'run ' + g.run_id)) : null,
          g.occurrences > 1 ? h('div', { class: 'sub' }, `同一路径报告了 ${g.occurrences} 次（没有重复发卡片）`) : null)],
        ['第几次', h('span', {}, `第 ${g.attempt} 次`, g.previous_id ? [' · 上一条 ', h('a', { class: 'mono', href: '/gaps/' + g.previous_id }, g.previous_id)] : null,
          g.superseded_by ? [' · 被 ', h('a', { class: 'mono', href: '/gaps/' + g.superseded_by }, g.superseded_by), ' 取代'] : null)],
        ['{{owner_name}} 的选择', h('span', {}, g.choice ? `${optionLabel(g.choice)} · ${when(g.chosen_at)}` : '还没选',
          g.stage2_choice ? h('div', { class: 'sub' }, `第二张卡片：${g.stage2_choice === 'done' ? '已完成' : '做不了'} · ${when(g.stage2_at)}`) : null)],
        ['解决', h('span', {}, g.resolution ? `${GAP_RESOLUTION[g.resolution] || g.resolution} · ${when(g.resolved_at)} · ${g.resolved_by || ''}` : '—',
          g.{{owner_key}}_manual ? h('div', {}, pill('{{owner_name}} 人工接手', 'warn')) : null,
          g.resolution_note ? h('div', { class: 'sub' }, g.resolution_note) : null)],
      ]));
      fill(texts, h('h3', {}, '说明'), kv([
        ['试过什么', g.tried, 'gap-text'],
        ['怎样能解开', g.unblock, 'gap-text'],
      ]));
      fill(more, h('h3', {}, '卡片与选项'), kv([
        ['卡片', (g.cards || []).length ? h('span', {}, g.cards.map((c) => h('div', {}, c.stage === 1 ? '阻塞卡片 ' : '“已完成”卡片 ', ext(c.slack_url, 'Slack ↗'), ' · ', when(c.posted_at))),
          (g.reminders || []).length ? h('div', { class: 'sub' }, '已提醒：' + g.reminders.map(when).join('、')) : null) : (g.card_error ? `没发出去：${g.card_error}` : '—')],
        ['选项', (g.options || []).length ? h('span', {}, g.options.map((o) => pill(`${o.label} → ${GAP_RESOLUTION[o.resolution] || o.resolution}`, null))) : '—'],
      ]));
    }
    every(15000, load);
  }

  // =====================================================================
  // Learning (P35: 从卡片反馈学规则) — aggregates only, never raw feedback or the distillation text
  // =====================================================================
  const LEARN_CARD = { ask_{{owner_key}}: '问 {{owner_name}}', rule_check: 'rule-check', rule_proposal: '规则提议',
    routine_proposal: '定时提议', routine_confirmation: '定时确认', skill_proposal: 'skill 提议',
    blocker_stage1: '能力阻塞', blocker_stage2: '阻塞(完成)', intake_brief: '收件简报', intake_draft: '收件草稿',
    orphan: '孤儿任务', deadline: '任务截止', decision: '决策卡片' };
  const LEARN_SIGNAL = { override_directive: '推翻 + 可泛化指令', repeat_correction: '同类第 ≥2 次纠正',
    override: '推翻推荐', directive: '可泛化指令' };
  const PROMOTE_STATE = { posting: ['发卡片中', null], open: ['等 {{owner_name}}', 'warn'], resolved: ['已处理', 'good'],
    failed: ['发卡片失败', 'bad'] };
  const PROMOTE_CHOICE = { distill: ['提炼成规则', 'good'], log: ['只记这次', null], drop: ['不用', null],
    distill_failed: ['提炼没接上', 'bad'] };
  const SIGNAL_LABEL = (s) => LEARN_SIGNAL[s] || s;
  function Learning() {
    const head = pageHead('学习', 'LEARNING · 从卡片反馈学规则',
      '每张给 {{owner_name}} 的卡片结清都会记一条交互记录（谁发来什么 → 系统怎么处理 → {{owner_name}} 的选择 + 是否推翻推荐）。很明确时'
      + '（推翻了推荐且用了可泛化措辞，或同类第 ≥2 次纠正）会发一张「要不要固化成规则」的卡片，采纳走正常的规则提议并卡'
      + ' {{owner_name}} 一次点击。这里只显示聚合：反馈原文只在私有脱敏语料里，不入库、不上看板。');
    const status = h('section', { class: 'ops-card', id: 'learning-status' });
    const weeks = h('section', { class: 'ops-card', id: 'learning-weeks' });
    const shapes = h('section', { class: 'ops-card', id: 'learning-shapes' });
    const promotes = h('section', { class: 'ops-card', id: 'learning-promotes' });
    clear(main).append(head, status, weeks, shapes, promotes);

    function renderStatus(d) {
      const c = d.counts || {};
      const cfg = d.config || {};
      const p = c.promote || {};
      const warns = [];
      if (cfg.enabled === false) warns.push('从卡片反馈学规则已关闭（feedback_learning.enabled=false）：不采集、不发固化卡片');
      if (d.config_error) warns.push(`配置无效：${d.config_error}`);
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, c.total || 0), h('span', {}, '交互记录')),
          h('div', { class: 'counter' }, h('b', {}, pctText(c.override_rate)), h('span', {}, '{{owner_name}} 推翻率')),
          h('div', { class: 'counter' }, h('b', {}, p.posted || 0), h('span', {}, '固化卡片')),
          h('div', { class: 'counter' }, h('b', {}, p.open || 0), h('span', {}, '等 {{owner_name}}')),
          h('div', { class: 'counter' }, h('b', {}, p.distill || 0), h('span', {}, '提炼成规则')),
          h('div', { class: 'counter' }, h('b', {}, pctText(c.adoption_rate)), h('span', {}, '固化率（distill / 已决定）'))),
        h('dl', { class: 'kv' },
          h('dt', {}, '开关'), h('dd', {}, pill(cfg.enabled === false ? '采集关' : '采集开', cfg.enabled === false ? 'bad' : 'good'), ' ',
            pill('强信号：' + (cfg.strong_signal_mode || '—'), cfg.strong_signal_mode === 'conservative' ? 'good' : 'warn'), ' ',
            pill(`每天上限 ${cfg.max_promote_cards_per_day ?? '—'}`, null), ' ',
            pill(`拒绝冷却 ${cfg.suppress_days ?? '—'} 天`, null)),
          h('dt', {}, '按卡片类型'), h('dd', {}, Object.keys(c.by_card_type || {}).length
            ? Object.entries(c.by_card_type).sort((a, b) => b[1] - a[1]).map(([t, n]) => pill(`${LEARN_CARD[t] || t} ${n}`, null)) : '—'),
          h('dt', {}, '强信号'), h('dd', {}, Object.keys(p.by_signal || {}).length
            ? Object.entries(p.by_signal).map(([s, n]) => pill(`${SIGNAL_LABEL(s)} ${n}`, 'warn')) : '—'),
          h('dt', {}, '隐私'), h('dd', {}, '反馈原文只在私有脱敏语料（{{feedback_corpus}}，0600）；库里只存提炼 + id'),
          h('dt', {}, '命令'), h('dd', { class: 'mono' }, 'interaction-distill · learning-review · smoke-feedback-learning')),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderWeeks(d) {
      const items = (d.weeks || []).slice().reverse();
      fill(weeks, h('h3', {}, '每周'),
        h('table', { class: 'ops-table', id: 'learning-week-table' },
          h('thead', {}, h('tr', {}, ['周（起）', '交互', '{{owner_name}} 推翻', '含指令', '固化卡片', '提炼成规则'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((w) => h('tr', {},
            h('td', { class: 'num' }, fmtDay.format(new Date(w.start * 1000))),
            h('td', { class: 'num' }, w.records), h('td', { class: 'num' }, w.overrides), h('td', { class: 'num' }, w.directive),
            h('td', { class: 'num' }, w.promotes), h('td', { class: 'num' }, w.distill))))));
    }
    function renderShapes(d) {
      const items = d.shapes || [];
      fill(shapes, h('h3', {}, '最常见的处理模式 ', h('span', { class: 'count' }, items.length)),
        h('p', { class: 'note' }, 'shape = 卡片类型 + 系统 / 类别 + agent 的动作；推翻多、含指令多的最值得固化成规则。'),
        items.length ? h('table', { class: 'ops-table', id: 'learning-shape-table' },
          h('thead', {}, h('tr', {}, ['工作流（shape）', '类型', '次数', '{{owner_name}} 推翻', '含指令', '最近'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((s) => h('tr', {},
            h('td', {}, s.label), h('td', {}, pill(LEARN_CARD[s.card_type] || s.card_type, null)),
            h('td', { class: 'num' }, s.count), h('td', { class: 'num' }, s.overrides), h('td', { class: 'num' }, s.directive),
            h('td', { class: 'num' }, when(s.last_at)))))) :
          h('div', { class: 'empty' }, h('b', {}, '还没有交互记录'),
            d.notice === 'learning_tables_missing' ? '守护进程还没有创建学习表。' : '每张给 {{owner_name}} 的卡片结清后会出现在这里。'));
    }
    function renderPromotes(d) {
      const items = d.promotes || [];
      fill(promotes, h('h3', {}, '「要不要固化成规则」卡片 ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'learning-promote-table' },
          h('thead', {}, h('tr', {}, ['时间', '工作流', '信号', '状态', '{{owner_name}} 的选择'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [sl, st] = PROMOTE_STATE[x.state] || [x.state, null];
            const [cl, ct] = x.choice ? (PROMOTE_CHOICE[x.choice] || [x.choice, null]) : ['—', null];
            return h('tr', {}, h('td', { class: 'num' }, when(x.created_at)), h('td', {}, x.label),
              h('td', {}, pill(SIGNAL_LABEL(x.signal), 'warn')), h('td', {}, pill(sl, st)), h('td', {}, pill(cl, ct)));
          }))) : h('p', { class: 'note' }, '还没有固化卡片：只有在很明确时（保守）才会问 {{owner_name}} 要不要把一类处理固化成规则。'),
        h('p', { class: 'note' }, '采纳 / 不采纳在 ', h('a', { href: '/rules' }, '规则'), ' 页的「规则提议」里（固化走正常的 rule-propose 卡片）。'));
    }
    async function load() {
      let data;
      try { data = await api('/api/learning'); online(true); } catch (e) { online(false); return; }
      renderStatus(data); renderWeeks(data); renderShapes(data); renderPromotes(data);
    }
    every(8000, load);
  }

  // =====================================================================
  // Health (P38: 守护进程健康) — provider status, stranded deliveries, stuck claims. Counts, ids and states only.
  // =====================================================================
  const HEALTH_STATUS = { healthy: ['健康', 'good'], degraded: ['过载 · 已改道', 'bad'] };
  const HEALTH_EVENT = { degraded: '变为过载', healthy: '恢复健康', notice_degraded: '已通知 {{owner_name}}：过载',
    notice_healthy: '已通知 {{owner_name}}：恢复' };
  const SWITCH_WHY = { long_lived: '常驻任务每条事件新会话', context_cap: '上下文到上限换新会话', auto_return: '切回首选 provider',
    degraded_reroute: '首选 provider 过载临时改道', stuck_on_degraded: '卡在过载的 provider 上，已换' };
  const STRANDED_STATE = { suspect: ['会话已退出 · 确认中', 'warn'], held: ['已交给任务处理', 'good'],
    rerouted: ['已改投活着的会话', 'good'], unowned: ['找不到所属任务', 'bad'], handled: ['已处理完', 'good'] };
  const STUCK_STATE = { posting: ['发卡片中', null], open: ['等 {{owner_name}}', 'warn'], resolved: ['已处理', 'good'],
    cleared: ['会话自己结算了', 'good'], failed: ['发卡片失败', 'bad'] };
  const STUCK_CHOICE = { requeue: '重新投递', settle: '标记为已处理', later: '先不管' };
  const PROBE_RESULT = { ok: ['已恢复', 'good'], down: ['还没恢复', 'bad'], other: ['不是宕机（让运行自己判断）', 'warn'] };  // P44
  // P38 (F49): the private-destination guard's refusals (codes and channel ids only).
  const CARD_DEST_VERDICT = { private: ['私有频道', 'good'], im: ['{{owner_name}} 的私信', 'good'], public: ['公开频道', 'bad'],
    mpim: ['群私信', 'bad'], ext_shared: ['和外部组织共享', 'bad'], im_not_{{owner_key}}: ['不是 {{owner_name}} 的私信', 'bad'], error: ['检查失败', 'bad'] };
  const CARD_DEST_REASON = { public: '频道是公开的', mpim: '是群私信', ext_shared: '频道和外部组织共享', im_not_{{owner_key}}: '不是 {{owner_name}} 的私信',
    not_a_configured_destination: '不是配置的决策频道', missing_scope: 'bot 缺少读频道信息的权限', channel_not_found: '找不到频道（bot 不在里面或 id 写错）',
    not_in_channel: 'bot 不在频道里', invalid_auth: 'bot token 无效', account_inactive: 'bot 账号已停用', no_channel_info: 'Slack 没返回频道信息' };
  function Health() {
    const head = pageHead('健康', 'HEALTH · 守护进程健康',
      '每个 provider 在滚动窗口里的运行情况：过载 / 断流 / 重试标记、失败、常驻任务跑太慢都算一次「出问题」，到阈值就标记过载，'
      + '新会话改用健康的 provider，恢复后自动切回首选，并给 {{owner_name}} 发一条通知（每次过载 / 恢复各一条）。下面还有投递到已退出会话、'
      + '没被消费的消息（会自动交给所属任务重新处理），以及领取后迟迟不结算、堵住后面投递的会话（发卡片问 {{owner_name}}，不会自动释放）。');
    const status = h('section', { class: 'ops-card', id: 'health-status' });
    const providers = h('section', { class: 'ops-card', id: 'health-providers' });
    const switches = h('section', { class: 'ops-card', id: 'health-switches' });
    const stranded = h('section', { class: 'ops-card', id: 'health-stranded' });
    const stuck = h('section', { class: 'ops-card', id: 'health-stuck' });
    const cards = h('section', { class: 'ops-card', id: 'health-card-destination' });
    const cache = h('section', { class: 'ops-card', id: 'health-cache' });
    const wait = h('section', { class: 'ops-card', id: 'health-provider-wait' });
    clear(main).append(head, status, cards, providers, wait, cache, switches, stranded, stuck);

    function renderStatus(d) {
      const cfg = d.config || {};
      const ph = cfg.provider_health || {};
      const ps = (d.providers || {}).providers || [];
      const sc = (d.stranded || {}).counts || {};
      const st = d.stuck_claims || {};
      const warns = [];
      if (ph.enabled === false) warns.push('provider 健康检测已关闭（dispatch.provider_health.enabled=false）：不改道、不通知、不自动切回');
      if (cfg.stranded_revive === false) warns.push('投递救回已关闭（dispatch.stranded_revive=false）');
      if (cfg.stuck_claim_minutes === 0) warns.push('领取卡住提醒已关闭（dispatch.stuck_claim_minutes=0）');
      if (d.config_error) warns.push(`配置无效：${d.config_error}`);
      if (d.notice === 'health_tables_missing') warns.push('守护进程还没有创建健康表（重启 {{service_label}} 后出现）。');
      fill(status,
        h('div', { class: 'counters' },
          ps.map((p) => h('div', { class: 'counter' }, h('b', {}, (HEALTH_STATUS[p.status] || [p.status])[0]),
            h('span', {}, `${p.label} · ${p.model}`))),
          h('div', { class: 'counter' }, h('b', {}, sc.suspect || 0), h('span', {}, '投递到已退出会话（确认中）')),
          h('div', { class: 'counter' }, h('b', {}, (sc.held || 0) + (sc.rerouted || 0) + (sc.handled || 0)), h('span', {}, '24 小时内已救回')),
          h('div', { class: 'counter' }, h('b', {}, sc.unowned || 0), h('span', {}, '找不到所属任务')),
          h('div', { class: 'counter' }, h('b', {}, (st.blocked || []).length), h('span', {}, '领取卡住的会话')),
          h('div', { class: 'counter' }, h('b', {}, st.open_claims || 0), h('span', {}, '未结算的领取'))),
        h('dl', { class: 'kv' },
          h('dt', {}, '健康检测'), h('dd', {}, pill(ph.enabled === false ? '关' : '开', ph.enabled === false ? 'bad' : 'good'), ' ',
            pill(`窗口 ${ph.window_minutes ?? '—'} 分钟`, null), ' ', pill(`${ph.degrade_threshold ?? '—'} 次出问题 → 过载`, null), ' ',
            pill(`连续 ${ph.recover_checks ?? '—'} 次干净 → 恢复`, null), ' ', pill(`常驻任务 ≥ ${ph.slow_run_minutes ?? '—'} 分钟算慢`, null)),
          h('dt', {}, '有界上下文'), h('dd', {}, pill(cfg.long_lived_fresh === false ? '常驻任务照旧 resume' : '常驻任务每条事件新会话',
            cfg.long_lived_fresh === false ? 'warn' : 'good'), ' ',
            pill(cfg.max_session_context_mb ? `会话上下文上限 ${cfg.max_session_context_mb} MB` : '上下文上限关', cfg.max_session_context_mb ? null : 'warn'), ' ',
            (cfg.long_lived_tasks || []).length ? pill('另加常驻：' + cfg.long_lived_tasks.join('、'), null) : null),
          h('dt', {}, '投递'), h('dd', {}, pill(cfg.stranded_revive === false ? '救回关' : '救回开', cfg.stranded_revive === false ? 'bad' : 'good'), ' ',
            pill(cfg.stuck_claim_minutes ? `领取超过 ${cfg.stuck_claim_minutes} 分钟没结算 → 卡片` : '领取卡住提醒关', cfg.stuck_claim_minutes ? null : 'warn')),
          h('dt', {}, '命令'), h('dd', { class: 'mono' }, 'health · health --sweep · config set dispatch.provider_health.enabled false')),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderCards(d) {
      const cd = d.card_destination;
      if (!cd) { fill(cards, h('h3', {}, '卡片频道检查'), h('p', { class: 'note' }, '还没有数据。')); return; }
      const last = cd.last || {};
      const aw = cd.awaiting_repost || {};
      const dests = cd.destinations || [];
      const why = (code) => (code ? (CARD_DEST_REASON[code] ? `${CARD_DEST_REASON[code]}（${code}）` : code) : '—');
      const line = cd.refused_total
        ? `卡片频道检查：24 小时内被拒 ${cd.refused_24h} 次（累计 ${cd.refused_total} 次），最近一次 ${when(last.at)}，原因 ${why(last.reason)}。修好后运行 card-destination-check --refresh 重新检查。`
        : last.at ? `卡片频道检查：最近一次被拒 ${when(last.at)}，原因 ${why(last.reason)}。修好后运行 card-destination-check --refresh 重新检查。`
          : '卡片频道检查：还没有卡片被拒过。';
      const [label, tone] = { refusing: ['正在拒发', 'bad'], refused_recently: ['最近被拒过', 'warn'] }[cd.status] || ['正常', 'good'];
      const notes = [line];
      if (cd.config_error) notes.push(`决策频道配置无效：${cd.config_error}`);
      if ((aw.breaker_cards || 0) + (aw.failure_cards || 0)) {
        notes.push(`${aw.breaker_cards || 0} 张熔断卡片、${aw.failure_cards || 0} 张失败卡片被拒，频道检查通过后自动补发。`);
      }
      fill(cards, h('h3', {}, '卡片频道检查 ', pill(label, tone)),
        h('p', { class: 'note' }, '发给 {{owner_name}} 的卡片和通知，发出前都要 Slack 确认目标是私有频道或 {{owner_name}} 的私信（群私信、和外部组织共享的频道都不行）；确认不了就不发，记在这里。'),
        cd.status === 'ok' ? h('p', { class: 'note' }, line) : h('div', { class: 'warn-box', role: 'status' }, notes.map((t) => h('div', {}, t))),
        last.at ? h('dl', { class: 'kv' }, h('dt', {}, '最近被拒'), h('dd', {}, when(last.at), ' ', h('span', { class: 'mono' }, last.kind || '—'), ' → ',
          h('span', { class: 'mono' }, last.channel || '—'), ' · ', why(last.reason))) : null,
        dests.length ? h('table', { class: 'ops-table', id: 'health-card-destination-table' },
          h('thead', {}, h('tr', {}, ['频道', '配置项', 'Slack 结论', '检查时间'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, dests.map((x) => {
            const [vl, vt] = x.verdict ? (CARD_DEST_VERDICT[x.verdict] || [x.verdict, 'bad']) : ['还没检查', null];
            return h('tr', {}, h('td', { class: 'mono' }, x.channel), h('td', { class: 'mono' }, x.source),
              h('td', {}, pill(vl, vt), x.error ? ' ' + why(x.error) : null,
                x.transient ? [' ', pill(`上次复查临时出错（${x.transient}），沿用之前的结论`, 'warn')] : null),
              h('td', { class: 'num' }, when(x.checked_at)));
          }))) : null);
    }
    function renderProviders(d) {
      const ov = d.providers || {};
      const items = ov.providers || [];
      const events = ov.events || [];
      const displaced = ov.displaced || [];
      fill(providers, h('h3', {}, 'provider'),
        items.length ? h('table', { class: 'ops-table', id: 'health-provider-table' },
          h('thead', {}, h('tr', {}, ['provider', '状态', '自', '窗口内运行', '出问题', '过载 / 断流', '失败', '太慢', '重试', '按模型'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((p) => {
            const [label, tone] = HEALTH_STATUS[p.status] || [p.status, null];
            const w = p.window || {};
            return h('tr', {}, h('td', {}, provChip(p.provider), ' ', h('span', { class: 'mono' }, p.model)), h('td', {}, pill(label, tone)),
              h('td', { class: 'num' }, when(p.since)), h('td', { class: 'num' }, w.runs ?? 0), h('td', { class: 'num' }, w.adverse ?? 0),
              h('td', { class: 'num' }, w.overload ?? 0), h('td', { class: 'num' }, w.failed ?? 0), h('td', { class: 'num' }, w.slow ?? 0),
              h('td', { class: 'num' }, w.retried ?? 0),
              h('td', {}, (w.models || []).map((m) => pill(`${m.model} ${m.adverse}/${m.runs}`, m.adverse ? 'warn' : null))));
          }))) : h('div', { class: 'empty' }, h('b', {}, '还没有数据'), '守护进程跑过健康检测后出现。'),
        h('h3', {}, '最近的变化 ', h('span', { class: 'count' }, events.length)),
        events.length ? h('table', { class: 'ops-table', id: 'health-event-table' },
          h('thead', {}, h('tr', {}, ['时间', 'provider', '变化', '窗口'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, events.map((e) => h('tr', {}, h('td', { class: 'num' }, when(e.at)), h('td', {}, provChip(e.provider)),
            h('td', {}, pill(HEALTH_EVENT[e.kind] || e.kind, e.kind.endsWith('degraded') ? 'bad' : 'good')),
            h('td', { class: 'num' }, e.detail ? `${e.detail.adverse ?? 0}/${e.detail.runs ?? 0}` : '—'))))) :
          h('p', { class: 'note' }, '还没有过载 / 恢复记录。'),
        h('h3', {}, '因回退 / 改道暂时不在首选 provider 上的任务 ', h('span', { class: 'count' }, displaced.length)),
        displaced.length ? h('table', { class: 'ops-table', id: 'health-displaced-table' },
          h('thead', {}, h('tr', {}, ['任务', '现在', '首选'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, displaced.map((t) => h('tr', {}, h('td', { class: 'mono' }, t.task_id), h('td', {}, provChip(t.current_provider)),
            h('td', {}, provChip(t.preferred)))))) :
          h('p', { class: 'note' }, '没有：首选 provider 健康时，下一次调度会自动切回。'));
    }
    // P44: dispatches waiting for a provider to come back, and the one probe per provider that releases them.
    function renderWait(d) {
      const pw = d.provider_wait;
      const cfg = d.config || {};
      if (!pw) { fill(wait, h('h3', {}, '等 AI 恢复'), h('p', { class: 'note' }, '还没有数据。')); return; }
      const ps = pw.providers || {};
      const off = cfg.provider_wait_hours === 0 || cfg.probe_enabled === false;
      fill(wait, h('h3', {}, '等 AI 恢复 ', h('span', { class: 'count' }, pw.waiting || 0)),
        h('p', { class: 'note' }, off ? '已关闭：provider 出错、重试和换 provider 都没用时，直接发失败卡片。'
          : `provider 出错、重试和换 provider 都没用时不发卡片，最多等 ${cfg.provider_wait_hours ?? 48} 小时；每个 provider 同一时间只用一个极小的请求探测，恢复了就自动接着处理。`),
        pw.waiting ? h('p', { class: 'note', id: 'health-wait-oldest' }, `${pw.waiting} 个调度在等，最早的从 ${when(pw.oldest_wait_since)} 开始，已等 ${pw.oldest_wait_hours} 小时。`) : null,
        h('table', { class: 'ops-table', id: 'health-probe-table' },
          h('thead', {}, h('tr', {}, ['provider', '上次探测', '结果', '连续失败', '下次探测', '在等的调度'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, Object.keys(ps).map((k) => {
            const p = ps[k];
            const [label, tone] = p.probing ? ['探测中', null] : (PROBE_RESULT[p.result] || ['还没探测过', null]);
            return h('tr', {}, h('td', {}, provChip(k), p.model ? [' ', h('span', { class: 'mono' }, p.model)] : null),
              h('td', { class: 'num' }, when(p.last_probe_at)),
              h('td', {}, pill(label, tone), p.error_class ? ' ' + (ERROR_LABEL[p.error_class] || p.error_class) : null),
              h('td', { class: 'num' }, p.fails || 0), h('td', { class: 'num' }, when(p.next_probe_at)), h('td', { class: 'num' }, p.waiting || 0));
          }))));
    }
    function renderSwitches(d) {
      const items = (d.providers || {}).switches || [];
      fill(switches, h('h3', {}, '换会话 / 换 provider 记录 ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'health-switch-table' },
          h('thead', {}, h('tr', {}, ['时间', '任务', '原因', '从', '到'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => h('tr', {}, h('td', { class: 'num' }, when(x.at)), h('td', { class: 'mono' }, x.task_key),
            h('td', {}, pill(SWITCH_WHY[x.reason] || x.reason, x.reason === 'long_lived' ? null : 'warn')),
            h('td', {}, x.from_provider ? provChip(x.from_provider) : '—'), h('td', {}, x.to_provider ? provChip(x.to_provider) : '—'))))) :
          h('p', { class: 'note' }, '还没有记录。'));
    }
    function renderStranded(d) {
      const items = (d.stranded || {}).recent || [];
      fill(stranded, h('h3', {}, '投递到已退出会话的消息 ', h('span', { class: 'count' }, items.length)),
        h('p', { class: 'note' }, '消息已排队给某个会话，但它没消费就退出了：连续两次确认它确实不在了（用运行时自己的存活检测，交互式会话不会被误判），'
          + '就交给所属任务（必要时从卡片反查任务）重新起一个后台运行处理，遵守并发上限和每任务一个消费者。'),
        items.length ? h('table', { class: 'ops-table', id: 'health-stranded-table' },
          h('thead', {}, h('tr', {}, ['首次发现', '任务', 'provider', '投递状态', '处理'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [label, tone] = STRANDED_STATE[x.state] || [x.state, null];
            return h('tr', {}, h('td', { class: 'num' }, when(x.first_seen)), h('td', { class: 'mono' }, x.task_key || '—'),
              h('td', {}, provChip(x.provider)), h('td', {}, statusPill(x.status)), h('td', {}, pill(label, tone)));
          }))) : h('p', { class: 'note' }, '没有。'));
    }
    function renderStuck(d) {
      const st = d.stuck_claims || {};
      const blocked = st.blocked || [];
      const cards = st.cards || [];
      fill(stuck, h('h3', {}, '领取后没结算、堵住后面投递的会话 ', h('span', { class: 'count' }, blocked.length)),
        blocked.length ? h('table', { class: 'ops-table', id: 'health-blocked-table' },
          h('thead', {}, h('tr', {}, ['会话', '领取未结算', '最早领取', '排在后面'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, blocked.map((b) => h('tr', {}, h('td', { class: 'mono' }, short(b.session)), h('td', { class: 'num' }, b.claimed),
            h('td', { class: 'num' }, dur(b.oldest_age) + '前'), h('td', { class: 'num' }, b.queued_behind))))) :
          h('p', { class: 'note' }, '没有。'),
        h('h3', {}, '领取卡住卡片 ', h('span', { class: 'count' }, cards.length)),
        cards.length ? h('table', { class: 'ops-table', id: 'health-stuck-table' },
          h('thead', {}, h('tr', {}, ['时间', '任务', '条数', '状态', '{{owner_name}} 的选择'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, cards.map((c) => {
            const [label, tone] = STUCK_STATE[c.state] || [c.state, null];
            return h('tr', {}, h('td', { class: 'num' }, when(c.created_at)), h('td', { class: 'mono' }, c.task_id || short(c.session_id)),
              h('td', { class: 'num' }, c.events), h('td', {}, pill(label, tone)), h('td', {}, c.choice ? pill(STUCK_CHOICE[c.choice] || c.choice, null) : '—'));
          }))) : h('p', { class: 'note' }, '还没有：卡片只在领取超过阈值、而且后面还有消息排队时才发，不会自动释放。'));
    }
    function renderCache(d) {
      const rows = d.summary || [];
      const pct = (x) => (x === null || x === undefined ? '—' : `${Math.round(x * 100)}%`);
      const k = (x) => (x === null || x === undefined ? '—' : `${(x / 1000).toFixed(1)}K`);
      fill(cache, h('h3', {}, '提示缓存（24 小时） ', h('span', { class: 'count' }, d.runs_total || 0)),
        h('p', { class: 'note' }, '每次后台运行第一次调用模型时，输入里有多少是从缓存读的（冷启动 = 一点没读到）。p39 = 旧的提示布局，'
          + 'p40 = 指令在前、本次运行信息在后，再加上按任务类别精简的上下文（dispatch.lean_context）；'
          + 'p40.1 = 每次第一条消息都一模一样，本次运行信息放在文件里，空闲时由保温刷新续上缓存。只有数字，没有消息内容。'),
        rows.length ? h('table', { class: 'ops-table', id: 'health-cache-table' },
          h('thead', {}, h('tr', {}, ['任务', 'provider', '配置', '运行', '冷启动', '首次输入（中位）', '缓存读占比', '耗时（中位）'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, rows.map((r) => h('tr', {}, h('td', { class: 'mono' }, r.task), h('td', {}, provChip(r.provider)),
            h('td', { class: 'mono' }, r.profile), h('td', { class: 'num' }, r.runs),
            h('td', { class: 'num' }, r.measured ? `${r.cold_starts}/${r.measured} · ${pct(r.cold_rate)}` : '—'),
            h('td', { class: 'num' }, k(r.first_input_median)), h('td', { class: 'num' }, pct(r.first_read_share)),
            h('td', { class: 'num' }, r.wall_seconds_median === null || r.wall_seconds_median === undefined ? '—' : dur(r.wall_seconds_median)))))) :
          h('p', { class: 'note' }, d.notice === 'state_database_missing' ? '数据库还不存在。' : '24 小时内没有后台运行。'),
        // P40.1: keep-warm refreshes are listed on their own (not runs): count, how many read the cache, cost.
        ((d.keep_warm && d.keep_warm.summary) || []).length ? h('p', { class: 'note', id: 'health-cache-keepwarm' },
          d.keep_warm.summary.map((s) => `保温刷新 ${s.task}：${s.refreshes} 次（续上 ${s.warmed}，失败 ${s.failed}，冷写 ${s.cold}），`
            + `首次读 ${k(s.first_read_median)} · 写 ${k(s.first_write_median)}，$${(s.cost_usd || 0).toFixed(3)}`).join('；')) : null,
        h('dl', { class: 'kv' }, h('dt', {}, '命令'), h('dd', { class: 'mono' }, 'cache-report [--task T] [--since H] · config set dispatch.lean_context.enabled false · config set dispatch.lean_context.keep_warm.enabled false')));
    }
    let cacheAt = 0;  // P40: the cache panel re-reads run logs, so at most once a minute (the server memoizes too)
    async function load() {
      let data;
      try { data = await api('/api/health'); online(true); } catch (e) { online(false); return; }
      renderStatus(data); renderCards(data); renderProviders(data); renderWait(data); renderSwitches(data); renderStranded(data); renderStuck(data);
      if (Date.now() - cacheAt >= 60000) {
        cacheAt = Date.now();
        try { renderCache(await api('/api/cache')); } catch (e) { cacheAt = 0; }
      }
    }
    every(10000, load);
  }

  // =====================================================================
  // Settings
  // =====================================================================
  const FIELD_LABEL = {
    'providers.claude.model_default': 'Claude 默认模型', 'providers.claude.model_important': 'Claude 重要任务模型',
    'providers.claude.effort': 'Claude effort', 'providers.codex.model': 'Codex 模型', 'providers.codex.effort': 'Codex effort',
    'routing.categories': '任务类别 → provider', 'routing.fallback_order': '后备顺序',
    'dispatch.enabled': '启用后台调度', 'dispatch.progress_updates': 'Slack 进度行', 'dispatch.concurrency': '同时运行上限',
    'dispatch.max_event_age_hours': '旧事件上限（小时）', 'dispatch.claude_env_passthrough': 'Claude 透传环境变量名',
    'circuit_breaker.round_trips_per_hour': '熔断：每小时来回上限', 'intake.channels': '收件频道', 'intake.burst_seconds': '连发合并窗口（秒）',
    'intake.enabled': '启用收件', 'intake.oncall_channels': 'oncall 频道（bot 告警）', 'intake.poll_seconds': '收件轮询间隔（秒）',
    'intake.alert_dedupe_minutes': '告警去重窗口（分钟）', 'intake.dm_queries': '私信发现查询', 'intake.mention_search': '搜索 @{{owner_name}}',
    'intake.delegate_bot_names': '代本人发言的 app',
    'intake.inbox_provider': '收件处 provider', 'intake.inbox_model': '收件处模型', 'intake.inbox_effort': '收件处 effort',
    'intake.oncall_provider': 'oncall provider', 'intake.oncall_model': 'oncall 模型', 'intake.oncall_effort': 'oncall effort',
    'ask.remind_after': '询问卡片提醒时间', 'ask.expire_after': '询问卡片过期时间',
    'rules.enabled': '启用规则引擎', 'rules.proposals_enabled': '启用每周规则提议', 'routines.enabled': '启用定时任务',
    'rules.typed_approval_max_age_hours': '卡片 thread 里亲手打的指示多久内算批准（小时）',
    'blockers.enabled': '启用能力阻塞卡片', 'blockers.skill_proposals_enabled': '启用每周 skill 提议',
    'cards.retire_buttons_on_reply': 'thread 回复后收起按钮', 'cards.thread_reply_decisions': '明确的文字回复直接算决定',
    'cards.read_receipts': '消息已读表情', 'cards.open_poll_seconds': '待决卡片 thread 轮询间隔（秒）',
    'cards.task_thread': '同一任务的卡片串成一条审批 thread', 'cards.retire_on': '哪些 thread 回复会收起按钮',
    'cards.max_open_per_task': '一个任务同时最多几张等你处理的卡',
    'tasks.max_wakes': '每个任务自己唤醒的次数上限', 'tasks.wake_window_hours': '唤醒次数的统计窗口（小时）',
    'tasks.stale_card_days': '没人点的旧检查卡几天后过期',
    'dispatch.provider_wait_hours': '最多等 AI 恢复（小时）', 'dispatch.probe_enabled': '探测 AI 是否恢复',
    'dispatch.probe_min_seconds': '最短探测间隔（秒）', 'dispatch.probe_max_seconds': '最长探测间隔（秒）',
    'dispatch.unresolved_retries': '没处理完的重跑次数',
  };
  const ERROR_FIELD = {
    providers_claude_model_default: 'Claude 默认模型', providers_claude_model_important: 'Claude 重要任务模型',
    providers_claude_effort: 'Claude effort', providers_codex_model: 'Codex 模型', providers_codex_effort: 'Codex effort',
    routing_categories: '任务类别路由', routing_fallback_order: '后备顺序', dispatch_enabled: '启用后台调度',
    dispatch_progress_updates: 'Slack 进度行', dispatch_concurrency: '同时运行上限', dispatch_max_event_age_hours: '旧事件上限',
    dispatch_claude_env_passthrough: 'Claude 透传环境变量名', circuit_breaker_round_trips_per_hour: '熔断上限',
    intake_channels: '收件频道', intake_burst_seconds: '连发合并窗口', ask_remind_after: '询问卡片提醒时间',
    intake_enabled: '启用收件', intake_oncall_channels: 'oncall 频道', intake_poll_seconds: '收件轮询间隔',
    intake_alert_dedupe_minutes: '告警去重窗口', intake_dm_queries: '私信发现查询', intake_mention_search: '搜索 @{{owner_name}}',
    intake_delegate_bot_names: '代本人发言的 app',
    intake_inbox_provider: '收件处 provider', intake_inbox_model: '收件处模型', intake_inbox_effort: '收件处 effort',
    intake_oncall_provider: 'oncall provider', intake_oncall_model: 'oncall 模型', intake_oncall_effort: 'oncall effort',
    intake_oncall_task: 'oncall 任务',
    ask_expire_after: '询问卡片过期时间',
    rules_enabled: '启用规则引擎', rules_proposals_enabled: '启用每周规则提议', routines_enabled: '启用定时任务',
    rules_typed_approval_max_age_hours: '亲手打的指示算批准的时限',
    blockers_enabled: '启用能力阻塞卡片', blockers_skill_proposals_enabled: '启用每周 skill 提议',
    cards_retire_buttons_on_reply: 'thread 回复后收起按钮', cards_thread_reply_decisions: '明确的文字回复直接算决定',
    cards_read_receipts: '消息已读表情', cards_open_poll_seconds: '待决卡片 thread 轮询间隔',
    cards_task_thread: '同一任务的卡片串成一条审批 thread', cards_retire_on: '哪些 thread 回复会收起按钮',
    cards_max_open_per_task: '一个任务同时最多几张等你处理的卡',
    tasks_max_wakes: '每个任务自己唤醒的次数上限', tasks_wake_window_hours: '唤醒次数的统计窗口',
    tasks_stale_card_days: '旧检查卡过期天数',
    dispatch_provider_wait_hours: '最多等 AI 恢复', dispatch_probe_enabled: '探测 AI 是否恢复',
    dispatch_probe_min_seconds: '最短探测间隔', dispatch_probe_max_seconds: '最长探测间隔', dispatch_unresolved_retries: '没处理完的重跑次数',
  };
  const ERROR_WHY = [
    [/^must_be_a_model_id$/, '必须是模型 ID（字母、数字、. _ : / @ -）'],
    [/^must_be_one_of_/, '只能选 low / medium / high / xhigh / max'],
    [/^must_be_an_integer_(\d+)_to_(\d+)$/, (m) => `必须是 ${m[1]} 到 ${m[2]} 的整数`],
    [/^must_be_0_or_(\d+)_to_(\d+)$/, (m) => `只能是 0（关），或 ${m[1]} 到 ${m[2]} 的整数`],
    [/^must_be_true_or_false$/, '只能是开或关'],
    [/^must_list_distinct_providers$/, '必须列出不重复的 codex / claude'],
    [/^must_be_a_non_empty_object$/, '至少保留一个类别'],
    [/^must_map_category_to_codex_or_claude$/, '类别名只能用小写字母开头的 a-z 0-9 _ -，并映射到 codex 或 claude'],
    [/^must_be_distinct_channel_names_or_ids$/, '每行一个频道：#名字 或 C… 频道 ID，不能重复'],
    [/^must_look_like_10h$/, '格式类似 10h、30m、2d'],
    [/^must_be_1h_to_7d$/, '必须在 1 小时到 7 天之间'],
    [/^must_be_shorter_than_expire_after$/, '必须早于过期时间'],
    [/^must_be_a_list_of_up_to_20_env_var_names$/, '只能填变量名（大写字母、数字、下划线），最多 20 个'],
    [/^must_be_up_to_5_distinct_single_line_queries$/, '最多 5 条、互不重复的单行查询'],
    [/^must_be_up_to_20_app_name_patterns$/, '最多 20 条，每条 1–60 个字符：字母、数字、* _ -'],
    [/^must_be_codex_or_claude$/, '只能选 codex 或 claude'],
    [/^must_differ_from_inbox_task$/, '不能和收件处任务相同'],
    [/^must_not_exceed_probe_max_seconds$/, '不能比最长探测间隔还长'],
  ];
  function explainError(code) {
    if (code === 'worker_config_changed_since_loaded_reload') return '配置文件在你打开页面之后被改过了（例如 config set）。请刷新页面再改。';
    if (code === 'invalid_page_token') return '页面令牌已过期，请刷新页面后再保存。';
    if (code === 'setting_not_editable_here') return '这一项不能在页面里修改。';
    if (code === 'same_origin_required') return '只接受本页面发出的保存请求。';
    const m = /^invalid_worker_config_(.+)$/.exec(code || '');
    if (m) {
      const rest = m[1];
      const field = Object.keys(ERROR_FIELD).sort((a, b) => b.length - a.length).find((f) => rest.startsWith(f + '_'));
      if (field) {
        const why = rest.slice(field.length + 1);
        for (const [re, text] of ERROR_WHY) {
          const mm = re.exec(why);
          if (mm) return `${ERROR_FIELD[field]}：${typeof text === 'function' ? text(mm) : text}`;
        }
        return `${ERROR_FIELD[field]}：${why}`;
      }
    }
    return '保存失败：' + (code || '未知错误');
  }

  function Settings() {
    let st = null, token = null;
    const form = h('form', { class: 'settings', id: 'settings-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); review(); } });
    const errBox = h('div', { class: 'err-box', id: 'settings-error', role: 'alert', hidden: true });
    const logCard = h('section', { class: 'ops-card', id: 'settings-log' });
    const info = h('p', { class: 'ops-note', id: 'settings-info' });
    const head = pageHead('设置', 'SETTINGS · 数字员工配置', null);
    head.firstChild.append(info);
    // a section index on the left (sticky), the form in one column on the right
    const index = h('nav', { class: 'set-index', 'aria-label': '设置分组' });
    clear(main).append(head, errBox, h('div', { class: 'set-layout' }, index, h('div', { class: 'set-col' }, form, logCard)));
    const dialog = document.getElementById('ops-confirm');
    const dialogBody = document.getElementById('ops-confirm-body');
    let pending = null;
    document.getElementById('ops-confirm-cancel').onclick = () => { pending = null; dialog.close(); };
    document.getElementById('ops-confirm-ok').onclick = () => { const p = pending; pending = null; dialog.close(); if (p) save(p, false); };

    const get = (cfg, key) => key.split('.').reduce((o, k) => (o && typeof o === 'object' ? o[k] : undefined), cfg);
    const inputs = {};

    function field(key, control, hint) {
      const dflt = get(st.defaults, key);
      const cur = get(st.config, key);
      // a row: the label, its config key as a caption and the hint on the left; the control on the right
      const lab = h('label', { class: 'set-field', 'data-key': key },
        h('span', { class: 'set-meta' },
          h('span', { class: 'set-label' }, FIELD_LABEL[key] || key,
            !same(cur, dflt) ? h('span', { class: 'pill', 'data-tone': 'warn' }, '≠ 默认') : null,
            h('span', { class: 'pill dirty', 'data-tone': 'bad', hidden: true }, '未保存')),
          h('code', { class: 'set-key' }, key),
          h('span', { class: 'set-hint' }, (hint ? hint + ' · ' : '') + '默认：' + (dflt === undefined ? '—' : showDefault(dflt)))),
        h('span', { class: 'set-control' }, control));
      return lab;
    }
    function text(key, attrs) {
      const el = h('input', { type: 'text', name: key, value: get(st.config, key), spellcheck: 'false', autocomplete: 'off', ...(attrs || {}) });
      inputs[key] = () => el.value.trim();
      return el;
    }
    function num(key, min, max) {
      const el = h('input', { type: 'number', name: key, value: get(st.config, key), min, max, step: 1 });
      inputs[key] = () => (el.value.trim() === '' || Number.isNaN(Number(el.value)) ? el.value : Number(el.value));
      return el;
    }
    function select(key, options) {
      const el = h('select', { name: key }, options.map((o) => h('option', { value: o, selected: get(st.config, key) === o }, o)));
      inputs[key] = () => el.value;
      return el;
    }
    function toggle(key, on, off) {
      const el = h('input', { type: 'checkbox', name: key, checked: !!get(st.config, key), role: 'switch' });
      const lbl = h('span', {}, el.checked ? on : off);
      el.addEventListener('change', () => { lbl.textContent = el.checked ? on : off; });
      inputs[key] = () => el.checked;
      return h('span', { class: 'switch' }, el, lbl);
    }
    function list(key, placeholder) {
      const el = h('textarea', { name: key, rows: 3, spellcheck: 'false', placeholder });
      el.value = (get(st.config, key) || []).join('\n');
      inputs[key] = () => el.value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
      return el;
    }
    function categories() {
      const box = h('div', { class: 'cat-box' });
      const rows = h('div', { class: 'cat-rows' });
      const providers = st.enums.providers;
      function addRow(name, prov) {
        const nameEl = h('input', { type: 'text', value: name || '', 'aria-label': '类别名', spellcheck: 'false', placeholder: '例如 infra' });
        const provEl = h('select', { 'aria-label': '映射到' }, providers.map((p) => h('option', { value: p, selected: p === prov }, PROVIDER[p] || p)));
        const row = h('div', { class: 'cat-row' }, nameEl, h('span', { 'aria-hidden': 'true' }, '→'), provEl,
          h('button', { type: 'button', class: 'btn sm ghost', 'aria-label': '删除类别', onclick: () => { row.remove(); markDirty(); } }, '删除'));
        rows.append(row);
      }
      for (const [k, v] of Object.entries(st.config.routing.categories)) addRow(k, v);
      box.append(rows, h('button', { type: 'button', class: 'btn sm', onclick: () => { addRow('', providers[0]); markDirty(); } }, '+ 添加类别'));
      inputs['routing.categories'] = () => {
        const out = {};
        for (const row of rows.querySelectorAll('.cat-row')) {
          const [n, p] = [row.querySelector('input').value.trim(), row.querySelector('select').value];
          if (n) out[n] = p;
        }
        return out;
      };
      return box;
    }
    function order() {
      const ol = h('ol', { class: 'order' });
      function render(items) {
        clear(ol).append(...items.map((p, i) => h('li', { 'data-provider': p }, h('span', { class: 'order-n' }, `${i + 1}.`), provChip(p),
          h('button', { type: 'button', class: 'btn sm ghost', disabled: i === 0, 'aria-label': '上移', onclick: () => { const a = items.slice(); [a[i - 1], a[i]] = [a[i], a[i - 1]]; render(a); markDirty(); } }, '↑'),
          h('button', { type: 'button', class: 'btn sm ghost', disabled: i === items.length - 1, 'aria-label': '下移', onclick: () => { const a = items.slice(); [a[i + 1], a[i]] = [a[i], a[i + 1]]; render(a); markDirty(); } }, '↓'))));
      }
      render(st.config.routing.fallback_order.slice());
      inputs['routing.fallback_order'] = () => [...ol.children].map((li) => li.dataset.provider);
      return ol;
    }

    function section(title, en, kids, note) {
      const id = 'set-' + en.toLowerCase().replace(/[^a-z]+/g, '-').replace(/^-|-$/g, '');
      return h('fieldset', { class: 'ops-card set-section', id }, h('legend', {}, title, h('small', {}, en)), note ? h('p', { class: 'note' }, note) : null, kids);
    }
    // the index lists the sections; the one at the top of the screen is marked, a section with unsaved changes has a dot
    function renderIndex() {
      const targets = [...form.querySelectorAll('.set-section'), form.querySelector('.set-readonly'), logCard].filter(Boolean);
      fill(index, targets.map((sec) => {
        const title = sec.matches('.set-section') ? sec.querySelector('legend').firstChild.textContent
          : sec === logCard ? '修改记录' : '其他（只读）';
        return h('a', { href: '#' + sec.id, 'data-target': sec.id, onclick: (ev) => {
          ev.preventDefault();
          if (sec.tagName === 'DETAILS') sec.open = true;
          sec.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
        } }, title);
      }));
      spy();
    }
    function spy() {
      const links = [...index.querySelectorAll('a')];
      const line = document.querySelector('.site-bar') ? document.querySelector('.site-bar').getBoundingClientRect().bottom + 40 : 100;
      let cur = links[0];
      for (const a of links) {
        const el = document.getElementById(a.dataset.target);
        if (el && el.getBoundingClientRect().top <= line) cur = a;
      }
      // at the bottom of the page the last section is the current one, even when it is too short to reach the top
      if (innerHeight + scrollY >= document.documentElement.scrollHeight - 4) cur = links[links.length - 1];
      for (const a of links) {
        if (a === cur) a.setAttribute('aria-current', 'true'); else a.removeAttribute('aria-current');
      }
    }
    let spyQueued = false;
    addEventListener('scroll', () => {
      if (spyQueued) return;
      spyQueued = true;
      requestAnimationFrame(() => { spyQueued = false; spy(); });
    }, { passive: true });
    function changes() {
      const out = [];
      for (const key of st.editable) {
        if (!inputs[key]) continue;
        const v = inputs[key]();
        if (!same(v, get(st.config, key))) out.push({ key, value: v });
      }
      return out;
    }
    function markDirty() {
      const dirty = new Set(changes().map((c) => c.key));
      for (const lab of form.querySelectorAll('.set-field')) {
        const d = lab.querySelector('.dirty');
        if (d) d.hidden = !dirty.has(lab.dataset.key);
      }
      saveBtn.disabled = !dirty.size;
      resetBtn.disabled = !dirty.size;
      // the save bar shows only while something is unsaved
      form.classList.toggle('is-dirty', dirty.size > 0);
      dirtyNote.textContent = dirty.size ? `${dirty.size} 项修改还没保存` : '';
      for (const a of index.querySelectorAll('a')) {
        const sec = document.getElementById(a.dataset.target);
        const n = sec && sec.matches('.set-section') ? [...sec.querySelectorAll('.set-field')].filter((f) => dirty.has(f.dataset.key)).length : 0;
        if (n) a.dataset.dirty = String(n); else delete a.dataset.dirty;
      }
    }
    const saveBtn = h('button', { type: 'submit', class: 'btn primary', id: 'settings-save', disabled: true }, '检查并保存…');
    const resetBtn = h('button', { type: 'button', class: 'btn ghost', id: 'settings-reset', disabled: true, onclick: () => render() }, '撤销修改');
    const dirtyNote = h('span', { class: 'set-dirty-note', 'aria-live': 'polite' });

    function render() {
      errBox.hidden = true;
      for (const k of Object.keys(inputs)) delete inputs[k];
      clear(form);
      const efforts = st.enums.efforts;
      form.append(
        section('模型与 effort', 'MODELS', [
          field('providers.claude.model_default', text('providers.claude.model_default')),
          field('providers.claude.model_important', text('providers.claude.model_important'), '设计或重要任务（--important）'),
          field('providers.claude.effort', select('providers.claude.effort', efforts)),
          field('providers.codex.model', text('providers.codex.model'), '通过 -m 传给 codex，不改 ~/.codex/config.toml'),
          field('providers.codex.effort', select('providers.codex.effort', efforts)),
        ], '每次启动后台运行时读取最新配置；已在运行的会话不受影响。'),
        section('路由', 'ROUTING', [
          field('routing.categories', categories(), '新任务按类别选 provider'),
          field('routing.fallback_order', order(), '基础设施类失败时按这个顺序换 provider'),
        ]),
        section('后台调度', 'DISPATCH', [
          field('dispatch.enabled', toggle('dispatch.enabled', '开（会话不在时后台唤醒）', '关（全部按原方式投递）')),
          field('dispatch.progress_updates', toggle('dispatch.progress_updates', '开（卡片每分钟最多刷新一次）', '关（只在开始和结束时更新卡片）')),
          field('dispatch.concurrency', num('dispatch.concurrency', 1, 20)),
          field('dispatch.max_event_age_hours', num('dispatch.max_event_age_hours', 1, 720), '更早的事件进“旧积压”，不会唤醒会话'),
          field('dispatch.claude_env_passthrough', list('dispatch.claude_env_passthrough', '每行一个变量名'), '只填变量名，值从登录 shell 读取，永远不会显示'),
        ]),
        section('熔断', 'BREAKER', [field('circuit_breaker.round_trips_per_hour', num('circuit_breaker.round_trips_per_hour', 1, 1000), '两个任务 1 小时内互相唤醒超过这个次数就暂停')]),
        section('收件', 'INTAKE', [
          field('intake.enabled', toggle('intake.enabled', '开（守护进程按节奏读私信、@{{owner_name}} 和订阅频道）', '关（不读 Slack，不产生新事件）')),
          field('intake.channels', list('intake.channels', '{{channel_example}}'), '每行一个频道 ID（C…）；{{channel_names}} 也能识别'),
          field('intake.oncall_channels', list('intake.oncall_channels', '{{oncall_channel_example}}'), '这些频道里的 bot 告警投给 {{owner_key}}-oncall'),
          field('intake.burst_seconds', num('intake.burst_seconds', 1, 3600), '同一人同一会话连发消息合并的滑动窗口'),
          field('intake.poll_seconds', num('intake.poll_seconds', 20, 3600), 'Slack 读取节奏；search.messages 是 Tier 2 限流'),
          field('intake.alert_dedupe_minutes', num('intake.alert_dedupe_minutes', 1, 1440), '同一服务 + 同一告警标题在这段时间内只产生一个事件'),
          field('intake.dm_queries', list('intake.dm_queries', 'to:me'), '发现私信 / 群私信的 search.messages 查询（用 intake-probe 验证）'),
          field('intake.mention_search', toggle('intake.mention_search', '开（搜索 @{{owner_name}}）', '关')),
          field('intake.delegate_bot_names', list('intake.delegate_bot_names', 'coder-*'), '名字匹配的 app 用某人自己的 token 发的消息算这个人本人，不当 bot 过滤；每行一个，可用 *；清空 = 关'),
          field('intake.inbox_provider', select('intake.inbox_provider', st.enums.providers), '收件处常驻任务的 provider'),
          field('intake.inbox_model', text('intake.inbox_model'), '换 provider 时记得一起换模型'),
          field('intake.inbox_effort', select('intake.inbox_effort', efforts)),
          field('intake.oncall_provider', select('intake.oncall_provider', st.enums.providers), 'oncall 常驻任务的 provider'),
          field('intake.oncall_model', text('intake.oncall_model')),
          field('intake.oncall_effort', select('intake.oncall_effort', efforts)),
        ], '修改后下一轮收件和下一次启动的收件处 / oncall 运行就会用新配置。'),
        section('询问卡片', 'ASK', [
          field('ask.remind_after', text('ask.remind_after', { placeholder: '10h' }), '格式 30m / 10h / 2d'),
          field('ask.expire_after', text('ask.expire_after', { placeholder: '24h' })),
        ]),
        section('规则与定时任务', 'RULES · ROUTINES', [
          field('rules.enabled', toggle('rules.enabled', '开（权限 / 账号类变更先过规则引擎）', '关（每条检查都交 {{owner_name}}）'), '紧急开关：关掉后 rule-check 一律给 {{owner_key}}'),
          field('rules.proposals_enabled', toggle('rules.proposals_enabled', '开（每周复盘可以发提议卡片）', '关（不发提议；已决定的卡片暂不生效）')),
          field('rules.typed_approval_max_age_hours', num('rules.typed_approval_max_age_hours', 0, 168), '你在卡片 thread 里亲手打的指示，这么多小时内可以直接算权限检查的批准，不再发第二张卡（做了不能撤回的一步仍要你点）；0 = 关，每次都发卡片'),
          field('routines.enabled', toggle('routines.enabled', '开（到点触发定时任务）', '关（所有定时任务暂停）')),
        ], '间隔、条数和次数上限只能在 config set 里收紧（硬上限：15 分钟、20 条、每天 48 次）。'),
        section('能力阻塞与缺口', 'BLOCKERS · GAPS', [
          field('blockers.enabled', toggle('blockers.enabled', '开（report-blocker 发卡片、“已完成”卡片和提醒）', '关（只记缺口，不发卡片）'), '紧急开关：关掉后 agent 需要时改用 ask-{{owner_key}}'),
          field('blockers.skill_proposals_enabled', toggle('blockers.skill_proposals_enabled', '开（每周复盘可以发 skill 提议卡片）', '关（不发提议；已决定的卡片暂不建任务）')),
        ], '提醒时间、每次提议条数和静默天数只能用 config set 修改。'),
        section('决策卡片与消息已读', 'CARDS · RECEIPTS', [
          field('cards.retire_buttons_on_reply', toggle('cards.retire_buttons_on_reply', '开（你在卡片 thread 里回复后，按下一项的规则收起按钮）', '关（按钮一直留着）')),
          field('cards.retire_on', select('cards.retire_on', ['plan_change', 'any_reply']), 'plan_change：只有明确的决定或改方案（改成 / 先别 / 取消…）才收起，提问时按钮留着；any_reply：任何回复都收起（P3.1 的老做法）'),
          field('cards.thread_reply_decisions', toggle('cards.thread_reply_decisions', '开（“批准 / 不行 / 选项名”开头的明确回复直接算决定）', '关（文字回复都由 agent 发确认卡）'), '紧急开关：关掉后只有按钮和确认卡能决定'),
          field('cards.read_receipts', toggle('cards.read_receipts', '开（消息进了任务上下文就加 {{receipt_emoji}}）', '关（不加表情）'), '私信用你的身份加，频道里用 bot'),
          field('cards.open_poll_seconds', num('cards.open_poll_seconds', 0, 900), '等你决定的卡片 thread 多久看一次（秒）；0 = 按普通节奏（1–15 分钟）'),
          field('cards.max_open_per_task', num('cards.max_open_per_task', 1, 20), '到上限后，这个任务的 agent 要先关掉或合并旧卡（--about）才能再发卡；接替旧卡的新卡和超过“旧检查卡过期天数”的卡不算，收件处和 oncall 不限（规则 R-0008）'),
          field('cards.task_thread', toggle('cards.task_thread', '开（同一任务的卡片串成一条 thread，从卡片建的任务继承父 thread）', '关（每张卡片各自占最外层，P3.2 前的样子）'), '紧急开关：关掉后恢复每张卡片独立发在频道最外层'),
        ], '只认你本人手打的回复；别人、代理人和 agent 的回复不会收起按钮，也不算决定。已读表情只加在进了任务上下文的真人消息上。'),
        section('任务自己唤醒', 'WAKE-UPS', [
          field('tasks.max_wakes', num('tasks.max_wakes', 0, 50), '窗口内超过这个次数，到点的唤醒改成发卡片问你；0 = 不自动唤醒，每次都发卡片'),
          field('tasks.wake_window_hours', num('tasks.wake_window_hours', 1, 720), '168 = 一周'),
          field('tasks.stale_card_days', num('tasks.stale_card_days', 0, 30), '旧的检查卡（没写要你做什么）这么多天没人点，就收起按钮、让 agent 自己接着跟进；0 = 不过期'),
        ], 'agent 用 task-deadline --wake 登记的是它自己的检查点：到点直接唤醒任务，不发卡片，也不带新授权。'),
        section('等 AI 恢复', 'PROVIDER WAIT', [
          field('dispatch.provider_wait_hours', num('dispatch.provider_wait_hours', 0, 168), '过了这么久还没恢复才发失败卡片；0 = 关（立刻发卡片，以前的做法）'),
          field('dispatch.probe_enabled', toggle('dispatch.probe_enabled', '开（定时探测 Claude / Codex 是否恢复）', '关（不探测，失败照旧发卡片）')),
          field('dispatch.probe_min_seconds', num('dispatch.probe_min_seconds', 30, 3600), '第一次探测前等多久；每失败一次间隔翻倍'),
          field('dispatch.probe_max_seconds', num('dispatch.probe_max_seconds', 60, 21600), '探测间隔最多翻倍到这么长'),
          field('dispatch.unresolved_retries', num('dispatch.unresolved_retries', 0, 5), 'provider 出过错后又有事件没处理完：恢复后最多再跑几次才发卡片；0 = 立刻发卡片'),
        ], 'Claude / Codex 返回 5xx、超时、限流或额度用完，重试和换 provider 都没用时，不发卡片给你：守护进程用一个极小的请求定时探测，恢复了就自动接着处理，期间的新消息都排着不会丢。'),
        readOnly(),
        h('div', { class: 'set-actions' }, dirtyNote, h('span', { class: 'set-buttons' }, resetBtn, saveBtn)));
      renderIndex();
      markDirty();
    }
    function readOnly() {
      const d = st.config.dispatch;
      const rows = [
        ['dispatch.liveness_cache_seconds', d.liveness_cache_seconds], ['dispatch.retry_backoff_seconds', d.retry_backoff_seconds],
        ['dispatch.partial_retries', d.partial_retries], ['dispatch.max_run_seconds', d.max_run_seconds],
        ['dispatch.unbound_watches', d.unbound_watches], ['dispatch.notify_cards', d.notify_cards],
        ['dispatch.failed_cooldown_seconds', d.failed_cooldown_seconds], ['dispatch.login_shell', d.login_shell],
        ['providers.claude.yolo_args', st.config.providers.claude.yolo_args], ['providers.codex.yolo_args', st.config.providers.codex.yolo_args],
        ['intake.inbox_task', st.config.intake.inbox_task], ['intake.oncall_task', st.config.intake.oncall_task],
        ['intake.search_max_pages', st.config.intake.search_max_pages], ['intake.max_api_calls_per_cycle', st.config.intake.max_api_calls_per_cycle],
        ['rules.proposal_suppress_days', st.config.rules.proposal_suppress_days], ['rules.max_proposals_per_run', st.config.rules.max_proposals_per_run],
        ['routines.min_interval_minutes', st.config.routines.min_interval_minutes], ['routines.max_per_task', st.config.routines.max_per_task],
        ['routines.max_runs_per_day', st.config.routines.max_runs_per_day], ['routines.scan_seconds', st.config.routines.scan_seconds],
        ['blockers.reminder_hours', (st.config.blockers || {}).reminder_hours],
        ['blockers.skill_proposal_suppress_days', (st.config.blockers || {}).skill_proposal_suppress_days],
        ['blockers.max_skill_proposals_per_run', (st.config.blockers || {}).max_skill_proposals_per_run],
      ];
      // a key with a name shows the name, its key as the caption
      return h('details', { class: 'ops-card set-readonly', id: 'set-readonly' }, h('summary', {}, '其他设置（只读，用 config set 修改）'),
        h('dl', { class: 'kv' }, rows.map(([k, v]) => [h('dt', {}, FIELD_LABEL[k] ? [FIELD_LABEL[k], h('code', { class: 'set-key' }, k)] : h('code', { class: 'set-key' }, k)),
          h('dd', { class: 'mono' }, show(v))])));
    }
    function renderLog(log) {
      clear(logCard).append(h('h3', {}, '修改记录 ', h('span', { class: 'count' }, log.length)),
        log.length ? h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['时间', '修改人', '设置', '旧值', '新值'].map((x) => h('th', { scope: 'col' }, x)))),
          h('tbody', {}, log.map((r) => h('tr', {},
            h('td', { class: 'num' }, when(r.ts)), h('td', {}, r.who === 'dashboard' ? '页面（dashboard）' : r.who || '—'),
            h('td', {}, FIELD_LABEL[r.key] || '', h('div', { class: 'sub mono' }, r.key)),
            h('td', { class: 'mono val' }, show(r.old)), h('td', { class: 'mono val' }, show(r.new))))))
          : h('p', { class: 'note' }, '还没有修改记录。'));
    }
    function renderInfo() {
      fill(info, '文件：', h('code', {}, st.path), st.source === 'default' ? '（还没有创建，保存时创建）' : null,
        ' · 保存前会先校验，再请你确认；修改人记为 dashboard。');
    }
    async function load() {
      try { st = await api('/api/settings'); online(true); } catch (e) { online(false); errBox.hidden = false; errBox.textContent = '读取设置失败。'; return; }
      token = st.page_token;
      renderInfo();
      renderLog(st.log || []);
      if (!st.ok) {
        errBox.hidden = false;
        errBox.textContent = `配置文件无效（${st.error}）。这里不会覆盖它：请先运行 slack_followup.py config validate 修复。`;
        clear(form);
        return;
      }
      render();
    }
    form.addEventListener('input', markDirty);
    form.addEventListener('change', markDirty);

    function review() {
      const list = changes();
      if (!list.length) { toast('没有改动'); return; }
      save(list, true);
    }
    async function save(list, dryRun) {
      errBox.hidden = true;
      saveBtn.disabled = true;
      let res;
      try {
        res = await api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Page-Token': token },
          body: JSON.stringify({ changes: list, digest: st.digest, dry_run: dryRun }) });
      } catch (e) {
        errBox.hidden = false;
        errBox.textContent = explainError(e.body && e.body.error);
        saveBtn.disabled = false;
        return;
      }
      token = res.page_token || token;
      if (dryRun) {
        if (!res.changes.length) { toast('没有实际改动'); saveBtn.disabled = false; return; }
        pending = list;
        clear(dialogBody).append(h('p', {}, `校验通过。将修改 ${res.changes.length} 项（修改人记为 dashboard）：`),
          h('table', { class: 'ops-table' }, h('thead', {}, h('tr', {}, ['设置', '旧值', '新值'].map((x) => h('th', { scope: 'col' }, x)))),
            h('tbody', {}, res.changes.map((c) => h('tr', {}, h('td', {}, FIELD_LABEL[c.key] || c.key, h('div', { class: 'sub mono' }, c.key)),
              h('td', { class: 'mono val' }, show(c.old)), h('td', { class: 'mono val' }, show(c.new)))))),
          h('p', { class: 'note' }, '保存后，下一次后台调度和下一次启动的运行就会用新配置。'));
        dialog.showModal();
        saveBtn.disabled = false;
        return;
      }
      toast(`已保存 ${res.changes.length} 项设置`, 'good');
      st.config = res.config || st.config;
      st.digest = res.digest;
      st.source = 'file';
      renderInfo();
      renderLog(res.log || []);
      render();
    }
    load();
  }

  // =====================================================================
  // Design (P39: 架构设计文档) — the generated review page of runbooks/architecture-design.md, read-only in a sandboxed
  // iframe: its scripts run in an opaque origin (no access to this page or the dashboard APIs) and its CSP blocks every
  // network request. Regenerating the file is enough: no dashboard restart, the page notices the new sha256.
  // =====================================================================
  const DESIGN_ERROR = { missing: '还没有生成', symlink_refused: '是符号链接，拒绝读取', not_regular_file: '不是普通文件，拒绝读取',
    too_large: '超过 16 MB 上限', unreadable: '读不了' };
  function fmtBytes(n) {
    if (n === null || n === undefined) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  }
  function Design() {
    const head = pageHead('设计', 'DESIGN · 架构设计文档',
      '数字员工的架构设计文档：真理源是任务 workspace 里的 runbooks/architecture-design.md，由 build_review_html.py 生成离线评审页'
      + '（mermaid 本地内联）。这里只读展示：文档在沙箱 iframe 里运行，读不到看板的数据，也不能发起任何网络请求。'
      + '文档更新后重新生成即可，看板不用重启。');
    const status = h('section', { class: 'ops-card', id: 'design-status' });
    const view = h('section', { class: 'ops-card', id: 'design-view' });
    clear(main).append(head, status, view);
    let frame = null;
    let shown = null;  // sha256 of the version in the iframe

    function loadFrame(sha) {
      shown = sha;
      // sandbox before src: scripts only, never same-origin, forms, popups or top navigation.
      frame = h('iframe', { sandbox: 'allow-scripts', referrerpolicy: 'no-referrer', class: 'design-frame',
        title: '架构设计文档', src: '/design/doc?v=' + encodeURIComponent(sha || '') });
      fill(view, frame);
    }
    function renderStatus(d) {
      const warns = [];
      if (d.exists && d.external_src_refs > 0) {
        warns.push(`文档引用了 ${d.external_src_refs} 个外部资源（src=http…，可能是用 --cdn 生成的）：这里禁止任何网络请求，`
          + '它们不会加载，图表可能渲染不出来。请用下面的命令（本地 mermaid）重新生成。');
      } else if (d.exists && d.external_refs > 0) {
        warns.push(`文档里有 ${d.external_refs} 个外部链接（href=http…）：只是链接，页面本身不联网；在框里点开可能打不开。`);
      }
      if (d.error && d.error !== 'missing') warns.push(`设计文档${DESIGN_ERROR[d.error] || d.error}（${d.error}）。`);
      const stale = d.exists && frame && shown !== d.sha256;
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, d.exists ? '已生成' : (DESIGN_ERROR[d.error] || '不可用')), h('span', {}, '文件')),
          h('div', { class: 'counter' }, h('b', {}, d.exists ? fmtBytes(d.size) : '—'), h('span', {}, '大小')),
          h('div', { class: 'counter' }, h('b', {}, d.exists ? when(d.mtime) : '—'), h('span', {}, '生成时间')),
          h('div', { class: 'counter' }, h('b', { class: 'mono' }, d.sha256 || '—'), h('span', {}, 'sha256（前 12 位）')),
          h('div', { class: 'counter' }, h('b', {}, d.exists ? d.external_refs : '—'), h('span', {}, '外部引用（src / href）'))),
        h('dl', { class: 'kv' },
          h('dt', {}, '文件'), h('dd', { class: 'mono' }, d.path),
          h('dt', {}, '展示'), h('dd', {}, pill('只读', 'good'), ' ', pill('沙箱 iframe · 仅脚本', 'good'), ' ',
            pill('禁止网络请求', 'good'), ' ', pill('不跟随符号链接', null), ' ', pill(`上限 ${fmtBytes(d.max_bytes)}`, null)),
          h('dt', {}, '打开'), h('dd', {}, d.exists ? h('a', { class: 'ext', href: d.doc_url, target: '_blank', rel: 'noopener noreferrer' },
            '单独打开 ↗') : '—')),
        stale ? h('div', { class: 'warn-box', role: 'status' }, '文档已经重新生成（',
          h('span', { class: 'mono' }, d.sha256), '）。 ',
          h('button', { type: 'button', class: 'btn sm', onclick: () => { loadFrame(d.sha256); renderStatus(d); } }, '重新加载')) : null,
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderMissing(d) {
      frame = null; shown = null;
      fill(view, h('h3', {}, '怎么生成'),
        h('p', { class: 'ops-note' }, '在终端里运行下面的命令（只用标准库；mermaid 从本地文件内联，不要用 --cdn）。'
          + '生成后刷新本页即可，看板不用重启。换文件位置：启动看板时加 --design-html PATH。'),
        h('pre', { class: 'json design-cmd' }, d.refresh_command || ''));
    }
    async function load() {
      let d;
      try { d = await api('/api/design'); online(true); } catch (e) { online(false); return; }
      if (!d.exists) renderMissing(d);
      else if (!frame) loadFrame(d.sha256);
      renderStatus(d);
    }
    every(15000, load);
  }

  // ---------- router ----------
  // body[data-path] lets the offline demo page (evidence/p48-trace/demo.html) render a route from a file:// URL
  const path = (document.body.dataset.path || location.pathname).replace(/\/+$/, '') || '/';
  const nav = path.startsWith('/runs') ? 'runs' : path.startsWith('/gaps') ? 'gaps' : path.slice(1);
  for (const a of document.querySelectorAll('[data-nav]')) {
    const on = a.dataset.nav === nav;
    a.setAttribute('aria-pressed', String(on));
    if (on) a.setAttribute('aria-current', 'page');
  }
  const m = /^\/runs\/([A-Za-z0-9-]{1,40})$/.exec(path);
  const g = /^\/gaps\/([a-z0-9]{1,40})$/.exec(path);
  if (m) { document.title = 'Kage · run ' + m[1]; RunDetail(m[1]); }
  else if (g) { document.title = 'Kage · 阻塞 ' + g[1]; GapDetail(g[1]); }
  else if (path === '/gaps') { document.title = 'Kage · 缺口'; Gaps(); }
  else if (path === '/runs') { document.title = 'Kage · 运行记录'; Runs(); }
  else if (path === '/backlog') { document.title = 'Kage · 旧积压'; Backlog(); }
  else if (path === '/intake') { document.title = 'Kage · 收件'; Intake(); }
  else if (path === '/rules') {
    const q = new URLSearchParams(location.search);
    const check = q.get('check');
    const prop = q.get('proposal');
    if (check && /^rc[0-9a-f]{14}$/.test(check)) { document.title = 'Kage · 检查 ' + check; RuleCheckDetail(check); }
    else if (prop && /^rp[0-9a-f]{14}$/.test(prop)) { document.title = 'Kage · 提议 ' + prop; RuleProposalDetail(prop); }
    else { document.title = 'Kage · 规则'; Rules(); }
  }
  else if (path === '/routines') { document.title = 'Kage · 定时任务'; Routines(); }
  else if (path === '/learning') { document.title = 'Kage · 学习'; Learning(); }
  else if (path === '/health') { document.title = 'Kage · 健康'; Health(); }
  else if (path === '/design') { document.title = 'Kage · 设计'; Design(); }
  else if (path === '/onboarding') { document.title = 'Kage · 上手'; Onboarding(); }
  else if (path === '/settings') { document.title = 'Kage · 设置'; Settings(); }
  else { clear(main).append(h('div', { class: 'empty' }, h('b', {}, '页面不存在'), h('a', { href: '/runs' }, '去运行记录'))); }
})();
