"use strict";
// Phil dashboard client. Vanilla JS + inline SVG, no dependencies.
// Every dynamic string (market questions, commit subjects, retros, logs) is
// untrusted and reaches the DOM only through text nodes, never innerHTML.

const STATE_EVERY_MS = 30000;
const MTM_EVERY_MS = 60000;
const SVGNS = "http://www.w3.org/2000/svg";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const S = {
  state: null, mtm: null, fetchError: null, lastFetch: 0,
  tradeFilter: "all", edgeGroup: "edge_class",
  tableView: { pnl: false, skill: false, calib: false },
};

// --- DOM builders -------------------------------------------------------------

function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function s(tag, attrs, ...kids) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    node.setAttribute(k, typeof v === "number" ? +v.toFixed(2) : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

// --- formatting ----------------------------------------------------------------

const MINUS = "−";
const nf = (dp) => new Intl.NumberFormat("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });

function usd(v, { sign = false, dp = 2 } = {}) {
  if (v == null || Number.isNaN(v)) return "—";
  const body = "$" + nf(dp).format(Math.abs(v));
  if (v < 0) return MINUS + body;
  return (sign && v > 0 ? "+" : "") + body;
}
function signed(v, dp = 4) {
  if (v == null || Number.isNaN(v)) return "—";
  if (v === 0) return nf(dp).format(0);
  return (v > 0 ? "+" : MINUS) + nf(dp).format(Math.abs(v));
}
function pct(v, dp = 1) {
  return v == null || Number.isNaN(v) ? "—" : nf(dp).format(v * 100) + "%";
}
function prob(v) {
  return v == null ? "—" : nf(2).format(v);
}
function parseT(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}
function when(iso) {
  const t = parseT(iso);
  if (t == null) return "—";
  const d = new Date(t);
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" }) + "Z";
}
function day(iso) {
  const t = parseT(iso);
  return t == null ? "—" : new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}
function ago(iso, now = Date.now()) {
  const t = parseT(iso);
  if (t == null) return "—";
  const secs = Math.round((now - t) / 1000);
  const future = secs < 0;
  const a = Math.abs(secs);
  const txt = a < 60 ? `${a}s` : a < 3600 ? `${Math.round(a / 60)}m` : a < 172800 ? `${Math.round(a / 3600)}h` : `${Math.round(a / 86400)}d`;
  return future ? `in ${txt}` : `${txt} ago`;
}
function duration(sec) {
  if (sec == null) return "—";
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), r = sec % 60;
  return m < 60 ? `${m}m ${String(r).padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}
const tone = (v, lowerIsBetter = false) =>
  v == null || v === 0 ? "" : (v < 0) === lowerIsBetter ? "good" : "bad";
const KIND = { hourly: "hourly cycle", triggered: "triggered cycle", "deep-retro": "deep retro", watch: "watch check" };

function statusEl(cls, label, extra) {
  return h("span", { class: `status-line ${cls}` }, h("i", { class: "status-icon", "aria-hidden": "true" }), label, extra);
}

function marketLink(row) {
  const text = row.question || row.market_id || "market";
  if (!row.slug) return document.createTextNode(text);
  return h("a", { href: `https://polymarket.com/market/${encodeURIComponent(row.slug)}`, target: "_blank", rel: "noopener noreferrer" }, text);
}

// --- tooltip ----------------------------------------------------------------------

const tip = () => $("#tooltip");
function showTip(x, y, title, rows, note) {
  const t = tip();
  t.replaceChildren(
    h("div", { class: "tt-title" }, title),
    ...rows.map((r) => h("div", { class: "tt-row" }, r.key ? h("i", { class: `tt-key ${r.key}` }) : null, h("strong", {}, r.value), h("span", { class: "muted" }, r.label))),
    note ? h("div", { class: "tt-note" }, note) : null,
  );
  t.hidden = false;
  const pad = 14, bw = t.offsetWidth, bh = t.offsetHeight;
  let left = x + pad, top = y + pad;
  if (left + bw > window.innerWidth - 8) left = x - bw - pad;
  if (top + bh > window.innerHeight - 8) top = y - bh - pad;
  t.style.left = Math.max(8, left) + "px";
  t.style.top = Math.max(8, top) + "px";
}
function hideTip() { tip().hidden = true; }

// --- chart helpers -------------------------------------------------------------------

function niceStep(span, count) {
  const raw = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
}
function niceTicks(lo, hi, count = 5) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const step = niceStep(hi - lo, count);
  const start = Math.floor(lo / step) * step, end = Math.ceil(hi / step) * step;
  const out = [];
  for (let v = start; v <= end + step / 2; v += step) out.push(+v.toFixed(10));
  return out;
}
function intTicks(lo, hi, maxTicks) {
  const step = Math.max(1, Math.ceil(niceStep(hi - lo, Math.max(2, maxTicks))));
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}
const HOUR = 3600e3, DAY = 24 * HOUR;
function timeTicks(lo, hi, maxTicks) {
  const steps = [HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY, 60 * DAY];
  const step = steps.find((st) => (hi - lo) / st <= Math.max(2, maxTicks)) || steps[steps.length - 1];
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return { ticks: out, step };
}
function timeLabel(v, step) {
  const d = new Date(v);
  if (step >= DAY) return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  const hm = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" });
  return d.getUTCHours() === 0 ? d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) : hm;
}
function emptyState(text) { return h("div", { class: "empty" }, text); }

function hostWidth(host) {
  return Math.max(280, Math.floor(host.getBoundingClientRect().width || host.clientWidth || 600));
}

/**
 * Single-series line chart with a crosshair tooltip.
 * o: {points, x, y, xType: "time"|"index", yFmt, baseline, step, wash, dimCount,
 *     extendTo, zones: {above, below}, endLabel, ariaLabel, tip(point) -> {title, rows, note}}
 */
