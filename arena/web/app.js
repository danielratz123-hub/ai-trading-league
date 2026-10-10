// AI Trading League dashboard. Plain JavaScript, no build step.
// Everything the agents wrote (market views, reasons, notes, code) is
// untrusted text: it only ever reaches the page through esc() / textContent.
"use strict";

// Same origin only: the dashboard is always served by the app itself, behind the login.
const API = "";
const state = { status: null, chartMode: "equity", tab: null, charts: {} };

// ------------------------------------------------------------------ helpers

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const money = v => v == null ? "–" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (v, signed = true) => v == null ? "–" : (signed && v > 0 ? "+" : "") + (v * 100).toFixed(2) + "%";
const signClass = v => v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Color follows the agent, never its rank: fixed slots by agent id.
const AGENT_SLOT = { claude: 1, gpt: 2 };
function agentVar(id) {
  const ids = (state.status?.agents || []).map(a => a.id);
  const slot = AGENT_SLOT[id] || Math.min(3, ids.indexOf(id) + 1);
  return `--series-${slot}`;
}
const agentColor = id => css(agentVar(id));
const label = id => ({ claude: "Claude", gpt: "GPT" }[id] || id);

async function get(path) {
  const r = await fetch(API + path);
  if (r.status === 401) {               // session expired: back to the login page
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    throw new Error("Not signed in");
  }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* not JSON */ }
    throw new Error(`${path}: ${msg}`);
  }
  return r.json();
}

function showError(e) {
  const el = document.getElementById("error");
  el.textContent = e ? String(e.message || e) : "";
  el.hidden = !e;
}

function storageGet(store, key) { try { return window[store].getItem(key); } catch (_) { return null; } }
function storageSet(store, key, v) { try { window[store].setItem(key, v); } catch (_) { /* private mode */ } }

// ------------------------------------------------------------------ theme

function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
}
applyTheme(storageGet("localStorage", "theme"));
document.getElementById("theme-btn").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  applyTheme(next);
  storageSet("localStorage", "theme", next);
  refresh();   // charts read colors from CSS variables, so redraw them
});

// ------------------------------------------------------------------ status & scoreboard

function renderStatus(s) {
  const w = s.latest_week;
  const parts = [];
  if (w) parts.push(`Week ${w.week_no}${w.simulate ? " (simulated)" : ""}: decided ${w.decision_date}, trades ${w.exec_date} (${w.status})`);
  const lv = state.live;
  if (lv && lv.market_open && lv.updated) parts.push(`● LIVE ${lv.updated} ET (prices refresh every 2 min)`);
  else if (s.as_of) parts.push(`as of ${s.as_of} ${s.as_of_field === "open" ? "open (today's close comes after 16:45 ET)" : "close"}`);
  if (!parts.length) parts.push("Not started yet");
  if (s.data_feed_label) parts.push(s.data_feed_label);   // e.g. "IEX feed" (ALPACA_DATA_FEED)
  document.getElementById("status-line").textContent = parts.join(" · ");
  document.getElementById("paused-badge").hidden = !s.paused;
}

function renderScoreboard(sb) {
  const el = document.getElementById("scoreboard");
  if (!sb.agents.length) {
    el.innerHTML = `<div class="card"><p class="muted">The league hasn't started yet. ${controlAvailable
      ? 'Use <a href="setup.html">Setup</a> to start it.' : "Check back once it's running."}</p></div>`;
    return;
  }
  const leader = Math.max(...sb.agents.map(a => a.points));
  const lv = state.live && state.live.market_open ? state.live : null;
  const cards = sb.agents.map(row => {
    const l = lv && lv.agents[row.agent];
    // During market hours, equity and returns come from live prices.
    const a = l ? { ...row, equity: l.equity, return_since_start: l.return_since_start, vs_spy: l.vs_spy ?? row.vs_spy } : row;
    return `
    <article class="card">
      <div class="who"><span class="swatch" style="background:var(${agentVar(a.agent)})"></span>${esc(label(a.agent))}
        ${a.points === leader && a.points > 0 && sb.agents.filter(x => x.points === leader).length === 1 ? '<span class="badge badge-good">leading</span>' : ""}</div>
      <div class="points"><b>${a.points}</b> point${a.points === 1 ? "" : "s"}</div>
      <div class="hero">${money(a.equity)}${l ? ' <span class="badge badge-good live">● live</span>' : ""}</div>
      <dl class="stats">
        ${l ? `<dt>Since ${esc(l.since_snapshot === new Date().toISOString().slice(0, 10) ? "this morning's trades" : "last close")}</dt><dd class="${signClass(l.change_since_last_snapshot)}">${pct(l.change_since_last_snapshot)}</dd>` : ""}
        <dt>Return since start</dt><dd class="${signClass(a.return_since_start)}">${pct(a.return_since_start)}</dd>
        <dt>vs SPY</dt><dd class="${signClass(a.vs_spy)}">${pct(a.vs_spy)}</dd>
        <dt>This week so far</dt><dd class="${signClass(a.week_to_date)}">${pct(a.week_to_date)}</dd>
        <dt>Last scored week</dt><dd class="${signClass(a.last_week_return)}">${pct(a.last_week_return)}</dd>
        <dt>Bonus deposits</dt><dd>${money(Math.max(0, (a.deposits || 0) - (state.status?.starting_cash || 0)))}</dd>
      </dl>
    </article>`;
  }).join("");
  const spy = `
    <article class="card">
      <div class="who"><span class="swatch dashed"></span>SPY benchmark</div>
      <div class="points">buy &amp; hold since ${esc(sb.since || "start")}</div>
      <div class="hero ${signClass(lv && lv.spy_return != null ? lv.spy_return : sb.spy_return)}">${pct(lv && lv.spy_return != null ? lv.spy_return : sb.spy_return)}${lv && lv.spy_return != null ? ' <span class="badge badge-good live">● live</span>' : ""}</div>
      <p class="muted small">Return-since-start figures exclude bonus deposits, so they compare fairly with SPY.</p>
    </article>`;
  el.innerHTML = cards + spy;
}

