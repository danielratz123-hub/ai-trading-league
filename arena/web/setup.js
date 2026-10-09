// Setup page. Talks only to this computer's own server (/api/control/*).
"use strict";

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let st = null;          // last /api/control/state
let pollTimer = null;

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    // This header is what proves the request comes from this page (see control.py).
    headers: { "Content-Type": "application/json", "X-Arena-Control": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch("/api/control" + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(data.detail || r.statusText);
    err.status = r.status;
    throw err;
  }
  return data;
}

function showError(e) { $("error").textContent = e ? e.message : ""; $("error").hidden = !e; }

function badge(step, done, text) {
  const b = document.querySelector(`#${step} .step-badge`);
  b.className = "step-badge badge " + (done ? "badge-good" : "badge-warning");
  b.textContent = done ? "✓ " + (text || "Done") : "! " + (text || "To do");
  document.getElementById(step).classList.toggle("done", !!done);
}

function msg(id, text, ok) {
  const el = $(id);
  el.textContent = text || "";
  el.className = "small " + (ok === true ? "pos" : ok === false ? "neg" : "muted");
}

// ------------------------------------------------------------------ render

function render() {
  const env = st.env, L = st.league, busy = st.task.status === "running";
  $("clock").textContent = `US market time: ${st.now_et}`;

  // 1. Alpaca
  const keysSet = env.ALPACA_API_KEY.set && env.ALPACA_SECRET_KEY.set;
  $("alpaca-key").placeholder = env.ALPACA_API_KEY.set ? `saved ${env.ALPACA_API_KEY.hint}` : "paste your API Key ID";
  $("alpaca-secret").placeholder = env.ALPACA_SECRET_KEY.set ? `saved ${env.ALPACA_SECRET_KEY.hint}` : "paste your Secret Key";
  badge("step-alpaca", keysSet, keysSet ? "Keys saved" : "Keys needed");

  // 2. Agents
  const names = { claude: "Claude", gpt: "ChatGPT (Codex)" };
  const checked = Object.keys(st.agents).length > 0;
  const allOk = checked && Object.values(st.agents).every(a => a.installed && a.signed_in);
  $("agent-cards").innerHTML = ["claude", "gpt"].map(id => {
    const a = st.agents[id];
    let line = "Press “Check status”.", cls = "muted";
    if (a) {
      if (!a.installed) { line = "Not installed yet."; cls = "neg"; }
      else if (!a.signed_in) { line = `Installed, not signed in. ${a.login}`; cls = "neg"; }
      else { line = `Ready: ${a.login}`; cls = "pos"; }
    }
    return `<div class="card"><div class="who">${esc(names[id])}</div>
      <p class="small ${cls}">${esc(line)}</p>
      <div class="row"><button type="button" data-login="${id}" ${a && !a.installed ? "disabled" : ""}>Sign in</button></div>
      ${a && a.installed ? `<p class="muted small">${esc(a.version)}</p>` : ""}</div>`;
  }).join("");
  document.querySelectorAll("[data-login]").forEach(b => b.onclick = () => signIn(b.dataset.login));
  badge("step-agents", allOk, allOk ? "Both ready" : checked ? "Needs attention" : "Not checked");
  if (!modelsDirty) {
    selectModel("claude-model", env.CLAUDE_MODEL.value || "claude-opus-5-5");
    selectModel("gpt-model", env.GPT_MODEL.value || "");
    if (env.REASONING_EFFORT.value) $("effort").value = env.REASONING_EFFORT.value;
  }

  // 3. Data
  const hasData = !!L.prices_up_to && L.tickers > 400;
  $("data-status").textContent = L.prices_up_to
    ? `Prices for ${L.tickers} tickers, up to ${L.prices_up_to}. Calendar until ${L.calendar_until || "–"}.`
    : "No data yet.";
  $("download-data").textContent = L.prices_up_to ? "Update data" : "Download data";
  badge("step-data", hasData, hasData ? "Up to " + L.prices_up_to : "No data");

  // 4. League
  const w = L.latest_week;
  $("league-status").textContent = L.started
    ? `Running: week ${w ? w.week_no : 1} (${w ? w.status : "starting"})${L.paused ? " · PAUSED" : ""}.`
    : (keysSet && hasData ? "Ready to start." : "Finish steps 1–3 first.");
  $("kickoff").disabled = busy || L.started || !keysSet || !hasData;
  $("kickoff").hidden = L.started;
  badge("step-league", L.started, L.started ? "Started" : "Not started");

  // 5. Scheduler
  const on = st.scheduler.running;
  $("sched-status").innerHTML = on
    ? `<span class="pos">● Running.</span> It will take care of everything from here.`
    : `<span class="neg">● Stopped.</span> ${L.started ? "Start it so the league keeps going." : "Start the league first."}`;
  $("sched-start").disabled = on || !L.started;
  $("sched-stop").disabled = !on;
  $("autostart").checked = env.AUTOSTART_SCHEDULER.value === "1";
  badge("step-scheduler", on, on ? "Running" : "Stopped");
  $("jobs").innerHTML = L.recent_jobs.length ? `<table><tr><th>Job</th><th>For</th><th>Started</th><th>Result</th></tr>${
    L.recent_jobs.map(j => `<tr><td>${esc(j.job)}</td><td>${esc(j.key)}</td><td>${esc((j.started_at || "").slice(0, 16).replace("T", " "))}</td>
      <td class="${j.exit_code === 0 ? "pos" : j.exit_code == null ? "muted" : "neg"}">${j.exit_code === 0 ? "✓ ok" : j.exit_code == null ? "running" : "✗ failed (see data\\logs)"}</td></tr>`).join("")}</table>` : "";

  // 6. Alpaca mirror
  const mm = env.MIRROR_MODE.value || (env.MIRROR_PAPER.value === "1" ? "leader" : "off");
  if (!mirrorDirty) $("mirror-mode").value = mm;
  $("mirror-each").hidden = $("mirror-mode").value !== "each";
  $("mirror-gpt-key").placeholder = env.ALPACA_MIRROR_GPT_KEY.set ? `saved ${env.ALPACA_MIRROR_GPT_KEY.hint}` : "paste the second account's Key ID";
  $("mirror-gpt-secret").placeholder = env.ALPACA_MIRROR_GPT_SECRET.set ? `saved ${env.ALPACA_MIRROR_GPT_SECRET.hint}` : "paste its Secret Key";
  $("mirror-claude-key").placeholder = env.ALPACA_MIRROR_CLAUDE_KEY.set ? `saved ${env.ALPACA_MIRROR_CLAUDE_KEY.hint}` : "(uses step 1's keys)";
  $("mirror-claude-secret").placeholder = env.ALPACA_MIRROR_CLAUDE_SECRET.set ? `saved ${env.ALPACA_MIRROR_CLAUDE_SECRET.hint}` : "(uses step 1's keys)";
  const gptKeys = env.ALPACA_MIRROR_GPT_KEY.set && env.ALPACA_MIRROR_GPT_SECRET.set;
  const mirrorReady = mm !== "off";
  badge("step-mirror", mm !== "off" && (mm !== "each" || gptKeys),
        { off: "Off", leader: "Leader", combined: "One account" }[mm] || (gptKeys ? "Each AI" : "Claude only: add ChatGPT's keys"));
  $("mirror-now").disabled = busy || mm === "off" || !mirrorReady || !L.started;

  // Buttons that start long tasks are disabled while one runs.
  ["download-data", "test-agents", "install-agents", "practice"].forEach(id => $(id).disabled = busy);
  renderTask(st.task);
}