function lineChart(host, o) {
  host.replaceChildren();
  if (!o.points.length) { host.append(emptyState(o.empty)); return; }
  const W = hostWidth(host), H = o.height || 250;
  const M = { t: 14, r: o.endLabel ? 70 : 18, b: 28, l: 58 };
  const xs = o.points.map(o.x), ys = o.points.map(o.y);
  let x0 = Math.min(...xs), x1 = Math.max(...xs, o.extendTo ?? -Infinity);
  if (x0 === x1) { const pad = o.xType === "time" ? HOUR : 1; x0 -= pad; x1 += pad; }
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (o.baseline != null) { lo = Math.min(lo, o.baseline); hi = Math.max(hi, o.baseline); }
  if (hi - lo < (o.minSpan || 0)) { const mid = (hi + lo) / 2; lo = mid - o.minSpan / 2; hi = mid + o.minSpan / 2; }
  const yt = niceTicks(lo, hi, 5), y0 = yt[0], y1 = yt[yt.length - 1];
  const px = (v) => M.l + ((v - x0) / (x1 - x0)) * (W - M.l - M.r);
  const py = (v) => M.t + ((y1 - v) / (y1 - y0)) * (H - M.t - M.b);

  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": o.ariaLabel, tabindex: "0" });
  for (const t of yt) {
    svg.append(s("line", { class: "grid", x1: M.l, x2: W - M.r, y1: py(t), y2: py(t) }),
      s("text", { class: "tick", x: M.l - 8, y: py(t) + 4, "text-anchor": "end" }, o.yFmt(t)));
  }
  if (o.xType === "time") {
    const { ticks, step } = timeTicks(x0, x1, Math.floor((W - M.l - M.r) / 84));
    for (const t of ticks) svg.append(s("text", { class: "tick", x: px(t), y: H - 8, "text-anchor": "middle" }, timeLabel(t, step)));
  } else {
    for (const t of intTicks(x0, x1, Math.floor((W - M.l - M.r) / 56))) {
      svg.append(s("text", { class: "tick", x: px(t), y: H - 8, "text-anchor": "middle" }, String(t)));
    }
  }
  if (o.baseline != null) {
    const by = py(o.baseline);
    svg.append(s("line", { class: "baseline", x1: M.l, x2: W - M.r, y1: by, y2: by }));
    if (o.zones) {
      // Left edge: the right edge is where the line ends and carries its label.
      svg.append(s("text", { class: "zone", x: M.l + 6, y: by - 6 }, o.zones.above),
        s("text", { class: "zone", x: M.l + 6, y: by + 15 }, o.zones.below));
    }
  }

  const pts = o.points.map((p) => [px(o.x(p)), py(o.y(p))]);
  const path = (cs, extend) => {
    let d = "";
    cs.forEach(([x, y], i) => {
      if (i === 0) d += `M${x.toFixed(1)},${y.toFixed(1)}`;
      else if (o.step) d += `H${x.toFixed(1)}V${y.toFixed(1)}`;
      else d += `L${x.toFixed(1)},${y.toFixed(1)}`;
    });
    if (extend != null && cs.length) d += `H${extend.toFixed(1)}`;
    return d;
  };
  const endX = o.extendTo != null ? px(o.extendTo) : null;
  if (o.wash) {
    const base = py(o.baseline ?? y0);
    svg.append(s("path", { class: "wash", d: `${path(pts, endX)}V${base.toFixed(1)}H${pts[0][0].toFixed(1)}Z` }));
  }
  const dim = Math.min(o.dimCount || 0, pts.length);
  if (dim > 0) {
    svg.append(s("path", { class: "line dim", d: path(pts.slice(0, dim)) }));
    if (pts.length > dim) svg.append(s("path", { class: "line s1", d: path(pts.slice(dim - 1), endX) }));
  } else {
    svg.append(s("path", { class: "line s1", d: path(pts, endX) }));
  }
  const [lx, ly] = pts[pts.length - 1];
  svg.append(s("circle", { class: `dot ${dim >= pts.length ? "dimdot" : "s1"}`, cx: endX ?? lx, cy: ly, r: 4.5 }));
  if (o.endLabel) svg.append(s("text", { class: "end-label", x: (endX ?? lx) + 9, y: ly + 4 }, o.endLabel));

  // Hover + keyboard layer: the crosshair snaps to the nearest point.
  const cross = s("line", { class: "crosshair", y1: M.t, y2: H - M.b, visibility: "hidden" });
  const focus = s("circle", { class: "dot s1", r: 5, visibility: "hidden" });
  const hit = s("rect", { class: "hit", x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b });
  svg.append(cross, focus, hit);
  let active = -1;
  const show = (i, cx, cy) => {
    active = i;
    const [x, y] = pts[i];
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    focus.setAttribute("cx", x); focus.setAttribute("cy", y); focus.setAttribute("visibility", "visible");
    const t = o.tip(o.points[i], i);
    if (cx == null) { const r = svg.getBoundingClientRect(); cx = r.left + (x / W) * r.width; cy = r.top + (y / H) * r.height; }
    showTip(cx, cy, t.title, t.rows, t.note);
  };
  const clear = () => { active = -1; cross.setAttribute("visibility", "hidden"); focus.setAttribute("visibility", "hidden"); hideTip(); };
  hit.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect();
    const mx = ((ev.clientX - r.left) / r.width) * W;
    let best = 0;
    for (let i = 1; i < pts.length; i++) if (Math.abs(pts[i][0] - mx) < Math.abs(pts[best][0] - mx)) best = i;
    show(best, ev.clientX, ev.clientY);
  });
  hit.addEventListener("pointerleave", clear);
  svg.addEventListener("blur", clear);
  svg.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    ev.preventDefault();
    const next = active < 0 ? pts.length - 1 : Math.max(0, Math.min(pts.length - 1, active + (ev.key === "ArrowRight" ? 1 : -1)));
    show(next);
  });
  host.append(svg);
}

