// V3 dashboard. Read-only: it only calls GET endpoints of this server and
// the live price stream (/api/stream). Everything an agent wrote is escaped.
(function () {
  "use strict";

  const COLORS = { claude: "#E8875F", gpt: "#4C8DF6", spy: "#8E8E93" };
  const NAMES = { claude: "Claude", gpt: "GPT", spy: "SPY" };
  const REDUCE = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = id => document.getElementById(id);
  const S = { agent: "claude", range: "ALL", hover: -1, quotes: {}, liveOn: false, first: true, draw: 1 };

  // ---------------------------------------------------------------- helpers
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = v => v == null ? "–" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const smoney = v => v == null ? "–" : (v >= 0 ? "+" : "") + money(v);
  const pct = (v, d = 2) => v == null ? "–" : (v >= 0 ? "+" : "") + (v * 100).toFixed(d) + "%";
  const ppct = (v, d = 1) => v == null ? "–" : (v * 100).toFixed(d) + "%";
  const cls = (v, light) => v == null ? "" : (v < 0 ? (light ? "down-l" : "down") : (light ? "up-l" : "up"));
  const px = v => v == null ? "–" : Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const qty = v => v == null ? "–" : Math.abs(v) >= 100 ? Math.abs(v).toFixed(1) : Math.abs(v).toFixed(Math.abs(v) >= 1 ? 2 : 4);
  const sw = (c, dashed) => dashed ? `<i class="sw dashed" aria-hidden="true"></i>` : `<i class="sw" aria-hidden="true" style="background:${c}"></i>`;
  const fmtDay = d => new Date(d + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const fmtDayLong = d => new Date(d + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" }).replace(",", "").toUpperCase();
  const ease = t => 1 - Math.pow(1 - t, 3);

  async function get(path) {
    const r = await fetch(path, { credentials: "same-origin" });
    if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); throw new Error("Not signed in"); }
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return r.json();
  }

  function countUp(el, to, fmt, dur = 1200) {
    const from = el._v ?? (S.first ? (to > 0 ? to * 0.96 : 0) : to);
    el._v = to;
    if (REDUCE || from === to) { el.textContent = fmt(to); return; }
    const t0 = performance.now();
    const step = now => {
      const t = Math.min(1, (now - t0) / dur);
      el.textContent = fmt(from + (to - from) * ease(t));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ---------------------------------------------------------------- data
  const D = {};
  async function load() {
    const [status, sb, eq, ov, closed] = await Promise.all([
      get("/api/status"), get("/api/scoreboard"), get("/api/equity"), get("/api/overview"), get("/api/closed-trades?limit=60")]);
    Object.assign(D, { status, sb, eq, ov, closed });
    const ids = status.agents.map(a => a.id);
    const per = await Promise.all(ids.map(a => Promise.all([
      get(`/api/agents/${a}`), get(`/api/strategies/${a}`), get(`/api/reasoning/${a}`)])));
    D.detail = {}; D.strat = {}; D.reason = {};
    ids.forEach((a, i) => { [D.detail[a], D.strat[a], D.reason[a]] = per[i]; });
    D.ids = ids;
  }

  // Estimated live values (display only): cash + shares x live price.
  function liveEquity(a) {
    const d = D.detail?.[a];
    if (!d || !S.liveOn) return null;
    return d.cash + d.holdings.reduce((s, h) => s + h.shares * (S.quotes[h.ticker] ?? h.last), 0);
  }

  // ---------------------------------------------------------------- 2. ticker
  function renderTicker() {
    const items = (D.ov?.ticker || []).map(t => {
      const last = S.quotes[t.ticker] ?? t.last;
      const ch = t.prev_close ? last / t.prev_close - 1 : t.change;
      return `<span class="tk m" data-tk="${esc(t.ticker)}"><span class="dots">${t.agents.map(a => `<i style="background:${COLORS[a] || COLORS.spy}"></i>`).join("")}</span>
        <b>${esc(t.ticker)}</b><span class="p" data-f="p">${px(last)}</span><span data-f="c" class="${cls(ch)}">${pct(ch)}</span></span>`;
    });
    // Twice, so the strip scrolls seamlessly.
    $("ticker").innerHTML = items.length ? items.join("") + items.join("") : `<span class="tk m"><span class="p">No positions yet</span></span>`;
    $("ticker").style.animationDuration = Math.max(24, items.length * 4) + "s";
  }

  // ---------------------------------------------------------------- 3. tiles
  function renderTiles() {
    const sb = D.sb, start = D.ov?.starting_cash || 1000, since = D.ov?.since ? fmtDay(D.ov.since) : "the start";
    const rows = sb.agents.map(r => ({ id: r.agent, equity: liveEquity(r.agent) ?? r.equity, ret: r.return_since_start, pts: r.points }));
    const ranked = [...rows].sort((a, b) => (b.pts - a.pts) || ((b.ret ?? -9) - (a.ret ?? -9)));
    const lead = ranked.length > 1 && (ranked[0].pts > ranked[1].pts || (ranked[0].ret ?? 0) > (ranked[1].ret ?? 0)) ? ranked[0].id : null;
    const ord = n => ["1st", "2nd", "3rd"][n] || `${n + 1}th`;
    const tiles = rows.map((r, i) => {
      const rank = ranked.findIndex(x => x.id === r.id);
      const isLead = r.id === lead;
      return `<div class="tile lift glow rise ${isLead ? "lead" : ""}" style="animation-delay:${240 + i * 80}ms">
        <div class="tile-top"><span class="who">${sw(COLORS[r.id])}${NAMES[r.id] || esc(r.id)}</span>
          <span class="badge m ${isLead ? "solid" : ""}" style="${isLead ? `background:${COLORS[r.id]}` : ""}">${ord(rank)} · ${r.pts} pt${r.pts === 1 ? "" : "s"}</span></div>
        <div class="tile-eq m"><span data-eq="${esc(r.id)}"></span>${S.liveOn ? `<span class="est m">ESTIMATED</span>` : ""}</div>
        <div class="tile-ret m ${cls(r.ret)}">${pct(r.ret)} <span class="since">since ${esc(since)}</span></div></div>`;
    });
    const spyRet = sb.spy_return;
    tiles.push(`<div class="tile lift glow rise" style="animation-delay:${240 + rows.length * 80}ms">
      <div class="tile-top"><span class="who">${sw(COLORS.spy, true)}SPY benchmark</span><span class="badge m">reference</span></div>
      <div class="tile-eq m"><span data-eq="spy"></span></div>
      <div class="tile-ret m ${cls(spyRet)}">${pct(spyRet)} <span class="since">since ${esc(since)}</span></div></div>`);
    $("tiles").innerHTML = tiles.join("");
    rows.forEach(r => countUp(document.querySelector(`[data-eq="${r.id}"]`), r.equity, money));
    countUp(document.querySelector(`[data-eq="spy"]`), spyRet == null ? start : start * (1 + spyRet), money);
    const wk = D.status.latest_week;
    $("eyebrow").textContent = wk ? `WEEK ${wk.week_no} · LIVE PAPER LEAGUE` : "LIVE PAPER LEAGUE";
  }

  // ---------------------------------------------------------------- 4. chart
  function series() {
    const eq = D.eq, start = eq.starting_cash || 1000;
    let dates = [...eq.dates];
    const s = D.ids.map(a => ({ id: a, c: COLORS[a], arr: [...(eq.agents[a]?.equity || [])] }));
    s.push({ id: "spy", c: COLORS.spy, dashed: true, arr: eq.spy_index.map(v => v == null ? null : start * v) });
    if (S.liveOn && dates.length) {                 // today's estimated value as the last point
      dates.push("now");
      s.forEach(x => x.arr.push(x.id === "spy" ? (S.quotes.SPY && D.spyBase ? start * S.quotes.SPY / D.spyBase : null) : liveEquity(x.id)));
    }
    const cut = S.range === "1W" ? Math.max(0, dates.length - 6) : 0;
    return { dates: dates.slice(cut), s: s.map(x => ({ ...x, arr: x.arr.slice(cut) })) };
  }

  function renderChart(animate) {
    const el = $("chart");
    const { dates, s } = series();
    const n = dates.length;
    if (n < 2) { el.innerHTML = `<p class="empty">The chart starts after the first two trading days.</p>`; $("axis").innerHTML = ""; return; }
    const vals = s.flatMap(x => x.arr).filter(v => v != null);
    const pad = Math.max(4, (Math.max(...vals) - Math.min(...vals)) * 0.08);
    const lo = Math.min(...vals) - pad, hi = Math.max(...vals) + pad;
    const X = i => (i / (n - 1)) * 1000, Y = v => 260 - ((v - lo) / (hi - lo)) * 220;
    const path = arr => arr.map((v, i) => v == null ? "" : `${i && arr[i - 1] != null ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join(" ");
    const lead = s[0];
    const lastI = lead.arr.length - 1;
    el.innerHTML = `<svg viewBox="0 0 1000 300" preserveAspectRatio="none" aria-hidden="true">
      <defs><linearGradient id="v3fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${lead.c}" stop-opacity="0.28"/><stop offset="1" stop-color="${lead.c}" stop-opacity="0"/></linearGradient>
        <clipPath id="v3clip"><rect id="clip" x="0" y="0" width="${animate && !REDUCE ? 0 : 1000}" height="300"/></clipPath></defs>
      ${[40, 113, 186].map(y => `<line x1="0" y1="${y}" x2="1000" y2="${y}" stroke="rgba(255,255,255,0.05)"/>`).join("")}
      <line x1="0" y1="260" x2="1000" y2="260" stroke="rgba(255,255,255,0.10)"/>
      <g clip-path="url(#v3clip)">
        <path d="${path(lead.arr)} L${X(lastI)} 260 L0 260 Z" fill="url(#v3fill)"/>
        ${[...s].reverse().map(x => `<path d="${path(x.arr)}" fill="none" stroke="${x.c}" stroke-width="${x.dashed ? 1.5 : x.id === "claude" ? 2.75 : 2.25}" ${x.dashed ? 'stroke-dasharray="5 5"' : ""} vector-effect="non-scaling-stroke" stroke-linejoin="round"/>`).join("")}
      </g></svg>
      <div class="cross" id="cross" hidden></div>
      ${s.map(x => `<span class="pt" data-pt="${x.id}"><span class="ring" style="background:${x.c}" ${x.dashed ? "hidden" : ""}></span><span class="core" style="background:${x.c}"></span></span>`).join("")}
      <div class="tip" id="tip" role="status" hidden></div>`;
    S.chart = { dates, s, X, Y, n };
    placeDots(-1, animate);
    const ticks = Math.min(4, n);
    $("axis").innerHTML = Array.from({ length: ticks }, (_, i) => {
      const d = dates[Math.round(i * (n - 1) / (ticks - 1))];
      return `<span>${d === "now" ? "now" : esc(fmtDay(d))}</span>`;
    }).join("");
    if (animate && !REDUCE) {                         // lines draw in
      const rect = $("clip"), t0 = performance.now(), dur = 1800;
      const step = now => { const t = Math.min(1, (now - t0) / dur); rect.setAttribute("width", (ease(t) * 1000).toFixed(1));
        if (t < 1) requestAnimationFrame(step); else placeDots(-1, false); };
      requestAnimationFrame(step);
    }
  }

  function placeDots(hover, hiddenUntilDrawn) {
    const c = S.chart;
    if (!c) return;
    const k = hover >= 0 ? hover : c.n - 1;
    c.s.forEach(x => {
      const el = document.querySelector(`[data-pt="${x.id}"]`);
      if (!el) return;
      const v = x.arr[k];
      el.style.opacity = v == null || hiddenUntilDrawn ? 0 : 1;
      if (v == null) return;
      el.style.left = (k / (c.n - 1)) * 100 + "%";
      el.style.top = (c.Y(v) / 300) * 100 + "%";
      const ring = el.querySelector(".ring");
      if (ring && !x.dashed) ring.hidden = hover >= 0;          // pulse only at the live end
    });
    const cross = $("cross"), tip = $("tip");
    if (hover < 0) { cross.hidden = tip.hidden = true; return; }
    const left = (k / (c.n - 1)) * 100;
    cross.hidden = tip.hidden = false;
    cross.style.left = left + "%";
    tip.style.left = Math.min(Math.max(left, 14), 86) + "%";
    const d = c.dates[k];
    tip.innerHTML = `<div class="tip-date m">${d === "now" ? "NOW · ESTIMATED" : esc(fmtDayLong(d))}</div>` +
      c.s.map(x => `<div class="tip-row"><span>${sw(x.c, x.dashed)}${NAMES[x.id] || esc(x.id)}</span><span class="m">${money(x.arr[k])}</span></div>`).join("");
  }

  function bindChart() {
    const el = $("chart");
    const pick = clientX => {
      if (!S.chart) return;
      const r = el.getBoundingClientRect();
      const i = Math.round(Math.min(Math.max((clientX - r.left) / r.width, 0), 1) * (S.chart.n - 1));
      if (i !== S.hover) { S.hover = i; placeDots(i); }
    };
    el.addEventListener("mousemove", e => pick(e.clientX));
    el.addEventListener("mouseleave", () => { S.hover = -1; placeDots(-1); });
    el.addEventListener("touchstart", e => pick(e.touches[0].clientX), { passive: true });
    el.addEventListener("touchmove", e => pick(e.touches[0].clientX), { passive: true });
    el.addEventListener("touchend", () => setTimeout(() => { S.hover = -1; placeDots(-1); }, 1200));
    $("ranges").addEventListener("click", e => {
      const b = e.target.closest("button[data-range]");
      if (!b || b.dataset.range === S.range) return;
      S.range = b.dataset.range;
      $("ranges").querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
      S.hover = -1;
      renderChart(true);
    });
  }

  // ---------------------------------------------------------------- 5. leaderboard
  function renderBoard() {
    const ex = D.ov?.leaderboard || { agents: {} };
    const start = D.ov?.starting_cash || 1000;
    const fmtScore = (tot, last) => tot == null ? null : `Score ${tot.toFixed(2)}` + (last != null ? ` · last wk ${last.toFixed(2)}` : "");
    const rows = D.sb.agents.map(r => {
      const e = ex.agents[r.agent] || {};
      const best = e.best_strategy ? `${e.best_strategy.name} · ${pct(e.best_strategy.return, 1)}` : null;
      const sc = fmtScore(r.total_score, r.last_week_score);
      return { id: r.agent, label: NAMES[r.id] || r.agent, isBench: false,
               equity: liveEquity(r.agent) ?? r.equity, total: r.return_since_start, week: r.week_to_date,
               pts: String(r.points), win: e.win_rate == null ? "–" : ppct(e.win_rate, 0),
               best: [best, sc].filter(Boolean).join(" · ") || "–" };
    });
    rows.sort((a, b) => (b.total ?? -9) - (a.total ?? -9));
    const benches = Array.isArray(D.sb.benchmarks) && D.sb.benchmarks.length ? D.sb.benchmarks : [
      { player_id: "bench_spy", label: "SPY buy-and-hold", cumulative_return: D.sb.spy_return, last_week_return: ex.spy_this_week },
      { player_id: "bench_random", label: "Random picker", cumulative_return: null, last_week_return: null }
    ];
    benches.forEach(b => {
      const pid = b.player_id || b.id;
      const isSpy = pid === "bench_spy";
      const total = b.cumulative_return ?? (isSpy ? D.sb.spy_return : null);
      const week = (isSpy ? ex.spy_this_week : null) ?? b.last_week_return;
      rows.push({
        id: pid, label: b.label || pid, isBench: true,
        equity: total == null ? null : start * (1 + total),
        total, week, pts: "", win: "—",
        best: fmtScore(b.total_score, b.last_week_score) || (isSpy ? "Buy and hold" : "Equal-weight 5 stocks")
      });
    });
    let agentIdx = 0;
    $("board-body").innerHTML = rows.map(r => {
      const rankHtml = r.isBench
        ? `<span class="rank m">–</span>`
        : `<span class="rank m ${agentIdx++ === 0 ? "top" : ""}">${agentIdx}</span>`;
      const nameHtml = r.isBench
        ? `<span class="agent-cell">${sw(COLORS.spy, true)}${esc(r.label)} <span class="bench-tag m">benchmark</span></span>`
        : `<span class="agent-cell">${sw(COLORS[r.id])}${NAMES[r.id] || esc(r.id)}</span>`;
      return `<tr class="${r.isBench ? "bench-row" : ""}">
        <td class="pl">${rankHtml}</td>
        <td>${nameHtml}</td>
        <td class="r m">${money(r.equity)}</td><td class="r m ${cls(r.total)}">${pct(r.total)}</td>
        <td class="r m ${cls(r.week)}">${pct(r.week)}</td><td class="r m">${esc(r.pts)}</td>
        <td class="r m" style="color:var(--ink-2)">${esc(r.win)}</td><td class="pr" style="color:var(--muted)">${esc(r.best)}</td></tr>`;
    }).join("");
    const o = D.ov;
    $("board-sub").textContent = o ? `1 point per weekly win · $${o.bonus_amount.toLocaleString()} bonus every ${o.bonus_every_weeks} weeks` : "";
  }

  // ---------------------------------------------------------------- 6. strategies
  function renderStrategies() {
    const a = S.agent, st = D.strat[a], c = COLORS[a];
    const shades = [1, 0.8, 0.62, 0.46, 0.32];
    if (!st || !st.strategies.length) {
      $("strategies").innerHTML = `<p class="empty">No strategies yet: they appear once the first week has been traded.</p>`;
      return;
    }
    const segs = st.strategies.map((s, i) => `<div style="flex:0 0 0%;background:${c};opacity:${shades[i] ?? 0.25}" data-w="${(s.allocation || 0) * 100}" title="${esc(s.name)} ${ppct(s.allocation)}"></div>`).join("")
      + `<div style="flex:0 0 0%;background:#C7C7CC" data-w="${(st.cash_pct || 0) * 100}" title="cash ${ppct(st.cash_pct)}"></div>`;
    $("strategies").innerHTML = `
      <div class="alloc-head"><span style="font-weight:500">Capital allocation</span>
        <span class="m">cash ${money(st.cash)} · long ${ppct(st.long_pct, 0)} · short ${ppct(st.short_pct, 0)}</span></div>
      <div class="alloc-bar" aria-hidden="true">${segs}</div>
      <div class="cards">${st.strategies.map(s => `
        <article class="scard lift">
          <div class="scard-top"><span class="m">SLOT 0${s.slot}${s.type === "code" ? " · CODE" : ""}</span><span class="tag m ${esc(s.change)}">${esc(s.change)}</span></div>
          <h3>${esc(s.name)}</h3>
          <p>${esc(s.thesis || "No thesis given.")}</p>
          <div class="scard-nums">
            <div><div class="lbl">Allocation</div><div class="big m">${ppct(s.allocation, 0)}</div></div>
            <div style="text-align:right"><div class="lbl">${s.since_inception == null ? "Return" : "Since start"}</div>
              <div class="big m ${cls(s.since_inception, true)}">${s.since_inception == null ? "new" : pct(s.since_inception, 1)}</div></div>
          </div>
          <div class="bar" aria-hidden="true"><div style="background:${c}" data-w="${Math.min((s.allocation || 0) / 0.3, 1) * 100}"></div></div>
        </article>`).join("")}</div>`;
    requestAnimationFrame(() => {                      // bars grow in
      $("strategies").querySelectorAll(".alloc-bar div").forEach(d => d.style.flexBasis = d.dataset.w + "%");
      $("strategies").querySelectorAll(".bar div").forEach(d => d.style.width = d.dataset.w + "%");
    });
  }

  // ---------------------------------------------------------------- 7. positions + reasoning
  function renderPositions() {
    const a = S.agent, d = D.detail[a];
    $("pos-agent").textContent = NAMES[a] || a;
    const at = D.status.as_of ? `${D.status.as_of} ${D.status.as_of_field === "open" ? "open" : "close"}` : "";
    $("pos-note").textContent = S.liveOn ? `live · ${S.liveLabel || ""}` : (at ? `last price ${at}` : "");
    const rows = [...(d?.holdings || [])].sort((x, y) => Math.abs(y.value) - Math.abs(x.value));
    $("pos-body").innerHTML = rows.map(h => {
      const last = S.quotes[h.ticker] ?? h.last;
      const value = h.shares * last, pnl = (last - h.avg_price) * h.shares;
      const side = h.shares < 0 ? "SHORT" : "LONG";
      return `<tr data-tk="${esc(h.ticker)}" data-sh="${h.shares}" data-avg="${h.avg_price}">
        <td class="pl"><div class="m" style="font-weight:500">${esc(h.ticker)}${h.price_stale ? ' <span title="price from an older day" style="color:var(--warn)">*</span>' : ""}</div><div class="tick-sub">${esc(h.strategy)}</div></td>
        <td><span class="side m ${side}">${side}</span></td>
        <td class="r m" style="color:var(--ink-2)">${qty(h.shares)}</td>
        <td class="r m" style="color:var(--muted)">${money(h.avg_price)}</td>
        <td class="r m" data-f="last">${money(last)}</td>
        <td class="r m" data-f="value">${money(Math.abs(value))}</td>
        <td class="r m pr ${cls(pnl)}" data-f="pnl">${smoney(pnl)}</td></tr>`;
    }).join("") || `<tr><td colspan="7" class="empty">No open positions.</td></tr>`;

    const r = D.reason[a];
    $("reason").innerHTML = `<h3>Latest reasoning</h3>` +
      (r && r.items.length ? r.items.map(t => `<div class="thought"><span class="m">${esc(t.when)}</span><p>${esc(t.text)}</p></div>`).join("")
        : `<div class="thought"><p>Shown once this agent's first decision has traded.</p></div>`) +
      `<div class="next"><span class="m">NEXT DECISION · ${esc((r?.next_decision || "Sat 10:00 ET").toUpperCase())}</span>
        <i class="breathe" style="width:88%"></i><i class="breathe" style="width:64%;animation-delay:200ms"></i></div>`;

    const g = d?.exposures || [];
    $("gauges").innerHTML = g.map(x => {
      const ratio = x.limit ? Math.min(Math.max(x.value, 0) / x.limit, 1) : 0;
      const color = ratio > 0.85 ? "var(--warn)" : COLORS[a];
      return `<div class="gauge lift"><span class="lbl">${esc(x.label)}${x.ticker ? ` · ${esc(x.ticker)}` : ""}</span>
        <span class="val m">${ppct(x.value)} <small>/ ${ppct(x.limit, 0)} limit</small></span>
        <div class="track"><div class="fill" style="background:${color}" data-w="${ratio * 100}"></div></div>
        ${x.trigger ? `<span class="note">engine covers above ${ppct(x.trigger, 0)}</span>` : ""}</div>`;
    }).join("");
    requestAnimationFrame(() => $("gauges").querySelectorAll(".fill").forEach(f => f.style.width = f.dataset.w + "%"));
  }

  // ---------------------------------------------------------------- 9. completed trades
  function renderClosed() {
    const rows = D.closed || [];
    $("closed-body").innerHTML = rows.map(c => `<tr>
      <td class="pl m" style="color:var(--muted)">${esc(fmtDay(c.closed))} · open</td>
      <td><span class="agent-cell" style="font-weight:400">${sw(COLORS[c.agent])}${NAMES[c.agent] || esc(c.agent)}</span></td>
      <td class="m" style="font-weight:500">${esc(c.ticker)}</td>
      <td><span class="side m ${esc(c.side)}">${esc(c.side)}</span></td>
      <td class="r m" style="color:var(--ink-2)">${money(c.entry)} → ${money(c.exit)}</td>
      <td class="r m" style="color:var(--muted)">${c.held_days}d</td>
      <td class="r m ${cls(c.net_pnl)}">${smoney(c.net_pnl)}</td>
      <td class="pr why">${esc(c.why)}</td></tr>`).join("") || `<tr><td colspan="8" class="empty">No position has been closed yet.</td></tr>`;
  }

  // ---------------------------------------------------------------- agent switch (spring pill + cross-fade)
  function bindSwitch() {
    $("switch").addEventListener("click", e => {
      const b = e.target.closest("button[data-agent]");
      if (!b || b.dataset.agent === S.agent || !D.detail?.[b.dataset.agent]) return;
      S.agent = b.dataset.agent;
      $("switch").querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
      placeIndicator();
      const panes = document.querySelectorAll(".swap");
      panes.forEach(p => p.classList.add("out"));
      setTimeout(() => { renderStrategies(); renderPositions(); panes.forEach(p => p.classList.remove("out")); }, REDUCE ? 0 : 260);
    });
  }
  function placeIndicator() {
    const btn = $("switch").querySelector(`button[data-agent="${S.agent}"]`);
    const ind = $("switch-ind");
    ind.style.transform = `translateX(${btn.offsetLeft - 4}px)`;
    ind.style.background = COLORS[S.agent];
  }

  // ---------------------------------------------------------------- live prices (display only)
  function onStatus(s) {
    S.lastStatus = s;
    S.liveOn = s.status === "live";
    S.liveLabel = s.label;
    const close = D.status?.as_of ? `${D.status.as_of} ${D.status.as_of_field || "close"}` : null;
    $("status-text").textContent = LiveFeed.pillText(s, close);
    document.querySelector(".status").classList.toggle("live", S.liveOn);
    $("status-ping").hidden = !S.liveOn;
  }

  function onPrices(changes, quotes) {
    S.quotes = quotes;
    if (!D.detail) return;
    for (const [t, { price, prev }] of Object.entries(changes)) {
      const up = prev == null || price >= prev;
      document.querySelectorAll(`.tk[data-tk="${CSS.escape(t)}"]`).forEach(el => {
        const tk = (D.ov?.ticker || []).find(x => x.ticker === t);
        const p = el.querySelector('[data-f="p"]'), c = el.querySelector('[data-f="c"]');
        p.textContent = px(price);
        if (tk?.prev_close) { const ch = price / tk.prev_close - 1; c.textContent = pct(ch); c.className = cls(ch); }
        LiveFeed.flash(p, up);
      });
      document.querySelectorAll(`#pos-body tr[data-tk="${CSS.escape(t)}"]`).forEach(tr => {
        const sh = Number(tr.dataset.sh), avg = Number(tr.dataset.avg), pnl = (price - avg) * sh;
        const last = tr.querySelector('[data-f="last"]');
        last.textContent = money(price);
        tr.querySelector('[data-f="value"]').textContent = money(Math.abs(sh * price));
        const pc = tr.querySelector('[data-f="pnl"]');
        pc.textContent = smoney(pnl); pc.className = `r m pr ${cls(pnl)}`;
        LiveFeed.flash(last, up);
      });
    }
    if (!S.liveOn) return;
    for (const a of D.ids || []) {                    // estimated equity on the tiles
      const el = document.querySelector(`[data-eq="${a}"]`);
      const v = liveEquity(a);
      if (el && v != null && Math.abs((el._v ?? 0) - v) > 0.005) { const up = v >= (el._v ?? v); countUp(el, v, money, 600); LiveFeed.flash(el, up); }
    }
  }

  // ---------------------------------------------------------------- boot
  function reveal() {
    const els = document.querySelectorAll(".sr:not(.in)");
    if (REDUCE || !("IntersectionObserver" in window)) { els.forEach(e => e.classList.add("in")); return; }
    const io = new IntersectionObserver(entries => entries.forEach(en => {
      if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
    }), { rootMargin: "0px 0px -8% 0px" });
    els.forEach(e => io.observe(e));
  }

  async function refresh() {
    try {
      await load();
      $("error").hidden = true;
      D.spyBase = D.ov.spy_base;
      renderTicker(); renderTiles(); renderChart(S.first); renderBoard();
      if (!D.detail[S.agent]) S.agent = D.ids[0] || "claude";
      renderStrategies(); renderPositions(); renderClosed(); placeIndicator();
      if (S.lastStatus) onStatus(S.lastStatus);       // the pill needs the close date loaded above
      if (S.first) { reveal(); S.first = false; }
    } catch (e) {
      if (/Not signed in/.test(e.message)) return;
      $("error").textContent = "Couldn't load the league data: " + e.message;
      $("error").hidden = false;
    }
  }

  fetch("/api/control/state", { credentials: "same-origin" }).then(r => { if (r.ok) $("setup-link").hidden = false; }).catch(() => {});
  bindChart(); bindSwitch();
  window.addEventListener("resize", () => placeIndicator());
  refresh();
  setInterval(refresh, 60000);
  new LiveFeed({ onStatus, onPrices }).start();
})();