function renderTask(t) {
  if (!t.name) { $("task-panel").hidden = true; return; }
  const labels = { "mirror-now": "Copying to Alpaca", "download-data": "Downloading market data", "test-agents": "Test turn", "kickoff": "Starting the league",
                   "practice-week": "Practice run", "install-agents": "Installing the AI apps" };
  $("task-panel").hidden = false;
  $("task-title").textContent = labels[t.name] || t.name;
  const b = $("task-badge");
  b.className = "badge " + ({ running: "badge-warning", done: "badge-good", failed: "badge-critical" }[t.status] || "");
  b.textContent = { running: "… working (you can leave this page open)", done: "✓ finished", failed: "✗ failed: see the log" }[t.status] || t.status;
  const log = $("task-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent = t.log.join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
}

// ------------------------------------------------------------------ models

let catalog = null, modelsDirty = false;

function fillSelect(id, models, defaultLabel) {
  const sel = $(id);
  sel.innerHTML = (defaultLabel ? `<option value="">${esc(defaultLabel)}</option>` : "") +
    models.map(m => `<option value="${esc(m.id)}">${esc(m.name)}${m.tier === "older" ? " (older)" : ""}</option>`).join("");
}

// Keep a saved model selectable even if it's not in the list (e.g. custom).
function selectModel(id, value) {
  const sel = $(id);
  if (value && ![...sel.options].some(o => o.value === value)) sel.add(new Option(value + " (saved)", value));
  sel.value = value;
  showNote(id);
}

function showNote(id) {
  const list = id === "claude-model" ? catalog?.claude : catalog?.gpt;
  const m = (list || []).find(x => x.id === $(id).value);
  $(id === "claude-model" ? "claude-note" : "gpt-note").textContent =
    m ? m.note : ($(id).value ? "" : "Whatever Codex uses by default for your plan.");
}

async function loadModels() {
  try {
    catalog = await api("/models");
    fillSelect("claude-model", catalog.claude, null);
    fillSelect("gpt-model", catalog.gpt, "(your plan's default)");
    const name = (list, id) => (list.find(m => m.id === id) || { name: id }).name;
    $("pairs").innerHTML = catalog.pairs.map(p =>
      `<button type="button" class="ghost pair" data-c="${esc(p.claude)}" data-g="${esc(p.gpt)}">${esc(p.label)}: ` +
      `${esc(name(catalog.claude, p.claude))} vs ${esc(name(catalog.gpt, p.gpt))}</button>`).join(" ");
    document.querySelectorAll(".pair").forEach(b => b.onclick = () => {
      modelsDirty = true;
      selectModel("claude-model", b.dataset.c);
      selectModel("gpt-model", b.dataset.g);
    });
    $("gpt-source").textContent = catalog.gpt_source === "live"
      ? "The ChatGPT list comes live from your ChatGPT plan (via Codex)."
      : "Couldn't read your ChatGPT plan's model list, so this is a built-in list. Install and sign in to Codex for the live list.";
    if (st) render();
  } catch (e) { showError(e); }
}
["claude-model", "gpt-model", "effort"].forEach(id => $(id).addEventListener("change", () => { modelsDirty = true; showNote(id); }));

// ------------------------------------------------------------------ actions

async function refresh() {
  try {
    st = await api("/state");
    showError(null);
    render();
  } catch (e) {
    if (e.status === 403 || e.status === 404) { $("locked").hidden = false; document.querySelectorAll(".step").forEach(s => s.hidden = true); }
    else showError(e);
  }
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refresh, st && st.task.status === "running" ? 1500 : 10000);
}

