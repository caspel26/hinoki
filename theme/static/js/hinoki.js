/* Hinoki theme: theme toggle, drawer, reading meter, command palette, copy,
   rule popovers, diagnostics, scroll-spy, hover highlight and reveal. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const base = (window.HN && window.HN.base) || ".";
  const root = document.documentElement;

  /* Generated from the rule classes by docsite/hooks.py, on pages that mention a rule. */
  const RULES = (window.HN && window.HN.rules) || {};
  const RULE_ID = /(HNK-[A-Z]+-\d{3})/;

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

  /* ---------- Documentation versions ---------- */
  const initDocVersions = async () => {
    const selector = $("[data-docs-version]");
    const trigger = $("[data-docs-version-trigger]", selector || document);
    const menu = $("[data-docs-version-menu]", selector || document);
    const stage = $("[data-project-stage]");
    const footer = $("[data-docs-version-footer]");
    if (!selector || !trigger || !menu || !window.HN?.site) return;

    const sitePath = new URL(window.HN.site, location.href).pathname.replace(/\/?$/, "/");
    const siteParts = sitePath.split("/").filter(Boolean);
    const slug = siteParts.at(-1) || "";
    const isVersion = (value) =>
      value === "dev" || value === "stable" || value === "latest" || /^v?\d+\.\d+(?:\.\d+)?(?:[-+][\w.-]+)?$/.test(value);
    const currentVersion = isVersion(slug) ? slug : "";
    const rootPath = currentVersion
      ? `/${siteParts.slice(0, -1).join("/")}/`
      : sitePath;
    const docsRoot = new URL(rootPath, location.origin);

    let versions;
    try {
      const response = await fetch(new URL("versions.json", docsRoot), { cache: "no-cache" });
      if (!response.ok) return;
      versions = await response.json();
    } catch (_error) {
      return;
    }
    if (!Array.isArray(versions)) return;
    versions = versions.filter(
      (item) => item && typeof item.version === "string" && !item.properties?.hidden,
    );
    if (versions.length < 2 || !currentVersion) return;

    const current = versions.find(
      (item) => item.version === currentVersion || item.aliases?.includes(currentVersion),
    );
    if (!current) return;

    const versionLabel = (item) => item.title || item.version;
    const currentLabel = versionLabel(current);
    $(`[data-docs-version-current]`, selector).textContent = currentLabel;
    trigger.setAttribute("aria-label", `Documentation version: ${currentLabel}. Choose a version`);
    if (footer) footer.textContent = current.version === "dev" ? "dev" : currentLabel;
    if (stage) stage.hidden = true;
    selector.hidden = false;

    const relativePath = location.pathname.startsWith(sitePath)
      ? location.pathname.slice(sitePath.length)
      : "";
    const makeTarget = (item, preservePage) => {
      const alias = item.aliases?.includes("stable") ? "stable" : item.version;
      const path = `${rootPath}${alias}/${preservePage ? relativePath : ""}`;
      const target = new URL(path, location.origin);
      if (preservePage) {
        target.search = location.search;
        target.hash = location.hash;
      }
      return target;
    };

    menu.replaceChildren();
    versions.forEach((item) => {
      const link = document.createElement("a");
      link.className = "docs-version__link";
      link.href = makeTarget(item, true).href;
      const label = document.createElement("span");
      label.textContent = item.version === "dev" ? "Development" : versionLabel(item);
      const note = document.createElement("small");
      note.textContent = item.version === "dev"
        ? "dev"
        : item.aliases?.includes("stable")
          ? "stable"
          : item.version;
      link.append(label, note);
      if (item === current) link.setAttribute("aria-current", "page");
      link.addEventListener("click", (event) => {
        event.preventDefault();
        const pageTarget = makeTarget(item, true);
        const versionHome = makeTarget(item, false);
        fetch(pageTarget, { method: "HEAD" })
          .then((result) => location.assign(result.ok ? pageTarget.href : versionHome.href))
          .catch(() => location.assign(pageTarget.href));
      });
      menu.append(link);
    });

    const closeMenu = (restoreFocus = false) => {
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      if (restoreFocus) trigger.focus();
    };
    trigger.addEventListener("click", () => {
      const open = menu.hidden;
      menu.hidden = !open;
      trigger.setAttribute("aria-expanded", String(open));
      if (open) $("a", menu)?.focus();
    });
    document.addEventListener("pointerdown", (event) => {
      if (!selector.contains(event.target)) closeMenu();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !menu.hidden) {
        event.preventDefault();
        closeMenu(true);
      }
    });
  };
  initDocVersions();

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
  const menuPanel = () => {
    const id = menu?.getAttribute("aria-controls") || "side";
    return $(`#${CSS.escape(id)}`);
  };
  const closeMenu = (restoreFocus = false) => {
    document.body.classList.remove("menu-open");
    const panel = menuPanel();
    panel?.removeAttribute("role");
    panel?.removeAttribute("aria-modal");
    if (menu) {
      menu.setAttribute("aria-expanded", "false");
      menu.setAttribute("aria-label", "Open navigation");
    }
    const ov = $("[data-overlay]");
    if (ov && !paletteOpen) ov.hidden = true;
    if (restoreFocus) menu?.focus();
  };
  if (menu) {
    menu.addEventListener("click", () => {
      const open = !document.body.classList.contains("menu-open");
      document.body.classList.toggle("menu-open", open);
      menu.setAttribute("aria-expanded", String(open));
      menu.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
      const panel = menuPanel();
      if (panel && open) {
        panel.setAttribute("role", "dialog");
        panel.setAttribute("aria-modal", "true");
      }
      const ov = $("[data-overlay]");
      if (ov) ov.hidden = !open;
      if (open) {
        const firstControl = $("[data-menu-close], a[href]", panel);
        firstControl?.focus();
      }
    });
  }
  $("[data-menu-close]")?.addEventListener("click", () => closeMenu(true));
  const sheetHandle = $(".mobile-sheet__handle", menuPanel());
  if (sheetHandle) {
    let dragStartY = null;
    sheetHandle.addEventListener("pointerdown", (event) => {
      dragStartY = event.clientY;
      sheetHandle.setPointerCapture(event.pointerId);
    });
    sheetHandle.addEventListener("pointerup", (event) => {
      if (dragStartY !== null && event.clientY - dragStartY > 64) closeMenu(true);
      dragStartY = null;
    });
    sheetHandle.addEventListener("pointercancel", () => {
      dragStartY = null;
    });
  }
  document.addEventListener("keydown", (event) => {
    if (!document.body.classList.contains("menu-open") || !menu) return;
    if (event.key === "Escape") {
      closeMenu(true);
      return;
    }
    if (event.key !== "Tab") return;
    const panel = menuPanel();
    const focusable = panel
      ? $$('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])', panel).filter((el) => el.getClientRects().length)
      : [];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!panel.contains(document.activeElement)) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  /* ---------- Header glass and reading meter, once per frame ---------- */
  const bar = $(".top");
  const meter = $(".meter i");
  let scrollMax = 0;
  let scrollFrame = 0;
  const measure = () => {
    scrollMax = document.documentElement.scrollHeight - innerHeight;
  };
  const onFrame = () => {
    scrollFrame = 0;
    if (bar) bar.classList.toggle("is-scrolled", scrollY > 12);
    if (meter) meter.style.transform = `scaleX(${scrollMax > 0 ? Math.min(1, scrollY / scrollMax) : 0})`;
  };
  const onScroll = () => {
    if (!scrollFrame) scrollFrame = requestAnimationFrame(onFrame);
  };
  const onResize = () => {
    measure();
    onScroll();
  };
  addEventListener("scroll", onScroll, { passive: true });
  addEventListener("resize", onResize, { passive: true });
  if ("ResizeObserver" in window) new ResizeObserver(onResize).observe(document.body);
  measure();
  onFrame();

  /* ---------- Command palette ---------- */
  const palette = $("[data-palette]");
  const overlay = $("[data-overlay]");
  const input = $("[data-palette-input]");
  const list = $("[data-palette-list]");
  let paletteOpen = false;
  let paletteOpener = null;
  let docs = null;
  let titles = {};
  let items = [];
  let sel = 0;

  /* Headings arrive HTML-escaped in the index (`&lt;name&gt;`); body text does not. */
  const decoder = document.createElement("textarea");
  const decode = (s) => {
    decoder.innerHTML = s;
    return decoder.value;
  };

  const loadIndex = async () => {
    if (docs) return;
    try {
      const res = await fetch(url("search/search_index.json"));
      const data = await res.json();
      docs = (data.docs || []).map((d) => {
        const title = decode(d.title);
        return { ...d, title, lowTitle: title.toLowerCase(), lowText: d.text.toLowerCase(), page: !d.location.includes("#") };
      });
      docs.forEach((d) => {
        if (d.page) titles[d.location] = d.title;
      });
    } catch (e) {
      docs = [];
    }
  };

  const commands = () => [
    { title: "Quick start", hint: "Get started", href: url("quick-start/") },
    { title: "Rules guide", hint: "Architecture checks", href: url("rules/") },
    { title: "Project status", hint: "What works today", href: url("status/") },
    { title: "Toggle light / dark theme", hint: "Appearance", run: toggleTheme },
    { title: "Open the repository on GitHub", hint: "caspel26/hinoki", href: (window.HN && window.HN.repo) || "#" },
  ];

  const tokens = (q) => q.toLowerCase().split(/[^a-z0-9_.=\-]+/).filter(Boolean);
  const escRe = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  const occurrences = (hay, t) => {
    let n = 0;
    for (let i = hay.indexOf(t); i >= 0 && n < 6; i = hay.indexOf(t, i + t.length)) n++;
    return n;
  };

  /* A whole word in the title beats a title prefix, which beats a substring. */
  const titleScore = (title, t) => {
    if (!title.includes(t)) return 0;
    if (new RegExp(`(^|[^a-z0-9_])${escRe(t)}($|[^a-z0-9_])`).test(title)) return 16;
    return title.startsWith(t) ? 14 : 9;
  };

  /* -1 when a token is missing. Titles count fully only when they hold every
     token; the query as a phrase and an exact-case title add a bonus. */
  const score = (d, q, ts) => {
    let inTitle = 0;
    let titled = 0;
    let inText = 0;
    for (const t of ts) {
      const s = titleScore(d.lowTitle, t);
      const n = occurrences(d.lowText, t);
      if (!s && !n) return -1;
      inTitle += s;
      titled += s ? 1 : 0;
      inText += n ? 1 + n : 0;
    }
    const phrase = ts.join(" ");
    let total = (titled === ts.length ? inTitle : inTitle / 2) + inText;
    if (d.lowTitle === phrase) total += 30;
    else if (ts.length > 1 && d.lowTitle.includes(phrase)) total += 20;
    if (ts.length > 1 && d.lowText.includes(phrase)) total += 20;
    if (d.title === q) total += 5;
    return total + (d.page ? 3 : 0);
  };

  const sectionOf = (location) => {
    if (location.startsWith("reference/")) return "API reference";
    if (location.startsWith("tutorial/")) return "Tutorial";
    return location ? "Guides" : "Home";
  };

  /* API symbols show their qualified name, so `db.atomic` and `Database.atomic` differ. */
  const hintOf = (d) => {
    if (d.page) return sectionOf(d.location);
    const [page, anchor] = d.location.split("#");
    return anchor.startsWith("hinoki.") ? anchor : titles[page] || page;
  };

  const search = (q) => {
    const ts = tokens(q);
    if (!ts.length || !docs) return [];
    const out = [];
    for (const d of docs) {
      const s = score(d, q, ts);
      if (s >= 0) out.push({ d, s });
    }
    out.sort((a, b) => b.s - a.s);
    return out.slice(0, 9).map(({ d }) => {
      const first = ts.map((t) => d.lowText.indexOf(t)).filter((i) => i >= 0).sort((a, b) => a - b)[0] || 0;
      const start = Math.max(0, first - 40);
      let snippet = esc(d.text.slice(start, start + 150).replace(/¶/g, ""));
      ts.forEach((t) => {
        snippet = snippet.replace(new RegExp(`(${escRe(esc(t))})`, "gi"), "<mark>$1</mark>");
      });
      return {
        title: d.title,
        hint: hintOf(d),
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
    paletteOpener = document.activeElement;
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
    if (paletteOpener && paletteOpener.isConnected && paletteOpener !== document.body) paletteOpener.focus({ preventScroll: true });
    paletteOpener = null;
    if (!document.body.classList.contains("menu-open")) overlay.hidden = true;
  }

  if (palette) {
    input.addEventListener("input", render);
    overlay.addEventListener("click", () => {
      closePalette();
      closeMenu(true);
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
    } else if (e.key === "Tab" && paletteOpen) {
      // The search dialog is modal: Tab cycles inside it.
      const focusable = $$('input, a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])', palette).filter((el) => el.offsetParent);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first) return;
      if (!palette.contains(document.activeElement)) {
        e.preventDefault();
        first.focus();
      } else if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });

  /* ---------- Scrollable tables ---------- */
  $$(".prose table").forEach((table) => {
    if (table.closest(".table-wrap, .bench-scroll, .benchmark-table-wrap, [data-no-wrap]")) return;
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    const caption = table.caption && table.caption.textContent.trim();
    wrap.setAttribute("aria-label", caption || "Table");
    table.before(wrap);
    wrap.appendChild(table);
  });

  /* ---------- Code block headers and copy buttons ---------- */
  const LANGS = { python: "Python", pycon: "Python", py: "Python", toml: "TOML", sh: "Shell", bash: "Shell", shell: "Shell", console: "Shell", json: "JSON", sql: "SQL", yaml: "YAML", css: "CSS", html: "HTML", rust: "Rust", js: "JavaScript" };
  const FILE_LINE = /^\s*(?:#|\/\/)\s*([\w.\/-]+\.\w+(?:\s+\([^)\n]*\))?)\s*$/;

  // Removes the first displayed line of a highlighted block (a "# path/file.py" comment).
  const dropFirstLine = (code) => {
    for (const node of [...code.childNodes]) {
      const text = node.textContent;
      const nl = text.indexOf("\n");
      if (nl < 0) {
        node.remove();
        continue;
      }
      if (node.nodeType === Node.TEXT_NODE) node.nodeValue = text.slice(nl + 1);
      else node.remove();
      return;
    }
  };

  const codeFile = (block, code) => {
    const title = $(".filename", block);
    if (title) {
      const name = title.textContent.trim();
      title.remove();
      return name;
    }
    const m = FILE_LINE.exec(code.textContent.split("\n", 1)[0]);
    if (!m) return "";
    dropFirstLine(code);
    return m[1];
  };

  const codeLang = (block) => {
    const cls = [...block.classList].find((c) => c.startsWith("language-"));
    return (cls && LANGS[cls.slice(9)]) || "";
  };

  const copyButton = (code) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.setAttribute("aria-label", "Copy code");
    btn.innerHTML = '<span class="i i--copy" aria-hidden="true"></span><span class="copy-btn__label">Copy</span>';
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.innerText.replace(/\n$/, ""));
      } catch (e) {
        toast("Copy failed: select the code instead");
        return;
      }
      const icon = $(".i", btn);
      btn.classList.add("is-done");
      icon.className = "i i--check";
      $(".copy-btn__label", btn).textContent = "Copied";
      toast("Copied to clipboard");
      setTimeout(() => {
        btn.classList.remove("is-done");
        icon.className = "i i--copy";
        $(".copy-btn__label", btn).textContent = "Copy";
      }, 1400);
    });
    return btn;
  };

  $$(".highlight").forEach((block) => {
    if (block.closest("[data-no-copy]") || block.classList.contains("doc-signature")) return;
    const code = $("pre code", block) || $("pre", block);
    if (!code) return;
    const file = codeFile(block, code);
    const lang = codeLang(block);
    const lines = code.textContent.replace(/\n$/, "").split("\n").length;
    const btn = copyButton(code);
    if (lines >= 7) block.classList.add("is-tall");
    if (file || (lang && lines >= 2)) {
      const head = document.createElement("div");
      head.className = "code-head";
      head.innerHTML = `<span class="code-head__file">${esc(file)}</span><span class="code-head__lang">${esc(lang)}</span>`;
      head.appendChild(btn);
      block.prepend(head);
    } else {
      block.classList.add("is-compact");
      block.appendChild(btn);
    }
  });

  /* ---------- Code tabs: fade the shown block only after the reader switches ---------- */
  document.addEventListener("change", (e) => {
    const input = e.target;
    if (input instanceof HTMLInputElement && input.matches(".tabbed-set > input")) {
      input.closest(".tabbed-set").classList.add("is-switched");
    }
  });

  /* ---------- Heading anchors: copy the link and confirm it where the reader clicked ---------- */
  let anchorTimer = 0;
  document.addEventListener("click", async (e) => {
    const link = e.target instanceof Element ? e.target.closest(".headerlink") : null;
    if (!link) return;
    try {
      await navigator.clipboard.writeText(link.href);
    } catch (err) {
      return; /* the browser still jumps to the heading; nothing to confirm */
    }
    $$(".headerlink.is-copied").forEach((el) => el.classList.remove("is-copied"));
    link.classList.add("is-copied");
    toast("Link copied");
    clearTimeout(anchorTimer);
    anchorTimer = setTimeout(() => link.classList.remove("is-copied"), 1400);
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
      `<a class="popover__more" href="${url("rules/")}">Read the Rules guide →</a>`;
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
        RULE_ID.test(n.nodeValue) && !n.parentElement.closest("a, button, h1, .rule-ref")
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_REJECT,
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const frag = document.createDocumentFragment();
      node.nodeValue.split(RULE_ID).forEach((part) => {
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

  /* Cross-document view transitions slide the active pill; a destination that is
     not in this sidebar is a section change, which also animates the sidebar. */
  addEventListener("pageswap", (e) => {
    const target = e.activation && e.activation.entry && e.activation.entry.url;
    if (!e.viewTransition || !target || !sideScroller) return;
    const path = (href) => new URL(href, location.href).pathname;
    const inSection = $$(".side__link", sideScroller).some(
      (a) => !a.closest(".side__sections") && path(a.href) === path(target),
    );
    if (inSection) return;
    e.viewTransition.types.add("section");
    try {
      sessionStorage.setItem("hn-section", "1");
    } catch (err) {
      /* storage blocked: only the outgoing page animates */
    }
  });

  /* ---------- Table of contents scroll-spy ---------- */
  const toc = $("[data-toc]");
  if (toc) {
    const links = $$("a", toc);
    const marker = $(".toc__marker", toc);
    const heads = links.map((a) => document.getElementById(decodeURIComponent(a.hash.slice(1)))).filter(Boolean);
    let current = null;
    /* At the bottom of the page a short final section never reaches the
       threshold: prefer the visible heading the URL names, else the last visible one. */
    const bottomIndex = () => {
      const visible = heads.map((h, i) => (h.getBoundingClientRect().top < innerHeight ? i : -1)).filter((i) => i >= 0);
      const named = heads.findIndex((h) => location.hash && h.id === decodeURIComponent(location.hash.slice(1)));
      return visible.includes(named) ? named : visible.length ? visible[visible.length - 1] : -1;
    };
    const setActive = () => {
      let idx = -1;
      heads.forEach((h, i) => {
        if (h.getBoundingClientRect().top <= 140) idx = i;
      });
      if (scrollY > 0 && innerHeight + scrollY >= document.documentElement.scrollHeight - 2) idx = Math.max(idx, bottomIndex());
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
    addEventListener("hashchange", setActive);
    setActive();
  }

  /* ---------- Sliding thumbs for segmented controls ---------- */
  const currentOf = (root) => $('[aria-pressed="true"], [aria-selected="true"]', root);

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
    if ("ResizeObserver" in window) new ResizeObserver(place).observe(root);
    addEventListener("resize", place, { passive: true });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(place);
  };
  const sliders = () => {
    $$("[data-slider]").forEach(setupSlider);
  };
  sliders();
  addEventListener("load", sliders);

  /* ---------- Scrollable regions reachable by keyboard ---------- */
  const focusables = () => {
    $$("pre, pre code, .tabbed-labels, .hn-walk__tabs, .top__nav, .hn-index__pages ul").forEach((el) => {
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
