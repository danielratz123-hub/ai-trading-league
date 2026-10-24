(function () {
  var root = document.documentElement;
  var saved = null;
  try { saved = localStorage.getItem("admin-theme"); } catch (e) {}
  if (saved) root.dataset.theme = saved;

  document.getElementById("theme").addEventListener("click", function () {
    var dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("admin-theme", root.dataset.theme); } catch (e) {}
  });

  function show() {
    var tab = (location.hash || "#status").slice(1);
    if (!document.getElementById(tab)) tab = "status";
    document.querySelectorAll(".tab").forEach(function (s) { s.hidden = s.id !== tab; });
    document.querySelectorAll(".nav a").forEach(function (a) {
      if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }
  addEventListener("hashchange", show);
  show();

  var ping = document.getElementById("ping");
  fetch("/admin/api/ping", { credentials: "same-origin" })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function () { ping.textContent = "Running"; ping.className = "big ok"; })
    .catch(function () { ping.textContent = "Not reachable"; ping.className = "big bad"; });
})();
