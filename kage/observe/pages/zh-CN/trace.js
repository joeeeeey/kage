/* Kage · 收件轨迹 (P48): the inbox list with each message's 8-stage pipeline, and the trace drawer that shows
   its whole chain (收到 → 过滤 / 合并 → 分诊 → 路由 → 派发 → 卡片 → 回复 → 结清). Vanilla JS, same-origin only, never parses HTML:
   data is always set as text; the icons are fixed inline SVG paths. Mounted by ops.js Intake(); the helpers come from there. */
'use strict';

(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const STAGES = [['received', '收到', 'RECEIVED'], ['filtered', '过滤 / 合并', 'FILTER'], ['triage', '分诊', 'TRIAGE'],
    ['route', '路由', 'ROUTE'], ['dispatch', '派发', 'DISPATCH'], ['cards', '卡片', 'CARDS'], ['replies', '回复', 'REPLIES'],
    ['resolved', '结清', 'RESOLVED']];
  const STAGE = Object.fromEntries(STAGES.map(([k, zh, en]) => [k, { zh, en }]));
  const ICONS = {
    received: ['M3 13h5l2 3h4l2-3h5', 'M5.5 5h13l2.5 8v5.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5V13z'],
    filtered: ['M3.5 5h17l-6.5 7.5V18l-4 2v-7.5z'],
    triage: ['M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13z', 'M20 20l-4.6-4.6', 'M7.5 10.5l2 2 3.5-3.5'],
    route: ['M6 3v18', 'M6 6h10l3 3-3 3H6', 'M6 14h-2'],
    dispatch: ['M6 4.5v15l13-7.5z'],
    cards: ['M3 6.5A1.5 1.5 0 0 1 4.5 5h15A1.5 1.5 0 0 1 21 6.5v11a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z', 'M3 10h18', 'M7 14.5h5'],
    replies: ['M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H9l-5 4z', 'M8 9h8', 'M8 12h5'],
    resolved: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'M8 12.2l2.8 2.8L16.5 9'],
    close: ['M6 6l12 12', 'M18 6L6 18'],
    link: ['M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1', 'M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1'],
    search: ['M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13z', 'M20 20l-4.6-4.6'],
    agent: ['M7 8h10a3 3 0 0 1 3 3v5a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3v-5a3 3 0 0 1 3-3z', 'M12 4v4', 'M9 13h.01', 'M15 13h.01'],
  };
  const STATUS = { done: '完成', active: '进行中', {{owner_key}}: '等 {{owner_name}}', failed: '失败', skip: '没到这一步' };
  const OVERALL = { done: ['已处理', 'done'], waiting: ['进行中', 'active'], {{owner_key}}: ['等你', '{{owner_key}}'], failed: ['失败', 'failed'] };
  const VIEWS = [['all', '全部', 'ALL'], ['{{owner_key}}', '等 {{owner_name}}', '{{OWNER_NAME}}'], ['failed', '失败 / 卡住', 'STUCK'],
    ['logged', '闲聊 / 只记录', 'LOGGED'], ['card_reply', '卡片回复', 'CARD'], ['agent', 'agent 消息', 'AGENT'],
    ['filtered', '已过滤 · 自己的', 'FILTERED']];
  const KIND = { burst: '收件', seen: '收件', card_reply: '卡片回复', local: 'agent', event: '监听', card: '卡片' };
  const OUTCOME = { filtered: '已过滤', logged: '只记录', replied: '已回复', carded: '有卡片', launched: '新任务',
    handed_off: '已转交', delivered: '已投递', needs_{{owner_key}}: '等你', failed: '失败', pending: '处理中' };
  const DAYS = [1, 3, 7, 14];
  const LIST_MS = 10000, LIVE_MS = 5000, IDLE_MS = 30000;

  function icon(name, size) {
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', String(size || 18));
    svg.setAttribute('height', String(size || 18));
    svg.setAttribute('class', 'ico tr-ico');
    svg.setAttribute('aria-hidden', 'true');
    for (const d of ICONS[name] || []) {
      const p = document.createElementNS(NS, 'path');
      p.setAttribute('d', d);
      svg.append(p);
    }
    return svg;
  }

  function mount(deps) {
    const { h, clear, fill, api, online, toast } = deps;
    const TZ = '{{tz_name}}';
    const fmtDay = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, month: '2-digit', day: '2-digit' });
    const fmtTime = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const fmtHM = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, hour: '2-digit', minute: '2-digit', hour12: false });
    const day = (ts) => ts ? fmtDay.format(new Date(ts * 1000)) : '';
    const hms = (ts) => ts ? fmtTime.format(new Date(ts * 1000)) : '—';
    const hm = (ts) => ts ? fmtHM.format(new Date(ts * 1000)) : '—';
    function span(sec) {
      sec = Math.max(0, Math.round(sec || 0));
      if (sec < 60) return `${sec} 秒`;
      const m = Math.floor(sec / 60);
      if (m < 60) return sec % 60 && m < 10 ? `${m} 分 ${sec % 60} 秒` : `${m} 分`;
      const hh = Math.floor(m / 60);
      if (hh < 48) return m % 60 ? `${hh} 小时 ${m % 60} 分` : `${hh} 小时`;
      return `${Math.floor(hh / 24)} 天`;
    }
    function ago(ts) {
      if (!ts) return '';
      const s = Date.now() / 1000 - ts;
      return s < 60 ? '刚刚' : s < 3600 ? `${Math.floor(s / 60)} 分钟前` : s < 172800 ? `${Math.floor(s / 3600)} 小时前` : `${Math.floor(s / 86400)} 天前`;
    }
    async function copy(text) {
      try { await navigator.clipboard.writeText(text); toast('已复制：' + text); } catch (e) { toast('复制失败，请手动复制：' + text, 'bad'); }
    }

    // ---------- URL state (deep link /intake?trace=<ref>) ----------
    const params = new URLSearchParams(location.search);
    const state = {
      view: VIEWS.some(([k]) => k === params.get('view')) ? params.get('view') : 'all',
      task: params.get('task') || '', days: DAYS.includes(Number(params.get('days'))) ? Number(params.get('days')) : 3,
      q: '', trace: params.get('trace') || '',
    };
    function url() {
      const q = new URLSearchParams();
      if (state.view !== 'all') q.set('view', state.view);
      if (state.task) q.set('task', state.task);
      if (state.days !== 3) q.set('days', String(state.days));
      if (state.trace) q.set('trace', state.trace);
      return location.pathname + (q.toString() ? '?' + q : '');
    }
    function navigate(push) {
      try { history[push ? 'pushState' : 'replaceState'](null, '', url()); } catch (e) { /* a file:// demo page */ }
    }

    // ---------- the list card ----------
    const section = h('section', { class: 'ops-card tr-card-list', id: 'intake-trace' });
    const total = h('span', { class: 'count' }, '0');
    const sync = h('span', { class: 'tr-sync', title: '每 10 秒刷新' }, h('i', { class: 'scope-led', 'aria-hidden': 'true' }), '实时');
    const chips = h('div', { class: 'scope tr-views', role: 'group', 'aria-label': '筛选' });
    const buttons = {};
    for (const [key, label, en] of VIEWS) {
      const b = h('button', { type: 'button', class: 'scope-btn', 'aria-pressed': String(state.view === key), 'data-view': key,
        onclick: () => { state.view = key; state.q = ''; input.value = ''; syncChips(); navigate(false); load(true); } },
        h('span', {}, label), h('small', {}, en), h('b', { class: 'count', 'data-count': key }, '0'));
      buttons[key] = b; chips.append(b);
    }
    const taskSel = h('select', { 'aria-label': '投给哪个任务', class: 'tr-select' }, h('option', { value: '' }, '投给：全部任务'));
    taskSel.addEventListener('change', () => { state.task = taskSel.value; navigate(false); load(true); });
    const daySel = h('select', { 'aria-label': '时间范围', class: 'tr-select tr-days' },
      DAYS.map((d) => h('option', { value: String(d) }, `近 ${d} 天`)));
    daySel.value = String(state.days);
    daySel.addEventListener('change', () => { state.days = Number(daySel.value); navigate(false); load(true); });
    const input = h('input', { type: 'search', class: 'tr-input', autocomplete: 'off', spellcheck: 'false',
      placeholder: '贴 Slack 链接、CHANNEL/TS、burst / 事件 / 任务 id，或输入名字', 'aria-label': '查找消息' });
    const form = h('form', { class: 'tr-search', role: 'search', onsubmit: (ev) => { ev.preventDefault(); search(); } },
      icon('search', 16), input, h('button', { type: 'submit', class: 'btn sm' }, '查找'));
    input.addEventListener('input', () => { if (!input.value.trim() && state.q) { state.q = ''; load(true); } });
    const legend = h('div', { class: 'tr-legend', 'aria-hidden': 'true' },
      ['done', 'active', '{{owner_key}}', 'failed', 'skip'].map((k) => h('span', { 'data-status': k }, h('i', {}), STATUS[k])));
    const searchNote = h('div', { class: 'tr-search-note', hidden: true });
    const list = h('ol', { class: 'tr-list', 'aria-label': '消息' });
    const empty = h('div', { class: 'empty', hidden: true });
    fill(section,
      h('header', { class: 'tr-head' },
        h('h3', {}, '消息去向 ', total, sync),
        h('p', { class: 'note' }, '每条消息从收到到结清的全过程：分诊判断、投给哪个任务、哪个会话在跑、开了什么卡片、回了什么。点一行看完整轨迹；地址可以直接分享。')),
      h('div', { class: 'tr-tools' }, chips, h('div', { class: 'tr-tools-2' }, taskSel, daySel, form)),
      legend, searchNote, list, empty);
    function syncChips() { for (const [k, b] of Object.entries(buttons)) b.setAttribute('aria-pressed', String(!state.q && state.view === k)); }

    function avatar(who) {
      if (who && who.kind === 'agent' && !who.id) return h('span', { class: 'tr-av tr-av-agent' }, icon('agent', 16));
      const name = (who && (who.name || who.id)) || '?';
      if (who && who.avatar && who.id) {
        const img = h('img', { class: 'tr-av', src: `/api/avatars/${encodeURIComponent(who.id)}`, alt: '', loading: 'lazy' });
        img.addEventListener('error', () => img.replaceWith(h('span', { class: 'tr-av' }, name.slice(0, 1))), { once: true });
        return img;
      }
      let hue = 0;
      for (const c of name) hue = (hue * 31 + c.codePointAt(0)) % 360;
      return h('span', { class: 'tr-av', style: `--h:${hue}` }, name.slice(0, 1));
    }
    function pipeline(stages, current, big) {
      const wrap = h('span', { class: 'tr-pipe' + (big ? ' big' : ''), role: 'img',
        'aria-label': STAGES.map(([k, zh], i) => `${zh}：${STATUS[stages[i]] || stages[i]}`).join('，') });
      STAGES.forEach(([k, zh], i) => {
        if (i) wrap.append(h('i', { class: 'tr-pl', 'data-status': stages[i] === 'skip' ? 'skip' : stages[i - 1] === 'skip' ? 'skip' : stages[i] }));
        wrap.append(h('i', { class: 'tr-pn' + (k === current ? ' cur' : ''), 'data-status': stages[i], title: `${zh}：${STATUS[stages[i]] || stages[i]}` }));
      });
      return wrap;
    }
    const known = new Set();
    let first = true;
    function row(x) {
      const [olabel, otone] = OVERALL[x.status] || [x.status, 'skip'];
      const status = x.stuck ? 'failed' : otone;
      const fresh = !first && !known.has(x.ref);
      known.add(x.ref);
      const who = x.who || {};
      const a = h('a', { class: 'tr-row' + (fresh ? ' tr-enter' : '') + (x.ref === state.trace ? ' sel' : ''),
        href: '/intake?trace=' + encodeURIComponent(x.ref), 'data-status': status, 'data-kind': x.kind,
        onclick: (ev) => { if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button) return; ev.preventDefault(); open(x.ref, true, a); } },
        h('span', { class: 'tr-stripe' }),
        h('span', { class: 'tr-when' }, h('b', {}, hm(x.at)), h('small', {}, day(x.at) + ' · ' + ago(x.at))),
        h('span', { class: 'tr-who' }, avatar(who),
          h('span', {}, h('b', {}, who.name || who.id || x.title || '—'),
            h('small', {}, h('em', { class: 'tr-kind', 'data-kind': x.kind }, KIND[x.kind] || x.kind), ' ', x.where || '',
              x.msg_count > 1 ? ` · ${x.msg_count} 条` : '', x.smoke ? ' · TEST' : ''))),
        pipeline(x.pipeline || [], x.current),
        h('span', { class: 'tr-text' },
          h('span', { class: 'tr-headline' }, x.headline || ''),
          h('span', { class: 'tr-tags' },
            h('em', { class: 'tr-state', 'data-status': status }, x.stuck ? '卡住' : olabel),
            ['needs_{{owner_key}}', 'failed', 'pending'].includes(x.outcome) ? null : h('em', { class: 'tr-outcome' }, OUTCOME[x.outcome] || x.outcome),
            (x.tasks || []).slice(0, 3).map((t) => h('em', { class: 'tr-task' }, t)))),
        h('span', { class: 'tr-go', 'aria-hidden': 'true' }, '›'));
      return h('li', {}, a);
    }
    let inflight = false;
    async function load(reset) {
      if (inflight && !reset) return;
      inflight = true;
      const q = new URLSearchParams({ days: String(state.days) });
      if (state.q) q.set('q', state.q);
      else { q.set('view', state.view); if (state.task) q.set('task', state.task); }
      let data;
      try { data = await api('/api/inbox?' + q); online(true); } catch (e) { online(false); inflight = false; return; } finally { inflight = false; }
      for (const [k, n] of Object.entries(data.counts || {})) {
        const el = chips.querySelector(`[data-count="${k}"]`);
        if (el) el.textContent = n;
      }
      const tasks = data.tasks || [];
      const want = [['', '投给：全部任务'], ...tasks.map((t) => [t.task_id, `${t.task_id}（${t.count}）`])];
      if (state.task && !tasks.some((t) => t.task_id === state.task)) want.push([state.task, state.task]);
      if (taskSel.options.length !== want.length || want.some(([v, l], i) => taskSel.options[i].value !== v || taskSel.options[i].textContent !== l)) {
        clear(taskSel).append(...want.map(([v, l]) => h('option', { value: v }, l)));
      }
      taskSel.value = state.task;
      syncChips();
      total.textContent = data.total || 0;
      if (state.q) {
        searchNote.hidden = false;
        fill(searchNote, data.total ? `「${state.q}」找到 ${data.total} 条` : `「${state.q}」没有找到（只查近 ${state.days} 天的列表和全部 id）`,
          ' ', h('button', { type: 'button', class: 'btn sm ghost', onclick: () => { state.q = ''; input.value = ''; load(true); } }, '清除'));
      } else searchNote.hidden = true;
      const items = data.items || [];
      fill(list, items.map(row));
      empty.hidden = items.length > 0;
      if (!items.length) fill(empty, h('b', {}, state.q ? '没有匹配的消息' : '这个筛选下没有消息'), state.q ? '换个写法试试：Slack 链接最准。' : '换个筛选或把时间放宽。');
      first = false;
      if (reset && state.q && items.length === 1 && !drawer.classList.contains('open')) open(items[0].ref, true);
    }
    function search() {
      state.q = input.value.trim();
      syncChips();
      load(true);
    }

    // ---------- the drawer ----------
    const scrim = h('div', { class: 'tr-scrim', hidden: true, onclick: () => close(true) });
    const titleEl = h('h2', { id: 'tr-title' });
    const metaEl = h('p', { class: 'tr-dmeta' });
    const slackBtn = h('a', { class: 'btn sm', target: '_blank', rel: 'noopener noreferrer', hidden: true }, 'Slack ↗');
    const linkBtn = h('button', { type: 'button', class: 'btn sm ghost', onclick: () => copy(location.origin + '/intake?trace=' + encodeURIComponent(current)) },
      icon('link', 14), ' 复制链接');
    const closeBtn = h('button', { type: 'button', class: 'tr-x', 'aria-label': '关闭', onclick: () => close(true) }, icon('close', 18));
    const body = h('div', { class: 'tr-dbody' });
    const drawer = h('aside', { class: 'tr-drawer', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'tr-title', hidden: true },
      h('header', { class: 'tr-dhead' },
        h('div', { class: 'tr-dtitle' }, h('p', { class: 'eyebrow' }, 'TRACE · 消息轨迹'), titleEl, metaEl),
        h('div', { class: 'tr-dactions' }, slackBtn, linkBtn, closeBtn)),
      body);
    document.body.append(scrim, drawer);
    document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && drawer.classList.contains('open')) close(true); });
    window.addEventListener('popstate', () => {
      const ref = new URLSearchParams(location.search).get('trace') || '';
      state.trace = ref;
      if (ref) open(ref, false); else close(false);
    });

    let current = '';
    let opener = null;
    let timer = null;
    let shown = new Set();
    function open(ref, push, from) {
      if (from) opener = from;
      const again = ref === current && drawer.classList.contains('open');
      current = ref; state.trace = ref;
      if (push) navigate(true);
      for (const r of list.querySelectorAll('.tr-row.sel')) r.classList.remove('sel');
      for (const r of list.querySelectorAll('.tr-row')) if (r.getAttribute('href') === '/intake?trace=' + encodeURIComponent(ref)) r.classList.add('sel');
      if (!again) {
        shown = new Set();
        fill(body, h('div', { class: 'tr-loading' }, h('i', { class: 'scope-led' }), '读取轨迹…'));
        fill(titleEl, '…'); fill(metaEl, ref);
      }
      scrim.hidden = false; drawer.hidden = false;
      document.body.classList.add('tr-locked');
      requestAnimationFrame(() => { scrim.classList.add('open'); drawer.classList.add('open'); });
      if (!again) closeBtn.focus({ preventScroll: true });
      refresh(!again);
    }
    function close(push) {
      if (!drawer.classList.contains('open') && drawer.hidden) return;
      clearTimeout(timer); timer = null;
      current = ''; state.trace = '';
      if (push) navigate(true);
      scrim.classList.remove('open'); drawer.classList.remove('open');
      document.body.classList.remove('tr-locked');
      for (const r of list.querySelectorAll('.tr-row.sel')) r.classList.remove('sel');
      setTimeout(() => { if (!drawer.classList.contains('open')) { scrim.hidden = true; drawer.hidden = true; } }, 400);
      if (opener && document.contains(opener)) opener.focus({ preventScroll: true });
    }
    async function refresh(firstLoad) {
      clearTimeout(timer);
      const ref = current;
      if (!ref) return;
      let data;
      try {
        data = await api('/api/trace?ref=' + encodeURIComponent(ref));
        online(true);
      } catch (e) {
        if (ref !== current) return;
        if (e.status === 404 || e.status === 400) {
          fill(titleEl, '没有找到'); fill(metaEl, ref);
          fill(body, h('div', { class: 'empty' }, h('b', {}, e.status === 400 ? '无法识别这个引用' : '没有这条消息的记录'),
            '贴 Slack 链接、CHANNEL/TS、burst / 事件 / 派发 / 运行 id 或任务 id。'));
          return;
        }
        online(false);
        timer = setTimeout(() => refresh(false), IDLE_MS);
        return;
      }
      if (ref !== current) return;
      render(data, firstLoad);
      if (!document.hidden || data.trace.live) timer = setTimeout(() => refresh(false), data.trace.live ? LIVE_MS : IDLE_MS);
    }

    function factValue(f) {
      const [, value, flag] = f;
      if (flag === 'copy') {
        return h('span', { class: 'tr-cmd' }, h('code', {}, value),
          h('button', { type: 'button', class: 'btn sm cp', onclick: () => copy(value) }, '复制'));
      }
      return h('span', { class: flag === 'mono' ? 'mono' : null }, String(value));
    }
    function linkChip(l) {
      if (!l || !l.href) return null;
      if (l.kind === 'trace' || l.href.startsWith('/intake?trace=')) {
        const ref = decodeURIComponent(l.href.split('trace=')[1] || '');
        return h('a', { class: 'tr-link', 'data-kind': 'trace', href: l.href, onclick: (ev) => { ev.preventDefault(); open(ref, true); } }, l.label + ' →');
      }
      const external = l.kind === 'slack' || /^https:/.test(l.href);
      return h('a', { class: 'tr-link', 'data-kind': l.kind || 'page', href: l.href,
        target: external ? '_blank' : null, rel: external ? 'noopener noreferrer' : null }, l.label + (external ? ' ↗' : ' →'));
    }
    const stepKey = (s) => `${s.stage}|${s.at}|${s.title}`;
    function render(data, firstLoad) {
      const t = data.trace;
      const who = t.who || {};
      const scroll = body.scrollTop;
      fill(titleEl, `${who.name || who.id || t.title || '消息'}`, h('span', { class: 'tr-dwhere' }, ' · ' + (t.where || '')));
      fill(metaEl, `${day(t.at)} ${hms(t.at)} · ${KIND[t.kind] || t.kind} · `, h('code', {}, t.ref || ''),
        t.msg_count > 1 ? ` · ${t.msg_count} 条` : '');
      slackBtn.hidden = !t.slack_url;
      if (t.slack_url) slackBtn.href = t.slack_url;
      const [olabel, otone] = OVERALL[t.status] || [t.status, 'skip'];
      const status = t.stuck ? 'failed' : otone;
      drawer.dataset.status = status;
      const flight = h('nav', { class: 'tr-flight', 'aria-label': '八个阶段' }, t.stages.map((s, i) => {
        const prev = i ? t.stages[i - 1].status : null;
        return h('button', { type: 'button', class: 'tr-fn' + (s.key === t.current ? ' cur' : ''), 'data-status': s.status,
          'data-prev': prev && (prev === 'skip' || s.status === 'skip') ? 'skip' : prev || null, title: `${s.label}：${s.summary}`,
          onclick: () => { const el = body.querySelector(`[data-stage="${s.key}"]`); if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); flash(el); } } },
          h('span', { class: 'tr-fi' }, icon(s.key, 18)),
          h('b', {}, s.label), h('small', {}, STAGE[s.key].en),
          h('em', {}, s.status === 'skip' ? '—' : (s.at ? hm(s.at) : STATUS[s.status])));
      }));
      const steps = t.steps || [];
      let prevAt = t.at || null;
      let prevDay = day(t.at);
      const timeline = h('ol', { class: 'tr-timeline' }, steps.map((s, i) => {
        const key = stepKey(s);
        const isNew = !shown.has(key);
        shown.add(key);
        const gap = s.at && prevAt ? s.at - prevAt : null;
        const d = day(s.at);
        const showDay = s.at && d !== prevDay;
        if (s.at) { prevAt = s.at; prevDay = d; }
        const next = steps[i + 1];
        return h('li', { class: 'tr-step' + (firstLoad ? ' tr-in' : isNew ? ' tr-in tr-new' : ''), 'data-stage': s.stage, 'data-status': s.status,
          'data-next': next ? next.status : null, style: `--i:${firstLoad ? Math.min(i, 14) : 0}` },
          h('div', { class: 'tr-time' }, showDay ? h('small', { class: 'tr-day' }, d) : null, h('b', {}, s.at ? hms(s.at) : '时间未知'),
            gap !== null && gap >= 0.5 ? h('span', { class: 'tr-gap' }, '+' + span(gap)) : null),
          h('div', { class: 'tr-node', title: STATUS[s.status] }, icon(s.stage, 18)),
          h('div', { class: 'tr-sc' },
            h('div', { class: 'tr-sc-head' }, h('span', { class: 'tr-stage' }, STAGE[s.stage].zh, h('small', {}, STAGE[s.stage].en)),
              h('em', { class: 'tr-state', 'data-status': s.status }, STATUS[s.status])),
            h('h4', {}, s.title),
            s.facts && s.facts.length ? h('dl', { class: 'kv tr-facts' }, s.facts.map((f) => [h('dt', {}, f[0]), h('dd', {}, factValue(f))])) : null,
            s.lines && s.lines.length ? h('blockquote', { class: 'tr-lines', 'aria-label': '运行日志里的摘要（已脱敏）' },
              s.lines.map((l) => h('p', {}, l))) : null,
            s.links && s.links.length ? h('div', { class: 'tr-links' }, s.links.map(linkChip)) : null));
      }));
      const skipped = t.stages.filter((s) => !s.count);
      fill(body,
        h('div', { class: 'tr-sum' + (firstLoad ? ' tr-in' : ''), 'data-status': status },
          h('div', { class: 'tr-sum-line' },
            h('em', { class: 'tr-state big', 'data-status': status }, t.stuck ? '卡住' : olabel),
            t.live ? h('span', { class: 'tr-sync' }, h('i', { class: 'scope-led', 'aria-hidden': 'true' }), '实时更新中') : null,
            h('span', { class: 'tr-now' }, '现在在：', h('b', {}, t.current_label))),
          h('p', { class: 'tr-headline big' }, t.headline)),
        flight,
        h('h3', { class: 'tr-sec' }, '经过 ', h('span', { class: 'count' }, steps.length), h('small', {}, '按时间，间隔是和上一步相比')),
        timeline,
        skipped.length ? h('section', { class: 'tr-skipped' }, h('h3', { class: 'tr-sec' }, '没有到的阶段'),
          h('ul', {}, skipped.map((s) => h('li', { 'data-status': s.status, 'data-stage': s.key },
            h('span', { class: 'tr-node mini' }, icon(s.key, 14)), h('b', {}, s.label), h('span', {}, s.reason || STATUS[s.status]))))) : null,
        (data.matches || []).length > 1 ? h('section', { class: 'tr-matches' }, h('h3', { class: 'tr-sec' }, `另外 ${data.matches.length - 1} 条也匹配`),
          h('ul', {}, data.matches.slice(1, 12).map((m) => h('li', {}, h('a', { href: '/intake?trace=' + encodeURIComponent(m.ref),
            onclick: (ev) => { ev.preventDefault(); open(m.ref, true); } }, `${hm(m.at)} ${day(m.at)} · ${(m.who || {}).name || m.title || ''} · ${m.headline}`))))) : null,
        h('footer', { class: 'tr-foot' }, '只读：本机 SQLite 的元数据，不调 Slack、不存正文；摘要行来自本机运行日志，已脱敏。命令行：',
          h('code', {}, `trace_chain.py ${t.ref}`)));
      body.scrollTop = firstLoad ? 0 : scroll;
      if (firstLoad && t.focus) {
        const el = body.querySelector(`.tr-step[data-stage="${t.focus}"]`);
        if (el) setTimeout(() => { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); flash(el); }, 350);
      }
    }
    function flash(el) {
      el.classList.remove('tr-flash');
      void el.offsetWidth;
      el.classList.add('tr-flash');
    }

    // ---------- loops ----------
    let listTimer = null;
    const tick = () => { if (!document.hidden) load(false); listTimer = setTimeout(tick, LIST_MS); };
    load(true);
    listTimer = setTimeout(tick, LIST_MS);
    if (state.trace) open(state.trace, false);
    return { section, open, close };
  }

  window.{{trace_global}} = { mount, STAGES, icon };
})();
