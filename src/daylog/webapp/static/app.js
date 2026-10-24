// daylog web app: the one interface to the journal and the assistant.
// Plain ES module, no build step. Talks to the JSON API in docs/web-api.md.
// Server text is only ever inserted as text nodes (see md()), never as HTML.

const SLOW = 180_000;     // LLM calls: brief, chat, plan, ripple, review, log
const NORMAL = 30_000;
const WORLD = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json";
const NS = "http://www.w3.org/2000/svg";

const S = {
  today: null, tz: null,
  homeP: null, homeStale: false,
  open: [],                 // open cards from GET /api/cards
  cardEls: new Map(),       // card id -> Set of rendered elements
  logDate: null,            // null = today (the server applies the day cutoff)
};

// ---------------------------------------------------------------- helpers

const $ = (sel, el = document) => el.querySelector(sel);

function h(tag, attrs, ...kids) {
  const [name, ...cls] = tag.split(".");
  const el = document.createElement(name || "div");
  if (cls.length) el.className = cls.join(" ");
  if (attrs != null && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    kids.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.classList.add(...String(v).split(" ").filter(Boolean));
    else if (k === "style" && typeof v === "object") {
      for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    } else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  add(el, kids);
  return el;
}

function fill(el, ...kids) {
  el.replaceChildren();
  return add(el, kids);
}

function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false || k === "") continue;
    el.append(k instanceof Node ? k : String(k));
  }
  return el;
}

const ICONS = {
  mic: '<rect x="9" y="3" width="6" height="11.5" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2.5"/>',
  chat: '<path d="M4.5 5h15a1 1 0 0 1 1 1v9.5a1 1 0 0 1-1 1H10l-5.5 4v-4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z"/>',
  send: '<path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  chev: '<path d="M9 6l6 6-6 6"/>',
  left: '<path d="M15 6l-6 6 6 6"/>',
  right: '<path d="M9 6l6 6-6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
};
function icon(name, cls) {
  const s = document.createElementNS(NS, "svg");
  if (cls) s.setAttribute("class", cls);
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = ICONS[name]; // constant markup, never server text
  return s;
}

const pad = n => String(n).padStart(2, "0");
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WD = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
function parseD(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ""));
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}
const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
function short(s) { const d = parseD(s); return d ? `${MON[d.getMonth()]} ${d.getDate()}` : String(s || ""); }
function weekday(s, len = 0) { const d = parseD(s); if (!d) return ""; const w = WD[d.getDay()]; return len ? w.slice(0, len) : w; }
function addDays(s, n) { const d = parseD(s); d.setDate(d.getDate() + n); return iso(d); }
function daysBetween(a, b) { return Math.round((parseD(b) - parseD(a)) / 86400000); }
const todayIso = () => S.today || iso(new Date());
const clock = x => { const m = Math.round(x * 60); return `${pad(Math.floor(m / 60))}:${pad(m % 60)}`; };
const num = (x, digits = 1) => x == null || Number.isNaN(+x) ? null
  : Math.abs(x) >= 100 ? Math.round(x).toLocaleString("en-US") : String(+(+x).toFixed(digits));
const words = s => String(s ?? "").replace(/_/g, " ");
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function announce(text) { const el = $("#announce"); el.textContent = ""; setTimeout(() => { el.textContent = text; }, 30); }

// --------------------------------------------------------- safe markdown