// ------------------------------------------------------------------ charts

// Vertical crosshair line at the hovered date.
const crosshair = {
  id: "crosshair",
  afterDatasetsDraw(chart) {
    const active = chart.tooltip?.getActiveElements?.() || [];
    if (!active.length) return;
    const x = active[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = css("--axis");
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
    ctx.restore();
  },
};

// Direct label at the end of each line (identity never by color alone),
// nudged apart vertically so labels of lines that end close together don't collide.
const endLabels = {
  id: "endLabels",
  afterDatasetsDraw(chart) {
    const ctx = chart.ctx, gap = 14;
    const labels = [];
    chart.data.datasets.forEach((ds, i) => {
      const meta = chart.getDatasetMeta(i);
      if (meta.hidden || chart.data.datasets.length > 6) return;
      for (let j = meta.data.length - 1; j >= 0; j--) {
        if (ds.data[j] != null) { labels.push({ text: ds.label, x: meta.data[j].x, y: meta.data[j].y }); break; }
      }
    });
    labels.sort((a, b) => a.y - b.y);
    for (let k = 1; k < labels.length; k++) labels[k].y = Math.max(labels[k].y, labels[k - 1].y + gap);
    ctx.save();
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillStyle = css("--ink-2");
    labels.forEach(l => ctx.fillText(l.text, Math.min(l.x + 6, chart.width - ctx.measureText(l.text).width - 2), l.y + 4));
    ctx.restore();
  },
};

function lineChart(canvasId, labels, datasets, formatY) {
  if (state.charts[canvasId]) state.charts[canvasId].destroy();
  if (!window.Chart) return;
  const grid = css("--grid"), muted = css("--muted");
  state.charts[canvasId] = new Chart(document.getElementById(canvasId), {
    type: "line",
    data: { labels, datasets },
    plugins: [crosshair, endLabels],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, spanGaps: true,
      layout: { padding: { right: 56 } },
      interaction: { mode: "index", intersect: false },
      elements: { point: { radius: 0, hoverRadius: 4, hitRadius: 12 }, line: { borderWidth: 2, tension: 0 } },
      plugins: {
        legend: { display: false },   // we render our own legend above the chart
        tooltip: {
          backgroundColor: css("--surface"), titleColor: css("--ink"), bodyColor: css("--ink"),
          borderColor: css("--border"), borderWidth: 1, padding: 10, usePointStyle: true,
          callbacks: { label: c => ` ${c.dataset.label}: ${c.raw == null ? "–" : formatY(c.raw)}` },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: muted, maxTicksLimit: innerWidth < 600 ? 4 : 8, maxRotation: 0 },
             border: { color: css("--axis") } },
        y: { grid: { color: grid }, ticks: { color: muted, callback: formatY }, border: { display: false } },
      },
    },
  });
}

function renderLegend(el, items) {
  el.innerHTML = items.map(i => `<span><span class="swatch ${i.dashed ? "dashed" : ""}" ${i.dashed ? "" : `style="background:${i.color}"`}></span>${esc(i.label)}</span>`).join("");
}

function renderEquity(eq) {
  const lv = state.live && state.live.market_open ? state.live : null;
  if (lv && Object.keys(lv.agents).length) {
    // Add today's live value as the last point (not stored; just for watching).
    eq = JSON.parse(JSON.stringify(eq));
    eq.dates.push(`now ${lv.updated}`);
    for (const [id, series] of Object.entries(eq.agents)) {
      const l = lv.agents[id];
      series.equity.push(l ? l.equity : null);
      series.index.push(l && l.return_since_start != null ? 1 + l.return_since_start : null);
    }
    eq.spy_index.push(lv.spy_return != null ? 1 + lv.spy_return : null);
  }
  const ids = Object.keys(eq.agents);
  const isIdx = state.chartMode === "index";
  const fmt = isIdx ? v => pct(v - 1) : v => "$" + Math.round(v).toLocaleString();
  const datasets = ids.map(id => ({
    label: label(id), borderColor: agentColor(id), backgroundColor: agentColor(id),
    data: isIdx ? eq.agents[id].index : eq.agents[id].equity,
  }));
  // SPY on the same single axis: as an index, or as "$starting_cash in SPY".
  datasets.push({
    label: "SPY", borderColor: css("--bench"), backgroundColor: css("--bench"), borderDash: [6, 4],
    data: eq.spy_index.map(v => v == null ? null : isIdx ? v : v * eq.starting_cash),
  });
  document.getElementById("eq-note").textContent = isIdx
    ? "Deposit-free performance (bonus deposits removed), so it compares fairly with SPY."
    : `Account value. SPY line = $${eq.starting_cash.toLocaleString()} invested at the first trade. Bonus deposits show up as jumps.`;
  renderLegend(document.getElementById("eq-legend"), datasets.map(d => ({ label: d.label, color: d.borderColor, dashed: d.label === "SPY" })));
  lineChart("eq-chart", eq.dates, datasets, fmt);
  // Table view (accessibility + exact numbers)
  const head = `<tr><th>Date</th>${datasets.map(d => `<th class="num">${esc(d.label)}</th>`).join("")}</tr>`;
  const rows = eq.dates.map((d, i) => `<tr><td>${esc(d)}</td>${datasets.map(ds => `<td class="num">${ds.data[i] == null ? "–" : fmt(ds.data[i])}</td>`).join("")}</tr>`).reverse().join("");
  document.getElementById("eq-table").innerHTML = `<table>${head}${rows}</table>`;
}

