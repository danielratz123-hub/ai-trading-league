(function () {
  var shortcutsEl = document.getElementById("db-shortcuts");
  var tablesEl = document.getElementById("db-tables");
  var browserEl = document.getElementById("db-browser");
  var titleEl = document.getElementById("db-table-title");
  var totalEl = document.getElementById("db-table-total");
  var searchEl = document.getElementById("db-search");
  var exportEl = document.getElementById("db-export");
  var statusEl = document.getElementById("db-status");
  var theadEl = document.getElementById("db-thead");
  var tbodyEl = document.getElementById("db-tbody");
  var prevBtn = document.getElementById("db-prev");
  var nextBtn = document.getElementById("db-next");
  var pageInfoEl = document.getElementById("db-page-info");
  var modalBackdrop = document.getElementById("db-modal-backdrop");
  var modalClose = document.getElementById("db-modal-close");
  var recordTitleEl = document.getElementById("db-record-title");
  var recordBodyEl = document.getElementById("db-record-body");

  var SHORTCUT_ORDER = [
    { key: "trades", label: "Trades" },
    { key: "positions", label: "Positions" },
    { key: "strategies", label: "Strategies" },
    { key: "agent_reports", label: "Agent reports" },
    { key: "weekly_scores", label: "Weekly scores" }
  ];

  var state = {
    initialized: false,
    tables: [],
    shortcuts: {},
    currentTable: null,
    page: 1,
    pages: 1,
    total: 0,
    sort: null,
    dir: "desc",
    q: "",
    columns: [],
    rows: [],
    reqId: 0
  };

  function clearChildren(el) {
    while (el.firstChild) {
      el.removeChild(el.firstChild);
    }
  }

  function formatCell(val) {
    if (val === null || val === undefined) return "NULL";
    if (typeof val === "object") {
      try { return JSON.stringify(val); } catch (e) { return String(val); }
    }
    var s = String(val);
    return s.length > 140 ? s.slice(0, 140) + "…" : s;
  }

  function isPrettyValue(val) {
    if (val !== null && typeof val === "object") return true;
    if (typeof val === "string") {
      var trimmed = val.trim();
      if ((trimmed.startsWith("{") && trimmed.endsWith("}")) ||
          (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
        try {
          JSON.parse(trimmed);
          return true;
        } catch (e) {}
      }
      if (val.indexOf("\n") !== -1 || val.length > 120) return true;
    }
    return false;
  }

  function formatRecordValue(val) {
    if (val === null || val === undefined) return "NULL";
    if (typeof val === "object") {
      try { return JSON.stringify(val, null, 2); } catch (e) { return String(val); }
    }
    if (typeof val === "string") {
      var trimmed = val.trim();
      if ((trimmed.startsWith("{") && trimmed.endsWith("}")) ||
          (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
        try {
          return JSON.stringify(JSON.parse(trimmed), null, 2);
        } catch (e) {}
      }
    }
    return String(val);
  }

  function updateExportLink() {
    if (!state.currentTable) return;
    var params = new URLSearchParams();
    if (state.sort) params.set("sort", state.sort);
    if (state.dir) params.set("dir", state.dir);
    if (state.q) params.set("q", state.q);
    var qs = params.toString();
    exportEl.href = "/admin/api/tables/" + encodeURIComponent(state.currentTable) +
      "/export.csv" + (qs ? "?" + qs : "");
    exportEl.download = state.currentTable + ".csv";
  }

  function highlightActiveTable() {
    var links = document.querySelectorAll("[data-db-table]");
    links.forEach(function (a) {
      if (a.dataset.dbTable === state.currentTable) {
        a.classList.add("active");
        a.setAttribute("aria-current", "true");
      } else {
        a.classList.remove("active");
        a.removeAttribute("aria-current");
      }
    });
  }

  function renderShortcuts() {
    clearChildren(shortcutsEl);
    var seen = {};
    SHORTCUT_ORDER.forEach(function (item) {
      var target = state.shortcuts[item.key];
      if (!target) return;
      seen[item.key] = true;
      var a = document.createElement("a");
      a.className = "chip";
      a.href = "#database/" + encodeURIComponent(target);
      a.dataset.dbTable = target;
      a.textContent = item.label;
      shortcutsEl.appendChild(a);
    });
    Object.keys(state.shortcuts).forEach(function (key) {
      if (seen[key]) return;
      var target = state.shortcuts[key];
      var a = document.createElement("a");
      a.className = "chip";
      a.href = "#database/" + encodeURIComponent(target);
      a.dataset.dbTable = target;
      a.textContent = key.replace(/_/g, " ");
      shortcutsEl.appendChild(a);
    });
    highlightActiveTable();
  }

  function renderTables() {
    clearChildren(tablesEl);
    if (!state.tables.length) {
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = "No tables found.";
      tablesEl.appendChild(p);
      return;
    }
    state.tables.forEach(function (t) {
      var a = document.createElement("a");
      a.className = "table-pill";
      a.href = "#database/" + encodeURIComponent(t.name);
      a.dataset.dbTable = t.name;
      var nameSpan = document.createElement("span");
      nameSpan.className = "table-pill-name";
      nameSpan.textContent = t.name;
      var countSpan = document.createElement("span");
      countSpan.className = "table-pill-count";
      countSpan.textContent = String(t.count);
      a.appendChild(nameSpan);
      a.appendChild(countSpan);
      tablesEl.appendChild(a);
    });
    highlightActiveTable();
  }

  function loadOverview() {
    if (state.initialized) return Promise.resolve();
    state.initialized = true;
    var pTables = fetch("/admin/api/tables", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        state.tables = Array.isArray(data) ? data : [];
        renderTables();
      })
      .catch(function () {
        state.initialized = false;
        clearChildren(tablesEl);
        var p = document.createElement("p");
        p.className = "muted";
        p.textContent = "Could not load tables.";
        tablesEl.appendChild(p);
      });

    var pShortcuts = fetch("/admin/api/shortcuts", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        state.shortcuts = data || {};
        renderShortcuts();
      })
      .catch(function () {});

    return Promise.all([pTables, pShortcuts]);
  }

  function renderRecordModal(titleText, recordObj) {
    recordTitleEl.textContent = titleText;
    clearChildren(recordBodyEl);
    var dl = document.createElement("dl");
    dl.className = "record-dl";
    Object.keys(recordObj).forEach(function (col) {
      var val = recordObj[col];
      var dt = document.createElement("dt");
      dt.textContent = col;
      var dd = document.createElement("dd");
      if (val === null || val === undefined) {
        dd.className = "null-val";
        dd.textContent = "NULL";
      } else if (isPrettyValue(val)) {
        var pre = document.createElement("pre");
        pre.className = "record-pre";
        pre.textContent = formatRecordValue(val);
        dd.appendChild(pre);
      } else {
        dd.textContent = String(val);
      }
      dl.appendChild(dt);
      dl.appendChild(dd);
    });
    recordBodyEl.appendChild(dl);
    modalBackdrop.hidden = false;
    modalClose.focus();
  }

  function openRowRecord(row) {
    var table = state.currentTable;
    if (!table) return;
    var rowid = null;
    if (typeof row.id === "number" || (typeof row.id === "string" && /^\d+$/.test(row.id))) {
      rowid = row.id;
    } else if (typeof row.rowid === "number") {
      rowid = row.rowid;
    }
    if (rowid !== null) {
      var url = "/admin/api/tables/" + encodeURIComponent(table) + "/rows/" + encodeURIComponent(rowid);
      fetch(url, { credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok) throw new Error(r.status);
          return r.json();
        })
        .then(function (rec) {
          renderRecordModal(table + " #" + rowid, rec);
        })
        .catch(function () {
          renderRecordModal(table + " row", row);
        });
    } else {
      renderRecordModal(table + " row", row);
    }
  }

  function renderGrid() {
    clearChildren(theadEl);
    clearChildren(tbodyEl);

    var trHead = document.createElement("tr");
    state.columns.forEach(function (col) {
      var th = document.createElement("th");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "th-sort";
      var arrow = "";
      if (state.sort === col) {
        arrow = state.dir === "asc" ? " ▲" : " ▼";
      }
      btn.textContent = col + arrow;
      btn.addEventListener("click", function () {
        if (state.sort === col) {
          state.dir = state.dir === "desc" ? "asc" : "desc";
        } else {
          state.sort = col;
          state.dir = "desc";
        }
        state.page = 1;
        fetchTableData();
      });
      th.appendChild(btn);
      trHead.appendChild(th);
    });
    theadEl.appendChild(trHead);

    if (!state.rows.length) {
      var trEmpty = document.createElement("tr");
      var tdEmpty = document.createElement("td");
      tdEmpty.colSpan = Math.max(state.columns.length, 1);
      tdEmpty.className = "muted";
      tdEmpty.textContent = "No matching rows.";
      trEmpty.appendChild(tdEmpty);
      tbodyEl.appendChild(trEmpty);
      return;
    }

    state.rows.forEach(function (row) {
      var tr = document.createElement("tr");
      tr.className = "db-row";
      tr.tabIndex = 0;
      tr.setAttribute("role", "button");
      tr.addEventListener("click", function () { openRowRecord(row); });
      tr.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openRowRecord(row);
        }
      });
      state.columns.forEach(function (col) {
        var td = document.createElement("td");
        var val = row[col];
        if (val === null || val === undefined) {
          td.className = "null-val";
        }
        td.textContent = formatCell(val);
        tr.appendChild(td);
      });
      tbodyEl.appendChild(tr);
    });
  }

  function fetchTableData() {
    if (!state.currentTable) return;
    updateExportLink();
    var reqId = ++state.reqId;
    var params = new URLSearchParams();
    params.set("page", String(state.page));
    if (state.sort) params.set("sort", state.sort);
    if (state.dir) params.set("dir", state.dir);
    if (state.q) params.set("q", state.q);

    statusEl.hidden = false;
    statusEl.textContent = "Loading…";

    var url = "/admin/api/tables/" + encodeURIComponent(state.currentTable) + "?" + params.toString();
    fetch(url, { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (reqId !== state.reqId) return;
        statusEl.hidden = true;
        state.columns = data.columns || [];
        state.rows = data.rows || [];
        state.total = typeof data.total === "number" ? data.total : 0;
        state.page = typeof data.page === "number" ? data.page : 1;
        state.pages = Math.max(typeof data.pages === "number" ? data.pages : 1, 1);

        titleEl.textContent = state.currentTable;
        totalEl.textContent = state.total + (state.total === 1 ? " row" : " rows");
        pageInfoEl.textContent = "page " + state.page + " of " + state.pages;
        prevBtn.disabled = state.page <= 1;
        nextBtn.disabled = state.page >= state.pages;

        renderGrid();
      })
      .catch(function () {
        if (reqId !== state.reqId) return;
        statusEl.hidden = false;
        statusEl.textContent = "Could not load table " + state.currentTable + ".";
        clearChildren(theadEl);
        clearChildren(tbodyEl);
      });
  }

  function openTable(name) {
    if (!name) {
      state.currentTable = null;
      browserEl.hidden = true;
      highlightActiveTable();
      return;
    }
    var changed = state.currentTable !== name;
    state.currentTable = name;
    browserEl.hidden = false;
    highlightActiveTable();
    if (changed) {
      state.page = 1;
      state.sort = null;
      state.dir = "desc";
      state.q = "";
      searchEl.value = "";
      titleEl.textContent = name;
      totalEl.textContent = "";
    }
    fetchTableData();
  }

  var debounceTimer = null;
  searchEl.addEventListener("input", function () {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () {
      state.q = searchEl.value.trim();
      state.page = 1;
      fetchTableData();
    }, 250);
  });

  prevBtn.addEventListener("click", function () {
    if (state.page > 1) {
      state.page -= 1;
      fetchTableData();
    }
  });

  nextBtn.addEventListener("click", function () {
    if (state.page < state.pages) {
      state.page += 1;
      fetchTableData();
    }
  });

  function closeModal() {
    modalBackdrop.hidden = true;
  }

  modalClose.addEventListener("click", closeModal);
  modalBackdrop.addEventListener("click", function (e) {
    if (e.target === modalBackdrop) closeModal();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !modalBackdrop.hidden) closeModal();
  });

  function handleRoute() {
    var raw = (location.hash || "#status").slice(1);
    var parts = raw.split("/");
    if (parts[0] !== "database") {
      closeModal();
      return;
    }
    var tablePart = parts.slice(1).join("/");
    var tableName = tablePart ? decodeURIComponent(tablePart) : null;
    loadOverview().then(function () {
      openTable(tableName);
    });
  }

  addEventListener("hashchange", handleRoute);
  handleRoute();
})();