/** Reliability diagram: one dot per probability bucket, the diagonal is perfect calibration. */
function calibChart(host, series) {
  host.replaceChildren();
  if (!series.some((sr) => sr.points.length)) {
    host.append(emptyState("No settled bets or forecasts yet. Calibration needs outcomes to compare against."));
    return;
  }
  const W = hostWidth(host);
  const side = Math.min(W - 60, 360);
  const M = { t: 12, l: 50, b: 40 };
  const H = side + M.t + M.b;
  const ox = M.l + Math.max(0, (W - M.l - side - 20) / 2);
  const px = (v) => ox + v * side, py = (v) => M.t + (1 - v) * side;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": "Calibration: forecast probability against realized frequency" });
  for (const t of [0, 0.25, 0.5, 0.75, 1]) {
    svg.append(
      s("line", { class: "grid", x1: ox, x2: ox + side, y1: py(t), y2: py(t) }),
      s("text", { class: "tick", x: ox - 8, y: py(t) + 4, "text-anchor": "end" }, `${t * 100}%`),
      s("text", { class: "tick", x: px(t), y: M.t + side + 16, "text-anchor": "middle" }, `${t * 100}%`),
    );
  }
  svg.append(
    s("line", { class: "ref", x1: px(0), y1: py(0), x2: px(1), y2: py(1) }),
    s("text", { class: "zone", x: ox + side / 2, y: H - 4, "text-anchor": "middle" }, "Phil's probability"),
    s("text", { class: "zone", x: 12, y: M.t + side / 2, "text-anchor": "middle", transform: `rotate(-90 12 ${M.t + side / 2})` }, "How often it happened"),
  );
  for (const sr of series) {
    for (const p of sr.points) {
      const cx = px(p.mean_est), cy = py(p.realized);
      const lowN = p.n < 5;
      svg.append(s("circle", { class: `dot ${sr.key} ${lowN ? "hollow" : ""}`, cx, cy, r: 5 }));
      const hit = s("circle", { class: "hit-dot", cx, cy, r: 12, tabindex: "0", role: "img",
        "aria-label": `${sr.name}: forecasts ${pct(p.lo, 0)} to ${pct(p.hi, 0)}, n ${p.n}, happened ${pct(p.realized)}` });
      const show = (x, y) => showTip(x, y, `${sr.name} · ${pct(p.lo, 0)}–${pct(p.hi, 0)} bucket`, [
        { key: sr.key, value: pct(p.realized), label: "happened" },
        { value: pct(p.mean_est), label: "Phil's average forecast" },
        { value: String(p.n), label: p.n === 1 ? "outcome" : "outcomes" },
      ], lowN ? "Fewer than 5 outcomes: read as anecdote." : null);
      hit.addEventListener("pointermove", (ev) => show(ev.clientX, ev.clientY));
      hit.addEventListener("pointerleave", hideTip);
      hit.addEventListener("focus", () => { const r = hit.getBoundingClientRect(); show(r.right, r.top); });
      hit.addEventListener("blur", hideTip);
      svg.append(hit);
    }
  }
  host.append(svg, h("div", { class: "legend" },
    ...series.map((sr) => h("span", { class: "key" }, h("i", { class: `sw ${sr.key}` }), `${sr.name} (${sr.points.reduce((a, p) => a + p.n, 0)})`)),
    h("span", { class: "key" }, h("i", { class: "sw ring" }), "hollow = fewer than 5"),
    h("span", { class: "key" }, h("i", { class: "ln" }), "perfect calibration"),
  ));
}

/** Inline diverging bar for a brier_delta cell: blue left = beats the market, red right = behind. */
function divbar(v, maxAbs) {
  const W = 84, H = 12, c = W / 2;
  const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });
  if (v != null && maxAbs > 0) {
    const w = (Math.min(1, Math.abs(v) / maxAbs)) * (c - 1);
    if (w > 0.5) svg.append(s("rect", { class: v < 0 ? "neg" : "pos", x: v < 0 ? c - w : c, y: 1, width: w, height: H - 2, rx: 2 }));
  }
  svg.append(s("line", { class: "mid", x1: c, x2: c, y1: 0, y2: H }));
  return h("span", { class: "divbar" }, svg, h("span", { class: `num ${tone(v, true)}` }, signed(v)));
}

function table(headers, rows, opts = {}) {
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ...headers.map((c) => h("th", { class: c.cls || "", scope: "col" }, c.label)))),
    h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((cell, i) => h("td", { class: headers[i].cls || "" }, cell))))),
  ), opts.caption ? h("p", { class: "caption" }, opts.caption) : null);
}

// --- markdown (retros) -> DOM, no innerHTML ------------------------------------------