// Paragraphs (soft-wrapped lines join, as in markdown), # headings, - / 1.
// lists, **bold**, *italic* / _italic_, `code` and http(s) links. Everything
// lands as text nodes.
const INLINE = /(\*\*([^*\n]+)\*\*|`([^`\n]+)`|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"']*[^\s<>"'.,;:!?)\]])|\*([^*\s][^*\n]*?)\*|(?<![\w])_([^_\s][^_\n]*?)_(?![\w]))/g;

function inline(el, text) {
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) el.append(text.slice(last, m.index));
    if (m[2]) el.append(h("strong", m[2]));
    else if (m[3]) el.append(h("code", m[3]));
    else if (m[4]) el.append(link(m[5], m[4]));
    else if (m[6]) el.append(link(m[6], m[6].replace(/^https?:\/\/(www\.)?/, "")));
    else if (m[7]) el.append(h("em", m[7]));
    else if (m[8]) el.append(h("em", m[8]));
    last = m.index + m[0].length;
  }
  if (last < text.length) el.append(text.slice(last));
  return el;
}
function link(href, text) {
  return /^https?:\/\//.test(href) ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text) : document.createTextNode(text);
}

function md(text, cls = "") {
  const root = h("div.md" + (cls ? "." + cls : ""));
  if (text == null || text === "") return root;
  const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
  let para = [], list = null;
  const flushPara = () => {
    if (!para.length) return;
    const p = h("p");
    inline(p, para.map(l => l.trim()).join(" "));
    root.append(p); para = [];
  };
  const flushList = () => { if (list) root.append(list); list = null; };
  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if ((m = /^\s{0,3}#{1,6}\s+(.*)$/.exec(line))) {
      flushPara(); flushList();
      const t = m[1].replace(/\s*#+$/, "");
      root.append(inline(/^\d{1,2}:\d{2}\b/.test(t) ? h("p.note-time") : h("h4"), t));
      continue;
    }
    if ((m = /^\s*(?:[-*•·]|(\d+)[.)])\s+(.*)$/.exec(line))) {
      flushPara();
      const ordered = !!m[1];
      if (!list || (list.tagName === "OL") !== ordered) { flushList(); list = h(ordered ? "ol" : "ul"); }
      list.append(inline(h("li"), m[2]));
      continue;
    }
    if (list && /^\s{2,}\S/.test(raw)) { const li = list.lastChild; li.append(" "); inline(li, line.trim()); continue; }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();
  return root;
}

// Report text (surf, fly, usage…) keeps its lines and spacing.
function pre(text) {
  const el = h("div.md.pre");
  return inline(el, String(text ?? ""));
}

// A long text, clamped with a "Read all" toggle.
function clamped(node, limit = 600) {
  const len = node.textContent.length;
  if (len < limit) return node;
  const wrap = h("div.clamp", node);
  const btn = h("button.btn.ghost", { type: "button", style: { "margin-left": "-12px" } }, "Read all");
  btn.addEventListener("click", () => {
    const open = wrap.classList.toggle("clamp");
    btn.textContent = open ? "Read all" : "Show less";
  });
  return h("div", wrap, btn);
}

// --------------------------------------------------------------- API client

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

const STATUS_TEXT = {
  400: "The server didn't accept that.",
  403: "The server refused that request (security check). Reload the app and try again.",
  404: "Not found.",
  410: "This has expired.",
  413: "That's too large to send.",
  429: "Too many requests; wait a moment.",
};

async function api(path, { method = "GET", body, raw, type, timeout = NORMAL, key } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  const headers = { Accept: "application/json" };
  let payload;
  if (method !== "GET") {
    headers["X-Daylog"] = "1";
    // One key per note, reused by Retry, so a resend isn't logged twice.
    if (key) headers["Idempotency-Key"] = key;
    if (raw) { payload = raw; headers["Content-Type"] = type || "application/octet-stream"; }
    else { payload = JSON.stringify(body ?? {}); headers["Content-Type"] = "application/json"; }
  }
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload, credentials: "same-origin", signal: ctrl.signal, cache: "no-store" });
  } catch {
    if (ctrl.signal.aborted) {
      throw new ApiError(0, "timeout", `No answer after ${Math.round(timeout / 1000)} s. It may still finish on the server; check before retrying.`);
    }
    throw new ApiError(0, "network", "Couldn't reach daylog. Check the connection.");
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (res.status === 401) { signedOut(); throw new ApiError(401, "signed_out", "You're signed out."); }
  if (!res.ok) {
    const msg = (data && data.message) || STATUS_TEXT[res.status] || `Server error (${res.status}).`;
    throw new ApiError(res.status, (data && data.error) || "http", msg);
  }
  if (data == null) throw new ApiError(res.status, "bad_json", "The server answered with something unreadable.");
  return data;
}
const post = (path, body, timeout = SLOW) => api(path, { method: "POST", body, timeout });

// ------------------------------------------------- loading / error / progress

function skeleton(kind = "lines") {
  if (kind === "block") return h("div", h("span.skel.block"));
  if (kind === "tiles") return h("div.tiles", [0, 1, 2, 3, 4, 5].map(() => h("div.tile", h("span.skel.h", { style: { width: "60%" } }), h("span.skel.w60"))));
  return h("div", { "aria-busy": "true", "aria-label": "Loading" }, h("span.skel.w80"), h("span.skel"), h("span.skel.w60"));
}

function errorBox(err, retry) {
  const box = h("div.error", { role: "alert" }, h("div", err && err.message ? err.message : "Something went wrong."));
  if (retry && !(err && err.status === 401)) box.append(h("button.btn", { type: "button", onclick: retry }, "Retry"));
  return box;
}

async function load(target, call, render, kind) {
  fill(target, skeleton(kind));
  try {
    const data = await call();
    fill(target);
    render(data, target);
  } catch (e) {
    fill(target, errorBox(e, () => load(target, call, render, kind)));
  }
}

function progressBox(label, hint) {
  const time = h("span.mono.num", "0:00");
  const sub = h("div.muted", { style: { "font-size": "13px", "margin-top": "4px" } }, hint || "");
  const el = h("div.progress", { role: "status" }, h("div.row", h("span", label), time), h("div.progress-bar"), sub);
  const t0 = Date.now();
  const iv = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    time.textContent = `${Math.floor(s / 60)}:${pad(s % 60)}`;
    if (s === 25 && !hint) sub.textContent = "Still working; these can take a minute.";
  }, 1000);
  return { el, stop: () => clearInterval(iv) };
}

// Runs a slow call with a visible timer; failures show inline with Retry.
function runSlow({ target, label, hint, call, render, button }) {
  const go = async () => {
    if (button) button.disabled = true;
    const p = progressBox(label, hint);
    fill(target, p.el);
    try {
      const r = await call();
      fill(target);
      render(r, target);
      return r;
    } catch (e) {
      fill(target, errorBox(e, go));
      return null;
    } finally {
      p.stop();
      if (button) button.disabled = false;
    }
  };
  return go();
}

function empty(title, text) { return h("div.empty", title ? h("b", title) : null, text || ""); }
function okMsg(text) { return h("div.ok-msg", icon("check"), h("span", text)); }

// ---------------------------------------------------------------- cards

const KIND = {
  goal_slip: "Goal date", itinerary: "Itinerary", correction: "Correction", chat_proposal: "Proposed change",
  discipline_fix: "Discipline", discipline_add: "New discipline", ripple: "Ripple", plan: "Plan",
  place: "Place", want: "Wishlist", research: "Research",
};

function cardView(card) {
  const btns = h("div.btns");
  const result = h("div.result");
  const el = h("article.dcard", { "data-card": card.id, "data-kind": card.kind },
    h("div.kind", KIND[card.kind] || "Needs you"),
    h("h3", card.title || "Confirm?"),
    card.detail ? md(card.detail) : null,
    btns, result);
  const actions = card.actions && card.actions.length ? card.actions
    : [{ id: "confirm", label: "Confirm", style: "primary" }, { id: "cancel", label: "Cancel" }];
  for (const a of actions) {
    const cls = a.style === "primary" ? ".primary" : a.style === "danger" ? ".danger" : "";
    btns.append(h("button.btn" + cls, { type: "button", onclick: e => answerCard(card, a, el, e.currentTarget) }, a.label || a.id));
  }
  if (!S.cardEls.has(card.id)) S.cardEls.set(card.id, new Set());
  S.cardEls.get(card.id).add(el);
  return el;
}

function cardList(cards) {
  return cards && cards.length ? h("div.cards-out", cards.map(cardView)) : null;
}

async function answerCard(card, action, el, btn) {
  const copies = [...(S.cardEls.get(card.id) || [el])].filter(c => c.isConnected || c === el);
  const all = copies.flatMap(c => [...c.querySelectorAll(".btns button")]);
  all.forEach(b => { b.disabled = true; });
  const label = btn.textContent;
  fill(btn, h("span.spin"), label);
  try {
    const r = await post(`/api/cards/${encodeURIComponent(card.id)}`, { action: action.id });
    for (const c of copies) {
      c.querySelector(".btns")?.remove();
      c.classList.add("done");
      c.querySelector(".result").replaceChildren(okMsg(r.message || "Done."));
    }
    const follow = cardList(r.cards);
    if (follow) el.querySelector(".result").append(follow);
    announce(r.message || "Done.");
    S.cardEls.delete(card.id);
    S.open = S.open.filter(c => c.id !== card.id);
    setBadge(S.open.length);
    const lbl = $("#home-cards .label");
    if (lbl) lbl.textContent = S.open.length ? `Waiting on you · ${S.open.length}` : "Waiting on you · all answered";
    S.homeStale = true;
    refreshOpenCards();
  } catch (e) {
    if (e.status === 410) {
      for (const c of copies) {
        c.querySelector(".btns")?.remove();
        c.classList.add("gone");
        c.querySelector(".result").replaceChildren(h("div.muted", "This card expired (cards last 24 h). Ask again if it still matters."));
      }
      S.open = S.open.filter(c => c.id !== card.id);
      setBadge(S.open.length);
    } else {
      btn.textContent = label;
      all.forEach(b => { b.disabled = false; });
      el.querySelector(".result").replaceChildren(errorBox(e));
    }
  }
}

function setBadge(n) {
  document.querySelectorAll('[data-badge="cards"]').forEach(b => {
    b.hidden = !n;
    b.textContent = n > 9 ? "9+" : String(n);
    b.setAttribute("aria-label", `${n} waiting`);
  });
}

async function refreshOpenCards(render = false) {
  try {
    const r = await api("/api/cards");
    S.open = Array.isArray(r) ? r : (r.cards || []);
    setBadge(S.open.length);
    if (render) renderCardStrip();
  } catch (e) {
    if (render) {
      const strip = $("#home-cards");
      if (strip) { strip.hidden = false; fill(strip, h("div.label", "Waiting on you"), errorBox(e, () => refreshOpenCards(true))); }
    }
  }
}

function renderCardStrip() {
  const strip = $("#home-cards");
  if (!strip) return;
  strip.hidden = !S.open.length;
  if (!S.open.length) return;
  const shown = S.open.slice(0, 3);
  const more = S.open.length - shown.length;
  const box = h("div", shown.map(cardView));
  fill(strip, 
    h("div.card-head", h("h2.label", `Waiting on you · ${S.open.length}`)),
    box,
    more > 0 ? h("button.btn.ghost", { type: "button", style: { "margin": "8px 0 -8px -12px" }, onclick: e => { fill(box, ...S.open.map(cardView)); e.currentTarget.remove(); } }, `Show ${more} more`) : null,
  );
}

// ---------------------------------------------------------------- sheet

const sheet = $("#sheet");
function openSheet(title, body) {
  $("#sheet-title").textContent = title;
  const b = $("#sheet-body");
  fill(b, body);
  b.scrollTop = 0;
  if (!sheet.open) { sheet.showModal(); b.focus({ preventScroll: true }); }
  return b;
}
sheet.addEventListener("click", e => { if (e.target === sheet) sheet.close(); });

function confirmSheet(title, text, okLabel, danger = false) {
  return new Promise(resolve => {
    let answered = false;
    const done = v => { if (answered) return; answered = true; resolve(v); sheet.close(); };
    const body = h("div.stack-lg",
      md(text),
      h("div.btns",
        h("button.btn.primary" + (danger ? ".danger" : ""), { type: "button", onclick: () => done(true) }, okLabel),
        h("button.btn", { type: "button", onclick: () => done(false) }, "Cancel")));
    openSheet(title, body);
    sheet.addEventListener("close", () => { if (!answered) { answered = true; resolve(false); } }, { once: true });
  });
}

// ------------------------------------------------------------- sign in

function signedOut() {
  const box = $("#signin");
  if (!box.hidden) return;
  box.hidden = false;
  $("#app").inert = true;
  if (sheet.open) sheet.close();
  setTimeout(() => $("#pw").focus(), 50);
}

// ---------------------------------------------------------------- formatting

function actText(a) {
  if (a == null) return "";
  if (typeof a !== "object") return words(a);
  const name = words(a.type || a.label || a.kind || a.what || "?");
  const bits = [name];
  if (a.hours) bits.push(`${num(a.hours)} h`);
  else if (a.days) bits.push(plural(a.days, "day"));
  return bits.join(" · ");
}

function fmtAny(v) {
  if (v == null) return "";
  if (Array.isArray(v)) return v.map(fmtAny).filter(Boolean).join("; ");
  if (typeof v !== "object") return words(v);
  const head = v.what || v.items || v.name || v.text || v.label || v.type || v.title || v.note;
  const bits = [];
  if (head) bits.push(Array.isArray(head) ? head.join(", ") : String(head));
  for (const [k, val] of Object.entries(v)) {
    if (val == null || val === "" || [v.what, v.items, v.name, v.text, v.label, v.type, v.title, v.note].includes(val)) continue;
    if (k === "kcal") bits.push(`${num(val)} kcal`);
    else if (k === "protein_g") bits.push(`${num(val)} g protein`);
    else if (k.endsWith("_g") || k.endsWith("_id") || k === "lat" || k === "lon") continue;
    else if (typeof val === "number") bits.push(`${words(k)} ${num(val)}`);
    else if (typeof val === "string") bits.push(k === "where" || k === "place" ? `at ${val}` : val);
  }
  return bits.join(" · ");
}

function feltText(f) {
  if (f == null) return "";
  if (typeof f === "object" && !Array.isArray(f)) {
    const bits = [];
    for (const k of ["energy", "mood", "focus"]) if (f[k] != null) bits.push(`${k} ${f[k]}/5`);
    for (const [k, v] of Object.entries(f)) {
      if (["energy", "mood", "focus"].includes(k) || v == null || v === "") continue;
      bits.push(typeof v === "string" ? v : `${words(k)}: ${fmtAny(v)}`);
    }
    return bits.join(" · ");
  }
  return fmtAny(f);
}

function li(left, right, ...extra) {
  return h("li", h("div.li-main", h("span", left), right ? h("span.li-right", right) : null), ...extra);
}
const sub = text => text ? h("div.li-sub", text) : null;
function track(percent) {
  return h("div.track", { role: "progressbar", "aria-valuenow": percent, "aria-valuemin": 0, "aria-valuemax": 100 },
    h("i", { style: { width: `${Math.max(0, Math.min(100, percent))}%` } }));
}

function card(order, labelText, opts = {}) {
  const head = h("div.card-head", h("h2.label", labelText), opts.action || null);
  const body = h("div");
  const el = h("section.card" + (opts.cls ? "." + opts.cls : ""), { style: { order: String(order) }, id: opts.id }, head, body);
  el.head = head; el.body = body;
  return el;
}

function seg(options, current, onPick, label, wrap = false) {
  const el = h("div.seg" + (wrap ? ".wrap" : ""), { role: "group", "aria-label": label });
  const set = v => el.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === String(v))));
  for (const [v, text] of options) {
    el.append(h("button", { type: "button", "data-v": v, onclick: () => { set(v); onPick(v); } }, text));
  }
  set(current);
  return el;
}

// ---------------------------------------------------------------- data shared by tabs

function getHome(force = false) {
  if (force || !S.homeP) {
    S.homeP = api("/api/home");
    S.homeP.catch(() => { S.homeP = null; });
    S.homeStale = false;
  }
  return S.homeP;
}

// ================================================================ HOME

function initHome() {
  const root = $("#home");
  const hero = h("header.hero");
  const today = card(1, "Today");
  const strip = h("section.card", { id: "home-cards", style: { order: "2" }, hidden: true });
  const brief = card(3, "Morning brief");
  const cond = card(3, "Conditions");
  const stats = card(4, "Last 7 days");
  const cal = card(5, "Week");
  const goals = card(6, "Goals");
  const next = card(7, "Next");
  const miles = card(8, "Milestones");
  fill(root, hero, h("div.grid", h("div.col", today, cond, brief, cal), h("div.col", strip, stats, goals, next, miles)));
  Object.assign(S, { hHero: hero, hToday: today, hBrief: brief, hStats: stats, hCal: cal, hGoals: goals, hNext: next, hMiles: miles });
  initConditions(cond);
  loadHome(false);
}

async function loadHome(force) {
  const parts = [S.hToday, S.hGoals, S.hNext, S.hMiles];
  fill(S.hHero, h("span.skel.w40"), h("span.skel.h", { style: { height: "40px", "margin-top": "14px" } }), h("span.skel.w60"));
  parts.forEach(p => fill(p.body, skeleton()));
  fill(S.hStats.body, skeleton("tiles"));
  loadCalendar(null);
  loadBrief();
  refreshOpenCards(true);
  try {
    const d = await getHome(force);
    if (d.today) S.today = d.today;
    renderHero(d);
    renderToday(d);
    renderStats(d.week);
    renderGoals(S.hGoals.body, d.goals, false);
    renderNext(S.hNext.body, d.next);
    renderMilestones(S.hMiles.body, d.milestones, 6);
  } catch (e) {
    const retry = () => loadHome(true);
    fill(S.hHero, h("div.eyebrow", `${weekday(todayIso())}, ${short(todayIso())}`), errorBox(e, retry));
    [...parts, S.hStats].forEach(p => fill(p.body, empty(null, "Couldn't load.")));
  }
}

function renderHero(d) {
  const w = d.where || {};
  const lede = [];
  if (w.since) lede.push(`Since ${short(w.since)}${w.day ? ` · day ${w.day}` : ""}`);
  const nx = (d.next || [])[0];
  let nextLine = null;
  if (nx) {
    const n = nx.date ? daysBetween(d.today || todayIso(), nx.date) : null;
    nextLine = h("p.lede", "Next: ", h("strong", nx.place), ` · ${nx.when || short(nx.date)}`,
      n != null && n >= 0 ? ` (${n === 0 ? "today" : n === 1 ? "tomorrow" : `in ${n} days`})` : "");
  }
  fill(S.hHero, 
    h("div.eyebrow", `${d.weekday || weekday(d.today)}, ${short(d.today)}`),
    h("h2", { title: w.path || null }, w.place || "Location unknown"),
    lede.length ? h("p.lede", lede.join(" · ")) : null,
    nextLine,
  );
}

function renderToday(d) {
  const t = d.today_entry;
  const body = S.hToday.body;
  fill(body);
  if (t && (t.summary || (t.activities || []).length)) {
    if (t.summary) body.append(clamped(md(t.summary), 420));
    if ((t.activities || []).length) body.append(h("div.chips.today-acts", t.activities.map(a => h("span.chip", actText(a)))));
  } else {
    body.append(empty("Nothing logged yet today.", "Say what you did, how you felt and what you ate; daylog turns it into the journal."));
  }
  body.append(h("div.quick",
    h("a.btn.primary", { href: "#/log" }, icon("mic"), "Log"),
    h("a.btn", { href: "#/chat" }, icon("chat"), "Ask")));
}

function tile(value, unit, label, meter) {
  return h("div.tile",
    h("b.num", value ?? "–", value != null && unit ? h("small", unit) : null),
    meter != null && value != null ? h("div.meter", { "aria-hidden": "true" }, [1, 2, 3, 4, 5].map(i => h("i", { class: i <= Math.round(meter) ? "on" : "" }))) : null,
    h("span", label));
}

function renderStats(w) {
  const body = S.hStats.body;
  if (!w) { fill(body, empty(null, "No stats yet.")); return; }
  const tiles = h("div.tiles",
    tile(w.days_logged ?? 0, `/${w.of || 7}`, "Days logged"),
    tile(num(w.energy), "/5", "Energy", w.energy),
    tile(num(w.mood), "/5", "Mood", w.mood),
    tile(num(w.focus), "/5", "Focus", w.focus),
    tile(num(w.kcal), null, "kcal / day"),
    tile(num(w.protein_g), "g", "Protein / day"));
  const acts = (w.activities || []).filter(a => a && a.label);
  const max = Math.max(w.of || 7, ...acts.map(a => a.days || 0));
  const bars = acts.length ? h("div.hbars", { role: "list", "aria-label": "Activities by days done" },
    acts.slice(0, 8).map(a => h("div.hbar", { role: "listitem", title: `${words(a.label)}: ${plural(a.days, "day")}${a.hours ? `, ${num(a.hours)} h` : ""}` },
      h("span.name", words(a.label)),
      h("span", h("span.hb", { style: { display: "block", width: `${100 * (a.days || 0) / max}%` } })),
      h("span.v", `${a.days} d${a.hours ? ` · ${num(a.hours)} h` : ""}`)))) : null;
  fill(body, tiles, bars, h("p.cal-key", "Food is a rough estimate per logged day."));
}

function goalRight(g) {
  if (g.progress != null && g.target) return `${num(g.progress)} / ${num(g.target)}`;
  if (g.progress != null) return `${num(g.progress)} ${words(g.metric || "")}`.trim();
  return g.when || "";
}

function renderGoals(body, goals, detail) {
  fill(body);
  if (!goals || !goals.length) { body.append(empty("No active goals.", "Ask the assistant to set one; it confirms before writing it.")); return; }
  body.append(h("ul.list", goals.map(g => {
    const right = goalRight(g);
    const meta = [g.progress != null && g.target ? words(g.metric) : "", g.when !== right ? g.when : "", g.slips ? `moved ${g.slips}×` : ""].filter(Boolean).join(" · ");
    return li(g.title, right,
      g.percent != null ? track(g.percent) : null,
      sub(meta),
      detail ? h("div.chips", { style: { "margin-top": "8px" } },
        g.type === "hard" ? h("span.chip.warn", "hard date") : null,
        g.discipline ? h("span.chip.accent", words(g.discipline)) : null) : null,
      detail && g.notes ? h("div", { style: { "margin-top": "8px", "font-size": "14px", color: "var(--muted)" } }, md(g.notes)) : null);
  })));
}

function renderNext(body, next) {
  fill(body);
  if (!next || !next.length) { body.append(empty("Nothing dated on the itinerary.", "Mention a plan in a note or ask the assistant to add a leg.")); return; }
  body.append(h("ul.list", next.map(n => li(n.place, n.when || short(n.date),
    h("div.chips", { style: { "margin-top": "6px" } },
      n.status ? h("span.chip" + (n.status === "planned" ? ".accent" : ""), n.status) : null,
      n.type === "hard" ? h("span.chip.warn", "hard date") : null)))));
}

function renderMilestones(body, miles, limit) {
  fill(body);
  const list = [...(miles || [])].sort((a, b) => String(b.date).localeCompare(String(a.date)));
  if (!list.length) { body.append(empty("None yet.", "Firsts and bests you mention (a PB, a course passed) land here.")); return; }
  body.append(h("ul.list", list.slice(0, limit || list.length).map(m => li(m.what, short(m.date)))));
}

// ---- brief

function loadBrief() {
  const body = S.hBrief.body;
  load(body, () => api("/api/brief"), d => renderBrief(d, body));
}
function renderBrief(d, body) {
  const isToday = d && d.text && d.date === todayIso();
  const out = h("div");
  const btn = h("button.btn" + (isToday ? "" : ".primary"), { type: "button" }, icon("spark"), isToday ? "Remake today's brief" : "Make today's brief");
  btn.addEventListener("click", () => runSlow({
    target: out, button: btn, label: "Writing today's brief…", hint: "Weather, surf, plans and goals; about a minute.",
    call: () => post("/api/brief"),
    render: r => { fill(body); renderBrief(r, body); },
  }));
  if (d && d.text) {
    if (!isToday) body.append(h("div.faint.mono", { style: { "font-size": "12px", "margin-bottom": "10px" } }, `From ${weekday(d.date)}, ${short(d.date)}`));
    body.append(clamped(md(d.text), 700));
  } else {
    body.append(empty("No brief yet today.", "The brief is written each morning; you can make one now."));
  }
  body.append(out, h("div.btns", { style: { "margin-top": "12px" } }, btn));
}

// ---- week calendar

function loadCalendar(week) {
  const c = S.hCal;
  fill(c.body, skeleton("block"));
  const q = week ? `?week=${encodeURIComponent(week)}` : "";
  api(`/api/calendar${q}`).then(renderCalendar, e => fill(c.body, errorBox(e, () => loadCalendar(week))));
}

function layoutLanes(blocks) {
  const bs = blocks.map(b => ({ ...b, end: b.start + Math.max(0.5, b.hours || 1) }))
    .sort((a, b) => a.start - b.start || a.end - b.end);
  let cluster = [], ends = [], cEnd = -1;
  const finish = () => cluster.forEach(b => { b.lanes = ends.length; });
  for (const b of bs) {
    if (cluster.length && b.start >= cEnd) { finish(); cluster = []; ends = []; cEnd = -1; }
    let i = ends.findIndex(e => e <= b.start);
    if (i < 0) { i = ends.length; ends.push(b.end); } else ends[i] = b.end;
    b.lane = i; cluster.push(b); cEnd = Math.max(cEnd, b.end);
  }
  finish();
  return bs;
}

function renderCalendar(d) {
  const c = S.hCal;
  const days = (d.days || []).map(x => ({ ...x, blocks: layoutLanes(x.blocks || []) }));
  const placed = days.flatMap(x => x.blocks);
  const lo = Math.floor(Math.min(7, ...placed.map(b => b.start)));
  const hi = Math.min(24, Math.ceil(Math.max(22, ...placed.map(b => b.end))));
  const first = d.week_of || (days[0] && days[0].date);
  const last = first ? addDays(first, 6) : "";
  const nav = h("div.cal-nav",
    h("button.icon-btn", { type: "button", "aria-label": "Previous week", disabled: !d.prev, onclick: () => loadCalendar(d.prev) }, icon("left")),
    h("span", first ? `${short(first)} – ${short(last)}` : ""),
    h("button.icon-btn", { type: "button", "aria-label": "Next week", disabled: !d.next, onclick: () => loadCalendar(d.next) }, icon("right")));
  fill(c.head, h("h2.label", "Week"), nav);

  const gutter = h("div.cal-gutter", { "aria-hidden": "true" });
  for (let t = lo; t < hi; t += 3) gutter.append(h("span", { style: { top: `calc(${t - lo} * var(--hour))` } }, pad(t)));
  const grid = h("div.cal", { style: { "--span": String(hi - lo) } }, gutter);
  const today = todayIso();
  for (const day of days) {
    const head = h("span.cal-head", weekday(day.date, 3), h("b", String(parseD(day.date)?.getDate() ?? "")));
    const bodyEl = h("span.cal-body", day.blocks.map(b => h("span.blk" + (b.guess ? ".guess" : ""), {
      title: `${b.guess ? "≈" : "~"}${clock(b.start)} ${words(b.label)}`,
      style: {
        "--h": String(b.hue ?? 210),
        top: `calc(${b.start - lo} * var(--hour))`,
        height: `calc(${b.end - b.start} * var(--hour) - 2px)`,
        left: `${(100 * b.lane / b.lanes).toFixed(2)}%`,
        width: `${(100 / b.lanes).toFixed(2)}%`,
      },
    }, words(b.label))));
    const has = day.summary || day.blocks.length;
    const cls = (has ? "" : ".empty") + (day.date === today ? ".today" : "");
    grid.append(has
      ? h("button.cal-day" + cls, { type: "button", "aria-label": `${weekday(day.date)} ${short(day.date)}`, onclick: () => daySheet(day) }, head, bodyEl)
      : h("div.cal-day" + cls, { "aria-label": `${weekday(day.date)} ${short(day.date)}: nothing logged` }, head, bodyEl));
  }
  fill(c.body, grid, h("p.cal-key", "Rough times: ~ estimated from what you said, ≈ guessed from the order. Tap a day for its summary."));
}

function daySheet(day) {
  const times = [...day.blocks].sort((a, b) => a.start - b.start)
    .map(b => h("li", `${b.guess ? "≈" : "~"}${clock(b.start)}–${clock(b.end)} ${words(b.label)}`));
  const more = h("div");
  const btn = h("button.btn", { type: "button", onclick: () => { btn.remove(); load(more, () => api(`/api/journal/${day.date}`), j => more.append(dayDetail(j, { summary: false }))); } }, "Details & transcript");
  openSheet(`${weekday(day.date)}, ${short(day.date)}`, h("div.stack-lg",
    times.length ? h("ul.times", times) : null,
    day.summary ? md(day.summary) : empty(null, "No summary."),
    h("div", btn, more)));
}

// ================================================================ LOG

let rec = null; // { recorder, stream, chunks, t0, iv, mime }

function initLog() {
  const root = $("#log");
  const note = card(1, "New note");
  const result = h("section.card", { id: "log-result", style: { order: "2" }, hidden: true, "aria-live": "polite" });
  const recent = card(3, "Recent days");
  fill(root, h("div.grid", h("div.col", note, result), h("div.col", recent)));

  // day picker
  const picker = h("div.scroller", { role: "group", "aria-label": "Which day this note is for" });
  const hint = h("p.muted", { style: { "font-size": "13px", "margin-top": "8px", "min-height": "20px" } });
  const t = todayIso();
  const pick = v => {
    S.logDate = v;
    picker.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === (v || ""))));
    hint.textContent = v ? `Backdating: this note goes to ${weekday(v)}, ${short(v)}.` : "Goes to today (before the day cutoff, the night before).";
  };
  for (let i = 0; i < 7; i++) {
    const d = addDays(t, -i);
    const v = i === 0 ? "" : d;
    picker.append(h("button.daychip", { type: "button", "data-v": v, onclick: () => pick(v || null) },
      h("b", i === 0 ? "Today" : i === 1 ? "Yesterday" : weekday(d, 3)), h("span", short(d))));
  }
  pick(null);

  // recorder
  const recBtn = h("button.rec-btn", { type: "button", "aria-label": "Record a voice note" }, icon("mic"));
  const time = h("div.rec-time.num", { "aria-hidden": "true" });
  const recHint = h("div.rec-hint", "Tap to record. Tap again to stop and log it.");
  const cancel = h("button.btn.ghost", { type: "button", hidden: true }, "Discard");
  recBtn.addEventListener("click", () => (rec ? stopRec(true) : startRec()));
  cancel.addEventListener("click", () => stopRec(false));
  const canRecord = !!(navigator.mediaDevices && window.MediaRecorder);
  if (!canRecord) { recBtn.disabled = true; recHint.textContent = "This browser can't record audio here. Type the note below."; }

  // text
  const ta = h("textarea.textarea", { id: "log-text", rows: 3, placeholder: "Surfed Uluwatu at dawn, 2 hours, shoulder-high. Nasi campur for lunch…", "aria-label": "Note text" });
  const logBtn = h("button.btn.primary", { type: "submit" }, "Log it");
  const form = h("form", { onsubmit: e => { e.preventDefault(); logText(ta, logBtn); } }, ta,
    h("div.compose-row", h("span.faint", { style: { "font-size": "12.5px" } }, "Same pipeline as a voice note."), logBtn));
  ta.addEventListener("keydown", e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); logText(ta, logBtn); } });

  note.body.append(picker, hint, h("div.recorder", recBtn, time, recHint, cancel), h("div.or", "or type it"), form);
  Object.assign(S, { lRecBtn: recBtn, lTime: time, lRecHint: recHint, lCancel: cancel, lResult: result, lRecent: recent });
  loadRecent();
  catchUpNotes();
}

function pickMime() {
  const types = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg"];
  return types.find(t => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
}

async function startRec() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    S.lRecHint.textContent = e && e.name === "NotAllowedError"
      ? "Microphone access was blocked. Allow it in Settings, or type the note."
      : "Couldn't start the microphone. Type the note instead.";
    return;
  }
  const mime = pickMime();
  let recorder;
  try { recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined); }
  catch { recorder = new MediaRecorder(stream); }
  // The recorder's last block arrives during stop(), after stopRec has cleared
  // `rec`; pushing to this array (not rec.chunks) keeps it. iPhone Safari hands
  // over audio in large blocks, so dropping the last one lost most of a note
  // (2026-10-20: only the first 59 s arrived).
  const chunks = [];
  rec = { recorder, stream, chunks, t0: Date.now(), mime };
  recorder.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
  recorder.start(1000);
  S.lRecBtn.classList.add("on");
  fill(S.lRecBtn, icon("stop"));
  S.lRecBtn.setAttribute("aria-label", "Stop and log");
  S.lCancel.hidden = false;
  // iOS pauses the microphone when the app leaves the screen.
  S.lRecHint.textContent = S.logDate
    ? `Recording for ${weekday(S.logDate)}, ${short(S.logDate)}. Keep the app open; tap to stop.`
    : "Recording. Keep the app open; tap to stop.";
  const tick = () => {
    const s = Math.floor((Date.now() - rec.t0) / 1000);
    S.lTime.textContent = `${Math.floor(s / 60)}:${pad(s % 60)}`;
    if (s >= 15 * 60) stopRec(true); // stays well under the 20 MB limit
  };
  tick();
  rec.iv = setInterval(tick, 500);
}

function stopRec(send) {
  if (!rec) return;
  const r = rec;
  rec = null;
  clearInterval(r.iv);
  S.lRecBtn.classList.remove("on");
  fill(S.lRecBtn, icon("mic"));
  S.lRecBtn.setAttribute("aria-label", "Record a voice note");
  S.lCancel.hidden = true;
  S.lTime.textContent = "";
  S.lRecHint.textContent = "Tap to record. Tap again to stop and log it.";
  r.recorder.onstop = () => {
    r.stream.getTracks().forEach(t => t.stop());
    if (!send) { announce("Recording discarded."); return; }
    const type = (r.recorder.mimeType || r.mime || "audio/mp4").split(";")[0];
    const blob = new Blob(r.chunks, { type });
    if (!blob.size) { S.lRecHint.textContent = "Nothing was recorded. Try again."; return; }
    const q = S.logDate ? `?date=${S.logDate}` : "";
    const key = noteKey();
    sendNote(() => api(`/api/voice${q}`, { method: "POST", raw: blob, type, timeout: SLOW, key }), S.lRecBtn);
  };
  r.recorder.stop();
}

function noteKey() {
  return (crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function logText(ta, btn) {
  const text = ta.value.trim();
  if (!text) { ta.focus(); return; }
  const key = noteKey();
  sendNote(() => api("/api/log", { method: "POST", body: { text, date: S.logDate }, timeout: SLOW, key }), btn)
    .then(r => { if (r) ta.value = ""; });
}

// Notes log in the background (see webapp/jobs.py): only the upload needs the
// app open. Closing it while a note is "logging" used to cut the request, show
// a failure for a note the server went on to log, and Retry logged it again.
const PENDING_KEY = "daylog.pendingNotes";
function pendingNotes() {
  try { return JSON.parse(localStorage.getItem(PENDING_KEY) || "[]"); } catch { return []; }
}
function setPending(ids) {
  try { localStorage.setItem(PENDING_KEY, JSON.stringify(ids)); } catch { /* private mode */ }
}

function sendNote(upload, button) {
  const box = S.lResult;
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return runSlow({
    target: box, label: "Uploading…", call: upload, button,
    render: r => {
      setPending([...new Set([...pendingNotes(), r.job])]);
      watchNote(r.job);
    },
  });
}

function showLogging(box) {
  fill(box, h("div.progress", { role: "status" },
    h("div.row", h("span", "Logging… you can close the app.")),
    h("div.progress-bar")));
}

async function watchNote(id) {
  const box = S.lResult;
  box.hidden = false;
  showLogging(box);
  for (let tries = 0; tries < 200; tries++) {
    if (document.visibilityState !== "visible") return; // resumes on return (catchUpNotes)
    let job;
    try { job = await api(`/api/jobs/${id}`); } catch (e) {
      if (e.status === 404) { finishNote(id, null, "Not saved — please send it again."); return; }
      await sleep(4000);
      continue;
    }
    if (job.status === "done") { finishNote(id, job.result); return; }
    if (job.status === "failed") {
      const said = job.result && (job.result.transcript || job.result.text);
      finishNote(id, null, (job.result && job.result.message) || "Not saved — please send it again.", said);
      return;
    }
    await sleep(3000);
  }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

function finishNote(id, r, error, said) {
  setPending(pendingNotes().filter(x => x !== id));
  const box = S.lResult;
  box.hidden = false;
  fill(box);
  if (error) {
    box.append(errorBox(new ApiError(0, "failed", error)));
    // The recording was heard even if logging failed: keep the words.
    if (said) {
      const key = noteKey();
      box.append(h("blockquote", said), h("button.btn", {
        type: "button",
        onclick: () => sendNote(() => api("/api/log", { method: "POST", body: { text: said, date: S.logDate }, timeout: SLOW, key })),
      }, "Try again"));
    }
    loadRecent();
    return;
  }
  box.append(
    h("div.card-head", h("h2.label", `Logged · ${weekday(r.date)}, ${short(r.date)}`)),
    h("div.stack",
      r.transcript ? h("details.transcript", { style: { "margin-top": "0" } }, h("summary", "What you said"), h("blockquote", r.transcript)) : null,
      r.summary ? md(r.summary) : null,
      (r.notes || []).length ? h("ul.notes-list", r.notes.map(n => h("li", n))) : null,
      cardList(r.cards)));
  announce("Logged.");
  S.homeStale = true;
  if (r.cards && r.cards.length) refreshOpenCards();
  loadRecent();
}

// Back in the app: pick up notes that finished (or failed) while it was closed.
function catchUpNotes() {
  for (const id of pendingNotes()) watchNote(id);
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && S.lResult) catchUpNotes();
});

function loadRecent() {
  const body = S.lRecent.body;
  load(body, () => api("/api/journal"), d => {
    const days = d.days || [];
    if (!days.length) { body.append(empty("No days logged yet.", "Record or type your first note above.")); return; }
    body.append(h("div", days.map(dayRow)));
  });
}

function dayRow(d) {
  const meta = [d.location, d.notes ? plural(d.notes, "note") : ""].filter(Boolean).join(" · ");
  const body = h("div.day-body");
  const det = h("details.day",
    h("summary", icon("chev", "chev"), h("div", { style: { "min-width": "0", flex: "1" } },
      h("div.row.between", h("span.day-date", `${weekday(d.date, 3)}, ${short(d.date)}`), h("span.li-right", meta)),
      d.summary ? h("div.day-line", d.summary.replace(/^#+\s*\d{1,2}:\d{2}\s*/gm, "").replace(/\s+/g, " ").trim()) : null)),
    body);
  det.addEventListener("toggle", () => {
    if (det.open && !body.dataset.loaded) {
      body.dataset.loaded = "1";
      load(body, () => api(`/api/journal/${d.date}`), j => body.append(dayDetail(j, { summary: true })));
    }
  });
  return det;
}

function dayDetail(j, { summary }) {
  const kv = h("dl.kv");
  const row = (k, v) => { if (v) kv.append(h("dt", k), h("dd", v)); };
  row("Where", typeof j.location === "object" ? fmtAny(j.location) : j.location);
  row("Felt", feltText(j.felt));
  row("Meals", (j.meals || []).length ? h("ul.notes-list", j.meals.map(m => h("li", fmtAny(m)))) : null);
  row("Wins", (j.milestones || []).map(m => (typeof m === "object" ? m.what : m)).join("; "));
  const out = h("div.stack");
  const recon = h("div");
  const rbtn = h("button.btn", { type: "button" }, "Rebuild from its notes");
  rbtn.addEventListener("click", () => runSlow({
    target: recon, button: rbtn, label: "Rebuilding the day from all its notes…",
    call: () => post("/api/reconcile", { date: j.date }),
    render: r => { recon.append(okMsg(r.message || "Rebuilt.")); S.homeStale = true; },
  }));
  add(out, [
    (j.activities || []).length ? h("div.chips", j.activities.map(a => h("span.chip", actText(a)))) : null,
    kv.childNodes.length ? kv : null,
    summary && j.summary ? md(j.summary) : null,
    j.transcript ? h("details.transcript", h("summary", "Transcript"), h("blockquote", j.transcript)) : null,
    h("div.btns", rbtn), recon,
  ]);
  return out;
}

// ================================================================ CHAT

let chatBusy = false;

function initChat() {
  const root = $("#chat");
  const list = h("div.msgs", { role: "log", "aria-live": "polite", "aria-label": "Conversation" });
  const ta = h("textarea", { rows: 1, placeholder: "Ask anything", "aria-label": "Message", enterkeyhint: "send" });
  const send = h("button.send", { type: "submit", "aria-label": "Send", disabled: true }, icon("send"));
  const form = h("form", { onsubmit: e => { e.preventDefault(); sendChat(ta.value); } }, ta, send);
  const fit = () => { ta.style.height = "auto"; ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`; send.disabled = chatBusy || !ta.value.trim(); };
  ta.addEventListener("input", fit);
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !matchMedia("(pointer: coarse)").matches) { e.preventDefault(); sendChat(ta.value); }
  });
  fill(root, list, h("div.composer", form));
  Object.assign(S, { cList: list, cTa: ta, cSend: send, cFit: fit });
  loadChat();
}