async function act(fn) {
  try { await fn(); showError(null); } catch (e) { showError(e); }
  refresh();
}

$("save-alpaca").onclick = () => act(async () => {
  const key = $("alpaca-key").value.trim(), secret = $("alpaca-secret").value.trim();
  if (key || secret) {
    const body = {};
    if (key) body.ALPACA_API_KEY = key;
    if (secret) body.ALPACA_SECRET_KEY = secret;
    await api("/env", body);
    $("alpaca-key").value = ""; $("alpaca-secret").value = "";
  }
  msg("alpaca-msg", "Testing…");
  const r = await api("/test-alpaca", {});
  msg("alpaca-msg", r.message, r.ok);
});

$("check-agents").onclick = () => act(async () => {
  $("check-agents").disabled = true;
  $("check-agents").textContent = "Checking…";
  try { await api("/agents/check", {}); }
  finally { $("check-agents").disabled = false; $("check-agents").textContent = "Check status"; }
});

async function signIn(id) {
  await act(async () => {
    const r = await api(`/agents/${id}/login`, {});
    alert(r.message);
  });
}

$("install-agents").onclick = () => act(() => api("/run/install-agents", {}));
$("test-agents").onclick = () => act(() => api("/run/test-agents", {}));
$("download-data").onclick = () => act(() => api("/run/download-data", {}));
$("kickoff").onclick = () => act(async () => {
  if (!confirm("Start the league now? Both AIs will make their first decisions (this takes several minutes).")) return;
  await api("/run/kickoff", {});
});
$("practice").onclick = () => act(() => api("/run/practice-week", { start: $("practice-start").value, weeks: +$("practice-weeks").value }));
$("save-models").onclick = () => act(async () => {
  await api("/env", { CLAUDE_MODEL: $("claude-model").value, GPT_MODEL: $("gpt-model").value,
                      REASONING_EFFORT: $("effort").value });
  modelsDirty = false;
  msg("models-msg", "Saved. Used from the next turn on.", true);
});
let mirrorDirty = false;
$("mirror-mode").addEventListener("change", () => { mirrorDirty = true; $("mirror-each").hidden = $("mirror-mode").value !== "each"; });
$("save-mirror").onclick = () => act(async () => {
  const body = { MIRROR_MODE: $("mirror-mode").value };
  for (const [id, key] of [["mirror-gpt-key", "ALPACA_MIRROR_GPT_KEY"], ["mirror-gpt-secret", "ALPACA_MIRROR_GPT_SECRET"],
                           ["mirror-claude-key", "ALPACA_MIRROR_CLAUDE_KEY"], ["mirror-claude-secret", "ALPACA_MIRROR_CLAUDE_SECRET"]]) {
    const v = $(id).value.trim();
    if (v) body[key] = v;
    $(id).value = "";
  }
  await api("/env", body);
  mirrorDirty = false;
  if (body.MIRROR_MODE === "off") { msg("mirror-msg", "Saved: nothing is copied to Alpaca.", true); return; }
  msg("mirror-msg", "Testing the paper account(s)…");
  const r = await api("/test-mirror", {});
  msg("mirror-msg", r.message, r.ok);
});
$("mirror-now").onclick = () => act(async () => {
  if (!confirm("Copy the AIs' current portfolios into the Alpaca paper account(s) now? This places paper orders.")) return;
  await api("/run/mirror-now", {});
});
$("sched-start").onclick = () => act(() => api("/scheduler/start", {}));
$("sched-stop").onclick = () => act(() => api("/scheduler/stop", {}));
$("autostart").onchange = e => act(() => api("/env", { AUTOSTART_SCHEDULER: e.target.checked ? "1" : "0" }));

refresh().then(loadModels);
