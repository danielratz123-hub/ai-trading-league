// Backtest lab section of the V3 dashboard (GET /api/lab only; read-only).
// Hidden until the lab's tables exist. Everything an agent wrote is escaped.
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const NAMES = { claude: "Claude", gpt: "GPT" };
  const COLORS = { claude: "#E8875F", gpt: "#4C8DF6" };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const p = (v, d = 1) => v == null ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(d) + "%";   // values are already in %
  const n = (v, d = 2) => v == null ? "–" : Number(v).toFixed(d);
  const cls = v => v == null ? "" : (v < 0 ? "down" : "up");
  const who = a => {
    const m = /^agent:(.+)$/.exec(a || ""); const id = m ? m[1] : a;
    if (!NAMES[id]) return `<span class="m" style="color:var(--muted)">${esc(a)}</span>`;
    return `<span class="agent-cell" style="font-weight:400"><i class="sw" aria-hidden="true" style="background:${COLORS[id]}"></i>${esc(NAMES[id])}</span>`;
  };
  const pill = s => `<span class="ln-pill m ${esc(String(s).split(" ")[0])}">${esc(String(s || "").toUpperCase())}</span>`;
  const empty = (cols, text) => `<tr><td colspan="${cols}" class="empty">${esc(text)}</td></tr>`;
  const STATE_TEXT = { up_low: "Up · calm", up_normal: "Up · normal vol", up_high: "Up · high vol",
                       down_low: "Down · calm", down_normal: "Down · normal vol", down_high: "Down · high vol" };
  const state = s => `<span class="lab-state ${esc(s || "none")}">${esc(STATE_TEXT[s] || s || "no label")}</span>`;
  const caveat = c => `<span class="lab-caveat" title="${esc(c)}">survivorship bias: optimistic</span>`;

  function bar(value, target, label, fmt) {
    const pct = target ? Math.max(0, Math.min(100, (value || 0) / target * 100)) : 0;
    return `<div class="lab-bar"><div class="lab-bar-l m"><span>${esc(label)}</span><span>${fmt(value)} / ${fmt(target)}</span></div>
      <div class="lab-bar-t"><div class="lab-bar-f${pct >= 100 ? " done" : ""}" style="width:${pct}%"></div></div></div>`;
  }

  function summaryRow(s, title, cav) {
    if (!s) return `<tr><td class="pl m" style="color:var(--muted)">${esc(title)}</td><td colspan="6" class="pr" style="color:var(--faint)">not run</td></tr>`;
    return `<tr><td class="pl m" style="color:var(--muted)">${esc(title)}</td>
      <td class="r m ${cls(s.total_return_pct)}">${p(s.total_return_pct)}</td><td class="r m">${n(s.max_drawdown_pct, 1)}%</td>
      <td class="r m">${n(s.risk_adjusted_score, 3)}</td><td class="r m">${n(s.period_score, 3)}</td>
      <td class="r m">${s.trades ?? "–"}</td><td class="r m pr">${p(s.vs_spy_pct)} ${cav}</td></tr>`;
  }

  function stateTable(s) {
    const rows = Object.entries((s && s.by_state) || {}).filter(([k]) => k !== "unlabelled");
    if (!rows.length) return "";
    return `<table class="tbl lab-mini"><thead><tr class="m"><th class="pl">MARKET STATE</th><th class="r">DAYS</th><th class="r">ANNUAL</th>
      <th class="r">TRADES</th><th class="r pr">WIN RATE</th></tr></thead><tbody>${rows.map(([k, v]) => `<tr>
      <td class="pl">${state(k)}</td><td class="r m">${v.days ?? "–"}</td><td class="r m ${cls(v.annual_return_pct)}">${p(v.annual_return_pct)}</td>
      <td class="r m">${v.trades ?? "–"}</td><td class="r m pr">${v.win_rate_pct == null ? "–" : n(v.win_rate_pct, 0) + "%"}</td></tr>`).join("")}</tbody></table>`;
  }

  function experiment(x, cav) {
    const sh = x.shadow;
    const prog = sh ? `<div class="lab-prog">
        ${bar(sh.trades, sh.min_trades, "shadow trades", v => v ?? 0)}
        ${bar(sh.prob_better == null ? 0 : sh.prob_better * 100, sh.min_prob * 100, "probability it's truly better", v => Math.round(v || 0) + "%")}
        ${bar(sh.weeks == null ? 0 : Math.max(sh.weeks, 0), sh.max_weeks, "weeks in shadow (time limit)", v => n(v, 1))}
        <p class="m lab-note">score ${n(sh.score, 3)} vs champion ${n(sh.champion_score, 3)} (needs +${n(sh.score_margin, 2)}) · since ${esc(sh.start_day)}</p>
      </div>` : "";
    const steps = x.steps.map(s => `<li class="${s.passed ? "ok" : "no"}"><span class="m">${s.passed ? "✓" : "✗"} ${esc(s.stage.replace("_", " "))}</span>
      <span class="lab-why">${esc(s.reason)}</span></li>`).join("");
    return `<article class="card lab-exp">
      <header><h4>${who(x.agent)} <span class="m" style="color:var(--faint)">slot ${x.slot ?? "–"}</span></h4>${pill(x.status)}</header>
      <p class="m lab-idea">${esc(x.idea_key)} · <b>${x.attempts}</b> backtest ${x.attempts === 1 ? "try" : "tries"} of this idea</p>
      <table class="tbl lab-mini"><thead><tr class="m"><th class="pl"></th><th class="r">RETURN</th><th class="r">MAX DD</th>
        <th class="r">WEEKLY SCORE</th><th class="r">PERIOD SCORE</th><th class="r">TRADES</th><th class="r pr">VS SPY</th></tr></thead>
        <tbody>${summaryRow(x.walk_forward, "walk-forward", cav)}${summaryRow(x.holdout, "holdout (once)", cav)}</tbody></table>
      ${stateTable(x.walk_forward)}
      ${prog}
      <details><summary class="m">every step (${x.steps.length})</summary><ol class="lab-steps">${steps}</ol></details>
    </article>`;
  }

  function render(L) {
    const cav = caveat(L.caveat);
    $("lab-sub").textContent = `evidence rules ${L.evidence_rules ? "on" : "off"} · last ${L.holdout_months} months locked as a holdout`;
    $("lab-caveat-text").textContent = L.caveat;
    const ms = L.market_state;
    $("lab-now").innerHTML = ms ? `${state(ms.label)}
      <span class="m lab-inputs">SPY ${n(ms.inputs.spy_close, 2)} vs 200-day ${n(ms.inputs.sma_200, 2)} · 20-day volatility ${n((ms.inputs.realized_vol || 0) * 100, 1)}% · ${esc(ms.day)}</span>`
      : `<span class="empty">No market state yet.</span>`;
    $("lab-strip").innerHTML = (L.state_history || []).map(s => `<i class="lab-cell ${esc(s.label)}" title="${esc(s.day)}: ${esc(STATE_TEXT[s.label] || s.label)}"></i>`).join("");

    $("lab-capital").innerHTML = (L.capital || []).map(c => `<tr>
      <td class="pl">${who(c.agent)}</td><td>${esc(c.strategy)} <span class="m" style="color:var(--faint)">slot ${c.slot}</span></td>
      <td class="r m">${n(c.share_pct, 1)}%</td><td class="r m pr ${cls(c.week_change_pct)}">${c.week_change_pct == null ? "–" : (c.week_change_pct >= 0 ? "+" : "") + n(c.week_change_pct, 1) + " pts"}</td></tr>`).join("")
      || empty(4, "No snapshots yet.");
    $("lab-dial").innerHTML = `<b class="m">${n(L.risk_dial.level_pct, 0)}%</b> <span class="lab-note">${esc(L.risk_dial.note)}</span>
      <br><span class="lab-note">${esc(L.allocator.note)}</span>`;

    $("lab-exps").innerHTML = (L.experiments || []).map(x => experiment(x, cav)).join("")
      || `<p class="empty">No challenger yet. ${L.evidence_rules ? "Agents propose them in the weekly review." : "Challengers start once the evidence rules are on."}</p>`;

    const piv = {};
    for (const r of L.strategy_by_state || []) {
      const k = r.strategy + "|" + r.market_state;
      piv[k] = piv[k] || { strategy: r.strategy, state: r.market_state, agent: r.agent };
      piv[k][r.source.startsWith("backtest") ? "bt" : "live"] = r;
    }
    const cell = r => r ? `<td class="r m">${r.trades}</td><td class="r m ${cls(r.avg_pct)}">${p(r.avg_pct, 2)}</td><td class="r m">${r.win_rate_pct == null ? "–" : n(r.win_rate_pct, 0) + "%"}</td>`
                       : `<td class="r m" colspan="3" style="color:var(--faint)">–</td>`;
    $("lab-strats").innerHTML = Object.values(piv).map(r => `<tr>
      <td class="pl">${esc(r.strategy)}</td><td>${state(r.state)}</td>${cell(r.live)}${cell(r.bt)}<td class="pr">${r.bt ? cav : ""}</td></tr>`).join("")
      || empty(9, "Filled in once live decisions have outcomes and strategies have been backtested.");

    $("lab-adaptive").innerHTML = (L.adaptive_study || []).map(a => `<tr>
      <td class="pl">${esc(a.variant)}</td><td class="m">${esc(a.purpose === "holdout" ? "holdout (once)" : "before holdout")}</td>
      <td class="r m ${cls(a.total_return_pct)}">${p(a.total_return_pct)}</td><td class="r m">${n(a.max_drawdown_pct, 1)}%</td>
      <td class="r m">${n(a.risk_adjusted_score, 3)}</td><td class="r m">${n(a.period_score, 3)}</td><td class="pr">${cav}</td></tr>`).join("")
      || empty(7, "The adaptive study hasn't been run on this database (python -m arena.lab adaptive-study).");

    $("lab-runs").innerHTML = (L.recent_runs || []).map(r => `<tr>
      <td class="pl m" style="color:var(--muted)">${esc(String(r.created_at).slice(0, 16).replace("T", " "))}</td><td>${who(r.requested_by)}</td>
      <td class="m">${esc(r.idea_key)}</td><td class="r m">#${r.attempt_no}</td><td class="m">${esc(r.purpose.replace("_", " "))}</td>
      <td>${pill(r.status)}</td>
      <td class="r m ${cls(r.results && r.results.total_return_pct)}">${r.results ? p(r.results.total_return_pct) : "–"}</td>
      <td class="r m">${r.results ? n(r.results.risk_adjusted_score, 3) : "–"}</td>
      <td class="pr ln-wrap">${r.error ? esc(r.error) : (r.results ? cav : "")}</td></tr>`).join("")
      || empty(9, "No backtests yet.");
  }

  async function refresh() {
    try {
      const r = await fetch("/api/lab", { credentials: "same-origin" });
      if (!r.ok) return;
      const L = await r.json();
      const show = !!L.available;
      $("lab").hidden = !show; $("lab-link").hidden = !show;
      if (show) render(L);
    } catch (e) { /* the rest of the dashboard reports errors; this section just stays as it was */ }
  }
  refresh();
  setInterval(refresh, 120000);
})();
