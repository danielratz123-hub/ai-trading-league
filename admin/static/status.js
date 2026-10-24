(function () {
  var statusSection = document.getElementById("status");
  var updatedEl = document.getElementById("status-updated");
  var refreshBtn = document.getElementById("status-refresh");

  var bannerEl = document.getElementById("status-banner");
  var bannerIconEl = document.getElementById("status-banner-icon");
  var bannerTitleEl = document.getElementById("status-banner-title");
  var reasonsEl = document.getElementById("status-reasons");

  var leagueBadgeEl = document.getElementById("league-badge");
  var leagueStatusEl = document.getElementById("league-status");
  var leagueUptimeEl = document.getElementById("league-uptime");
  var leagueRestartEl = document.getElementById("league-restart");
  var leagueVersionEl = document.getElementById("league-version");

  var hubBadgeEl = document.getElementById("hub-badge");
  var hubStatusEl = document.getElementById("hub-status");

  var schedulerBasisEl = document.getElementById("scheduler-basis");
  var schedulerErrorEl = document.getElementById("scheduler-error");
  var schedulerTbodyEl = document.getElementById("scheduler-tbody");

  var brokerBadgeEl = document.getElementById("broker-badge");
  var brokerStatusEl = document.getElementById("broker-status");
  var brokerLastSuccessEl = document.getElementById("broker-last-success");
  var brokerMarketEl = document.getElementById("broker-market");

  var agentsErrorEl = document.getElementById("agents-error");
  var agentsTbodyEl = document.getElementById("agents-tbody");

  var machineDbSummaryEl = document.getElementById("machine-db-summary");
  var cpuLabelEl = document.getElementById("cpu-label");
  var cpuProgressEl = document.getElementById("cpu-progress");
  var cpuBarEl = document.getElementById("cpu-bar");
  var ramLabelEl = document.getElementById("ram-label");
  var ramProgressEl = document.getElementById("ram-progress");
  var ramBarEl = document.getElementById("ram-bar");
  var diskLabelEl = document.getElementById("disk-label");
  var diskProgressEl = document.getElementById("disk-progress");
  var diskBarEl = document.getElementById("disk-bar");
  var dbSizeEl = document.getElementById("db-size");

  var logsCountEl = document.getElementById("logs-count");
  var logsServiceEl = document.getElementById("logs-service");
  var logsStatusEl = document.getElementById("logs-status");
  var logsListEl = document.getElementById("logs-list");

  var JOB_ORDER = [
    "execute",
    "daily",
    "decide",
    "risk-check",
    "daily-review",
    "weekly-review"
  ];

  var AGENT_ORDER = [
    { id: "claude", name: "Claude" },
    { id: "gpt", name: "GPT" }
  ];

  var state = {
    service: "all",
    statusReqId: 0,
    logsReqId: 0
  };

  function clearChildren(el) {
    if (!el) return;
    while (el.firstChild) {
      el.removeChild(el.firstChild);
    }
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function formatClock(date) {
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds());
  }

  function formatTimestamp(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) +
      " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  function formatDuration(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds) || seconds < 0) return "—";
    var s = Math.round(seconds);
    if (s < 60) return s + "s";
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    var remS = s % 60;
    if (d > 0) return d + "d " + h + "h " + m + "m";
    if (h > 0) return h + "h " + m + "m";
    return m + "m " + remS + "s";
  }

  function formatBytes(bytes) {
    if (typeof bytes !== "number" || !isFinite(bytes) || bytes < 0) return "—";
    if (bytes < 1024) return bytes + " B";
    var units = ["KB", "MB", "GB", "TB"];
    var val = bytes;
    var u = -1;
    while (val >= 1024 && u < units.length - 1) {
      val /= 1024;
      u += 1;
    }
    return (val >= 100 ? val.toFixed(0) : val >= 10 ? val.toFixed(1) : val.toFixed(2)) + " " + units[u];
  }

  function setBadge(el, text, kind) {
    if (!el) return;
    el.textContent = text;
    el.className = "badge" + (kind ? " badge-" + kind : "");
  }

  function renderBanner(overall, unreachable) {
    clearChildren(reasonsEl);
    if (unreachable) {
      bannerEl.className = "card health-banner banner-red";
      bannerIconEl.textContent = "✕";
      bannerTitleEl.textContent = "Admin cannot reach the status";
      var liErr = document.createElement("li");
      liErr.textContent = "GET /admin/api/status failed or timed out.";
      reasonsEl.appendChild(liErr);
      reasonsEl.hidden = false;
      return;
    }

    var colour = (overall && overall.colour) || "yellow";
    var reasons = (overall && Array.isArray(overall.reasons)) ? overall.reasons : [];

    if (colour === "green") {
      bannerEl.className = "card health-banner banner-green";
      bannerIconEl.textContent = "✓";
      bannerTitleEl.textContent = "Healthy — all systems operational";
    } else if (colour === "red") {
      bannerEl.className = "card health-banner banner-red";
      bannerIconEl.textContent = "✕";
      bannerTitleEl.textContent = "Critical — action required";
    } else {
      bannerEl.className = "card health-banner banner-yellow";
      bannerIconEl.textContent = "⚠";
      bannerTitleEl.textContent = "Degraded — attention needed";
    }

    if (reasons.length > 0) {
      reasons.forEach(function (reason) {
        var li = document.createElement("li");
        li.textContent = String(reason);
        reasonsEl.appendChild(li);
      });
      reasonsEl.hidden = false;
    } else {
      reasonsEl.hidden = true;
    }
  }

  function renderLeagueAndHub(data) {
    var league = (data && data.league_app) || {};
    var isUp = league.status === "up";
    setBadge(leagueBadgeEl, isUp ? "UP" : "DOWN", isUp ? "ok" : "bad");

    var statusParts = [isUp ? "Up" : "Down"];
    if (league.heartbeat_stale) {
      statusParts.push("heartbeat stale");
    } else if (typeof league.heartbeat_age_s === "number") {
      statusParts.push("beat " + Math.round(league.heartbeat_age_s) + "s ago");
    } else if (league.error) {
      statusParts.push(String(league.error));
    }
    leagueStatusEl.textContent = statusParts.join(" · ");

    if (typeof league.uptime_s === "number") {
      var upText = formatDuration(league.uptime_s);
      if (league.uptime_source === "heartbeat_lower_bound") {
        upText += " (since beat)";
      }
      leagueUptimeEl.textContent = upText;
    } else {
      leagueUptimeEl.textContent = "—";
    }

    leagueRestartEl.textContent = league.last_restart ? formatTimestamp(league.last_restart) : "Not recorded";

    var ver = data && data.version;
    if (ver && ver.sha) {
      leagueVersionEl.textContent = ver.branch ? (ver.sha + " (" + ver.branch + ")") : String(ver.sha);
    } else {
      leagueVersionEl.textContent = "—";
    }

    var hub = (data && data.agent_hub) || {};
    var hubUp = hub.status === "up";
    setBadge(hubBadgeEl, hubUp ? "UP" : "DOWN", hubUp ? "ok" : "bad");
    hubStatusEl.textContent = hubUp ? "Up" : ("Down" + (hub.error ? " (" + hub.error + ")" : ""));
  }

  function renderScheduler(sched) {
    clearChildren(schedulerTbodyEl);
    if (!sched || sched.error) {
      schedulerErrorEl.hidden = false;
      schedulerErrorEl.textContent = "Scheduler status unavailable" + (sched && sched.error ? ": " + sched.error : ".");
      schedulerBasisEl.textContent = "";
      return;
    }
    schedulerErrorEl.hidden = true;
    schedulerBasisEl.textContent = sched.learning_observed ? "Learning active" : "";

    var jobs = sched.jobs || {};
    var keys = [];
    var seen = {};
    JOB_ORDER.forEach(function (k) {
      if (Object.prototype.hasOwnProperty.call(jobs, k)) {
        keys.push(k);
        seen[k] = true;
      }
    });
    Object.keys(jobs).forEach(function (k) {
      if (!seen[k]) keys.push(k);
    });

    if (!keys.length) {
      var trEmpty = document.createElement("tr");
      var tdEmpty = document.createElement("td");
      tdEmpty.colSpan = 4;
      tdEmpty.className = "muted";
      tdEmpty.textContent = "No scheduled jobs.";
      trEmpty.appendChild(tdEmpty);
      schedulerTbodyEl.appendChild(trEmpty);
      return;
    }

    keys.forEach(function (jobName) {
      var job = jobs[jobName] || {};
      var tr = document.createElement("tr");

      var tdName = document.createElement("td");
      var nameSpan = document.createElement("span");
      nameSpan.className = "job-name";
      nameSpan.textContent = jobName;
      tdName.appendChild(nameSpan);
      if (job.late) {
        var lateBadge = document.createElement("span");
        lateBadge.className = "badge badge-warn badge-inline";
        lateBadge.textContent = "LATE";
        tdName.appendChild(lateBadge);
      }

      var tdLast = document.createElement("td");
      tdLast.textContent = formatTimestamp(job.finished_at || job.started_at);

      var tdRes = document.createElement("td");
      var resBadge = document.createElement("span");
      if (job.result === "ok") {
        setBadge(resBadge, "ok", "ok");
      } else if (job.result === "failed") {
        setBadge(resBadge, "failed", "bad");
      } else if (job.result === "running") {
        setBadge(resBadge, "running", "warn");
      } else {
        setBadge(resBadge, "—", "neutral");
      }
      tdRes.appendChild(resBadge);

      var tdNext = document.createElement("td");
      tdNext.textContent = formatTimestamp(job.next_expected_run);

      tr.appendChild(tdName);
      tr.appendChild(tdLast);
      tr.appendChild(tdRes);
      tr.appendChild(tdNext);
      schedulerTbodyEl.appendChild(tr);
    });
  }

  function renderBroker(broker) {
    if (!broker || broker.error) {
      setBadge(brokerBadgeEl, "ERROR", "bad");
      brokerStatusEl.textContent = "Unavailable" + (broker && broker.error ? " (" + broker.error + ")" : "");
      brokerLastSuccessEl.textContent = "—";
      brokerMarketEl.textContent = "—";
      return;
    }
    var st = broker.status || "unknown";
    if (st === "ok") {
      setBadge(brokerBadgeEl, "OK", "ok");
      brokerStatusEl.textContent = "OK";
    } else if (st === "failed") {
      setBadge(brokerBadgeEl, "FAILED", "bad");
      brokerStatusEl.textContent = "Failed";
    } else {
      setBadge(brokerBadgeEl, "UNKNOWN", "warn");
      brokerStatusEl.textContent = "Unknown";
    }
    brokerLastSuccessEl.textContent = formatTimestamp(broker.last_success);
    var mkt = broker.market === "open" ? "Open" : broker.market === "closed" ? "Closed" : "—";
    brokerMarketEl.textContent = mkt;
  }

  function renderAgents(agents) {
    clearChildren(agentsTbodyEl);
    if (!agents || agents.error) {
      agentsErrorEl.hidden = false;
      agentsErrorEl.textContent = "Agents status unavailable" + (agents && agents.error ? ": " + agents.error : ".");
      return;
    }
    agentsErrorEl.hidden = true;

    AGENT_ORDER.forEach(function (item) {
      var info = agents[item.id] || {};
      var tr = document.createElement("tr");

      var tdAgent = document.createElement("td");
      tdAgent.className = "job-name";
      tdAgent.textContent = item.name;

      var tdTime = document.createElement("td");
      tdTime.textContent = formatTimestamp(info.time);

      var tdResult = document.createElement("td");
      var badge = document.createElement("span");
      if (info.result === "ok") {
        setBadge(badge, "ok", "ok");
      } else if (info.result === "failed") {
        setBadge(badge, "failed", "bad");
      } else {
        setBadge(badge, "—", "neutral");
      }
      tdResult.appendChild(badge);

      var tdDur = document.createElement("td");
      tdDur.textContent = formatDuration(info.duration_s);

      tr.appendChild(tdAgent);
      tr.appendChild(tdTime);
      tr.appendChild(tdResult);
      tr.appendChild(tdDur);
      agentsTbodyEl.appendChild(tr);
    });
  }

  function updateMeter(metric, labelEl, progressEl, barEl, includeBytes) {
    if (!metric || metric.error || typeof metric.percent !== "number") {
      labelEl.textContent = metric && metric.error ? ("Unavailable (" + metric.error + ")") : "—";
      barEl.style.width = "0%";
      barEl.className = "meter-fill";
      progressEl.setAttribute("aria-valuenow", "0");
      return;
    }
    var pct = Math.max(0, Math.min(100, metric.percent));
    var pctText = Math.round(pct) + "%";
    if (includeBytes && typeof metric.used_bytes === "number" && typeof metric.total_bytes === "number") {
      labelEl.textContent = formatBytes(metric.used_bytes) + " / " + formatBytes(metric.total_bytes) + " (" + pctText + ")";
    } else {
      labelEl.textContent = pctText;
    }
    barEl.style.width = pct.toFixed(1) + "%";
    progressEl.setAttribute("aria-valuenow", String(Math.round(pct)));

    if (pct > 95) {
      barEl.className = "meter-fill meter-red";
    } else if (pct > 80 || metric.warn) {
      barEl.className = "meter-fill meter-orange";
    } else {
      barEl.className = "meter-fill meter-ok";
    }
  }

  function renderMachine(machine) {
    var m = machine || {};
    updateMeter(m.cpu, cpuLabelEl, cpuProgressEl, cpuBarEl, false);
    updateMeter(m.ram, ramLabelEl, ramProgressEl, ramBarEl, true);
    updateMeter(m.disk, diskLabelEl, diskProgressEl, diskBarEl, true);

    var dbInfo = m.database || {};
    if (typeof dbInfo.size_bytes === "number") {
      var formatted = formatBytes(dbInfo.size_bytes);
      dbSizeEl.textContent = formatted;
      machineDbSummaryEl.textContent = "DB " + formatted;
    } else {
      dbSizeEl.textContent = dbInfo.error ? ("Unavailable (" + dbInfo.error + ")") : "—";
      machineDbSummaryEl.textContent = "";
    }
  }

  function syncServiceOptions(allowed) {
    if (!Array.isArray(allowed) || !allowed.length) return;
    var existing = {};
    for (var i = 0; i < logsServiceEl.options.length; i++) {
      existing[logsServiceEl.options[i].value] = true;
    }
    allowed.forEach(function (svc) {
      if (!existing[svc]) {
        var opt = document.createElement("option");
        opt.value = svc;
        opt.textContent = svc;
        logsServiceEl.appendChild(opt);
      }
    });
  }

  function renderLogs(data) {
    clearChildren(logsListEl);
    var lines = (data && Array.isArray(data.lines)) ? data.lines : [];
    var errors = (data && Array.isArray(data.errors)) ? data.errors : [];

    syncServiceOptions(data && data.allowed_services);

    logsCountEl.textContent = lines.length + (lines.length === 1 ? " line" : " lines") + " (max 50)";

    if (errors.length > 0) {
      logsStatusEl.hidden = false;
      var errParts = errors.map(function (e) {
        return (e.origin || "log") + ": " + (e.error || "error");
      });
      logsStatusEl.textContent = "Partial log read (" + errParts.join(", ") + ")";
    } else {
      logsStatusEl.hidden = true;
    }

    if (!lines.length) {
      var emptyP = document.createElement("p");
      emptyP.className = "muted";
      emptyP.textContent = "No warnings or errors for this filter.";
      logsListEl.appendChild(emptyP);
      return;
    }

    lines.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "log-item";

      var meta = document.createElement("div");
      meta.className = "log-meta";

      var lvl = document.createElement("span");
      var levelName = (item.level || "error").toLowerCase();
      setBadge(lvl, levelName.toUpperCase(), levelName === "warning" ? "warn" : "bad");

      var svc = document.createElement("span");
      svc.className = "log-service";
      svc.textContent = item.service || "league";

      var ts = document.createElement("span");
      ts.className = "log-ts muted";
      ts.textContent = formatTimestamp(item.ts);

      meta.appendChild(lvl);
      meta.appendChild(svc);
      if (item.source || item.logger || item.job || item.agent) {
        var ctx = document.createElement("span");
        ctx.className = "log-ctx muted";
        var parts = [];
        if (item.source) parts.push(item.source);
        else if (item.logger) parts.push(item.logger);
        if (item.job) parts.push("job=" + item.job);
        if (item.agent) parts.push("agent=" + item.agent);
        if (item.run_id) parts.push(item.run_id);
        ctx.textContent = parts.join(" · ");
        meta.appendChild(ctx);
      }
      meta.appendChild(ts);

      var msg = document.createElement("p");
      msg.className = "log-msg";
      msg.textContent = item.message || "";

      row.appendChild(meta);
      row.appendChild(msg);

      if (item.exc) {
        var excPre = document.createElement("pre");
        excPre.className = "record-pre log-exc";
        excPre.textContent = String(item.exc);
        row.appendChild(excPre);
      }

      logsListEl.appendChild(row);
    });
  }

  function fetchStatus() {
    var reqId = ++state.statusReqId;
    return fetch("/admin/api/status", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (reqId !== state.statusReqId) return;
        renderBanner(data.overall, false);
        renderLeagueAndHub(data);
        renderScheduler(data.scheduler);
        renderBroker(data.broker);
        renderAgents(data.agents);
        renderMachine(data.machine);
        updatedEl.textContent = "updated " + formatClock(new Date());
      })
      .catch(function () {
        if (reqId !== state.statusReqId) return;
        renderBanner(null, true);
      });
  }

  function fetchLogs() {
    var reqId = ++state.logsReqId;
    var params = new URLSearchParams();
    params.set("service", state.service);
    params.set("limit", "50");
    return fetch("/admin/api/logs?" + params.toString(), { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (reqId !== state.logsReqId) return;
        renderLogs(data);
      })
      .catch(function () {
        if (reqId !== state.logsReqId) return;
        clearChildren(logsListEl);
        logsStatusEl.hidden = false;
        logsStatusEl.textContent = "Admin cannot reach the error logs.";
      });
  }

  function refreshAll() {
    fetchStatus();
    fetchLogs();
  }

  function isStatusTabVisible() {
    if (document.hidden) return false;
    if (statusSection && statusSection.hidden) return false;
    return true;
  }

  refreshBtn.addEventListener("click", function () {
    refreshAll();
  });

  logsServiceEl.addEventListener("change", function () {
    state.service = logsServiceEl.value || "all";
    fetchLogs();
  });

  document.addEventListener("visibilitychange", function () {
    if (isStatusTabVisible()) {
      refreshAll();
    }
  });

  addEventListener("hashchange", function () {
    if (isStatusTabVisible()) {
      refreshAll();
    }
  });

  setInterval(function () {
    if (isStatusTabVisible()) {
      refreshAll();
    }
  }, 30000);

  refreshAll();
})();