document.querySelectorAll(".segmented button").forEach(b => b.addEventListener("click", () => {
  state.chartMode = b.dataset.mode;
  document.querySelectorAll(".segmented button").forEach(x => x.classList.toggle("on", x === b));
  get("/api/equity").then(renderEquity).catch(showError);
}));

// ------------------------------------------------------------------ tabs

function setupTabs() {
  const tabs = [...(state.status?.agents || []).map(a => ({ id: a.id, name: label(a.id) })),
                { id: "timeline", name: "Weekly timeline" }, { id: "reports", name: "Reports" },
                { id: "research", name: "Research & costs" },
                ...(state.mirrorMode && state.mirrorMode !== "off" ? [{ id: "alpaca", name: "Alpaca" }] : [])];
  if (!state.tab || !tabs.some(t => t.id === state.tab)) state.tab = storageGet("sessionStorage", "tab") || tabs[0].id;
  if (!tabs.some(t => t.id === state.tab)) state.tab = tabs[0].id;
  const nav = document.getElementById("tabs");
  nav.innerHTML = tabs.map(t => `<button type="button" role="tab" data-tab="${esc(t.id)}" aria-selected="${t.id === state.tab}">${esc(t.name)}</button>`).join("");
  nav.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    state.tab = b.dataset.tab;
    storageSet("sessionStorage", "tab", state.tab);
    setupTabs();
    renderTab().catch(showError);
  }));
}

async function renderTab() {
  const el = document.getElementById("tab-panels");
  const tab = state.tab;
  if (tab === "timeline") return renderTimeline(el, await get("/api/weeks"));
  if (tab === "reports") return renderReports(el, await get("/api/reports"));
  if (tab === "alpaca") return renderMirror(el, await get("/api/mirror"));
  if (tab === "research") {
    const [research, costs] = await Promise.all([get("/api/research"), get("/api/costs")]);
    return renderResearch(el, research, costs);
  }
  const [detail, slots] = await Promise.all([get(`/api/agents/${encodeURIComponent(tab)}`),
                                             get(`/api/agents/${encodeURIComponent(tab)}/slots`)]);
  renderAgent(el, detail, slots);
}

// ------------------------------------------------------------------ agent tab

function gaugeHtml(g, color) {
  const ratio = g.limit ? g.value / g.limit : 0;
  let note = `${pct(g.value, false)} of ${pct(g.limit, false)} limit` + (g.trigger ? ` · forced cover above ${pct(g.trigger, false)}` : "");
  let badge = "";
  // Status always comes with an icon and words, never color alone.
  if (g.trigger && g.value > g.trigger) badge = `<span class="badge badge-critical">▲ forced cover at next open</span>`;
  else if (ratio > 1) badge = `<span class="badge badge-warning">↗ drifted above limit</span>`;
  else if (ratio >= 0.9) badge = `<span class="badge badge-warning">! near limit</span>`;
  return `<div class="gauge">
    <div class="label"><span>${esc(g.label)}${g.ticker ? ` <span class="muted">(${esc(g.ticker)})</span>` : ""}</span>${badge}</div>
    <div class="track" role="meter" aria-valuemin="0" aria-valuemax="${g.limit}" aria-valuenow="${g.value}" aria-label="${esc(g.label)}">
      <div class="fill" style="width:${Math.min(100, ratio * 100).toFixed(1)}%;background:${g.trigger && g.value > g.trigger ? "var(--critical)" : color}"></div></div>
    <div class="note">${note}</div></div>`;
}

