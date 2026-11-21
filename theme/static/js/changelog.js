(() => {
  const root = document.querySelector("[data-changelog]");
  if (!root) return;
  const cards = [...root.querySelectorAll(".cl__card")];
  const versions = [...root.querySelectorAll(".cl__ver")];
  const kinds = [...root.querySelectorAll(".cl__kind")];
  const active = new Set();

  const select = (slug) => {
    const found = cards.some((c) => c.id === slug);
    const target = found ? slug : cards[0].id;
    cards.forEach((c) => { c.hidden = c.id !== target; });
    versions.forEach((v) => {
      v.setAttribute("aria-current", String(v.dataset.target === target));
    });
  };

  const filter = () => {
    root.querySelectorAll(".cl__sec").forEach((s) => {
      s.hidden = active.size > 0 && !active.has(s.dataset.kind);
    });
  };

  versions.forEach((v) => v.addEventListener("click", (e) => {
    e.preventDefault();
    history.replaceState(null, "", "#" + v.dataset.target);
    select(v.dataset.target);
  }));
  kinds.forEach((k) => k.addEventListener("click", () => {
    const on = k.getAttribute("aria-pressed") !== "true";
    k.setAttribute("aria-pressed", String(on));
    on ? active.add(k.dataset.kind) : active.delete(k.dataset.kind);
    filter();
  }));
  window.addEventListener("hashchange", () => select(location.hash.slice(1)));
  select(location.hash.slice(1));
})();
