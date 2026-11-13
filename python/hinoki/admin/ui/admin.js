/* Progressive enhancement only: every page works without this file. */
(function () {
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem("hinoki-admin-mode");
    if (saved === "light" || saved === "dark") { root.setAttribute("data-mode", saved); }
  } catch (e) { /* storage can be blocked: the system setting still applies */ }

  function isDark() {
    var mode = root.getAttribute("data-mode");
    if (mode) { return mode === "dark"; }
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var toggle = document.querySelector("[data-hn-theme-toggle]");
    if (toggle) {
      toggle.setAttribute("aria-pressed", isDark() ? "true" : "false");
      toggle.addEventListener("click", function () {
        var next = isDark() ? "light" : "dark";
        root.setAttribute("data-mode", next);
        toggle.setAttribute("aria-pressed", next === "dark" ? "true" : "false");
        try { localStorage.setItem("hinoki-admin-mode", next); } catch (e) { /* ignore */ }
      });
    }

    document.addEventListener("keydown", function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        var field = document.querySelector("[data-hn-global-search]");
        if (field) { event.preventDefault(); field.focus(); field.select(); }
      }
    });

    var all = document.querySelector("[data-hn-select-all]");
    var rows = Array.prototype.slice.call(document.querySelectorAll("[data-hn-select-row]"));
    var bar = document.querySelector("[data-hn-bulkbar]");
    var count = document.querySelector("[data-hn-selected-count]");
    function refresh() {
      var chosen = rows.filter(function (box) { return box.checked; });
      rows.forEach(function (box) {
        var tr = box.closest("tr");
        if (tr) { tr.classList.toggle("is-selected", box.checked); }
      });
      if (bar) { bar.classList.toggle("is-active", chosen.length > 0); }
      if (count) { count.textContent = String(chosen.length); }
      if (all) {
        all.checked = chosen.length === rows.length && rows.length > 0;
        all.indeterminate = chosen.length > 0 && chosen.length < rows.length;
      }
    }
    rows.forEach(function (box) { box.addEventListener("change", refresh); });
    if (all) {
      all.addEventListener("change", function () {
        rows.forEach(function (box) { box.checked = all.checked; });
        refresh();
      });
    }
  });
})();