function renderAgent(el, d, slotCurves) {
  const color = agentColor(d.agent);
  const slotColors = ["var(--bench)", "var(--series-1)", "var(--series-2)", "var(--series-3)", "#eda100", "#e87ba4"];
  const alloc = d.slots.filter(s => (s.pct_equity || 0) > 0)
    .map(s => `<div title="${esc(s.name)} ${pct(s.pct_equity, false)}" style="width:${(s.pct_equity * 100).toFixed(2)}%;background:${slotColors[s.slot] || "var(--bench)"}"></div>`).join("");
  const slotRows = d.slots.map(s => `<tr>
      <td style="white-space:nowrap"><span class="swatch" style="background:${slotColors[s.slot] || "var(--bench)"}"></span> ${s.slot ? s.slot : ""}</td>
      <td>${esc(s.name || "(unnamed)")}</td><td>${esc(s.type)}</td>
      <td class="num">${s.slot ? pct(s.target_allocation, false) : "–"}</td>
      <td class="num">${money(s.equity)}</td><td class="num">${pct(s.pct_equity, false)}</td>
      <td class="num ${signClass(s.since_inception)}">${pct(s.since_inception)}</td></tr>`).join("");
  const lvAgent = state.live && state.live.market_open && state.live.agents[d.agent];
  if (lvAgent) {
    for (const h of d.holdings) {
      const lp = lvAgent.positions.find(p => p.slot === h.slot && p.ticker === h.ticker);
      if (lp) Object.assign(h, { last: lp.last, value: lp.value, unrealized_pnl: lp.unrealized_pnl,
                                 pct_equity: lp.value / lvAgent.equity });
    }
  }
  const holdingRows = side => d.holdings.filter(h => h.side === side).map(h => `<tr>
      <td>${esc(h.ticker)}</td><td class="num">${h.slot}</td><td class="num">${h.shares}</td>
      <td class="num">${Number(h.avg_price).toFixed(2)}</td><td class="num">${Number(h.last).toFixed(2)}</td><td class="num">${money(h.value)}</td>
      <td class="num">${pct(h.pct_equity)}</td><td class="num ${signClass(h.unrealized_pnl)}">${money(h.unrealized_pnl)}</td>
      <td class="num">${h.stop_loss ?? "–"}</td><td class="num">${h.take_profit ?? "–"}</td></tr>`).join("")
      || `<tr><td colspan="10" class="muted">None</td></tr>`;
  const hHead = `<tr><th>Ticker</th><th class="num">Slot</th><th class="num">Shares</th><th class="num">Avg</th><th class="num">${lvAgent ? "Live" : "Last"}</th><th class="num">Value</th><th class="num">% equity</th><th class="num">Unrealized</th><th class="num">Stop</th><th class="num">Take profit</th></tr>`;
  const trades = d.recent_trades.map(t => `<tr><td>${esc(t.date)}</td><td>${esc(t.ticker)}</td><td>${t.side}</td>
      <td class="num">${t.shares}</td><td class="num">${t.fill_price}</td><td class="num">${money(t.value)}</td>
      <td class="num">${money(t.commission)}</td><td class="num ${signClass(t.realized_pnl)}">${money(t.realized_pnl)}</td>
      <td>${t.reason === "rebalance" ? "rebalance" : `<span class="badge badge-warning">${esc(t.reason.replace("_", " "))}</span>`}</td>
      <td class="why">${esc(t.note || "")}</td></tr>`).join("")
      || `<tr><td colspan="10" class="muted">No trades yet</td></tr>`;
  const code = d.slots.filter(s => s.code).map(s => `<details open><summary>Slot ${s.slot}: ${esc(s.name)}</summary><pre class="code">${esc(s.code)}</pre></details>`).join("")
      || `<p class="muted small">No code slots.</p>`;
  const c = d.costs;
  el.innerHTML = `<div class="tab-panel">
    <div class="grid-2">
      <section class="panel"><h2>Slots &amp; allocation</h2>
        <div class="alloc" aria-hidden="true">${alloc}</div>
        <div class="scroll"><table><tr><th></th><th>Name</th><th>Type</th><th class="num">Target</th><th class="num">Equity</th><th class="num">% now</th><th class="num">Since start</th></tr>${slotRows}</table></div>
        <p class="muted small">${lvAgent ? `Equity ${money(lvAgent.equity)} · cash ${money(d.cash)} · ● live ${esc(state.live.updated)} ET`
      : `Equity ${money(d.equity)} · cash ${money(d.cash)} · as of ${esc(d.as_of)} close`}</p>
      </section>
      <section class="panel"><h2>Exposure vs limits</h2>
        <div class="gauges">${d.exposures.map(g => gaugeHtml(g, color)).join("") || '<p class="muted">No equity.</p>'}</div>
      </section>
    </div>
    <section class="panel"><h2>Slot equity</h2><div id="slot-legend" class="legend"></div>
      <div class="chart-box small"><canvas id="slot-chart" aria-label="Equity per slot"></canvas></div></section>
    <section class="panel"><h2>Holdings</h2>
      <h3>Long</h3><div class="scroll"><table>${hHead}${holdingRows("long")}</table></div>
      <h3>Short</h3><div class="scroll"><table>${hHead}${holdingRows("short")}</table></div>
    </section>
    <section class="panel"><h2>Recent trades</h2>
      <div class="scroll"><table><tr><th>Date</th><th>Ticker</th><th>Side</th><th class="num">Shares</th><th class="num">Fill</th><th class="num">Value</th><th class="num">Commission</th><th class="num">Realized</th><th>Type</th><th>Why</th></tr>${trades}</table></div>
      <p class="muted small">All-time costs: commissions ${money(c.commissions)}, slippage ${money(c.slippage)}, borrow ${money(c.borrow_fees)}.</p>
    </section>
    <section class="panel"><h2>Current strategy code</h2>${code}</section>
  </div>`;
  const resolve = v => v.startsWith("var(") ? css(v.slice(4, -1)) : v;
  const ds = slotCurves.slots.map(s => ({ label: s.slot ? `${s.slot}: ${s.name || "slot"}` : "cash", borderColor: resolve(slotColors[s.slot] || "var(--bench)"),
                                          backgroundColor: resolve(slotColors[s.slot] || "var(--bench)"), data: s.equity }));
  renderLegend(document.getElementById("slot-legend"), ds.map(x => ({ label: x.label, color: x.borderColor })));
  lineChart("slot-chart", slotCurves.dates, ds, v => "$" + Math.round(v).toLocaleString());
}