function loadChat() {
  const list = S.cList;
  fill(list, h("div", { style: { padding: "16px 0" } }, skeleton()));
  api("/api/chat").then(d => {
    fill(list);
    const msgs = d.messages || [];
    if (!msgs.length) chatEmpty();
    else msgs.forEach(m => list.append(msgView(m)));
    scrollChat(false);
  }, e => fill(list, h("div", { style: { padding: "16px 0" } }, errorBox(e, loadChat))));
}

function chatEmpty() {
  const ideas = ["What should I do this afternoon?", "How did my week go?", "Find a freediving coach near me", "Move Austin back a week"];
  fill(S.cList, h("div.chat-empty",
    h("h2", "Ask the assistant"),
    h("p.muted", "It reads your journal, goals, itinerary and places, and can search the web. Anything it wants to change comes back as a card for you to confirm."),
    h("div.suggest", ideas.map(q => h("button.btn", { type: "button", onclick: () => sendChat(q) }, q)))));
}

function timeLabel(at) {
  if (!at) return "";
  const d = new Date(at);
  if (Number.isNaN(+d)) return "";
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return iso(d) === todayIso() ? t : `${short(iso(d))}, ${t}`;
}

function msgView(m, extra = {}) {
  const user = m.role === "user";
  const bubble = h("div.bubble", user ? m.text : md(m.text));
  const meta = [timeLabel(m.at), extra.cost ? `$${(+extra.cost).toFixed(3)}` : ""].filter(Boolean).join(" · ");
  return h("div.msg." + (user ? "user" : "assistant"), bubble, meta ? h("div.meta", meta) : null, user ? null : cardList(extra.cards));
}

