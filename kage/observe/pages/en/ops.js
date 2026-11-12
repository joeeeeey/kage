/* Kage · digital worker runs page / Backlog / Settings (P1b) / Intake (P2; P48 intake trace: trace.js) / Rules · Routines (P3) / Gaps (P3.1) / Health (P38) / Design (P39). Vanilla JS, same-origin only, no innerHTML for data. */
'use strict';

(() => {
  const main = document.getElementById('ops-main');
  const chip = document.getElementById('ops-chip');
  const chipLabel = document.getElementById('ops-chip-label');
  const syncLabel = document.getElementById('ops-sync');
  const toasts = document.getElementById('toasts');
  const TZ = '{{tz_name}}';

  const PROVIDER = { codex: 'Codex', claude: 'Claude', grok: 'Grok' };
  const RUN_STATE = { running: 'Running', ended: 'Ended' };
  const OUTCOME = {
    success: ['Succeeded', 'good'], failed: ['Failed', 'bad'], partial: ['Partly handled', 'warn'],
    unresolved: ['Unfinished', 'bad'], stopped: ['Stopped', 'warn'],
  };
  const DISPATCH_STATE = {
    waiting: ['Waiting to start', 'warn'], running: ['Running', 'live'], paused: ['Paused', 'warn'], failed: ['Failed · needs you', 'bad'],
    needs_{{owner_key}}: ['Waiting for your answer', 'warn'], done: ['Done', 'good'], superseded: ['Merged', ''],
  };
  const ERROR_LABEL = {
    transient: 'Transient fault (5xx / timeout / network)', rate_limited: 'Rate limited (429)', quota: 'Quota used up', auth: 'Auth failed (401/403)',
    model_unsupported: 'Model unavailable (relay 404)', session_missing: 'Session to resume not found', session_live: 'Session still running elsewhere',
    unknown: 'Unknown error', task_unresolved: 'Events not fully handled', run_timeout: 'Run timed out', spawn_failed: 'Could not start the CLI',
    stopped: 'Stopped', crashed: 'Process crashed', provider_degraded: 'Provider overloaded; switched to a healthy one',
    ttft_timeout: 'No first token in time; next provider', error_grace: 'API errors did not recover in time; next provider',
  };
  const TRIGGER = {
    slack_message: 'Slack message', decision_answer: 'Decision card reply', ask_answer: 'Ask card reply', agent_message: 'Agent message',
    note: 'Task note', routine: 'Routine', launch: 'Task launch', transient_retry: 'Auto retry', partial_retry: 'Partial retry',
    fallback: 'Provider switch', {{owner_key}}_retry: '{{owner_name}} clicked Retry', event: 'Event',
  };
  const EVENT_STATUS = {
    handled: ['Handled', 'good'], dispatched: ['Processing in background', null], pending: ['Awaiting delivery', 'warn'], queued: ['Queued (awaiting session ack)', 'warn'],
    claimed: ['Claimed (awaiting completion)', 'warn'], delivery_unknown: ['Delivery result unknown', 'bad'], sending: ['Sending', 'warn'],
  };
  const statusPill = (st) => { const [label, tone] = EVENT_STATUS[st] || [st, null]; return pill(label, tone); };
  const KIND_TONE = { 'command': 'cmd', 'exit': 'exit', 'edit': 'edit', 'tool': 'tool', 'error': 'error', 'retry': 'retry',
    'result': 'result', 'says': 'say', 'start': 'start', 'end': 'end', 'todo': 'todo', 'note': 'note', 'output': 'raw' };  // the run log's kind labels: kage/locale/en.json runlog.label.*

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
  const fmtDT = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const fmtClockF = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const fmtHM = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, hour: '2-digit', minute: '2-digit', hour12: false });
  const fmtDayKey = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' });
  const fmtWeekday = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, weekday: 'short' });
  const when = (ts) => ts ? fmtDT.format(new Date(ts * 1000)) : '—';
  const clock = (ts) => ts ? fmtClockF.format(new Date(ts * 1000)) : '—';
  const hm = (ts) => ts ? fmtHM.format(new Date(ts * 1000)) : '—';
  // the owner's calendar day of a time: its key (YYYY-MM-DD), and a heading for a list grouped by day
  const dayKey = (ts) => fmtDayKey.format(new Date(ts * 1000));
  function dayLabel(ts) {
    const key = dayKey(ts), now = Date.now() / 1000;
    const date = `${key.slice(5)} ${fmtWeekday.format(new Date(ts * 1000))}`;
    if (key === dayKey(now)) return ['Today', date];
    if (key === dayKey(now - 86400)) return ['Yesterday', date];
    return [date, null];
  }
  function dur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
    const hh = Math.floor(m / 60);
    if (hh < 48) return `${hh}h ${m % 60}m`;
    return `${Math.floor(hh / 24)}d ${hh % 24}h`;
  }
  const short = (s, n = 8) => (s || '').slice(0, n);
  function outcomePill(run) {
    if (run.state === 'running') return pill('Running', null, true);
    const [label, tone] = OUTCOME[run.outcome] || [run.outcome || 'Ended', null];
    return pill(label, tone);
  }
  function stable(v) {
    if (Array.isArray(v)) return '[' + v.map(stable).join(',') + ']';
    if (v && typeof v === 'object') return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + stable(v[k])).join(',') + '}';
    return JSON.stringify(v);
  }
  const same = (a, b) => stable(a) === stable(b);
  const show = (v) => typeof v === 'string' ? v : JSON.stringify(v);
  const showDefault = (v) => Array.isArray(v) ? v.join(', ') || '(empty)'
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
    chipLabel.textContent = label || (ok ? 'Connected locally' : 'Read failed');
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
    const filterBox = h('div', { class: 'scope', role: 'group', 'aria-label': 'Filter' });
    const taskSel = h('select', { 'aria-label': 'Filter by task', id: 'task-filter' }, h('option', { value: '' }, 'All tasks'));
    const queue = h('section', { class: 'ops-card', id: 'dispatch-queue', hidden: true });
    const list = h('section', { class: 'ops-card', id: 'run-list' });
    const filters = [['all', 'All', 'ALL'], ['running', 'Running', 'RUNNING'], ['failed', 'Failed', 'FAILED'], ['today', 'Today', 'TODAY']];
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
      pageHead('Runs', 'RUNS · background sessions', 'Each time the digital worker handles events in the background, that is one run. Logs are redacted and read only on this machine; to stop or resume, use the buttons on the Slack card.',
        h('div', { class: 'ops-filters' }, filterBox, h('label', { class: 'field compact' }, h('span', { class: 'sr' }, 'Task'), taskSel))),
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
        clear(taskSel).append(h('option', { value: '' }, 'All tasks'), ...tasks.map((t) => h('option', { value: t }, t)));
        if (state.task && !tasks.includes(state.task)) taskSel.append(h('option', { value: state.task }, state.task));
      }
      taskSel.value = current;
      renderQueue(data.dispatches || []);
      renderRuns(data);
    }

    function renderQueue(items) {
      queue.hidden = !items.length;
      if (!items.length) return;
      clear(queue).append(h('h3', {}, 'Dispatch queue ', h('span', { class: 'count' }, items.length)),
        h('p', { class: 'note' }, 'Dispatches not finished yet: waiting to start, paused, failed and waiting on you, or waiting for your answer.'),
        h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['Task', 'Status', 'Pending events', 'Next', 'Reason', 'Card'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((d) => {
            const [label, tone] = DISPATCH_STATE[d.state] || [d.state, null];
            return h('tr', {},
              h('td', { class: 'mono' }, d.task),
              h('td', {}, pill(label, tone === 'live' ? null : tone, tone === 'live')),
              h('td', { class: 'num' }, d.held_events),
              h('td', {}, d.next_provider ? `${PROVIDER[d.next_provider] || d.next_provider} · ${d.next_mode === 'resume' ? 'Resume session' : 'New session'}` : '—'),
              h('td', {}, ERROR_LABEL[d.error_class] || d.error_class || '—'),
              h('td', { class: 'links' }, ext(d.card_url, 'Run card ↗'), ext(d.fail_card_url, 'Failure card ↗')));
          }))));
    }

    function renderRuns(data) {
      const runs = data.runs || [];
      clear(list);
      if (data.notice === 'state_database_missing' || data.notice === 'dispatcher_tables_missing') {
        list.append(h('div', { class: 'empty' }, h('b', {}, 'No dispatch data yet'), 'The daemon has not created any run records yet.'));
        return;
      }
      if (!runs.length) {
        list.append(h('div', { class: 'empty' }, h('b', {}, 'No matching runs'), state.filter === 'running' ? 'No session is running in the background right now.' : 'Try another filter.'));
        return;
      }
      const COLS = ['Status', 'Task', 'Execution', 'Trigger', 'Started', 'Duration', 'Steps', 'Events', 'Latest step', ''];
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
          const [label, date] = r.started_at ? dayLabel(r.started_at) : ['Time unknown', null];
          const n = perDay[key];
          rows.push(h('tr', { class: 'day-sep' }, h('th', { scope: 'rowgroup', colspan: COLS.length },
            h('span', {}, label), date ? h('small', {}, date) : null, h('small', { class: 'day-n' }, `${n} run(s)`))));
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
          h('span', { class: 'tag', title: r.mode === 'resume' ? 'Resume session ' + (r.session || '') : 'New session' }, r.mode === 'resume' ? 'Resume' : 'New'))),
        h('td', { class: 'nowrap meta trig' }, TRIGGER[r.trigger] || r.trigger || '—', r.attempt > 1 ? h('div', { class: 'sub' }, `attempt ${r.attempt}`) : null),
        h('td', { class: 'num meta', title: when(r.started_at) }, hm(r.started_at)),
        h('td', { class: 'num meta' }, dur(r.elapsed_seconds)),
        h('td', { class: 'num meta counted' }, r.steps || '—'),
        h('td', { class: 'num meta counted' }, `${r.handled}/${r.events}`),
        h('td', { class: 'summary' }, r.last_summary || (r.has_log ? '—' : '(no log)')),
        h('td', { class: 'links' }, h('a', { href }, 'Details'), ext(r.card_url, 'Card ↗'))));
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
    const [head, headStatus] = detailHead('run ' + runId, 'RUN · run details', null);
    const summary = h('section', { class: 'ops-card summary', id: 'run-summary' });
    const facts = h('section', { class: 'ops-card', id: 'run-facts' });
    const events = h('section', { class: 'ops-card', id: 'run-events' });
    const logList = h('ol', { class: 'log', id: 'run-log', 'aria-label': 'Readable log (redacted)', tabindex: '0' });
    const follow = h('input', { type: 'checkbox', id: 'log-follow', checked: true });
    const live = h('span', { id: 'log-live' });
    const count = h('span', { class: 'count', id: 'log-count' }, '0');
    const source = h('span', { class: 'note', id: 'log-source' });
    const copyBtn = h('button', { type: 'button', class: 'btn sm', onclick: () => copy(`{{tail_command}} ${runId}`) }, 'Copy {{tail_command}} command');
    const logCard = h('section', { class: 'ops-card log-card' },
      h('div', { class: 'log-bar' }, h('h3', {}, 'Readable log ', count), live, source,
        h('label', { class: 'toggle' }, follow, h('span', {}, 'Follow latest')), copyBtn),
      h('div', { class: 'log-wrap' }, logList));
    clear(main).append(
      h('nav', { class: 'crumbs' }, h('a', { href: '/runs' }, '← Runs')), head, summary,
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
        source.textContent = data.source === 'rendered' ? '(older run: rendered on the fly from raw output)' : data.source === 'missing' ? '(no log directory)' : '';
        clear(live).append(data.complete ? pill('Ended', 'good') : pill('Live', null, true));
        if (data.complete) {
          if (done) return true;
          done = true;  // one more read after the end, then stop
        }
      } catch (e) {
        online(false);
        if (e.status === 404) { clear(logList).append(h('li', { 'data-kind': 'error' }, h('span', {}, 'Run not found.'))); return true; }
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
        ['Task', d.task, 'mono'],
        ['Execution', h('span', { class: 'exec-line' }, provChip(d.provider), model ? h('span', { class: 'exec-model', title: model }, model) : null)],
        ['Mode', d.mode === 'resume' ? 'Resume session' : 'New session'],
        ['Trigger', (TRIGGER[d.trigger] || d.trigger || '—') + (d.attempt > 1 ? ` · attempt ${d.attempt}` : '')],
        ['Started', when(d.started_at)],
        ['Duration', dur(d.elapsed_seconds)],
        ['Steps', d.steps ? String(d.steps) : '—'],
        ['Events', `${d.handled} handled / ${d.events} total`],
      ]));
      fill(facts, h('h3', {}, 'Run info'), kv([
        ['Session', d.session, 'mono'],
        ['Ended', d.ended_at ? when(d.ended_at) : (d.state === 'running' ? 'Still running' : '—')],
        ['Exit code', d.exit ? String(d.exit.exit_code) + (d.exit.stopped ? ' (stopped)' : '') : '—'],
        ['Working directory', d.cwd, 'mono'],
        d.env_injected && d.env_injected.length ? ['Injected variable names', d.env_injected.join(', '), 'mono'] : null,
        ['Slack', h('span', {}, ext(d.card_url, 'Run card ↗') || '—', ' ', ext(d.fail_card_url, 'Failure card ↗'))],
        d.dispatch ? ['Dispatch', h('span', {}, `${(DISPATCH_STATE[d.dispatch.state] || [d.dispatch.state])[0]} · `,
          ...d.dispatch.runs.map((r, i) => [i ? ' · ' : '', r === d.run_id ? h('b', {}, short(r.split('-').pop(), 6)) : h('a', { href: '/runs/' + r }, short(r.split('-').pop(), 6))]))] : null,
        d.progress ? ['Progress line', d.progress.stage ? `${d.progress.stage}${d.progress.posted_at ? ' · ' + clock(d.progress.posted_at) : ''}` : '—'] : null,
      ]));
      fill(events, h('h3', {}, 'Events ', h('span', { class: 'count' }, (d.event_list || []).length)),
        (d.event_list || []).length ? h('ul', { class: 'ev-list' }, d.event_list.map((e) => h('li', {},
          h('code', {}, short(e.id, 10)), ' ', statusPill(e.status),
          e.kind ? h('span', { class: 'sub' }, ' ' + (TRIGGER[e.kind] || e.kind)) : null, ' ', ext(e.slack_url, 'Original message ↗')))) : h('p', { class: 'note' }, 'This run had no events (for example, a task launch).'),
        (d.actions || []).length ? h('details', { class: 'hist' }, h('summary', {}, `Card button log (${d.actions.length})`),
          h('ul', { class: 'ev-list' }, d.actions.map((a) => h('li', {}, `${when(a.at)} · ${a.action} · ${a.result || ''}`)))) : null);
    }
    async function copy(text) {
      try { await navigator.clipboard.writeText(text); toast('Copied: ' + text); } catch (e) { toast('Copy failed; type it manually: ' + text, 'bad'); }
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
    const head = pageHead('Backlog', 'BACKLOG · read-only', null);
    const body = h('section', { class: 'ops-card', id: 'backlog' });
    clear(main).append(head, body);
    async function load() {
      let data;
      try { data = await api('/api/backlog'); online(true); } catch (e) { online(false); return; }
      const hours = data.max_event_age_hours;
      clear(head.querySelector('div')).append(h('p', { class: 'eyebrow' }, 'BACKLOG · read-only'), h('h2', {}, 'Backlog'),
        h('p', { class: 'ops-note' }, `The dispatcher will not wake a session for these events: the watch has expired, or the event is older than ${hours} hours (dispatch.max_event_age_hours). This page is read-only so you can review them; to follow up, handle it manually in the original Slack message.`));
      clear(body).append(h('div', { class: 'counters' },
        h('div', { class: 'counter' }, h('b', {}, data.total || 0), h('span', {}, 'Total')),
        h('div', { class: 'counter' }, h('b', {}, data.counts.expired_watch), h('span', {}, 'Watch expired')),
        h('div', { class: 'counter' }, h('b', {}, data.counts.older_than_max_age), h('span', {}, `Older than ${hours} h`)),
        h('div', { class: 'counter' }, h('b', {}, (data.tasks || []).length), h('span', {}, 'Tasks affected'))));
      if (!data.total) { body.append(h('div', { class: 'empty' }, h('b', {}, 'No backlog'), 'All unhandled events are still within the normal delivery range.')); return; }
      if (data.truncated) body.append(h('p', { class: 'warn-box' }, `Showing only the oldest ${data.items.length}.`));
      const byTask = new Map();
      for (const item of data.items) { if (!byTask.has(item.task)) byTask.set(item.task, []); byTask.get(item.task).push(item); }
      for (const t of data.tasks) {
        const items = byTask.get(t.task) || [];
        body.append(h('div', { class: 'bl-task' },
          h('h3', {}, h('span', { class: 'mono' }, t.task), ' ', h('span', { class: 'count' }, t.count),
            h('span', { class: 'sub' }, ` · oldest ${dur(data.generated_at - t.oldest)} ago`)),
          h('table', { class: 'ops-table' },
            h('thead', {}, h('tr', {}, ['Received', 'Age', 'Status', 'Reason', 'Watch', 'Links'].map((x) => h('th', { scope: 'col' }, x)))),
            h('tbody', {}, items.map((it) => h('tr', {},
              h('td', { class: 'num' }, when(it.received)),
              h('td', { class: 'num' }, dur(it.age_seconds)),
              h('td', {}, statusPill(it.status)),
              h('td', {}, pill(it.reason === 'expired_watch' ? 'Watch expired' : `Older than ${hours} h`, it.reason === 'expired_watch' ? null : 'bad')),
              h('td', {}, h('span', { class: 'mono' }, short(it.watch.id, 8)), ' ', provChip(it.watch.provider),
                h('div', { class: 'sub' }, [{ expired: 'Expired', running: 'Running', inbox: 'Inbox' }[it.watch.state] || it.watch.state, it.watch.channel, it.watch.mode, 'Session ' + it.watch.session].filter(Boolean).join(' · '))),
              h('td', { class: 'links' }, ext(it.slack_url, 'Original message ↗'), ext(it.watch.slack_url, 'Thread ↗'))))))));
      }
    }
    every(15000, load);
  }

  // =====================================================================
  // Onboarding (O2e) — the instance mode and what observe-only mode recorded; the owner's triage corrections (O2f)
  // =====================================================================
  const MODE_LABEL = { installed: ['Installed', 'warn'], observe: ['Observing', 'warn'], active: ['Active', 'good'], paused: ['Paused', 'bad'] };
  const SHADOW_KIND = { triage: 'Triage', slack_write: 'Slack post', dispatch: 'Agent run' };
  const ROUTE_LABEL = { task: 'would start a task', reply: 'would reply', log: 'would only record', ignore: 'would ignore', unclear: 'unclear' };
  const MODE_ACTOR = { services: 'Setup check', owner_button: 'Owner (start card)', owner: 'Owner (terminal)', breaker: 'Safety breaker', init: 'kage init' };
  const HELD_WHY = { mode_observe: 'Held: observing', mode_installed: 'Held: not started yet', mode_paused: 'Held: paused' };
  const CARD_STATE = { open: ['Waiting', 'warn'], deciding: ['Deciding', 'warn'], started: ['Started', 'good'], later: ['Another day', null],
    refused: ['Refused', 'bad'], expired: ['Expired', null], stale: ['Out of date', null] };
  // O2f: what a triage should have been (kage.core.observe.ROUTES), as the owner picks it
  const CORRECT_ROUTE = { task: ['Start a task', 'It needs work: a new task.'], reply: ['Reply', 'A short answer in the conversation is enough.'],
    log: ['Only record', 'Worth keeping a note of; nothing to do.'], ignore: ['Ignore', 'Not for you.'] };
  const CORRECT_ERROR = { shadow_record_not_found: 'This record is no longer there; reload the page.', only_triage_records_are_corrected: 'Only a triage can be corrected.',
    only_the_owner_corrects_shadow_records: 'Corrections come from you, not from an agent session.', note_must_be_at_most_300_characters: 'The note is longer than 300 characters.',
    text_looks_like_it_contains_a_credential_use_a_one_time_share_link: 'The note looks like it contains a credential; leave it out.',
    invalid_csrf_token: 'The page is out of date; reload it and try again.', local_state_unavailable: 'The local store is busy; try again in a moment.' };
  const MODE_NOTE = {
    installed: 'Installed: start the services once the doctor passes; the instance then observes before it acts.',
    observe: 'Observing: nothing is sent or run on your behalf. Below is what it would have done. Start it with the button on the daily start card in your DMs.',
    active: 'Active: it replies and runs tasks for you. Anything recorded while it was observing stays listed below.',
    paused: 'Paused: no new run starts until you resume it (`mode resume` in your terminal).',
  };
  // O3b: what Kage understood from the owner's self-introduction (kage.onboard.extract), confirmed item by item
  const INTRO_TARGET = { owner: 'About you', org: 'Your organisation', people: 'People you work with', rule: 'Rules',
    playbook: 'How you handle things', glossary: 'Words and names' };
  const INTRO_FIELD = { name: 'Name', short_name: 'Short name', role: 'Role', company: 'Company', timezone: 'Time zone',
    language: 'Language', voice: 'Voice', tracker: 'Tracker', channels: 'Channels', systems: 'Systems', team: 'Team',
    relationship: 'Relationship', communication: 'How to talk to them', hint: 'How to act', applies_to: 'Applies to',
    term: 'Term', meaning: 'Meaning', asr: 'Often heard as', kind: 'Kind', title: 'Title', statement: 'Statement' };
  const INTRO_STATE = { candidate: ['To confirm', 'warn'], accepted: ['Accepted', 'good'], rejected: ['Not this', null],
    edited: ['Edited', 'good'], applied: ['Applied', 'good'] };
  const INTRO_RUN = { done: ['Read', 'good'], over_cap: ['Over the cap', 'warn'], failed: ['Failed', 'bad'] };
  const RULE_KIND = { fact: 'Fact', limit: 'Limit', preference: 'Preference', principle: 'Principle', gotcha: 'Gotcha' };
  const INTRO_EDIT = { owner: ['name', 'short_name', 'role', 'company', 'timezone', 'language', 'voice'],
    org: ['company', 'tracker', 'channels', 'systems', 'team'], people: ['name', 'role', 'relationship', 'communication'],
    rule: ['title', 'statement', 'hint'], playbook: ['hint', 'applies_to'], glossary: ['term', 'meaning', 'asr'] };
  const INTRO_LISTS = ['channels', 'systems', 'asr'];
  const INTRO_VERIFY = { verified: ['Verified', 'good'], contradicted: ['Slack disagrees', 'bad'], ambiguous: ['Several matches', 'warn'],
    not_found: ['Not in Slack', null], unverifiable: null, error: ['Check failed', null] };
  const INTRO_ERROR = { candidate_not_found: 'This item is no longer there; reload the page.',
    only_the_owner_decides_candidates: 'Decisions come from you, not from an agent session.',
    text_looks_like_it_contains_a_credential_use_a_one_time_share_link: 'That looks like a credential; leave it out.',
    invalid_csrf_token: 'The page is out of date; reload it and try again.', local_state_unavailable: 'The local store is busy; try again in a moment.' };
  function Onboarding() {
    const head = pageHead('Onboarding', 'ONBOARDING · observe and correct', null);
    const body = h('section', { class: 'ops-card', id: 'onboarding' });
    clear(main).append(head, body);
    let csrf = '';
    const sheet = document.getElementById('ops-confirm');
    const sheetBody = document.getElementById('ops-confirm-body');
    const sheetOk = document.getElementById('ops-confirm-ok');
    document.getElementById('ops-confirm-cancel').onclick = () => sheet.close();
    function correctTriage(r) {
      const c = r.correction || {};
      const choices = h('div', { class: 'oc-routes', role: 'radiogroup', 'aria-label': 'What it should have done' },
        Object.entries(CORRECT_ROUTE).map(([route, [label, hint]]) => h('label', { class: 'oc-route' },
          h('input', { type: 'radio', name: 'oc-route', value: route, checked: (c.route || '') === route }),
          h('span', { class: 'oc-route-text' }, h('span', { class: 'oc-route-title' }, h('b', {}, label), route === r.route ? h('span', { class: 'oc-same' }, 'its call') : null),
            h('span', { class: 'sub' }, hint)))));
      const note = h('textarea', { id: 'oc-note', rows: 3, maxlength: 300, placeholder: 'Optional: why (who sent it, what you would have done)' });
      note.value = c.note || '';
      const count = h('span', { class: 'oc-count' }, `${note.value.length} / 300`);
      note.addEventListener('input', () => { count.textContent = `${note.value.length} / 300`; });
      const err = h('p', { class: 'err-box', role: 'alert', hidden: true });
      const pick = () => { const el = choices.querySelector('input:checked'); return el ? el.value : ''; };
      choices.addEventListener('change', () => { sheetOk.disabled = !pick(); });
      sheet.querySelector('.sh-title .eyebrow').textContent = 'CORRECT · Triage';
      document.getElementById('ops-confirm-title').textContent = 'What should it have done?';
      clear(sheetBody).append(
        h('div', { class: 'oc-was' }, h('span', { class: 'sub' }, `${when(r.at)} · ${r.target || '—'}`),
          h('div', {}, 'It ', h('b', {}, ROUTE_LABEL[r.route] || r.route || '—'), r.text ? h('span', { class: 'sub' }, ' — ' + r.text) : null)),
        choices,
        h('div', { class: 'oc-note' }, h('label', { for: 'oc-note' }, 'Note ', h('span', { class: 'sub' }, '(optional)')), note, count),
        h('p', { class: 'note' }, 'Kept next to the record as evidence for setting it up; it changes nothing while it observes.'),
        err);
      sheetOk.textContent = r.correction ? 'Save the new correction' : 'Save correction';
      sheetOk.disabled = !pick();
      sheetOk.onclick = async () => {
        err.hidden = true;
        sheetOk.disabled = true;
        try {
          await api('/api/onboarding/correct', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: JSON.stringify({ id: r.id, route: pick(), note: note.value.trim() || null }) });
        } catch (e) {
          const code = e.body && e.body.error;
          err.textContent = CORRECT_ERROR[code] || `Not saved (${code || e.message}).`;
          err.hidden = false;
          sheetOk.disabled = false;
          return;
        }
        sheet.close();
        toast('Correction saved', 'good');
        load();
      };
      sheet.showModal();
      (choices.querySelector('input:checked') || choices.querySelector('input')).focus();
    }
    function yourCall(r) {
      if (r.kind !== 'triage' || !r.id) return h('span', { class: 'sub' }, '—');
      const c = r.correction;
      if (!c) return h('button', { type: 'button', class: 'btn sm', onclick: () => correctTriage(r) }, 'Correct…');
      const agrees = c.route === r.route;
      return h('div', { class: 'oc-call' },
        h('div', {}, pill(agrees ? 'Agreed' : 'Should: ' + (CORRECT_ROUTE[c.route] || [c.route])[0].toLowerCase(), agrees ? 'good' : 'warn'),
          h('button', { type: 'button', class: 'btn sm ghost', 'aria-label': 'Change the correction', onclick: () => correctTriage(r) }, 'Change')),
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
        toast(INTRO_ERROR[code] || `Not saved (${code || e.message})`, 'bad');
        return false;
      }
      toast('Saved', 'good');
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
          INTRO_LISTS.includes(f) ? h('span', { class: 'sub' }, ' (comma separated)') : null), el);
      });
      const err = h('p', { class: 'err-box', role: 'alert', hidden: true });
      sheet.querySelector('.sh-title .eyebrow').textContent = 'EDIT · Candidate';
      document.getElementById('ops-confirm-title').textContent = 'Correct what I understood';
      clear(sheetBody).append(
        h('div', { class: 'oc-was' }, h('span', { class: 'sub' }, 'You said'), h('div', {}, h('mark', {}, i.quote))),
        kind ? h('label', { class: 'ob-field' }, h('span', {}, 'Kind'), kind) : null,
        h('div', { class: 'ob-fields' }, rows),
        h('p', { class: 'note' }, 'Kept with the item; nothing is used until it is applied.'), err);
      sheetOk.textContent = 'Save the edit';
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
          err.textContent = INTRO_ERROR[code] || `Not saved (${code || e.message})`;
          err.hidden = false;
          sheetOk.disabled = false;
          return;
        }
        sheet.close();
        toast('Saved', 'good');
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
      toast(`Accepted ${res.accepted}`, 'good');
      load();
    }
    function itemCard(i) {
      const [label, tone] = INTRO_STATE[i.status] || [i.status, null];
      return h('article', { class: 'ob-item', 'data-state': i.status },
        h('div', { class: 'ob-item-head' }, pill(label, tone), verifyPill(i), i.support === 'inferred' ? pill('Inferred', 'warn') : null,
          h('span', { class: 'sub' }, `confidence ${Math.round((i.confidence || 0) * 100)}%`)),
        h('dl', { class: 'ob-value' }, flatValue(i.target, (i.decision && i.decision.value) || i.value).map(([k, x]) =>
          [h('dt', {}, INTRO_FIELD[k] || k), h('dd', {}, x)])),
        h('blockquote', { class: 'ob-quote' }, h('mark', {}, i.quote)),
        h('div', { class: 'ob-actions' },
          h('button', { type: 'button', class: 'btn sm primary', disabled: i.status === 'accepted', onclick: () => decideItem(i, 'accept') }, 'Accept'),
          h('button', { type: 'button', class: 'btn sm', onclick: () => editItem(i) }, 'Edit…'),
          h('button', { type: 'button', class: 'btn sm ghost', disabled: i.status === 'rejected', onclick: () => decideItem(i, 'reject') }, 'Not this')));
    }
    function introSection(intro) {
      const sec = h('div', { class: 'ob-intro', id: 'intro' }, h('h3', {}, 'What I understood about you'));
      const run = intro.run;
      if (!run) {
        sec.append(h('div', { class: 'empty' }, h('b', {}, 'No self-introduction yet'), 'In your terminal run onboard intro add FILE, then onboard extract (it shows the cost first). Nothing here is used until you accept it.'));
        return sec;
      }
      const [label, tone] = INTRO_RUN[run.status] || [run.status, null];
      const dropped = Object.values(run.dropped || {}).reduce((a, b) => a + b, 0);
      sec.append(h('p', { class: 'ops-note ob-run' }, pill(label, tone), ` ${when(run.at)} · ${run.items} candidate(s) · `,
        run.cost_usd != null ? `$${run.cost_usd.toFixed(2)} of $${(run.cap_usd || 0).toFixed(2)}` : 'cost not reported',
        dropped ? ` · ${dropped} dropped by the checks` : null, run.error ? ` · ${run.error}` : null));
      const open = intro.items.filter((i) => i.status === 'candidate').length;
      if (intro.items.length) sec.append(h('p', { class: 'ops-note' }, `${open} to confirm. Accepting records your choice; it is used once applied (onboard apply in your terminal).`));
      const ready = intro.items.filter((i) => i.status === 'candidate' && i.verify && i.verify.result === 'verified').length;
      if (ready) sec.append(h('div', { class: 'ob-bulk' }, h('button', { type: 'button', class: 'btn sm primary', onclick: (e) => acceptVerified(e.currentTarget) },
        `Accept all verified (${ready})`)));
      else if (intro.items.length && !intro.items.some((i) => i.verify)) sec.append(h('p', { class: 'note' }, 'Facts are checked against Slack with onboard verify in your terminal; only verified ones can be accepted together.'));
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
      clear(head.querySelector('div')).append(h('p', { class: 'eyebrow' }, 'ONBOARDING · observe and correct'), h('h2', {}, 'Onboarding'),
        h('p', { class: 'ops-note' }, MODE_NOTE[data.mode] || ''));
      const counts = data.counts || {};
      const total = Object.values(counts).reduce((a, b) => a + b, 0);
      const corrected = data.recent.filter((r) => r.correction).length;
      clear(body).append(h('div', { class: 'counters' },
        h('div', { class: 'counter' }, h('b', {}, pill(label, tone)), h('span', {}, 'Mode')),
        h('div', { class: 'counter' }, h('b', {}, total), h('span', {}, 'Recorded, not done')),
        h('div', { class: 'counter' }, h('b', {}, counts.triage || 0), h('span', {}, 'Messages triaged')),
        h('div', { class: 'counter' }, h('b', {}, corrected), h('span', {}, 'Triages you corrected')),
        h('div', { class: 'counter' }, h('b', {}, counts.dispatch || 0), h('span', {}, 'Agent runs held')),
        h('div', { class: 'counter' }, h('b', {}, counts.slack_write || 0), h('span', {}, 'Slack posts held'))));
      if (!data.row) body.append(h('p', { class: 'ops-note' }, 'This instance predates onboarding: it has always been active.'));
      body.append(introSection(data.intro || { run: null, items: [] }));
      body.append(h('h3', {}, 'What it would have done'));
      if (!data.recent.length) body.append(h('div', { class: 'empty' }, h('b', {}, 'Nothing recorded'), 'Observe-only mode has not held anything back yet.'));
      else {
        if (counts.triage) body.append(h('p', { class: 'ops-note' }, 'A triage you disagree with: correct it. Corrections are kept as evidence and change nothing while it observes.'));
        body.append(h('div', { class: 'oc-scroll' }, h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Kind', 'Where', 'Would have', 'Why', 'Your call'].map((x) => h('th', { scope: 'col' }, x)))),
          h('tbody', {}, data.recent.map((r) => h('tr', {},
            h('td', { class: 'num' }, when(r.at)),
            h('td', {}, pill(SHADOW_KIND[r.kind] || r.kind, null)),
            h('td', {}, h('span', { class: 'mono' }, r.target || '—')),
            h('td', {}, r.kind === 'triage' ? (ROUTE_LABEL[r.route] || r.route || '—') : (r.endpoint ? r.endpoint + ': ' : '') + (r.text || '')),
            h('td', { class: 'sub' }, r.kind === 'triage' ? (r.text || '') : (HELD_WHY[r.why] || r.why)),
            h('td', { class: 'oc-cell' }, yourCall(r))))))));
      }
      body.append(h('h3', {}, 'Mode changes'));
      if (!data.log.length) body.append(h('p', { class: 'ops-note' }, 'No change recorded.'));
      else body.append(h('table', { class: 'ops-table' },
        h('thead', {}, h('tr', {}, ['Time', 'From', 'To', 'By', 'Reason'].map((x) => h('th', { scope: 'col' }, x)))),
        h('tbody', {}, data.log.map((r) => h('tr', {},
          h('td', { class: 'num' }, when(r.at)), h('td', {}, r.old ? (MODE_LABEL[r.old] || [r.old])[0] : '—'),
          h('td', {}, (MODE_LABEL[r.new] || [r.new])[0]), h('td', {}, MODE_ACTOR[r.actor] || r.actor), h('td', { class: 'sub' }, r.reason || ''))))));
      if (data.cards.length) {
        body.append(h('h3', {}, 'Start cards'));
        body.append(h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['Posted', 'State', 'Settled'].map((x) => h('th', { scope: 'col' }, x)))),
          h('tbody', {}, data.cards.map((c) => h('tr', {},
            h('td', { class: 'num' }, when(c.posted_at)), h('td', {}, pill(...(CARD_STATE[c.state] || [c.state, null]))), h('td', { class: 'num' }, when(c.settled_at)))))));
      }
    }
    every(15000, load);
  }

  // =====================================================================
  // Intake (P2) — read-only, metadata only
  // =====================================================================
  const CONV = { im: 'DM', mpim: 'Group DM', channel: 'Channel' };
  const ROUTE = { inbox: 'Intake', oncall: 'oncall', bound: 'Bound task', ref: 'Ticket / PR' };
  const ROUTE_REASON = { default_inbox: 'Default to Intake', oncall_alert: 'oncall alert', intake_binding: 'Conversation bound', {{ticket_ref_reason}}: '{{ticket_team}} ticket',
    pr_link: 'PR link' };
  const BURST_STATE = { open: ['Merging', null, true], sealed: ['Awaiting routing', 'warn'], routed: ['Delivered', 'good'],
    waiting: ['Waiting for task session', 'warn'], covered: ['Merged into task', ''], expired: ['Expired', 'bad'] };
  const FILTER_REASON = { burst: 'Joined a burst', alert_duplicate: 'Alert dedup', {{owner_key}}_authored: '{{owner_name}} (self)', our_agent: 'Our agent',
    system_noise: 'System noise', bot_outside_oncall: 'Non-oncall bot', covered_by_watch: 'Already watched', pending: 'In progress' };
  const DISPOSITION = { forward: 'Forward to task', launch: 'New task', answer: 'Reply directly', ask_requester: 'Ask requester', brief: 'Brief {{owner_name}}',
    card_{{owner_key}}: 'Card to {{owner_name}}', log: 'Log only', ignore: 'Ignore', investigate: 'Investigate', escalate: 'Escalate incident' };
  const INTENT = { request: 'Request', question: 'Question', directive: 'Directive', follow_up: 'Follow-up', fyi: 'FYI', chit_chat: 'Chit-chat',
    alert: 'Alert', other: 'Other' };
  const CLARITY = { clear: 'Clear', partial: 'Partly clear', vague: 'Vague' };
  const INTAKE_STATUS = { ok: ['OK', 'good'], backoff: ['Rate-limited, backing off', 'warn'], error: ['Error, backing off', 'bad'],
    auth_error: ['Auth failed', 'bad'] };
  const tsWhen = (ts) => ts ? when(Number(ts)) : '—';

  function Intake() {
    const head = pageHead('Intake', 'INTAKE · Slack intake',
      'The daemon collects messages from DMs, @{{owner_name}} mentions and subscribed channels, merges them into bursts and routes them to tasks by rule (no model calls). Open any message below to see its full trace through the system. Metadata only; message bodies are never written to disk.');
    const status = h('section', { class: 'ops-card', id: 'intake-status' });
    // P48: the raw tables stay, folded under the trace list (built once, so an opened <details> survives the refresh)
    const raw = (id, title) => {
      const body = h('div', { class: 'raw-body' });
      const count = h('span', { class: 'count' }, '0');
      const sec = h('section', { class: 'ops-card', id }, h('details', { class: 'hist' }, h('summary', {}, title, ' ', count), body));
      return { sec, body, count };
    };
    const burstsBox = raw('intake-bursts', 'Raw records: recent bursts');
    const triageBox = raw('intake-triage', 'Raw records: triage');
    const bursts = burstsBox.body, triage = triageBox.body;
    const bindingsBox = raw('intake-bindings', 'Conversation bindings');
    const bindings = bindingsBox.body;
    const trace = window.{{trace_global}} ? window.{{trace_global}}.mount({ h, clear, fill, api, online, toast }).section : null;
    clear(main).append(head, status, trace, burstsBox.sec, triageBox.sec, bindingsBox.sec);

    function routeCell(b) {
      const reason = b.route_reason && b.route_reason.startsWith('watch:') ? 'Task watch' : (ROUTE_REASON[b.route_reason] || b.route_reason);
      return h('td', {}, b.route ? (ROUTE[b.route] || b.route) : '—',
        b.target_task ? h('div', { class: 'sub mono' }, b.target_task) : null,
        reason ? h('div', { class: 'sub' }, reason) : null,
        b.handled_task && b.handled_task !== b.target_task ? h('div', { class: 'sub mono' }, '→ ' + b.handled_task) : null);
    }
    function renderStatus(d) {
      const st = d.state || {};
      const cfg = d.config || {};
      const last = st.last_cycle || {};
      const [label, tone] = !cfg.enabled ? ['Off', 'warn'] : (INTAKE_STATUS[st.status] || [st.status ? st.status : 'Not started yet', null]);
      const warns = [];
      if (d.config_error) warns.push(`Invalid config: ${d.config_error}`);
      for (const c of st.unresolved_channels || []) warns.push(`Channel ${c} not recognized; use a channel ID (C…)`);
      for (const [ep, until] of Object.entries(st.backoff || {})) if (until * 1000 > Date.now()) warns.push(`${ep} rate-limited, backing off until ${clock(until)}`);
      if (st.last_error) warns.push(`Last error: ${st.last_error}${st.last_error_at ? ' · ' + when(st.last_error_at) : ''}`);
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, pill(label, tone)), h('span', {}, 'Status')),
          h('div', { class: 'counter' }, h('b', {}, last.at ? clock(last.at) : '—'), h('span', {}, 'Last cycle')),
          h('div', { class: 'counter' }, h('b', {}, last.at ? `${last.api_calls}/${cfg.max_api_calls_per_cycle || '?'}` : '—'),
            h('span', {}, 'API calls this cycle / cap')),
          h('div', { class: 'counter' }, h('b', {}, st.api_calls_last_hour || 0), h('span', {}, 'Calls, last hour')),
          h('div', { class: 'counter' }, h('b', {}, st.next_cycle_at ? clock(st.next_cycle_at) : '—'), h('span', {}, 'Next cycle')),
          h('div', { class: 'counter' }, h('b', {}, (d.counts || {}).waiting || 0), h('span', {}, 'Waiting for task session'))),
        h('dl', { class: 'kv' },
          h('dt', {}, 'Channels'), h('dd', {}, (d.channels || []).map((c) => h('span', { class: 'pill', 'data-tone': c.id ? null : 'bad' },
            `${c.entry}${c.id && c.id !== c.entry ? ' · ' + c.id : ''}${c.oncall ? ' · oncall' : ''}`))),
          h('dt', {}, 'Intake tasks'), h('dd', {}, ['inbox', 'oncall'].map((r) => {
            const t = (d.tasks || {})[r];
            return t ? h('div', { class: 'mono' }, `${t.task_id} · ${t.current_provider ? (PROVIDER[t.current_provider] || t.current_provider) + ' ' + t.current_session : (t.registered ? 'no current session' : 'not initialized (intake-bootstrap)')}`) : null;
          })),
          h('dt', {}, 'Cadence'), h('dd', {}, `every ${cfg.poll_seconds || '?'} s · burst window ${cfg.burst_seconds || '?'} s · alert dedup ${cfg.alert_dedupe_minutes || '?'} min`),
          h('dt', {}, 'Filtered (24 h)'), h('dd', {}, Object.entries(d.filtered_24h || {}).map(([k, n]) => h('span', { class: 'pill' }, `${FILTER_REASON[k] || k} ${n}`)))),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderBursts(d) {
      const items = d.bursts || [];
      burstsBox.count.textContent = items.length;
      fill(bursts,
        items.length ? h('table', { class: 'ops-table', id: 'intake-burst-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Conversation', 'Sender', 'Messages', 'Route', 'Status', 'Triage', 'Alert'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((b) => {
            const [label, tone, live] = BURST_STATE[b.state] || [b.state, null];
            return h('tr', { 'data-burst': b.burst_id },
              h('td', { class: 'num' }, tsWhen(b.first_ts), b.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', {}, CONV[b.conv_type] || b.conv_type, ' ', h('span', { class: 'mono' }, b.channel), b.thread_root ? h('div', { class: 'sub' }, 'thread') : null,
                ' ', ext(b.slack_url, 'Original message ↗')),
              h('td', { class: 'mono' }, b.sender || '—', b.person_style === 'strategic_directive' ? h('div', {}, pill('Directive', 'warn')) : null),
              h('td', { class: 'num' }, b.msg_count),
              routeCell(b),
              h('td', {}, pill(label, tone === '' ? null : tone, live), b.event_status ? h('div', { class: 'sub' }, (EVENT_STATUS[b.event_status] || [b.event_status])[0]) : null,
                b.error ? h('div', { class: 'sub' }, b.error) : null),
              h('td', {}, (b.triage || []).map((x) => pill(DISPOSITION[x] || x))),
              h('td', {}, b.alert_fp ? [h('span', { class: 'mono' }, b.alert_service || '?'), h('div', { class: 'sub mono' }, b.alert_fp.slice(0, 8)),
                b.dup_count ? h('div', { class: 'sub' }, `repeated ${b.dup_count}×`) : null, b.alert_state ? h('div', { class: 'sub' }, b.alert_state) : null] : '—'));
          }))) : h('div', { class: 'empty' }, h('b', {}, 'No bursts yet'), d.notice === 'intake_tables_missing' ? 'The daemon has not created the intake tables yet.' : 'Bursts appear here once messages arrive.'));
    }
    function renderTriage(d) {
      const items = d.triage || [];
      triageBox.count.textContent = items.length;
      fill(triage,
        h('p', { class: 'note' }, 'One row per triage; to correct one, run slack_followup.py triage-correct --record ID --disposition … --note …'),
        items.length ? h('table', { class: 'ops-table', id: 'intake-triage-table' },
          h('thead', {}, h('tr', {}, ['Time', 'burst', 'Intent', 'Clarity', 'Disposition', 'Target task', 'Confidence', 'Basis', 'Correction'].map((t) => h('th', { scope: 'col' }, t)))),
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
          h('div', { class: 'empty' }, h('b', {}, 'No triage records yet'), 'Intake writes them here as it handles events.'));
    }
    function renderBindings(d) {
      const items = d.bindings || [];
      bindingsBox.count.textContent = items.length;
      fill(bindings,
        items.length ? h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['Conversation', 'Task', 'Watch', 'Expires', ''].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => h('tr', {},
            h('td', { class: 'mono' }, x.channel, x.thread_root ? h('div', { class: 'sub' }, 'thread ' + x.thread_root) : h('div', { class: 'sub' }, 'whole conversation')),
            h('td', { class: 'mono' }, x.task_id),
            h('td', { class: 'mono' }, x.watch_id ? short(x.watch_id, 8) : (x.error || '—')),
            h('td', {}, x.expires_at ? when(x.expires_at) : 'No expiry', x.active ? null : h('div', {}, pill('Inactive'))),
            h('td', {}, ext(x.slack_url, 'Slack ↗')))))) : h('p', { class: 'note' }, 'No bindings yet.'));
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
  // Rules (P3) — read-only: outcomes, approvals, overrides, proposals, backtest. Ids only, never plans.
  // =====================================================================
  const RULE_OUTCOME = { auto: ['Auto', 'good'], one_tap: ['One-tap approve', 'warn'], clarify: ['Clarify first', null], route: ['Route', null],
    {{owner_key}}: ['To {{owner_name}}', 'bad'] };
  const APPROVAL = { not_required: ['Not required', null], clarify_first: ['Clarify first', null], not_requested: ['No card sent', 'warn'],
    pending: ['Awaiting approval', 'warn'], approved: ['Approved', 'good'], approved_with_condition: ['Conditional (resubmit)', 'warn'],
    rejected: ['Rejected', 'bad'], self_approval_refused: ['Self-approval refused', 'bad'], approver_not_in_rules: ['Approver not in rules', 'bad'],
    unknown: ['Unknown', null] };
  const CHECK_RESULT = { executed: 'Executed', failed: 'Execution failed', abandoned: 'Abandoned' };
  const PROPOSAL_STATUS = { posting: ['Posting card', null], pending: ['Waiting for {{owner_name}}', 'warn'], applied: ['Applied', 'good'],
    rejected: ['Not adopted (not re-proposed for 28 days)', null], needs_revision: ['Conditional (resubmit)', 'warn'], conflict: ['Not applied (conflict)', 'bad'],
    failed: ['Card post failed', 'bad'], written: ['Written', 'good'] };
  const SHAPE = { unknown_role: 'Unknown role', admin_or_owner: 'admin / owner level', prod_destructive: 'Prod destructive / irreversible',
    spend_over_threshold: 'Spend over threshold', group_wide_expansion: 'Group-wide expansion', personal_prod_access: 'Personal prod Writer / exec / Secret',
    workload_admin_identity: 'Admin workload identity', auth_entry_change: 'Auth entry change', account_wide_secret_read: 'Account-wide Secret read',
    unregistered_principal: 'Unregistered personal principal' };
  const LEVEL = { auto: 'Auto', one_tap: 'One-tap approve', {{owner_key}}: 'To {{owner_name}}' };
  const pctText = (v) => v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`;
  const fmtDay = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, month: '2-digit', day: '2-digit' });
  const outcomePillRule = (o) => { const [label, tone] = RULE_OUTCOME[o] || [o, null]; return pill(label, tone); };

  function Rules() {
    const head = pageHead('Rules', 'RULES · heuristics engine',
      'Access and account changes go through the rule engine first: auto runs directly, one_tap / {{owner_key}} post a decision card, clarify asks first, route hands off. Lists show metadata only; click a check ID to see its full record (including the plan).');
    const registry = h('section', { class: 'ops-card', id: 'rules-registry' });
    const status = h('section', { class: 'ops-card', id: 'rules-status' });
    const weeks = h('section', { class: 'ops-card', id: 'rules-weeks' });
    const backtest = h('section', { class: 'ops-card', id: 'rules-backtest' });
    const recent = h('section', { class: 'ops-card', id: 'rules-recent' });
    const proposals = h('section', { class: 'ops-card', id: 'rules-proposals' });
    clear(main).append(head, registry, status, weeks, backtest, recent, proposals);

    function renderRegistry(d) {  // P47: one row per rule of the registry (the switch and the text of each code check)
      const r = d.registry || { rules: [] };
      fill(registry, h('h3', {}, 'Rule registry'),
        h('p', { class: 'note' }, (r.source === 'live' ? 'Live directory ' : 'Initial version bundled with the skill ') + (r.dir || '') + '; rules change only via rules install (by you in person, never an agent).'),
        h('table', { class: 'ops-table', id: 'rules-registry-table' },
          h('thead', {}, h('tr', {}, ['Rule', 'How it checks', 'Why', 'Status'].map((x) => h('th', {}, x)))),
          h('tbody', {}, r.rules.map((x) => h('tr', {}, h('td', {}, h('span', { class: 'mono' }, x.id + ' '), x.title),
            h('td', {}, x.how), h('td', {}, x.why), h('td', {}, x.status))))));
    }

    function renderStatus(d) {
      const spec = d.spec || {};
      const cfg = d.config || {};
      const week = (d.weeks || []).slice(-1)[0] || { checks: 0, shares: {}, approval_latency: {} };
      const warns = [];
      if (d.spec_error) warns.push(`Rule file unavailable: ${d.spec_error} (rule-check will error; agents must ask {{owner_name}})`);
      if (cfg.enabled === false) warns.push('Rule engine is off (rules.enabled=false): every check goes to {{owner_name}}');
      if (cfg.proposals_enabled === false) warns.push('Rule proposals are off (rules.proposals_enabled=false)');
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, spec.version ? `v${spec.version}` : '—'), h('span', {}, 'Current version (access)')),
          h('div', { class: 'counter' }, h('b', {}, spec.rules || '—'), h('span', {}, 'Rules')),
          h('div', { class: 'counter' }, h('b', {}, week.checks), h('span', {}, 'Checks this week')),
          h('div', { class: 'counter' }, h('b', {}, pctText(week.shares.auto_tier)), h('span', {}, 'No {{owner_name}} needed this week')),
          h('div', { class: 'counter' }, h('b', {}, pctText(week.shares.auto_or_one_tap)), h('span', {}, 'Incl. one-tap')),
          h('div', { class: 'counter' }, h('b', {}, week.approval_latency.median === null || week.approval_latency.median === undefined ? '—' : dur(week.approval_latency.median)), h('span', {}, 'Approval time (median)')),
          h('div', { class: 'counter' }, h('b', {}, (d.totals || {}).overrides || 0), h('span', {}, '{{owner_name}} overrides'))),
        h('dl', { class: 'kv' },
          h('dt', {}, 'Rule file'), h('dd', { class: 'mono' }, spec.file ? `${spec.file} · ${spec.digest}` : '—'),
          h('dt', {}, 'Levels'), h('dd', {}, Object.entries(spec.levels || {}).map(([k, n]) => h('span', { class: 'pill' }, `${LEVEL[k] || k} ${n}`))),
          h('dt', {}, 'never-auto'), h('dd', {}, Object.entries(spec.never_auto || {}).map(([k, lvl]) => h('span', { class: 'pill', 'data-tone': lvl === '{{owner_key}}' ? 'bad' : 'warn' }, `${SHAPE[k] || k} → ${LEVEL[lvl] || lvl}`))),
          h('dt', {}, 'One-tap exceptions'), h('dd', {}, Object.entries(spec.claims || {}).map(([rid, c]) => h('span', { class: 'pill' }, `${rid}: ${Object.keys(c).map((x) => SHAPE[x] || x).join(', ')}`))),
          h('dt', {}, 'Switches'), h('dd', {}, pill(cfg.enabled === false ? 'Engine off' : 'Engine on', cfg.enabled === false ? 'bad' : 'good'), ' ',
            pill(cfg.proposals_enabled === false ? 'Proposals off' : 'Proposals on', cfg.proposals_enabled === false ? 'warn' : 'good'))),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderWeeks(d) {
      const items = (d.weeks || []).slice().reverse();
      fill(weeks, h('h3', {}, 'Weekly outcome mix'),
        h('p', { class: 'note' }, 'Auto share = auto + clarify + route (no {{owner_name}} action needed; same basis as the v1 estimate); “incl. one-tap” also adds one_tap.'),
        h('table', { class: 'ops-table', id: 'rules-week-table' },
          h('thead', {}, h('tr', {}, ['Week (from)', 'Checks', 'Auto', 'One-tap', 'Clarify', 'Route', 'To {{owner_name}}', 'No {{owner_name}}', 'Incl. one-tap', 'Approval time median / P90', 'Overrides', 'Executed'].map((t) => h('th', { scope: 'col' }, t)))),
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
      if (!b) { fill(backtest, h('h3', {}, 'Backtest'), h('p', { class: 'note' }, 'No backtest recorded yet (rules backtest --record).')); return; }
      const sh = b.shares || {};
      const ok = b.pass && b.pass.overall;
      fill(backtest, h('h3', {}, 'Backtest ', pill(ok ? 'Passed' : 'Failed', ok ? 'good' : 'bad')),
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, b.cases), h('span', {}, 'Cases')),
          h('div', { class: 'counter' }, h('b', {}, `${pctText(sh.engine_auto)} / ${pctText(sh.v1_auto)}`), h('span', {}, 'No {{owner_name}}: engine / v1')),
          h('div', { class: 'counter' }, h('b', {}, `${pctText(sh.engine_auto_or_one_tap)} / ${pctText(sh.v1_auto_or_one_tap)}`), h('span', {}, 'Incl. one-tap: engine / v1')),
          h('div', { class: 'counter' }, h('b', {}, `${sh.delta_auto_pp} / ${sh.delta_auto_or_one_tap_pp} pp`), h('span', {}, `Delta (tolerance ±${b.tolerance_pp} pp)`)),
          h('div', { class: 'counter' }, h('b', {}, `${(b.agreement || {}).cases || 0}/${b.cases}`), h('span', {}, 'Per-case agreement'))),
        h('p', { class: 'note' }, `Rules ${b.spec ? 'v' + b.spec.version + ' · ' + b.spec.digest : ''} · generated ${b.generated_at || '—'} · per-case disagreements stay in the private directory only.`));
    }
    function renderRecent(d) {
      const items = d.recent || [];
      fill(recent, h('h3', {}, 'Recent checks ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'rules-recent-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Check', 'System · action', 'Matched rules', 'Outcome', 'never-auto', 'Approval', 'Result', 'Task'].map((t) => h('th', { scope: 'col' }, t)))),
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
              h('td', {}, CHECK_RESULT[c.result] || '—', c.corrected ? h('div', {}, pill('{{owner_name}} corrected', 'warn')) : null),
              h('td', { class: 'mono' }, c.task_id || '—'));
          }))) : h('div', { class: 'empty' }, h('b', {}, 'No rule checks yet'), d.notice === 'rules_tables_missing' ? 'The daemon has not created the rules tables yet.' : 'Checks appear here once an agent runs rule-check before an access or account change.'));
    }
    function renderProposals(d) {
      const items = d.proposals || [];
      fill(proposals, h('h3', {}, 'Rule proposals ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'rules-proposal-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Proposal', 'Type', 'Title', 'Status', 'Version', ''].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [label, tone] = PROPOSAL_STATUS[x.status] || [x.status, null];
            return h('tr', {},
              h('td', { class: 'num' }, when(x.created_at), x.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, h('a', { href: '/rules?proposal=' + x.id }, x.id)),
              h('td', {}, x.kind === 'rule_patch' ? 'Rule patch' : 'Triage rule addition'),
              h('td', {}, x.title_zh),
              h('td', {}, pill(label, tone), x.error ? h('div', { class: 'sub' }, x.error) : null),
              h('td', { class: 'num' }, x.applied_version ? `v${x.base_version} → v${x.applied_version}` : (x.base_version ? `based on v${x.base_version}` : '—')),
              h('td', {}, x.suppress_until ? h('div', { class: 'sub' }, 'Snoozed until ' + when(x.suppress_until) + ' (no re-proposal)') : null));
          }))) : h('p', { class: 'note' }, 'No rule proposals yet: the {{owner_key}}-inbox routine raises them after its weekly review, Mondays at 10:07.'),
        (d.changelog || []).length ? h('details', { class: 'hist' }, h('summary', {}, `Change log (${d.changelog.length})`),
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
  const CHECK_STATUS = { fail: 'Failed', missing: 'Missing info', escalate: 'Needs approval', route: 'Route' };
  const RESULT_LABEL = { executed: 'Executed', failed: 'Execution failed', abandoned: 'Abandoned' };
  const PROVENANCE_LABEL = { slack_button: 'Button click', thread_reply: 'Thread reply', text_confirmed: 'Text + confirm button', smoke_synthetic: 'TEST synthetic',
    {{owner_key}}_typed: 'Instruction you typed yourself in the card thread' };
  function RuleCheckDetail(id) {
    const crumbs = h('nav', { class: 'crumbs' }, h('a', { href: '/rules' }, '← Rules'));
    const [head, headStatus] = detailHead('Check ' + id, 'RULE CHECK · check details',
      'The Slack card holds a single sentence; the matched rules, failed checks, facts, approvers and plan are all here.');
    const facts = h('section', { class: 'ops-card summary', id: 'check-detail' });
    const why = h('section', { class: 'ops-card', id: 'check-why' });
    const planCard = h('section', { class: 'ops-card', id: 'check-plan' });
    clear(main).append(crumbs, head, facts, why, planCard);
    async function load() {
      let d;
      try { d = await api('/api/rules/checks/' + encodeURIComponent(id)); online(true); } catch (e) {
        online(e.status === 404);
        fill(facts, h('div', { class: 'empty' }, h('b', {}, e.status === 404 ? 'No such check' : 'Read failed'), h('a', { href: '/rules' }, 'Back to Rules')));
        return;
      }
      const c = d.check;
      const a = c.approval || {};
      const [alabel, atone] = APPROVAL[a.status] || [a.status || '—', null];
      const nameOf = (uid) => ((c.approvers || []).find((x) => x.id === uid) || {}).name || uid;
      fill(headStatus, outcomePillRule(c.outcome), pill(alabel, atone), c.smoke ? pill('TEST', 'warn') : null);
      fill(facts, h('div', { class: 'lead' }, h('span', { class: 'lead-label' }, 'Agent’s one-liner'),
        h('p', { class: 'gap-text' }, c.summary || '(none: this check posted no card)')),
      factGrid([
        // the codes behind the words stay in the tooltip
        ['What', `${c.what.system_zh} · ${c.what.action_zh} · ${c.what.kind_zh}`, null, `${c.what.system} · ${c.what.action} · ${c.what.kind}`],
        ['Outcome', c.outcome_zh],
        ['Approval', h('span', {}, a.actor ? `${nameOf(a.actor)} · ${when(a.decided_at)}` : alabel,
          a.provenance ? h('div', { class: 'sub' }, 'Via: ' + (PROVENANCE_LABEL[a.provenance] || a.provenance), a.source ? [' · ', ext(a.source, 'That message ↗')] : null) : null,
          a.note ? h('div', { class: 'sub' }, 'Condition: ' + a.note) : null)],
        ['Result', h('span', {}, c.result ? `${RESULT_LABEL[c.result.result] || c.result.result} · ${when(c.result.at)}` : '—',
          c.result && c.result.by ? h('div', { class: 'sub mono' }, c.result.by) : null,
          c.correction ? h('div', {}, pill('{{owner_name}} corrected: ' + (c.correction.outcome || ''), 'warn')) : null)],
        ['Task', h('span', {}, h('span', { class: 'mono' }, c.task_id || '—'), h('div', { class: 'sub' }, `${when(c.created_at)} · ${c.caller}`))],
        ['Card', c.card ? h('span', {}, ext(c.card.slack_url, 'Slack ↗'), ' · ', when(c.card.posted_at)) : 'No card posted'],
        ['Rule version', h('span', {}, `${c.category} v${c.spec_version || '?'}`, c.spec_digest ? h('div', { class: 'sub' }, c.spec_digest) : null), 'mono'],
        ['Approvers', h('span', {}, (c.approvers || []).map((x) => pill(x.name || x.id, x.id === c.recipient_slack ? 'bad' : null)),
          c.recipient_slack && (c.approvers || []).some((x) => x.id === c.recipient_slack) ? h('div', { class: 'sub' }, 'The subject is an approver: they cannot approve this themselves') : null)],
      ]),
      c.result && c.result.note ? h('div', { class: 'lead' }, h('span', { class: 'lead-label' }, 'Execution note'), h('p', { class: 'gap-text' }, c.result.note)) : null);
      fill(why, h('h3', {}, 'Why this outcome'),
        h('table', { class: 'ops-table', id: 'check-rules' },
          h('thead', {}, h('tr', {}, ['Rule', 'Description'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, (c.matched_rules || []).map((r) => h('tr', {}, h('td', { class: 'mono' }, r.id), h('td', {}, r.title_zh || '—'))))),
        (c.failed_checks || []).length ? h('table', { class: 'ops-table', id: 'check-failed' },
          h('thead', {}, h('tr', {}, ['Rule · check', 'Status', 'In plain words', 'Reason code', 'Goes to'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, c.failed_checks.map((f) => h('tr', {},
            h('td', { class: 'mono' }, `${f.rule} · ${f.check}`), h('td', {}, CHECK_STATUS[f.status] || f.status),
            h('td', {}, f.why_zh || '—'), h('td', { class: 'mono' }, f.reason), h('td', {}, RULE_OUTCOME[f.outcome] ? RULE_OUTCOME[f.outcome][0] : f.outcome))))) : h('p', { class: 'note' }, 'All checks passed.'),
        (c.never_auto || []).length ? h('div', { class: 'sub' }, 'never-auto: ', c.never_auto.map((x) => pill(`${SHAPE[x.shape] || x.shape}${x.zh ? ' (' + x.zh + ')' : ''} → ${LEVEL[x.effective] || x.effective}${x.claimed_by ? ' · ' + x.claimed_by : ''}`, 'bad'))) : null,
        h('details', { class: 'hist' }, h('summary', {}, 'Facts (found by code)'), h('pre', { class: 'json' }, JSON.stringify(c.facts, null, 2))));
      fill(planCard, h('h3', {}, 'Plan ', h('span', { class: 'sub mono' }, c.plan_hash)),
        h('p', { class: 'note' }, 'Approval covers this exact plan only; any change to the plan needs a new check.'),
        h('pre', { class: 'json' }, JSON.stringify(c.plan, null, 2)));
    }
    every(15000, load);
  }

  function RuleProposalDetail(id) {
    const crumbs = h('nav', { class: 'crumbs' }, h('a', { href: '/rules' }, '← Rules'));
    const [head, headStatus] = detailHead('Proposal ' + id, 'RULE PROPOSAL · rule proposal details', 'The card holds one sentence and the rationale; the exact change is here.');
    const card = h('section', { class: 'ops-card summary', id: 'proposal-detail' });
    const body = h('section', { class: 'ops-card', id: 'proposal-body' });
    clear(main).append(crumbs, head, card, body);
    async function load() {
      let d;
      try { d = await api('/api/rules/proposals/' + encodeURIComponent(id)); online(true); } catch (e) {
        online(e.status === 404);
        fill(card, h('div', { class: 'empty' }, h('b', {}, e.status === 404 ? 'No such proposal' : 'Read failed'), h('a', { href: '/rules' }, 'Back to Rules')));
        return;
      }
      const x = d.proposal;
      const [label, tone] = PROPOSAL_STATUS[x.status] || [x.status, null];
      const ev = x.evidence || {};
      fill(headStatus, pill(label, tone), x.smoke ? pill('TEST', 'warn') : null);
      fill(card, h('div', { class: 'lead' }, h('span', { class: 'lead-label' }, 'Title'), h('p', { class: 'lead-title' }, x.title_zh)),
        factGrid([
          ['Type', x.kind === 'rule_patch' ? 'Rule patch' : 'Triage rule addition'],
          ['Status', h('span', {}, label, x.error ? h('div', { class: 'sub' }, x.error) : null)],
          ['Version', x.applied_version ? `v${x.base_version} → v${x.applied_version}` : (x.base_version ? `based on v${x.base_version}` : '—'), 'mono'],
          ['Proposed', h('span', {}, when(x.created_at), x.proposer ? h('div', { class: 'sub' }, x.proposer) : null)],
          ['Decided', x.decided_at ? h('span', {}, when(x.decided_at), x.decided_by ? h('div', { class: 'sub' }, x.decided_by) : null) : 'Not decided yet'],
        ]));
      fill(body, h('h3', {}, 'Content'), kv([
        ['Rationale', x.rationale_zh, 'gap-text'],
        x.text_zh ? ['Text to add', x.text_zh, 'gap-text'] : null,
        x.kind === 'rule_patch' ? ['Change', (x.lines || []).length ? h('ul', { class: 'patch-lines' }, x.lines.map((l) => h('li', { class: 'mono' }, l))) : h('pre', { class: 'json' }, JSON.stringify(x.patch, null, 2))] : null,
        ['Evidence', h('span', {}, Object.entries(ev.counts || {}).map(([k, v]) => pill(`${k} ${v}`, null)),
          (ev.check_ids || []).map((cid) => [' ', h('a', { class: 'mono', href: '/rules?check=' + cid }, cid)]))],
      ]));
    }
    every(15000, load);
  }

  // =====================================================================
  // Routines (P3) — read-only
  // =====================================================================
  const ROUTINE_STATE = { active: ['Enabled', 'good'], disabled: ['Disabled', 'warn'], expired: ['Expired', null], unapproved: ['Unapproved (not run)', 'bad'],
    awaiting_confirmation: ['Awaiting confirmation from {{owner_name}} (not run)', 'warn'], rejected: ['Not enabled by {{owner_name}} (not run)', 'bad'],
    invalid: ['Invalid file (not run)', 'bad'], removed: ['Removed', null] };
  const PROVENANCE = { {{owner_key}}_authored: 'Written by {{owner_name}}', {{owner_key}}_approved: 'Approved via card', unapproved: 'Unapproved' };
  const CONFIRM_VIA = { card: 'Confirmed via card', cli: 'Confirmed via routines enable in the terminal', smoke: 'TEST auto-confirmed' };
  const CONFIRM_STATUS = { confirmed: ['Confirmed', 'good'], pending: ['Confirmation card sent, waiting for {{owner_name}}', 'warn'], posting: ['Posting confirmation card', 'warn'],
    failed: ['Confirmation card not sent', 'bad'], rejected: ['Declined', 'bad'], needs_revision: ['Conditional: edit the file or run routines enable', 'warn'],
    not_{{owner_key}}: ['A click by someone else does not count: {{owner_name}} must run routines enable', 'warn'] };
  function confirmLine(c) {
    if (!c) return 'Not confirmed yet';
    if (c.status === 'confirmed') return `${CONFIRM_VIA[c.via] || c.via}${c.decided_at ? ' · ' + when(c.decided_at) : ''}`;
    return (CONFIRM_STATUS[c.status] || [c.status])[0] + (c.error ? `: ${c.error}` : '');
  }
  const ROUTINE_RUN_STATE = { fired: ['Fired', 'good'], skipped_in_flight: ['Skipped: previous run still going', 'warn'], skipped_daily_cap: ['Skipped: daily cap reached', 'warn'],
    failed: ['Failed', 'bad'] };
  const RUN_TRIGGER = { schedule: 'Scheduled', catch_up: 'Catch-up (a missed run is made up once)', run_now: 'Manual', smoke: 'Test' };

  function Routines() {
    const head = pageHead('Routines', 'ROUTINES · scheduled tasks',
      'routines.yaml in each task directory: a hand-written entry needs one confirmation from {{owner_name}} (the card’s [Enable] button or routines enable in the terminal; reconfirm after edits), then runs within its scope as {{owner_name}} intended; entries an agent proposes are written by code once approved on a card. A missed run is made up at most once; a run is skipped while the previous one is still going.');
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
      if (cfg.enabled === false) warns.push('The routines master switch is off (routines.enabled=false): all routines are paused');
      if (d.config_error) warns.push(`Invalid config: ${d.config_error}`);
      for (const e of d.errors || []) warns.push(`${e.task_id}: routines.yaml is invalid (none of this task’s routines run): ${e.error}`);
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, pill(cfg.enabled === false ? 'Off' : 'Running', cfg.enabled === false ? 'bad' : 'good')), h('span', {}, 'Master switch')),
          h('div', { class: 'counter' }, h('b', {}, c.active || 0), h('span', {}, 'Enabled')),
          h('div', { class: 'counter' }, h('b', {}, c.disabled || 0), h('span', {}, 'Disabled')),
          h('div', { class: 'counter' }, h('b', {}, c.awaiting_confirmation || 0), h('span', {}, 'Awaiting {{owner_name}}')),
          h('div', { class: 'counter' }, h('b', {}, (c.unapproved || 0) + (c.rejected || 0) + (c.invalid || 0)), h('span', {}, 'Unapproved / not enabled / invalid')),
          h('div', { class: 'counter' }, h('b', {}, day), h('span', {}, 'Fired, last 24 h'))),
        h('dl', { class: 'kv' },
          h('dt', {}, 'Limits'), h('dd', {}, `interval ≥ ${cfg.min_interval_minutes || 15} min · up to ${cfg.max_per_task || 20} per task · up to ${cfg.max_runs_per_day || 48} runs per task per day · files scanned every ${cfg.scan_seconds || 60} s`),
          h('dt', {}, 'Commands'), h('dd', { class: 'mono' }, 'routines list | validate | run-now | disable | enable ({{owner_name}}) · routine-propose (agent)')),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderList(d) {
      const items = d.routines || [];
      fill(table, h('h3', {}, 'Routines ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'routines-table' },
          h('thead', {}, h('tr', {}, ['Task', 'id', 'When', 'Next', 'Last', 'Status', 'Source', 'Scope / prompt'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((r) => {
            const [label, tone] = ROUTINE_STATE[r.state] || [r.state, null];
            return h('tr', { 'data-routine': `${r.task_id}/${r.routine_id}` },
              h('td', { class: 'mono' }, r.task_id, r.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, r.routine_id),
              h('td', {}, r.when || '—', h('div', { class: 'sub mono' }, r.schedule ? `cron ${r.schedule}` : (r.every ? `every ${r.every}` : '')),
                r.until ? h('div', { class: 'sub' }, 'Until ' + r.until) : null),
              h('td', { class: 'num' }, r.next_at ? when(r.next_at) : '—'),
              h('td', { class: 'num' }, r.last_fired_at ? when(r.last_fired_at) : '—'),
              h('td', {}, pill(label, tone), r.error ? h('div', { class: 'sub' }, r.error) : null),
              h('td', {}, PROVENANCE[r.provenance] || r.provenance, r.approved_by ? h('div', { class: 'sub mono' }, r.approved_by) : null,
                r.provenance === '{{owner_key}}_authored' ? h('div', { class: 'sub' }, confirmLine(r.confirmation)) : null),
              h('td', {}, r.scope || '—', h('div', { class: 'sub' }, r.prompt || '')));
          }))) : h('div', { class: 'empty' }, h('b', {}, 'No routines yet'), d.notice === 'routines_tables_missing' ? 'The daemon has not created the routines tables yet.' : 'Write routines.yaml in a task directory, or have an agent propose one with routine-propose.'));
    }
    function renderRuns(d) {
      const items = d.runs || [];
      fill(runs, h('h3', {}, 'Recent fires ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'routines-run-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Task', 'id', 'Trigger', 'Result', 'Delivery'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [label, tone] = ROUTINE_RUN_STATE[x.state] || [x.state, null];
            const delivered = x.delivery === 'event' ? ['To current session', x.event_status ? (EVENT_STATUS[x.event_status] || [x.event_status])[0] : null]
              : x.delivery === 'launch' ? ['New session', x.dispatch_state ? ((DISPATCH_STATE[x.dispatch_state] || [x.dispatch_state])[0]) : null] : ['—', null];
            return h('tr', {},
              h('td', { class: 'num' }, when(x.fired_at), x.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, x.task_id), h('td', { class: 'mono' }, x.routine_id),
              h('td', {}, RUN_TRIGGER[x.trigger] || x.trigger),
              h('td', {}, pill(label, tone), x.error ? h('div', { class: 'sub' }, x.error) : null),
              h('td', {}, delivered[0], delivered[1] ? h('div', { class: 'sub' }, delivered[1]) : null));
          }))) : h('p', { class: 'note' }, 'No fires yet.'));
    }
    function renderProposals(d) {
      const items = d.proposals || [];
      const confs = d.confirmations || [];
      fill(props, h('details', { class: 'hist', id: 'routines-confirmations' }, h('summary', {}, `Confirmations by {{owner_name}} (${confs.length})`),
        confs.length ? h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Task', 'id', 'Definition', 'Status', 'Via'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, confs.map((x) => {
            const [label, tone] = CONFIRM_STATUS[x.status] || [x.status, null];
            return h('tr', {}, h('td', { class: 'num' }, when(x.decided_at || x.created_at), x.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, x.task_id), h('td', { class: 'mono' }, x.routine_id), h('td', { class: 'mono' }, x.digest),
              h('td', {}, pill(label, tone), x.error ? h('div', { class: 'sub' }, x.error) : null),
              h('td', {}, x.via ? (CONFIRM_VIA[x.via] || x.via) : (x.card ? 'Card' : '—')));
          }))) : h('p', { class: 'note' }, 'No hand-written routines need confirmation yet.')),
        h('details', { class: 'hist', id: 'routines-proposal-list' }, h('summary', {}, `Routine proposals (${items.length})`),
        items.length ? h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Proposal', 'Task', 'id', 'Status', 'Proposer'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [label, tone] = PROPOSAL_STATUS[x.status] || [x.status, null];
            return h('tr', {}, h('td', { class: 'num' }, when(x.created_at)), h('td', { class: 'mono' }, x.id),
              h('td', { class: 'mono' }, x.task_id), h('td', { class: 'mono' }, x.routine_id),
              h('td', {}, pill(label, tone), x.error ? h('div', { class: 'sub' }, x.error) : null),
              h('td', { class: 'mono' }, short(x.proposer, 20)));
          }))) : h('p', { class: 'note' }, 'No proposals yet.')));
    }
    async function load() {
      let data;
      try { data = await api('/api/routines'); online(true); } catch (e) { online(false); return; }
      renderStatus(data); renderList(data); renderRuns(data); renderProposals(data);
    }
    every(8000, load);
  }

  // =====================================================================
  // Gaps (P3.1) — capability blockers + automation gap ledger, read-only. Labels and ids only on the list;
  // tried / unblock text on the detail page; verification codes are never stored anywhere.
  // =====================================================================
  const GAP_TYPE = { no_api: 'No API support', no_skill: 'No skill', auth_missing: 'Missing credential', auth_expired: 'Credential expired',
    mfa_code: 'Needs a verification code', permission: 'Insufficient permission', ui_only: 'Web UI only', external_owner: 'Waiting on external owner' };
  const GAP_STATUS = { open: ['Waiting for {{owner_name}} to choose', 'warn'], in_progress: ['In progress', null], resolved: ['Resolved', 'good'],
    superseded: ['Superseded by a newer report', null] };
  const GAP_RESOLUTION = { {{owner_key}}_manual: 'Handled manually by {{owner_name}}', credential_added: '{{owner_name}} added a credential', agent_browser: 'Agent logged in itself',
    skill_built: 'Built a script / skill', skipped: 'Skipped' };
  const GAP_CHOICE = { manual: '{{owner_name}} does it', credential: '{{owner_name}} adds a credential', agent_login: 'Agent logs in itself',
    agent_build: 'Agent writes a script / skill', skip: 'Skip' };
  const GAP_PROPOSAL = { posting: ['Posting card', null], pending: ['Waiting for {{owner_name}}', 'warn'], launching: ['Creating task', 'warn'],
    launched: ['Task created', 'good'], launch_failed: ['Task not created', 'bad'], rejected: ['Not now', null],
    needs_revision: ['Conditional (resubmit)', 'warn'], failed: ['Card post failed', 'bad'] };
  const typePill = (t) => pill(GAP_TYPE[t] || t, t === 'mfa_code' ? 'warn' : null);
  function gapStatus(g) {
    const [label, tone] = GAP_STATUS[g.status] || [g.status, null];
    const sub = g.status === 'in_progress' && g.waiting_on_{{owner_key}} ? 'Waiting for {{owner_name}} to finish and click “Done”'
      : g.status === 'in_progress' && g.choice ? `{{owner_name}} chose: ${GAP_CHOICE[g.choice] || g.choice}`
        : g.resolution ? GAP_RESOLUTION[g.resolution] || g.resolution : null;
    return [pill(label, tone), sub ? h('div', { class: 'sub' }, sub) : null, g.smoke ? h('div', {}, pill('TEST', 'warn')) : null];
  }
  const gapLink = (g) => h('a', { class: 'mono', href: '/gaps/' + g.id }, g.id);

  function Gaps() {
    const head = pageHead('Gaps', 'GAPS · capability blockers and automation gaps',
      'When an agent cannot do something (no API / skill, a missing or expired credential, a verification code, insufficient permission, web UI only, waiting on an external owner), report-blocker sends {{owner_name}} a card and logs a gap here; the weekly review proposes skill tasks for frequent paths. Lists show only system / operation / type / status; “What was tried / How to unblock” is on the detail page; verification codes are never stored.');
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
      if (cfg.enabled === false) warns.push('Blocker cards are off (blockers.enabled=false): report-blocker only logs gaps and sends no cards, no “Done” cards and no reminders');
      if (cfg.skill_proposals_enabled === false) warns.push('Skill proposals are off (blockers.skill_proposals_enabled=false): decided cards create no tasks for now');
      if (d.config_error) warns.push(`Invalid config: ${d.config_error}`);
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, (c.open || 0) + (c.in_progress || 0)), h('span', {}, 'Unresolved')),
          h('div', { class: 'counter' }, h('b', {}, c.waiting_on_{{owner_key}} || 0), h('span', {}, 'Waiting for {{owner_name}}')),
          h('div', { class: 'counter' }, h('b', {}, c.resolved || 0), h('span', {}, 'Resolved')),
          h('div', { class: 'counter' }, h('b', {}, c.{{owner_key}}_manual || 0), h('span', {}, 'Taken over by {{owner_name}}')),
          h('div', { class: 'counter' }, h('b', {}, c.superseded || 0), h('span', {}, 'Superseded')),
          h('div', { class: 'counter' }, h('b', {}, c.total || 0), h('span', {}, 'Total gaps (excl. TEST)'))),
        h('dl', { class: 'kv' },
          h('dt', {}, 'Switches'), h('dd', {}, pill(cfg.enabled === false ? 'Cards off' : 'Cards on', cfg.enabled === false ? 'bad' : 'good'), ' ',
            pill(cfg.skill_proposals_enabled === false ? 'Skill proposals off' : 'Skill proposals on', cfg.skill_proposals_enabled === false ? 'warn' : 'good')),
          h('dt', {}, 'Reminder'), h('dd', {}, `If nobody clicks a card within ${cfg.reminder_hours || 24} h, remind once in its thread (once per card)`),
          h('dt', {}, 'Commands'), h('dd', { class: 'mono' }, 'report-blocker · resolve-blocker · blocker-code · gaps list | show · gap-propose · smoke-blocker')),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderOpen(d) {
      const items = d.open || [];
      fill(open, h('h3', {}, 'Unresolved blockers ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'gaps-open-table' },
          h('thead', {}, h('tr', {}, ['Reported', 'ID', 'Task', 'System · operation', 'Type', 'Status', 'Waiting'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((g) => h('tr', { 'data-gap': g.id },
            h('td', { class: 'num' }, when(g.created_at), g.occurrences > 1 ? h('div', { class: 'sub' }, `reported ${g.occurrences}×`) : null),
            h('td', {}, gapLink(g), g.attempt > 1 ? h('div', { class: 'sub' }, `attempt ${g.attempt}`) : null),
            h('td', { class: 'mono task' }, g.task_id),
            h('td', {}, h('span', { class: 'mono' }, g.system), ' · ', g.operation),
            h('td', {}, typePill(g.type)),
            h('td', {}, gapStatus(g)),
            h('td', { class: 'num' }, g.age_hours === null || g.age_hours === undefined ? '—' : dur(g.age_hours * 3600),
              g.reminded ? h('div', { class: 'sub' }, 'Reminded') : null))))) :
          h('div', { class: 'empty' }, h('b', {}, 'No unresolved blockers'), d.notice === 'gaps_tables_missing' ? 'The daemon has not created the gaps tables yet.' : 'Blockers appear here once an agent reports them with report-blocker.'));
    }
    function renderPaths(d) {
      const items = d.top_paths || [];
      fill(paths, h('h3', {}, 'Most common paths ', h('span', { class: 'count' }, items.length)),
        h('p', { class: 'note' }, 'Same system + same operation = one path; the weekly review proposes a skill for paths that recur or that {{owner_name}} took over manually.'),
        items.length ? h('table', { class: 'ops-table', id: 'gaps-path-table' },
          h('thead', {}, h('tr', {}, ['System · operation', 'Reports', 'Occurrences', '{{owner_name}} manual', 'Unresolved', 'Latest'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((p) => h('tr', {},
            h('td', {}, h('span', { class: 'mono' }, p.system), ' · ', p.operation),
            h('td', { class: 'num' }, p.reports), h('td', { class: 'num' }, p.occurrences),
            h('td', { class: 'num' }, p.{{owner_key}}_manual), h('td', { class: 'num' }, p.open),
            h('td', { class: 'num' }, when(p.last_at)))))) : h('p', { class: 'note' }, 'No gaps recorded yet.'));
    }
    function renderWeeks(d) {
      const items = (d.weeks || []).slice().reverse();
      fill(weeks, h('h3', {}, 'Weekly'),
        h('table', { class: 'ops-table', id: 'gaps-week-table' },
          h('thead', {}, h('tr', {}, ['Week (from)', 'Reported', 'Resolved', '{{owner_name}} manual', 'By type'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((w) => h('tr', {},
            h('td', { class: 'num' }, fmtDay.format(new Date(w.start * 1000))),
            h('td', { class: 'num' }, w.reported), h('td', { class: 'num' }, w.resolved), h('td', { class: 'num' }, w.{{owner_key}}_manual),
            h('td', {}, Object.entries(w.by_type || {}).map(([t, n]) => pill(`${GAP_TYPE[t] || t} ${n}`, null))))))));
    }
    function renderSystems(d) {
      const items = d.by_system || [];
      fill(systems, h('h3', {}, 'By system ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'gaps-system-table' },
          h('thead', {}, h('tr', {}, ['System', 'Gaps', 'Unresolved', '{{owner_name}} manual'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((s) => h('tr', {}, h('td', { class: 'mono' }, s.system), h('td', { class: 'num' }, s.total),
            h('td', { class: 'num' }, s.open), h('td', { class: 'num' }, s.{{owner_key}}_manual))))) : null,
        h('dl', { class: 'kv' },
          h('dt', {}, 'By type'), h('dd', {}, Object.keys(d.by_type || {}).length ? Object.entries(d.by_type).map(([t, n]) => pill(`${GAP_TYPE[t] || t} ${n}`, t === 'mfa_code' ? 'warn' : null)) : '—'),
          h('dt', {}, 'Resolution'), h('dd', {}, Object.keys(d.by_resolution || {}).length ? Object.entries(d.by_resolution).map(([r, n]) => pill(`${GAP_RESOLUTION[r] || r} ${n}`, r === '{{owner_key}}_manual' || r === 'credential_added' ? 'warn' : 'good')) : '—')));
    }
    function renderRecent(d) {
      const items = d.recent || [];
      fill(recent, h('h3', {}, 'Recent blockers ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'gaps-recent-table' },
          h('thead', {}, h('tr', {}, ['Reported', 'ID', 'Task', 'System · operation', 'Type', 'Status / resolution'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((g) => h('tr', { 'data-gap': g.id },
            h('td', { class: 'num' }, when(g.created_at)), h('td', {}, gapLink(g)), h('td', { class: 'mono task' }, g.task_id),
            h('td', {}, h('span', { class: 'mono' }, g.system), ' · ', g.operation), h('td', {}, typePill(g.type)),
            h('td', {}, gapStatus(g)))))) : h('p', { class: 'note' }, 'No blockers recorded yet.'));
    }
    function renderProposals(d) {
      const items = d.proposals || [];
      fill(props, h('h3', {}, 'Skill proposals ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'gaps-proposal-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Proposal', 'System · operation', 'Status', 'Task'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [label, tone] = GAP_PROPOSAL[x.status] || [x.status, null];
            return h('tr', {}, h('td', { class: 'num' }, when(x.created_at), x.smoke ? h('div', {}, pill('TEST', 'warn')) : null),
              h('td', { class: 'mono' }, x.id), h('td', {}, h('span', { class: 'mono' }, x.system), ' · ', x.operation),
              h('td', {}, pill(label, tone), x.error ? h('div', { class: 'sub' }, x.error) : null,
                x.suppress_until ? h('div', { class: 'sub' }, 'Snoozed until ' + when(x.suppress_until) + ' (no re-proposal)') : null),
              h('td', { class: 'mono' }, x.task_id || '—'));
          }))) : h('p', { class: 'note' }, 'No skill proposals yet: the {{owner_key}}-inbox weekly review routine raises them every Monday from the gaps here.'));
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
    const crumbs = h('nav', { class: 'crumbs' }, h('a', { href: '/gaps' }, '← Gaps'));
    const [head, headStatus] = detailHead('Blocker ' + id, 'GAP · blocker details', '“What was tried / How to unblock” are notes the agent wrote (checked for credentials before the card was sent); verification codes are never stored.');
    const facts = h('section', { class: 'ops-card summary', id: 'gap-detail' });
    const texts = h('section', { class: 'ops-card', id: 'gap-texts' });
    const more = h('section', { class: 'ops-card', id: 'gap-more' });
    clear(main).append(crumbs, head, facts, texts, more);
    async function load() {
      let d;
      try { d = await api('/api/gaps/' + encodeURIComponent(id)); online(true); } catch (e) {
        online(e.status === 404);
        fill(facts, h('div', { class: 'empty' }, h('b', {}, e.status === 404 ? 'No such blocker' : 'Read failed'), h('a', { href: '/gaps' }, 'Back to Gaps')));
        return;
      }
      const g = d.gap;
      const optionLabel = (oid) => ((g.options || []).find((o) => o.id === oid) || {}).label || oid;
      const [slabel, stone] = GAP_STATUS[g.status] || [g.status, null];
      fill(headStatus, pill(slabel, stone), typePill(g.type), g.smoke ? pill('TEST', 'warn') : null);
      fill(facts, factGrid([
        ['Status', h('span', {}, slabel, gapStatus(g)[1])],
        ['Task', g.task_id, 'mono'],
        ['System · operation', h('span', {}, h('span', { class: 'mono' }, g.system), ' · ', g.operation)],
        ['Reported', h('span', {}, `${when(g.created_at)} · ${g.reporter}`, g.run_id ? h('div', { class: 'sub' }, h('a', { href: '/runs/' + g.run_id }, 'run ' + g.run_id)) : null,
          g.occurrences > 1 ? h('div', { class: 'sub' }, `Same path reported ${g.occurrences}× (no duplicate card sent)`) : null)],
        ['Attempt', h('span', {}, `attempt ${g.attempt}`, g.previous_id ? [' · previous ', h('a', { class: 'mono', href: '/gaps/' + g.previous_id }, g.previous_id)] : null,
          g.superseded_by ? [' · superseded by ', h('a', { class: 'mono', href: '/gaps/' + g.superseded_by }, g.superseded_by), ''] : null)],
        ['{{owner_name}}’s choice', h('span', {}, g.choice ? `${optionLabel(g.choice)} · ${when(g.chosen_at)}` : 'Not chosen yet',
          g.stage2_choice ? h('div', { class: 'sub' }, `Second card: ${g.stage2_choice === 'done' ? 'Done' : 'Can’t do'} · ${when(g.stage2_at)}`) : null)],
        ['Resolution', h('span', {}, g.resolution ? `${GAP_RESOLUTION[g.resolution] || g.resolution} · ${when(g.resolved_at)} · ${g.resolved_by || ''}` : '—',
          g.{{owner_key}}_manual ? h('div', {}, pill('Taken over by {{owner_name}}', 'warn')) : null,
          g.resolution_note ? h('div', { class: 'sub' }, g.resolution_note) : null)],
      ]));
      fill(texts, h('h3', {}, 'Notes'), kv([
        ['What was tried', g.tried, 'gap-text'],
        ['How to unblock', g.unblock, 'gap-text'],
      ]));
      fill(more, h('h3', {}, 'Cards and options'), kv([
        ['Cards', (g.cards || []).length ? h('span', {}, g.cards.map((c) => h('div', {}, c.stage === 1 ? 'Blocker card ' : '“Done” card ', ext(c.slack_url, 'Slack ↗'), ' · ', when(c.posted_at))),
          (g.reminders || []).length ? h('div', { class: 'sub' }, 'Reminded: ' + g.reminders.map(when).join(', ')) : null) : (g.card_error ? `Not sent: ${g.card_error}` : '—')],
        ['Options', (g.options || []).length ? h('span', {}, g.options.map((o) => pill(`${o.label} → ${GAP_RESOLUTION[o.resolution] || o.resolution}`, null))) : '—'],
      ]));
    }
    every(15000, load);
  }

  // =====================================================================
  // Learning (P35: learning rules from card feedback) — aggregates only, never raw feedback or the distillation text
  // =====================================================================
  const LEARN_CARD = { ask_{{owner_key}}: 'Ask {{owner_name}}', rule_check: 'rule-check', rule_proposal: 'Rule proposal',
    routine_proposal: 'Routine proposal', routine_confirmation: 'Routine confirmation', skill_proposal: 'Skill proposal',
    blocker_stage1: 'Blocker', blocker_stage2: 'Blocker (done)', intake_brief: 'Intake brief', intake_draft: 'Intake draft',
    orphan: 'Orphan task', deadline: 'Task deadline', decision: 'Decision card' };
  const LEARN_SIGNAL = { override_directive: 'Override + generalizable directive', repeat_correction: '≥2nd correction of the same kind',
    override: 'Overrode recommendation', directive: 'Generalizable directive' };
  const PROMOTE_STATE = { posting: ['Posting card', null], open: ['Waiting for {{owner_name}}', 'warn'], resolved: ['Handled', 'good'],
    failed: ['Card post failed', 'bad'] };
  const PROMOTE_CHOICE = { distill: ['Distill into a rule', 'good'], log: ['Log this once', null], drop: ['Drop', null],
    distill_failed: ['Distill hand-off failed', 'bad'] };
  const SIGNAL_LABEL = (s) => LEARN_SIGNAL[s] || s;
  function Learning() {
    const head = pageHead('Learning', 'LEARNING · rules from card feedback',
      'Every card to {{owner_name}} that gets settled logs one interaction record (who sent what → how the system handled it → {{owner_name}}’s choice + whether it overrode the recommendation). When the signal is clear'
      + ' (the recommendation was overridden with generalizable wording, or the same kind was corrected a 2nd time or more), a “Make this a rule?” card is sent; adopting goes through the normal rule proposal and still needs'
      + ' one click from {{owner_name}}. Only aggregates are shown here: the raw feedback stays in a private redacted corpus, never in the database or on the dashboard.');
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
      if (cfg.enabled === false) warns.push('Learning from card feedback is off (feedback_learning.enabled=false): nothing is collected and no promote cards are sent');
      if (d.config_error) warns.push(`Invalid config: ${d.config_error}`);
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, c.total || 0), h('span', {}, 'Interaction records')),
          h('div', { class: 'counter' }, h('b', {}, pctText(c.override_rate)), h('span', {}, '{{owner_name}} override rate')),
          h('div', { class: 'counter' }, h('b', {}, p.posted || 0), h('span', {}, 'Promote cards')),
          h('div', { class: 'counter' }, h('b', {}, p.open || 0), h('span', {}, 'Waiting for {{owner_name}}')),
          h('div', { class: 'counter' }, h('b', {}, p.distill || 0), h('span', {}, 'Distilled into rules')),
          h('div', { class: 'counter' }, h('b', {}, pctText(c.adoption_rate)), h('span', {}, 'Promote rate (distill / decided)'))),
        h('dl', { class: 'kv' },
          h('dt', {}, 'Switches'), h('dd', {}, pill(cfg.enabled === false ? 'Collection off' : 'Collection on', cfg.enabled === false ? 'bad' : 'good'), ' ',
            pill('Strong signal: ' + (cfg.strong_signal_mode || '—'), cfg.strong_signal_mode === 'conservative' ? 'good' : 'warn'), ' ',
            pill(`Daily cap ${cfg.max_promote_cards_per_day ?? '—'}`, null), ' ',
            pill(`Rejection cooldown ${cfg.suppress_days ?? '—'} d`, null)),
          h('dt', {}, 'By card type'), h('dd', {}, Object.keys(c.by_card_type || {}).length
            ? Object.entries(c.by_card_type).sort((a, b) => b[1] - a[1]).map(([t, n]) => pill(`${LEARN_CARD[t] || t} ${n}`, null)) : '—'),
          h('dt', {}, 'Strong signals'), h('dd', {}, Object.keys(p.by_signal || {}).length
            ? Object.entries(p.by_signal).map(([s, n]) => pill(`${SIGNAL_LABEL(s)} ${n}`, 'warn')) : '—'),
          h('dt', {}, 'Privacy'), h('dd', {}, 'Raw feedback stays only in the private redacted corpus ({{feedback_corpus}}, 0600); the database stores only distillations + ids'),
          h('dt', {}, 'Commands'), h('dd', { class: 'mono' }, 'interaction-distill · learning-review · smoke-feedback-learning')),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderWeeks(d) {
      const items = (d.weeks || []).slice().reverse();
      fill(weeks, h('h3', {}, 'Weekly'),
        h('table', { class: 'ops-table', id: 'learning-week-table' },
          h('thead', {}, h('tr', {}, ['Week (from)', 'Interactions', '{{owner_name}} overrides', 'With directive', 'Promote cards', 'Distilled'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((w) => h('tr', {},
            h('td', { class: 'num' }, fmtDay.format(new Date(w.start * 1000))),
            h('td', { class: 'num' }, w.records), h('td', { class: 'num' }, w.overrides), h('td', { class: 'num' }, w.directive),
            h('td', { class: 'num' }, w.promotes), h('td', { class: 'num' }, w.distill))))));
    }
    function renderShapes(d) {
      const items = d.shapes || [];
      fill(shapes, h('h3', {}, 'Most common handling patterns ', h('span', { class: 'count' }, items.length)),
        h('p', { class: 'note' }, 'shape = card type + system / category + the agent’s action; shapes with many overrides or directives are the best candidates for a rule.'),
        items.length ? h('table', { class: 'ops-table', id: 'learning-shape-table' },
          h('thead', {}, h('tr', {}, ['Workflow (shape)', 'Type', 'Count', '{{owner_name}} overrides', 'With directive', 'Latest'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((s) => h('tr', {},
            h('td', {}, s.label), h('td', {}, pill(LEARN_CARD[s.card_type] || s.card_type, null)),
            h('td', { class: 'num' }, s.count), h('td', { class: 'num' }, s.overrides), h('td', { class: 'num' }, s.directive),
            h('td', { class: 'num' }, when(s.last_at)))))) :
          h('div', { class: 'empty' }, h('b', {}, 'No interaction records yet'),
            d.notice === 'learning_tables_missing' ? 'The daemon has not created the learning tables yet.' : 'Each card to {{owner_name}} appears here once it is settled.'));
    }
    function renderPromotes(d) {
      const items = d.promotes || [];
      fill(promotes, h('h3', {}, '“Make this a rule?” cards ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'learning-promote-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Workflow', 'Signal', 'Status', '{{owner_name}}’s choice'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [sl, st] = PROMOTE_STATE[x.state] || [x.state, null];
            const [cl, ct] = x.choice ? (PROMOTE_CHOICE[x.choice] || [x.choice, null]) : ['—', null];
            return h('tr', {}, h('td', { class: 'num' }, when(x.created_at)), h('td', {}, x.label),
              h('td', {}, pill(SIGNAL_LABEL(x.signal), 'warn')), h('td', {}, pill(sl, st)), h('td', {}, pill(cl, ct)));
          }))) : h('p', { class: 'note' }, 'No promote cards yet: {{owner_name}} is asked whether to turn a kind of handling into a rule only when the signal is clear (conservative).'),
        h('p', { class: 'note' }, 'Adopt or decline it on the ', h('a', { href: '/rules' }, 'Rules'), ' page under “Rule proposals” (promotion uses the normal rule-propose card).'));
    }
    async function load() {
      let data;
      try { data = await api('/api/learning'); online(true); } catch (e) { online(false); return; }
      renderStatus(data); renderWeeks(data); renderShapes(data); renderPromotes(data);
    }
    every(8000, load);
  }

  // =====================================================================
  // Health (P38: daemon health) — provider status, stranded deliveries, stuck claims. Counts, ids and states only.
  // =====================================================================
  const HEALTH_STATUS = { healthy: ['Healthy', 'good'], degraded: ['Overloaded · rerouted', 'bad'] };
  const HEALTH_EVENT = { degraded: 'Became overloaded', healthy: 'Back to healthy', notice_degraded: 'Notified {{owner_name}}: overloaded',
    notice_healthy: 'Notified {{owner_name}}: recovered' };
  const SWITCH_WHY = { long_lived: 'Long-lived task: new session per event', context_cap: 'Context at cap: new session', auto_return: 'Back to preferred provider',
    degraded_reroute: 'Preferred provider overloaded: temporary reroute', stuck_on_degraded: 'Stuck on an overloaded provider: switched',
    ttft_timeout: 'No first token in time: next provider', error_grace: 'API errors did not recover: next provider' };
  const STRANDED_STATE = { suspect: ['Session exited · confirming', 'warn'], held: ['Handed to the task', 'good'],
    rerouted: ['Redelivered to a live session', 'good'], unowned: ['Owning task not found', 'bad'], handled: ['Handled', 'good'] };
  const STUCK_STATE = { posting: ['Posting card', null], open: ['Waiting for {{owner_name}}', 'warn'], resolved: ['Handled', 'good'],
    cleared: ['Session settled on its own', 'good'], failed: ['Card post failed', 'bad'] };
  const STUCK_CHOICE = { requeue: 'Redeliver', settle: 'Mark as handled', later: 'Leave for now' };
  const PROBE_RESULT = { ok: ['Recovered', 'good'], down: ['Not recovered yet', 'bad'], other: ['Not an outage (the run decides)', 'warn'] };  // P44
  // P38 (F49): the private-destination guard's refusals (codes and channel ids only).
  const CARD_DEST_VERDICT = { private: ['Private channel', 'good'], im: ['DM with {{owner_name}}', 'good'], public: ['Public channel', 'bad'],
    mpim: ['Group DM', 'bad'], ext_shared: ['Shared with an external org', 'bad'], im_not_{{owner_key}}: ['Not a DM with {{owner_name}}', 'bad'], error: ['Check failed', 'bad'] };
  const CARD_DEST_REASON = { public: 'Channel is public', mpim: 'Is a group DM', ext_shared: 'Channel is shared with an external org', im_not_{{owner_key}}: 'Not a DM with {{owner_name}}',
    not_a_configured_destination: 'Not a configured decision channel', missing_scope: 'Bot lacks permission to read channel info', channel_not_found: 'Channel not found (bot not in it, or wrong id)',
    not_in_channel: 'Bot not in channel', invalid_auth: 'Bot token invalid', account_inactive: 'Bot account deactivated', no_channel_info: 'Slack returned no channel info' };
  function Health() {
    const head = pageHead('Health', 'HEALTH · daemon health',
      'How each provider did over a rolling window: an overload / dropped stream / retry flag, a failure, or a long-lived task running too slow each count as one “problem”; at the threshold the provider is marked overloaded,'
      + ' new sessions use a healthy provider, it switches back to the preferred one after recovery, and {{owner_name}} gets a notice (one per overload / recovery). Below are also messages delivered to exited sessions'
      + ' and never consumed (handed back to the owning task automatically), and sessions that claimed messages but never settled, blocking later deliveries (a card asks {{owner_name}}; never released automatically).');
    const status = h('section', { class: 'ops-card', id: 'health-status' });
    const providers = h('section', { class: 'ops-card', id: 'health-providers' });
    const switches = h('section', { class: 'ops-card', id: 'health-switches' });
    const stranded = h('section', { class: 'ops-card', id: 'health-stranded' });
    const stuck = h('section', { class: 'ops-card', id: 'health-stuck' });
    const cards = h('section', { class: 'ops-card', id: 'health-card-destination' });
    const cache = h('section', { class: 'ops-card', id: 'health-cache' });
    const wait = h('section', { class: 'ops-card', id: 'health-provider-wait' });
    const sys = h('section', { class: 'ops-card', id: 'health-system' });  // 0.78.6 (F18)
    clear(main).append(head, status, sys, cards, providers, wait, cache, switches, stranded, stuck);

    // 0.78.6 (F18): the host's disk / memory / CPU / processes (kage.core.system_status), the stored directory sizes and
    // the threshold crossings. Sizes and counts only.
    const SYS_LEVEL = { ok: ['Normal', 'good'], warning: ['High', 'warn'], critical: ['Critical', 'bad'] };
    const SYS_EVENT = {"warning": "over the warning line", "critical": "over the critical line", "ok": "back to normal", "notice_warning": "notified: high", "notice_critical": "notified: critical", "notice_recovered": "notified: recovered"};
    const SYS_METRIC = {"disk": "Disk", "memory": "Memory"};
    const SYS_HOLDS = {"home": "home", "state": "state database"};
    const sfmt = (t, v) => t.replace(/\{(\w+)\}/g, (_, k) => (v[k] === undefined || v[k] === null ? '—' : v[k]));
    function bytes(n) {
      if (n === null || n === undefined) return '—';
      const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
      let i = 0;
      while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
      return (i ? n.toFixed(n >= 100 ? 0 : 1) : String(Math.round(n))) + ' ' + u[i];
    }
    const pctText = (p) => (p === null || p === undefined ? '—' : `${Math.round(p)}%`);
    function meter(name, pct, level, ...lines) {
      const bar = h('i', {});
      bar.style.width = `${Math.max(0, Math.min(100, pct || 0))}%`;
      return h('div', { class: 'sys-meter', 'data-level': level || 'ok' },
        h('div', { class: 'sys-meter-head' }, h('span', {}, name), h('b', {}, pctText(pct))),
        h('div', { class: 'sys-meter-bar', 'aria-hidden': 'true' }, bar),
        lines.filter(Boolean).map((t) => h('small', {}, t)));
    }
    function navAlert(level) {
      const nav = document.querySelector('.site-nav [data-nav="health"]');
      if (!nav) return;
      if (level === 'warning' || level === 'critical') nav.dataset.alert = level; else delete nav.dataset.alert;
    }
    function renderSystem(d) {
      const s = d.system;
      if (!s || s.error) { fill(sys, h('h3', {}, 'System'), h('p', { class: 'note' }, s && s.error ? s.error : '—')); navAlert(null); return; }
      const lv = s.levels || {};
      const th = s.thresholds || {};
      const disks = s.disks || [];
      const full = disks.slice().sort((a, b) => (b.pct || 0) - (a.pct || 0))[0] || {};
      const mem = s.memory || {};
      const cpu = s.cpu || {};
      const pr = s.processes || {};
      const [label, tone] = SYS_LEVEL[s.alert] || [s.alert, null];
      const errs = Object.entries(s.stored || {}).filter(([, a]) => a.notice_error).map(([m, a]) => `${SYS_METRIC[m] || m}: ${sfmt('The notice was not sent (retried next pass): {e}', { e: a.notice_error })}`);
      fill(sys, h('h3', {}, 'System ', pill(label, tone)),
        h('p', { class: 'note' }, sfmt("Disk, memory and CPU of this machine (in a container: the cgroup usage and limits). When disk or memory crosses a system.* warning / critical line, {{owner_name}} gets one notice (one per crossing, and one when it is back 3 points under the warning line). Directory sizes are counted every {m} minutes.", { m: s.consumers_minutes || 60 })),
        s.enabled === false ? h('div', { class: 'warn-box', role: 'status' }, 'Checks off (system.enabled=false): numbers only, no notices') : null,
        errs.length ? h('div', { class: 'warn-box', role: 'status' }, errs.map((t) => h('div', {}, t))) : null,
        h('div', { class: 'sys-meters' },
          meter('Disk', full.pct, lv.disk, full.mount ? `${full.mount} · ${sfmt('{free} free of {total}', { free: bytes(full.free), total: bytes(full.total) })}` : null,
            full.inodes_pct !== null && full.inodes_pct !== undefined ? sfmt('inodes {p}%', { p: Math.round(full.inodes_pct) }) : null),
          meter('Memory', mem.pct, lv.memory, sfmt('{used} of {limit}', { used: bytes(mem.used), limit: bytes(mem.limit) }) + (mem.limit && !mem.limited ? ' (no limit: the whole machine)' : ''),
            mem.oom_kills ? sfmt('{n} OOM kills', { n: mem.oom_kills }) : null, mem.source ? sfmt('source: {s}', { s: mem.source.replace('cgroup', 'cgroup v') }) : null),
          meter('CPU', cpu.pct, 'ok', cpu.cores_used !== null && cpu.cores_used !== undefined ? sfmt('{used} of {limit} cores', { used: cpu.cores_used, limit: cpu.limit_cores }) : sfmt('{used} of {limit} cores', { used: '—', limit: cpu.limit_cores }) + ' (usage appears after the second read)',
            cpu.loadavg ? sfmt('load {a} / {b} / {c}', { a: cpu.loadavg[0], b: cpu.loadavg[1], c: cpu.loadavg[2] }) : null)),
        disks.length > 1 ? h('table', { class: 'ops-table', id: 'health-system-disks' },
          h('thead', {}, h('tr', {}, ['Filesystem', 'Holds', 'Used', 'Free', 'Share', 'Inodes'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, disks.map((x) => h('tr', {}, h('td', { class: 'mono' }, x.mount), h('td', {}, (x.holds || []).map((k) => SYS_HOLDS[k] || k).join(' · ')),
            h('td', { class: 'num' }, bytes(x.used)), h('td', { class: 'num' }, bytes(x.free)), h('td', { class: 'num' }, pctText(x.pct)),
            h('td', { class: 'num' }, pctText(x.inodes_pct)))))) : null,
        h('h3', {}, 'Largest directories ', s.consumers_at ? h('span', { class: 'count' }, sfmt('counted {t}', { t: when(s.consumers_at) })) : null),
        (s.consumers || []).length ? h('table', { class: 'ops-table', id: 'health-system-dirs' },
          h('thead', {}, h('tr', {}, ['Directory', 'Size', 'Files'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, s.consumers.map((c) => h('tr', {}, h('td', { class: 'mono' }, c.path),
            h('td', { class: 'num' }, bytes(c.bytes) + (c.partial ? ' (time ran out: at least this much)' : '')), h('td', { class: 'num' }, Number(c.files || 0).toLocaleString('en-US')))))) :
          h('p', { class: 'note' }, 'Not counted yet: shown after the first pass once the supervisor checks.'),
        h('dl', { class: 'kv' },
          h('dt', {}, 'Processes'), h('dd', {}, [pr.total !== null && pr.total !== undefined ? pill(sfmt('{n} processes', { n: pr.total }), null) : null,
            pr.kage_children !== null && pr.kage_children !== undefined ? pill(sfmt('{n} Kage child processes', { n: pr.kage_children }), null) : null,
            pr.agent_runs !== null && pr.agent_runs !== undefined ? pill(sfmt('{n} background runs', { n: pr.agent_runs }), null) : null,
            pr.pids_max ? pill(sfmt('pids {a} of {b}', { a: pr.pids_current, b: pr.pids_max }), null) : null].filter(Boolean).map((x, i) => (i ? [' ', x] : x))),
          h('dt', {}, 'Thresholds'), h('dd', {}, pill(sfmt('disk warning {w}% · critical {c}%', { w: th.disk_warn_pct, c: th.disk_crit_pct }), null), ' ',
            pill(sfmt('memory warning {w}% · critical {c}%', { w: th.memory_warn_pct, c: th.memory_crit_pct }), null), ' ', pill(sfmt('checked every {m} minutes', { m: s.check_minutes }), null)),
          h('dt', {}, 'Commands'), h('dd', { class: 'mono' }, 'config set system.disk_warn_pct 85 · config set system.watch_paths \'["~/src"]\' · config set system.enabled false')),
        h('h3', {}, 'Recent crossings / recoveries ', h('span', { class: 'count' }, (s.events || []).length)),
        (s.events || []).length ? h('table', { class: 'ops-table', id: 'health-system-events' },
          h('thead', {}, h('tr', {}, ['Time', 'Item', 'Change', 'Value'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, s.events.map((e) => h('tr', {}, h('td', { class: 'num' }, when(e.at)), h('td', {}, SYS_METRIC[e.metric] || e.metric),
            h('td', {}, pill(SYS_EVENT[e.kind] || e.kind, /critical/.test(e.kind) ? 'bad' : /warning/.test(e.kind) ? 'warn' : 'good')),
            h('td', { class: 'num' }, e.detail && e.detail.value !== undefined ? pctText(e.detail.value) : '—'))))) :
          h('p', { class: 'note' }, 'No crossings yet.'));
      navAlert(s.alert);
    }

    function renderStatus(d) {
      const cfg = d.config || {};
      const ph = cfg.provider_health || {};
      const wd = cfg.watchdog || {};
      const ps = (d.providers || {}).providers || [];
      const sc = (d.stranded || {}).counts || {};
      const st = d.stuck_claims || {};
      const warns = [];
      if (ph.enabled === false) warns.push('Provider health checks are off (dispatch.provider_health.enabled=false): no rerouting, no notices, no automatic switch-back');
      if (cfg.stranded_revive === false) warns.push('Stranded-delivery rescue is off (dispatch.stranded_revive=false)');
      if (cfg.stuck_claim_minutes === 0) warns.push('Stuck-claim alerts are off (dispatch.stuck_claim_minutes=0)');
      if (d.config_error) warns.push(`Invalid config: ${d.config_error}`);
      if (d.notice === 'health_tables_missing') warns.push('The daemon has not created the health tables yet (they appear after restarting {{service_label}}).');
      fill(status,
        h('div', { class: 'counters' },
          ps.map((p) => h('div', { class: 'counter' }, h('b', {}, (HEALTH_STATUS[p.status] || [p.status])[0]),
            h('span', {}, `${p.label} · ${p.model}`))),
          h('div', { class: 'counter' }, h('b', {}, sc.suspect || 0), h('span', {}, 'Delivered to exited session (confirming)')),
          h('div', { class: 'counter' }, h('b', {}, (sc.held || 0) + (sc.rerouted || 0) + (sc.handled || 0)), h('span', {}, 'Rescued in 24 h')),
          h('div', { class: 'counter' }, h('b', {}, sc.unowned || 0), h('span', {}, 'Owning task not found')),
          h('div', { class: 'counter' }, h('b', {}, (st.blocked || []).length), h('span', {}, 'Sessions with stuck claims')),
          h('div', { class: 'counter' }, h('b', {}, st.open_claims || 0), h('span', {}, 'Unsettled claims'))),
        h('dl', { class: 'kv' },
          h('dt', {}, 'Health checks'), h('dd', {}, pill(ph.enabled === false ? 'Off' : 'On', ph.enabled === false ? 'bad' : 'good'), ' ',
            pill(`Window ${ph.window_minutes ?? '—'} min`, null), ' ', pill(`${ph.degrade_threshold ?? '—'} problems → overloaded`, null), ' ',
            pill(`${ph.recover_checks ?? '—'} clean checks in a row → recovered`, null), ' ', pill(`Long-lived task ≥ ${ph.slow_run_minutes ?? '—'} min = slow`, null)),
          h('dt', {}, 'Fast failover'), h('dd', {}, pill(wd.ttft_seconds ? `First token within ${wd.ttft_seconds} s (Claude)` : 'First-token watchdog off', wd.ttft_seconds ? null : 'warn'), ' ',
            pill(wd.error_grace_seconds ? `After an API error: recover within ${wd.error_grace_seconds} s (Claude / Codex)` : 'Error-grace watchdog off', wd.error_grace_seconds ? null : 'warn'), ' ',
            pill('Else the next provider takes over', null)),
          h('dt', {}, 'Bounded context'), h('dd', {}, pill(cfg.long_lived_fresh === false ? 'Long-lived tasks resume as before' : 'Long-lived tasks: new session per event',
            cfg.long_lived_fresh === false ? 'warn' : 'good'), ' ',
            pill(cfg.max_session_context_mb ? `Session context cap ${cfg.max_session_context_mb} MB` : 'Context cap off', cfg.max_session_context_mb ? null : 'warn'), ' ',
            (cfg.long_lived_tasks || []).length ? pill('Also long-lived: ' + cfg.long_lived_tasks.join(', '), null) : null),
          h('dt', {}, 'Delivery'), h('dd', {}, pill(cfg.stranded_revive === false ? 'Rescue off' : 'Rescue on', cfg.stranded_revive === false ? 'bad' : 'good'), ' ',
            pill(cfg.stuck_claim_minutes ? `Claim unsettled > ${cfg.stuck_claim_minutes} min → card` : 'Stuck-claim alerts off', cfg.stuck_claim_minutes ? null : 'warn')),
          h('dt', {}, 'Commands'), h('dd', { class: 'mono' }, 'health · health --sweep · config set dispatch.provider_health.enabled false')),
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderCards(d) {
      const cd = d.card_destination;
      if (!cd) { fill(cards, h('h3', {}, 'Card channel check'), h('p', { class: 'note' }, 'No data yet.')); return; }
      const last = cd.last || {};
      const aw = cd.awaiting_repost || {};
      const dests = cd.destinations || [];
      const why = (code) => (code ? (CARD_DEST_REASON[code] ? `${CARD_DEST_REASON[code]} (${code})` : code) : '—');
      const line = cd.refused_total
        ? `Card channel check: refused ${cd.refused_24h}× in 24 h (${cd.refused_total}× in total), last at ${when(last.at)}, reason: ${why(last.reason)}. Once fixed, run card-destination-check --refresh to check again.`
        : last.at ? `Card channel check: last refused at ${when(last.at)}, reason: ${why(last.reason)}. Once fixed, run card-destination-check --refresh to check again.`
          : 'Card channel check: no card has been refused yet.';
      const [label, tone] = { refusing: ['Refusing', 'bad'], refused_recently: ['Refused recently', 'warn'] }[cd.status] || ['OK', 'good'];
      const notes = [line];
      if (cd.config_error) notes.push(`Decision channel config invalid: ${cd.config_error}`);
      if ((aw.breaker_cards || 0) + (aw.failure_cards || 0)) {
        notes.push(`${aw.breaker_cards || 0} circuit breaker card(s) and ${aw.failure_cards || 0} failure card(s) were refused; they are re-sent automatically once the channel check passes.`);
      }
      fill(cards, h('h3', {}, 'Card channel check ', pill(label, tone)),
        h('p', { class: 'note' }, 'Before any card or notice to {{owner_name}} goes out, Slack must confirm the target is a private channel or a DM with {{owner_name}} (group DMs and channels shared with external orgs are not allowed); if that cannot be confirmed, nothing is sent and it is logged here.'),
        cd.status === 'ok' ? h('p', { class: 'note' }, line) : h('div', { class: 'warn-box', role: 'status' }, notes.map((t) => h('div', {}, t))),
        last.at ? h('dl', { class: 'kv' }, h('dt', {}, 'Last refused'), h('dd', {}, when(last.at), ' ', h('span', { class: 'mono' }, last.kind || '—'), ' → ',
          h('span', { class: 'mono' }, last.channel || '—'), ' · ', why(last.reason))) : null,
        dests.length ? h('table', { class: 'ops-table', id: 'health-card-destination-table' },
          h('thead', {}, h('tr', {}, ['Channel', 'Config key', 'Slack verdict', 'Checked at'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, dests.map((x) => {
            const [vl, vt] = x.verdict ? (CARD_DEST_VERDICT[x.verdict] || [x.verdict, 'bad']) : ['Not checked yet', null];
            return h('tr', {}, h('td', { class: 'mono' }, x.channel), h('td', { class: 'mono' }, x.source),
              h('td', {}, pill(vl, vt), x.error ? ' ' + why(x.error) : null,
                x.transient ? [' ', pill(`Last recheck hit a transient error (${x.transient}); keeping the previous verdict`, 'warn')] : null),
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
          h('thead', {}, h('tr', {}, ['provider', 'Status', 'Since', 'Runs in window', 'Problems', 'Overload / dropped', 'Failed', 'Too slow', 'Retried', 'By model'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((p) => {
            const [label, tone] = HEALTH_STATUS[p.status] || [p.status, null];
            const w = p.window || {};
            return h('tr', {}, h('td', {}, provChip(p.provider), ' ', h('span', { class: 'mono' }, p.model)), h('td', {}, pill(label, tone)),
              h('td', { class: 'num' }, when(p.since)), h('td', { class: 'num' }, w.runs ?? 0), h('td', { class: 'num' }, w.adverse ?? 0),
              h('td', { class: 'num' }, w.overload ?? 0), h('td', { class: 'num' }, w.failed ?? 0), h('td', { class: 'num' }, w.slow ?? 0),
              h('td', { class: 'num' }, w.retried ?? 0),
              h('td', {}, (w.models || []).map((m) => pill(`${m.model} ${m.adverse}/${m.runs}`, m.adverse ? 'warn' : null))));
          }))) : h('div', { class: 'empty' }, h('b', {}, 'No data yet'), 'Appears after the daemon runs a health check.'),
        h('h3', {}, 'Recent changes ', h('span', { class: 'count' }, events.length)),
        events.length ? h('table', { class: 'ops-table', id: 'health-event-table' },
          h('thead', {}, h('tr', {}, ['Time', 'provider', 'Change', 'Window'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, events.map((e) => h('tr', {}, h('td', { class: 'num' }, when(e.at)), h('td', {}, provChip(e.provider)),
            h('td', {}, pill(HEALTH_EVENT[e.kind] || e.kind, e.kind.endsWith('degraded') ? 'bad' : 'good')),
            h('td', { class: 'num' }, e.detail ? `${e.detail.adverse ?? 0}/${e.detail.runs ?? 0}` : '—'))))) :
          h('p', { class: 'note' }, 'No overload / recovery records yet.'),
        h('h3', {}, 'Tasks temporarily off their preferred provider (fallback / reroute) ', h('span', { class: 'count' }, displaced.length)),
        displaced.length ? h('table', { class: 'ops-table', id: 'health-displaced-table' },
          h('thead', {}, h('tr', {}, ['Task', 'Now', 'Preferred'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, displaced.map((t) => h('tr', {}, h('td', { class: 'mono' }, t.task_id), h('td', {}, provChip(t.current_provider)),
            h('td', {}, provChip(t.preferred)))))) :
          h('p', { class: 'note' }, 'None: when the preferred provider is healthy, the next dispatch switches back automatically.'));
    }
    // P44: dispatches waiting for a provider to come back, and the one probe per provider that releases them.
    function renderWait(d) {
      const pw = d.provider_wait;
      const cfg = d.config || {};
      if (!pw) { fill(wait, h('h3', {}, 'Waiting for AI recovery'), h('p', { class: 'note' }, 'No data yet.')); return; }
      const ps = pw.providers || {};
      const off = cfg.provider_wait_hours === 0 || cfg.probe_enabled === false;
      fill(wait, h('h3', {}, 'Waiting for AI recovery ', h('span', { class: 'count' }, pw.waiting || 0)),
        h('p', { class: 'note' }, off ? 'Off: when a provider errors and neither retries nor a provider switch helps, a failure card is sent right away.'
          : `When a provider errors and neither retries nor a provider switch helps, no card is sent; it waits up to ${cfg.provider_wait_hours ?? 48} h, probing each provider with one tiny request at a time, and carries on automatically once it recovers.`),
        pw.waiting ? h('p', { class: 'note', id: 'health-wait-oldest' }, `${pw.waiting} dispatch(es) waiting; the oldest since ${when(pw.oldest_wait_since)}, ${pw.oldest_wait_hours} h so far.`) : null,
        h('table', { class: 'ops-table', id: 'health-probe-table' },
          h('thead', {}, h('tr', {}, ['provider', 'Last probe', 'Result', 'Failures in a row', 'Next probe', 'Waiting dispatches'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, Object.keys(ps).map((k) => {
            const p = ps[k];
            const [label, tone] = p.probing ? ['Probing', null] : (PROBE_RESULT[p.result] || ['Not probed yet', null]);
            return h('tr', {}, h('td', {}, provChip(k), p.model ? [' ', h('span', { class: 'mono' }, p.model)] : null),
              h('td', { class: 'num' }, when(p.last_probe_at)),
              h('td', {}, pill(label, tone), p.error_class ? ' ' + (ERROR_LABEL[p.error_class] || p.error_class) : null),
              h('td', { class: 'num' }, p.fails || 0), h('td', { class: 'num' }, when(p.next_probe_at)), h('td', { class: 'num' }, p.waiting || 0));
          }))));
    }
    function renderSwitches(d) {
      const items = (d.providers || {}).switches || [];
      fill(switches, h('h3', {}, 'Session / provider switches ', h('span', { class: 'count' }, items.length)),
        items.length ? h('table', { class: 'ops-table', id: 'health-switch-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Task', 'Reason', 'From', 'To'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => h('tr', {}, h('td', { class: 'num' }, when(x.at)), h('td', { class: 'mono' }, x.task_key),
            h('td', {}, pill(SWITCH_WHY[x.reason] || x.reason, x.reason === 'long_lived' ? null : 'warn')),
            h('td', {}, x.from_provider ? provChip(x.from_provider) : '—'), h('td', {}, x.to_provider ? provChip(x.to_provider) : '—'))))) :
          h('p', { class: 'note' }, 'No records yet.'));
    }
    function renderStranded(d) {
      const items = (d.stranded || {}).recent || [];
      fill(stranded, h('h3', {}, 'Messages delivered to exited sessions ', h('span', { class: 'count' }, items.length)),
        h('p', { class: 'note' }, 'A message was queued for a session that exited without consuming it: once two checks in a row confirm the session is really gone (using the runtime’s own liveness check, so interactive sessions are not misjudged),'
          + ' it is handed to the owning task (looked up from the card if needed), which starts a new background run to handle it, respecting the concurrency cap and one consumer per task.'),
        items.length ? h('table', { class: 'ops-table', id: 'health-stranded-table' },
          h('thead', {}, h('tr', {}, ['First seen', 'Task', 'provider', 'Delivery status', 'Handling'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, items.map((x) => {
            const [label, tone] = STRANDED_STATE[x.state] || [x.state, null];
            return h('tr', {}, h('td', { class: 'num' }, when(x.first_seen)), h('td', { class: 'mono' }, x.task_key || '—'),
              h('td', {}, provChip(x.provider)), h('td', {}, statusPill(x.status)), h('td', {}, pill(label, tone)));
          }))) : h('p', { class: 'note' }, 'None.'));
    }
    function renderStuck(d) {
      const st = d.stuck_claims || {};
      const blocked = st.blocked || [];
      const cards = st.cards || [];
      fill(stuck, h('h3', {}, 'Sessions that claimed but never settled, blocking later deliveries ', h('span', { class: 'count' }, blocked.length)),
        blocked.length ? h('table', { class: 'ops-table', id: 'health-blocked-table' },
          h('thead', {}, h('tr', {}, ['Session', 'Claimed, unsettled', 'Oldest claim', 'Queued behind'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, blocked.map((b) => h('tr', {}, h('td', { class: 'mono' }, short(b.session)), h('td', { class: 'num' }, b.claimed),
            h('td', { class: 'num' }, dur(b.oldest_age) + ' ago'), h('td', { class: 'num' }, b.queued_behind))))) :
          h('p', { class: 'note' }, 'None.'),
        h('h3', {}, 'Stuck-claim cards ', h('span', { class: 'count' }, cards.length)),
        cards.length ? h('table', { class: 'ops-table', id: 'health-stuck-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Task', 'Events', 'Status', '{{owner_name}}’s choice'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, cards.map((c) => {
            const [label, tone] = STUCK_STATE[c.state] || [c.state, null];
            return h('tr', {}, h('td', { class: 'num' }, when(c.created_at)), h('td', { class: 'mono' }, c.task_id || short(c.session_id)),
              h('td', { class: 'num' }, c.events), h('td', {}, pill(label, tone)), h('td', {}, c.choice ? pill(STUCK_CHOICE[c.choice] || c.choice, null) : '—'));
          }))) : h('p', { class: 'note' }, 'None yet: a card is sent only when a claim exceeds the threshold and more messages are queued behind it; claims are never released automatically.'));
    }
    function renderCache(d) {
      const rows = d.summary || [];
      const pct = (x) => (x === null || x === undefined ? '—' : `${Math.round(x * 100)}%`);
      const k = (x) => (x === null || x === undefined ? '—' : `${(x / 1000).toFixed(1)}K`);
      fill(cache, h('h3', {}, 'Prompt cache (24 h) ', h('span', { class: 'count' }, d.runs_total || 0)),
        h('p', { class: 'note' }, 'How much of the input to each background run’s first model call was read from the cache (cold start = nothing read). p39 = the old prompt layout,'
          + ' p40 = instructions first and this run’s details last, plus context trimmed by task category (dispatch.lean_context);'
          + ' p40.1 = an identical first message every time, this run’s details in a file, and keep-warm refreshes that extend the cache while idle. Numbers only, no message content.'),
        rows.length ? h('table', { class: 'ops-table', id: 'health-cache-table' },
          h('thead', {}, h('tr', {}, ['Task', 'provider', 'Profile', 'Runs', 'Cold starts', 'First input (median)', 'Cache read share', 'Duration (median)'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, rows.map((r) => h('tr', {}, h('td', { class: 'mono' }, r.task), h('td', {}, provChip(r.provider)),
            h('td', { class: 'mono' }, r.profile), h('td', { class: 'num' }, r.runs),
            h('td', { class: 'num' }, r.measured ? `${r.cold_starts}/${r.measured} · ${pct(r.cold_rate)}` : '—'),
            h('td', { class: 'num' }, k(r.first_input_median)), h('td', { class: 'num' }, pct(r.first_read_share)),
            h('td', { class: 'num' }, r.wall_seconds_median === null || r.wall_seconds_median === undefined ? '—' : dur(r.wall_seconds_median)))))) :
          h('p', { class: 'note' }, d.notice === 'state_database_missing' ? 'The database does not exist yet.' : 'No background runs in the last 24 h.'),
        // P40.1: keep-warm refreshes are listed on their own (not runs): count, how many read the cache, cost.
        ((d.keep_warm && d.keep_warm.summary) || []).length ? h('p', { class: 'note', id: 'health-cache-keepwarm' },
          d.keep_warm.summary.map((s) => `Keep-warm ${s.task}: ${s.refreshes} refreshes (${s.warmed} warmed, ${s.failed} failed, ${s.cold} cold writes), `
            + `first read ${k(s.first_read_median)} · write ${k(s.first_write_median)}, $${(s.cost_usd || 0).toFixed(3)}`).join('; ')) : null,
        h('dl', { class: 'kv' }, h('dt', {}, 'Commands'), h('dd', { class: 'mono' }, 'cache-report [--task T] [--since H] · config set dispatch.lean_context.enabled false · config set dispatch.lean_context.keep_warm.enabled false')));
    }
    let cacheAt = 0;  // P40: the cache panel re-reads run logs, so at most once a minute (the server memoizes too)
    async function load() {
      let data;
      try { data = await api('/api/health'); online(true); } catch (e) { online(false); return; }
      renderStatus(data); renderSystem(data); renderCards(data); renderProviders(data); renderWait(data); renderSwitches(data); renderStranded(data); renderStuck(data);
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
    'providers.grok.model': 'Grok model', 'providers.grok.effort': 'Grok effort',
    'providers.claude.model_default': 'Claude default model', 'providers.claude.model_important': 'Claude model for important tasks',
    'providers.claude.effort': 'Claude effort', 'providers.codex.model': 'Codex model', 'providers.codex.effort': 'Codex effort',
    'routing.categories': 'Task category → provider', 'routing.fallback_order': 'Fallback order',
    'dispatch.enabled': 'Enable background dispatch', 'dispatch.progress_updates': 'Slack progress line', 'dispatch.concurrency': 'Concurrent run cap',
    'dispatch.max_event_age_hours': 'Stale event cutoff (hours)', 'dispatch.claude_env_passthrough': 'Claude pass-through env var names',
    'circuit_breaker.round_trips_per_hour': 'Circuit breaker: round trips per hour', 'intake.channels': 'Intake channels', 'intake.burst_seconds': 'Burst merge window (s)',
    'intake.enabled': 'Enable Intake', 'intake.oncall_channels': 'oncall channels (bot alerts)', 'intake.poll_seconds': 'Intake polling interval (s)',
    'intake.alert_dedupe_minutes': 'Alert dedup window (min)', 'intake.dm_queries': 'DM discovery queries', 'intake.mention_search': 'Search @{{owner_name}}',
    'intake.delegate_bot_names': 'Apps that post as a person',
    'intake.inbox_provider': 'Intake provider', 'intake.inbox_model': 'Intake model', 'intake.inbox_effort': 'Intake effort',
    'intake.oncall_provider': 'oncall provider', 'intake.oncall_model': 'oncall model', 'intake.oncall_effort': 'oncall effort',
    'ask.remind_after': 'Ask card reminder after', 'ask.expire_after': 'Ask card expiry',
    'rules.enabled': 'Enable rule engine', 'rules.proposals_enabled': 'Enable weekly rule proposals', 'routines.enabled': 'Enable routines',
    'rules.typed_approval_max_age_hours': 'How long an instruction you type in a card thread counts as approval (hours)',
    'blockers.enabled': 'Enable blocker cards', 'blockers.skill_proposals_enabled': 'Enable weekly skill proposals',
    'cards.retire_buttons_on_reply': 'Retire buttons after a thread reply', 'cards.thread_reply_decisions': 'Clear text replies count as decisions',
    'cards.read_receipts': 'Read-receipt emoji', 'cards.open_poll_seconds': 'Open card thread polling interval (s)',
    'cards.task_thread': 'Group a task’s cards into one approval thread', 'cards.retire_on': 'Which thread replies retire buttons',
    'cards.max_open_per_task': 'Max open cards waiting on you per task',
    'tasks.max_wakes': 'Self-wake cap per task', 'tasks.wake_window_hours': 'Wake count window (hours)',
    'tasks.stale_card_days': 'Days until an unclicked check-in card expires',
    'dispatch.provider_wait_hours': 'Max wait for AI recovery (hours)', 'dispatch.probe_enabled': 'Probe whether AI has recovered',
    'dispatch.probe_min_seconds': 'Min probe interval (s)', 'dispatch.probe_max_seconds': 'Max probe interval (s)',
    'dispatch.unresolved_retries': 'Reruns for unfinished events',
  };
  const ERROR_FIELD = {
    providers_claude_model_default: 'Claude default model', providers_claude_model_important: 'Claude model for important tasks',
    providers_claude_effort: 'Claude effort', providers_codex_model: 'Codex model', providers_codex_effort: 'Codex effort',
    routing_categories: 'Task category routing', routing_fallback_order: 'Fallback order', dispatch_enabled: 'Enable background dispatch',
    dispatch_progress_updates: 'Slack progress line', dispatch_concurrency: 'Concurrent run cap', dispatch_max_event_age_hours: 'Stale event cutoff',
    dispatch_claude_env_passthrough: 'Claude pass-through env var names', circuit_breaker_round_trips_per_hour: 'Circuit breaker cap',
    intake_channels: 'Intake channels', intake_burst_seconds: 'Burst merge window', ask_remind_after: 'Ask card reminder after',
    intake_enabled: 'Enable Intake', intake_oncall_channels: 'oncall channels', intake_poll_seconds: 'Intake polling interval',
    intake_alert_dedupe_minutes: 'Alert dedup window', intake_dm_queries: 'DM discovery queries', intake_mention_search: 'Search @{{owner_name}}',
    intake_delegate_bot_names: 'Apps that post as a person',
    intake_inbox_provider: 'Intake provider', intake_inbox_model: 'Intake model', intake_inbox_effort: 'Intake effort',
    intake_oncall_provider: 'oncall provider', intake_oncall_model: 'oncall model', intake_oncall_effort: 'oncall effort',
    intake_oncall_task: 'oncall task',
    ask_expire_after: 'Ask card expiry',
    rules_enabled: 'Enable rule engine', rules_proposals_enabled: 'Enable weekly rule proposals', routines_enabled: 'Enable routines',
    rules_typed_approval_max_age_hours: 'Typed-instruction approval window',
    blockers_enabled: 'Enable blocker cards', blockers_skill_proposals_enabled: 'Enable weekly skill proposals',
    cards_retire_buttons_on_reply: 'Retire buttons after a thread reply', cards_thread_reply_decisions: 'Clear text replies count as decisions',
    cards_read_receipts: 'Read-receipt emoji', cards_open_poll_seconds: 'Open card thread polling interval',
    cards_task_thread: 'Group a task’s cards into one approval thread', cards_retire_on: 'Which thread replies retire buttons',
    cards_max_open_per_task: 'Max open cards waiting on you per task',
    tasks_max_wakes: 'Self-wake cap per task', tasks_wake_window_hours: 'Wake count window',
    tasks_stale_card_days: 'Check-in card expiry (days)',
    dispatch_provider_wait_hours: 'Max wait for AI recovery', dispatch_probe_enabled: 'Probe whether AI has recovered',
    dispatch_probe_min_seconds: 'Min probe interval', dispatch_probe_max_seconds: 'Max probe interval', dispatch_unresolved_retries: 'Reruns for unfinished events',
  };
  const ERROR_WHY = [
    [/^must_be_a_model_id$/, 'Must be a model ID (letters, digits, . _ : / @ -)'],
    [/^must_be_one_of_/, 'Must be low / medium / high / xhigh / max'],
    [/^must_be_an_integer_(\d+)_to_(\d+)$/, (m) => `Must be an integer from ${m[1]} to ${m[2]}`],
    [/^must_be_0_or_(\d+)_to_(\d+)$/, (m) => `Must be 0 (off) or an integer from ${m[1]} to ${m[2]}`],
    [/^must_be_true_or_false$/, 'Must be on or off'],
    [/^must_list_distinct_providers$/, 'Must list codex / claude / grok without repeats'],
    [/^must_be_a_non_empty_object$/, 'Keep at least one category'],
    [/^must_map_category_to_codex_or_claude$/, 'Category names must start with a lowercase letter, use only a-z 0-9 _ -, and map to codex, claude or grok'],
    [/^must_be_distinct_channel_names_or_ids$/, 'One channel per line: #name or a C… channel ID, no duplicates'],
    [/^must_look_like_10h$/, 'Use a format like 10h, 30m, 2d'],
    [/^must_be_1h_to_7d$/, 'Must be between 1 hour and 7 days'],
    [/^must_be_shorter_than_expire_after$/, 'Must be earlier than the expiry time'],
    [/^must_be_a_list_of_up_to_20_env_var_names$/, 'Variable names only (uppercase letters, digits, underscores), up to 20'],
    [/^must_be_up_to_5_distinct_single_line_queries$/, 'Up to 5 distinct single-line queries'],
    [/^must_be_up_to_20_app_name_patterns$/, 'Up to 20 entries, each 1–60 characters: letters, digits, * _ -'],
    [/^must_be_codex_or_claude$/, 'Must be codex, claude or grok'],
    [/^must_differ_from_inbox_task$/, 'Must differ from the Intake task'],
    [/^must_not_exceed_probe_max_seconds$/, 'Must not exceed the max probe interval'],
  ];
  function explainError(code) {
    if (code === 'worker_config_changed_since_loaded_reload') return 'The config file changed after you opened this page (for example, via config set). Refresh the page and edit again.';
    if (code === 'invalid_page_token') return 'The page token has expired; refresh the page and save again.';
    if (code === 'setting_not_editable_here') return 'This setting cannot be changed on this page.';
    if (code === 'same_origin_required') return 'Only save requests sent from this page are accepted.';
    const m = /^invalid_worker_config_(.+)$/.exec(code || '');
    if (m) {
      const rest = m[1];
      const field = Object.keys(ERROR_FIELD).sort((a, b) => b.length - a.length).find((f) => rest.startsWith(f + '_'));
      if (field) {
        const why = rest.slice(field.length + 1);
        for (const [re, text] of ERROR_WHY) {
          const mm = re.exec(why);
          if (mm) return `${ERROR_FIELD[field]}: ${typeof text === 'function' ? text(mm) : text}`;
        }
        return `${ERROR_FIELD[field]}: ${why}`;
      }
    }
    return 'Save failed: ' + (code || 'unknown error');
  }

  function Settings() {
    let st = null, token = null;
    const form = h('form', { class: 'settings', id: 'settings-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); review(); } });
    const errBox = h('div', { class: 'err-box', id: 'settings-error', role: 'alert', hidden: true });
    const logCard = h('section', { class: 'ops-card', id: 'settings-log' });
    const info = h('p', { class: 'ops-note', id: 'settings-info' });
    const head = pageHead('Settings', 'SETTINGS · digital worker config', null);
    head.firstChild.append(info);
    // a section index on the left (sticky), the form in one column on the right
    const index = h('nav', { class: 'set-index', 'aria-label': 'Settings sections' });

    // 0.78.9 (F21) / 0.78.10 (F22): the owners and, among them, the active approvers, edited here; one save writes the
    // card config and a new access-rules version (kage.core.approvers), the actor is the signed-in Access email.
    const APPR_ERR = {"approvers_changed_since_loaded_reload": "The approvers changed after you opened the page; reloaded, please edit again.", "invalid_page_token": "The page token expired; reload the page and save again.", "the_owner_stays_an_approver_change_the_owner_through_the_deployment": "The deployment owner must stay an approver; changing the owner means redeploying.", "at_most_5_approvers": "At most 5 approvers.", "an_hr_contact_cannot_be_an_approver": "An HR contact cannot also be an approver.", "approver_must_be_a_person_not_a_bot": "Only people, not bots.", "approver_account_is_deactivated": "This account is deactivated.", "approver_must_be_a_full_member_of_this_workspace": "Must be a full member of this workspace (no guests or other organisations).", "approver_not_found_in_slack": "Not found in Slack (check the id or email; email lookup needs the bot scope users:read.email).", "approver_must_be_a_slack_user_id_or_an_email": "Enter a Slack user id (U…) or an email.", "nothing_to_change": "Nothing changed.", "approvers_must_be_a_list_of_slack_ids_or_emails": "Keep at least one approver.", "an_active_approver_must_be_one_of_the_owners": "An approver must be one of the owners.", "at_most_5_owners": "At most 5 owners."};
    const apprCard = h('section', { class: 'ops-card', id: 'settings-approvers' });
    let appr = null, apprToken = null, apprList = [];
    function apprError(e) {
      const code = (e.body && e.body.error) || e.message;
      return (APPR_ERR[code] || code) + (e.body && e.body.detail ? ` (${e.body.detail})` : '');
    }
    const apprName = (x) => (x.name ? `${x.name} (${x.id})` : x.id);
    function renderApprovers() {
      if (!appr) return;
      const rows = apprList.map((a) => {
        const sw = h('input', { type: 'checkbox', role: 'switch', checked: a.active ? true : null, 'aria-label': 'Approve ' + (a.name || a.id) });
        sw.addEventListener('change', () => { a.active = sw.checked; renderApprovers(); });
        return h('li', { class: 'appr-row' + (a.active ? ' is-active' : '') },
          h('span', { class: 'appr-name' }, a.name || a.id, a.pending ? h('small', {}, ' (not saved yet)') : null),
          h('code', { class: 'appr-id' }, a.id),
          h('span', { class: 'appr-tags' }, a.owner ? pill('deployment owner', 'good') : null,
            a.active && a.in_rules === false ? pill('not in the rules', 'warn') : null),
          h('label', { class: 'switch appr-switch' }, sw, h('span', {}, 'Approve')),
          a.owner ? h('span', {}) : h('button', { type: 'button', class: 'btn sm ghost', onclick: () => {
            apprList = apprList.filter((x) => x !== a); renderApprovers(); } }, 'Remove'));
      });
      const input = h('input', { type: 'text', class: 'appr-input', placeholder: 'Add an owner: Slack user id (U…) or email', spellcheck: 'false', autocomplete: 'off',
        'aria-label': 'Add an owner: Slack user id (U…) or email' });
      const add = () => {
        const v = input.value.trim();
        if (!v || apprList.some((x) => x.id === v)) return;
        apprList = apprList.concat([{ id: v, name: null, pending: true, active: false }]);
        renderApprovers();
      };
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
      const save = h('button', { type: 'button', class: 'btn primary', id: 'approvers-save', onclick: async () => {
        const active = apprList.filter((x) => x.active);
        if (!active.length) { toast('Switch on at least one approver.', 'bad'); return; }
        const text = "Save?\nOwners: {owners}\nApprovers: {approvers}\nRecorded under your sign-in email; a notice goes to the decision channel.".replace('{owners}', apprList.map(apprName).join(', ')).replace('{approvers}', active.map(apprName).join(', '));
        if (!window.confirm(text)) return;
        save.disabled = true; save.textContent = 'Saving…';
        try {
          const res = await api('/api/approvers', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Page-Token': apprToken },
            body: JSON.stringify({ owners: apprList.map((x) => x.id), approvers: active.map((x) => x.id), digest: appr.digest }) });
          apprToken = res.page_token || apprToken;
          appr = res.state; apprList = appr.owners.map((x) => ({ ...x }));
          toast("Saved: {o} owners, {a} approvers".replace('{o}', appr.owners.length).replace('{a}', appr.approvers.length), 'good');
        } catch (e) {
          toast(apprError(e), 'bad');
          if (e.body && ['approvers_changed_since_loaded_reload', 'invalid_page_token'].includes(e.body.error)) loadApprovers();
        }
        renderApprovers();
      } }, 'Save');
      const log = appr.changes || [];
      const nameOf = (id) => (appr.people.find((p) => p.id === id) || {}).name || id;
      const who = (x) => {
        const v = String(x || '').replace(/^(dashboard|slack):/, '');
        return /^[UW][A-Z0-9]+$/.test(v) ? nameOf(v) : (v || '—');
      };
      const sc = appr.schedule;
      fill(apprCard, h('h3', {}, 'Owner and approvers ', h('span', { class: 'count' }, apprList.length)),
        h('p', { class: 'note' }, "Owners count as the owner (bot identity, owner_like=approvers): they may ask the bot privately in the decision channel, change settings and switch the approvers. Owners switched on as Approve are the active approvers: cards mention them and only their clicks or answers decide. The deployment owner stays in the list. An owner can also write 'approver -> @someone' or 'until 11-20, approver -> @someone' in the decision channel; code switches it at once (and back when the date is over). One save updates the card config and a new version of the access rules, and posts a notice in the decision channel."),
        sc ? h('div', { class: 'warn-box', role: 'status' }, "The approvers switch back to {people} at {date} (set by {who}).".replace('{date}', when(sc.at))
          .replace('{people}', (sc.then || []).map(nameOf).join(', ')).replace('{who}', who(sc.set_by))) : null,
        h('ul', { class: 'appr-list' }, rows),
        h('div', { class: 'appr-add' }, input, h('button', { type: 'button', class: 'btn sm', onclick: add }, 'Add'), save),
        log.length ? [h('h3', {}, 'Recent changes'), h('table', { class: 'ops-table', id: 'approvers-log' },
          h('thead', {}, h('tr', {}, ['Time', 'By', 'Approvers', 'Owners'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, log.slice(0, 10).map((r) => h('tr', {}, h('td', { class: 'num' }, when(r.ts)), h('td', {}, who(r.who)),
            h('td', {}, (r.new || []).map(nameOf).join(', ') || '—'),
            h('td', {}, (r.owners_new || []).map(nameOf).join(', ') || '—')))))] : null);
    }
    async function loadApprovers() {
      try {
        appr = await api('/api/approvers');
      } catch (e) {
        fill(apprCard, h('h3', {}, 'Owner and approvers'), h('p', { class: 'note' }, e.status === 404 ? 'This deployment cannot edit approvers on the page.' : 'Could not read the approvers.'));
        return;
      }
      apprToken = appr.page_token; apprList = appr.owners.map((x) => ({ ...x }));
      renderApprovers();
    }
    clear(main).append(head, errBox, h('div', { class: 'set-layout' }, index, h('div', { class: 'set-col' }, apprCard, form, logCard)));
    loadApprovers();
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
            !same(cur, dflt) ? h('span', { class: 'pill', 'data-tone': 'warn' }, '≠ default') : null,
            h('span', { class: 'pill dirty', 'data-tone': 'bad', hidden: true }, 'Unsaved')),
          h('code', { class: 'set-key' }, key),
          h('span', { class: 'set-hint' }, (hint ? hint + ' · ' : '') + 'Default: ' + (dflt === undefined ? '—' : showDefault(dflt)))),
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
        const nameEl = h('input', { type: 'text', value: name || '', 'aria-label': 'Category name', spellcheck: 'false', placeholder: 'e.g. infra' });
        const provEl = h('select', { 'aria-label': 'Maps to' }, providers.map((p) => h('option', { value: p, selected: p === prov }, PROVIDER[p] || p)));
        const row = h('div', { class: 'cat-row' }, nameEl, h('span', { 'aria-hidden': 'true' }, '→'), provEl,
          h('button', { type: 'button', class: 'btn sm ghost', 'aria-label': 'Delete category', onclick: () => { row.remove(); markDirty(); } }, 'Delete'));
        rows.append(row);
      }
      for (const [k, v] of Object.entries(st.config.routing.categories)) addRow(k, v);
      box.append(rows, h('button', { type: 'button', class: 'btn sm', onclick: () => { addRow('', providers[0]); markDirty(); } }, '+ Add category'));
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
          h('button', { type: 'button', class: 'btn sm ghost', disabled: i === 0, 'aria-label': 'Move up', onclick: () => { const a = items.slice(); [a[i - 1], a[i]] = [a[i], a[i - 1]]; render(a); markDirty(); } }, '↑'),
          h('button', { type: 'button', class: 'btn sm ghost', disabled: i === items.length - 1, 'aria-label': 'Move down', onclick: () => { const a = items.slice(); [a[i + 1], a[i]] = [a[i], a[i + 1]]; render(a); markDirty(); } }, '↓'))));
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
          : sec === logCard ? 'Change log' : 'Other (read-only)';
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
      dirtyNote.textContent = dirty.size ? `${dirty.size} unsaved change(s)` : '';
      for (const a of index.querySelectorAll('a')) {
        const sec = document.getElementById(a.dataset.target);
        const n = sec && sec.matches('.set-section') ? [...sec.querySelectorAll('.set-field')].filter((f) => dirty.has(f.dataset.key)).length : 0;
        if (n) a.dataset.dirty = String(n); else delete a.dataset.dirty;
      }
    }
    const saveBtn = h('button', { type: 'submit', class: 'btn primary', id: 'settings-save', disabled: true }, 'Check and save…');
    const resetBtn = h('button', { type: 'button', class: 'btn ghost', id: 'settings-reset', disabled: true, onclick: () => render() }, 'Discard changes');
    const dirtyNote = h('span', { class: 'set-dirty-note', 'aria-live': 'polite' });

    function render() {
      errBox.hidden = true;
      for (const k of Object.keys(inputs)) delete inputs[k];
      clear(form);
      const efforts = st.enums.efforts;
      form.append(
        section('Models and effort', 'MODELS', [
          field('providers.claude.model_default', text('providers.claude.model_default')),
          field('providers.claude.model_important', text('providers.claude.model_important'), 'Design or important tasks (--important)'),
          field('providers.claude.effort', select('providers.claude.effort', efforts)),
          field('providers.codex.model', text('providers.codex.model'), 'Passed to codex via -m; ~/.codex/config.toml is left unchanged'),
          field('providers.codex.effort', select('providers.codex.effort', efforts)),
          ...(st.config.providers.grok ? [field('providers.grok.model', text('providers.grok.model')),
            field('providers.grok.effort', select('providers.grok.effort', efforts))] : []),
        ], 'Each background run reads the latest config when it starts; sessions already running are not affected.'),
        section('Routing', 'ROUTING', [
          field('routing.categories', categories(), 'New tasks pick a provider by category'),
          field('routing.fallback_order', order(), 'On infrastructure failures, switch providers in this order'),
        ]),
        section('Background dispatch', 'DISPATCH', [
          field('dispatch.enabled', toggle('dispatch.enabled', 'On (wake in the background when no session is live)', 'Off (deliver everything the old way)')),
          field('dispatch.progress_updates', toggle('dispatch.progress_updates', 'On (the card refreshes at most once a minute)', 'Off (update the card only at start and end)')),
          field('dispatch.concurrency', num('dispatch.concurrency', 1, 20)),
          field('dispatch.max_event_age_hours', num('dispatch.max_event_age_hours', 1, 720), 'Older events go to “Backlog” and do not wake a session'),
          field('dispatch.claude_env_passthrough', list('dispatch.claude_env_passthrough', 'One variable name per line'), 'Names only; values are read from the login shell and never shown'),
        ]),
        section('Circuit breaker', 'BREAKER', [field('circuit_breaker.round_trips_per_hour', num('circuit_breaker.round_trips_per_hour', 1, 1000), 'Pause when two tasks wake each other more than this many times in an hour')]),
        section('Intake', 'INTAKE', [
          field('intake.enabled', toggle('intake.enabled', 'On (the daemon reads DMs, @{{owner_name}} mentions and subscribed channels on a cadence)', 'Off (no Slack reads, no new events)')),
          field('intake.channels', list('intake.channels', '{{channel_example}}'), 'One channel ID (C…) per line; {{channel_names}} are recognized too'),
          field('intake.oncall_channels', list('intake.oncall_channels', '{{oncall_channel_example}}'), 'Bot alerts in these channels go to {{owner_key}}-oncall'),
          field('intake.burst_seconds', num('intake.burst_seconds', 1, 3600), 'Sliding window that merges back-to-back messages from the same person in the same conversation'),
          field('intake.poll_seconds', num('intake.poll_seconds', 20, 3600), 'Slack read cadence; search.messages is rate-limited at Tier 2'),
          field('intake.alert_dedupe_minutes', num('intake.alert_dedupe_minutes', 1, 1440), 'The same service + alert title produces only one event within this period'),
          field('intake.dm_queries', list('intake.dm_queries', 'to:me'), 'search.messages queries that discover DMs / group DMs (verify with intake-probe)'),
          field('intake.mention_search', toggle('intake.mention_search', 'On (search @{{owner_name}})', 'Off')),
          field('intake.delegate_bot_names', list('intake.delegate_bot_names', 'coder-*'), 'Messages a matching app posts with a person’s own token count as that person and are not filtered as a bot; one per line, * allowed; empty = off'),
          field('intake.inbox_provider', select('intake.inbox_provider', st.enums.providers), 'Provider of the long-lived Intake task'),
          field('intake.inbox_model', text('intake.inbox_model'), 'Change the model too when you change the provider'),
          field('intake.inbox_effort', select('intake.inbox_effort', efforts)),
          field('intake.oncall_provider', select('intake.oncall_provider', st.enums.providers), 'Provider of the long-lived oncall task'),
          field('intake.oncall_model', text('intake.oncall_model')),
          field('intake.oncall_effort', select('intake.oncall_effort', efforts)),
        ], 'After a change, the next intake cycle and the next Intake / oncall run use the new config.'),
        section('Ask cards', 'ASK', [
          field('ask.remind_after', text('ask.remind_after', { placeholder: '10h' }), 'Format 30m / 10h / 2d'),
          field('ask.expire_after', text('ask.expire_after', { placeholder: '24h' })),
        ]),
        section('Rules and Routines', 'RULES · ROUTINES', [
          field('rules.enabled', toggle('rules.enabled', 'On (access / account changes go through the rule engine first)', 'Off (every check goes to {{owner_name}})'), 'Kill switch: when off, rule-check always returns {{owner_key}}'),
          field('rules.proposals_enabled', toggle('rules.proposals_enabled', 'On (the weekly review may post proposal cards)', 'Off (no proposals; decided cards take no effect for now)')),
          field('rules.typed_approval_max_age_hours', num('rules.typed_approval_max_age_hours', 0, 168), 'For this many hours, an instruction you type yourself in a card thread counts as approval for an access check, so no second card is sent (an irreversible step still needs your click); 0 = off, always send a card'),
          field('routines.enabled', toggle('routines.enabled', 'On (fire routines when due)', 'Off (all routines paused)')),
        ], 'Interval, entry and run caps can only be tightened with config set (hard limits: 15 min, 20 entries, 48 runs a day).'),
        section('Blockers and Gaps', 'BLOCKERS · GAPS', [
          field('blockers.enabled', toggle('blockers.enabled', 'On (report-blocker sends cards, “Done” cards and reminders)', 'Off (log gaps only, no cards)'), 'Kill switch: when off, agents use ask-{{owner_key}} instead when needed'),
          field('blockers.skill_proposals_enabled', toggle('blockers.skill_proposals_enabled', 'On (the weekly review may post skill proposal cards)', 'Off (no proposals; decided cards create no tasks for now)')),
        ], 'Reminder time, proposals per run and quiet days can only be changed with config set.'),
        section('Decision cards and read receipts', 'CARDS · RECEIPTS', [
          field('cards.retire_buttons_on_reply', toggle('cards.retire_buttons_on_reply', 'On (after you reply in a card thread, retire buttons per the next setting)', 'Off (buttons stay)')),
          field('cards.retire_on', select('cards.retire_on', ['plan_change', 'any_reply']), 'plan_change: retire only on a clear decision or plan change (change to / hold off / cancel…), and keep buttons when you ask a question; any_reply: retire on any reply (the old P3.1 behavior)'),
          field('cards.thread_reply_decisions', toggle('cards.thread_reply_decisions', 'On (a clear reply starting with “approve / no / an option name” counts as a decision)', 'Off (the agent sends a confirmation card for every text reply)'), 'Kill switch: when off, only buttons and confirmation cards decide'),
          field('cards.read_receipts', toggle('cards.read_receipts', 'On (add {{receipt_emoji}} once a message enters the task context)', 'Off (no emoji)'), 'Added as you in DMs, as the bot in channels'),
          field('cards.open_poll_seconds', num('cards.open_poll_seconds', 0, 900), 'How often to check the threads of cards awaiting your decision (s); 0 = normal cadence (1–15 min)'),
          field('cards.max_open_per_task', num('cards.max_open_per_task', 1, 20), 'At the cap, the task’s agent must close or merge an old card (--about) before posting another; a new card replacing an old one and cards past “Check-in card expiry (days)” do not count; Intake and oncall have no cap (rule R-0008)'),
          field('cards.task_thread', toggle('cards.task_thread', 'On (a task’s cards share one thread; a task created from a card inherits the parent thread)', 'Off (each card is top-level, as before P3.2)'), 'Kill switch: when off, every card is posted top-level in the channel again'),
        ], 'Only replies you type yourself count; replies from others, delegates or agents neither retire buttons nor count as decisions. Read-receipt emoji go only on human messages that entered the task context.'),
        section('Task self-wakes', 'WAKE-UPS', [
          field('tasks.max_wakes', num('tasks.max_wakes', 0, 50), 'Past this many wakes in the window, a due wake becomes a card asking you; 0 = no automatic wakes, always send a card'),
          field('tasks.wake_window_hours', num('tasks.wake_window_hours', 1, 720), '168 = one week'),
          field('tasks.stale_card_days', num('tasks.stale_card_days', 0, 30), 'When an old check-in card (one that asks nothing of you) goes unclicked this many days, retire its buttons and let the agent follow up on its own; 0 = never expires'),
        ], 'What an agent registers with task-deadline --wake is its own checkpoint: when due, the task is woken directly, with no card and no new authorization.'),
        section('Wait for AI recovery', 'PROVIDER WAIT', [
          field('dispatch.provider_wait_hours', num('dispatch.provider_wait_hours', 0, 168), 'Send the failure card only if there is no recovery after this long; 0 = off (send the card at once, the old behavior)'),
          field('dispatch.probe_enabled', toggle('dispatch.probe_enabled', 'On (periodically probe whether Claude / Codex has recovered)', 'Off (no probing; failures send a card as before)')),
          field('dispatch.probe_min_seconds', num('dispatch.probe_min_seconds', 30, 3600), 'How long to wait before the first probe; the interval doubles after each failure'),
          field('dispatch.probe_max_seconds', num('dispatch.probe_max_seconds', 60, 21600), 'The probe interval doubles up to this length'),
          field('dispatch.unresolved_retries', num('dispatch.unresolved_retries', 0, 5), 'When events are left unfinished after a provider error: how many reruns after recovery before a card is sent; 0 = send a card at once'),
        ], 'When Claude / Codex returns 5xx, times out, is rate-limited or out of quota, and neither retries nor a provider switch helps, no card is sent to you: the daemon probes periodically with one tiny request and carries on automatically once it recovers; new messages in the meantime stay queued and are not lost.'),
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
      return h('details', { class: 'ops-card set-readonly', id: 'set-readonly' }, h('summary', {}, 'Other settings (read-only; change with config set)'),
        h('dl', { class: 'kv' }, rows.map(([k, v]) => [h('dt', {}, FIELD_LABEL[k] ? [FIELD_LABEL[k], h('code', { class: 'set-key' }, k)] : h('code', { class: 'set-key' }, k)),
          h('dd', { class: 'mono' }, show(v))])));
    }
    function renderLog(log) {
      clear(logCard).append(h('h3', {}, 'Change log ', h('span', { class: 'count' }, log.length)),
        log.length ? h('table', { class: 'ops-table' },
          h('thead', {}, h('tr', {}, ['Time', 'Changed by', 'Setting', 'Old value', 'New value'].map((x) => h('th', { scope: 'col' }, x)))),
          h('tbody', {}, log.map((r) => h('tr', {},
            h('td', { class: 'num' }, when(r.ts)), h('td', {}, r.who === 'dashboard' ? 'Page (dashboard)' : r.who || '—'),
            h('td', {}, FIELD_LABEL[r.key] || '', h('div', { class: 'sub mono' }, r.key)),
            h('td', { class: 'mono val' }, show(r.old)), h('td', { class: 'mono val' }, show(r.new))))))
          : h('p', { class: 'note' }, 'No changes yet.'));
    }
    function renderInfo() {
      fill(info, 'File: ', h('code', {}, st.path), st.source === 'default' ? ' (not created yet; created on save)' : null,
        ' · Changes are validated and then confirmed with you before saving; the editor is recorded as dashboard (your sign-in email when opened through Cloudflare Access).');
    }
    async function load() {
      try { st = await api('/api/settings'); online(true); } catch (e) { online(false); errBox.hidden = false; errBox.textContent = 'Failed to read settings.'; return; }
      token = st.page_token;
      renderInfo();
      renderLog(st.log || []);
      if (!st.ok) {
        errBox.hidden = false;
        errBox.textContent = `The config file is invalid (${st.error}). This page will not overwrite it: run slack_followup.py config validate to fix it first.`;
        clear(form);
        return;
      }
      render();
    }
    form.addEventListener('input', markDirty);
    form.addEventListener('change', markDirty);

    function review() {
      const list = changes();
      if (!list.length) { toast('No changes'); return; }
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
        if (!res.changes.length) { toast('No actual changes'); saveBtn.disabled = false; return; }
        pending = list;
        clear(dialogBody).append(h('p', {}, `Validation passed. ${res.changes.length} setting(s) will change (editor recorded as dashboard or your sign-in email):`),
          h('table', { class: 'ops-table' }, h('thead', {}, h('tr', {}, ['Setting', 'Old value', 'New value'].map((x) => h('th', { scope: 'col' }, x)))),
            h('tbody', {}, res.changes.map((c) => h('tr', {}, h('td', {}, FIELD_LABEL[c.key] || c.key, h('div', { class: 'sub mono' }, c.key)),
              h('td', { class: 'mono val' }, show(c.old)), h('td', { class: 'mono val' }, show(c.new)))))),
          h('p', { class: 'note' }, 'After saving, the next background dispatch and the next run started use the new config.'));
        dialog.showModal();
        saveBtn.disabled = false;
        return;
      }
      toast(`Saved ${res.changes.length} setting(s)`, 'good');
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
  // Design (P39: architecture design doc) — the generated review page of runbooks/architecture-design.md, read-only in a sandboxed
  // iframe: its scripts run in an opaque origin (no access to this page or the dashboard APIs) and its CSP blocks every
  // network request. Regenerating the file is enough: no dashboard restart, the page notices the new sha256.
  // =====================================================================
  const DESIGN_ERROR = { missing: 'not generated yet', symlink_refused: 'is a symlink; refused to read', not_regular_file: 'is not a regular file; refused to read',
    too_large: 'exceeds the 16 MB limit', unreadable: 'unreadable' };
  function fmtBytes(n) {
    if (n === null || n === undefined) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  }
  function Design() {
    const head = pageHead('Design', 'DESIGN · architecture design doc',
      'The digital worker’s architecture design doc: the source of truth is runbooks/architecture-design.md in the task workspace, from which build_review_html.py generates an offline review page'
      + ' (mermaid inlined locally). It is shown here read-only: the doc runs in a sandboxed iframe, cannot read dashboard data and cannot make any network request.'
      + ' After the doc changes, just regenerate it; no dashboard restart needed.');
    const status = h('section', { class: 'ops-card', id: 'design-status' });
    const view = h('section', { class: 'ops-card', id: 'design-view' });
    clear(main).append(head, status, view);
    let frame = null;
    let shown = null;  // sha256 of the version in the iframe

    function loadFrame(sha) {
      shown = sha;
      // sandbox before src: scripts only, never same-origin, forms, popups or top navigation.
      frame = h('iframe', { sandbox: 'allow-scripts', referrerpolicy: 'no-referrer', class: 'design-frame',
        title: 'Architecture design doc', src: '/design/doc?v=' + encodeURIComponent(sha || '') });
      fill(view, frame);
    }
    function renderStatus(d) {
      const warns = [];
      if (d.exists && d.external_src_refs > 0) {
        warns.push(`The doc references ${d.external_src_refs} external resource(s) (src=http…, probably generated with --cdn): all network requests are blocked here, `
          + 'so they will not load and diagrams may not render. Regenerate it with the command below (local mermaid).');
      } else if (d.exists && d.external_refs > 0) {
        warns.push(`The doc has ${d.external_refs} external link(s) (href=http…): they are only links and the page itself stays offline; they may not open from inside the frame.`);
      }
      if (d.error && d.error !== 'missing') warns.push(`Design doc ${DESIGN_ERROR[d.error] || d.error} (${d.error}).`);
      const stale = d.exists && frame && shown !== d.sha256;
      fill(status,
        h('div', { class: 'counters' },
          h('div', { class: 'counter' }, h('b', {}, d.exists ? 'Generated' : (DESIGN_ERROR[d.error] || 'Unavailable')), h('span', {}, 'File')),
          h('div', { class: 'counter' }, h('b', {}, d.exists ? fmtBytes(d.size) : '—'), h('span', {}, 'Size')),
          h('div', { class: 'counter' }, h('b', {}, d.exists ? when(d.mtime) : '—'), h('span', {}, 'Generated at')),
          h('div', { class: 'counter' }, h('b', { class: 'mono' }, d.sha256 || '—'), h('span', {}, 'sha256 (first 12)')),
          h('div', { class: 'counter' }, h('b', {}, d.exists ? d.external_refs : '—'), h('span', {}, 'External refs (src / href)'))),
        h('dl', { class: 'kv' },
          h('dt', {}, 'File'), h('dd', { class: 'mono' }, d.path),
          h('dt', {}, 'Display'), h('dd', {}, pill('Read-only', 'good'), ' ', pill('Sandboxed iframe · scripts only', 'good'), ' ',
            pill('No network requests', 'good'), ' ', pill('No symlinks followed', null), ' ', pill(`Limit ${fmtBytes(d.max_bytes)}`, null)),
          h('dt', {}, 'Open'), h('dd', {}, d.exists ? h('a', { class: 'ext', href: d.doc_url, target: '_blank', rel: 'noopener noreferrer' },
            'Open in a new tab ↗') : '—')),
        stale ? h('div', { class: 'warn-box', role: 'status' }, 'The doc has been regenerated (',
          h('span', { class: 'mono' }, d.sha256), '). ',
          h('button', { type: 'button', class: 'btn sm', onclick: () => { loadFrame(d.sha256); renderStatus(d); } }, 'Reload')) : null,
        warns.length ? h('div', { class: 'warn-box', role: 'status' }, warns.map((w) => h('div', {}, w))) : null);
    }
    function renderMissing(d) {
      frame = null; shown = null;
      fill(view, h('h3', {}, 'How to generate'),
        h('p', { class: 'ops-note' }, 'Run the command below in a terminal (standard library only; mermaid is inlined from a local file, so do not use --cdn).'
          + ' Then refresh this page; no dashboard restart needed. To use another file location, start the dashboard with --design-html PATH.'),
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
  else if (g) { document.title = 'Kage · blocker ' + g[1]; GapDetail(g[1]); }
  else if (path === '/gaps') { document.title = 'Kage · Gaps'; Gaps(); }
  else if (path === '/runs') { document.title = 'Kage · Runs'; Runs(); }
  else if (path === '/backlog') { document.title = 'Kage · Backlog'; Backlog(); }
  else if (path === '/intake') { document.title = 'Kage · Intake'; Intake(); }
  else if (path === '/rules') {
    const q = new URLSearchParams(location.search);
    const check = q.get('check');
    const prop = q.get('proposal');
    if (check && /^rc[0-9a-f]{14}$/.test(check)) { document.title = 'Kage · check ' + check; RuleCheckDetail(check); }
    else if (prop && /^rp[0-9a-f]{14}$/.test(prop)) { document.title = 'Kage · proposal ' + prop; RuleProposalDetail(prop); }
    else { document.title = 'Kage · Rules'; Rules(); }
  }
  else if (path === '/routines') { document.title = 'Kage · Routines'; Routines(); }
  else if (path === '/learning') { document.title = 'Kage · Learning'; Learning(); }
  else if (path === '/health') { document.title = 'Kage · Health'; Health(); }
  else if (path === '/design') { document.title = 'Kage · Design'; Design(); }
  else if (path === '/onboarding') { document.title = 'Kage · Onboarding'; Onboarding(); }
  else if (path === '/settings') { document.title = 'Kage · Settings'; Settings(); }
  else { clear(main).append(h('div', { class: 'empty' }, h('b', {}, 'Page not found'), h('a', { href: '/runs' }, 'Go to Runs'))); }
})();