// ------------------------------------------------------------------ timeline

function sideHtml(id, a) {
  if (!a) return `<div class="side" style="--c:var(${agentVar(id)})"><h3>${esc(label(id))}</h3><p class="muted">No turn.</p></div>`;
  const result = a.weekly_return == null ? "not scored yet"
    : `<span class="${signClass(a.weekly_return)}">${pct(a.weekly_return)}</span>${a.point ? " · ★ point" : ""}`;
  let body;
  if (a.hidden) {
    body = `<p class="hidden-note">🔒 Reasoning, positions and code are revealed after this week is scored.</p>`;
  } else if (a.status !== "accepted") {
    body = `<p class="neg">Turn failed: ${esc((a.error || "no valid decision").replace(/\.+$/, ""))}. Portfolio unchanged.</p>`;
  } else {
    const slots = (a.slots || []).map(s => `<div class="slotline">
        <b>Slot ${esc(s.slot)}: ${esc(s.name)}</b> <span class="muted">[${esc(s.type)}] ${pct(s.allocation, false)}</span><br>
        ${Object.entries(s.positions || {}).map(([t, w]) => `<span class="chip">${esc(t)} ${pct(w)}</span>`).join("")}
        <div class="reason">${esc(s.reason)}</div>
        ${s.code ? `<details><summary class="small">code</summary><pre class="code">${esc(s.code)}</pre></details>` : ""}
      </div>`).join("") || `<p class="muted">All cash.</p>`;
    body = `<p class="view">${esc(a.market_view)}</p>${slots}
      ${a.notes_to_self ? `<details><summary class="small">notes to self</summary><p class="reason">${esc(a.notes_to_self)}</p></details>` : ""}`;
  }
  return `<div class="side" style="--c:var(${agentVar(id)})"><h3>${esc(label(id))}</h3>
    <div class="result">${result} · ${a.attempts} submit${a.attempts === 1 ? "" : "s"}</div>${body}</div>`;
}

function renderTimeline(el, weeks) {
  if (!weeks.length) { el.innerHTML = `<p class="muted">No weeks yet.</p>`; return; }
  const ids = (state.status?.agents || []).map(a => a.id);
  el.innerHTML = `<div class="tab-panel">${weeks.map(w => `
    <article class="week">
      <div class="week-head"><h2>Week ${w.week}${w.simulated ? " (simulated)" : ""}</h2>
        <span class="muted small">decided ${esc(w.decision_date)} · traded ${esc(w.exec_date)} · ${esc(w.status)}
        ${w.spy_return != null ? ` · SPY benchmark <span class="${signClass(w.spy_return)}">${pct(w.spy_return)}</span>` : ""}
        ${w.bonus_winner ? ` · <span class="badge badge-good">$ bonus to ${esc(label(w.bonus_winner))}</span>` : ""}</span></div>
      <div class="grid-2">${ids.map(id => sideHtml(id, w.agents[id])).join("")}</div>
    </article>`).join("")}</div>`;
}

// ------------------------------------------------------------------ strategy reports

function reportSideHtml(id, a) {
  const head = `<h3>${esc(label(id))}</h3>`;
  if (!a) return `<div class="side" style="--c:var(${agentVar(id)})">${head}<p class="muted">No report.</p></div>`;
  if (a.status !== "accepted" || !a.report) {
    const why = a.status === "running" ? "Being written…" : `Report failed: ${esc(a.error || "no report")}`;
    return `<div class="side" style="--c:var(${agentVar(id)})">${head}<p class="${a.status === "running" ? "muted" : "neg"}">${why}</p></div>`;
  }
  const r = a.report;
  const tick = t => t ? `${esc(t.ticker)} <span class="${signClass(t.pnl)}">${money(t.pnl)}</span>` : "–";
  const slots = r.slots.map(s => `<div class="slotline">
      <b>Slot ${esc(s.slot)}: ${esc(s.name)}</b> <span class="muted">${s.type ? `[${esc(s.type)}] ` : ""}${pct(s.allocation, false)} of equity</span>
      <div class="report-nums small">
        <span>Return <b class="${signClass(s.slot_return)}">${pct(s.slot_return)}</b></span>
        <span>Contribution <b class="${signClass(s.contribution)}">${pct(s.contribution)}</b></span>
        <span>P&amp;L <b class="${signClass(s.pnl_usd)}">${money(s.pnl_usd)}</b></span>
        <span>Hit rate <b>${s.hit_rate == null ? "–" : pct(s.hit_rate, false)}</b> of ${s.positions}</span>
        <span>Costs <b>${money(s.costs.total)}</b></span>
        <span>Best ${tick(s.biggest_winner)}</span><span>Worst ${tick(s.biggest_loser)}</span>
      </div>
      <dl class="report-words">
        <dt>Worked</dt><dd>${esc(s.what_worked)}</dd>
        <dt>Didn't</dt><dd>${esc(s.what_didnt)}</dd>
        <dt>Improve</dt><dd>${esc(s.what_to_improve)}</dd>
        ${s.what_to_preserve.length ? `<dt>Preserve</dt><dd>${s.what_to_preserve.map(p =>
          `<div><span class="chip" title="times this lesson was marked">${esc(p.key)} ×${p.times}</span> ${esc(p.text)}</div>`).join("")}</dd>` : ""}
      </dl>
    </div>`).join("") || `<p class="muted">All cash that week.</p>`;
  return `<div class="side" style="--c:var(${agentVar(id)})">${head}
    <div class="result"><span class="${signClass(r.weekly_return)}">${pct(r.weekly_return)}</span>${r.point ? " · ★ point" : ""}</div>
    <p class="view">${esc(r.summary)}</p>${slots}
    <details><summary class="small">text version</summary><pre class="code">${esc(a.text)}</pre></details></div>`;
}