function inline(parent, text) {
  const re = /(\*\*.+?\*\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) parent.append(text.slice(last, m.index));
    const tok = m[0];
    parent.append(tok.startsWith("**") ? h("strong", {}, tok.slice(2, -2)) : h("code", {}, tok.slice(1, -1)));
    last = re.lastIndex;
  }
  if (last < text.length) parent.append(text.slice(last));
  return parent;
}
function mdTable(lines) {
  const cells = (ln) => ln.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const rows = lines.filter((ln) => !/^\s*\|?\s*:?-{2,}/.test(ln)).map(cells);
  const [head, ...body] = rows;
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ...head.map((c) => inline(h("th"), c)))),
    h("tbody", {}, ...body.map((r) => h("tr", {}, ...r.map((c) => inline(h("td"), c)))))));
}
function renderMarkdown(text) {
  const out = h("div", { class: "md" });
  const lines = text.replace(/\r/g, "").split("\n");
  let i = 0;
  const isBlock = (ln) => /^(#{1,6}\s|\s*\||\s*[-*]\s+|\s*\d+\.\s+|```)/.test(ln);
  while (i < lines.length) {
    const ln = lines[i];
    if (!ln.trim()) { i++; continue; }
    let m;
    if ((m = ln.match(/^(#{1,6})\s+(.*)$/))) { out.append(inline(h(`h${Math.min(m[1].length + 2, 6)}`), m[2])); i++; continue; }
    if (/^```/.test(ln)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out.append(h("pre", {}, buf.join("\n"))); continue;
    }
    if (/^\s*\|/.test(ln)) {
      const buf = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) buf.push(lines[i++]);
      out.append(mdTable(buf)); continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(ln)) {
      const ordered = /^\s*\d+\./.test(ln);
      const list = h(ordered ? "ol" : "ul");
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        // Join the item's continuation lines first: bold often spans them.
        const parts = [lines[i].replace(/^\s*([-*]|\d+\.)\s+/, "")];
        i++;
        while (i < lines.length && lines[i].trim() && /^\s{2,}/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s+/.test(lines[i])) parts.push(lines[i++].trim());
        list.append(inline(h("li"), parts.join(" ")));
      }
      out.append(list); continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !isBlock(lines[i])) buf.push(lines[i++].trim());
    out.append(inline(h("p"), buf.join(" ")));
  }
  return out;
}

async function openReader(title, loader) {
  const dlg = $("#reader");
  $("#reader-title").textContent = title;
  const body = $("#reader-body");
  body.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  if (!dlg.open) dlg.showModal();
  try { body.replaceChildren(await loader()); }
  catch (e) { body.replaceChildren(h("p", { class: "bad" }, `Could not load: ${e.message}`)); }
}
async function fetchText(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.text();
}

// --- sections ------------------------------------------------------------------------------

function runnerStatus(r) {
  if (!r || !r.connected) return { cls: "st-neutral", label: r && /local/.test(r.reason || "") ? "Local preview · no runner" : "Runner not connected" };
  if (r.stale) return { cls: "st-critical", label: `Runner silent · last seen ${ago(r.updated_utc)}` };
  if (r.state === "blocked") return { cls: "st-critical", label: `Blocked · ${r.blocked_reason || "see runner panel"}` };
  if (r.state === "paused") return { cls: "st-warning", label: "Paused by operator" };
  if (r.state === "capped") return { cls: "st-warning", label: "Daily session cap reached · resumes 00:00Z" };
  if (r.state === "running" && r.current) return { cls: "st-good live", label: `Running ${KIND[r.current.kind] || r.current.kind} · ${ago(r.current.started_utc).replace(" ago", "")}` };
  if (r.state === "starting") return { cls: "st-neutral", label: "Starting…" };
  if (r.state === "stopping" || r.state === "stopped") return { cls: "st-neutral", label: "Restarting (redeploy)" };
  const next = r.next && r.next.hourly_utc ? ` · next cycle ${ago(r.next.hourly_utc)}` : "";
  return { cls: "st-neutral", label: `Idle${next}` };
}

function renderTop() {
  const st = S.state;
  const pill = $("#runner-pill");
  const rs = runnerStatus(st && st.runner);
  pill.className = `pill ${rs.cls}`;
  pill.replaceChildren(h("i", { class: "status-icon", "aria-hidden": "true" }), h("span", { class: "label" }, rs.label));
  pill.title = rs.label;

  const problems = [];
  if (S.fetchError) problems.push(`Could not refresh: ${S.fetchError}. Showing the last good data.`);
  if (st && st.fatal) problems.push(`The dashboard could not read the run: ${st.fatal}`);
  if (st && st.engine_error) problems.push(st.engine_error);
  for (const w of (st && st.data_warnings) || []) problems.push(w);
  const banner = $("#banner");
  banner.hidden = !problems.length;
  banner.replaceChildren(h("i", { class: "status-icon" }), h("div", {}, ...problems.map((p) => h("div", {}, p))));
  banner.className = "banner st-critical";
}

function tile({ label, value, cls, sub, extra, hero }) {
  return h("div", { class: `card tile${hero ? " hero" : ""}` },
    h("span", { class: "label" }, label),
    h("span", { class: `value ${cls || ""}` }, value),
    extra || null,
    sub ? h("span", { class: "sub" }, sub) : null);
}

function renderKpis() {
  const st = S.state, money = st.money, run = st.run;
  const o = st.bets && st.bets.overall;
  const f = st.forecasts && st.forecasts.overall;
  const settled = o ? o.n : 0;
  const target = run.target || 100;
  const mtmRows = (S.mtm && S.mtm.rows) || [];
  const unreal = mtmRows.filter((r) => r.unrealized_usd != null).reduce((a, r) => a + r.unrealized_usd, 0);
  const brier = o ? o.brier_delta : null;
  const brierStatus = o == null ? statusEl("st-neutral", "No settled bets yet")
    : brier < 0 ? statusEl("st-good", "Beating the market") : statusEl("st-warning", brier === 0 ? "Level with the market" : "Behind the market");
  $("#kpis").replaceChildren(
    tile({ hero: true, label: "Paper balance", value: usd(money.balance),
      sub: `Started at ${usd(run.bankroll_start)} · cash ${usd(money.cash)} · ${usd(money.open_cost)} in ${money.open_count} open` }),
    tile({ label: "Realized P&L", value: usd(money.realized_pnl, { sign: true }), cls: tone(money.realized_pnl),
      sub: o ? `ROI ${pct(o.roi)} on ${usd(o.staked_usd, { dp: 0 })} staked` : "Nothing settled yet" }),
    tile({ label: "Settled bets", value: `${settled} / ${target}`,
      extra: h("div", { class: "meter", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(target), "aria-valuenow": String(settled) },
        (() => { const i = h("i"); i.style.width = `${Math.min(100, (settled / target) * 100)}%`; return i; })()),
      sub: settled >= target ? "Target reached" : `${target - settled} to go` }),
    tile({ label: "Win rate", value: o ? pct(o.win_rate) : "—", sub: o ? `${o.wins} won · ${o.losses} lost` : "—" }),
    tile({ label: "Brier delta (bets)", value: signed(brier), cls: tone(brier, true), extra: brierStatus,
      sub: o ? `Phil ${o.brier_agent.toFixed(4)} · market ${o.brier_market.toFixed(4)}` : "Negative = Phil beat the price" }),
    tile({ label: "Brier delta (forecasts)", value: signed(f ? f.brier_delta : null), cls: tone(f ? f.brier_delta : null, true),
      sub: `${st.forecasts ? st.forecasts.settled : 0} settled · ${st.forecasts ? st.forecasts.open : 0} open, stake-free` }),
    tile({ label: "Open positions", value: String(money.open_count),
      sub: mtmRows.length ? `Marked ${usd(unreal, { sign: true })} unrealized` : `${usd(money.open_cost)} at risk` }),
  );
}

function renderExperiment() {
  const st = S.state, sc = st.scorecard, run = st.run;
  const body = $("#experiment-body");
  if (!sc) { body.replaceChildren(emptyState("Scorecard unavailable.")); return; }
  const target = run.target || 100;
  const at = sc.at_target;
  const rows = [
    ["Balance", (x) => usd(x.balance), (x) => ""],
    ["Trades settled", (x) => String(x.settled)],
    ["Wins", (x) => String(x.wins)],
    ["Losses", (x) => String(x.losses)],
    ["P&L", (x) => usd(x.pnl_usd, { sign: true }), (x) => tone(x.pnl_usd)],
    ["Brier delta (negative = Phil better)", (x) => signed(x.brier_delta), (x) => tone(x.brier_delta, true)],
    ["Strategy changes (lesson commits)", (x) => String(x.strategy_changes)],
  ];
  const cell = (x, fmt, cls) => x == null ? h("span", { class: "muted" }, "—") : h("span", { class: `num ${cls ? cls(x) : ""}` }, fmt(x));
  const started = run.started_utc ? `Run started ${when(run.started_utc)} (${ago(run.started_utc)})` : "No config/run.json: showing the whole ledger";
  body.replaceChildren(
    h("p", { class: "caption" }, started, run.archived_run ? ` · previous run archived in ${run.archived_run}` : ""),
    table(
      [{ label: "Track" }, { label: "Start", cls: "num" }, { label: "Now", cls: "num" },
        { label: at ? `At ${target} trades (${day(at.reached_utc)})` : `At ${target} trades`, cls: "num" }],
      rows.map(([label, fmt, cls]) => [label, cell(sc.start, fmt, cls), cell(sc.now, fmt, cls), at ? cell(at, fmt, cls) : h("span", { class: "muted" }, `${target - sc.now.settled} to go`)]),
    ),
  );
}

function withTableToggle(key, host, drawChart, drawTable) {
  const btn = $(`[data-table-toggle="${key}"]`);
  if (btn) btn.textContent = S.tableView[key] ? "Chart" : "Table";
  if (S.tableView[key]) { host.replaceChildren(drawTable()); } else drawChart();
}

function renderPnl() {
  const b = S.state.bets;
  const host = $("#chart-pnl");
  const pts = (b ? b.pnl_series : []).filter((p) => parseT(p.t) != null);
  const settledPts = pts.filter((p) => p.id);
  withTableToggle("pnl", host, () => lineChart(host, {
    points: settledPts.length ? pts : [],
    empty: "No settled bets yet. The curve starts at the first resolution; short-term markets usually settle within a day or two.",
    x: (p) => parseT(p.t), y: (p) => p.pnl, xType: "time", step: true, wash: true, baseline: 0, minSpan: 10,
    extendTo: Date.now(), yFmt: (v) => usd(v, { dp: 0 }), endLabel: usd(settledPts.length ? settledPts[settledPts.length - 1].pnl : 0, { sign: true }),
    ariaLabel: "Cumulative realized profit and loss over time",
    tip: (p) => p.id
      ? { title: when(p.t), rows: [{ key: "s1", value: usd(p.pnl, { sign: true }), label: "cumulative" }, { value: usd(p.delta_usd, { sign: true }), label: `this bet (${p.status})` }], note: p.q }
      : { title: when(p.t), rows: [{ key: "s1", value: usd(0), label: "run start" }] },
  }), () => table(
    [{ label: "Settled" }, { label: "Market" }, { label: "Result" }, { label: "Bet P&L", cls: "num" }, { label: "Cumulative", cls: "num" }],
    settledPts.slice().reverse().map((p) => [when(p.t), p.q, p.status, h("span", { class: tone(p.delta_usd) }, usd(p.delta_usd, { sign: true })), usd(p.pnl, { sign: true })])));
}

function renderSkill() {
  const b = S.state.bets, win = S.state.run.window || 20;
  const host = $("#chart-skill");
  const pts = b ? b.skill_series : [];
  const warm = pts.filter((p) => p.warming).length;
  withTableToggle("skill", host, () => {
    lineChart(host, {
      points: pts,
      empty: `No settled bets yet. Each bet adds one point; the rolling window fills at ${win} bets.`,
      x: (p) => p.n, y: (p) => p.rolling, xType: "index", baseline: 0, minSpan: 0.1, dimCount: warm,
      zones: { above: "behind market ↑", below: "beats market ↓" },
      yFmt: (v) => signed(v, 2), endLabel: pts.length ? signed(pts[pts.length - 1].rolling, 3) : null,
      ariaLabel: "Rolling brier delta by bet number; below zero means Phil beat the market price",
      tip: (p) => ({
        title: `Bet #${p.n} · placed ${when(p.t)}`,
        rows: [{ key: "s1", value: signed(p.rolling), label: p.warming ? `rolling (${p.n} of ${win})` : `rolling ${win}` },
          { value: signed(p.delta), label: `this bet (${p.status})` }],
        note: p.q,
      }),
    });
    if (warm && pts.length) host.append(h("p", { class: "caption" }, `Grey = warm-up: fewer than ${win} bets in the window.`));
  }, () => table(
    [{ label: "#", cls: "num" }, { label: "Placed" }, { label: "Market" }, { label: "Bet delta", cls: "num" }, { label: `Rolling ${win}`, cls: "num" }],
    pts.slice().reverse().map((p) => [String(p.n), when(p.t), p.q, h("span", { class: tone(p.delta, true) }, signed(p.delta)), h("span", { class: tone(p.rolling, true) }, signed(p.rolling))])));
}

const VERDICT = {
  improved: ["st-good", "Improved"],
  luck: ["st-warning", "Probably luck"],
  "no-change": ["st-neutral", "No clear change"],
  worse: ["st-serious", "Got worse"],
  collecting: ["st-neutral", "Collecting data"],
  unjudged: ["st-neutral", "Verdict rule not set yet"],
  error: ["st-critical", "Verdict rule failed"],
};

function renderCompare() {
  const body = $("#compare-body");
  const b = S.state.bets;
  const c = b && b.comparison;
  if (!c) { body.replaceChildren(emptyState("Comparison unavailable.")); return; }
  const v = c.verdict || { verdict: "collecting", reason: "" };
  const [cls, label] = VERDICT[v.verdict] || ["st-neutral", v.verdict];
  const verdictBox = h("div", { class: `verdict ${cls}` }, h("i", { class: "status-icon", "aria-hidden": "true" }),
    h("div", {}, h("strong", {}, label), h("p", {}, v.reason || "")));
  if (!c.first) { body.replaceChildren(verdictBox); return; }
  const F = c.first, L = c.last;
  // lowerIsBetter: true / false colors the change; null leaves it neutral.
  const change = (a, b2, fmt, lowerIsBetter) => {
    if (a == null || b2 == null) return h("span", { class: "muted" }, "—");
    const d = b2 - a;
    return h("span", { class: `num ${lowerIsBetter == null ? "" : tone(d, lowerIsBetter)}` }, fmt(d));
  };
  const commits = (w) => w && w.strategy_commits.length
    ? h("ul", { class: "commit-list" }, ...w.strategy_commits.map((x) => h("li", { title: x.subject }, `${x.short} ${x.subject}`))) : null;
  const col = (w, fn) => (w ? fn(w) : h("span", { class: "muted" }, `${c.needed - c.settled} more to unlock`));
  const rows = [
    ["Bets placed", (w) => `${day(w.placed_from)} – ${day(w.placed_to)}`, null],
    ["P&L", (w) => h("span", { class: `num ${tone(w.pnl_usd)}` }, usd(w.pnl_usd, { sign: true })), change(F.pnl_usd, L && L.pnl_usd, (d) => usd(d, { sign: true }), false)],
    ["Win rate", (w) => pct(w.win_rate), change(F.win_rate, L && L.win_rate, (d) => signed(d * 100, 1) + " pts", false)],
    ["Brier delta (lower is better)", (w) => h("span", { class: `num ${tone(w.brier_delta, true)}` }, signed(w.brier_delta)), change(F.brier_delta, L && L.brier_delta, (d) => signed(d), true)],
    ["Phil's Brier / market's Brier", (w) => `${w.brier_agent.toFixed(4)} / ${w.brier_market.toFixed(4)}`, null],
    // Neutral on purpose: less claimed edge can mean better calibration, not worse trading.
    ["Average estimated edge", (w) => w.avg_edge == null ? "—" : signed(w.avg_edge, 3), change(F.avg_edge, L && L.avg_edge, (d) => signed(d, 3), null)],
    ["Strategy changes while placing", (w) => h("div", {}, String(w.strategy_changes), commits(w)), null],
  ];
  body.replaceChildren(verdictBox, table(
    [{ label: "" }, { label: `First ${c.window}` }, { label: `Last ${c.window}` }, { label: "Change", cls: "num" }],
    rows.map(([label, fn, delta]) => [label, col(F, fn), col(L, fn), delta || h("span", { class: "muted" }, "")]),
    { caption: `Windows are in placement order: a bet reflects the strategy in force when it was placed. ${c.settled} settled so far; the windows stop overlapping at ${c.needed}.` },
  ));
}

function renderCalib() {
  const b = S.state.bets, f = S.state.forecasts;
  const host = $("#chart-calib");
  const series = [
    { key: "s1", name: "Bets", points: (b && b.calibration) || [] },
    { key: "s2", name: "Forecasts", points: (f && f.calibration) || [] },
  ];
  withTableToggle("calib", host, () => calibChart(host, series), () => table(
    [{ label: "Stream" }, { label: "Bucket" }, { label: "n", cls: "num" }, { label: "Avg forecast", cls: "num" }, { label: "Happened", cls: "num" }],
    series.flatMap((sr) => sr.points.map((p) => [sr.name, `${pct(p.lo, 0)}–${pct(p.hi, 0)}`, String(p.n), pct(p.mean_est), pct(p.realized)]))));
}

function renderEdge() {
  const b = S.state.bets;
  const rows = (b && (S.edgeGroup === "category" ? b.by_category : b.by_edge_class)) || [];
  const body = $("#edge-body");
  $$("#edge [data-group]").forEach((btn) => btn.setAttribute("aria-pressed", String(btn.dataset.group === S.edgeGroup)));
  if (!rows.length) { body.replaceChildren(emptyState("No settled bets yet.")); return; }
  const maxAbs = Math.max(0.05, ...rows.map((r) => Math.abs(r.brier_delta || 0)));
  body.replaceChildren(table(
    [{ label: S.edgeGroup === "category" ? "Category" : "Edge class" }, { label: "n", cls: "num" }, { label: "Win rate", cls: "num" }, { label: "P&L", cls: "num" }, { label: "Brier delta" }],
    rows.map((r) => [r.name, String(r.n), pct(r.win_rate), h("span", { class: tone(r.pnl_usd) }, usd(r.pnl_usd, { sign: true })), divbar(r.brier_delta, maxAbs)]),
    { caption: "Bars: blue, left of centre = Phil beat the price in this group; red, right = behind it." }));
}

function marketCell(row) {
  if (!row.rationale) return marketLink(row);
  return h("details", {}, h("summary", {}, row.question || row.market_id), h("p", {}, row.rationale),
    row.slug ? h("p", {}, marketLink({ ...row, question: "Open on Polymarket ↗" })) : null);
}

function renderOpen() {
  const b = S.state.bets;
  const rows = (b && b.open) || [];
  const body = $("#open-body");
  const marks = new Map(((S.mtm && S.mtm.rows) || []).map((r) => [r.id, r]));
  const note = $("#mtm-note");
  if (S.mtm && S.mtm.updated_utc) note.textContent = `Live marks from the Polymarket CLOB mid, refreshed ${ago(S.mtm.updated_utc)}. Advisory only; nothing is realized until the market resolves.`;
  if (!rows.length) { body.replaceChildren(emptyState("No open positions.")); return; }
  body.replaceChildren(table(
    [{ label: "Market" }, { label: "Side" }, { label: "Entry", cls: "num" }, { label: "Phil's est.", cls: "num" }, { label: "Stake", cls: "num" },
      { label: "Mark", cls: "num" }, { label: "Unrealized", cls: "num" }, { label: "Ends (UTC)" }],
    rows.map((r) => {
      const m = marks.get(r.id);
      const mid = m && m.mid != null ? prob(m.mid) : m && m.error ? "n/a" : "…";
      const un = m && m.unrealized_usd != null ? h("span", { class: tone(m.unrealized_usd) }, usd(m.unrealized_usd, { sign: true })) : "—";
      const ends = h("span", {}, when(r.end_date), m && m.past_end_date ? h("span", { class: "badge" }, " past end") : null);
      return [h("div", { class: "q" }, marketCell(r)), r.outcome, prob(r.entry_price), prob(r.est_prob), usd(r.stake_usd), mid, un, ends];
    })));
}

function renderTrades() {
  const b = S.state.bets;
  const all = (b && b.trades) || [];
  $$("#trades [data-filter]").forEach((btn) => btn.setAttribute("aria-pressed", String(btn.dataset.filter === S.tradeFilter)));
  const rows = S.tradeFilter === "all" ? all : all.filter((t) => t.status === S.tradeFilter);
  const body = $("#trades-body");
  if (!rows.length) { body.replaceChildren(emptyState(all.length ? "No trades match this filter." : "No paper bets yet. Phil places its first bets on its first FULL cycle, once research finds an edge above risk.json's min_edge.")); return; }
  const shown = rows.slice(0, 300);
  const status = (t) => t.status === "won" ? statusEl("st-good", "Won") : t.status === "lost" ? statusEl("st-critical", "Lost")
    : t.status === "open" ? statusEl("st-neutral", "Open") : statusEl("st-warning", "Void");
  body.replaceChildren(table(
    [{ label: "Placed (UTC)" }, { label: "Market" }, { label: "Side" }, { label: "Entry", cls: "num" }, { label: "Est.", cls: "num" }, { label: "Edge", cls: "num" },
      { label: "Stake", cls: "num" }, { label: "Category · class" }, { label: "Status" }, { label: "P&L", cls: "num" }],
    shown.map((t) => [when(t.ts), h("div", { class: "q" }, marketCell(t)), t.outcome, prob(t.entry_price), prob(t.est_prob),
      t.edge == null ? "—" : signed(t.edge, 3), usd(t.stake_usd), `${t.category || "—"} · ${t.edge_class || "—"}`, status(t),
      t.pnl_usd == null ? "—" : h("span", { class: tone(t.pnl_usd) }, usd(t.pnl_usd, { sign: true }))]),
    { caption: rows.length > shown.length ? `Showing the newest ${shown.length} of ${rows.length}.` : `${rows.length} trade${rows.length === 1 ? "" : "s"}.` }));
}

function renderLessons() {
  const L = S.state.lessons, repo = S.state.repo;
  const body = $("#lessons-body");
  if (!L || !L.git_ok) { body.replaceChildren(emptyState("Git history unavailable.")); return; }
  const kindBadge = { retro: "retro", "deep-retro": "deep retro", cycle: "cycle", triggered: "triggered", operator: "operator", other: "commit" };
  const items = L.commits.map((c) => h("li", {},
    h("div", { class: "meta" }, h("span", { class: "badge" }, kindBadge[c.kind] || c.kind), ago(c.t),
      repo && repo.web ? h("a", { href: `${repo.web}/commit/${c.sha}`, target: "_blank", rel: "noopener noreferrer", class: "mono" }, c.short) : h("span", { class: "mono" }, c.short)),
    h("div", { class: "subject" }, c.subject),
    c.files.length ? h("div", { class: "files" }, ...c.files.map((f) => h("code", {}, f.replace(/^strategy\//, "")))) : null));
  body.replaceChildren(
    h("div", { class: "counters" },
      h("div", {}, h("strong", {}, String(L.strategy_changes)), h("span", {}, "strategy changes")),
      h("div", {}, h("strong", {}, String(L.retro_commits)), h("span", {}, "retro commits")),
      h("div", {}, h("strong", {}, String(L.pacing_edits)), h("span", {}, "pacing/watch edits")),
      h("div", {}, h("strong", {}, String(L.cycle_commits)), h("span", {}, "cycles committed"))),
    items.length ? h("ul", { class: "feed" }, ...items) : emptyState("No self-edits yet. Phil edits its strategy after bets settle and it writes a retro."));
}

function renderRetros() {
  const list = S.state.retros || [];
  const body = $("#retros-body");
  if (!list.length) { body.replaceChildren(emptyState("No retros yet. The first one is written on the cycle after a bet or forecast settles.")); return; }
  body.replaceChildren(h("ul", { class: "feed" }, ...list.map((r) => h("li", {},
    h("div", { class: "meta" }, h("span", { class: "badge" }, r.kind === "deep" ? "deep retro" : "retro"), r.name.replace(/\.md$/, "")),
    h("button", { class: "linklike", type: "button", onclick: () => openReader(r.title, async () => renderMarkdown(await fetchText(`/api/retro?name=${encodeURIComponent(r.name)}`))) }, r.title),
    r.summary ? h("div", { class: "summary" }, r.summary) : null))));
}

function renderRunner() {
  const st = S.state, r = st.runner, cyc = st.cycles, repo = st.repo;
  const body = $("#runner-body");
  const parts = [];
  if (!r || !r.connected) {
    parts.push(h("p", { class: "caption" }, `Not connected to a supervisor (${(r && r.reason) || "unknown"}). On Railway this panel shows the schedule, each session's result, and the watcher's last verdict.`));
  } else {
    const cfg = r.config || {};
    const kv = (label, value) => h("div", {}, h("span", {}, label), h("strong", { title: String(value) }, value));
    parts.push(h("div", { class: "kv" },
      kv("State", r.state === "running" && r.current ? `running ${KIND[r.current.kind] || r.current.kind}` : r.state),
      kv("Next hourly cycle", r.next && r.next.hourly_utc ? `${when(r.next.hourly_utc)} (${ago(r.next.hourly_utc)})` : "—"),
      kv("Next watch check", r.next && r.next.watch_utc ? ago(r.next.watch_utc) : "off"),
      kv("Next deep retro", r.next && r.next.deep_retro_utc ? `${when(r.next.deep_retro_utc)}` : "off"),
      kv("Sessions today", `${r.sessions_today ?? 0} / ${r.max_sessions_per_day ?? "∞"}`),
      kv("Cadence", `${cfg.cycle_interval_min ?? "?"}m cycles · ${cfg.watch_interval_min ? cfg.watch_interval_min + "m watch" : "watch off"}`),
      kv("Claude auth", r.auth || "—"),
      kv("Git push", r.push || "—"),
      kv("Supervisor", `${r.version || "dev"} · up ${ago(r.started_utc).replace(" ago", "")}`),
    ));
    if (r.state === "blocked" && r.blocked_reason) parts.push(h("div", { class: "verdict st-critical" }, h("i", { class: "status-icon" }), h("div", {}, h("strong", {}, "Blocked"), h("p", {}, r.blocked_reason))));
    for (const w of r.warnings || []) parts.push(h("div", { class: "verdict st-warning" }, h("i", { class: "status-icon" }), h("div", {}, h("strong", {}, "Warning"), h("p", {}, w))));
    if (r.deep_note) parts.push(h("p", { class: "caption" }, `Deep retro: ${r.deep_note}`));
    const lw = r.last_watch;
    if (lw) {
      parts.push(h("p", { class: "caption" }, `Last watch check ${ago(lw.utc)}: `,
        lw.error ? `error (${lw.error})` : lw.trigger ? `fired on ${lw.keys.join(", ")}` : "quiet",
        lw.notes && lw.notes.length ? ` · ${lw.notes.slice(0, 2).join(" · ")}` : "",
        r.watch_fires_today != null ? ` · ${r.watch_fires_today} fire(s) today` : ""));
    }
    const hist = r.history || [];
    if (hist.length) {
      const result = (x) => x.timed_out ? statusEl("st-critical", "Timed out") : x.interrupted ? statusEl("st-warning", "Interrupted")
        : x.claude_failed ? statusEl("st-critical", "Claude failed") : x.exit === 0 ? statusEl("st-good", "OK") : statusEl("st-critical", `Exit ${x.exit}`);
      parts.push(h("h3", {}, "Recent sessions"), table(
        [{ label: "Kind" }, { label: "Started (UTC)" }, { label: "Took", cls: "num" }, { label: "Result" }, { label: "Reported" }, { label: "" }],
        hist.slice(0, 15).map((x) => [KIND[x.kind] || x.kind, when(x.started_utc), duration(x.duration_s), result(x),
          h("span", { class: "small" }, (x.summary || "").slice(0, 220)),
          x.log ? h("button", { class: "icon-btn small", type: "button", onclick: () => openReader(`Session log · ${x.log}`, async () => h("pre", {}, await fetchText(`/api/log?name=${encodeURIComponent(x.log)}`))) }, "Log") : ""]),
      ));
    }
  }
  const last24 = cyc && cyc.last_24h ? Object.entries(cyc.last_24h).map(([k, v]) => `${v} ${k}`).join(" · ") : "";
  parts.push(h("h3", {}, "Cycle log"), h("p", { class: "caption" }, `${cyc ? cyc.total : 0} cycles logged${last24 ? ` · last 24h: ${last24}` : ""}.`));
  if (cyc && cyc.recent.length) {
    parts.push(table(
      [{ label: "Time (UTC)" }, { label: "Tick" }, { label: "Placed", cls: "num" }, { label: "Settled", cls: "num" }, { label: "Cash", cls: "num" }, { label: "Notes" }],
      cyc.recent.slice(0, 12).map((c) => [when(c.t), h("span", { class: "badge" }, c.tick), c.placed ?? "—", c.settled ?? "—", c.cash == null ? "—" : usd(c.cash),
        h("details", {}, h("summary", { class: "small" }, c.text.slice(0, 90) + (c.text.length > 90 ? "…" : "")), h("p", {}, c.text))])));
  }
  if (repo) {
    const bits = [`HEAD ${repo.head ? repo.head.slice(0, 7) : "?"} on ${repo.branch || "?"}`];
    if (repo.unpushed != null) bits.push(repo.unpushed ? `${repo.unpushed} commit(s) not pushed yet` : "in sync with origin");
    if (repo.last_commit) bits.push(`last commit ${ago(repo.last_commit.t)}`);
    parts.push(h("p", { class: "caption" }, bits.join(" · ")));
  }
  body.replaceChildren(...parts);
}

function renderFooter() {
  const st = S.state, repo = st.repo;
  $("#footer-repo").replaceChildren(repo && repo.web ? h("a", { href: repo.web, target: "_blank", rel: "noopener noreferrer" }, repo.web.replace("https://", "")) : "");
  $("#footer-gen").textContent = `Snapshot ${when(st.generated_utc)}`;
  $$("[data-window]").forEach((n) => { n.textContent = String(st.run.window || 20); });
}

function renderCharts() {
  if (!S.state || S.state.fatal) return;
  renderPnl(); renderSkill(); renderCalib();
}

function renderAll() {
  renderTop();
  const st = S.state;
  if (!st || st.fatal) return;
  renderKpis(); renderExperiment(); renderCharts(); renderCompare(); renderEdge();
  renderOpen(); renderTrades(); renderLessons(); renderRetros(); renderRunner(); renderFooter();
  tickUpdated();
}

function tickUpdated() {
  const el = $("#updated");
  el.textContent = S.lastFetch ? `Updated ${ago(new Date(S.lastFetch).toISOString())}` : "";
}

// --- data -----------------------------------------------------------------------------------

async function refreshState() {
  const main = $("#main");
  if (S.state) main.classList.add("refreshing");
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    S.state = await r.json();
    S.lastFetch = Date.now();
    S.fetchError = null;
  } catch (e) {
    S.fetchError = e.message;
  } finally {
    main.classList.remove("refreshing");
  }
  if (S.state) renderAll(); else renderTop();
}

async function refreshMarks() {
  try {
    const r = await fetch("/api/mtm", { cache: "no-store" });
    if (r.ok) { S.mtm = await r.json(); if (S.state && !S.state.fatal) { renderOpen(); renderKpis(); } }
  } catch { /* marks are advisory */ }
}

// --- wiring ------------------------------------------------------------------------------------

function wire() {
  $("#theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const current = root.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("phil-theme", next); } catch { /* not persisted */ }
  });
  $$("[data-table-toggle]").forEach((btn) => btn.addEventListener("click", () => {
    const key = btn.dataset.tableToggle;
    S.tableView[key] = !S.tableView[key];
    renderCharts();
  }));
  $$("#trades [data-filter]").forEach((btn) => btn.addEventListener("click", () => { S.tradeFilter = btn.dataset.filter; renderTrades(); }));
  $$("#edge [data-group]").forEach((btn) => btn.addEventListener("click", () => { S.edgeGroup = btn.dataset.group; renderEdge(); }));
  $("#reader-close").addEventListener("click", () => $("#reader").close());
  $("#reader").addEventListener("click", (ev) => { if (ev.target.id === "reader") ev.target.close(); });
  let resizeTimer = null;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderCharts, 150); });
  document.addEventListener("scroll", hideTip, { passive: true });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshState(); });
}

wire();
refreshState().then(refreshMarks);
setInterval(refreshState, STATE_EVERY_MS);
setInterval(refreshMarks, MTM_EVERY_MS);
setInterval(tickUpdated, 5000);