function scrollChat(smooth) {
  if (location.hash !== "#/chat") return;
  requestAnimationFrame(() => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: smooth ? "smooth" : "auto" }));
}

function sendChat(raw) {
  const text = String(raw || "").trim();
  if (!text || chatBusy) return;
  if (S.cList.querySelector(".chat-empty")) fill(S.cList);
  S.cList.append(msgView({ role: "user", text, at: new Date().toISOString() }));
  S.cTa.value = "";
  S.cFit();
  askChat(text);
}

function askChat(text) {
  chatBusy = true;
  S.cSend.disabled = true;
  const status = h("span.mono", { style: { "font-size": "12px", color: "var(--faint)", "margin-left": "10px" } }, "0:00");
  const pending = h("div.msg.assistant", { "aria-busy": "true" },
    h("div.bubble", h("div.row", h("span.typing", { "aria-label": "Thinking" }, h("i"), h("i"), h("i")), status)));
  S.cList.append(pending);
  scrollChat(true);
  const t0 = Date.now();
  const iv = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    status.textContent = `${Math.floor(s / 60)}:${pad(s % 60)}${s > 15 ? " · looking things up" : ""}`;
  }, 1000);
  post("/api/chat", { text }).then(r => {
    pending.replaceWith(msgView({ role: "assistant", text: r.reply || "(no reply)", at: new Date().toISOString() }, { cards: r.cards, cost: r.cost }));
    if (r.cards && r.cards.length) refreshOpenCards();
    announce("Reply received.");
  }, e => {
    pending.removeAttribute("aria-busy");
    fill(pending, errorBox(e, () => { pending.remove(); askChat(text); }));
  }).finally(() => {
    clearInterval(iv);
    chatBusy = false;
    S.cFit();
    scrollChat(true);
  });
}

