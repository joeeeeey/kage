(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const POLL_MS = 2000;
  const FETCH_TIMEOUT_MS = 8000;
  const STOP_TIMEOUT_MS = 10000;
  const STOP_ALL_HOLD_MS = 12000; // how long the session stop button waits for a post-stop snapshot before re-enabling
  const FRESH_MS = 6000;
  const EVENT_DISPLAY_MAX = 200;
  const WATCH_LIST_MAX = 150;
  const SCENE_STEP = 12;
  const DOCK = { w: 216, h: 230 };
  const POD = { w: 208, h: 104 };
  const ZOOM_MIN = 0.22;
  const ZOOM_MAX = 2.2;
  const WHEEL_LOCK_MS = 160; // idle gap that ends a trackpad gesture (and its inertia tail)
  const PV_MS = 15000; // preview refresh cadence while a thread inspector is open
  const PV_DEBOUNCE_MS = 900;
  const PV_TIMEOUT_MS = 12000;
  const PV_RETRY_MAX_S = 300;

  const $ = (id) => document.getElementById(id);
  const reducedMq = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  const app = {
    snap: null,
    lastOkAt: 0,
    error: null,
    inflight: false,
    rerun: false,
    timer: 0,
    firstDone: false,
    seenEvents: null,
    fresh: new Map(),
    bursts: new Set(),
    sigs: {},
    focusKey: null,
    focusPerson: null,
    selectedWatch: null,
    detailReturn: [],
    selectedSession: null,
    sessionReturn: [],
    snapAt: 0,
    freshColors: {},
    confirmWatch: null,
    confirmReturn: [],
    stopUnsure: null,
    pendingStops: new Set(),
    sessionStop: null, // open "stop whole session" confirmation: target is captured at open time and never follows app.selectedSession
    stopAllHold: null, // after a successful session stop: keeps the button disabled until a newer snapshot arrives
    ingestN: 0,
    peopleCtx: null,
    peopleReturn: [],
    search: "",
    stateFilter: "running", // default: only RUNNING watches; history is an explicit opt-in
    providerFilter: "all",
    motion: true,
    drawerOpen: false,
    tab: "watches",
    sceneLimit: SCENE_STEP,
    order: new Map(),
    orderN: 0,
  };

  // tooltip specs keyed by element (functions are evaluated lazily on hover)
  const tipMap = new WeakMap();

  /* ---------- helpers ---------- */

  const arr = (v) => (Array.isArray(v) ? v : []);
  const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});
  const str = (v) => (v === null || v === undefined ? "" : String(v));
  const num = (v) => {
    if (v === null || v === undefined || v === "" || typeof v === "boolean") return null;
    const n = typeof v === "number" ? v : Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const cnt = (v) => num(v) || 0;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const flag = (v) => (typeof v === "boolean" ? v : null);

  function el(tag, opts, ...kids) {
    const n = document.createElement(tag);
    applyOpts(n, opts);
    for (const k of kids) if (k !== null && k !== undefined && k !== false) n.append(k);
    return n;
  }
  function sv(tag, opts, ...kids) {
    const n = document.createElementNS(SVG_NS, tag);
    applyOpts(n, opts);
    for (const k of kids) if (k) n.append(k);
    return n;
  }
  function applyOpts(n, opts) {
    if (!opts) return;
    for (const [k, v] of Object.entries(opts)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "text") n.textContent = String(v);
      else if (k === "class") n.setAttribute("class", String(v));
      else if (k === "data") for (const [dk, dv] of Object.entries(v)) n.setAttribute("data-" + dk, String(dv));
      else if (k === "vars") for (const [vk, vv] of Object.entries(v)) n.style.setProperty(vk, String(vv));
      else n.setAttribute(k, v === true ? "" : String(v));
    }
  }

  const pad = (n) => String(n).padStart(2, "0");
  function fmtClock(sec, withDate) {
    const n = num(sec);
    if (n === null) return "—";
    const d = new Date(n * 1000);
    const t = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    return withDate ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${t}` : t;
  }
  function fmtMinute(sec) {
    const n = num(sec);
    if (n === null) return "";
    const d = new Date(n * 1000);
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function fmtRel(sec) {
    const n = num(sec);
    if (n === null) return "—";
    const diff = Math.round(Date.now() / 1000 - n);
    const a = Math.abs(diff);
    let s;
    if (a < 5) return "just now";
    if (a < 60) s = `${a}s`;
    else if (a < 3600) s = `${Math.round(a / 60)} min`;
    else if (a < 86400) s = `${Math.round(a / 3600)}h`;
    else s = `${Math.round(a / 86400)}d`;
    return diff >= 0 ? `${s} ago` : `in ${s}`;
  }
  function relNode(sec) {
    const n = num(sec);
    if (n === null) return el("span", { text: "—" });
    return el("time", { text: fmtRel(n), "data-rel": n, title: fmtClock(n, true) });
  }
  function short(s, n) {
    const t = str(s);
    return t.length > n ? t.slice(0, n - 1) + "…" : t;
  }
  function safeUrl(u) {
    const s = str(u);
    return /^https:\/\//i.test(s) ? s : null;
  }
  // image sources must stay same-origin: only the local avatar API and bundled assets
  function localUrl(u) {
    const s = str(u);
    if (!s || s.length > 400) return null;
    return /^\/(?:api\/avatars|assets)\/[^\\]+$/.test(s) ? s : null;
  }
  // per-message links must point at Slack itself
  function slackMsgUrl(u) {
    const s = safeUrl(u);
    if (!s) return null;
    try {
      const h = new URL(s).hostname.toLowerCase();
      return h === "slack.com" || h.endsWith(".slack.com") ? s : null;
    } catch (_) { return null; }
  }
  function fmtFull(sec) {
    const n = num(sec);
    if (n === null) return "—";
    const d = new Date(n * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  function fmtDur(sec) {
    const n = num(sec);
    if (n === null) return "—";
    const s = Math.max(0, Math.round(n));
    if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
    if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    if (s >= 60) return `${Math.floor(s / 60)} min`;
    return `${s}s`;
  }
  function slackLink(url, label) {
    const u = safeUrl(url);
    if (!u) return null;
    return el("a", { href: u, target: "_blank", rel: "noopener noreferrer", text: label || "Slack ↗", "aria-label": "Open in Slack (new window)" });
  }
  function hash(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function rand01(seed) {
    let t = (seed + 0x6d2b79f5) >>> 0;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }
  function initialOf(name) {
    const c = Array.from(str(name).trim().replace(/^[@#]+/, ""))[0];
    return c ? c.toUpperCase() : "?";
  }
  function pushTo(map, key, val) {
    const l = map.get(key);
    if (l) l.push(val); else map.set(key, [val]);
  }

  /* ---------- providers & characters ---------- */

  // logo is null on purpose: the bundled /assets/claude-logo.png is a photo of a person, not a brand mark,
  // so the badge uses a CSS glyph unless the API supplies an explicit providers[].logo_url
  const KNOWN_ART = {
    codex: { avatar: "/assets/codex-agent.png", logo: null, glyph: "›_" },
    claude: { avatar: "/assets/claude-agent.png", logo: null, glyph: "C" },
  };
  function providerMap() {
    const m = new Map();
    for (const p of arr(app.snap && app.snap.providers)) {
      const o = obj(p);
      if (o.id) m.set(str(o.id), o);
    }
    return m;
  }
  function provInfo(id) {
    const k = str(id);
    const p = obj(providerMap().get(k));
    const art = KNOWN_ART[k] || {};
    let note = "";
    if (p.delivery_supported === false) note = p.status === "planned" ? "Planned" : "No delivery yet";
    return {
      id: k,
      label: str(p.label || k || "Unknown"),
      avatar: localUrl(p.avatar_url) || art.avatar || null,
      logo: localUrl(p.logo_url) || art.logo || null,
      glyph: art.glyph || initialOf(p.label || k),
      note,
      // transport capability as declared by the backend; says nothing about whether any session is connected
      cap: { supported: flag(p.delivery_supported), status: str(p.status), path: str(p.delivery_path), consumer: flag(p.consumer_required), autoResume: flag(p.auto_resume) },
    };
  }
  function provLabel(id) { return provInfo(id).label; }
  function provNote(id) {
    const i = provInfo(id);
    if (!i.note) return "";
    return i.note === "Planned" ? "Planned · delivery not enabled yet" : "Delivery not supported yet";
  }
  // known providers have explicit palettes in CSS; anything else derives a hue so it never looks like a real brand
  function theme(node, id) {
    const k = str(id);
    if (k === "codex" || k === "claude") node.setAttribute("data-prov", k);
    else { node.setAttribute("data-prov", "other"); node.style.setProperty("--h", String(hash(k) % 360)); }
    return node;
  }
  function robotNode() {
    return el("span", { class: "robot", "aria-hidden": "true" }, el("i"), el("i"), el("b"));
  }
  function characterNode(id) {
    const info = provInfo(id);
    if (!info.avatar) return robotNode();
    const img = el("img", { class: "char", src: info.avatar, alt: "", decoding: "async", draggable: "false" });
    img.addEventListener("error", () => img.replaceWith(robotNode()), { once: true });
    return img;
  }
  function badgeNode(id) {
    const info = provInfo(id);
    const b = el("span", { class: "pbadge", "aria-hidden": "true" });
    if (info.logo) {
      const img = el("img", { src: info.logo, alt: "", decoding: "async", draggable: "false" });
      img.addEventListener("error", () => img.replaceWith(document.createTextNode(info.glyph)), { once: true });
      b.append(img);
    } else b.textContent = info.glyph;
    return b;
  }
  // small provider character (rail chips, legend, drawer groups); falls back to a neutral CSS chip
  function thumbNode(id, imgCls, robotCls) {
    const info = provInfo(id);
    const robot = () => theme(el("span", { class: robotCls || "rc-robot", "aria-hidden": "true" }), id);
    if (!info.avatar) return robot();
    const img = el("img", { class: imgCls, src: info.avatar, alt: "", decoding: "async", draggable: "false" });
    img.addEventListener("error", () => img.replaceWith(robot()), { once: true });
    return img;
  }

  /* ---------- state labels ---------- */

  function watchTone(state) {
    if (state === "running") return "good";
    if (state === "stopped" || state === "expired") return "mute";
    return "warn";
  }
  const WATCH_STATE_LABEL = { running: "Running", stopped: "Stopped", expired: "Expired" };
  function watchStateLabel(s) { return WATCH_STATE_LABEL[s] || str(s) || "Unknown"; }
  function stateKey(s) { return s === "running" || s === "stopped" || s === "expired" ? s : "other"; }

  /* ---------- freshness (polling cadence) & lifecycle window ---------- */

  // Freshness is the polling interval, not message age or progress; the backend owns key/label/color,
  // these defaults only apply while the field is absent.
  const FRESH_KEYS = ["fresh", "warm", "steady", "quiet", "cold"];
  const FRESH_DEFAULT = {
    fresh: { min: 1, color: "#16a34a" },
    warm: { min: 2, color: "#06b6d4" },
    steady: { min: 5, color: "#2563eb" },
    quiet: { min: 10, color: "#f59e0b" },
    cold: { min: 15, color: "#8b93c0" },
  };
  const CLOSED_COLOR = "#94a3b8";
  const OTHER_COLOR = "#f59e0b";
  function hexColor(v) {
    const s = str(v).trim();
    if (/^#[0-9a-f]{6}$/i.test(s)) return s.toLowerCase();
    if (/^#[0-9a-f]{3}$/i.test(s)) return "#" + Array.from(s.slice(1)).map((c) => c + c).join("").toLowerCase();
    return null;
  }
  function hexRgb(hex) {
    return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(", ");
  }
  function freshKeyFor(minutes) {
    return minutes <= 1 ? "fresh" : minutes <= 2 ? "warm" : minutes <= 5 ? "steady" : minutes <= 10 ? "quiet" : "cold";
  }
  function fmtMin(n) { return `${Number.isInteger(n) ? n : n.toFixed(1)} min`; }
  function freshOf(w) {
    const state = stateKey(str(w.state));
    const f = obj(w.freshness);
    if (state === "stopped" || state === "expired" || str(f.key) === "closed") {
      const label = state === "stopped" || state === "expired" ? watchStateLabel(state) : str(f.label) || watchStateLabel(state);
      return { key: "closed", label, color: CLOSED_COLOR, minutes: null, live: false, closed: true, known: true };
    }
    let key = FRESH_KEYS.includes(str(f.key)) ? str(f.key) : "";
    let minutes = num(f.interval_minutes);
    if (minutes === null && num(w.poll_interval_seconds) !== null) minutes = num(w.poll_interval_seconds) / 60;
    if (!key && minutes !== null) key = freshKeyFor(minutes);
    if (!key) {
      return { key: "unknown", label: "Polling interval not provided", color: state === "other" ? OTHER_COLOR : CLOSED_COLOR, minutes: null, live: state === "running", closed: false, known: false };
    }
    if (minutes === null) minutes = FRESH_DEFAULT[key].min;
    return {
      key, minutes, live: state === "running", closed: false, known: true,
      label: str(f.label) || `Polling every ${fmtMin(minutes)}`,
      color: hexColor(f.color) || app.freshColors[key] || FRESH_DEFAULT[key].color,
    };
  }
  function applyFresh(node, fr) {
    const rgb = hexRgb(fr.color);
    node.style.setProperty("--fc", fr.color);
    node.style.setProperty("--fc-fill", `rgba(${rgb}, .22)`);
    node.style.setProperty("--fc-fill-a", `rgba(${rgb}, .09)`);
    node.style.setProperty("--fc-edge", `rgba(${rgb}, .6)`);
    node.style.setProperty("--fc-glow", `rgba(${rgb}, .75)`);
    node.setAttribute("data-fresh", fr.key);
    return node;
  }
  function freshChip(fr) {
    const c = el("span", { class: "fresh-chip", title: fr.closed ? fr.label : "Color = polling interval, not message age or errors" }, el("i", { "aria-hidden": "true" }), fr.label);
    c.style.setProperty("--fc", fr.color);
    c.style.setProperty("--fc-edge", `rgba(${hexRgb(fr.color)}, .6)`);
    return c;
  }
  // learn the backend palette so legend dots and cards always agree
  function learnFreshness(data) {
    let changed = false;
    for (const raw of arr(data.watches)) {
      const f = obj(obj(raw).freshness);
      const key = str(f.key), c = hexColor(f.color);
      if (FRESH_KEYS.includes(key) && c && app.freshColors[key] !== c) { app.freshColors[key] = c; changed = true; }
    }
    if (changed) for (const k of FRESH_KEYS) if (app.freshColors[k]) document.documentElement.style.setProperty("--fr-" + k, app.freshColors[k]);
  }

  // Elapsed fraction of the CURRENT inactivity/lifetime window (resets when a new post renews it).
  // Prefers remaining_seconds / deadline / window_started_at so it can advance between snapshots.
  function lifeOf(w) {
    const state = stateKey(str(w.state));
    const until = str(w.lifecycle) === "until_stop";
    const win = num(w.window_seconds);
    const hasWin = win !== null && win > 0 && !until;
    const stat = num(w.lifecycle_progress);
    let p = null, rem = null;
    if (until) return { p: null, rem: null, win: null, until: true, state };
    if (state === "stopped" || state === "expired") return { p: stat === null ? null : clamp(stat, 0, 1), rem: null, win: hasWin ? win : null, until, state };
    if (hasWin) {
      const r = num(w.remaining_seconds), dl = num(w.deadline), st = num(w.window_started_at);
      const nowS = Date.now() / 1000;
      if (r !== null) rem = r - (Date.now() - app.snapAt) / 1000;
      else if (dl !== null) rem = dl - nowS;
      else if (st !== null) rem = win - (nowS - st);
      if (rem !== null) { rem = Math.max(0, rem); p = clamp(1 - rem / win, 0, 1); }
    }
    if (p === null && stat !== null) { p = clamp(stat, 0, 1); if (rem === null) rem = num(w.remaining_seconds); }
    return { p, rem, win: hasWin ? win : null, until, state };
  }
  const pctOf = (p) => Math.round(p * 100);
  function lifeText(w) {
    const L = lifeOf(w);
    if (L.state === "stopped") return "Stopped · no longer polling";
    if (L.state === "expired") return "Expired · window ended";
    if (L.until) return "Runs until stopped manually · no expiry window";
    if (L.p === null) return "Window progress not provided";
    return `Window ${pctOf(L.p)}% elapsed` + (L.rem !== null ? ` · ${fmtDur(L.rem)} left` : "");
  }
  function lifeShort(w) {
    const L = lifeOf(w);
    if (L.state === "stopped" || L.state === "expired") return "";
    const fr = freshOf(w);
    const bits = [];
    if (fr.minutes !== null) bits.push(`${Number.isInteger(fr.minutes) ? fr.minutes : fr.minutes.toFixed(1)}m`);
    if (L.p !== null) bits.push(`${pctOf(L.p)}%`);
    else if (L.until) bits.push("ongoing");
    return bits.join(" · ");
  }
  function lifeFill(wid) {
    return el("span", { class: "life-fill", "aria-hidden": "true", data: { lfw: wid, mode: "none" } }, el("i"));
  }
  function setFill(node, w) {
    const L = lifeOf(w);
    const hide = L.state === "stopped" || L.p === null;
    const v = hide ? "0" : (L.p * 100).toFixed(1);
    if (node.getAttribute("data-p") === v) return;
    node.setAttribute("data-p", v);
    node.setAttribute("data-mode", hide ? "none" : "on");
    node.style.setProperty("--lp", v);
  }
  // advances the fills and percentage labels once per second without touching the scene structure
  function tickLifecycle() {
    if (!app.snap) return;
    const m = model();
    for (const f of document.querySelectorAll(".life-fill[data-lfw]")) {
      const w = m.watchById.get(f.getAttribute("data-lfw"));
      if (w) setFill(f, w);
    }
    for (const t of document.querySelectorAll("[data-life]")) {
      const w = m.watchById.get(t.getAttribute("data-life"));
      if (w) setText(t, lifeText(w));
    }
    for (const [id, p] of scene.pods) {
      const w = m.watchById.get(id);
      if (w) setText(p.life, lifeShort(w));
    }
  }
  // fields that change every snapshot must not invalidate render signatures
  const VOLATILE_W = ["lifecycle_progress", "remaining_seconds", "inactivity_seconds"];
  function stableW(w) {
    const c = { ...w };
    for (const k of VOLATILE_W) delete c[k];
    return c;
  }

  const EVENT_STATUS = {
    queued: ["Queued (awaiting session)", "warn"],
    claimed: ["Claimed (not yet handled)", "warn"], // delivered to a consumer, NOT handled: never "good"
    handled: ["Handled", "good"],
    pending: ["Pending", "mute"],
    sending: ["Sending", "warn"],
    delivery_unknown: ["Delivery result unknown", "bad"],
  };
  const EVENT_HINT = {
    queued: "Written to the delivery queue, waiting for the session to claim it (not the same as handled)",
    claimed: "Claimed by the session; completion not yet confirmed (not the same as handled)",
  };
  function eventStatus(s) {
    return EVENT_STATUS[s] || [str(s) || "Unknown status", "mute"];
  }
  // status explanation plus optional claim:{batch_id, claimed_at}
  function eventHint(e) {
    const c = obj(e.claim);
    const bits = [];
    if (EVENT_HINT[str(e.status)]) bits.push(EVENT_HINT[str(e.status)]);
    if (num(c.claimed_at) !== null) bits.push(`Claimed at ${fmtClock(c.claimed_at, true)}`);
    if (str(c.batch_id)) bits.push(`Batch ${short(str(c.batch_id), 12)}`);
    return bits.join(" · ");
  }
  function pill(text, tone, live, title) {
    return el("span", { class: "pill" + (live ? " live" : ""), text, title, data: { tone: tone || "mute" } });
  }
  const TYPE_LABEL = { dm: ["DM", "DM"], channel_thread: ["THREAD", "Thread"], channel: ["CHANNEL", "Channel"] };

  /* ---------- people ---------- */

  function person(p, fallbackId) {
    const o = obj(p);
    const id = str(o.id || fallbackId);
    const count = cnt(o.message_count);
    return {
      id,
      name: str(o.name || o.real_name || o.display_name) || id || "Unknown",
      avatar: localUrl(o.avatar_url),
      owner: o.is_owner === true,
      count,
      last: num(o.last_interaction),
      status: str(o.profile_status),
      observed: o.observed === undefined ? count > 0 : o.observed === true,
    };
  }
  function sortPeople(list) {
    return list.sort((a, b) => (Number(b.owner) - Number(a.owner)) || ((b.last || 0) - (a.last || 0)) || (b.count - a.count) || a.name.localeCompare(b.name));
  }
  // owner is always shown; the remaining slots go to the latest active peers
  function pickVisible(people) {
    const own = people.find((p) => p.owner);
    const rest = people.filter((p) => !p.owner);
    return own ? [own, ...rest.slice(0, 2)] : rest.slice(0, 3);
  }
  function levelFrom(n) { return n > 0 ? clamp(Math.log1p(n) / Math.log1p(60), 0, 1) : 0; }
  function avatarApiUrl(id, m) {
    return m.avatarApi && /^[UW][A-Z0-9]{5,}$/.test(id) ? "/api/avatars/" + id : null;
  }

  function avatarEl(p, o) {
    const opt = o || {};
    const face = el("span", { class: "av-face" }, el("span", { text: initialOf(p.name), "aria-hidden": "true" }));
    const a = el("span", {
      class: "av" + (p.owner ? " owner" : "") + (p.owner && !p.observed ? " quiet" : "") + (opt.lg ? " lg" : "") + (opt.pulse ? " pulse" : ""),
      data: { pid: p.id }, vars: { "--ah": hash(p.id || p.name) % 360 },
    }, face);
    if (p.avatar) {
      const img = el("img", { src: p.avatar, alt: "", loading: "lazy", decoding: "async", draggable: "false" });
      img.addEventListener("error", () => img.remove(), { once: true });
      face.append(img);
    }
    if (opt.tip !== false) tipMap.set(a, () => personTip(p));
    return a;
  }
  function moreBubble(n) {
    const b = el("span", { class: "av more", text: "+" + n, "aria-hidden": "true" });
    tipMap.set(b, () => ({ tag: "MORE", title: `${n} more participants`, lines: ["Click the avatar group to see the full list and interaction counts"] }));
    return b;
  }
  function avatarGroup(visible, overflow, fresh) {
    const g = el("span", { class: "avs" });
    for (const p of visible) g.append(avatarEl(p, { pulse: !!(fresh && fresh.has(p.id)) }));
    if (overflow > 0) g.append(moreBubble(overflow));
    return g;
  }
  function personTip(p) {
    const lines = [];
    if (p.observed) {
      lines.push(`${p.count} messages observed`);
      lines.push(`Last interaction ${p.last !== null ? `${fmtRel(p.last)} · ${fmtClock(p.last, true)}` : "—"}`);
    } else if (p.owner) {
      lines.push("0 messages · no messages from the owner observed here yet");
    } else lines.push("No observed interactions yet");
    if (p.status === "pending") lines.push("Syncing avatar");
    else if (p.status === "error") lines.push("Avatar unavailable, showing initial");
    return { tag: p.owner ? "OWNER" : "PERSON · Colleague", title: p.name, lines };
  }

  /* ---------- derived view model ---------- */

  let modelCache = null;
  function model() {
    const snap = app.snap;
    if (modelCache && modelCache.snap === snap) return modelCache.m;
    const sessions = arr(snap && snap.sessions).map(obj);
    const watches = arr(snap && snap.watches).map(obj);
    const events = arr(snap && snap.events).map(obj);
    const sessByKey = new Map(sessions.map((s) => [str(s.key), s]));
    const watchById = new Map(watches.map((w) => [str(w.id), w]));
    const eventsByWatch = new Map();
    for (const e of events) pushTo(eventsByWatch, str(e.watch_id), e);
    const ownerRaw = snap && snap.owner && typeof snap.owner === "object" ? snap.owner : null;
    const owner = ownerRaw ? { ...person(ownerRaw), owner: true } : null;
    const m = {
      sessions, watches, events, sessByKey, watchById, eventsByWatch, owner,
      avatarApi: !!(snap && snap.avatar_cache),
      vms: new Map(),
      sessLabel: (key) => {
        const s = sessByKey.get(str(key));
        return s ? str(s.label || s.id || s.key) : str(key) || "Unknown session";
      },
    };
    modelCache = { snap, m };
    return m;
  }

  // Fallback when the API has no participant fields yet: only real IDs from peers / event authors, never invented people.
  function fallbackPeople(w, m) {
    const map = new Map();
    const touch = (id, prof) => {
      let p = map.get(id);
      if (!p) {
        const base = prof ? person(prof, id) : person({}, id);
        p = { ...base, id, count: 0, last: null, observed: false };
        if (!p.avatar) p.avatar = avatarApiUrl(id, m);
        if (m.owner && id === m.owner.id) { p.owner = true; p.name = m.owner.name; p.avatar = m.owner.avatar || p.avatar; }
        map.set(id, p);
      }
      return p;
    };
    if (m.owner) touch(m.owner.id, m.owner);
    for (const raw of arr(w.peers)) {
      const id = str(typeof raw === "object" && raw ? raw.id : raw);
      if (id) touch(id, typeof raw === "object" ? raw : null);
    }
    for (const e of m.eventsByWatch.get(str(w.id)) || []) {
      const prof = obj(e.author_profile);
      const id = str(prof.id || e.author);
      if (!id) continue;
      const p = touch(id, e.author_profile || null);
      p.count += 1;
      p.observed = true;
      const t = num(e.received) ?? num(e.ts);
      if (t !== null && (p.last === null || t > p.last)) p.last = t;
    }
    return sortPeople([...map.values()]);
  }

  function inferType(w) {
    const t = str(w.conversation_type);
    if (t === "dm" || t === "channel_thread" || t === "channel") return t;
    if (/^D/.test(str(w.channel))) return "dm";
    return str(w.thread) ? "channel_thread" : "channel";
  }

  function wvm(w, m) {
    const id = str(w.id);
    const hit = m.vms.get(id);
    if (hit) return hit;
    const type = inferType(w);
    const apiVisible = arr(w.visible_participants).map((p) => person(p));
    const apiAll = arr(w.participants).map((p) => person(p));
    const fromApi = apiAll.length > 0 || apiVisible.length > 0;
    const people = apiAll.length ? apiAll : apiVisible.length ? apiVisible : fallbackPeople(w, m);
    const visible = apiVisible.length ? apiVisible.slice(0, 3) : pickVisible(people);
    const total = fromApi && num(w.participant_count) !== null ? cnt(w.participant_count) : people.length;
    const overflow = fromApi && num(w.overflow_count) !== null ? cnt(w.overflow_count) : Math.max(0, total - visible.length);
    const interaction = num(w.interaction_count) !== null ? cnt(w.interaction_count) : people.reduce((a, p) => a + p.count, 0);
    const level = num(w.activity_level) !== null ? clamp(cnt(w.activity_level), 0, 1) : levelFrom(interaction);
    const name = str(w.channel_name) || str(w.channel) || "Unknown channel";
    const title = type === "dm" ? (name.replace(/^\s*DM\s*[·・•-]\s*/i, "") || name) : name;
    const t = num(w.thread);
    const sub = TYPE_LABEL[type][1] + (t !== null && type !== "channel" ? ` · ${fmtMinute(t)}` : "");
    const vm = {
      id, w, type, name, title, sub, people, visible, total, overflow, interaction, level,
      source: str(w.participant_source), partial: fromApi ? w.participants_partial === true : true,
      state: stateKey(str(w.state)), sessionKey: str(w.session_key),
      hay: [id, w.channel, name, w.thread, w.scope, w.mode, w.state, w.provider, w.session_key, w.session_id, TYPE_LABEL[type].join(" "), ...people.map((p) => p.name)]
        .map(str).join(" ").toLowerCase(),
    };
    m.vms.set(id, vm);
    return vm;
  }

  // dock-level people always aggregate the watches that survived the current filter.
  // Overlapping watches (channel scope + exact threads) report identical participant_activity buckets, so those are
  // merged by [channel,root,author] taking the max, never summed. Watches without it (older API) keep the summed
  // fallback and mark the result partial: no dedup guarantee there.
  function sessionVm(s, wvs) {
    const map = new Map();
    const buckets = new Map();
    let legacy = false, legacyInteraction = 0;
    for (const vm of wvs) {
      const act = vm.w.participant_activity;
      const bucketed = Array.isArray(act);
      if (bucketed) {
        for (const raw of act) {
          const b = obj(raw);
          const author = str(b.author);
          if (!author) continue;
          const bk = [b.channel, b.root, author].map(str).join("\u0000");
          const n = cnt(b.count), t = num(b.last);
          const hit = buckets.get(bk);
          if (!hit) buckets.set(bk, { author, count: n, last: t });
          else {
            hit.count = Math.max(hit.count, n);
            if (t !== null && (hit.last === null || t > hit.last)) hit.last = t;
          }
        }
      } else {
        legacy = true;
        legacyInteraction += vm.interaction;
      }
      // bucketed watches only contribute identity here; their counts/last come from the merged buckets below
      for (const p of vm.people) {
        const k = p.id || p.name;
        const cur = map.get(k);
        if (!cur) map.set(k, bucketed ? { ...p, count: 0, last: null, observed: false } : { ...p });
        else {
          if (!bucketed) {
            cur.count += p.count;
            if (p.last !== null && (cur.last === null || p.last > cur.last)) cur.last = p.last;
            cur.observed = cur.observed || p.observed;
          }
          cur.owner = cur.owner || p.owner;
          if (!cur.avatar && p.avatar) cur.avatar = p.avatar;
        }
      }
    }
    let bucketTotal = 0;
    for (const b of buckets.values()) {
      bucketTotal += b.count;
      const p = map.get(b.author);
      if (!p) continue;
      p.count += b.count;
      if (b.count > 0) p.observed = true;
      if (b.last !== null && (p.last === null || b.last > p.last)) p.last = b.last;
    }
    const people = sortPeople([...map.values()]);
    const visible = pickVisible(people);
    return {
      s, key: str(s.key), wvs, people, visible, total: people.length, overflow: Math.max(0, people.length - visible.length),
      interaction: bucketTotal + legacyInteraction,
      level: wvs.reduce((a, v) => Math.max(a, v.level), 0),
      run: wvs.filter((v) => v.state === "running").length,
      partial: legacy || wvs.some((v) => v.partial),
    };
  }

  function sessionHay(s) {
    return [s.key, s.id, s.label, s.cwd, s.provider, provLabel(s.provider)].map(str).join(" ").toLowerCase();
  }
  function stateMatch(state) {
    const f = app.stateFilter;
    if (f === "all") return true;
    if (f === "other") return !(state === "running" || state === "stopped" || state === "expired");
    return state === f;
  }
  function ord(key) {
    let v = app.order.get(key);
    if (v === undefined) { v = app.orderN++; app.order.set(key, v); }
    return v;
  }

  function visible() {
    const m = model();
    const q = app.search.trim().toLowerCase();
    const pf = app.providerFilter;
    const provOk = (p) => pf === "all" || str(p) === pf;
    const shay = new Map();
    const sessHay = (key) => {
      if (!shay.has(key)) { const s = m.sessByKey.get(key); shay.set(key, s ? sessionHay(s) : ""); }
      return shay.get(key);
    };
    const watches = m.watches.filter((w) => provOk(w.provider) && stateMatch(str(w.state)) &&
      (!q || wvm(w, m).hay.includes(q) || sessHay(str(w.session_key)).includes(q)));
    const keys = new Set(watches.map((w) => str(w.session_key)));
    if (app.focusKey && app.snap && !keys.has(app.focusKey)) app.focusKey = null;
    const sessions = m.sessions.filter((s) => keys.has(str(s.key)));
    const watchIds = new Set(watches.map((w) => str(w.id)));
    const fk = app.focusKey;
    const events = m.events.filter((e) => provOk(e.provider) && (!fk || str(e.session_key) === fk) && watchIds.has(str(e.watch_id)));
    events.sort((a, b) => (num(b.received) || num(b.ts) || 0) - (num(a.received) || num(a.ts) || 0));
    const focusedWatches = fk ? watches.filter((w) => str(w.session_key) === fk) : watches;
    return { m, sessions, watches, focusedWatches, events: events.slice(0, EVENT_DISPLAY_MAX), eventTotal: events.length };
  }

  /* ---------- incremental render guard ---------- */

  function once(key, sig, fn) {
    if (app.sigs[key] === sig) return;
    app.sigs[key] = sig;
    fn();
  }
  function withFocusRestore(fn) {
    const a = document.activeElement;
    const fk = a && a.getAttribute ? a.getAttribute("data-fk") : null;
    fn();
    if (fk && (!document.activeElement || document.activeElement === document.body)) focusByKey([fk]);
  }
  function focusByKey(keys) {
    for (const k of keys) {
      if (!k) continue;
      const t = document.querySelector(`[data-fk="${CSS.escape(k)}"]`);
      if (t && !t.disabled && t.getClientRects().length) { t.focus(); return true; }
    }
    const sc = $("scene");
    if (sc) sc.focus();
    return false;
  }

  function setEmptyBox(node, title, sub) {
    if (!title) { node.hidden = true; node.replaceChildren(); return; }
    node.hidden = false;
    node.replaceChildren(el("b", { text: title }), ...(sub ? [el("span", { text: sub })] : []));
  }
  function filtersActive() {
    return !!(app.search.trim() || app.providerFilter !== "all" || app.stateFilter !== "running" || app.focusKey);
  }
  function emptyReason(kind) {
    if (!app.snap) {
      if (app.error) return ["Cannot fetch the state snapshot right now", `Request failed (${app.error}); retrying automatically every 2 seconds.`];
      return ["Loading…", "Waiting for the first state snapshot."];
    }
    if (app.stateFilter === "running" && !app.search.trim() && app.providerFilter === "all") return ["No running " + kind, "Switch to “History · All” to see ended records."];
    if (filtersActive()) return ["No matching " + kind, "Adjust the search or filters, or return to the overview."];
    return ["No " + kind, "The snapshot has no matching records."];
  }

  /* ================= Kage scene (Scene) ================= */

  const scene = {
    el: null, world: null, svg: null, nodeHost: null, zoneHost: null, routeHost: null, fxHost: null,
    vw: 800, vh: 600, mode: "wing",
    layout: null, layoutSig: "", layoutAspect: 0,
    docks: new Map(), pods: new Map(), zones: new Map(),
    cam: { x: 0, y: 0, k: 1 }, user: false, needFit: true, fitted: false, tween: 0,
    suppressClick: false, sm: null,
  };
  const fx = { parts: [], bursts: [], raf: 0, last: 0, sig: "" };
  const tip = { cur: null };

  /* ---------- tooltip ---------- */

  function hideTip() {
    const t = $("tip");
    if (!t.hidden) t.hidden = true;
    tip.cur = null;
  }
  function tipSpecFor(node) {
    for (let n = node; n && n !== scene.el; n = n.parentElement) {
      const f = tipMap.get(n);
      if (f) return { node: n, spec: f };
    }
    return null;
  }
  function showTip(hit, x, y) {
    const t = $("tip");
    if (tip.cur !== hit.node) {
      const s = hit.spec();
      t.replaceChildren(el("span", { class: "tip-tag", text: s.tag }), el("b", { text: s.title }), ...arr(s.lines).map((l) => el("span", { text: l })));
      tip.cur = hit.node;
    }
    t.hidden = false;
    placeTip(x, y);
  }
  function placeTip(x, y) {
    const t = $("tip");
    const w = t.offsetWidth, h = t.offsetHeight;
    let px = x + 16, py = y + 18;
    if (px + w > window.innerWidth - 8) px = x - w - 14;
    if (py + h > window.innerHeight - 8) py = y - h - 14;
    t.style.left = Math.max(8, px) + "px";
    t.style.top = Math.max(8, py) + "px";
  }

  /* ---------- layout ---------- */

  function boundIsland(pods, pad, leftExtra) {
    let x0 = -DOCK.w / 2 - (leftExtra || 0), x1 = DOCK.w / 2, y0 = -DOCK.h / 2, y1 = DOCK.h / 2;
    for (const p of pods) {
      x0 = Math.min(x0, p.x - POD.w / 2); x1 = Math.max(x1, p.x + POD.w / 2);
      y0 = Math.min(y0, p.y - POD.h / 2); y1 = Math.max(y1, p.y + POD.h / 2);
    }
    return { pods, w: x1 - x0 + 2 * pad, h: y1 - y0 + 2 * pad, cx: -x0 + pad, cy: -y0 + pad };
  }
  // dock in the middle, pods in staggered columns on both wings (at most 3 rows per column).
  // Cells never overlap by construction, so any pod count stays collision-free.
  function wingIsland(n) {
    const pods = [];
    if (n > 0) {
      const per = [Math.ceil(n / 2), Math.floor(n / 2)]; // right wing takes the extra pod
      const cps = Math.max(1, Math.ceil(per[0] / 3));
      const rowH = POD.h + 26, colW = POD.w + 30, inner = DOCK.w / 2 + 44 + POD.w / 2;
      for (let i = 0; i < n; i++) {
        const s = i % 2, k = (i - s) / 2, m = per[s];
        const rows = Math.ceil(m / cps), col = Math.floor(k / rows), row = k % rows;
        const inCol = Math.min(rows, m - col * rows);
        const jitter = ((i * 37) % 11 - 5) * 0.9;
        pods.push({
          x: (s === 0 ? 1 : -1) * (inner + col * colW),
          y: (row - (inCol - 1) / 2) * rowH + (col % 2 ? rowH / 2 : 0) + jitter,
        });
      }
    }
    return boundIsland(pods, 34, 0);
  }
  // narrow screens: dock on top, pods stacked below, routes sweep down the left margin
  function columnIsland(n) {
    const pods = [];
    for (let i = 0; i < n; i++) pods.push({ x: 0, y: DOCK.h / 2 + 64 + POD.h / 2 + i * (POD.h + 34) });
    return boundIsland(pods, 28, 62);
  }
  function layoutScene(islands, mode, vw, vh) {
    const built = islands.map((i) => (mode === "column" ? columnIsland(i.wvs.length) : wingIsland(i.wvs.length)));
    const GAP = mode === "column" ? 40 : 70, PAD = 50;
    const n = built.length;
    const sum = (a) => a.reduce((x, y) => x + y, 0);
    let best = null;
    const maxCols = mode === "column" ? 1 : Math.max(1, Math.min(n, 5));
    for (let cols = 1; cols <= maxCols; cols++) {
      const rows = Math.ceil(n / cols);
      const colW = new Array(cols).fill(0), rowH = new Array(rows).fill(0);
      built.forEach((b, i) => {
        const c = i % cols, r = Math.floor(i / cols);
        colW[c] = Math.max(colW[c], b.w); rowH[r] = Math.max(rowH[r], b.h);
      });
      const W = sum(colW) + GAP * (cols - 1) + 2 * PAD, H = sum(rowH) + GAP * (rows - 1) + 2 * PAD;
      const k = Math.min(vw / W, vh / H);
      if (!best || k > best.k + 0.001) best = { cols, colW, rowH, W, H, k };
    }
    const out = { W: best.W, H: best.H, mode, islands: [] };
    built.forEach((b, i) => {
      const c = i % best.cols, r = Math.floor(i / best.cols);
      let x = PAD, y = PAD;
      for (let cc = 0; cc < c; cc++) x += best.colW[cc] + GAP;
      for (let rr = 0; rr < r; rr++) y += best.rowH[rr] + GAP;
      x += (best.colW[c] - b.w) / 2; y += (best.rowH[r] - b.h) / 2;
      const dcx = x + b.cx, dcy = y + b.cy;
      out.islands.push({
        key: islands[i].key, x, y, w: b.w, h: b.h, cx: dcx, cy: dcy,
        pods: b.pods.map((p, j) => ({ id: islands[i].wvs[j].id, cx: dcx + p.x, cy: dcy + p.y })),
      });
    });
    return out;
  }
  function rectEdge(cx, cy, hw, hh, tx, ty) {
    const dx = tx - cx, dy = ty - cy;
    if (!dx && !dy) return { x: cx, y: cy };
    const s = Math.min(dx ? hw / Math.abs(dx) : Infinity, dy ? hh / Math.abs(dy) : Infinity);
    return { x: cx + dx * s, y: cy + dy * s };
  }
  function routeD(mode, dk, pd, idx) {
    const f = (v) => v.toFixed(1);
    if (mode === "column") {
      const sx = dk.cx, sy = dk.cy + DOCK.h / 2 - 6, ex = pd.cx - POD.w / 2 + 4, ey = pd.cy;
      return `M ${f(sx)} ${f(sy)} C ${f(sx - 110)} ${f(sy + 46)}, ${f(ex - 80)} ${f(ey - 56)}, ${f(ex)} ${f(ey)}`;
    }
    const a = rectEdge(dk.cx, dk.cy, DOCK.w / 2 - 6, DOCK.h / 2 - 6, pd.cx, pd.cy);
    const b = rectEdge(pd.cx, pd.cy, POD.w / 2 - 3, POD.h / 2 - 3, dk.cx, dk.cy);
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len;
    const bend = Math.min(70, len * 0.24) * (idx % 2 ? -1 : 1);
    return `M ${f(a.x)} ${f(a.y)} C ${f(a.x + dx * 0.35 + nx * bend)} ${f(a.y + dy * 0.35 + ny * bend)}, ${f(b.x - dx * 0.35 - nx * bend)} ${f(b.y - dy * 0.35 - ny * bend)}, ${f(b.x)} ${f(b.y)}`;
  }

  /* ---------- scene model ---------- */

  function sceneModel(v) {
    const byS = new Map();
    for (const w of v.watches) pushTo(byS, str(w.session_key), wvm(w, v.m));
    let list = v.sessions.map((s) => ({ s, key: str(s.key), wvs: byS.get(str(s.key)) || [] })).filter((x) => x.wvs.length);
    list.sort((a, b) => ord("s:" + a.key) - ord("s:" + b.key));
    const total = list.length;
    list = list.slice(0, app.sceneLimit);
    const islands = list.map((x) => {
      x.wvs.sort((a, b) => ord("w:" + a.id) - ord("w:" + b.id));
      return { ...x, sv: sessionVm(x.s, x.wvs) };
    });
    return { islands, total, hidden: total - islands.length };
  }

  /* ---------- fresh (real) arrivals ---------- */

  function isFresh(e) { const t = app.fresh.get(str(e.id)); return !!t && t > Date.now(); }
  function isWatchFresh(wid) {
    return arr(app.snap && app.snap.events).some((e) => str(obj(e).watch_id) === wid && isFresh(obj(e)));
  }
  function freshAuthors(wid) {
    const s = new Set();
    for (const e of arr(app.snap && app.snap.events)) {
      const o = obj(e);
      if (str(o.watch_id) === wid && isFresh(o)) s.add(str(obj(o.author_profile).id || o.author));
    }
    return s;
  }

  /* ---------- node builders ---------- */

  function tiltBind(card) {
    card.addEventListener("pointermove", (e) => {
      if (!app.motion || e.pointerType === "touch") return;
      const r = card.getBoundingClientRect();
      const px = (e.clientX - r.left) / Math.max(1, r.width) - 0.5, py = (e.clientY - r.top) / Math.max(1, r.height) - 0.5;
      card.style.setProperty("--ry", (px * 9).toFixed(2) + "deg");
      card.style.setProperty("--rx", (-py * 9).toFixed(2) + "deg");
    });
    card.addEventListener("pointerleave", () => { card.style.removeProperty("--rx"); card.style.removeProperty("--ry"); });
  }
  function bobVars(id) {
    return { "--bob": (5.2 + rand01(hash(id)) * 2.4).toFixed(2) + "s", "--bd": "-" + (rand01(hash(id) + 7) * 6).toFixed(2) + "s" };
  }
  function fillPeople(btn, x, prefix, fresh) {
    btn.replaceChildren();
    if (!x.visible.length) {
      btn.classList.add("empty-people");
      btn.disabled = true;
      btn.removeAttribute("aria-label");
      btn.textContent = "No participant data yet";
      return;
    }
    btn.classList.remove("empty-people");
    btn.disabled = false;
    btn.append(avatarGroup(x.visible, x.overflow, fresh));
    btn.setAttribute("aria-label", `${prefix}: ${x.visible.map((p) => p.name).join(", ")}${x.overflow ? `, ${x.total} in total` : ""}. View the full list`);
  }

  function createDock(key, prov) {
    const r = { key, prov, placed: false, sig: "", cur: null, peopleIds: new Set(), pos: null };
    r.ptag = el("span", { class: "ptag" });
    r.note = el("span", { class: "dk-note", hidden: true });
    r.runPill = el("span", { class: "dk-run" });
    r.name = el("span", { class: "dk-name" });
    r.cwd = el("span", { class: "dk-cwd" });
    const stage = el("span", { class: "dk-stage" },
      el("span", { class: "think", "aria-hidden": "true" }, el("i"), el("i"), el("i")),
      characterNode(prov), badgeNode(prov));
    r.main = el("button", { type: "button", class: "dock-main", "data-fk": "d:" + key, "aria-haspopup": "dialog" },
      el("span", { class: "dk-top" }, r.ptag, r.note, r.runPill), stage, r.name, r.cwd);
    r.people = el("button", { type: "button", class: "people-btn", "data-fk": "dp:" + key });
    r.meta = el("span", { class: "pd-meta" });
    r.card = el("div", { class: "card" }, r.main, el("div", { class: "dk-foot" }, r.people, r.meta));
    r.el = theme(el("div", { class: "node dock entering", vars: bobVars(key) }, r.card), prov);
    // card click opens the session inspector; scene focus lives on its own "Focus session" button and the dock rail
    r.main.addEventListener("click", () => openSession(key, ["d:" + key]));
    r.people.addEventListener("click", () => openPeople({ type: "session", key }, ["dp:" + key]));
    // whitespace on the card opens it too; embedded controls (avatars) keep their own actions
    r.el.addEventListener("click", (event) => {
      if (event.target.closest("button, a, input, select, textarea, [role='button']")) return;
      openSession(key, ["d:" + key]);
    });
    tipMap.set(r.card, () => dockTip(r));
    tiltBind(r.card);
    setTimeout(() => r.el.classList.remove("entering"), 900);
    return r;
  }
  function dockTip(r) {
    const isl = r.cur;
    if (!isl) return { tag: "SESSION", title: r.key, lines: [] };
    const s = isl.s, info = provInfo(s.provider);
    const lines = [`${info.label}${info.note ? " · " + info.note : ""}`];
    if (s.cwd) lines.push(str(s.cwd));
    lines.push(`${isl.sv.run} running / ${isl.sv.wvs.length} watches (current filter)`);
    lines.push(`${isl.sv.total} participants · ${isl.sv.interaction} observed interactions`);
    if (num(s.last_activity) !== null) lines.push(`Last activity ${fmtRel(s.last_activity)}`);
    lines.push("Click for session details (to focus, use the button inside or the quick-focus bar below)");
    return { tag: "SESSION · AI session", title: str(s.label || s.id || r.key), lines };
  }
  function updateDock(r, isl) {
    const s = isl.s, sv = isl.sv;
    r.cur = isl;
    r.peopleIds = new Set(sv.people.map((p) => p.id));
    const label = str(s.label || s.id || isl.key);
    const info = provInfo(s.provider);
    const sig = JSON.stringify([label, s.cwd, info.label, info.note, sv.run, sv.wvs.length, sv.level.toFixed(2),
      sv.visible.map((p) => [p.id, p.name, p.avatar, p.count, p.observed]), sv.overflow, sv.total, sv.interaction]);
    if (sig === r.sig) return;
    r.sig = sig;
    r.el.setAttribute("data-run", sv.run > 0 ? "1" : "0");
    r.el.style.setProperty("--lv", sv.level.toFixed(2));
    r.ptag.textContent = info.label;
    r.note.hidden = !info.note;
    r.note.textContent = info.note;
    r.runPill.replaceChildren(sv.run > 0 ? pill(`${sv.run} running`, "good", true) : pill("No running watches", "mute"));
    r.name.textContent = label;
    r.cwd.textContent = str(s.cwd) || "(no workspace path)";
    r.main.setAttribute("aria-label", `View session details: ${label}, ${info.label}, ${sv.run} running watches`);
    fillPeople(r.people, sv, `Participants of ${label}`);
    r.meta.replaceChildren(el("span", { text: `${sv.interaction} interactions` }), el("span", { class: "meter" }, el("i")));
  }

  function createPod(id, sk, prov) {
    const r = { id, sk, prov, placed: false, sig: "", vm: null, peopleIds: new Set(), pos: null };
    r.chip = el("span", { class: "type-chip" });
    r.title = el("span", { class: "pd-title" });
    r.led = el("i", { class: "led", "aria-hidden": "true" });
    r.sub = el("span", { class: "pd-sub" });
    r.main = el("button", { type: "button", class: "pod-main", "data-fk": "p:" + id },
      el("span", { class: "pd-top" }, r.chip, r.title, r.led), r.sub);
    r.people = el("button", { type: "button", class: "people-btn", "data-fk": "pp:" + id });
    r.meta = el("span", { class: "pd-meta" });
    r.life = el("span", { class: "pd-life", "aria-hidden": "true" });
    r.fill = lifeFill(id);
    r.card = el("div", { class: "card" }, r.fill, r.main, el("div", { class: "pd-foot" }, r.people, r.meta));
    r.el = theme(el("div", { class: "node pod entering", vars: bobVars(id) }, r.card), prov);
    r.route = createRoute(id, prov, r);
    r.main.addEventListener("click", () => openDetail(id, ["p:" + id]));
    r.people.addEventListener("click", () => openPeople({ type: "watch", id }, ["pp:" + id]));
    // The stationary node remains clickable while its visual card lifts on hover.
    // Embedded controls retain their own actions.
    r.el.addEventListener("click", (event) => {
      if (event.target.closest("button, a, input, select, textarea, [role='button']")) return;
      openDetail(id, ["p:" + id]);
    });
    r.card.addEventListener("pointerenter", () => r.route.g.classList.add("hover"));
    r.card.addEventListener("pointerleave", () => r.route.g.classList.remove("hover"));
    tipMap.set(r.card, () => podTip(r));
    tiltBind(r.card);
    setTimeout(() => r.el.classList.remove("entering"), 900);
    return r;
  }
  function podTip(r) {
    const vm = r.vm;
    if (!vm) return { tag: "THREAD", title: r.id, lines: [] };
    const w = vm.w;
    const lines = [`${TYPE_LABEL[vm.type][1]} · ${watchStateLabel(str(w.state))} · ${provLabel(w.provider)}`];
    if (vm.sub && vm.type !== "dm" && vm.type !== "channel") lines.push(vm.sub);
    lines.push(`${vm.total} participants · ${vm.interaction} observed interactions`);
    lines.push(num(w.last_poll) !== null ? `Last polled ${fmtRel(w.last_poll)}` : "Not polled yet");
    const fr = freshOf(w);
    lines.push(fr.closed ? `State: ${fr.label} (no longer polling)` : `Polling cadence: ${fr.label} (color = polling interval, not message age or errors)`);
    lines.push(lifeText(w));
    if (w.lifecycle === "idle") lines.push("Auto-closes after 48 hours without new messages · new posts renew the deadline");
    if (w.expiry_pending_check) lines.push("Expiry pending check: retrying the Slack read");
    lines.push("Click for thread details");
    return { tag: "THREAD · " + TYPE_LABEL[vm.type][0], title: vm.name, lines };
  }
  function updatePod(r, vm) {
    r.vm = vm;
    r.peopleIds = new Set(vm.people.map((p) => p.id));
    const w = vm.w;
    const fresh = isWatchFresh(vm.id);
    const fa = fresh ? freshAuthors(vm.id) : null;
    const fr = freshOf(w);
    const sig = JSON.stringify([vm.type, vm.title, vm.sub, vm.state, num(w.last_poll), vm.level.toFixed(2), vm.interaction, vm.total, vm.overflow,
      vm.visible.map((p) => [p.id, p.name, p.avatar, p.count, p.observed]), fresh, fa ? [...fa] : 0,
      fr.key, fr.label, fr.color, fr.minutes, w.lifecycle, w.window_seconds, w.window_started_at]);
    r.route.running = vm.state === "running";
    r.route.level = vm.level;
    r.route.g.setAttribute("data-state", vm.state);
    r.route.g.style.setProperty("--rw", (2.6 + vm.level * 3).toFixed(1));
    if (sig === r.sig) return;
    r.sig = sig;
    r.el.setAttribute("data-state", vm.state);
    r.el.setAttribute("data-live", fr.live ? "1" : "0");
    applyFresh(r.el, fr);
    r.el.style.setProperty("--lv", vm.level.toFixed(2));
    r.el.classList.toggle("pulse", fresh);
    r.chip.textContent = TYPE_LABEL[vm.type][0];
    r.chip.setAttribute("data-type", vm.type);
    r.title.textContent = vm.title;
    const stateTxt = watchStateLabel(str(w.state));
    const cadence = fr.closed ? "" : `, polling cadence ${fr.label}`;
    r.main.setAttribute("aria-label", `${TYPE_LABEL[vm.type][1]} ${vm.title}, ${stateTxt}${cadence}, ${vm.total} participants, ${vm.interaction} observed interactions. Open thread details`);
    r.sub.replaceChildren(vm.sub + " · ", num(w.last_poll) !== null ? relNode(w.last_poll) : "Not polled yet");
    fillPeople(r.people, vm, `Participants of ${vm.title}`, fa);
    setFill(r.fill, w);
    setText(r.life, lifeShort(w));
    r.meta.replaceChildren(
      ...(vm.state === "running" ? [r.life] : [el("span", { class: "pd-state", text: stateTxt })]),
      el("span", { text: `${vm.interaction} interactions` }),
      el("span", { class: "meter" }, el("i")));
  }

  function createRoute(id, prov, podRef) {
    const glow = sv("path", { class: "r-glow" }), core = sv("path", { class: "r-core" });
    const flow = sv("path", { class: "r-flow" }), hit = sv("path", { class: "r-hit" });
    const g = theme(sv("g", { class: "route", "data-wid": id }), prov);
    g.append(glow, core, flow, hit);
    hit.addEventListener("click", () => openDetail(id, ["p:" + id]));
    hit.addEventListener("pointerenter", () => { g.classList.add("hover"); podRef.el.classList.add("peek"); });
    hit.addEventListener("pointerleave", () => { g.classList.remove("hover"); podRef.el.classList.remove("peek"); });
    tipMap.set(hit, () => podTip(podRef));
    return { g, glow, core, flow, hit, prov, d: "", L: 0, running: false, level: 0 };
  }

  function setPos(ref, x, y) {
    const n = ref.el;
    n.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
    if (!ref.placed) {
      // first placement must not slide in from the origin
      const prev = n.style.transition;
      n.style.transition = "none";
      void n.offsetWidth;
      n.style.transition = prev;
      ref.placed = true;
    }
  }
  function dropMissing(map, seen, parts) {
    for (const [k, ref] of map) {
      if (seen.has(k)) continue;
      map.delete(k);
      const nodes = parts(ref);
      for (const n of nodes) n.classList.add("leaving");
      setTimeout(() => nodes.forEach((n) => n.remove()), 480);
    }
  }

  function reconcileScene(sm) {
    const seenD = new Set(), seenP = new Set();
    for (const isl of sm.islands) {
      seenD.add(isl.key);
      let d = scene.docks.get(isl.key);
      if (!d) { d = createDock(isl.key, isl.s.provider); scene.docks.set(isl.key, d); scene.nodeHost.append(d.el); }
      updateDock(d, isl);
      if (!scene.zones.has(isl.key)) {
        const z = theme(el("div", { class: "zone" }), isl.s.provider);
        scene.zones.set(isl.key, z);
        scene.zoneHost.append(z);
      }
      for (const vm of isl.wvs) {
        seenP.add(vm.id);
        let p = scene.pods.get(vm.id);
        if (!p) {
          p = createPod(vm.id, isl.key, isl.s.provider);
          scene.pods.set(vm.id, p);
          scene.nodeHost.append(p.el);
          scene.routeHost.append(p.route.g);
        }
        p.sk = isl.key;
        updatePod(p, vm);
      }
    }
    dropMissing(scene.docks, seenD, (d) => [d.el]);
    dropMissing(scene.pods, seenP, (p) => [p.el, p.route.g]);
    dropMissing(scene.zones, seenD, (z) => [z]);
  }

  function placeAll() {
    const lay = scene.layout;
    if (!lay) {
      scene.world.style.width = "0px";
      scene.world.style.height = "0px";
      return;
    }
    scene.world.style.width = lay.W + "px";
    scene.world.style.height = lay.H + "px";
    scene.svg.setAttribute("width", String(lay.W));
    scene.svg.setAttribute("height", String(lay.H));
    for (const li of lay.islands) {
      const d = scene.docks.get(li.key);
      if (!d) continue;
      setPos(d, li.cx - DOCK.w / 2, li.cy - DOCK.h / 2);
      d.pos = { cx: li.cx, cy: li.cy };
      const z = scene.zones.get(li.key);
      if (z) {
        const gx = li.w * 0.03, gy = li.h * 0.03;
        z.style.left = li.x - gx + "px"; z.style.top = li.y - gy + "px";
        z.style.width = li.w + gx * 2 + "px"; z.style.height = li.h + gy * 2 + "px";
        z.style.borderRadius = lay.mode === "column" ? "56px" : "";
      }
      li.pods.forEach((lp, idx) => {
        const p = scene.pods.get(lp.id);
        if (!p) return;
        setPos(p, lp.cx - POD.w / 2, lp.cy - POD.h / 2);
        p.pos = { cx: lp.cx, cy: lp.cy };
        const r = p.route;
        r.d = routeD(lay.mode, li, lp, idx);
        for (const n of [r.glow, r.core, r.flow, r.hit]) n.setAttribute("d", r.d);
        r.L = r.core.getTotalLength();
      });
    }
  }

  /* ---------- focus / dimming ---------- */

  function applyFocus() {
    const fk = app.focusKey, sel = app.selectedWatch, fp = app.focusPerson;
    const selOn = !!(sel && scene.pods.has(sel));
    const selSk = selOn ? scene.pods.get(sel).sk : null;
    for (const [id, p] of scene.pods) {
      const dim = !!((fk && p.sk !== fk) || (selOn && sel !== id) || (fp && !p.peopleIds.has(fp)));
      p.el.classList.toggle("dim", dim);
      p.el.classList.toggle("hot", selOn && sel === id);
      p.route.g.classList.toggle("dim", dim);
      p.route.g.classList.toggle("hot", selOn && sel === id);
    }
    for (const [key, d] of scene.docks) {
      const dim = !!((fk && key !== fk) || (selSk && key !== selSk) || (fp && !d.peopleIds.has(fp)));
      d.el.classList.toggle("dim", dim);
      d.el.classList.toggle("focused", fk === key);
      const z = scene.zones.get(key);
      if (z) { z.classList.toggle("dim", dim); z.classList.toggle("focused", fk === key); }
    }
  }
  function setFocus(key) {
    const prev = app.focusKey;
    app.focusKey = key;
    app.sigs.watches = null; app.sigs.timeline = null; app.sigs.summary = null;
    renderAll();
    if (key) focusIsland(key);
    else if (prev) { scene.user = false; fitAll(); }
  }
  function setPerson(id) {
    app.focusPerson = id || null;
    renderAll();
  }
  function findPerson(id) {
    if (!app.snap) return null;
    const m = model();
    for (const w of m.watches) {
      const p = wvm(w, m).people.find((x) => x.id === id);
      if (p) return p;
    }
    return null;
  }

  /* ---------- camera ---------- */

  function applyCam() {
    const c = scene.cam;
    scene.world.style.transform = `translate(${c.x.toFixed(2)}px, ${c.y.toFixed(2)}px) scale(${c.k.toFixed(4)})`;
    const s = scene.el.style;
    s.setProperty("--k", c.k.toFixed(4));
    s.setProperty("--px", c.x.toFixed(1) + "px");
    s.setProperty("--py", c.y.toFixed(1) + "px");
  }
  function clampCam(c) {
    const lay = scene.layout;
    if (!lay) return c;
    const W = lay.W * c.k, H = lay.H * c.k, padPx = 120;
    c.x = W + 2 * padPx <= scene.vw ? (scene.vw - W) / 2 : clamp(c.x, scene.vw - W - padPx, padPx);
    c.y = H + 2 * padPx <= scene.vh ? (scene.vh - H) / 2 : clamp(c.y, scene.vh - H - padPx, padPx);
    return c;
  }
  function cancelTween() {
    if (scene.tween) cancelAnimationFrame(scene.tween);
    scene.tween = 0;
  }
  function flyTo(target, instant) {
    cancelTween();
    clampCam(target);
    if (instant || !app.motion) { scene.cam = target; applyCam(); return; }
    const from = { ...scene.cam }, t0 = performance.now(), dur = 620;
    const step = (now) => {
      const p = clamp((now - t0) / dur, 0, 1), e = 1 - Math.pow(1 - p, 3);
      scene.cam = {
        k: from.k * Math.pow(target.k / from.k, e),
        x: from.x + (target.x - from.x) * e,
        y: from.y + (target.y - from.y) * e,
      };
      applyCam();
      scene.tween = p < 1 ? requestAnimationFrame(step) : 0;
    };
    scene.tween = requestAnimationFrame(step);
  }
  function insets() {
    // top inset leaves room for the HUD card with its legend
    return scene.vw < 700 ? { t: 72, r: 54, b: 86, l: 8 } : { t: 92, r: 64, b: 76, l: 16 };
  }
  function hudBox() {
    // the legend card (top left) in scene pixels, or null while it is hidden
    const el = document.querySelector(".hud-tl");
    if (!el || !scene.el || !el.getClientRects().length) return null;
    const r = el.getBoundingClientRect(), s = scene.el.getBoundingClientRect();
    return r.width && r.height ? { right: r.right - s.left, bottom: r.bottom - s.top } : null;
  }
  function camWithin(rect, kMin, kMax, ins) {
    const { t, r, b, l } = ins;
    const aw = Math.max(80, scene.vw - l - r), ah = Math.max(80, scene.vh - t - b);
    const raw = Math.min(aw / rect.w, ah / rect.h);
    const k = clamp(raw, kMin, kMax);
    if (raw < kMin) return { k, x: l + 8 - rect.x * k, y: t + 4 - rect.y * k };
    return { k, x: l + (aw - rect.w * k) / 2 - rect.x * k, y: t + (ah - rect.h * k) / 2 - rect.y * k };
  }
  function camFor(rect, kMin, kMax) {
    const base = insets(), hud = hudBox();
    if (!hud) return camWithin(rect, kMin, kMax, base);
    // no node starts under the legend card: its box is reserved beside it or below it, whichever leaves the map larger
    const beside = camWithin(rect, kMin, kMax, { ...base, l: Math.max(base.l, hud.right + 12) });
    const below = camWithin(rect, kMin, kMax, { ...base, t: Math.max(base.t, hud.bottom + 12) });
    return beside.k > below.k + 1e-6 ? beside : below;
  }
  function fitAll(instant) {
    const lay = scene.layout;
    if (!lay) return;
    const kMin = scene.vw < 700 ? 0.62 : 0.34;
    let cam = camFor({ x: 0, y: 0, w: lay.W, h: lay.H }, kMin, 1.15);
    // too many islands to read at once: start on the first one instead of a tiny overview
    if (cam.k <= kMin + 1e-6 && lay.islands.length > 1) {
      const i0 = lay.islands[0];
      cam = camFor({ x: i0.x, y: i0.y, w: i0.w, h: i0.h }, kMin, 1.15);
    }
    flyTo(cam, instant);
  }
  function focusIsland(key) {
    const i = scene.layout && scene.layout.islands.find((x) => x.key === key);
    if (!i) return;
    flyTo(camFor({ x: i.x, y: i.y, w: i.w, h: i.h }, scene.vw < 700 ? 0.62 : 0.5, 1.35));
    scene.user = true;
  }
  function zoomAt(f, sx, sy) {
    const c = scene.cam, k = clamp(c.k * f, ZOOM_MIN, ZOOM_MAX), r = k / c.k;
    scene.cam = clampCam({ k, x: sx - (sx - c.x) * r, y: sy - (sy - c.y) * r });
    scene.user = true;
    applyCam();
  }
  function resetView() {
    app.focusPerson = null;
    scene.user = false;
    if (app.focusKey) setFocus(null);
    else { renderAll(); fitAll(); }
  }
  function ensureVisible(node) {
    if (scene.tween) return;
    const r = node.getBoundingClientRect(), s = scene.el.getBoundingClientRect(), m = 56;
    const topM = m + 24, rightM = m + 56;
    let dx = 0, dy = 0;
    if (r.left < s.left + m) dx = s.left + m - r.left;
    else if (r.right > s.right - rightM) dx = s.right - rightM - r.right;
    if (r.top < s.top + topM) dy = s.top + topM - r.top;
    else if (r.bottom > s.bottom - m) dy = s.bottom - m - r.bottom;
    if (!dx && !dy) return;
    const c = scene.cam;
    scene.user = true;
    flyTo({ k: c.k, x: c.x + dx, y: c.y + dy });
  }
  // keep a pod clear of the right-hand inspector sheet
  function nudgeFromSheet(id) {
    const p = scene.pods.get(id);
    if (!p || !p.pos || scene.vw < 760) return;
    const c = scene.cam, sx = c.x + p.pos.cx * c.k, sheet = Math.min(460, scene.vw) + 40;
    const limit = scene.vw - sheet;
    const sy = c.y + p.pos.cy * c.k;
    if (sx + (POD.w / 2) * c.k > limit || sx < 40 || sy < 70 || sy > scene.vh - 60) {
      const k = Math.max(c.k, 0.8);
      scene.user = true;
      flyTo({ k, x: limit / 2 - p.pos.cx * k, y: scene.vh / 2 - p.pos.cy * k });
    }
  }
  function ensureOnStage(sessionKey) {
    if (scene.docks.has(sessionKey)) return true;
    const v = visible();
    const keys = v.sessions.map((s) => str(s.key)).sort((a, b) => ord("s:" + a) - ord("s:" + b));
    const idx = keys.indexOf(sessionKey);
    if (idx < 0) return false;
    app.sceneLimit = Math.max(app.sceneLimit, Math.ceil((idx + 1) / SCENE_STEP) * SCENE_STEP);
    renderAll();
    return scene.docks.has(sessionKey);
  }
  function locateWatch(id) {
    const w = model().watchById.get(id);
    if (!w || !ensureOnStage(str(w.session_key))) { toast("This watch is not in view; check the filters.", "bad"); return; }
    const p = scene.pods.get(id);
    if (!p || !p.pos) return;
    const k = Math.max(scene.cam.k, 0.9);
    scene.user = true;
    flyTo({ k, x: scene.vw / 2 - p.pos.cx * k, y: scene.vh / 2 - p.pos.cy * k });
    p.el.classList.add("ping");
    setTimeout(() => p.el.classList.remove("ping"), 4600);
    if (scene.vw < 760) setDrawer(false);
  }

  /* ---------- signal particles (observed relation / activity, not messages) ---------- */

  function stopFx() { if (fx.raf) cancelAnimationFrame(fx.raf); fx.raf = 0; }
  function startFx() {
    if (fx.raf || !app.motion || !(fx.parts.length || fx.bursts.length)) return;
    fx.last = performance.now();
    fx.raf = requestAnimationFrame(fxTick);
  }
  function makeParticle(r, dir, t0, speed, burst) {
    const g = theme(sv("g", { class: "sig " + (burst ? "burst" : dir > 0 ? "agent" : "human") }), r.prov);
    const t2 = sv("circle", { class: "trail", r: 2, opacity: 0.25 });
    const t1 = sv("circle", { class: "trail", r: 2.8, opacity: 0.4 });
    const body = sv("circle", { class: "body", r: burst ? 6.5 : 4.2 });
    g.append(t2, t1, body);
    scene.fxHost.append(g);
    return { r, dir, t: t0, speed, g, body, t1, t2, burst, cycles: burst ? 2 : 0, done: false };
  }
  function stepParticle(p, dt) {
    const L = p.r.L;
    if (!L) return;
    p.t += (p.dir * p.speed * dt) / L;
    if (p.t > 1 || p.t < 0) {
      if (p.burst) { p.cycles -= 1; if (p.cycles <= 0) { p.done = true; return; } }
      p.t = p.t > 1 ? p.t - 1 : p.t + 1;
    }
    const path = p.r.core;
    const at = (t) => path.getPointAtLength(clamp(t, 0, 1) * L);
    const a = at(p.t), b = at(p.t - (p.dir * 12) / L), c = at(p.t - (p.dir * 24) / L);
    p.body.setAttribute("cx", a.x.toFixed(1)); p.body.setAttribute("cy", a.y.toFixed(1));
    p.t1.setAttribute("cx", b.x.toFixed(1)); p.t1.setAttribute("cy", b.y.toFixed(1));
    p.t2.setAttribute("cx", c.x.toFixed(1)); p.t2.setAttribute("cy", c.y.toFixed(1));
    p.g.setAttribute("opacity", Math.sqrt(Math.sin(Math.PI * clamp(p.t, 0, 1))).toFixed(2));
  }
  function fxTick(now) {
    const dt = Math.min(0.05, (now - fx.last) / 1000);
    fx.last = now;
    for (const p of fx.parts) stepParticle(p, dt);
    for (let i = fx.bursts.length - 1; i >= 0; i--) {
      const p = fx.bursts[i];
      stepParticle(p, dt);
      if (p.done) { p.g.remove(); fx.bursts.splice(i, 1); }
    }
    fx.raf = fx.parts.length || fx.bursts.length ? requestAnimationFrame(fxTick) : 0;
  }
  // ambient flow: only along routes of running watches; more observed activity => a few more motes
  function rebuildFx() {
    stopFx();
    scene.fxHost.replaceChildren();
    fx.parts = [];
    fx.bursts = [];
    if (!app.motion) return;
    for (const [id, p] of scene.pods) {
      const r = p.route;
      if (!r.running || !r.L) continue;
      const n = 1 + Math.round(r.level * 2);
      for (let k = 0; k < n; k++) {
        const seed = hash(id + ":" + k);
        fx.parts.push(makeParticle(r, k % 2 === 0 ? 1 : -1, rand01(seed), 70 + rand01(seed + 1) * 40 + r.level * 50, false));
      }
    }
    startFx();
  }
  // a real newly arrived event: separate coral pulse travelling pod -> dock
  function fireBurst(wid) {
    const p = scene.pods.get(wid);
    if (!p || !app.motion || !p.route.L) return;
    fx.bursts.push(makeParticle(p.route, -1, 1, 230, true));
    startFx();
  }
  function flushBursts() {
    for (const wid of app.bursts) fireBurst(wid);
    app.bursts.clear();
  }

  /* ---------- HUD, legend, rail, empty state ---------- */

  function setText(node, t) { if (node.textContent !== t) node.textContent = t; }

  function updateHud(v, sm) {
    const f = app.stateFilter;
    setText($("hud-title"), f === "running" ? "Running" : f === "all" ? "History · All" : "History · " + (WATCH_STATE_LABEL[f] || "Other"));
    const crew = new Set();
    for (const w of v.watches) for (const p of wvm(w, v.m).people) if (!p.owner) crew.add(p.id || p.name);
    let t = app.snap ? `${v.sessions.length} sessions · ${v.watches.length} watches · ${crew.size} colleagues` : "Loading…";
    if (app.snap && (app.search.trim() || app.providerFilter !== "all")) t += " · filtered";
    if (sm.hidden > 0) t += ` · showing the first ${sm.islands.length} sessions`;
    setText($("hud-counts"), t);

    const fb = $("focus-bar");
    fb.hidden = !app.focusKey;
    if (app.focusKey) setText($("focus-label"), v.m.sessLabel(app.focusKey));
    const pb = $("person-bar");
    pb.hidden = !app.focusPerson;
    if (app.focusPerson) { const p = findPerson(app.focusPerson); setText($("person-label"), p ? p.name : app.focusPerson); }

    const more = $("more-btn");
    more.hidden = sm.hidden <= 0;
    if (sm.hidden > 0) setText(more, `Show more sessions (${sm.hidden} more)`);

    const ac = obj(app.snap && app.snap.avatar_cache);
    const pill_ = $("avatar-pill");
    const labels = { refreshing: "Syncing avatars", degraded: "Avatar cache degraded", empty: "Avatar cache empty" };
    const lab = labels[str(ac.status)];
    pill_.hidden = !lab;
    if (lab) { setText(pill_, lab); pill_.title = ac.error ? "Avatar cache: " + str(ac.error) + "; initials shown when missing" : "Initials shown when missing"; }

    const sc = scene.el;
    const stale = app.error && app.snap ? "1" : "0";
    if (sc.getAttribute("data-stale") !== stale) sc.setAttribute("data-stale", stale);
  }

  function providerIds() {
    const snap = app.snap;
    const ids = new Map();
    for (const p of arr(snap && snap.providers)) { const o = obj(p); if (o.id) ids.set(str(o.id), provLabel(o.id)); }
    for (const x of [...arr(snap && snap.sessions), ...arr(snap && snap.watches), ...arr(snap && snap.events)]) {
      const id = str(obj(x).provider);
      if (id && !ids.has(id)) ids.set(id, provLabel(id));
    }
    return ids;
  }
  function renderLegend(ids) {
    once("legend", JSON.stringify([...ids, app.motion]), () => {
      const lg = $("legend");
      lg.replaceChildren();
      for (const [id] of ids) {
        const info = provInfo(id);
        lg.append(theme(el("span", { class: "lg-item" }, thumbNode(id, "lg-char", "lg-robot"), info.label, info.note ? el("span", { class: "lg-tag", text: info.note }) : null), id));
      }
      lg.append(
        el("span", { class: "lg-item" }, el("i", { class: "lg-dot" }), "Running"),
        el("span", { class: "lg-item", title: "Flowing light shows relationships and observed activity, not live messages" }, el("i", { class: "lg-flow" }), "Flow · relationships"),
        el("span", { class: "lg-item", title: "Pulses only when a new event actually appears in the snapshot" }, el("i", { class: "lg-pulse" }), "New-event pulse"));
    });
  }

  function renderRail(sm) {
    const host = $("dock-rail");
    host.hidden = sm.islands.length < 2;
    if (host.hidden) return;
    const sig = JSON.stringify([sm.islands.map((i) => [i.key, str(i.s.label || i.s.id), i.s.provider, i.sv.run]), app.focusKey]);
    once("rail", sig, () => withFocusRestore(() => {
      host.replaceChildren();
      for (const isl of sm.islands) {
        const key = isl.key, label = str(isl.s.label || isl.s.id || key);
        const chip = theme(el("button", {
          type: "button", class: "rail-chip", title: label, "data-fk": "r:" + key,
          "aria-pressed": app.focusKey === key ? "true" : "false",
          "aria-label": `${app.focusKey === key ? "Back to overview" : "Focus session"} ${label}, ${isl.sv.run} running`,
        }, thumbNode(isl.s.provider), el("span", { text: label }), isl.sv.run ? el("small", { text: String(isl.sv.run) }) : null), isl.s.provider);
        chip.addEventListener("click", () => setFocus(app.focusKey === key ? null : key));
        host.append(chip);
      }
    }));
  }

  function renderEmpty(v, sm) {
    const host = $("scene-empty");
    let kind = "";
    if (!app.snap) kind = app.error ? "error" : "loading";
    else if (!sm.islands.length) kind = app.stateFilter === "running" && !app.search.trim() && app.providerFilter === "all" ? "quiet" : "filtered";
    once("empty", kind + "|" + (app.error || ""), () => {
      host.replaceChildren();
      host.hidden = !kind;
      if (!kind) return;
      const beacon = el("span", { class: "se-beacon", "aria-hidden": "true" }, el("i"), el("i"), el("i"), el("b"));
      const card = el("div", { class: "se-card" }, beacon);
      if (kind === "loading") card.append(el("h2", { text: "Loading…" }), el("div", { class: "skeleton" }), el("p", { text: "Waiting for the first state snapshot." }));
      else if (kind === "error") {
        const b = el("button", { type: "button", class: "btn primary", text: "Retry now" });
        b.addEventListener("click", refreshNow);
        card.append(el("h2", { text: "Cannot reach the service right now" }), el("p", { text: `Request failed (${app.error}); retrying automatically every 2 seconds.` }), b);
      } else if (kind === "quiet") {
        const b = el("button", { type: "button", class: "btn primary", text: "View History · All" });
        b.addEventListener("click", () => setScope("all"));
        card.append(el("h2", { text: "No running follow-ups" }), el("p", { text: "There are no running watches right now. When a new follow-up starts, AI sessions and colleagues will appear here." }), b);
      } else {
        const b = el("button", { type: "button", class: "btn primary", text: "Clear filters" });
        b.addEventListener("click", clearFilters);
        card.append(el("h2", { text: "No matching follow-ups" }), el("p", { text: "Adjust the search, provider, or history range and try again." }), b);
      }
      host.append(card);
    });
  }

  /* ---------- scene render ---------- */

  function renderScene(v) {
    const sm = sceneModel(v);
    scene.sm = sm;
    const lsig = scene.mode + "|" + sm.islands.map((i) => i.key + ">" + i.wvs.map((x) => x.id).join(",")).join(";");
    let relayout = false;
    if (lsig !== scene.layoutSig) {
      scene.layoutSig = lsig;
      scene.layoutAspect = scene.vw / scene.vh;
      scene.layout = sm.islands.length ? layoutScene(sm.islands, scene.mode, scene.vw, scene.vh) : null;
      relayout = true;
    }
    reconcileScene(sm);
    if (relayout) {
      placeAll();
      if (!scene.user) scene.needFit = true;
      else { scene.cam = clampCam({ ...scene.cam }); applyCam(); }
    }
    if (scene.layout && scene.needFit) {
      scene.needFit = false;
      if (app.focusKey && scene.fitted) focusIsland(app.focusKey);
      else fitAll(!scene.fitted);
      scene.fitted = true;
    }
    const fsig = JSON.stringify([app.motion, scene.layoutSig, [...scene.pods].map(([id, p]) => [id, p.route.running, Math.round(p.route.level * 4)])]);
    if (fsig !== fx.sig) { fx.sig = fsig; rebuildFx(); }
    flushBursts();
    applyFocus();
    updateHud(v, sm);
    renderEmpty(v, sm);
    renderRail(sm);
  }

  /* ---------- scene input ---------- */

  function initScene() {
    scene.el = $("scene"); scene.world = $("world"); scene.svg = $("routes");
    scene.nodeHost = $("nodes"); scene.zoneHost = $("zones"); scene.routeHost = $("g-routes"); scene.fxHost = $("g-fx");
    const sc = scene.el;
    const measure = () => {
      const r = sc.getBoundingClientRect();
      scene.vw = Math.max(280, r.width);
      scene.vh = Math.max(240, r.height);
      scene.mode = scene.vw < 700 ? "column" : "wing";
    };
    measure();
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        const prevMode = scene.mode, prevAsp = scene.layoutAspect;
        measure();
        const asp = scene.vw / scene.vh;
        if (scene.mode !== prevMode || (prevAsp && Math.abs(asp - prevAsp) > 0.35)) {
          scene.layoutSig = "";
          scene.user = false;
          renderAll();
        } else if (!scene.user && scene.layout) fitAll(true);
        else if (scene.layout) { scene.cam = clampCam({ ...scene.cam }); applyCam(); }
      }).observe(sc);
      // the legend card grows once its counts arrive: the map's fit keeps clear of its new size
      const hud = document.querySelector(".hud-tl");
      let hudH = 0;
      if (hud) new ResizeObserver(() => {
        const h = hud.offsetHeight;
        if (Math.abs(h - hudH) > 2 && !scene.user && scene.layout) { hudH = h; fitAll(true); } else hudH = h;
      }).observe(hud);
    }
    applyCam();

    // hover tooltips (mouse / pen) and keyboard focus tooltips
    sc.addEventListener("pointerover", (e) => {
      if (e.pointerType === "touch") return;
      const hit = tipSpecFor(e.target);
      if (!hit) { hideTip(); return; }
      showTip(hit, e.clientX, e.clientY);
    });
    sc.addEventListener("pointermove", (e) => { if (e.pointerType !== "touch" && !$("tip").hidden) placeTip(e.clientX, e.clientY); });
    sc.addEventListener("pointerleave", hideTip);
    sc.addEventListener("focusin", (e) => {
      const t = e.target;
      if (!t || !t.matches || !t.matches(":focus-visible") || t === sc) return;
      ensureVisible(t);
      const hit = tipSpecFor(t);
      if (hit) { const r = t.getBoundingClientRect(); showTip(hit, r.left + r.width * 0.5, r.top + r.height * 0.9); }
    });
    sc.addEventListener("focusout", hideTip);

    // pan (drag) and pinch / wheel zoom
    const ptrs = new Map();
    let drag = null, pinch = null;
    const local = (e) => { const r = sc.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    sc.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      if (e.target.closest && e.target.closest(".hud, .scene-empty")) return;
      hideTip();
      cancelTween();
      ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (ptrs.size === 1) drag = { id: e.pointerId, sx: e.clientX, sy: e.clientY, cx: scene.cam.x, cy: scene.cam.y, active: false };
      else if (ptrs.size === 2) {
        const [a, b] = [...ptrs.values()];
        pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 };
        drag = null;
        scene.suppressClick = true;
        sc.classList.add("panning");
      }
    });
    sc.addEventListener("pointermove", (e) => {
      if (!ptrs.has(e.pointerId)) return;
      ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pinch && ptrs.size >= 2) {
        const [a, b] = [...ptrs.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y) || 1, mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        const r = sc.getBoundingClientRect();
        scene.cam.x += mx - pinch.mx; scene.cam.y += my - pinch.my;
        zoomAt(d / pinch.d, mx - r.left, my - r.top);
        pinch.d = d; pinch.mx = mx; pinch.my = my;
        return;
      }
      if (!drag || drag.id !== e.pointerId) return;
      const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
      if (!drag.active) {
        if (Math.hypot(dx, dy) < 6) return;
        drag.active = true;
        scene.suppressClick = true;
        try { sc.setPointerCapture(e.pointerId); } catch (_) { /* pointer already gone */ }
        sc.classList.add("panning");
      }
      scene.cam = clampCam({ k: scene.cam.k, x: drag.cx + dx, y: drag.cy + dy });
      scene.user = true;
      applyCam();
    });
    const endPtr = (e) => {
      if (!ptrs.delete(e.pointerId)) return;
      if (drag && drag.id === e.pointerId) drag = null;
      if (ptrs.size < 2) pinch = null;
      if (!ptrs.size) {
        sc.classList.remove("panning");
        setTimeout(() => { scene.suppressClick = false; }, 0);
      }
    };
    sc.addEventListener("pointerup", endPtr);
    sc.addEventListener("pointercancel", endPtr);
    // a drag must never also activate the node under the pointer
    sc.addEventListener("click", (e) => {
      if (scene.suppressClick) { e.stopPropagation(); e.preventDefault(); scene.suppressClick = false; }
    }, true);
    // two-finger horizontal scroll pans, vertical scroll zooms; the axis stays locked for the whole gesture so
    // diagonal wobble never does both. Ctrl+wheel (browser trackpad pinch) always zooms.
    let wheelAxis = null, wheelAt = 0;
    sc.addEventListener("wheel", (e) => {
      if (e.target.closest && e.target.closest(".hud")) return;
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      const dx = e.deltaX * unit, dy = e.deltaY * unit;
      if (!dx && !dy) return;
      hideTip();
      cancelTween();
      const p = local(e);
      if (e.ctrlKey) {
        wheelAxis = null;
        if (dy) zoomAt(Math.exp(-dy * 0.012), p.x, p.y);
        return;
      }
      const ax = Math.abs(dx), ay = Math.abs(dy);
      const major = ax > ay ? "x" : "y";
      const kept = wheelAxis === "x" ? ax : ay, other = wheelAxis === "x" ? ay : ax;
      if (!wheelAxis || e.timeStamp - wheelAt > WHEEL_LOCK_MS || (major !== wheelAxis && other > kept * 2 + 4)) wheelAxis = major;
      wheelAt = e.timeStamp;
      if (wheelAxis === "x") {
        if (!dx) return;
        scene.cam = clampCam({ k: scene.cam.k, x: scene.cam.x - dx, y: scene.cam.y });
        scene.user = true;
        applyCam();
      } else if (dy) zoomAt(Math.exp(-dy * 0.0016), p.x, p.y);
    }, { passive: false });

    sc.addEventListener("keydown", (e) => {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      const k = e.key;
      const own = e.target === sc;
      const step = 90;
      const pan = (dx, dy) => { e.preventDefault(); scene.user = true; flyTo({ k: scene.cam.k, x: scene.cam.x + dx, y: scene.cam.y + dy }, true); };
      if (own && k === "ArrowLeft") pan(step, 0);
      else if (own && k === "ArrowRight") pan(-step, 0);
      else if (own && k === "ArrowUp") pan(0, step);
      else if (own && k === "ArrowDown") pan(0, -step);
      else if (k === "+" || k === "=") { e.preventDefault(); zoomAt(1.25, scene.vw / 2, scene.vh / 2); }
      else if (k === "-" || k === "_") { e.preventDefault(); zoomAt(0.8, scene.vw / 2, scene.vh / 2); }
      else if (k === "0") { e.preventDefault(); scene.user = false; fitAll(); }
      else if (k === "Escape") {
        if (app.focusPerson) { e.preventDefault(); setPerson(null); }
        else if (app.focusKey) { e.preventDefault(); setFocus(null); }
      }
    });

    $("cam-in").addEventListener("click", () => zoomAt(1.3, scene.vw / 2, scene.vh / 2));
    $("cam-out").addEventListener("click", () => zoomAt(1 / 1.3, scene.vw / 2, scene.vh / 2));
    $("cam-fit").addEventListener("click", () => { scene.user = false; fitAll(); });
    $("cam-reset").addEventListener("click", resetView);
    $("clear-focus").addEventListener("click", () => setFocus(null));
    $("clear-person").addEventListener("click", () => setPerson(null));
    $("more-btn").addEventListener("click", () => { app.sceneLimit += SCENE_STEP; scene.user = false; renderAll(); });
  }

  /* ================= secondary panels ================= */

  function setDrawer(open, opts) {
    app.drawerOpen = !!open;
    document.body.setAttribute("data-drawer", open ? "open" : "closed");
    $("drawer-toggle").setAttribute("aria-expanded", open ? "true" : "false");
    if (!(opts && opts.silent)) {
      try { localStorage.setItem("fo-drawer", open ? "1" : "0"); } catch (_) { /* storage unavailable */ }
    }
    if (open) {
      app.sigs.watches = null; app.sigs.timeline = null; app.sigs.summary = null;
      renderDrawer();
      if (opts && opts.focus) $("tab-" + app.tab).focus();
    } else if (opts && opts.restore) $("drawer-toggle").focus();
  }
  function selectTab(name, focus) {
    app.tab = name;
    for (const t of document.querySelectorAll(".tab")) {
      const key = t.getAttribute("data-tab"), on = key === name;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      $("panel-" + key).hidden = !on;
    }
    if (focus) $("tab-" + name).focus();
    renderDrawer();
  }

  function stoppable(w) { return w.state !== "stopped" && w.state !== "expired"; }
  function countPills(w) {
    const ec = obj(w.event_counts);
    const defs = [["pending", "pending", "warn"], ["queued", "queued", "warn"], ["claimed", "claimed", "warn"], ["handled", "handled", "good"], ["sending", "sending", "warn"], ["delivery_unknown", "unknown", "bad"]];
    const out = defs.filter(([k]) => cnt(ec[k]) > 0).map(([k, l, t]) => pill(`${cnt(ec[k])} ${l}`, t, false, EVENT_HINT[k]));
    return out.length ? out : [pill("No events", "mute")];
  }

  function renderWatchList(v) {
    const host = $("watch-list");
    const lim = obj(app.snap && app.snap.limits);
    const items = v.focusedWatches;
    $("inv-note").textContent = (app.stateFilter === "running"
      ? "Showing running watches only; switch to “History · All” to see ended records."
      : "Showing watches that match the filters, including stopped and expired history.") +
      (lim.watches_truncated ? ` Note: truncated at the limit of ${lim.watches ?? ""}.` : "");
    const sig = JSON.stringify([items.slice(0, WATCH_LIST_MAX).map(stableW), items.length, [...app.pendingStops], app.focusKey, app.selectedWatch, !!app.snap, !!app.error, app.stateFilter]);
    once("watches", sig, () => withFocusRestore(() => {
      host.replaceChildren();
      if (!items.length) {
        const [t, s] = emptyReason("watches");
        setEmptyBox($("watch-empty"), t, s);
        return;
      }
      setEmptyBox($("watch-empty"));
      const m = v.m;
      const groups = new Map();
      const shown = items.slice(0, WATCH_LIST_MAX);
      for (const w of shown) pushTo(groups, str(w.session_key), wvm(w, m));
      const keys = [...groups.keys()].sort((a, b) => ord("s:" + a) - ord("s:" + b));
      for (const key of keys) {
        const s = m.sessByKey.get(key);
        const vms = groups.get(key).sort((a, b) => ord("w:" + a.id) - ord("w:" + b.id));
        const prov = str(s ? s.provider : vms[0].w.provider);
        const sessBtn = el("button", { type: "button", class: "btn sm", text: "Session details", "data-fk": "sd:" + key, "aria-label": `View session details: ${m.sessLabel(key)}` });
        sessBtn.addEventListener("click", () => openSession(key, ["sd:" + key]));
        const head = el("div", { class: "wl-head" }, thumbNode(prov), el("b", { text: m.sessLabel(key) }), sessBtn, el("span", { class: "count", text: String(vms.length) }));
        const group = theme(el("section", { class: "wl-group" }, head), prov);
        for (const vm of vms) group.append(watchRow(vm, m));
        host.append(group);
      }
      if (items.length > shown.length) host.append(el("p", { class: "note", text: `Listing the first ${shown.length} of ${items.length}; use search to narrow it down.` }));
    }));
  }
  function watchRow(vm, m) {
    const w = vm.w, id = vm.id;
    const title = el("button", { type: "button", class: "link-btn", text: vm.title, "data-fk": "w:" + id, "aria-label": `View thread details: ${vm.title}, ${id}` });
    title.addEventListener("click", () => openDetail(id, ["w:" + id]));
    const people = el("button", { type: "button", class: "people-btn", "data-fk": "wp:" + id });
    fillPeople(people, vm, `Participants of ${vm.title}`);
    people.addEventListener("click", () => openPeople({ type: "watch", id }, ["wp:" + id]));
    const fr = freshOf(w);
    const fill = lifeFill(id);
    const row = theme(el("div", { class: "wl-row" + (app.selectedWatch === id ? " hot" : "") },
      fill,
      el("div", { class: "wl-top" },
        pill(watchStateLabel(w.state), watchTone(w.state), w.state === "running"),
        el("span", { class: "type-chip", text: TYPE_LABEL[vm.type][0], data: { type: vm.type } }),
        title),
      el("div", { class: "wl-sub" }, `${provLabel(w.provider)} · ${vm.sub} · ${id} · `, w.last_poll ? relNode(w.last_poll) : "Not polled yet"),
      el("div", { class: "wl-line" }, freshChip(fr), el("span", { class: "wl-sub", text: lifeText(w), data: { life: id } })),
      el("div", { class: "wl-line" }, people, ...countPills(w)),
      el("div", { class: "wl-line" })), str(w.provider));
    applyFresh(row, fr);
    setFill(fill, w);
    const actions = el("div", { class: "wl-actions" });
    const loc = el("button", { type: "button", class: "btn sm", text: "Locate", "aria-label": `Locate ${vm.title} in the scene` });
    loc.addEventListener("click", () => locateWatch(id));
    actions.append(loc);
    const link = slackLink(w.slack_url, "Slack ↗");
    if (link) { link.className = "btn sm"; actions.append(link); }
    if (stoppable(w)) {
      const b = el("button", { type: "button", class: "btn danger sm", text: app.pendingStops.has(id) ? "Stopping…" : "Stop…", "data-stop-id": id, "data-fk": "s:" + id, "aria-label": `Stop watch ${id}` });
      b.disabled = app.pendingStops.has(id);
      b.addEventListener("click", () => openConfirm(id, ["s:" + id, "w:" + id]));
      actions.append(b);
    }
    row.lastChild.append(actions);
    return row;
  }

  function eventPerson(e, m) {
    const prof = obj(e.author_profile);
    const aid = str(e.author);
    if (prof.id || prof.name) return person(prof, aid);
    const w = m.watchById.get(str(e.watch_id));
    const hit = w ? wvm(w, m).people.find((p) => p.id === aid || p.name === aid) : null;
    if (hit) return hit;
    const base = person({}, aid);
    return { ...base, name: aid || "Unknown author", avatar: avatarApiUrl(aid, m) };
  }
  function renderTimeline(v) {
    const host = $("timeline");
    const lim = obj(app.snap && app.snap.limits);
    $("act-note").textContent = `Recent activity under the current filters, up to ${lim.events ?? EVENT_DISPLAY_MAX} items (${v.eventTotal} total).` +
      (lim.events_truncated ? " Older records were truncated." : "") + " Metadata only, no message bodies.";
    const sig = JSON.stringify([v.events.map((e) => [e, isFresh(e)]), !!app.snap, !!app.error, app.stateFilter]);
    once("timeline", sig, () => withFocusRestore(() => {
      host.replaceChildren();
      if (!v.events.length) {
        const [t, s] = emptyReason("activity");
        setEmptyBox($("event-empty"), t, s);
        return;
      }
      setEmptyBox($("event-empty"));
      const m = v.m;
      for (const e of v.events) {
        const [label, tone] = eventStatus(e.status);
        const when = num(e.received) ?? num(e.ts);
        const wid = str(e.watch_id);
        const w = m.watchById.get(wid);
        const vm = w ? wvm(w, m) : null;
        const who = eventPerson(e, m);
        const sub = el("div", { class: "tl-sub" });
        if (w) {
          const b = el("button", { type: "button", class: "link-btn", text: short(vm.title, 22), "data-fk": "t:" + str(e.id), "aria-label": `View thread details: ${vm.title}` });
          b.addEventListener("click", () => openDetail(wid, ["t:" + str(e.id), "w:" + wid]));
          sub.append(b);
        } else sub.append(short(wid, 14) || "—");
        sub.append(` · ${m.sessLabel(e.session_key)} · ${provLabel(e.provider)}`);
        if (e.disposition) sub.append(` · disposition ${str(e.disposition)}`);
        if (e.receipt) sub.append(` · receipt ${short(e.receipt, 10)}`);
        const link = slackLink(e.slack_url, "Slack ↗");
        if (link) sub.append(" · ", link);
        const detail = str(e.status) === "claimed" || e.claim ? eventHint(e) : "";
        host.append(el("li", { class: "tl-item" + (isFresh(e) ? " fresh" : ""), data: { tone } },
          el("div", { class: "tl-top" }, pill(label, tone, false, eventHint(e) || null), el("span", { class: "tl-time" }, relNode(when), " · " + fmtClock(when))),
          el("div", { class: "tl-who" }, avatarEl(who, { tip: false }), el("span", { text: `${who.name} · ${str(e.channel_name) || (vm ? vm.name : str(e.channel)) || "—"}` })),
          sub,
          detail ? el("div", { class: "tl-sub", text: detail }) : null));
      }
    }));
  }

  const SUMMARY_LABELS = [
    ["sessions", "Sessions"], ["watches", "Watches"], ["running", "Running"], ["stopped", "Stopped"], ["expired", "Expired"],
    ["pending", "Pending"], ["queued", "Queued (awaiting session)"], ["claimed", "Claimed (not yet handled)"], ["handled", "Handled"], ["uncertain", "Delivery unknown"], ["errors", "Errors"],
  ];
  function renderSummary(v) {
    const agg = { pending: 0, queued: 0, claimed: 0, handled: 0, uncertain: 0, errors: 0, running: 0 };
    const crew = new Set();
    for (const w of v.watches) {
      const ec = obj(w.event_counts);
      agg.pending += cnt(ec.pending); agg.queued += cnt(ec.queued); agg.claimed += cnt(ec.claimed); agg.handled += cnt(ec.handled);
      agg.uncertain += cnt(ec.sending) + cnt(ec.delivery_unknown);
      agg.errors += w.last_error ? 1 : 0;
      agg.running += w.state === "running" ? 1 : 0;
      for (const p of wvm(w, v.m).people) if (!p.owner) crew.add(p.id || p.name);
    }
    const sum = obj(app.snap && app.snap.summary);
    const ac = obj(app.snap && app.snap.avatar_cache);
    const sig = JSON.stringify([v.sessions.length, v.watches.length, crew.size, agg, sum, ac, app.stateFilter, app.snap ? 1 : 0]);
    once("summary", sig, () => {
      const host = $("summary-body");
      host.replaceChildren();
      const chip = (n, label, tone) => el("div", { class: "sum-chip", data: { tone: tone || "mute" } }, el("b", { text: String(n) }), el("span", { text: label }));
      host.append(
        el("p", { class: "note", text: "These numbers count only watches within the current filters." }),
        el("div", { class: "sum-chips" },
          chip(v.sessions.length, "Sessions"), chip(v.watches.length, "Watches"), chip(agg.running, "Running", agg.running ? "good" : "mute"),
          chip(crew.size, "Colleagues"), chip(agg.pending, "Pending", agg.pending ? "warn" : "mute"), chip(agg.queued, "Queued", agg.queued ? "warn" : "mute"),
          chip(agg.claimed, "Claimed", agg.claimed ? "warn" : "mute"),
          chip(agg.handled, "Handled", agg.handled ? "good" : "mute"), chip(agg.uncertain, "Delivery unknown", agg.uncertain ? "bad" : "mute"),
          chip(agg.errors, "Errors", agg.errors ? "bad" : "mute")));
      const kvl = el("dl", { class: "kv" });
      // claimed is absent from older backends: 0 there, "—" for other missing fields
      for (const [k, label] of SUMMARY_LABELS) kvl.append(el("dt", { text: label }), el("dd", { text: num(sum[k]) === null ? (k === "claimed" ? "0" : "—") : String(sum[k]) }));
      const det = el("details", { class: "hist" }, el("summary", { text: "History summary (all stored records)" }),
        el("p", { class: "note", text: "Counted from stored history; may include watches that ended long ago and does not indicate a current problem." }), kvl);
      host.append(det);
      const own = v.m.owner;
      const acText = { ready: "Ready", refreshing: "Syncing", degraded: "Degraded (initials shown when missing)", empty: "Empty (showing initials)" }[str(ac.status)] || "Unknown";
      host.append(el("dl", { class: "kv", style: null },
        el("dt", { text: "Owner" }), el("dd", { text: own ? own.name : "Not provided" }),
        el("dt", { text: "Avatar cache" }), el("dd", { text: app.snap && app.snap.avatar_cache ? `${acText}${num(ac.profiles) !== null ? ` · ${ac.profiles} profiles` : ""}${num(ac.last_sync) !== null ? " · synced at " + fmtClock(ac.last_sync) : ""}` : "Not provided by the API" })));
      host.lastChild.style.marginTop = "12px";
      host.append(el("p", { class: "lg-note", text: "Legend: flowing light in the scene only shows the relationships between sessions and threads and observed activity, not live messages; a coral pulse appears only when a new event actually shows up in the snapshot. Avatar size and glow are based only on observed interaction counts." }));
    });
  }

  function renderDrawer(v0) {
    const v = v0 || visible();
    $("watch-count").textContent = String(v.focusedWatches.length);
    $("event-count").textContent = String(v.events.length);
    const fresh = [...app.fresh.values()].filter((t) => t > Date.now()).length;
    const badge = $("drawer-badge");
    badge.hidden = fresh === 0;
    badge.textContent = String(fresh);
    if (!app.drawerOpen) return;
    if (app.tab === "watches") renderWatchList(v);
    else if (app.tab === "events") renderTimeline(v);
    else renderSummary(v);
  }

  /* ---------- thread inspector ---------- */

  function kv(...pairs) {
    const dl = el("dl", { class: "kv" });
    for (const [k, v, mono] of pairs) dl.append(el("dt", { text: k }), el("dd", { class: mono === true ? "mono" : mono || null }, ...(Array.isArray(v) ? v : [v])));
    return dl;
  }
  function personLine(p) {
    if (p.observed) return `${p.count} messages · last ${p.last !== null ? fmtRel(p.last) : "—"}`;
    return p.owner ? "0 messages · no messages from the owner observed yet" : "No observed interactions yet";
  }
  function crewRow(p, onHighlight) {
    const row = el("div", { class: "crew-row" + (p.owner ? " me" : "") },
      avatarEl(p, { lg: true, tip: false }),
      el("div", {},
        el("div", { class: "crew-name" }, p.name, p.owner ? el("span", { class: "crew-tag", text: "OWNER" }) : null),
        el("div", { class: "crew-sub", text: personLine(p) })));
    if (onHighlight) {
      const b = el("button", { type: "button", class: "btn sm", text: "Highlight", "aria-label": `Highlight the links of ${p.name} in the scene` });
      b.addEventListener("click", () => onHighlight(p));
      row.append(b);
    }
    return row;
  }
  const SOURCE_LABEL = { slack_metadata: "Slack channel metadata", local_events: "Locally observed events", peer_binding: "Peers bound to the watch" };

  function closeOtherSheets(keep) {
    for (const id of ["detail", "session", "people"]) {
      const d = $(id);
      if (id !== keep && d.open) { d.setAttribute("data-silent", "1"); d.close(); }
    }
  }
  // a dialog closed by another dialog opening must not pull focus back to the scene
  function takeSilent(dlg) {
    const silent = dlg.hasAttribute("data-silent");
    dlg.removeAttribute("data-silent");
    return silent;
  }
  function lifeCard(w, fr) {
    const id = str(w.id);
    const L = lifeOf(w);
    const fill = lifeFill(id);
    const bits = [];
    if (L.win !== null) bits.push(`Window length ${fmtDur(L.win)}`);
    if (num(w.window_started_at) !== null) bits.push(`Window start ${fmtClock(w.window_started_at, true)}`);
    if (num(w.deadline) !== null && !L.until) bits.push(`${w.lifecycle === "idle" ? "Closes if quiet at" : "Deadline"} ${fmtClock(w.deadline, true)}`);
    const card = el("div", { class: "ins-life", data: { state: stateKey(str(w.state)) } },
      fill,
      el("div", { class: "il-top" }, freshChip(fr), el("span", { class: "pill", text: fr.closed ? "Ended · no longer polling" : "Color = polling interval", data: { tone: "mute" } })),
      el("div", { class: "il-main", text: lifeText(w), data: { life: id } }),
      bits.length ? el("div", { class: "il-sub", text: bits.join(" · ") }) : null,
      el("div", { class: "il-note", text: "Background fill = elapsed share of the current window; new messages renew the window. Color only shows polling cadence, not errors." }));
    applyFresh(card, fr);
    setFill(fill, w);
    return card;
  }

  function openDetail(id, returnKeys) {
    closeOtherSheets("detail");
    app.selectedWatch = id;
    app.detailReturn = returnKeys || [];
    app.sigs.detail = null;
    renderDetail();
    const dlg = $("detail");
    if (!dlg.open) dlg.showModal();
    $("detail-close").focus();
    app.sigs.watches = null;
    applyFocus();
    nudgeFromSheet(id);
    if (model().watchById.has(id) && pv.wid !== id) pvOpen(id);
  }
  function renderDetail() {
    if (!app.selectedWatch) return;
    const m = model();
    const w = m.watchById.get(app.selectedWatch);
    const top = $("detail-top"), rest = $("detail-rest");
    const vm = w ? wvm(w, m) : null;
    $("preview").hidden = !w;
    if (w) pvSignal(pvSig(w, m));
    once("detail", JSON.stringify([w ? stableW(w) : null, vm ? vm.people : null, m.eventsByWatch.get(app.selectedWatch) || [], [...app.pendingStops]]), () => {
      top.replaceChildren();
      rest.replaceChildren();
      const stopBtn = $("detail-stop"), slackA = $("detail-slack");
      if (!w) {
        top.append(el("div", { class: "warn-box", text: "This watch is no longer in the latest snapshot." }));
        stopBtn.hidden = true; slackA.hidden = true;
        return;
      }
      $("detail-title").textContent = vm.name;
      const ec = obj(w.event_counts);
      const info = provInfo(w.provider);
      const fr = freshOf(w);
      const ctx = { type: "watch", id: vm.id };
      const crew = el("div", { class: "ins-block" },
        el("h3", { text: `CREW · ${vm.total} participants` }),
        el("div", { class: "crew" }, ...vm.people.slice(0, 5).map((p) => crewRow(p, (x) => { setPerson(x.id); closeDetail(); }))));
      if (vm.people.length > 5) {
        const all = el("button", { type: "button", class: "btn sm", text: `View all ${vm.people.length}`, "data-fk": "dall:" + vm.id });
        all.addEventListener("click", () => openPeople(ctx, ["dall:" + vm.id]));
        crew.append(all);
      }
      if (!vm.people.length) crew.append(el("p", { class: "note", text: "No participant data yet." }));
      if (vm.partial && vm.people.length) crew.append(el("p", { class: "note", text: "The list may be incomplete: it only includes observed or bound members." }));
      top.append(
        el("div", { class: "ins-hero" },
          el("span", { class: "type-chip", text: TYPE_LABEL[vm.type][0], data: { type: vm.type } }),
          pill(watchStateLabel(w.state), watchTone(w.state), w.state === "running"),
          pill(info.label + (info.note ? ` · ${info.note}` : ""), "mute")),
        lifeCard(w, fr),
        el("div", { class: "counters" },
          ...[["pending", "Pending"], ["queued", "Queued"], ["claimed", "Claimed"], ["handled", "Handled"], ["sending", "Sending"], ["delivery_unknown", "Delivery unknown"]]
            .map(([k, l]) => el("div", { class: "counter" }, el("b", { text: cnt(ec[k]) }), el("span", { text: l })))));
      rest.append(
        crew,
        kv(
          ["Watch ID", str(w.id), true],
          ["Type", `${TYPE_LABEL[vm.type][1]}${vm.sub.includes("·") ? " · " + vm.sub.split(" · ")[1] : ""}`],
          ["Channel", str(w.channel_name) ? `${str(w.channel_name)} (${str(w.channel) || "—"})` : str(w.channel) || "—", true],
          ["Thread", str(w.thread) || "—", true],
          ["Session", m.sessLabel(w.session_key)],
          ["Session ID", str(w.session_id || w.session_key), true],
          ["Scope / mode", `${str(w.scope) || "—"} / ${str(w.mode) || "—"}`],
          ["Lifecycle", w.lifecycle === "idle" ? "Auto-closes after 48 hours without new messages" : w.lifecycle === "until_stop" ? "Runs until stopped manually" : w.lifecycle === "fixed" ? "Fixed deadline" : str(w.lifecycle) || "—"],
          [w.lifecycle === "idle" ? "Closes if quiet at" : "Deadline", num(w.deadline) !== null ? fmtClock(w.deadline, true) : "None"],
          ["Started", num(w.started) !== null ? [fmtClock(w.started, true), " · ", relNode(w.started)] : "—"],
          ["Last poll", num(w.last_poll) !== null ? [fmtClock(w.last_poll, true), " · ", relNode(w.last_poll)] : "Not polled yet"],
          ["Next poll", num(w.next_poll) !== null ? [fmtClock(w.next_poll, true), " · ", relNode(w.next_poll)] : "—"],
          ["Current polling interval", num(w.poll_interval_seconds) !== null ? `${w.poll_interval_seconds / 60} min` : "—"],
          ["Polling cadence", fr.closed ? `${fr.label} (no longer polling)` : `${fr.label} (color = polling interval, not message age or errors)`],
          ["Backoff schedule", "1, 2, 2, 5, 5, 5, 10, 10, 10, 15 min, then every 15 min; new posts reset it"],
          ["Poll count", String(cnt(w.polls))],
          ["Observed interactions", `${vm.interaction}${vm.source ? " · source: " + (SOURCE_LABEL[vm.source] || vm.source) : ""}`],
          ["Root messages", String(cnt(w.root_count))],
          ["Stop reason", w.stop_reason === "inactivity_48h" ? "No new messages for 48 hours" : w.stop_reason === "explicit_stop" ? "Stopped manually" : str(w.stop_reason) || "—"],
          ["Expiry check", w.expiry_pending_check ? "Read failed or pagination incomplete; waiting to retry" : "—"]),
        ...(w.last_error ? [el("div", { class: "err-box", text: "Last error: " + str(w.last_error) })] : []));
      const evs = (m.eventsByWatch.get(app.selectedWatch) || []).slice(0, 8);
      rest.append(el("div", { class: "ins-block" }, el("h3", { text: `Recent activity · ${evs.length} (metadata)` }),
        ...evs.map((e) => {
          const [l, t] = eventStatus(e.status);
          const who = eventPerson(e, m);
          return el("div", { class: "tl-who" }, avatarEl(who, { tip: false }), pill(l, t, false, eventHint(e) || null), el("span", { text: who.name }), el("span", { class: "tl-time" }, relNode(num(e.received) ?? num(e.ts))));
        })));
      const link = safeUrl(w.slack_url);
      slackA.hidden = !link;
      if (link) slackA.href = link;
      stopBtn.hidden = !stoppable(w);
      stopBtn.disabled = app.pendingStops.has(str(w.id));
      stopBtn.textContent = stopBtn.disabled ? "Stopping…" : "Stop this watch…";
    });
  }
  function closeDetail() {
    const dlg = $("detail");
    if (dlg.open) dlg.close();
  }

  /* ---------- clipboard ---------- */

  // fallback for browsers/contexts without the async clipboard API; the helper node lives inside the open
  // dialog because the rest of the page is inert while a modal dialog is showing
  function legacyCopy(text, host) {
    const prev = document.activeElement;
    const ta = el("textarea", { class: "cp-ghost", readonly: true, tabindex: "-1", "aria-hidden": "true" });
    ta.value = text;
    host.append(ta);
    let ok = false;
    try {
      ta.focus({ preventScroll: true });
      ta.select();
      ta.setSelectionRange(0, text.length);
      ok = document.execCommand("copy");
    } catch (_) { ok = false; }
    ta.remove();
    if (prev && prev.focus) { try { prev.focus({ preventScroll: true }); } catch (_) { /* element gone */ } }
    return ok;
  }
  async function copyText(text, host) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) { /* permission denied or insecure context: try the legacy path */ }
    return legacyCopy(text, host);
  }
  function copyStatus(dlg, msg, tone) {
    const s = dlg.querySelector(".copy-status");
    if (!s) return;
    s.setAttribute("data-tone", tone || "good");
    s.textContent = "";
    if (msg) setTimeout(() => { s.textContent = msg; }, 40); // re-announce identical messages
  }
  function hideCopyFallback(dlg) {
    const box = dlg.querySelector(".copy-fallback");
    if (!box) return;
    box.hidden = true;
    box.querySelector("textarea").value = "";
  }
  // last resort: show the text selected so the user can press Ctrl/⌘+C
  function showCopyFallback(dlg, what, text) {
    const box = dlg.querySelector(".copy-fallback");
    if (!box) return;
    box.hidden = false;
    box.querySelector("span").textContent = `Copy manually (${what}):`;
    const ta = box.querySelector("textarea");
    ta.value = text;
    ta.focus();
    ta.select();
  }
  function copyBtn(what, getText, fk) {
    const b = el("button", { type: "button", class: "btn sm cp", text: "Copy", "data-fk": fk, "aria-label": `Copy ${what}` });
    b.addEventListener("click", async () => {
      const text = getText();
      const dlg = b.closest("dialog") || document.body;
      const ok = await copyText(text, dlg);
      b.setAttribute("data-copy", ok ? "ok" : "fail");
      setText(b, ok ? "Copied ✓" : "Copy failed");
      if (ok) { hideCopyFallback(dlg); copyStatus(dlg, `Copied: ${what}`); }
      else { copyStatus(dlg, `Copy failed (${what}); the text is selected, press Ctrl/⌘+C to copy it manually.`, "bad"); showCopyFallback(dlg, what, text); }
      setTimeout(() => { if (b.isConnected) { b.removeAttribute("data-copy"); setText(b, "Copy"); } }, 1800);
    });
    return b;
  }

  /* ---------- session inspector ---------- */

  function sessionNumbers(s, wvs) {
    const agg = { pending: 0, queued: 0, claimed: 0, handled: 0, uncertain: 0 };
    for (const w of wvs) {
      const ec = obj(w.event_counts);
      agg.pending += cnt(ec.pending); agg.queued += cnt(ec.queued); agg.claimed += cnt(ec.claimed); agg.handled += cnt(ec.handled);
      agg.uncertain += cnt(ec.sending) + cnt(ec.delivery_unknown);
    }
    const pick = (k) => (num(s[k]) !== null ? cnt(s[k]) : agg[k]);
    return { pending: pick("pending"), queued: pick("queued"), claimed: pick("claimed"), handled: pick("handled"), uncertain: pick("uncertain") };
  }
  function sessionChannels(s, wvs) {
    const names = new Map();
    for (const w of wvs) { const id = str(w.channel); if (id) names.set(id, str(w.channel_name)); }
    const out = new Map();
    const add = (id, name) => {
      const k = str(id) || str(name);
      if (!k) return;
      const nm = str(name) || names.get(k) || "";
      if (!out.has(k) || (nm && !out.get(k))) out.set(k, nm);
    };
    for (const c of arr(s.channels)) {
      if (c && typeof c === "object") add(c.id || c.channel, c.name || c.channel_name);
      else add(c, "");
    }
    for (const [id, nm] of names) add(id, nm);
    return [...out].map(([id, nm]) => (nm && nm !== id ? `${nm} (${id})` : id));
  }
  function countStates(wvs) {
    const by = (st) => wvs.filter((w) => stateKey(str(w.state)) === st).length;
    const running = by("running"), stopped = by("stopped"), expired = by("expired");
    return { running, stopped, expired, other: wvs.length - running - stopped - expired };
  }
  function sessionSummary(s, wvs, m) {
    const info = provInfo(s.provider);
    const nums = sessionNumbers(s, wvs);
    const c = countStates(wvs);
    const lines = [
      "Kage session summary",
      `Title: ${str(s.label || s.id || s.key)}`,
      `Provider: ${info.label}${s.provider ? ` (${str(s.provider)})` : ""}`,
      `Session ID: ${str(s.id) || "—"}`,
      ...(str(s.key) && str(s.key) !== str(s.id) ? [`Session key: ${str(s.key)}`] : []),
      `Workspace: ${str(s.cwd) || "—"}`,
      `Watches: ${wvs.length} in snapshot · running ${c.running} · stopped ${c.stopped} · expired ${c.expired}${c.other ? ` · other ${c.other}` : ""}`,
      `Channels: ${sessionChannels(s, wvs).join(", ") || "—"}`,
      `Events: pending ${nums.pending} · queued ${nums.queued} · claimed ${nums.claimed} · handled ${nums.handled} · delivery unknown ${nums.uncertain}`,
      `Last activity: ${num(s.last_activity) !== null ? `${fmtFull(s.last_activity)} (${fmtRel(s.last_activity)})` : "—"}`,
      "Related watches:",
    ];
    for (const w of wvs) {
      const vm = wvm(w, m), fr = freshOf(w);
      lines.push(`- [${watchStateLabel(w.state)}] ${TYPE_LABEL[vm.type][1]} ${vm.title} · watch ID ${str(w.id)}${fr.closed ? "" : ` · ${fr.label}`} · ${lifeText(w)}`);
    }
    if (!wvs.length) lines.push("- (no watches for this session in the snapshot)");
    return lines.join("\n");
  }

  function sessWatchRow(vm) {
    const w = vm.w, id = vm.id, fr = freshOf(w), state = stateKey(str(w.state));
    const fill = lifeFill(id);
    const row = el("button", {
      type: "button", class: "sw-row", "data-fk": "sw:" + id, data: { state },
      "aria-label": `View thread details: ${vm.title}, ${watchStateLabel(w.state)}, ${fr.closed ? "Ended" : fr.label}`,
    },
    fill,
    el("span", { class: "sw-top" },
      pill(watchStateLabel(w.state), watchTone(w.state)),
      el("span", { class: "type-chip", text: TYPE_LABEL[vm.type][0], data: { type: vm.type } }),
      el("span", { class: "sw-title", text: vm.title })),
    el("span", { class: "sw-sub" }, freshChip(fr), el("span", { text: lifeText(w), data: { life: id } })),
    el("span", { class: "sw-sub" }, `${vm.sub} · last polled `, w.last_poll ? relNode(w.last_poll) : "not yet"));
    applyFresh(row, fr);
    setFill(fill, w);
    row.addEventListener("click", () => openDetail(id, app.sessionReturn));
    return row;
  }

  const DELIVERY_PATH = {
    sqlite_outbox_native_hook_or_waiter: "SQLite outbox + native hook / background waiter",
    codex_queue: "Codex queue",
  };
  const CONSUMER_STATUS = {
    claimed_unresolved: "Some events were claimed but are not yet confirmed as handled",
    awaiting_consumer: "Events are queued, waiting for a consumer to claim them",
    no_pending_events: "No queued or claimed-but-unfinished events",
  };
  // Transport detail only: queued / wake / claimed are delivery steps, never proof of handling or of a live CLI connection.
  // Session fields win; the provider capability fills in whatever the session DTO does not carry.
  function deliveryBlock(s, info, nums) {
    const d = obj(s.delivery), cap = info.cap;
    const hasD = Object.keys(d).length > 0;
    const path = str(d.path || s.delivery_path || cap.path);
    if (!path && !hasD) return null;
    const consumer = flag(d.consumer_required) ?? cap.consumer;
    const resume = flag(d.auto_resume) ?? cap.autoResume;
    const sup = cap.supported === true ? " · backend declares support" : cap.supported === false ? " · backend declares no support yet" : "";
    const rows = [["Path", `${DELIVERY_PATH[path] || path || "Not provided"}${sup}`]];
    if (consumer !== null) rows.push(["Consumer", consumer ? "A session-side consumer (native hook or background waiter) must claim events" : "No session-side consumer needed"]);
    if (hasD) {
      const q = num(d.queued) !== null ? cnt(d.queued) : nums.queued;
      const c = num(d.claimed) !== null ? cnt(d.claimed) : nums.claimed;
      rows.push(["Waiting / claimed", `${q} queued awaiting claim · ${c} claimed awaiting completion`]);
      const cs = str(d.consumer_status);
      if (cs) rows.push(["Consumer status", CONSUMER_STATUS[cs] || cs]);
      const raw = d.last_signal, sig = num(raw);
      const when = sig !== null ? [fmtClock(sig, true), " · ", relNode(sig)]
        : raw === null || raw === undefined || raw === "" ? "No record yet"
        : short(typeof raw === "object" ? JSON.stringify(raw) : str(raw), 80);
      rows.push(["Last wake-up", [].concat(when, d.signal_pending === true ? [" · wake-up signal pending"] : [])]);
    }
    if (resume !== null) rows.push(["Auto-resume", resume ? "Enabled" : "No · sessions are not resumed or started automatically"]);
    const notes = [];
    if (consumer === true || hasD) notes.push("“Queued” or “wake-up sent” only means the event entered the delivery path, not that it was handled; the session must claim it and finish handling it. ");
    if (resume === false) notes.push("Sessions are not resumed automatically. ");
    notes.push("This describes the delivery path, not whether the session is online or offline.");
    return el("div", { class: "ins-block deliv" },
      el("h3", { text: "Delivery path · DELIVERY" }),
      kv(...rows),
      ...(str(d.last_error) ? [el("div", { class: "err-box", text: "Last delivery error: " + short(str(d.last_error), 300) })] : []),
      el("p", { class: "note", text: notes.join("") }));
  }

  function openSession(key, returnKeys) {
    closeOtherSheets("session");
    app.selectedSession = key;
    app.sessionReturn = returnKeys || [];
    app.sigs.session = null;
    const dlg = $("session");
    hideCopyFallback(dlg);
    copyStatus(dlg, "");
    renderSession();
    if (!dlg.open) dlg.showModal();
    $("session-close").focus();
  }
  function renderSession() {
    const key = app.selectedSession;
    if (!key) return;
    const m = model();
    const s = m.sessByKey.get(key) || null;
    const wvs = s ? m.watches.filter((w) => str(w.session_key) === key).sort((a, b) => ord("w:" + str(a.id)) - ord("w:" + str(b.id))) : [];
    const evs = m.events.filter((e) => str(e.session_key) === key)
      .sort((a, b) => (num(b.received) || num(b.ts) || 0) - (num(a.received) || num(a.ts) || 0)).slice(0, 8);
    once("session", JSON.stringify([key, s, wvs.map(stableW), evs, !!app.snap, s ? provInfo(s.provider).cap : null]), () => withFocusRestore(() => {
      const body = $("session-body");
      body.replaceChildren();
      if (!s) {
        $("session-title").textContent = "Session details";
        body.append(el("div", { class: "warn-box", text: "This session is no longer in the latest snapshot." }));
        return;
      }
      theme(body, s.provider);
      const info = provInfo(s.provider);
      const label = str(s.label || s.id || key);
      $("session-title").textContent = label;
      const vms = wvs.map((w) => wvm(w, m));
      const sv_ = sessionVm(s, vms);
      let people = sv_.people, visibleP = sv_.visible, overflow = sv_.overflow;
      if (!people.length && arr(s.participants).length) {
        people = sortPeople(arr(s.participants).map((p) => (p && typeof p === "object" ? person(p) : person({}, str(p)))));
        visibleP = pickVisible(people);
        overflow = Math.max(0, people.length - visibleP.length);
      }
      const nums = sessionNumbers(s, wvs);
      const chans = sessionChannels(s, wvs);
      const c = countStates(wvs);
      const sid = str(s.id), cwd = str(s.cwd);
      const copyRow = (what, value, fk, mono) => [what, [el("span", { text: value }), copyBtn(what, () => value, fk)], "copyable" + (mono ? " mono" : "")];

      const peopleBtn = el("button", { type: "button", class: "people-btn", "data-fk": "sp:" + key });
      fillPeople(peopleBtn, { visible: visibleP, overflow, total: people.length }, `Participants of ${label}`);
      peopleBtn.addEventListener("click", () => openPeople({ type: "session", key, all: true }, ["sp:" + key]));

      const countsText = `${wvs.length} in snapshot · running ${c.running} · stopped ${c.stopped} · expired ${c.expired}${c.other ? ` · other ${c.other}` : ""}` +
        (num(s.watch_count) !== null && cnt(s.watch_count) !== wvs.length ? ` (session total: ${cnt(s.watch_count)})` : "");
      body.append(
        el("div", { class: "sess-head" },
          el("span", { class: "sess-avatar" }, characterNode(s.provider)),
          el("div", { class: "sess-chips" },
            pill(info.label + (info.note ? ` · ${info.note}` : ""), "mute"),
            pill(`Running watches ${c.running}`, c.running ? "good" : "mute"),
            pill(`Watches ${wvs.length}`, "mute"))),
        el("div", { class: "counters" },
          ...[["Pending", nums.pending], ["Queued", nums.queued], ["Claimed", nums.claimed], ["Handled", nums.handled], ["Delivery unknown", nums.uncertain]]
            .map(([l, n]) => el("div", { class: "counter" }, el("b", { text: n }), el("span", { text: l })))),
        kv(
          ["Provider", `${info.label}${str(s.provider) && str(s.provider) !== info.label ? ` (${str(s.provider)})` : ""}`],
          copyRow("Full title", label, "cp:title"),
          sid ? copyRow("Session ID", sid, "cp:sid", true) : ["Session ID", "—", true],
          ...(str(s.key) && str(s.key) !== sid ? [["Session key", str(s.key), true]] : []),
          cwd ? copyRow("Workspace path", cwd, "cp:cwd", true) : ["Workspace path", "(no workspace path)"],
          ["Watch count", countsText],
          ["Channels", chans.length ? el("div", { class: "chan-list" }, ...chans.slice(0, 30).map((t) => el("span", { class: "chan-chip", text: t })), chans.length > 30 ? el("span", { class: "chan-chip", text: `+${chans.length - 30}` }) : null) : "—"],
          ["Last activity", num(s.last_activity) !== null ? [fmtClock(s.last_activity, true), " · ", relNode(s.last_activity)] : "—"],
          ["Participants", el("div", { class: "sess-people" }, peopleBtn, el("span", { class: "note", text: `${people.length} people${sv_.partial ? " · list may be incomplete" : ""}` })), null]));

      const deliv = deliveryBlock(s, info, nums);
      if (deliv) body.append(deliv);

      const list = el("div", { class: "sw-list" }, ...vms.map(sessWatchRow));
      body.append(el("div", { class: "ins-block" },
        el("h3", { text: `Related watches · ${vms.length} (current state)` }),
        vms.length ? list : el("p", { class: "note", text: "No watches for this session in the snapshot." }),
        vms.length ? el("p", { class: "note", text: "Click a watch for thread details; color = polling interval, background fill = elapsed share of the current window." }) : null));

      body.append(el("div", { class: "ins-block" }, el("h3", { text: `Recent activity · ${evs.length} (metadata)` }),
        ...(evs.length ? evs.map((e) => {
          const [l, t] = eventStatus(e.status);
          const who = eventPerson(e, m);
          const w = m.watchById.get(str(e.watch_id));
          const where = str(e.channel_name) || (w ? wvm(w, m).name : str(e.channel));
          return el("div", { class: "tl-who" }, avatarEl(who, { tip: false }), pill(l, t, false, eventHint(e) || null), el("span",{ text: where ? `${who.name} · ${where}` : who.name }), el("span", { class: "tl-time" }, relNode(num(e.received) ?? num(e.ts))));
        }) : [el("p", { class: "note", text: "No activity recorded yet." })])));
      body.append(el("p", { class: "note", text: "All of the above comes from local snapshot metadata, without message bodies; “Running watches” is a watch state and does not mean the AI session is online." }));
    }));
    syncSessionFoot();
  }
  function syncSessionFoot() {
    const key = app.selectedSession;
    const s = key ? model().sessByKey.get(key) : null;
    const f = $("session-focus");
    f.disabled = !s;
    setText(f, s && app.focusKey === key ? "Back to overview (unfocus)" : "Focus session");
    $("session-copy-all").disabled = !s;
    syncSessionStopZone(key, s);
  }
  // sidebar button for stopping every running watch of this session; the count always comes from the unfiltered snapshot
  function syncSessionStopZone(key, s) {
    const btn = $("session-stop-all"), note = $("session-stop-note");
    const busy = stopAllBusy(), holding = !!key && stopAllHold(key);
    const ready = !!(s && str(s.provider) && str(s.id));
    const run = key ? sessionRunning(key) : { n: 0, truncated: false };
    const off = !s || !ready || busy || holding || run.n === 0;
    if (btn.disabled !== off) btn.disabled = off;
    setText(btn, busy ? "Stopping…" : holding ? "Refreshing…" : "Stop all follow-ups for this session");
    let t;
    if (!s) t = "This session is no longer in the latest snapshot.";
    else if (!ready) t = "Missing provider or session ID; cannot stop by session.";
    else if (busy) t = "Submitting the stop request…";
    else if (holding) t = "Submitted; refreshing state.";
    else if (run.n === 0) t = "No running watches in the current snapshot." + (run.truncated ? " The snapshot was truncated at the limit; watches not shown cannot be judged here." : "");
    else t = `${run.truncated ? `At least ${run.n}` : `${run.n}`} running watch(es) in the snapshot (including those hidden by filters) will be stopped; the AI session will not end.` + (run.truncated ? " The snapshot is truncated; there may be more." : "");
    setText(note, t);
  }
  function focusFromSession() {
    const key = app.selectedSession;
    if (!key) return;
    if (app.focusKey === key) setFocus(null);
    else if (ensureOnStage(key)) setFocus(key);
    else { toast("This session is not visible under the current filters; adjust the filters before focusing.", "bad"); return; }
    $("session").close();
  }
  async function copySessionSummary(btn) {
    const key = app.selectedSession;
    const m = model();
    const s = key ? m.sessByKey.get(key) : null;
    if (!s) return;
    const text = sessionSummary(s, m.watches.filter((w) => str(w.session_key) === key), m);
    const dlg = $("session");
    const ok = await copyText(text, dlg);
    if (ok) { hideCopyFallback(dlg); copyStatus(dlg, "Copied the full summary"); }
    else { copyStatus(dlg, "Copy failed; the summary text is selected, press Ctrl/⌘+C to copy it manually.", "bad"); showCopyFallback(dlg, "Full summary", text); }
    btn.setAttribute("data-copy", ok ? "ok" : "fail");
    setTimeout(() => btn.removeAttribute("data-copy"), 1800);
  }

  /* ---------- on-demand Slack message preview (thread inspector only) ---------- */

  const PV_ERRORS = {
    missing_scope: "Missing Slack read permission (missing_scope); cannot read message content.",
    preview_unavailable: "Message preview is unavailable right now.",
    watch_not_found: "Watch not found.",
    rate_limited: "Too many requests; will retry automatically shortly.",
    csrf_missing: "Waiting for the security token from the state snapshot; will retry shortly.",
    timeout: "Request timed out.",
    network: "Network error; cannot reach the local service.",
    bad_response: "The service returned an unrecognized response.",
  };
  const PV_SCOPE = { thread: "Thread messages", channel: "Recent channel messages", dm: "Recent DM messages" };
  // one preview at a time: wid is the only watch whose messages may be rendered; seq invalidates in-flight work
  const pv = {
    wid: null, seq: 0, ctl: null, inflight: false, data: null, msgs: [], error: null, fails: 0,
    nextAt: 0, waitUntil: 0, cacheUntil: 0, dirtyAt: 0, sig: null, listSig: "", alertSig: "", firstRender: true,
  };

  function pvSig(w, m) {
    return JSON.stringify([w.last_activity, w.event_counts, (m.eventsByWatch.get(str(w.id)) || []).slice(0, 5).map((e) => [e.id, e.status])]);
  }
  // a changed event signature schedules one debounced refresh; repeated changes coalesce into it
  function pvSignal(sig) {
    if (!pv.wid) return;
    if (pv.sig === null) { pv.sig = sig; return; }
    if (sig !== pv.sig) {
      pv.sig = sig;
      if (!pv.dirtyAt) pv.dirtyAt = Date.now() + PV_DEBOUNCE_MS;
    }
  }
  function pvSay(text) {
    const n = $("pv-live");
    n.textContent = "";
    setTimeout(() => { n.textContent = text; }, 40);
  }
  function pvClearUi() {
    $("pv-list").replaceChildren();
    $("pv-alert").replaceChildren();
    $("pv-alert").hidden = true;
    $("pv-when").textContent = "";
    $("pv-note").textContent = "";
    $("pv-live").textContent = "";
    $("preview").setAttribute("data-stale", "0");
    $("preview").setAttribute("aria-busy", "false");
  }
  function pvReset() {
    pv.seq += 1;
    if (pv.ctl) { try { pv.ctl.abort(); } catch (_) { /* already settled */ } }
    Object.assign(pv, {
      wid: null, ctl: null, inflight: false, data: null, msgs: [], error: null, fails: 0,
      nextAt: 0, waitUntil: 0, cacheUntil: 0, dirtyAt: 0, sig: null, listSig: "", alertSig: "", firstRender: true,
    });
  }
  // closing or switching drops the in-flight request and the message bodies held in the browser
  function pvStop() {
    pvReset();
    pvClearUi();
  }
  function pvOpen(wid) {
    pvStop();
    pv.wid = wid;
    const m = model(), w = m.watchById.get(wid);
    pv.sig = w ? pvSig(w, m) : null;
    pvFetch("open");
  }

  function pvNormalize(list) {
    const out = arr(list).map(obj).map((raw) => {
      const a = obj(raw.author);
      const id = str(a.id);
      return {
        ts: str(raw.ts),
        tsNum: num(raw.ts),
        author: { id, name: str(a.name) || id || "Unknown", avatar: localUrl(a.avatar_url), owner: a.is_owner === true },
        text: str(raw.text),
        bot: raw.is_bot === true,
        files: arr(raw.files).map(obj).map((f) => ({ title: str(f.title), mime: str(f.mimetype) })).filter((f) => f.title || f.mime).slice(0, 6),
        url: slackMsgUrl(raw.slack_url),
      };
    });
    out.sort((a, b) => (a.tsNum ?? 0) - (b.tsNum ?? 0));
    return out.slice(-50);
  }
  function pvRetryAfter(res, body) {
    let s = null;
    const h = res.headers && res.headers.get("Retry-After");
    if (h) {
      if (/^\d+(\.\d+)?$/.test(h.trim())) s = Number(h);
      else { const t = Date.parse(h); if (Number.isFinite(t)) s = (t - Date.now()) / 1000; }
    }
    const b = num(body.retry_after);
    if (b !== null) s = Math.max(s === null ? 0 : s, b);
    if (s === null && res.status === 429) s = PV_MS / 1000;
    return s === null ? null : clamp(Math.ceil(s), 1, PV_RETRY_MAX_S);
  }
  function pvErrText(e) {
    const code = /^[a-z0-9_.:-]{1,64}$/i.test(str(e.code)) ? str(e.code) : "unknown";
    const t = e.status === 429 ? PV_ERRORS.rate_limited : PV_ERRORS[code] || `Read failed (${code}).`;
    return "Message preview failed: " + t;
  }

  // automatic preview traffic needs a visible tab and the thread inspector as the topmost dialog
  function pvCanRun() {
    return !!pv.wid && !document.hidden && app.selectedWatch === pv.wid && $("detail").open &&
      !$("people").open && !$("session").open && !$("confirm").open && !$("session-stop").open;
  }
  // a hidden tab drops the in-flight request (its result is discarded via seq) but keeps the messages already shown
  function pvPause() {
    if (!pv.wid || !pv.inflight) return;
    pv.seq += 1;
    if (pv.ctl) { try { pv.ctl.abort(); } catch (_) { /* already settled */ } }
    pv.ctl = null;
    pv.inflight = false;
    pv.nextAt = Date.now(); // refresh as soon as the tab is visible again; waitUntil still applies
    pvRender();
  }

  async function pvFetch(reason) {
    const wid = pv.wid;
    if (!wid || pv.inflight || Date.now() < pv.waitUntil) return;
    if (reason !== "open" && !pvCanRun()) return; // the initial open runs right after showModal
    const manual = reason === "manual";
    const token = str(app.snap && app.snap.csrf_token);
    const seq = ++pv.seq;
    const ctl = new AbortController();
    let timedOut = false;
    const to = setTimeout(() => { timedOut = true; ctl.abort(); }, PV_TIMEOUT_MS);
    pv.inflight = true;
    pv.ctl = ctl;
    pv.dirtyAt = 0;
    pvRender();
    let res = null, data = null, failure = null;
    try {
      if (!token) failure = { code: "csrf_missing" };
      else {
        // Origin is attached by the browser; the endpoint is read-only and never stops or reactivates a watch
        res = await fetch("/api/watches/" + encodeURIComponent(wid) + "/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
          body: "{}",
          credentials: "same-origin",
          signal: ctl.signal,
        });
        try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
      }
    } catch (_) {
      failure = { code: timedOut ? "timeout" : "network" };
    } finally {
      clearTimeout(to);
    }
    if (seq !== pv.seq || wid !== pv.wid) return; // closed or switched while waiting: never render into another watch
    pv.inflight = false;
    pv.ctl = null;
    const body = obj(data);
    if (!failure) {
      if (res.ok && !body.error && Array.isArray(body.messages) && (!body.watch_id || str(body.watch_id) === wid)) {
        const now = Date.now();
        const prev = pv.data ? pv.data.fetchedAt : null;
        const fetchedAt = num(body.fetched_at) ?? now / 1000;
        pv.data = { fetchedAt, partial: body.partial === true, scope: str(body.scope), note: short(str(body.note), 160) };
        pv.msgs = pvNormalize(body.messages);
        pv.error = null;
        pv.fails = 0;
        pv.waitUntil = 0;
        pv.nextAt = now + PV_MS;
        pv.cacheUntil = clamp((num(body.expires_at) ?? fetchedAt + PV_MS / 1000) * 1000, now, now + 60000);
        if (manual) pvSay(prev !== null && prev === fetchedAt ? `Server cache still valid; showing messages read at ${fmtClock(fetchedAt)}.` : "Recent messages refreshed.");
        else if (prev === null) pvSay(`Loaded ${pv.msgs.length} recent messages.`);
      } else failure = { code: str(body.error) || (res.ok ? "bad_response" : `http_${res.status}`), status: res.status, retry: pvRetryAfter(res, body) };
    }
    if (failure) {
      const now = Date.now();
      const first = !pv.error || pv.error.code !== failure.code;
      pv.fails += 1;
      pv.error = { code: failure.code, status: failure.status };
      if (failure.retry) pv.waitUntil = now + failure.retry * 1000;
      pv.nextAt = Math.max(now + Math.min(PV_MS * 2 ** (pv.fails - 1), 60000), pv.waitUntil);
      if (first || manual) pvSay(pvErrText(pv.error) + (pv.data ? " Messages from the last successful read are still shown and may be outdated." : ""));
    }
    pvRender();
  }

  function pvMsgNode(x) {
    const a = x.author;
    const who = { id: a.id, name: a.name, avatar: a.avatar, owner: a.owner, observed: true, count: 0, last: null, status: "" };
    const meta = el("div", { class: "pv-meta" }, el("b", { text: a.name }));
    if (a.owner) meta.append(el("span", { class: "pv-tag own", text: "Me" }));
    if (x.bot) meta.append(el("span", { class: "pv-tag", text: "BOT", title: "Bot / app message" }));
    if (x.tsNum !== null) meta.append(el("span", { class: "pv-time" }, fmtClock(x.tsNum, true) + " · ", relNode(x.tsNum)));
    if (x.url) meta.append(el("a", { class: "pv-link", href: x.url, target: "_blank", rel: "noopener noreferrer", text: "Slack ↗", "aria-label": "Open this message in Slack (new window)" }));
    const bubble = el("div", { class: "pv-bubble" }, meta);
    const text = x.text.length > 2000 ? x.text.slice(0, 2000) + "… (truncated)" : x.text;
    if (text) bubble.append(el("div", { class: "pv-text", text }));
    else if (!x.files.length) bubble.append(el("div", { class: "pv-text dim", text: "(no text content)" }));
    if (x.files.length) {
      bubble.append(el("ul", { class: "pv-files", "aria-label": "Attachments" },
        ...x.files.map((f) => el("li", {}, `📎 ${short(f.title || "Untitled file", 120)}`, f.mime ? el("small", { text: short(f.mime, 40) }) : null))));
    }
    return el("li", { class: "pv-msg " + (a.owner ? "own" : "peer") + (x.bot ? " bot" : "") }, avatarEl(who, { tip: false }), bubble);
  }
  function pvRenderList() {
    const list = $("pv-list");
    const stick = pv.firstRender || list.scrollHeight - list.scrollTop - list.clientHeight < 24;
    const top = list.scrollTop;
    list.replaceChildren();
    if (!pv.data) {
      if (pv.inflight) list.append(el("li", { class: "pv-empty" }, el("div", { class: "skeleton" }), "Reading recent Slack messages…"));
      else list.append(el("li", { class: "pv-empty", text: "No messages to show yet (the preview has not loaded successfully)." }));
      return;
    }
    if (!pv.msgs.length) { list.append(el("li", { class: "pv-empty", text: "No messages were read." })); return; }
    for (const x of pv.msgs) list.append(pvMsgNode(x));
    list.scrollTop = stick ? list.scrollHeight : top;
    pv.firstRender = false;
  }
  function pvRenderButton() {
    const b = $("pv-refresh");
    const wait = Math.ceil((pv.waitUntil - Date.now()) / 1000);
    const busy = pv.inflight, waiting = !busy && wait > 0;
    setText(b, busy ? "Reading…" : waiting ? `Please wait ${wait}s` : "Refresh");
    const off = busy || waiting;
    if (b.disabled !== off) b.disabled = off;
  }
  function pvRender() {
    if (!pv.wid) return;
    const d = pv.data;
    const sec = $("preview");
    sec.setAttribute("aria-busy", pv.inflight && !d ? "true" : "false");
    sec.setAttribute("data-stale", pv.error && d ? "1" : "0");
    $("pv-when").replaceChildren(...(d
      ? [pv.error ? "Last successful read at " : "Read at ", fmtClock(d.fetchedAt), " · ", relNode(d.fetchedAt)]
      : [pv.inflight ? "Reading…" : "Not read yet"]));
    $("pv-note").textContent = d
      ? [PV_SCOPE[d.scope], d.note || "Recent messages; bodies cached in memory only", "Read-only"].filter(Boolean).join(" · ")
      : "Slack is read only while this panel is open; message bodies are cached in memory only, never written to the database.";
    const alertText = [pv.error ? pvErrText(pv.error) + (d ? " Messages from the last successful read are still shown below and may be outdated." : "") : "", d && d.partial ? "Partial result: only recent messages within a limited range, not the full history." : ""];
    const asig = JSON.stringify(alertText);
    if (asig !== pv.alertSig) {
      pv.alertSig = asig;
      const al = $("pv-alert");
      al.replaceChildren(...(alertText[0] ? [el("div", { class: "err-box", text: alertText[0] })] : []), ...(alertText[1] ? [el("div", { class: "warn-box", text: alertText[1] })] : []));
      al.hidden = !alertText[0] && !alertText[1];
    }
    const lsig = JSON.stringify([pv.wid, !!d, pv.msgs.map((x) => [x.ts, x.text, x.author, x.bot, x.files, x.url]), !d && pv.inflight]);
    if (lsig !== pv.listSig) { pv.listSig = lsig; pvRenderList(); }
    pvRenderButton();
  }
  // once a second: label countdown, plus the 15s refresh / debounced event refresh (never overlapping, never hidden or covered)
  function pvTick() {
    if (!pv.wid) return;
    pvRenderButton();
    if (!pvCanRun() || pv.inflight || !model().watchById.has(pv.wid)) return;
    const now = Date.now();
    if (now < pv.waitUntil) return;
    if (pv.dirtyAt && now >= pv.dirtyAt && now >= pv.cacheUntil) pvFetch("event");
    else if (now >= pv.nextAt) pvFetch("poll");
  }

  /* ---------- participants dialog ---------- */

  function peopleFor(ctx) {
    const m = model();
    if (ctx.type === "watch") {
      const w = m.watchById.get(ctx.id);
      if (!w) return null;
      const vm = wvm(w, m);
      return { title: vm.title, kind: TYPE_LABEL[vm.type][1], people: vm.people, source: vm.source, partial: vm.partial };
    }
    const v = visible();
    const s = m.sessByKey.get(ctx.key);
    if (!s) return null;
    // the session inspector lists every watch in the snapshot, so its participants do too
    const pool = ctx.all ? m.watches : v.watches;
    const vms = pool.filter((w) => str(w.session_key) === ctx.key).map((w) => wvm(w, m));
    const sv_ = sessionVm(s, vms);
    return { title: m.sessLabel(ctx.key), kind: ctx.all ? "Session (all watches in the snapshot)" : "Session (watches within the current filters)", people: sv_.people, source: "", partial: sv_.partial };
  }
  function openPeople(ctx, returnKeys) {
    closeOtherSheets("people");
    app.peopleCtx = ctx;
    app.peopleReturn = returnKeys || [];
    app.sigs.people = null;
    renderPeople();
    const dlg = $("people");
    if (!dlg.open) dlg.showModal();
    $("people-close").focus();
  }
  function renderPeople() {
    const ctx = app.peopleCtx;
    if (!ctx) return;
    const info = peopleFor(ctx);
    once("people", JSON.stringify([ctx, info]), () => {
      const body = $("people-body");
      body.replaceChildren();
      if (!info) {
        $("people-title").textContent = "Participants";
        body.append(el("div", { class: "warn-box", text: "This item is no longer in the latest snapshot." }));
        return;
      }
      $("people-title").textContent = `${info.title} · ${info.people.length} people`;
      const list = el("div", { class: "crew" });
      for (const p of info.people) list.append(crewRow(p, (x) => { setPerson(x.id); $("people").close(); }));
      body.append(
        el("p", { class: "note", text: `${info.kind}. Message counts only include locally observed interactions; if the owner shows 0, none of their messages have been observed yet, which does not mean they did not take part.` }),
        info.people.length ? list : el("div", { class: "empty" }, el("b", { text: "No participant data yet" })));
      const bits = [];
      if (info.source) bits.push("Source: " + (SOURCE_LABEL[info.source] || info.source));
      if (info.partial) bits.push("List may be incomplete");
      if (bits.length) body.append(el("p", { class: "note", text: bits.join(" · ") }));
    });
  }

  /* ---------- stop flow ---------- */

  function openConfirm(id, returnKeys) {
    const m = model();
    const w = m.watchById.get(id);
    if (!w) return;
    const vm = wvm(w, m);
    app.confirmWatch = id;
    app.confirmReturn = returnKeys || [];
    const others = m.watches.filter((x) => str(x.session_key) === str(w.session_key) && str(x.id) !== id && x.state === "running").length;
    $("confirm-desc").replaceChildren(
      el("p", {}, "About to stop this watch:"),
      kv(["Watch ID", id, true], ["Session", m.sessLabel(w.session_key)], ["Channel", vm.name, true], ["Thread", str(w.thread) || "—", true]),
      el("div", { class: "warn-box" },
        `Only this watch is stopped; other watches of the same task${others ? ` (${others} more running)` : ""} keep running. Stopping is permanent: it does not resume automatically, and the UI offers no resume button.`),
      el("div", { class: "err-box", id: "confirm-error", hidden: true }));
    $("confirm-ok").disabled = app.pendingStops.has(id);
    $("confirm-ok").textContent = "Confirm stop";
    $("confirm").showModal();
    $("confirm-cancel").focus();
  }

  function toast(text, tone) {
    const t = el("div", { class: "toast", text, data: { tone: tone || "good" } });
    $("toasts").append(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 350); }, tone === "bad" ? 7000 : 4500);
    while ($("toasts").children.length > 4) $("toasts").firstChild.remove();
  }

  function syncPending() {
    app.sigs.watches = null; app.sigs.detail = null;
    const id = app.confirmWatch;
    $("confirm-ok").disabled = !!id && app.pendingStops.has(id);
    $("confirm-cancel").disabled = !!id && app.pendingStops.has(id);
    renderAll();
  }

  async function doStop() {
    const id = app.confirmWatch;
    if (!id || app.pendingStops.has(id)) return;
    app.pendingStops.add(id);
    $("confirm-ok").textContent = "Stopping…";
    syncPending();
    const errBox = () => $("confirm-error");
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), STOP_TIMEOUT_MS);
    let ambiguous = false;
    try {
      let res;
      try {
        res = await fetch("/api/watches/" + encodeURIComponent(id) + "/stop", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": str(app.snap && app.snap.csrf_token) },
          body: "{}",
          credentials: "same-origin",
          signal: ctl.signal,
        });
      } catch (e) {
        // no response received: the server may or may not have applied the stop
        ambiguous = true;
        throw e && e.name === "AbortError" ? new Error("Request timed out") : e;
      }
      let data = null;
      try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
      if (!res.ok || !data || data.ok !== true) throw new Error((data && data.error) ? str(data.error) : `HTTP ${res.status}`);
      app.pendingStops.delete(id);
      app.confirmWatch = null;
      toast(`Watch ${short(id, 16)} stopped (state: ${str(data.state) || "stopped"}). Other watches are not affected.`);
      const ret = app.confirmReturn;
      $("confirm").close();
      closeDetail();
      focusByKey([...ret, "w:" + id]);
      syncPending();
      refreshNow();
    } catch (err) {
      app.pendingStops.delete(id);
      const msg = err && err.message ? err.message : "Unknown error";
      const full = ambiguous
        ? `${msg}; the stop may have taken effect. Refreshing the latest state; check the watch state before deciding whether to retry.`
        : "Stop failed: " + msg;
      toast(ambiguous ? "Stop result uncertain: " + full : `Failed to stop watch: ${msg}`, "bad");
      if (errBox()) { errBox().hidden = false; errBox().textContent = full; }
      $("confirm-ok").textContent = ambiguous ? "Retry after checking state" : "Retry stop";
      app.stopUnsure = ambiguous ? id : null;
      syncPending();
      if (ambiguous) refreshNow();
    } finally {
      clearTimeout(to);
    }
  }

  /* ---------- stop whole session flow ---------- */

  // Dedicated to the session sidebar: separate dialog and handler from the single-watch stop above.
  // The server stops every running watch with this exact provider + session id (scanning all stored watches),
  // so the snapshot-derived numbers here are only a preview and are never presented as the final result.
  function stopAllBusy() { return !!(app.sessionStop && app.sessionStop.busy); }
  function stopAllHold(key) {
    const h = app.stopAllHold;
    return !!h && h.key === key && app.ingestN < h.target && Date.now() < h.until;
  }
  // running watches of one session over the UNFILTERED snapshot: search, provider and scope filters never apply
  function sessionRunning(key) {
    const list = model().watches.filter((w) => str(w.session_key) === key && stateKey(str(w.state)) === "running");
    const by = { channel_thread: 0, dm: 0, channel: 0 };
    for (const w of list) by[inferType(w)] += 1;
    return { n: list.length, by, truncated: obj(app.snap && app.snap.limits).watches_truncated === true };
  }

  function openSessionStop() {
    const key = app.selectedSession;
    const s = key ? model().sessByKey.get(key) : null;
    if (!s || stopAllBusy() || $("session-stop").open) return;
    const provider = str(s.provider), sid = str(s.id);
    if (!provider || !sid || stopAllHold(key) || sessionRunning(key).n === 0) return;
    const label = str(s.label || s.id || key), pl = provLabel(s.provider);
    app.sessionStop = { key, provider, sid, label, busy: false, unsure: false, retry: "", after: 0 };
    $("session-stop-desc").replaceChildren(
      el("p", {}, "About to stop all follow-ups of this AI session:"),
      kv(["Provider", pl + (provider !== pl ? ` (${provider})` : "")], ["Session", label], ["Session ID", sid, true]),
      el("div", { class: "bulk-count", role: "group", "aria-label": "Number of running watches to be stopped" },
        el("b", { id: "session-stop-n" }), el("span", { text: "running watches" }), el("small", { id: "session-stop-sub" })),
      el("div", { class: "warn-box", text: "This is a bulk action, unlike “Stop this watch”: it stops every running thread, DM, and channel watch of this session at once, including watches hidden by the current search or filters and not shown in the scene or lists. Stopping is permanent: it does not resume automatically, and the UI offers no resume button." }),
      el("ul", { class: "bulk-points" },
        el("li", { text: "Affects only this session (exact match on provider and session ID); other sessions, including sessions with the same ID from other providers, are not affected." }),
        el("li", { text: "Does not end the AI session itself, and does not delete any history, events, or activity." }),
        el("li", { text: "Watches this session registers later are not blocked and can start normally." })),
      el("p", { class: "note", id: "session-stop-status", role: "status", "aria-live": "polite" }),
      el("div", { class: "err-box", id: "session-stop-error", role: "alert", hidden: true }));
    syncSessionStopDialog();
    syncSessionFoot();
    $("session-stop").showModal();
    $("session-stop-cancel").focus(); // cancel is the safe default
  }
  function sessionStopError(text) {
    const box = $("session-stop-error");
    if (!box) return;
    box.hidden = !text;
    box.textContent = text;
  }
  // the identity (provider / id / label) stays frozen; only the count and button state follow new snapshots
  function syncSessionStopDialog() {
    const st = app.sessionStop;
    const n = $("session-stop-n");
    if (!st || !n) return;
    const run = sessionRunning(st.key);
    const lim = obj(app.snap && app.snap.limits);
    setText(n, run.truncated ? `At least ${run.n}` : String(run.n));
    setText($("session-stop-sub"),
      `Threads ${run.by.channel_thread} · DMs ${run.by.dm} · channels ${run.by.channel}. Counted from the full snapshot, unaffected by search, provider, or history-range filters.` +
      (run.truncated ? ` The snapshot was truncated at the limit${lim.watches ? ` of ${lim.watches}` : ""}; the actual number may be higher, and the stop result is authoritative.` : "") +
      (run.n === 0 ? " The snapshot shows no running watches left for this session." : ""));
    $("session-stop-ok").disabled = st.busy || run.n === 0;
    $("session-stop-cancel").disabled = st.busy;
    setText($("session-stop-ok"), st.busy ? "Stopping…" : st.retry || "Confirm stopping all follow-ups");
    setText($("session-stop-status"), st.busy ? "Submitting the stop request; do not click again. It will not retry automatically." : "");
  }

  // kind: "ok" | "failed" (the request was rejected, nothing was executed) | "unsure" (outcome unknown)
  function classifySessionStop(res, data, st) {
    const body = obj(data);
    const code = /^[a-z0-9_.:-]{1,64}$/i.test(str(body.error)) ? str(body.error) : "";
    if (res.ok && body.ok === true) {
      const same = (!body.provider || str(body.provider) === st.provider) && (!body.session_id || str(body.session_id) === st.sid);
      if (!same) return { kind: "unsure", msg: "The session returned by the service does not match the requested session; cannot confirm the stop result" };
      const n = num(body.stopped_count);
      return { kind: "ok", n: n !== null && n >= 0 ? Math.floor(n) : null };
    }
    const why = code || `HTTP ${res.status}`;
    if (res.status === 404) return { kind: "failed", msg: `The server cannot find this session (${why}); nothing was stopped` };
    if (res.status === 400) return { kind: "failed", msg: `The service rejected the request (${why}); nothing was stopped` };
    if (res.status === 403) return { kind: "failed", msg: `Request denied (${why}); the page security token may have expired and nothing was stopped. Try again after the state refreshes` };
    if (res.status === 503) return { kind: "unsure", msg: `The service cannot access local state right now (${why}); no stop result received` };
    if (res.status >= 400 && res.status < 500) return { kind: "failed", msg: `The request was not accepted (${why}); nothing was stopped` };
    return { kind: "unsure", msg: `The service returned a response that cannot be confirmed (${why})` };
  }
  // exactly one POST per click; there is no automatic retry, a retry is always an explicit user action
  async function postSessionStop(st) {
    const token = str(app.snap && app.snap.csrf_token);
    if (!token) return { kind: "failed", msg: "Security token not ready yet (waiting for the state snapshot); the request was not sent" };
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), STOP_TIMEOUT_MS);
    try {
      let res;
      try {
        res = await fetch("/api/sessions/stop", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
          body: JSON.stringify({ provider: st.provider, session_id: st.sid }),
          credentials: "same-origin",
          signal: ctl.signal,
        });
      } catch (e) {
        // no response received: the server may or may not have applied the stop
        return { kind: "unsure", msg: e && e.name === "AbortError" ? "Request timed out; no response from the service" : "Network error; no response from the service" };
      }
      let data = null;
      try { data = await res.json(); } catch (_) { /* non-JSON error body or interrupted read */ }
      return classifySessionStop(res, data, st);
    } finally {
      clearTimeout(to);
    }
  }

  async function doSessionStop() {
    const st = app.sessionStop;
    if (!st || st.busy || !$("session-stop").open || sessionRunning(st.key).n === 0) return;
    st.busy = true;
    st.unsure = false;
    st.retry = "";
    sessionStopError("");
    syncSessionStopDialog();
    syncSessionFoot();
    $("session-stop-desc").focus(); // the clicked button is about to be disabled; keep focus inside the dialog
    const out = await postSessionStop(st);
    st.busy = false;
    if (out.kind === "ok") {
      const n = out.n;
      const msg = n === null ? "The stop request completed, but the service returned no stopped count; refreshing state to verify."
        : n === 0 ? "No running watches needed stopping (0 actually stopped): the watches of this session may already have been stopped."
        : `Stopped ${n} running watches of this session. The AI session itself and its history are not affected.`;
      app.sessionStop = null;
      app.stopAllHold = { key: st.key, target: app.ingestN + (app.inflight ? 2 : 1), until: Date.now() + STOP_ALL_HOLD_MS };
      toast(msg);
      copyStatus($("session"), msg); // also announced inside the open modal sheet
      syncSessionFoot();
      $("session-stop").close();
      // no local state is faked: the poll below re-renders the scene, drawer lists and this session sheet from the new snapshot
      refreshNow();
      return;
    }
    const unsure = out.kind === "unsure";
    st.unsure = unsure;
    st.retry = unsure ? "Retry after checking state" : "Retry stop";
    st.after = app.ingestN + (app.inflight ? 2 : 1); // first snapshot fetched after this response
    const full = unsure
      ? `${out.msg}. The stop may have taken effect fully, partly, or not at all. Refreshing the latest state; check the number of running watches before deciding whether to retry. A manual retry is safe; watches already stopped are not affected.`
      : `${out.msg}. Click “Retry stop” to try again.`;
    toast(unsure ? "Stop result uncertain: " + out.msg : `Failed to stop all follow-ups of the session: ${out.msg}`, "bad");
    sessionStopError(full);
    syncSessionStopDialog();
    syncSessionFoot();
    $("session-stop-cancel").focus();
    refreshNow();
  }
  // after an unconfirmed result, a snapshot fetched afterwards may show the session has nothing left running
  function resolveUnsureStopAll() {
    const st = app.sessionStop;
    if (!st || !st.unsure || st.busy || app.error || app.ingestN < st.after) return;
    const run = sessionRunning(st.key);
    if (run.n > 0 || run.truncated) return; // a truncated snapshot cannot prove that nothing is left
    app.sessionStop = null;
    const msg = "Confirmed: the refreshed snapshot shows no running watches left for this session; no retry needed.";
    toast(msg);
    copyStatus($("session"), msg);
    $("session-stop").close();
  }

  /* ================= status, filters, orchestration ================= */

  function renderStatus() {
    const chip = $("conn-chip");
    let state, label, sub;
    if (!app.firstDone && !app.error) { state = "loading"; label = "Connecting"; sub = "LOCAL · CONNECTING"; }
    else if (app.error) {
      state = "error";
      label = app.snap ? "Refresh failed · data stale" : "Failed to fetch state";
      sub = "LOCAL · REQUEST FAILED";
    } else { state = "live"; label = "Connected · live"; sub = "LOCAL · LIVE"; }
    chip.setAttribute("data-state", state);
    chip.title = sub;
    setText($("conn-label"), label);
    setText($("conn-sub"), sub);

    const banner = $("stale-banner");
    if (app.error && app.snap) {
      banner.hidden = false;
      setText(banner, `Failed to fetch the latest state (${app.error}). Showing the last snapshot from ${fmtClock(app.lastOkAt / 1000)}; data may be stale. Retrying automatically.`);
    } else banner.hidden = true;

    const sup = obj(app.snap && app.snap.supervisor);
    const sc = $("sup-chip");
    const health = ["healthy", "stale", "offline"].includes(sup.health) ? sup.health : "unknown";
    const hl = { healthy: "Supervisor healthy", stale: "Supervisor heartbeat lagging", offline: "Supervisor offline", unknown: "Supervisor status unknown" }[health];
    sc.setAttribute("data-state", app.error && app.snap ? "stale" : health === "unknown" ? "loading" : health);
    setText($("sup-label"), hl);
    const bits = ["SUPERVISOR"];
    if (num(sup.pid) !== null) bits.push("PID " + sup.pid);
    const age = num(sup.heartbeat_age_seconds);
    bits.push(age === null ? "No heartbeat" : `Heartbeat ${age < 10 ? age.toFixed(1) : Math.round(age)}s`);
    if (sup.version) bits.push(str(sup.version));
    const subText = bits.join(" · ") + (app.error && app.snap ? " · snapshot stale" : "");
    setText($("sup-sub"), subText);
    sc.title = subText;
    tickSync();
  }
  function tickSync() {
    const lab = $("sync-label");
    if (!app.lastOkAt) { setText(lab, "Not synced yet"); return; }
    setText(lab, `${fmtClock(app.lastOkAt / 1000)} · ${fmtRel(app.lastOkAt / 1000)}`);
  }
  function tickRel() {
    for (const t of document.querySelectorAll("time[data-rel]")) t.textContent = fmtRel(t.getAttribute("data-rel"));
    tickSync();
  }

  function renderProviders() {
    const ids = providerIds();
    once("providers", JSON.stringify([...ids]), () => {
      const sel = $("provider-filter");
      sel.replaceChildren(el("option", { value: "all", text: "All providers" }));
      for (const [id, label] of ids) sel.append(el("option", { value: id, text: label }));
      if (app.providerFilter !== "all" && !ids.has(app.providerFilter)) app.providerFilter = "all";
      sel.value = app.providerFilter;
    });
    renderLegend(ids);
  }

  function syncScopeUI() {
    const running = app.stateFilter === "running";
    $("scope-running").setAttribute("aria-pressed", running ? "true" : "false");
    $("scope-all").setAttribute("aria-pressed", running ? "false" : "true");
    $("state-field").hidden = running;
    $("state-filter").value = app.stateFilter;
  }
  function afterFilterChange() {
    app.sceneLimit = SCENE_STEP;
    scene.user = false;
    scene.needFit = true;
    app.sigs.watches = null; app.sigs.timeline = null; app.sigs.summary = null;
    syncScopeUI();
    renderAll();
  }
  function setScope(scope) {
    app.stateFilter = scope;
    afterFilterChange();
  }
  function clearFilters() {
    app.search = ""; app.providerFilter = "all"; app.stateFilter = "running"; app.focusKey = null; app.focusPerson = null;
    $("search").value = "";
    $("provider-filter").value = "all";
    afterFilterChange();
  }

  function renderAll() {
    renderStatus();
    renderProviders();
    const v = visible();
    renderScene(v);
    renderDrawer(v);
    renderDetail();
    renderSession();
    renderPeople();
    syncSessionStopDialog();
    resolveUnsureStopAll();
    resolveUnsureStop();
    tickLifecycle();
  }

  // after an ambiguous stop timeout, a fresh snapshot may show the stop did apply
  function resolveUnsureStop() {
    const id = app.stopUnsure;
    if (!id || app.error || app.pendingStops.has(id)) return;
    const w = model().watchById.get(id);
    if (w && stoppable(w)) return;
    app.stopUnsure = null;
    if (app.confirmWatch === id && $("confirm").open) $("confirm").close();
    toast(`Confirmed: watch ${short(id, 16)} state: ${w ? watchStateLabel(w.state) : "no longer in the snapshot"}; no retry needed.`);
  }

  function ingest(data) {
    const events = arr(data.events).map(obj);
    const ids = new Set(events.map((e) => str(e.id)));
    if (app.seenEvents) {
      const fresh = events.filter((e) => e.id && !app.seenEvents.has(str(e.id)));
      for (const e of fresh) {
        app.fresh.set(str(e.id), Date.now() + FRESH_MS);
        if (e.watch_id) app.bursts.add(str(e.watch_id));
      }
      if (fresh.length) toast(`Detected ${fresh.length} new activity records (from the API).`);
    }
    for (const [k, t] of app.fresh) if (t < Date.now()) app.fresh.delete(k);
    app.seenEvents = ids;
    app.snap = data;
    app.snapAt = Date.now();
    app.ingestN += 1;
    learnFreshness(data);
  }

  async function poll() {
    app.inflight = true;
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
    try {
      const res = await fetch("/api/state", { cache: "no-store", headers: { Accept: "application/json" }, signal: ctl.signal, credentials: "same-origin" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Invalid response format");
      ingest(data);
      app.error = null;
      app.lastOkAt = Date.now();
      // let fresh highlights expire and re-render once
      if (app.fresh.size) setTimeout(() => { if (!app.inflight) { app.sigs.timeline = null; renderAll(); } }, FRESH_MS + 100);
    } catch (err) {
      app.error = err && err.name === "AbortError" ? "Request timed out" : (err && err.message) || "Network error";
    } finally {
      clearTimeout(to);
      app.firstDone = true;
      app.inflight = false;
      renderAll();
      if (app.rerun) { app.rerun = false; poll(); } else schedule();
    }
  }
  function schedule() {
    clearTimeout(app.timer);
    if (document.hidden) return;
    app.timer = setTimeout(() => { if (!app.inflight) poll(); }, POLL_MS);
  }
  function refreshNow() {
    clearTimeout(app.timer);
    if (app.inflight) { app.rerun = true; return; }
    poll();
  }

  function applyMotion() {
    const reduced = !!(reducedMq && reducedMq.matches);
    let pref = true;
    try { pref = localStorage.getItem("fo-motion") !== "off"; } catch (_) { /* storage unavailable */ }
    app.motion = pref && !reduced;
    document.body.setAttribute("data-motion", app.motion ? "on" : "off");
    const btn = $("motion-toggle");
    btn.setAttribute("aria-pressed", app.motion ? "true" : "false");
    $("motion-label").textContent = reduced ? "Motion off (system setting)" : app.motion ? "Motion on" : "Motion off";
    btn.disabled = reduced;
    fx.sig = "";
    if (!app.motion) cancelTween();
  }

  function bind() {
    let deb = 0;
    $("search").addEventListener("input", (e) => {
      clearTimeout(deb);
      deb = setTimeout(() => { app.search = e.target.value; afterFilterChange(); }, 140);
    });
    $("scope-running").addEventListener("click", () => setScope("running"));
    $("scope-all").addEventListener("click", () => setScope("all"));
    $("state-filter").addEventListener("change", (e) => { app.stateFilter = e.target.value; afterFilterChange(); });
    $("provider-filter").addEventListener("change", (e) => { app.providerFilter = e.target.value; afterFilterChange(); });
    $("refresh-btn").addEventListener("click", refreshNow);
    $("motion-toggle").addEventListener("click", () => {
      try { localStorage.setItem("fo-motion", app.motion ? "off" : "on"); } catch (_) { /* ignore */ }
      applyMotion();
      renderAll();
    });
    if (reducedMq && reducedMq.addEventListener) reducedMq.addEventListener("change", () => { applyMotion(); renderAll(); });

    // drawer + tabs
    $("drawer-toggle").addEventListener("click", () => setDrawer(!app.drawerOpen, { focus: !app.drawerOpen }));
    $("drawer-close").addEventListener("click", () => setDrawer(false, { restore: true }));
    $("drawer").addEventListener("keydown", (e) => {
      if (e.key === "Escape" && app.drawerOpen && !document.querySelector("dialog[open]")) { e.preventDefault(); setDrawer(false, { restore: true }); }
    });
    const tabs = [...document.querySelectorAll(".tab")];
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => selectTab(t.getAttribute("data-tab"), false));
      t.addEventListener("keydown", (e) => {
        const dir = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (dir) { e.preventDefault(); selectTab(tabs[(i + dir + tabs.length) % tabs.length].getAttribute("data-tab"), true); }
        else if (e.key === "Home") { e.preventDefault(); selectTab(tabs[0].getAttribute("data-tab"), true); }
        else if (e.key === "End") { e.preventDefault(); selectTab(tabs[tabs.length - 1].getAttribute("data-tab"), true); }
      });
    });

    // dialogs
    const detail = $("detail"), confirm = $("confirm"), people = $("people");
    $("detail-close").addEventListener("click", closeDetail);
    detail.addEventListener("click", (e) => { if (e.target === detail) closeDetail(); });
    detail.addEventListener("close", () => {
      const silent = takeSilent(detail);
      const keys = app.detailReturn;
      app.selectedWatch = null;
      app.sigs.detail = null; app.sigs.watches = null;
      pvStop();
      applyFocus();
      renderDrawer();
      if (!silent) requestAnimationFrame(() => focusByKey(keys));
    });
    $("detail-stop").addEventListener("click", () => { if (app.selectedWatch) openConfirm(app.selectedWatch, ["w:" + app.selectedWatch, ...app.detailReturn]); });
    $("pv-refresh").addEventListener("click", () => pvFetch("manual"));

    const session = $("session");
    $("session-close").addEventListener("click", () => session.close());
    session.addEventListener("click", (e) => { if (e.target === session) session.close(); });
    session.addEventListener("close", () => {
      const silent = takeSilent(session);
      const keys = app.sessionReturn;
      app.selectedSession = null;
      app.sigs.session = null;
      hideCopyFallback(session);
      copyStatus(session, "");
      if (!silent) requestAnimationFrame(() => focusByKey(keys));
    });
    $("session-focus").addEventListener("click", focusFromSession);
    $("session-copy-all").addEventListener("click", (e) => copySessionSummary(e.currentTarget));

    $("people-close").addEventListener("click", () => people.close());
    $("people-done").addEventListener("click", () => people.close());
    people.addEventListener("click", (e) => { if (e.target === people) people.close(); });
    people.addEventListener("close", () => {
      const silent = takeSilent(people);
      const keys = app.peopleReturn;
      app.peopleCtx = null;
      app.sigs.people = null;
      if (!silent) requestAnimationFrame(() => focusByKey(keys));
    });

    $("confirm-cancel").addEventListener("click", () => confirm.close());
    $("confirm-ok").addEventListener("click", doStop);
    confirm.addEventListener("click", (e) => { if (e.target === confirm && !(app.confirmWatch && app.pendingStops.has(app.confirmWatch))) confirm.close(); });
    confirm.addEventListener("cancel", (e) => { if (app.confirmWatch && app.pendingStops.has(app.confirmWatch)) e.preventDefault(); });
    confirm.addEventListener("close", () => {
      const keys = app.confirmReturn;
      app.confirmWatch = null;
      if (detail.open) $("detail-stop").focus();
      else requestAnimationFrame(() => focusByKey(keys));
    });

    // stop whole session: separate dialog; cancel / Esc / backdrop are safe, and none of them close it while the request is in flight
    const sstop = $("session-stop");
    $("session-stop-all").addEventListener("click", openSessionStop);
    $("session-stop-cancel").addEventListener("click", () => { if (!stopAllBusy()) sstop.close(); });
    $("session-stop-ok").addEventListener("click", doSessionStop);
    sstop.addEventListener("click", (e) => { if (e.target === sstop && !stopAllBusy()) sstop.close(); });
    sstop.addEventListener("cancel", (e) => { if (stopAllBusy()) e.preventDefault(); });
    sstop.addEventListener("close", () => {
      if (stopAllBusy()) { sstop.showModal(); $("session-stop-desc").focus(); return; } // closed by the browser anyway (repeated Esc): keep showing the pending request
      app.sessionStop = null;
      syncSessionFoot();
      // the sidebar button may be disabled by now (nothing left to stop): fall back to the sheet's close button
      if ($("session").open) ($("session-stop-all").disabled ? $("session-close") : $("session-stop-all")).focus();
    });

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { clearTimeout(app.timer); pvPause(); }
      else { refreshNow(); pvTick(); }
    });
    setInterval(() => { tickRel(); tickLifecycle(); pvTick(); }, 1000);
  }

  function init() {
    applyMotion();
    initScene();
    bind();
    let open = false;
    try { open = localStorage.getItem("fo-drawer") === "1"; } catch (_) { /* storage unavailable */ }
    setDrawer(open && window.innerWidth >= 900, { silent: true });
    selectTab("watches", false);
    syncScopeUI();
    renderStatus();
    renderAll();
    poll();
  }

  init();
})();
