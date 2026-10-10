// Learning section of the V3 dashboard (GET /api/learning only; read-only).
// Hidden until the learning layer's tables exist. Everything an agent wrote is escaped.
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const NAMES = { claude: "Claude", gpt: "GPT" };
  const COLORS = { claude: "#E8875F", gpt: "#4C8DF6" };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const p = (v, d = 2) => v == null ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(d) + "%";   // values are already in %
  const dd = v => v == null ? "–" : Number(v).toFixed(2) + "%";
  const cls = v => v == null ? "" : (v < 0 ? "down" : "up");
  const day = s => s ? String(s).slice(0, 10) : "–";
  const who = a => `<span class="agent-cell" style="font-weight:400"><i class="sw" aria-hidden="true" style="background:${COLORS[a] || "#8E8E93"}"></i>${esc(NAMES[a] || a)}</span>`;
  const pill = s => `<span class="ln-pill m ${esc(s)}">${esc(String(s || "").toUpperCase())}</span>`;
  const empty = (n, text) => `<tr><td colspan="${n}" class="empty">${esc(text)}</td></tr>`;

  function render(L) {
    const r = L.regime;
    $("ln-sub").textContent = `${L.enabled ? "on" : "off (tracking tables only)"} · risk-adjusted scoring from week ${L.start_week}`
      + (r ? ` · regime ${r.regime || "n/a"} (${r.day})` : "");
    $("ln-scores").innerHTML = L.weekly_scores.map(s => `<tr>
      <td class="pl m">${s.week_no}</td><td>${who(s.agent)}</td>
      <td class="m" style="color:var(--muted)">${s.mode === "risk_adjusted" ? "RISK-ADJ." : "RAW"}</td>
      <td class="r m ${cls(s.return_pct)}">${p(s.return_pct)}</td><td class="r m">${dd(s.max_drawdown_pct)}</td>
      <td class="r m">${s.broke_hard_limit ? '<span style="color:var(--down)">0 (limit broken)</span>' : Number(s.score).toFixed(3)}</td>
      <td class="r m">${s.point_won ? "1" : "–"}</td><td class="r m pr">${s.bonus_added ? "$" + Number(s.bonus_added).toLocaleString() : "–"}</td></tr>`).join("")
      || empty(8, "No week scored since the learning layer started.");
    $("ln-regime").innerHTML = L.strategy_by_regime.map(x => `<tr>
      <td class="pl">${who(x.agent)}</td><td>${esc(x.strategy)}</td><td class="m">${esc(x.regime || "–")}</td>
      <td class="r m">${x.trades}</td><td class="r m ${cls(x.avg_1w_pct)}">${p(x.avg_1w_pct, 3)}</td>
      <td class="r m pr">${x.win_rate_pct == null ? "–" : Number(x.win_rate_pct).toFixed(1) + "%"}</td></tr>`).join("")
      || empty(6, "Filled in once decisions have a 1-week outcome.");
    $("ln-reports").innerHTML = L.reports.map(x => `<article class="card ln-report">
      <h4>${who(x.agent)}</h4><span class="m">WEEK ${x.week_no} · ${esc(day(x.period_start))} → ${esc(day(x.period_end))}</span>
      <dl><dt>Summary</dt><dd>${esc(x.summary)}</dd><dt>Worked</dt><dd>${esc(x.worked)}</dd><dt>Didn't work</dt><dd>${esc(x.didnt_work)}</dd>
      <dt>To improve</dt><dd>${esc(x.to_improve)}</dd><dt>To preserve</dt><dd>${esc(x.to_preserve || "–")}</dd></dl></article>`).join("")
      || `<p class="empty">No weekly report yet.</p>`;
    $("ln-lessons").innerHTML = L.lessons.map(x => `<tr>
      <td class="pl">${who(x.agent)}</td><td>${pill(x.status)}</td>
      <td class="ln-wrap"><span class="m" style="color:var(--faint)">${esc(x.key)}</span><br>${esc(x.lesson)}</td>
      <td class="m">${esc(x.regime || "any")}</td><td class="r m">${x.times_confirmed}×</td>
      <td class="pr m" style="color:var(--muted)">${esc(x.source)}${x.strategy ? " · slot " + x.slot : ""}</td></tr>`).join("")
      || empty(6, "No lessons yet.");
    $("ln-versions").innerHTML = L.versions.map(x => `<tr>
      <td class="pl m" style="color:var(--muted)">${esc(day(x.created_at))}</td><td>${who(x.agent)}</td>
      <td>${esc(x.strategy)} <span class="m" style="color:var(--faint)">slot ${x.slot}</span></td>
      <td class="m">v${x.version}</td><td>${pill(x.role)}</td><td class="m" style="color:var(--muted)">${esc(x.change_kind)}</td>
      <td class="pr ln-wrap">${esc(x.change_reason || "")}${x.hypothesis ? `<br><span style="color:var(--faint)">hypothesis: ${esc(x.hypothesis)}</span>` : ""}
        <br><span class="m" style="color:var(--faint)">${esc(JSON.stringify(x.params))}</span></td></tr>`).join("")
      || empty(7, "No strategy versions yet.");
    $("ln-shadow").innerHTML = L.shadow.map(x => `<tr>
      <td class="pl m">${x.week_no}</td><td>${who(x.agent)}</td><td>${esc(x.strategy)} <span class="m" style="color:var(--faint)">v${x.version}</span></td>
      <td class="r m">${p(x.return_pct)} <small style="color:var(--faint)">dd ${dd(x.max_drawdown_pct)}</small></td>
      <td class="r m">${p(x.champion_return_pct)} <small style="color:var(--faint)">dd ${dd(x.champion_max_drawdown_pct)}</small></td>
      <td>${pill(x.verdict)}</td><td class="pr ln-wrap">${esc(x.hypothesis || "")}</td></tr>`).join("")
      || empty(7, "No challenger has been tested yet.");
    $("ln-rejected").innerHTML = L.rejected.map(x => `<tr>
      <td class="pl m" style="color:var(--muted)">${esc(String(x.ts).slice(0, 16).replace("T", " "))}</td><td>${who(x.agent)}</td>
      <td class="m">${esc(x.source)}</td><td class="m">${esc(x.kind)}${x.slot != null ? " · slot " + x.slot : ""}</td>
      <td class="pr ln-wrap">${esc(x.reason)}</td></tr>`).join("")
      || empty(5, "Nothing refused so far.");
  }

  async function refresh() {
    try {
      const r = await fetch("/api/learning", { credentials: "same-origin" });
      if (!r.ok) return;
      const L = await r.json();
      const show = !!L.available;
      $("learning").hidden = !show; $("learning-link").hidden = !show;
      if (show) render(L);
    } catch (e) { /* the rest of the dashboard reports errors; this section just stays as it was */ }
  }
  refresh();
  setInterval(refresh, 120000);
})();