async function resetChat() {
  if (!S.cList) return;
  const ok = await confirmSheet("Start a new chat?", "Clears this conversation's history on the server. Your journal and vault aren't touched.", "New chat");
  if (!ok) return;
  try {
    await post("/api/chat/reset", {}, NORMAL);
    chatEmpty();
    announce("New chat.");
  } catch (e) {
    S.cList.append(errorBox(e, resetChat));
  }
}

// ================================================================ TRIPS

function initTrips() {
  const root = $("#trips");
  const map = card(1, "Where I'm headed", { cls: "wide" });
  const next = card(2, "Next legs");
  const plan = card(3, "Plan");
  const ripple = card(4, "What changed?");
  const places = card(5, "Places");
  const wish = card(6, "Wishlist");
  const trail = card(7, "Where I've been");
  fill(root, h("div.grid", map, h("div.col", plan, ripple, wish), h("div.col", next, places, trail)));

  load(map.body, () => api("/api/map"), d => drawMapCard(map.body, d), "block");
  fill(next.body, skeleton());
  getHome().then(d => renderNext(next.body, d.next), e => fill(next.body, errorBox(e, () => getHome(true).then(d => renderNext(next.body, d.next)))));
  load(plan.body, () => api("/api/plan"), d => renderPlan(plan.body, d));
  initRipple(ripple.body);
  initPlaces(places.body);
  initWishlist(wish.body);
  initTrail(trail);
}

// ---- map (ported from dashboard.py _MAP_JS)

let worldP = null;
function drawMapCard(body, data) {
  const stops = (data && data.stops) || [];
  if (!stops.length && !(data && data.here)) { body.append(empty("Nothing dated on the itinerary.", "Legs with dates and places show up here.")); return; }
  if (!window.d3 || !window.topojson) { body.append(errorBox({ message: "The map library didn't load (no connection to the CDN)." }, () => location.reload())); return; }
  const box = h("div.viz.map");
  body.append(h("div.legend", h("span", h("i", { style: { background: "var(--here)" } }), "Now"),
    h("span", h("i", { style: { background: "var(--dot)" } }), "Planned"),
    h("span", h("i", { style: { background: "var(--planned)", "border-radius": "2px" } }), "Countries ahead")), box);
  drawMap(box, data);
}

function drawMap(box, DATA) {
  const d3 = window.d3;
  const NAT = { "United States": "United States of America", USA: "United States of America" };
  const nat = c => NAT[c] || c;
  const W = 1000, H = 520;
  const svg = d3.create("svg").attr("viewBox", `0 0 ${W} ${H}`).attr("role", "img")
    .attr("aria-label", "World map of where the itinerary goes next");
  const g = svg.append("g");
  const tip = h("div.tip");
  const btn = (name, label, fn) => h("button", { type: "button", "aria-label": label, onclick: fn }, icon(name));
  fill(box, svg.node(), tip, h("div.viz-tools",
    btn("plus", "Zoom in", () => step(1.6)), btn("minus", "Zoom out", () => step(1 / 1.6)), btn("fit", "Fit", () => fit(all()))));
  box.querySelectorAll(".viz-tools svg").forEach(s => { s.style.cssText = "width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round"; });

  const projection = d3.geoNaturalEarth1().scale(190).translate([W / 2, H / 2 + 20]);
  const path = d3.geoPath(projection);
  const ahead = new Set(DATA.stops.filter(s => s.country).map(s => nat(s.country)));
  const here = DATA.here && DATA.here.lat != null ? [{ ...DATA.here, here: true }] : [];
  let pins = [];
  const all = () => [...here, ...pins];

  const zoom = d3.zoom().scaleExtent([1, 24]).translateExtent([[0, 0], [W, H]]).on("zoom", e => {
    const k = e.transform.k;
    g.attr("transform", e.transform);
    g.selectAll(".pin").attr("r", 6 / Math.sqrt(k)).attr("stroke-width", 1.6 / k);
    g.selectAll(".country").attr("stroke-width", 0.5 / k);
    g.selectAll(".route").attr("stroke-width", 1.6 / k).attr("stroke-dasharray", `${4 / k} ${3 / k}`);
  });
  svg.call(zoom);

  function centre(points) {
    const lons = points.map(p => p.lon);
    let best = Infinity, at = 0;
    for (let c = -180; c < 180; c += 10) {
      const span = d3.extent(lons, l => ((l - c + 540) % 360) - 180);
      if (lons.length && span[1] - span[0] < best) { best = span[1] - span[0]; at = c; }
    }
    projection.rotate([-at, 0]);
  }
  function showTip(evt, p) {
    const r = box.getBoundingClientRect();
    fill(tip, p.name, h("small", p.here ? "Now" : p.when || ""));
    tip.style.left = `${Math.min(Math.max(evt.clientX - r.left, 80), r.width - 80)}px`;
    tip.style.top = `${evt.clientY - r.top}px`;
    tip.style.opacity = 1;
  }
  const hideTip = () => { tip.style.opacity = 0; };
  function fit(points) {
    if (!points.length) return;
    const xy = points.map(p => projection([p.lon, p.lat]));
    const [x0, x1] = d3.extent(xy, d => d[0]), [y0, y1] = d3.extent(xy, d => d[1]);
    const k = Math.min(8, 0.8 / Math.max((x1 - x0 + 1) / W, (y1 - y0 + 1) / H));
    svg.transition().duration(300).call(zoom.transform,
      d3.zoomIdentity.translate(W / 2, H / 2).scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2));
  }
  const step = f => svg.transition().duration(300).call(zoom.scaleBy, f);

  worldP = worldP || d3.json(WORLD);
  worldP.then(world => {
    const countries = window.topojson.feature(world, world.objects.countries).features;
    const byName = new Map(countries.map(f => [f.properties.name, f]));
    pins = DATA.stops.map(s => {
      if (s.lat != null) return s;
      const f = s.country && byName.get(nat(s.country));
      if (!f) return null;
      const [lon, lat] = d3.geoCentroid(f);
      return { ...s, lon, lat };
    }).filter(Boolean);
    centre(all());
    g.append("g").selectAll("path").data(countries).join("path")
      .attr("class", d => "country" + (ahead.has(d.properties.name) ? " planned" : "")).attr("d", path);
    g.append("g").selectAll("path").data(d3.pairs(all())).join("path").attr("class", "route")
      .attr("d", ([a, b]) => path({ type: "LineString", coordinates: [[a.lon, a.lat], [b.lon, b.lat]] }));
    g.append("g").selectAll("circle").data([...pins, ...here]).join("circle")
      .attr("class", p => "pin" + (p.here ? " here" : ""))
      .attr("cx", p => projection([p.lon, p.lat])[0]).attr("cy", p => projection([p.lon, p.lat])[1]).attr("r", 6)
      .on("pointerenter", showTip).on("pointerleave", hideTip)
      .on("click", (e, p) => { showTip(e, p); setTimeout(hideTip, 2000); })
      .append("title").text(p => `${p.name}${p.here ? " (now)" : p.when ? ` · ${p.when}` : ""}`);
    svg.call(zoom.transform, d3.zoomIdentity);
    fit(all());
  }, () => {
    worldP = null;
    fill(box, errorBox({ message: "Couldn't load the world outline." }, () => drawMap(box, DATA)));
  });
}

// ---- plan

function renderPlan(body, d) {
  fill(body);
  const out = h("div");
  const btn = h("button.btn" + (d && d.text ? "" : ".primary"), { type: "button" }, icon("spark"), d && d.text ? "Make a new plan" : "Make a plan");
  btn.addEventListener("click", () => runSlow({
    target: out, button: btn, label: "Planning routes…", hint: "Reads the itinerary, goals, seasons and budget; usually 30–90 s.",
    call: () => post("/api/plan"),
    render: r => renderPlan(body, r),
  }));
  if (d && d.text) {
    if (d.date) body.append(h("div.faint.mono", { style: { "font-size": "12px", "margin-bottom": "10px" } }, `Made ${short(d.date)}`));
    body.append(clamped(md(d.text), 500));
    if ((d.options || []).length) {
      body.append(h("div.options", d.options.map(o => h("div.option",
        h("b", h("span.num-badge", String((o.index ?? 0) + 1)), o.title), o.summary ? h("div.muted", { style: { "font-size": "14px", "margin-top": "4px" } }, o.summary) : null))));
    }
    const cl = cardList(d.cards);
    if (cl) body.append(cl);
  } else {
    body.append(empty("No plan yet.", "A plan is 2–3 dated route options to pick from, built from the itinerary and goals."));
  }
  body.append(out, h("div.btns", { style: { "margin-top": "12px" } }, btn));
}

// ---- ripple

function initRipple(body) {
  const ta = h("textarea.textarea", { rows: 3, placeholder: "Got the Austin job offer, starts Nov 3", "aria-label": "What changed" });
  const btn = h("button.btn.primary", { type: "submit" }, "Trace it");
  const out = h("div", { style: { "margin-top": "12px" } });
  body.append(
    h("p.muted", { style: { "font-size": "14px", "margin-bottom": "12px" } }, "A decision, a flight, an injury: see what it moves in the itinerary, goals and plan. Nothing changes until you confirm."),
    h("form", { onsubmit: e => {
      e.preventDefault();
      const decision = ta.value.trim();
      if (!decision) { ta.focus(); return; }
      runSlow({
        target: out, button: btn, label: "Tracing what it changes…",
        call: () => post("/api/ripple", { decision }),
        render: r => { out.append(md(r.text), cardList(r.cards) || ""); if (r.cards && r.cards.length) refreshOpenCards(); },
      });
    } }, ta, h("div.compose-row", h("span"), btn)),
    out);
}

// ---- places