function renderReports(el, data) {
  const ids = (state.status?.agents || []).map(a => a.id);
  const lessons = ids.map(id => {
    const items = (data.preserve[id] || []).filter(p => p.times > 1).slice(0, 8);
    return `<div class="side" style="--c:var(${agentVar(id)})"><h3>${esc(label(id))}</h3>${items.length
      ? items.map(p => `<div><span class="chip">${esc(p.key)} ×${p.times}</span> <span class="small">${esc(p.text)}</span></div>`).join("")
      : `<p class="muted small">No repeated lessons yet.</p>`}</div>`;
  }).join("");
  const weeks = data.weeks.map(w => `
    <article class="week">
      <div class="week-head"><h2>Week ${w.week}${w.simulated ? " (simulated)" : ""}</h2>
        <span class="muted small">decided ${esc(w.decision_date)} · traded ${esc(w.exec_date)} · scored</span></div>
      <div class="grid-2">${ids.map(id => reportSideHtml(id, w.agents[id])).join("")}</div>
    </article>`).join("");
  el.innerHTML = `<div class="tab-panel">
    <p class="muted small">After each week is scored, each AI reviews its own strategies. The numbers come from the
      engine; the words are the AI's. Reports appear only once their week is scored.</p>
    <section class="panel"><h2>Lessons each AI keeps marking to preserve</h2><div class="grid-2">${lessons}</div></section>
    ${weeks || `<p class="muted">No reports yet: the first ones are written after week 1 is scored.</p>`}
  </div>`;
}

// ------------------------------------------------------------------ research & costs

