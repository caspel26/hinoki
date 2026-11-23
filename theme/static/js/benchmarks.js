/* Benchmark page: request-type tabs and the stack legend filter. Without this script every panel is visible and nothing is hidden. */
(() => {
  const explorer = document.querySelector("[data-bench-explorer]");
  if (!explorer) return;

  const tabs = Array.from(explorer.querySelectorAll('[role="tab"]'));
  const panels = tabs.map((tab) => document.getElementById(tab.getAttribute("aria-controls")));

  const select = (index, focus) => {
    tabs.forEach((tab, i) => {
      const on = i === index;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      panels[i].hidden = !on;
    });
    if (focus) tabs[index].focus();
  };

  const keyTarget = (key, index) => {
    const last = tabs.length - 1;
    if (key === "ArrowRight") return index === last ? 0 : index + 1;
    if (key === "ArrowLeft") return index === 0 ? last : index - 1;
    if (key === "Home") return 0;
    if (key === "End") return last;
    return -1;
  };

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(index, false));
    tab.addEventListener("keydown", (event) => {
      const target = keyTarget(event.key, index);
      if (target < 0) return;
      event.preventDefault();
      select(target, true);
    });
  });

  const start = Math.max(0, tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true"));
  explorer.classList.add("is-enhanced");
  select(start, false);

  const chips = Array.from(document.querySelectorAll(".bench-chip"));
  const applyFilter = () => {
    const hidden = new Set(chips.filter((c) => c.getAttribute("aria-pressed") === "false").map((c) => c.dataset.stack));
    document.querySelectorAll("[data-bench-filterable] [data-stack]").forEach((el) => {
      el.hidden = hidden.has(el.dataset.stack);
    });
    document.querySelectorAll("[data-bench-filterable]").forEach((root) => {
      if (root.matches("[data-stack]")) root.hidden = false;
    });
    // A group header spans only the visible columns of its group.
    document.querySelectorAll("[data-bench-filterable] .bench-groups th[data-group]").forEach((head) => {
      const columns = head.closest("table").querySelectorAll('thead th[scope="col"][data-group]');
      const visible = Array.from(columns).filter((th) => th.dataset.group === head.dataset.group && !th.hidden).length;
      head.hidden = visible === 0;
      if (visible) head.colSpan = visible;
    });
  };

  chips.forEach((chip) => {
    chip.addEventListener("click", () => {
      const pressed = chip.getAttribute("aria-pressed") === "true";
      const others = chips.filter((c) => c !== chip && c.getAttribute("aria-pressed") === "true");
      if (pressed && others.length === 0) return; // keep at least one stack visible
      chip.setAttribute("aria-pressed", String(!pressed));
      applyFilter();
    });
  });
})();