function initPlaces(body) {
  const input = h("input.input", { type: "search", placeholder: "Search places, e.g. Uluwatu", "aria-label": "Search places", autocomplete: "off", enterkeyhint: "search" });
  const results = h("div", { "aria-live": "polite", style: { "margin-top": "12px" } }, h("p.faint", { style: { "font-size": "13px" } }, "Everything the vault knows about a place: research, checklist, your notes."));
  let timer = 0, seq = 0;
  const search = () => {
    const q = input.value.trim();
    const mine = ++seq;
    if (q.length < 2) { fill(results); return; }
    fill(results, skeleton());
    api(`/api/places?q=${encodeURIComponent(q)}`).then(d => {
      if (mine !== seq) return;
      const list = d.results || [];
      fill(results, list.length
        ? h("ul.list", list.map(p => h("li", { style: { padding: "0" } }, h("button.li-btn", { type: "button", onclick: () => placeSheet(p.id, p.name) },
          h("div", { style: { padding: "10px 0" } }, h("div.li-main", h("span", p.name), h("span.li-right", words(p.kind))), sub(p.path))))))
        : empty(null, `Nothing matches “${q}”.`));
    }, e => { if (mine === seq) fill(results, errorBox(e, search)); });
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 250); });
  body.append(input, results);
}

function placeSheet(id, name) {
  const body = openSheet(name || "Place", skeleton());
  api(`/api/places/${encodeURIComponent(id)}`).then(p => {
    $("#sheet-title").textContent = p.name || name || "Place";
    const out = h("div.stack-lg");
    out.append(h("div.faint.mono", { style: { "font-size": "12px" } }, [words(p.kind), p.path].filter(Boolean).join(" · ")));
    if (p.description) out.append(md(p.description));
    const section = (title, node) => node && out.append(h("div", h("h3.label", { style: { "margin-bottom": "8px" } }, title), node));
    const listOf = v => {
      if (!v || (Array.isArray(v) && !v.length)) return null;
      if (typeof v === "string") return md(v);
      if (Array.isArray(v)) return h("ul.notes-list", v.map(x => h("li", fmtAny(x))));
      return h("dl.kv", Object.entries(v).flatMap(([k, val]) => [h("dt", words(k)), h("dd", fmtAny(val))]));
    };
    section("Facts", listOf(p.facts));
    section("Checklist", listOf(p.checklist));
    section("My notes", p.my_notes ? (typeof p.my_notes === "string" ? md(p.my_notes) : listOf(p.my_notes)) : null);
    if ((p.children || []).length) {
      section("Inside", h("div.chips", p.children.map(c => h("button.btn", { type: "button", style: { "min-height": "38px" }, onclick: () => placeSheet(c.id, c.name) }, c.name || c.id))));
    }
    if (!p.description && !p.facts && p.card_text) out.append(pre(p.card_text));
    const res = h("div");
    const wbtn = h("button.btn", { type: "button" }, "Add to wishlist");
    const rbtn = h("button.btn", { type: "button" }, "Research it");
    wbtn.addEventListener("click", () => runSlow({ target: res, button: wbtn, label: "Adding…", call: () => post("/api/wishlist", { place: p.name }), render: r => res.append(cardList(r.cards) || okMsg("Added.")) }));
    rbtn.addEventListener("click", () => runSlow({ target: res, button: rbtn, label: `Researching ${p.name}…`, call: () => post("/api/research", { place: p.name }), render: r => res.append(okMsg(r.message || "Done."), cardList(r.cards) || "") }));
    out.append(h("div.btns", wbtn, rbtn), res);
    fill(body, out);
  }, e => fill(body, errorBox(e, () => placeSheet(id, name))));
}

// ---- wishlist

function initWishlist(body) {
  const input = h("input.input", { placeholder: "A place you want to go", "aria-label": "Add a place to the wishlist" });
  const btn = h("button.btn.primary", { type: "submit" }, "Add");
  const out = h("div");
  const list = h("div", { style: { "margin-top": "16px" } });
  const form = h("form.row", { onsubmit: e => {
    e.preventDefault();
    const place = input.value.trim();
    if (!place) { input.focus(); return; }
    runSlow({ target: out, button: btn, label: `Adding ${place}…`, call: () => post("/api/wishlist", { place }), render: r => {
      out.append(cardList(r.cards) || okMsg("Added."));
      input.value = "";
      if (r.cards && r.cards.length) refreshOpenCards();
    } });
  } }, input, btn);
  body.append(form, out, list);
  const reload = () => load(list, () => api("/api/wishlist"), d => {
    const entries = d.entries || [];
    if (!entries.length) { list.append(empty("The wishlist is empty.", "Add somewhere above, or say “I want to go to …” in a note.")); return; }
    list.append(h("ul.list", entries.map(e => {
      const res = h("div");
      const rb = !e.research ? h("button.btn.ghost", { type: "button", style: { "min-height": "36px", "margin-left": "-12px" } }, "Research") : null;
      if (rb) rb.addEventListener("click", () => runSlow({ target: res, button: rb, label: `Researching ${e.place}…`, call: () => post("/api/research", { place: e.place }), render: r => res.append(okMsg(r.message || "Done."), cardList(r.cards) || "") }));
      return li(e.place, e.date ? short(e.date) : "",
        sub(e.why),
        h("div.row.wrap", { style: { "margin-top": "6px", gap: "6px" } },
          e.status ? h("span.chip" + (e.status === "planned" ? ".accent" : ""), e.status) : null,
          e.type ? h("span.chip", e.type) : null,
          e.research ? h("span.chip", "researched") : null, rb),
        res);
    })));
  });
  reload();
}

// ---- trail

function initTrail(c) {
  let days = "30";
  const box = h("div");
  const reload = () => load(box, () => api(`/api/trail?days=${days}`), d => {
    const stays = d.stays || [];
    if (stays.length) box.append(h("ul.list", stays.map(s => li(s.place, s.when, sub(s.did)))));
    else if (d.text) box.append(pre(d.text));
    else box.append(empty("No stays logged.", "Your location history builds up as you move."));
  });
  c.head.append(seg([["30", "30 days"], ["all", "All"]], days, v => { days = v; reload(); }, "Time range"));
  c.body.append(box);
  reload();
}

// ================================================================ YOU

function initYou() {
  const root = $("#you");
  const disc = card(1, "Disciplines", { cls: "wide" });
  const review = card(2, "Weekly review");
  const goals = card(3, "Goals");
  const reports = card(4, "Reports");
  const ranks = card(5, "Rankings");
  const miles = card(6, "Milestones");
  const changes = card(7, "Recent changes");
  const account = card(8, "Session");
  fill(root, h("div.grid", disc, h("div.col", review, goals, ranks), h("div.col", reports, miles, changes, account)));

  initDisciplines(disc);
  load(review.body, () => api("/api/review"), d => renderReview(review.body, d));
  fill(goals.body, skeleton());
  fill(miles.body, skeleton());
  const fromHome = force => getHome(force).then(d => {
    renderGoals(goals.body, d.goals, true);
    renderMilestones(miles.body, d.milestones);
  }, e => { fill(goals.body, errorBox(e, () => fromHome(true))); fill(miles.body); });
  fromHome(false);
  initReports(reports);
  initRankings(ranks.body);
  loadChanges(changes.body);
  const notify = h("div", { style: { "margin-top": "12px" } });
  account.body.append(
    h("p.muted", { style: { "font-size": "14px" } }, `Signed in on this device${S.tz ? ` · times in ${S.tz}` : ""}.`),
    notify,
    h("form", { method: "post", action: "/logout", style: { "margin-top": "12px" } }, h("button.btn", { type: "submit" }, "Sign out")));
  initNotifications(notify);
}

// The morning brief on the lock screen (push.py). iOS allows it only for the
// Home Screen app, and permission must be asked from a tap.
function initNotifications(box) {
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  if (!supported) {
    fill(box, h("p.muted", { style: { "font-size": "14px" } }, "Add daylog to your Home Screen to get the brief as a notification."));
    return;
  }
  const show = async () => {
    let st;
    try { st = await api("/api/push"); } catch { fill(box); return; }
    if (!st.configured) { fill(box); return; }
    const sub = await navigator.serviceWorker.ready.then(r => r.pushManager.getSubscription()).catch(() => null);
    if (sub && Notification.permission === "granted") {
      fill(box, h("div.row", h("span.muted", { style: { "font-size": "14px" } }, "Brief notifications on."),
        h("button.btn.ghost", { type: "button", onclick: () => post("/api/push/test", {}, NORMAL).then(r => announce(r.ok ? "Sent." : "Not sent.")) }, "Send a test")));
      return;
    }
    fill(box, h("button.btn", { type: "button", onclick: () => turnOn(st.publicKey) }, "Turn on brief notifications"));
  };
  const turnOn = async key => {
    if (await Notification.requestPermission() !== "granted") { announce("Notifications are off in Settings."); return; }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64urlBytes(key) });
    await post("/api/push/subscribe", { subscription: sub.toJSON() }, NORMAL);
    show();
  };
  show();
}