function renderResearch(el, research, costs) {
  const ids = (state.status?.agents || []).map(a => a.id);
  const rRows = research.map(w => `<tr><td>${w.week}</td><td>${esc(w.decision_date)}</td>${ids.map(id => {
      const a = w.agents[id];
      if (!a) return `<td colspan="4" class="muted">–</td>`;
      const top = Object.entries(a.by_tool).slice(0, 3).map(([t, n]) => `${esc(t)} ×${n}`).join(", ");
      return `<td class="num">${a.tool_calls}</td><td class="num">${a.web_searches}</td><td class="num">${a.submit_attempts}</td>
              <td class="small">${esc(a.status)}${top ? `<br><span class="muted">${top}</span>` : ""}</td>`;
    }).join("")}</tr>`).join("") || `<tr><td colspan="${2 + ids.length * 4}" class="muted">No turns yet</td></tr>`;
  const rHead = `<tr><th rowspan="2">Week</th><th rowspan="2">Decision</th>${ids.map(id => `<th colspan="4">${esc(label(id))}</th>`).join("")}</tr>
    <tr>${ids.map(() => `<th class="num">Tools</th><th class="num">Web</th><th class="num">Submits</th><th>Status · top tools</th>`).join("")}</tr>`;
  const tRows = ids.map(id => { const c = costs.totals[id]; return `<tr><td>${esc(label(id))}</td>
      <td class="num">${money(c.commissions)}</td><td class="num">${money(c.slippage)}</td><td class="num">${money(c.borrow_fees)}</td>
      <td class="num">${(c.input_tokens || 0).toLocaleString()}</td><td class="num">${(c.output_tokens || 0).toLocaleString()}</td>
      <td class="num">${money(c.api_cost_usd)}</td></tr>`; }).join("");
  const wRows = costs.weekly.slice().reverse().map(w => `<tr><td>${w.week}</td>${ids.map(id => {
      const c = w.agents[id] || {};
      return `<td class="num">${money(c.commissions)}</td><td class="num">${money(c.slippage)}</td><td class="num">${money(c.borrow_fees)}</td>`;
    }).join("")}</tr>`).join("");
  // Web searches per weekly turn, side by side, against the soft limit.
  const lim = state.status?.web_search_soft_limit;
  const maxS = Math.max(1, lim || 0, ...research.flatMap(w => ids.map(id => w.agents[id]?.web_searches || 0)));
  const sRows = research.map(w => `<tr><td>${w.week}</td>${ids.map(id => {
      const a = w.agents[id];
      if (!a) return `<td class="muted">–</td>`;
      const over = lim != null && a.web_searches > lim;
      return `<td><div class="bar-cell"><div class="bar" style="width:${(a.web_searches / maxS * 100).toFixed(1)}%;background:var(${agentVar(id)})"></div>
        <span class="num ${over ? "neg" : ""}">${a.web_searches}${over ? " ▲" : ""}</span></div></td>`;
    }).join("")}</tr>`).join("") || `<tr><td colspan="${1 + ids.length}" class="muted">No turns yet</td></tr>`;
  const mRows = (costs.monthly || []).map(m => ids.map((id, i) => {
      const c = m.agents[id] || {};
      return `<tr>${i === 0 ? `<td rowspan="${ids.length}">${esc(m.month)}</td>` : ""}<td>${esc(label(id))}</td>
        <td class="num">${(c.turns || 0)} + ${(c.report_turns || 0)}</td>
        <td class="num">${((c.input_tokens || 0) + (c.cached_tokens || 0) + (c.cache_write_tokens || 0)).toLocaleString()}</td>
        <td class="num">${(c.output_tokens || 0).toLocaleString()}</td><td class="num">${(c.web_searches || 0)}</td>
        <td class="num">${money(c.est_ai_cost_usd || 0)}</td>
        <td class="num">${money((c.commissions || 0) + (c.slippage || 0) + (c.borrow_fees || 0))}</td></tr>`;
    }).join("")).join("") || `<tr><td colspan="8" class="muted">No usage yet</td></tr>`;
  const dLim = costs.daily_alert_usd;
  const dRows = (costs.daily || []).map(d => `<tr><td>${esc(d.day)}</td>${ids.map(id => {
      const c = d.agents[id];
      if (!c) return `<td class="num muted">–</td><td class="num muted">–</td>`;
      const over = dLim != null && c.est_ai_cost_usd > dLim;
      return `<td class="num">${c.turns} · ${(c.tokens || 0).toLocaleString()}</td>
              <td class="num ${over ? "neg" : ""}">${money(c.est_ai_cost_usd)}${over ? " ▲" : ""}</td>`;
    }).join("")}</tr>`).join("") || `<tr><td colspan="${1 + ids.length * 2}" class="muted">No turns in the last 30 days</td></tr>`;
  el.innerHTML = `<div class="tab-panel">
    <section class="panel"><h2>AI usage per day (last 30 days, UTC)</h2>
      <div class="scroll"><table><tr><th rowspan="2">Day</th>${ids.map(id => `<th colspan="2">${esc(label(id))}</th>`).join("")}</tr>
        <tr>${ids.map(() => `<th class="num">Turns · tokens</th><th class="num">Est. cost</th>`).join("")}</tr>${dRows}</table></div>
      <p class="muted small">Alert above ${money(dLim ?? 0)} per agent per day (▲). An alert only: agents are never paused.
        On subscriptions nothing is billed per token; the estimate prices the tokens at API rates.</p></section>
    <section class="panel"><h2>Web searches per weekly turn</h2>
      <p class="muted small">Soft limit ${lim ?? "–"} per turn, the same for both (written into each briefing, not enforced).
        ▲ = above the limit.</p>
      <div class="scroll"><table><tr><th>Week</th>${ids.map(id => `<th>${esc(label(id))}</th>`).join("")}</tr>${sRows}</table></div></section>
    <section class="panel"><h2>Usage and costs per month</h2>
      <div class="scroll"><table><tr><th>Month</th><th>Agent</th><th class="num">Turns + reports</th><th class="num">Input tokens</th>
        <th class="num">Output tokens</th><th class="num">Web searches</th><th class="num">Est. AI cost</th><th class="num">Trading costs</th></tr>${mRows}</table></div>
      <p class="muted small">Est. AI cost = the tokens priced at pay-per-token API rates (config.py). On Claude / ChatGPT
        subscriptions you don't pay this; it shows the size of the usage. Trading costs = commissions + slippage + borrow fees.</p></section>
    <section class="panel"><h2>Research activity</h2>
      <div class="scroll"><table>${rHead}${rRows}</table></div></section>
    <section class="panel"><h2>Costs (all time)</h2>
      <div class="scroll"><table><tr><th>Agent</th><th class="num">Commissions</th><th class="num">Slippage</th><th class="num">Borrow fees</th><th class="num">Input tokens</th><th class="num">Output tokens</th><th class="num">API cost</th></tr>${tRows}</table></div>
      <p class="muted small">API cost is $0 when agents run on subscriptions (Claude Code / Codex); tokens are still recorded.</p></section>
    <section class="panel"><h2>Trading costs per week</h2>
      <div class="scroll"><table><tr><th rowspan="2">Week</th>${ids.map(id => `<th colspan="3">${esc(label(id))}</th>`).join("")}</tr>
        <tr>${ids.map(() => `<th class="num">Commission</th><th class="num">Slippage</th><th class="num">Borrow</th>`).join("")}</tr>${wRows}</table></div></section>
  </div>`;
}

// ------------------------------------------------------------------ Alpaca mirror

