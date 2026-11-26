/* Benchmark page: tabs for the worker count and for the request type. Without this script every panel is visible and nothing is hidden. */
(() => {
  const keyTarget = (key, index, last) => {
    if (key === "ArrowRight") return index === last ? 0 : index + 1;
    if (key === "ArrowLeft") return index === 0 ? last : index - 1;
    if (key === "Home") return 0;
    if (key === "End") return last;
    return -1;
  };

  const setup = (root) => {
    // Only this set's own tab list: a worker panel holds request-type tabs of its own.
    const list = root.querySelector(':scope > [role="tablist"]');
    if (!list) return;
    const tabs = Array.from(list.querySelectorAll('[role="tab"]'));
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

    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => select(index, false));
      tab.addEventListener("keydown", (event) => {
        const target = keyTarget(event.key, index, tabs.length - 1);
        if (target < 0) return;
        event.preventDefault();
        select(target, true);
      });
    });

    const start = Math.max(0, tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true"));
    root.classList.add("is-enhanced");
    select(start, false);
  };

  document.querySelectorAll("[data-bench-explorer]").forEach(setup);
})();