function b64urlBytes(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

function renderReview(body, d) {
  fill(body);
  const out = h("div");
  const btn = h("button.btn" + (d && d.text ? "" : ".primary"), { type: "button" }, icon("spark"), "Write this week's review");
  btn.addEventListener("click", () => runSlow({
    target: out, button: btn, label: "Writing the weekly review…", hint: "About a minute.",
    call: () => post("/api/review"), render: r => renderReview(body, r),
  }));
  if (d && d.text) {
    if (d.week) body.append(h("div.faint.mono", { style: { "font-size": "12px", "margin-bottom": "10px" } }, `Week of ${short(d.week) || d.week}`));
    body.append(clamped(md(d.text), 700));
  } else body.append(empty("No review yet.", "Reviews are written on Sundays; you can write this week's now."));
  body.append(out, h("div.btns", { style: { "margin-top": "12px" } }, btn));
}

// ---- disciplines (ported from dashboard.py _DISC_JS)

const STATES = {
  active: "Pursuing", "not started": "Pursuing · nothing logged yet",
  ready: "Want · prerequisites met", locked: "Want · locked",
};

function initDisciplines(c) {
  let view = "graph", days = "30", data = null;
  const content = h("div");
  const legend = h("div");
  const draw = () => {
    fill(content);
    if (!data) return;
    if (view === "graph") drawDiscCard(content, data);
    else content.append(discList(data));
  };
  const reload = () => load(content, () => api(`/api/disciplines?days=${days}`), d => {
    data = d;
    const roots = (d.nodes || []).filter(n => !n.parent);
    fill(legend, 
      h("div.legend", roots.map(r => h("span", h("i", { style: { background: `var(--root-${(d.slots || {})[r.id] || 1})` } }), r.name))),
      h("div.legend", h("span", h("i.glyph.full"), "Pursuing"), h("span", h("i.glyph"), "Not started"),
        h("span", h("i.glyph.ready"), "Want, ready"), h("span", h("i.glyph.locked"), "Locked"), h("span", h("i.glyph.line"), "Also counts under")));
    if (!(d.nodes || []).length) { content.append(empty("No disciplines yet.", "Sports you log join the graph once confirmed.")); return; }
    draw();
  }, "block");
  c.head.append(seg([["graph", "Graph"], ["list", "List"]], view, v => { view = v; draw(); }, "View"));
  c.body.append(
    h("div.row.wrap", { style: { "margin-bottom": "12px" } }, seg([["30", "30 days"], ["90", "90 days"], ["365", "Year"]], days, v => { days = v; reload(); }, "Recent window")),
    legend, content);
  reload();
}

function drawDiscCard(content, GD) {
  if (!window.d3) { content.append(errorBox({ message: "The graph library didn't load. Switch to List." })); return; }
  const box = h("div.viz.disc");
  content.append(box, h("p.cal-key", "Size is all-time sessions. Tap a sport for its numbers, a root to zoom in."));
  drawDisc(box, GD);
}

function drawDisc(box, GD) {
  const d3 = window.d3;
  const S0 = 1000, R = 320, R0 = 90;
  const svg = d3.create("svg").attr("viewBox", `${-S0 / 2} ${-S0 / 2} ${S0} ${S0}`).attr("role", "img")
    .attr("aria-label", "The discipline graph: sports under their roots, sized by sessions")
    .style("height", "min(92vw, 680px)");
  const g = svg.append("g");
  const tip = h("div.tip");
  const btn = (name, label, fn) => h("button", { type: "button", "aria-label": label, onclick: fn }, icon(name));
  fill(box, svg.node(), tip, h("div.viz-tools",
    btn("plus", "Zoom in", () => svg.transition().call(zoom.scaleBy, 1.6)),
    btn("minus", "Zoom out", () => svg.transition().call(zoom.scaleBy, 1 / 1.6)),
    btn("fit", "Fit", () => svg.transition().call(zoom.transform, d3.zoomIdentity))));
  box.querySelectorAll(".viz-tools svg").forEach(s => { s.style.cssText = "width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round"; });

  const byId = new Map(GD.nodes.map(n => [n.id, n]));
  const rows = [{ id: "_", parent: null }, ...GD.nodes.map(n => ({ ...n, parent: n.parent && byId.has(n.parent) ? n.parent : "_" }))];
  let tree;
  try { tree = d3.stratify().id(d => d.id).parentId(d => d.parent)(rows); }
  catch { fill(box, errorBox({ message: "The graph has a loop or a missing parent; use the List view." })); return; }
  d3.cluster().size([2 * Math.PI, R]).separation((a, b) => (a.parent === b.parent ? 1 : 1.6))(tree);
  const ring = (R - R0) / Math.max(1, tree.height - 1);
  tree.each(d => { d.y = d.depth === 0 ? 0 : d.height === 0 ? R : R0 + (d.depth - 1) * ring; });
  const at = new Map(); tree.each(d => at.set(d.id, d));
  const xy = d => [Math.sin(d.x) * d.y, -Math.cos(d.x) * d.y];
  const colour = id => `var(--root-${(GD.slots || {})[id] || 1})`;
  const radius = n => n.parent === null ? 11 : n.total ? Math.min(14, 4 + Math.sqrt(n.total) * 1.6) : 4.5;
  const filled = n => n.state === "active" || n.state == null;
  const dashed = n => n.state === "ready" || n.state === "locked";

  // Labels read at ~11px whatever the card's width.
  const shown = Math.min(box.clientWidth || 600, Math.min(window.innerWidth * 0.92, 680));
  const fs = Math.max(13, Math.min(22, 11 * S0 / shown));

  g.append("g").selectAll("path").data(tree.links()).join("path")
    .attr("class", l => "dlink" + (l.source.id === "_" ? " hub" : ""))
    .attr("stroke", l => colour(l.target.data.root))
    .attr("d", d3.linkRadial().angle(d => d.x).radius(d => d.y));
  const extra = GD.nodes.flatMap(n => (n.also || []).filter(p => at.has(p)).map(p => [n, p]));
  g.append("g").selectAll("path").data(extra).join("path").attr("class", "dalso")
    .attr("stroke", ([, p]) => colour(byId.get(p).root))
    .attr("d", ([n, p]) => {
      const [x1, y1] = xy(at.get(n.id)), [x2, y2] = xy(at.get(p));
      return `M${x1},${y1}Q${(x1 + x2) * 0.3},${(y1 + y2) * 0.3} ${x2},${y2}`;
    });
  const nodes = tree.descendants().filter(d => d.id !== "_");
  g.append("circle").attr("r", 5).attr("fill", "var(--text)");
  g.append("g").selectAll("circle").data(nodes).join("circle").attr("class", "dnode")
    .attr("cx", d => xy(d)[0]).attr("cy", d => xy(d)[1]).attr("r", d => radius(d.data))
    .attr("fill", d => filled(d.data) ? colour(d.data.root) : "var(--bg)")
    .attr("stroke", d => filled(d.data) ? "var(--bg)" : d.data.state === "locked" ? "var(--faint)" : colour(d.data.root))
    .attr("stroke-width", d => filled(d.data) ? 2 : 2.2)
    .attr("stroke-dasharray", d => dashed(d.data) ? "3 2" : null)
    .on("pointerenter", show).on("pointerleave", hide)
    .on("click", (e, d) => { e.stopPropagation(); show(e, d); if (d.depth === 1) zoomTo(d); });
  // Bigger invisible hit targets than the dots.
  g.append("g").selectAll("circle").data(nodes).join("circle")
    .attr("cx", d => xy(d)[0]).attr("cy", d => xy(d)[1]).attr("r", d => radius(d.data) + 8)
    .attr("fill", "transparent").style("cursor", "pointer")
    .on("pointerenter", show).on("pointerleave", hide)
    .on("click", (e, d) => { e.stopPropagation(); show(e, d); if (d.depth === 1) zoomTo(d); });

  g.append("g").selectAll("text").data(nodes).join("text")
    .attr("class", d => "dlabel" + (d.depth === 1 ? " root" : "") + (d.data.state === "locked" ? " locked" : ""))
    .attr("font-size", d => d.depth === 1 ? fs * 1.55 : fs)
    .attr("stroke-width", fs * 0.28)
    .attr("dy", "0.32em")
    .attr("transform", d => {
      if (d.height > 0) {
        const [x, y] = xy(d);
        return `translate(${x},${y - radius(d.data) - (d.depth === 1 ? fs * 1.1 : fs * 0.75)})`;
      }
      const flip = d.x >= Math.PI;
      return `rotate(${d.x * 180 / Math.PI - 90}) translate(${d.y},0)` + (flip ? " rotate(180)" : "");
    })
    .attr("x", d => d.height > 0 ? 0 : (d.x < Math.PI ? 1 : -1) * (radius(d.data) + 5))
    .attr("text-anchor", d => d.height > 0 ? "middle" : d.x < Math.PI ? "start" : "end")
    .text(d => d.data.name);

  function show(evt, d) {
    const n = d.data, r = box.getBoundingClientRect();
    const kids = [h("div", n.name)];
    const path = d.ancestors().slice(1, -1).reverse().map(a => a.data.name);
    if (path.length) kids.push(h("small", path.join(" › ") + ((n.also || []).length ? " · also " + n.also.map(p => byId.get(p)?.name || p).join(", ") : "")));
    if (n.state) kids.push(h("small", STATES[n.state] || n.state));
    kids.push(h("small", `${n.recent} in ${GD.days} days${n.recent_hours ? ` · ${n.recent_hours}h` : ""} · ${n.total} all-time${n.last ? ` · last ${short(n.last)}` : ""}`));
    (n.goals || []).forEach(t => kids.push(h("small", "Goal: " + t)));
    if ((n.needs || []).length) kids.push(h("small", "Needs: " + n.needs.join(", ")));
    fill(tip, ...kids);
    tip.style.left = `${Math.min(Math.max(evt.clientX - r.left, 110), r.width - 110)}px`;
    tip.style.top = `${Math.max(evt.clientY - r.top, 90)}px`;
    tip.style.opacity = 1;
  }
  function hide() { tip.style.opacity = 0; }
  svg.on("click", hide);

  const zoom = d3.zoom().scaleExtent([1, 8]).on("zoom", e => g.attr("transform", e.transform));
  svg.call(zoom);
  function zoomTo(d) {
    const pts = d.descendants().map(xy);
    const [x0, x1] = d3.extent(pts, p => p[0]), [y0, y1] = d3.extent(pts, p => p[1]);
    const padd = 130, k = Math.min(8, S0 / Math.max(x1 - x0 + 2 * padd, y1 - y0 + 2 * padd));
    svg.transition().duration(400).call(zoom.transform, d3.zoomIdentity.scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2));
  }
}

function discList(GD) {
  const children = new Map();
  for (const n of GD.nodes) {
    const p = n.parent || null;
    if (!children.has(p)) children.set(p, []);
    children.get(p).push(n);
  }
  const items = [];
  const walk = (parent, depth) => {
    for (const n of children.get(parent) || []) {
      let state = n.state && n.state !== "active" ? n.state : "";
      if (n.state === "locked" && (n.needs || []).length) state = "locked: needs " + n.needs.join(", ");
      const extra = [state, ...(n.goals || []).map(t => "goal: " + t)].filter(Boolean).join(" · ");
      items.push(h("li", { style: { "padding-left": `${depth * 18}px` } },
        h("div.li-main",
          h("span", { style: depth === 0 ? { "font-weight": "600" } : null }, h("span.swatch", { style: { background: `var(--root-${(GD.slots || {})[n.root] || 1})` } }), n.name),
          h("span.li-right", `${n.recent} in ${GD.days}d · ${n.total} all`)),
        extra ? h("div.li-sub", { style: { "padding-left": "16px" } }, extra) : null));
      walk(n.id, depth + 1);
    }
  };
  walk(null, 0);
  return h("ul.list.disc-list", items);
}

// ---- conditions: rated surf per spot and flyability per site, drawn

// Rating 0–5 on one blue ordinal ramp (validated, light and dark: --rate-1..5);
// flat and missing are neutral. Text on a chip stays in ink, never the hue.
const rateStep = r => (r == null ? 0 : Math.min(5, Math.max(1, Math.ceil(r + 0.001))));
const dayName = (s, i) => (i === 0 ? "Today" : i === 1 ? "Tomorrow" : `${weekday(s, 3)} ${parseD(s)?.getDate() ?? ""}`);
const hrs = (a, b) => `${pad(a)}–${pad(b)}`;