function renderMirror(el, m) {
  const ids = (state.status?.agents || []).map(a => a.id);
  const modeText = { combined: "Both AIs share one Alpaca paper account. Alpaca shows the totals; this page shows whose part is whose.",
                     each: "Each AI has its own Alpaca paper account.", leader: "Your paper account copies the leading AI." }[m.mode] || "";
  const sync = m.last_sync ? `Last sync ${esc(m.last_sync.date)} (${esc(m.last_sync.ts)} UTC): paper equity ${money(m.last_sync.equity)}, ${m.last_sync.orders} order(s).` : "Not synced yet.";
  const holdings = m.holdings.length ? `<div class="scroll"><table><tr><th>Ticker</th>${ids.map(id => `<th class="num">${esc(label(id))}</th>`).join("")}<th class="num">In Alpaca (total)</th></tr>${
    m.holdings.map(h => `<tr><td>${esc(h.ticker)}</td>${ids.map(id => `<td class="num">${h.by_agent[id] ? h.by_agent[id].toFixed(4) : "–"}</td>`).join("")}<td class="num">${h.total.toFixed(4)}</td></tr>`).join("")}</table></div>`
    : `<p class="muted">${m.mode === "combined" ? "Nothing in the shared account yet." : "Holdings per AI are tracked in the combined mode."}</p>`;
  const orders = m.orders.length ? `<div class="scroll"><table><tr><th>Date</th><th>AI</th><th>Ticker</th><th>Side</th><th class="num">Shares</th><th>Alpaca order</th><th>Note</th></tr>${
    m.orders.map(o => `<tr><td>${esc(o.date)}</td><td><span class="swatch" style="background:var(${agentVar(o.agent)})"></span> ${esc(label(o.agent))}</td>
      <td>${esc(o.ticker)}</td><td>${esc(o.side)}</td><td class="num">${o.qty ? Math.abs(o.qty).toFixed(4) : "–"}</td>
      <td class="small">${o.order_ids.map(x => esc(x.slice(0, 8)) + "…").join(", ") || "–"}</td><td class="small muted">${esc(o.note || "")}</td></tr>`).join("")}</table></div>`
    : `<p class="muted">No orders sent to Alpaca yet.</p>`;
  el.innerHTML = `<div class="tab-panel">
    <section class="panel"><h2>Alpaca paper account</h2><p class="muted">${esc(modeText)} ${sync}</p>
      <p class="muted small">Orders carry the AI's name in Alpaca too: open an order there and look at its Client Order ID (e.g. arena-claude-…).</p></section>
    <section class="panel"><h2>Whose shares are whose</h2>${holdings}</section>
    <section class="panel"><h2>Orders sent to Alpaca, by AI</h2>${orders}</section></div>`;
}

// ------------------------------------------------------------------ admin

const dialog = document.getElementById("admin-dialog");
const tokenInput = document.getElementById("admin-token");
document.getElementById("admin-btn").addEventListener("click", () => {
  tokenInput.value = storageGet("sessionStorage", "adminToken") || "";
  document.getElementById("admin-msg").textContent = state.status?.paused ? "The league is paused." : "The league is running.";
  dialog.showModal();
});
async function admin(action) {
  const msg = document.getElementById("admin-msg");
  storageSet("sessionStorage", "adminToken", tokenInput.value);
  try {
    const r = await fetch(`${API}/api/admin/${action}`, { method: "POST", headers: { Authorization: `Bearer ${tokenInput.value}` } });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || r.statusText);
    msg.textContent = body.paused ? "League paused." : "League resumed.";
    refresh();
  } catch (e) { msg.textContent = `Failed: ${e.message}`; }
}
document.getElementById("pause-btn").addEventListener("click", () => admin("pause"));
document.getElementById("resume-btn").addEventListener("click", () => admin("resume"));

// ------------------------------------------------------------------ main

// Is the setup page available? Only on the computer running the league.
let controlAvailable = null;
async function checkControl() {
  if (controlAvailable === null && !API) {
    try { controlAvailable = (await fetch("/api/control/state")).ok; } catch (_) { controlAvailable = false; }
    document.getElementById("setup-link").hidden = !controlAvailable;
  }
  return controlAvailable;
}

async function refresh() {
  try {
    await checkControl();
    state.status = await get("/api/status");
    try { state.mirrorMode = (await get("/api/mirror")).mode; } catch (_) { state.mirrorMode = "off"; }
    document.getElementById("welcome").hidden = !(controlAvailable && !state.status.agents.length);
    renderStatus(state.status);
    const [sb, eq] = await Promise.all([get("/api/scoreboard"), get("/api/equity")]);
    try { state.live = await get("/api/live"); } catch (_) { state.live = null; }
    renderStatus(state.status);
    renderScoreboard(sb);
    // Before the league starts there's nothing to chart or browse.
    const started = state.status.agents.length > 0;
    document.querySelector('[aria-labelledby="eq-title"]').hidden = !started;
    document.getElementById("tabs").hidden = !started;
    document.getElementById("tab-panels").hidden = !started;
    if (!started) { showError(null); return; }
    renderEquity(eq);
    setupTabs();
    if (state.status.agents.length) await renderTab();
    showError(null);
  } catch (e) {
    // First run: there's no league database yet, so go straight to setup.
    if (controlAvailable && /doesn't exist yet/.test(e.message)) { location.href = "setup.html"; return; }
    showError(e);
  }
}

refresh();
setInterval(refresh, 60_000);
