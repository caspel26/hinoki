/* Hinoki theme: theme toggle, drawer, reading meter, command palette, copy,
   rule popovers, diagnostics, scroll-spy, hover highlight and reveal. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const base = (window.HN && window.HN.base) || ".";
  const root = document.documentElement;

  const RULES = {
    "HNK-ACC-001": {
      phase: "startup",
      title: "Every matching action names an Access policy",
      decl: "Actions.require_access(match, except_)",
      fix: "add access=<policy> to the action (or list it in except_ when it is public on purpose)",
    },
    "HNK-TXN-001": {
      phase: "startup",
      title: "State-changing actions use transaction=True",
      decl: "Transactions.require_atomic(methods)",
      fix: "add transaction=True to the action (or declare it on an RouteGroup(..., atomic=True) group)",
    },
    "HNK-API-001": {
      phase: "startup",
      title: "No action returns a Model; responses are Schema classes",
      decl: "Actions.forbid_model_responses(match, except_)",
      fix: "return a Schema such as PostOut.from_model(row)",
    },
    "HNK-SCH-001": {
      phase: "startup",
      title: "Named fields never appear in a response Schema",
      decl: "Schemas.forbid_exposure(*fields)",
      fix: "remove the field from the response Schema, or return a Schema that does not carry it",
    },
    "HNK-IMP-001": {
      phase: "static",
      title: "Modules under a source never import a target",
      decl: "Imports.forbid(source, target)",
      fix: "move the code to an allowed layer, or depend on it through the layer in between",
    },
  };

  const url = (path) => `${base.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  /* ---------- Toast ---------- */
  let toastTimer = 0;
  const toast = (text) => {
    const el = $("[data-toast]");
    if (!el) return;
    el.textContent = text;
    el.classList.add("is-on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("is-on"), 1600);
  };
  window.HN = Object.assign(window.HN || {}, { toast });

  /* ---------- Theme ---------- */
  const setTheme = (theme) => {
    root.dataset.theme = theme;
    try {
      localStorage.setItem("hn-theme", theme);
    } catch (e) {
      /* storage unavailable */
    }
  };
  const toggleTheme = () => {
    setTheme(root.dataset.theme === "dark" ? "light" : "dark");
    toast(`${root.dataset.theme === "dark" ? "Dark" : "Light"} theme`);
  };
  $$("[data-theme-toggle]").forEach((b) => b.addEventListener("click", toggleTheme));

  const sectionNav = $(".top__nav");
  const activeSection = sectionNav && $(".top__link.is-active", sectionNav);
  if (sectionNav && activeSection && matchMedia("(max-width: 960px)").matches) {
    const itemLeft =
      activeSection.getBoundingClientRect().left -
      sectionNav.getBoundingClientRect().left +
      sectionNav.scrollLeft;
    sectionNav.scrollLeft =
      itemLeft - (sectionNav.clientWidth - activeSection.clientWidth) / 2;
  }

  /* ---------- Drawer ---------- */
  const menu = $("[data-menu]");
  const closeMenu = () => {
    document.body.classList.remove("menu-open");
    if (menu) {
      menu.setAttribute("aria-expanded", "false");
      menu.setAttribute("aria-label", "Open navigation");
    }
    const ov = $("[data-overlay]");
    if (ov && !paletteOpen) ov.hidden = true;
  };
  if (menu) {
    menu.addEventListener("click", () => {
      const open = !document.body.classList.contains("menu-open");
      document.body.classList.toggle("menu-open", open);
      menu.setAttribute("aria-expanded", String(open));
      menu.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
      const ov = $("[data-overlay]");
      if (ov) ov.hidden = !open;
    });
  }
  addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("menu-open")) {
      closeMenu();
      menu && menu.focus();
    }
  });

  /* ---------- Header glass on scroll ---------- */
  const bar = $(".top");
  const glass = () => bar && bar.classList.toggle("is-scrolled", scrollY > 12);
  addEventListener("scroll", glass, { passive: true });
  glass();

  /* ---------- Reading meter ---------- */
  const meter = $(".meter i");
  const updateMeter = () => {
    if (!meter) return;
    const max = document.documentElement.scrollHeight - innerHeight;
    meter.style.transform = `scaleX(${max > 0 ? Math.min(1, scrollY / max) : 0})`;
  };
  addEventListener("scroll", updateMeter, { passive: true });
  updateMeter();

  /* ---------- Command palette ---------- */
  const palette = $("[data-palette]");
  const overlay = $("[data-overlay]");
  const input = $("[data-palette-input]");
  const list = $("[data-palette-list]");
  let paletteOpen = false;
  let docs = null;
  let titles = {};
  let items = [];
  let sel = 0;

  const loadIndex = async () => {
    if (docs) return;
    try {
      const res = await fetch(url("search/search_index.json"));
      const data = await res.json();
      docs = data.docs || [];
      docs.forEach((d) => {
        if (!d.location.includes("#")) titles[d.location] = d.title;
      });
    } catch (e) {
      docs = [];
    }
  };

  const commands = () => [
    { title: "Quick start", hint: "Get started", href: url("QUICK-START/") },
    { title: "Rules guide", hint: "Architecture checks", href: url("RULES/") },
    { title: "Project status", hint: "What works today", href: url("STATUS/") },
    { title: "Toggle light / dark theme", hint: "Appearance", run: toggleTheme },
    { title: "Open the repository on GitHub", hint: "caspel26/hinoki", href: (window.HN && window.HN.repo) || "#" },
  ];

  const tokens = (q) => q.toLowerCase().split(/[^a-z0-9_.=\-]+/).filter(Boolean);

  const search = (q) => {
    const ts = tokens(q);
    if (!ts.length || !docs) return [];
    const out = [];
    for (const d of docs) {
      const title = d.title.toLowerCase();
      const text = d.text.toLowerCase();
      let score = 0;
      let ok = true;
      for (const t of ts) {
        const inTitle = title.includes(t);
        const inText = text.includes(t);
        if (!inTitle && !inText) {
          ok = false;
          break;
        }
        score += (inTitle ? (title.startsWith(t) ? 14 : 9) : 0) + (inText ? 2 : 0);
      }
      if (!ok) continue;
      if (!d.location.includes("#")) score += 3;
      out.push({ d, score, text });
    }
    out.sort((a, b) => b.score - a.score);
    return out.slice(0, 9).map(({ d, text }) => {
      const first = ts.map((t) => text.indexOf(t)).filter((i) => i >= 0).sort((a, b) => a - b)[0] || 0;
      const start = Math.max(0, first - 40);
      let snippet = esc(d.text.slice(start, start + 150).replace(/¶/g, ""));
      ts.forEach((t) => {
        snippet = snippet.replace(new RegExp(`(${t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi"), "<mark>$1</mark>");
      });
      const page = d.location.split("#")[0];
      return {
        title: d.title,
        hint: d.location.includes("#") ? titles[page] || page : "",
        snippet: (start > 0 ? "…" : "") + snippet,
        href: url(d.location),
      };
    });
  };

  const render = () => {
    const q = input.value.trim();
    list.innerHTML = "";
    items = q ? search(q) : commands();
    if (!items.length) {
      list.innerHTML = `<li class="palette__empty">No results for “${esc(q)}”. Try a rule ID, a symbol or a command name.</li>`;
      return;
    }
    const group = document.createElement("li");
    group.className = "palette__group";
    group.textContent = q ? `${items.length} result${items.length > 1 ? "s" : ""}` : "Go to";
    list.appendChild(group);
    items.forEach((it, i) => {
      const li = document.createElement("li");
      li.className = "palette__item";
      li.setAttribute("role", "option");
      li.style.setProperty("--i", i);
      li.dataset.index = i;
      li.innerHTML =
        `<span class="palette__title">${esc(it.title)}${it.hint ? ` <small style="color:var(--hn-text-3);font-weight:400">· ${esc(it.hint)}</small>` : ""}</span>` +
        (it.snippet ? `<span class="palette__snippet">${it.snippet}</span>` : "");
      li.addEventListener("pointermove", () => select(i));
      li.addEventListener("click", () => open(i));
      list.appendChild(li);
    });
    select(0);
  };

  const select = (i) => {
    sel = i;
    $$(".palette__item", list).forEach((li) => li.setAttribute("aria-selected", String(+li.dataset.index === i)));
    const cur = $(`.palette__item[data-index="${i}"]`, list);
    cur && cur.scrollIntoView({ block: "nearest" });
  };

  const open = (i) => {
    const it = items[i];
    if (!it) return;
    closePalette();
    if (it.run) it.run();
    else if (it.href) location.href = it.href;
  };

  const openPalette = async () => {
    if (!palette || paletteOpen) return;
    paletteOpen = true;
    palette.hidden = false;
    overlay.hidden = false;
    input.value = "";
    render();
    input.focus();
    await loadIndex();
    if (input.value.trim()) render();
  };

  function closePalette() {
    if (!palette) return;
    paletteOpen = false;
    palette.hidden = true;
    if (!document.body.classList.contains("menu-open")) overlay.hidden = true;
  }

  if (palette) {
    input.addEventListener("input", render);
    overlay.addEventListener("click", () => {
      closePalette();
      closeMenu();
    });
    palette.addEventListener("click", (e) => {
      if (e.target === palette) closePalette();
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        select(Math.min(items.length - 1, sel + 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        select(Math.max(0, sel - 1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        open(sel);
      }
    });
  }
  $$("[data-search]").forEach((b) => b.addEventListener("click", openPalette));
  document.addEventListener("keydown", (e) => {
    const typing = e.target instanceof HTMLElement && e.target.closest("input, textarea, select, [contenteditable]");
    if ((e.key === "/" && !typing && !e.metaKey && !e.ctrlKey) || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k")) {
      e.preventDefault();
      openPalette();
    } else if (e.key === "Escape") {
      if (paletteOpen) closePalette();
      closeMenu();
      hidePopover();
    }
  });

  /* ---------- Copy buttons ---------- */
  $$(".highlight").forEach((block) => {
    if (block.closest("[data-no-copy]")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy code");
    btn.addEventListener("click", async () => {
      const code = $("pre", block);
      try {
        await navigator.clipboard.writeText(code.innerText.replace(/\n$/, ""));
      } catch (e) {
        return;
      }
      btn.textContent = "Copied";
      toast("Copied to clipboard");
      setTimeout(() => {
        btn.textContent = "Copy";
      }, 1400);
    });
    block.appendChild(btn);
  });

  /* ---------- Diagnostics in rule output ---------- */
  const LINE = /^(\S+?:\d+:)(\s+)(HNK-[A-Z]+-\d+)(\s+)\[(violated|waived|unverifiable)\](\s+)(.*)$/;
  const structure = (code) => {
    const text = code.textContent;
    if (!/\[(violated|waived|unverifiable)\]/.test(text) || code.querySelector(".k, .nf, .s")) return;
    const cls = { violated: "dg-bad", waived: "dg-wvd", unverifiable: "dg-unv" };
    code.innerHTML = text
      .replace(/\n$/, "")
      .split("\n")
      .map((line) => {
        const m = LINE.exec(line);
        if (m) {
          return `<span class="dg-loc">${esc(m[1])}</span>${m[2]}<span class="dg-id">${m[3]}</span>${m[4]}<span class="${cls[m[5]]}">[${m[5]}]</span>${m[6]}${esc(m[7])}`;
        }
        if (/^\s+(fix|waived):/.test(line)) return `<span class="dg-fix">${esc(line)}</span>`;
        const sum = /^(\d+ rule\(s\):\s*)(\d+) violated(.*)$/.exec(line);
        if (sum) return `${esc(sum[1])}<span class="${+sum[2] ? "dg-bad" : "dg-ok"}">${sum[2]} violated</span>${esc(sum[3])}`;
        return esc(line);
      })
      .join("\n");
  };
  $$(".highlight pre code").forEach(structure);

  /* ---------- Rule popovers ---------- */
  const pop = $("[data-popover]");
  let popFor = null;
  let popTimer = 0;

  const hidePopover = () => {
    if (!pop) return;
    pop.hidden = true;
    popFor = null;
  };

  const showPopover = (el) => {
    const rule = RULES[el.dataset.rule];
    if (!rule || !pop) return;
    clearTimeout(popTimer);
    popFor = el;
    pop.innerHTML =
      `<p class="popover__id">${el.dataset.rule} · ${rule.phase}</p>` +
      `<p class="popover__title">${esc(rule.title)}</p><code>${esc(rule.decl)}</code>` +
      `<p class="popover__fix">fix: ${esc(rule.fix)}</p>` +
      `<a class="popover__more" href="${url("RULES/")}">Read the Rules guide →</a>`;
    pop.hidden = false;
    const r = el.getBoundingClientRect();
    const w = pop.offsetWidth;
    const left = Math.max(8, Math.min(innerWidth - w - 8, r.left + scrollX));
    const below = r.bottom + 8;
    const top = below + pop.offsetHeight < innerHeight ? below + scrollY : r.top - pop.offsetHeight - 8 + scrollY;
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  };

  const wrapRules = (container) => {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) =>
        /HNK-(ACC|TXN|API|SCH|IMP)-001/.test(n.nodeValue) && !n.parentElement.closest("a, button, h1, .rule-ref")
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_REJECT,
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const frag = document.createDocumentFragment();
      node.nodeValue.split(/(HNK-(?:ACC|TXN|API|SCH|IMP)-001)/).forEach((part) => {
        if (RULES[part]) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "rule-ref";
          b.dataset.rule = part;
          b.textContent = part;
          frag.appendChild(b);
        } else if (part) {
          frag.appendChild(document.createTextNode(part));
        }
      });
      node.parentNode.replaceChild(frag, node);
    });
  };
  const prose = $(".prose");
  if (prose) wrapRules(prose);

  document.addEventListener("pointerover", (e) => {
    const el = e.target.closest && e.target.closest(".rule-ref");
    if (el) showPopover(el);
  });
  document.addEventListener("pointerout", (e) => {
    if (e.target.closest && e.target.closest(".rule-ref")) popTimer = setTimeout(hidePopover, 160);
  });
  pop && pop.addEventListener("pointerenter", () => clearTimeout(popTimer));
  pop && pop.addEventListener("pointerleave", () => (popTimer = setTimeout(hidePopover, 160)));
  document.addEventListener("focusin", (e) => {
    const el = e.target.closest && e.target.closest(".rule-ref");
    if (el) showPopover(el);
  });
  document.addEventListener("click", (e) => {
    const el = e.target.closest && e.target.closest(".rule-ref");
    if (el) showPopover(el);
    else if (!e.target.closest("[data-popover]")) hidePopover();
  });

  const sideScroller = $(".side__scroll");
  const activeSideLink = sideScroller && $(".side__link.is-active", sideScroller);
  activeSideLink && activeSideLink.scrollIntoView({ block: "nearest" });

  /* ---------- Table of contents scroll-spy ---------- */
  const toc = $("[data-toc]");
  if (toc) {
    const links = $$("a", toc);
    const marker = $(".toc__marker", toc);
    const heads = links.map((a) => document.getElementById(decodeURIComponent(a.hash.slice(1)))).filter(Boolean);
    let current = null;
    const setActive = () => {
      let idx = -1;
      heads.forEach((h, i) => {
        if (h.getBoundingClientRect().top <= 140) idx = i;
      });
      const a = idx >= 0 ? links[idx] : null;
      if (a === current) return;
      current = a;
      links.forEach((l) => l.classList.toggle("is-active", l === a));
      if (a && marker) {
        marker.style.opacity = "1";
        marker.style.height = `${a.offsetHeight}px`;
        marker.style.transform = `translateY(${a.offsetTop}px)`;
      } else if (marker) {
        marker.style.opacity = "0";
      }
    };
    addEventListener("scroll", setActive, { passive: true });
    setActive();
  }

  /* ---------- Sliding thumbs for segmented controls and tabs ---------- */
  const currentOf = (root) => {
    if (root.classList.contains("tabbed-labels")) {
      const inputs = $$(":scope > input", root.parentElement);
      const idx = inputs.findIndex((i) => i.checked);
      return idx >= 0 ? $$(":scope > label", root)[idx] : null;
    }
    return $('[aria-pressed="true"], [aria-selected="true"]', root);
  };

  const setupSlider = (root) => {
    if (root.dataset.sliderReady) return;
    root.dataset.sliderReady = "1";
    const thumb = document.createElement("span");
    thumb.className = "slider__thumb";
    thumb.setAttribute("aria-hidden", "true");
    root.prepend(thumb);
    root.classList.add("has-thumb");
    const place = () => {
      const el = currentOf(root);
      if (!el || !el.offsetWidth) return;
      thumb.style.setProperty("--x", `${el.offsetLeft}px`);
      thumb.style.setProperty("--y", `${el.offsetTop}px`);
      thumb.style.setProperty("--w", `${el.offsetWidth}px`);
      thumb.style.setProperty("--h", `${el.offsetHeight}px`);
    };
    place();
    requestAnimationFrame(() => requestAnimationFrame(() => root.classList.add("is-ready")));
    new MutationObserver(place).observe(root, { subtree: true, attributes: true, attributeFilter: ["aria-pressed", "aria-selected", "hidden"] });
    if (root.classList.contains("tabbed-labels")) {
      root.parentElement.addEventListener("change", place);
    }
    if ("ResizeObserver" in window) new ResizeObserver(place).observe(root);
    addEventListener("resize", place, { passive: true });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(place);
  };
  const sliders = () => {
    $$("[data-slider], .tabbed-labels").forEach(setupSlider);
  };
  sliders();
  addEventListener("load", sliders);

  /* ---------- Scrollable regions reachable by keyboard ---------- */
  const focusables = () => {
    $$("pre, pre code, .prose table, .tabbed-labels, .hn-walk__tabs, .top__nav, .hn-index__pages ul").forEach((el) => {
      const scrolls = el.matches("pre") || el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
      if (scrolls && !el.hasAttribute("tabindex")) {
        el.tabIndex = 0;
        el.dataset.autoFocusable = "";
      } else if (!scrolls && "autoFocusable" in el.dataset) {
        el.removeAttribute("tabindex");
        delete el.dataset.autoFocusable;
      }
    });
  };
  focusables();
  addEventListener("resize", focusables, { passive: true });
  addEventListener("load", focusables);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(focusables);

  /* ---------- Reveal on scroll ---------- */
  const revealTargets = $$(".reveal");
  $$("[data-stagger]").forEach((group) => [...group.children].forEach((c, i) => c.style.setProperty("--i", i)));
  if ("IntersectionObserver" in window && !reduced()) {
    const io = new IntersectionObserver(
      (entries) =>
        entries.forEach((en) => {
          if (en.isIntersecting) {
            en.target.classList.add("is-in");
            io.unobserve(en.target);
          }
        }),
      { rootMargin: "0px 0px -8% 0px", threshold: 0.1 },
    );
    revealTargets.forEach((t) => io.observe(t));
  } else {
    revealTargets.forEach((t) => t.classList.add("is-in"));
  }
})();