function windArrow(from, kn) {
  // Points the way the wind blows. The drawn arrow points south (180°), so
  // turning it by the "from" bearing makes it point to from + 180°.
  const s = document.createElementNS(NS, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("class", "wind-arrow");
  s.setAttribute("aria-hidden", "true");
  s.style.transform = `rotate(${Number(from) || 0}deg)`;
  s.innerHTML = kn < 3 ? '<circle cx="12" cy="12" r="3.5"/>' : '<path d="M12 4v16M6.5 14.5 12 20l5.5-5.5"/>';
  return s;
}

function hourStrip(hours, value, hot, title) {
  // One bar per hour; the window (hot) at full strength, the rest muted.
  const s = document.createElementNS(NS, "svg");
  const n = hours.length || 1, w = 100 / n;
  s.setAttribute("viewBox", "0 0 100 22");
  s.setAttribute("preserveAspectRatio", "none");
  s.setAttribute("class", "hour-strip");
  s.setAttribute("role", "img");
  s.setAttribute("aria-label", title);
  hours.forEach((hr, i) => {
    const v = value(hr);
    const r = document.createElementNS(NS, "rect");
    const ht = Math.max(2, v * 20);
    r.setAttribute("x", String(i * w + 0.6));
    r.setAttribute("width", String(w - 1.2));
    r.setAttribute("y", String(22 - ht));
    r.setAttribute("height", String(ht));
    r.setAttribute("rx", "1");
    r.setAttribute("class", hot(hr) ? "hot" : "");
    r.style.fill = `var(--rate-${rateStep(hr.rating ?? (hr.ok ? 4 : 1))})`;
    if (hr.ok === false) r.style.fill = "var(--line-strong)";
    const t = document.createElementNS(NS, "title");
    t.textContent = `${pad(hr.hour)}:00 · ${hr.rating != null ? hr.rating.toFixed(1) : hr.ok ? "flyable" : "not flyable"}`;
    r.append(t);
    s.append(r);
  });
  return h("div.strip-wrap", s, h("div.strip-axis", h("span", `${pad(hours[0]?.hour ?? 6)}`), h("span", "12"), h("span", `${pad((hours.at(-1)?.hour ?? 18) + 1)}`)));
}

function surfRow(spot, day) {
  const flat = day.label === "FLAT";
  const step = flat ? 0 : rateStep(day.rating);
  const chip = h("div.rate-chip", { style: { "--chip": `var(--rate-${step})`, "--chip-ink": `var(--rate-ink-${step})` } },
    h("b", flat ? "–" : day.rating.toFixed(1)), h("span", day.label));
  const wind = day.wind_kn < 3 ? "glassy" : `${day.wind_kn} kn ${day.wind_dir}${day.offshore ? " · offshore" : ""}`;
  const facts = h("div.cond-facts",
    h("span", `${day.faces[0]}–${day.faces[1]} m`),
    h("span", `${day.period}s ${day.swell_dir}`),
    h("span.wind", windArrow(day.wind_from, day.wind_kn), wind),
    h("span", `${day.tide} tide ${day.tide_trend === "rising" ? "↑" : "↓"}`));
  const notes = [];
  if (day.above_comfort) notes.push(h("span.flag", "⚠ above your comfort size"));
  if (day.drops_to) notes.push(h("span.faint", `${day.drops_to.label.toLowerCase()} by ${pad(day.drops_to.hour)}:00`));
  return h("li.cond-row", chip,
    h("div.cond-main",
      h("div.row.between", h("b.cond-name", spot.name), h("span.mono.cond-when", hrs(day.start, day.end))),
      facts,
      notes.length ? h("div.cond-notes", notes) : null,
      hourStrip(day.hours, hr => hr.rating / 5, hr => hr.hour >= day.start && hr.hour < day.end, `${spot.name} hourly rating`)));
}

function flyRow(site, day) {
  const pill = day.flyable
    ? h("span.status.good", icon("check"), `Flyable ${hrs(day.start, day.end)}`)
    : h("span.status.bad", "✕ Not flyable");
  const facts = day.flyable
    ? h("div.cond-facts",
        h("span.wind", windArrow(day.wind_from, day.wind_kn), `${day.wind_kn} kn ${day.wind_dir} aloft`),
        h("span", `gusts ${day.gust_kn} kn`),
        h("span", `thermals ${day.thermals}`),
        h("span", day.headroom_m > -500 ? `base ~${day.base_m.toLocaleString()} m (${day.headroom_m >= 0 ? "+" : ""}${day.headroom_m} over launch)` : "base below launch"))
    : h("div.cond-facts", (day.blockers || []).map(b => h("span", b)));
  return h("li.cond-row.fly",
    h("div.cond-main",
      h("div.row.between", h("b.cond-name", site.name), pill),
      facts,
      hourStrip(day.hours, hr => (hr.ok ? 1 : 0.25), hr => hr.ok, `${site.name} flyable hours`)));
}

function conditionsView(list, kind) {
  // Day tabs; under each, every spot ranked best first.
  const dates = [...new Set(list.flatMap(s => s.days.map(d => d.date)))].sort();
  if (!dates.length) return empty(null, "No forecast came back.");
  const box = h("div");
  const show = date => {
    const rows = list
      .map(s => ({ s, d: s.days.find(x => x.date === date) }))
      .filter(x => x.d)
      .sort((a, b) => kind === "surf" ? b.d.rating - a.d.rating : (b.d.flyable_hours || 0) - (a.d.flyable_hours || 0));
    const top = rows[0];
    const best = kind === "surf" && top && top.d.label !== "FLAT"
      ? h("div.best-bet", h("span.label", "Best bet"), h("b", `${top.s.name} · ${top.d.label.toLowerCase()} ${hrs(top.d.start, top.d.end)}`), h("span.muted", ` ${top.d.faces[0]}–${top.d.faces[1]} m`))
      : null;
    fill(box, best, h("ul.list.cond-list", rows.map(x => (kind === "surf" ? surfRow(x.s, x.d) : flyRow(x.s, x.d)))));
  };
  const tabs = seg(dates.map((d, i) => [d, dayName(d, i)]), dates[0], show, "Day");
  show(dates[0]);
  const legend = kind === "surf"
    ? h("div.rate-legend", h("span.faint", "Rating"), [1, 2, 3, 4, 5].map(n => h("i", { style: { background: `var(--rate-${n})` } })), h("span.faint", "0 → 5 against each spot's own best"))
    : null;
  return h("div", h("div.cond-tabs", tabs), legend, box);
}

function initConditions(c) {
  const box = h("div", { "aria-live": "polite" });
  const cache = {};
  const SRC = { surf: ["/api/surf", "spots"], fly: ["/api/fly", "sites"] };
  let cur = "surf";
  const show = (key, force) => {
    cur = key;
    if (cache[key] && !force) { fill(box, cache[key]); return; }
    load(box, () => api(SRC[key][0], { timeout: SLOW }), d => {
      const list = d[SRC[key][1]] || [];
      const node = list.length ? conditionsView(list, key) : empty(null, d.message || "Nothing to show.");
      cache[key] = h("div", node);
      if (cur === key) fill(box, cache[key]);
    });
  };
  c.head.append(seg([["surf", "Surf"], ["fly", "Fly"]], cur, k => show(k), "Conditions"));
  c.body.append(box, h("p.faint.cond-foot", "Open-Meteo model data, rated against each spot's profile and your own rules. Guidance only."));
  show(cur);
}

// ---- reports: usage, site

function usageView(d) {
  const pct = d.budget ? Math.min(100, (d.spent / d.budget) * 100) : 0;
  const max = Math.max(...(d.by_kind || []).map(k => k.usd), 0.01);
  return h("div",
    h("div.row.between", h("span", h("b.big-num", `$${d.spent.toFixed(2)}`), h("span.muted", ` of $${Number(d.budget).toFixed(0)} · ${d.month}`)), h("span.mono.faint", `${pct.toFixed(0)}%`)),
    h("div.meter", { role: "meter", "aria-valuenow": String(d.spent), "aria-valuemin": "0", "aria-valuemax": String(d.budget), "aria-label": "API spend this month" }, h("div", { style: { width: `${pct}%` }, class: pct >= 90 ? "over" : "" })),
    h("ul.list.spend", (d.by_kind || []).map(k => h("li.row.between",
      h("span", k.kind),
      h("span.kv-bar", h("i", { style: { width: `${(k.usd / max) * 100}%` } })),
      h("span.mono", `$${k.usd.toFixed(2)}`)))));
}

function initReports(c) {
  const box = h("div", { "aria-live": "polite" });
  const cache = {};
  let cur = "usage";
  const show = (key, force) => {
    cur = key;
    if (cache[key] && !force) { fill(box, cache[key]); return; }
    const path = key === "usage" ? "/api/usage" : "/api/site?days=7";
    load(box, () => api(path), d => {
      let node;
      if (key === "usage") node = usageView(d);
      else if (d.text == null) node = empty(null, d.message ? `Not available: ${d.message}.` : "Nothing to show.");
      else node = pre(d.text);
      cache[key] = h("div", node, h("button.btn.ghost", { type: "button", style: { "margin": "8px 0 -8px -12px" }, onclick: () => show(cur, true) }, "Refresh"));
      if (cur === key) fill(box, cache[key]);
    });
  };
  c.body.append(h("div", { style: { "margin-bottom": "12px" } }, seg([["usage", "API spend"], ["site", "Site"]], cur, k => show(k), "Report")), box);
  show(cur);
}

// ---- rankings

function initRankings(body) {
  let current = null, data = null;
  const chips = h("div");
  const box = h("div", { style: { "margin-top": "12px" } });
  const render = () => {
    const tiers = (data.lists || {})[current];
    fill(box);
    if (!tiers) {
      load(box, () => api(`/api/rankings?category=${encodeURIComponent(current)}`), d => {
        data.lists = { ...(data.lists || {}), ...(d.lists || {}) };
        render();
      });
      return;
    }
    if (!tiers.length) { box.append(empty(null, "Nothing ranked in this category yet.")); return; }
    box.append(h("div", tiers.map(t => h("div.tier", h("b", t.tier), h("div", (t.items || []).join(" · "))))));
  };
  load(box, () => api("/api/rankings"), d => {
    data = d;
    const cats = d.categories || Object.keys(d.lists || {});
    if (!cats.length) { box.append(empty("Nothing ranked yet.", "Rank places you've been to build tiers.")); return; }
    current = cats[0];
    fill(chips, seg(cats.map(c => [c, words(c)]), current, v => { current = v; render(); }, "Category", true));
    render();
  });
  body.append(chips, box);
}

// ---- recent changes with undo

function loadChanges(body) {
  load(body, () => api("/api/changes"), d => {
    const list = d.changes || [];
    if (!list.length) { body.append(empty(null, "No recent changes.")); return; }
    body.append(h("p.muted", { style: { "font-size": "13.5px", "margin-bottom": "8px" } }, "What daylog wrote to the vault. Undo reverts one change."),
      h("ul.list", list.map(ch => {
        const res = h("div");
        const btn = h("button.btn.ghost.danger", { type: "button", "aria-label": `Undo: ${ch.subject}` }, icon("undo"), "Undo");
        btn.addEventListener("click", async () => {
          const ok = await confirmSheet("Undo this change?", `**${ch.subject}**\n\nCommit ${String(ch.sha).slice(0, 7)}. This reverts it in the vault; the revert itself shows up here and can be undone too.`, "Revert", true);
          if (!ok) return;
          runSlow({
            target: res, button: btn, label: "Reverting…", call: () => post(`/api/changes/${encodeURIComponent(ch.sha)}/revert`, {}, NORMAL),
            render: r => { res.append(okMsg(r.message || "Reverted.")); S.homeStale = true; setTimeout(() => loadChanges(body), 1800); },
          });
        });
        return h("li", h("div.change", h("code", String(ch.sha).slice(0, 7)), h("span.subj", ch.subject), btn), res);
      })));
  });
}

// ================================================================ router & shell

const VIEWS = { home: initHome, log: initLog, chat: initChat, trips: initTrips, you: initYou };
const TITLES = { home: "Home", log: "Log", chat: "Assistant", trips: "Trips", you: "You" };
const ON_SHOW = {
  home: () => { if (S.homeStale) { S.homeStale = false; loadHome(true); } },
  chat: () => scrollChat(false),
};
const inited = new Set();
const scrolls = {};
let current = null;

function route() {
  const name = (/^#\/([a-z]+)/.exec(location.hash) || [])[1];
  const tab = name in VIEWS ? name : "home";
  if (!name) history.replaceState(null, "", "#/home");
  if (current === tab) return;
  if (current) scrolls[current] = window.scrollY;
  document.querySelectorAll(".view").forEach(v => { v.hidden = v.dataset.view !== tab; });
  document.querySelectorAll(".tab").forEach(a => {
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  document.title = `${TITLES[tab]} · daylog`;
  current = tab;
  const first = !inited.has(tab);
  if (first) { inited.add(tab); VIEWS[tab](); } else if (ON_SHOW[tab]) ON_SHOW[tab]();
  if (tab !== "chat") window.scrollTo(0, scrolls[tab] || 0);
  updateBar();
}

function updateBar() {
  const bar = document.querySelector(".view:not([hidden]) .bar");
  if (bar) bar.classList.toggle("scrolled", window.scrollY > 4);
}

document.addEventListener("click", e => {
  const el = e.target.closest("[data-action]");
  if (!el) return;
  const a = el.dataset.action;
  if (a === "sheet-close") sheet.close();
  else if (a === "refresh-home") loadHome(true);
  else if (a === "chat-reset") resetChat();
});

// On phones, the tab bar gives way to the keyboard while typing.
const coarse = matchMedia("(pointer: coarse)");
document.addEventListener("focusin", e => {
  if (coarse.matches && e.target.matches("textarea, input:not([type=checkbox]):not([type=radio])")) document.body.classList.add("kb");
});
document.addEventListener("focusout", () => {
  setTimeout(() => { if (!document.activeElement || !document.activeElement.matches("textarea, input")) document.body.classList.remove("kb"); }, 80);
});

window.addEventListener("scroll", updateBar, { passive: true });
window.addEventListener("hashchange", route);

async function start() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  try {
    const me = await api("/api/me");
    S.today = me.today || null;
    S.tz = me.tz || null;
  } catch (e) {
    if (e.status === 401) return; // sign-in screen is showing
    // Carry on; each card shows its own error.
  }
  route();
  if (current !== "home") refreshOpenCards();
}
start();
