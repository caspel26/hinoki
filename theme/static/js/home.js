/* Hinoki homepage interactions (v5):
   - Hero canvas: living Hinoki cypress rings, light wave, gliding arcs, komorebi motes
   - Live check console (violations / fixed / waived / --format json) driving the hero request-flow spine
   - Accessible tablists
   - Contract graph: per-arrow offset-path pulses + hover/focus inspector tooltips
   - Rust/Kiso <-> Python signal path: animated packet with a live FFI crossing counter
   - Kiso benchmark switcher, rule explorer, copy buttons, scroll-lit spine
   - Cursor spotlight on cards and the manual index page popovers
   - Decorative loops pause while off screen or while the tab is hidden */

(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

  const toast = (msg) => {
    if (window.HN && typeof window.HN.toast === "function") {
      window.HN.toast(msg);
    }
  };

  /* ---------- Hero check console ---------- */
  const setupConsole = (el) => {
    const hero = el.closest(".hn-hero");
    const buttons = $$("[data-state-btn]", el);
    const views = $$("[data-state-view]", el);
    const lines = $$(".hn-ln[data-line]", el);
    const diags = $$(".hn-diag[data-lines]", el);
    const cmdText = $("[data-cmd-text]", el);
    const statusBadge = $("[data-console-status]", el);

    const commands = {
      before: "$ uv run hinoki check",
      after: "$ uv run hinoki check",
      waived: "$ uv run hinoki check",
      json: "$ uv run hinoki check --format json",
    };

    const statuses = {
      before: { text: "exit 1 · 3 violated", cls: "is-bad" },
      after: { text: "exit 0 · 0 violated", cls: "is-ok" },
      waived: { text: "exit 0 · 3 waived", cls: "is-wvd" },
      json: { text: "exit 1 · json", cls: "is-bad" },
    };

    const setState = (state) => {
      el.dataset.state = state;
      if (hero) hero.dataset.hnState = state;
      buttons.forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.stateBtn === state)),
      );
      views.forEach((v) => {
        v.hidden = v.dataset.stateView !== state;
      });
      if (cmdText && commands[state]) {
        cmdText.textContent = commands[state];
      }
      if (statusBadge && statuses[state]) {
        statusBadge.textContent = statuses[state].text;
        statusBadge.className = `hn-console__status ${statuses[state].cls}`;
      }
      lines.forEach((l) => {
        l.classList.remove("is-linked");
        if (state === "before") {
          const n = l.dataset.line;
          l.classList.toggle("is-problem", n === "7" || n === "12");
        } else {
          l.classList.remove("is-problem");
        }
      });
    };

    buttons.forEach((b) =>
      b.addEventListener("click", () => setState(b.dataset.stateBtn)),
    );

    const highlight = (spec, on) => {
      const wanted = new Set((spec || "").split(",").filter(Boolean));
      lines.forEach((l) => {
        if (wanted.has(l.dataset.line)) l.classList.toggle("is-linked", on);
      });
    };

    diags.forEach((d) => {
      const spec = d.dataset.lines;
      d.addEventListener("pointerenter", () => highlight(spec, true));
      d.addEventListener("pointerleave", () => highlight(spec, false));
      d.addEventListener("focus", () => highlight(spec, true));
      d.addEventListener("blur", () => highlight(spec, false));
    });

    setState("before");
  };

  /* ---------- Sliding indicators & panel reveal helpers ---------- */
  const triggerPanelAnim = (el) => {
    if (!el || reduced()) return;
    el.classList.remove("hn-panel-anim");
    void el.offsetWidth;
    el.classList.add("hn-panel-anim");
  };

  const attachVSlider = (container) => {
    if (!container) return () => {};
    const bar = document.createElement("span");
    bar.className = "hn-vslider";
    bar.setAttribute("aria-hidden", "true");
    container.prepend(bar);
    container.classList.add("has-vslider");
    let current = null;
    const sync = (target, instant) => {
      if (target) current = target;
      if (!current || current.hidden || current.offsetHeight === 0) return;
      if (instant) bar.style.transition = "none";
      bar.style.transform = `translateY(${current.offsetTop}px)`;
      bar.style.height = `${current.offsetHeight}px`;
      bar.classList.add("is-ready");
      if (instant) {
        void bar.offsetHeight;
        bar.style.transition = "";
      }
    };
    window.addEventListener("resize", () => sync(null, true), { passive: true });
    return sync;
  };

  const attachSwitchThumb = (container) => {
    if (!container) return () => {};
    const thumb = document.createElement("span");
    thumb.className = "hn-switch__thumb";
    thumb.setAttribute("aria-hidden", "true");
    container.prepend(thumb);
    container.classList.add("has-thumb");
    let current = null;
    const sync = (btn, instant) => {
      if (btn) current = btn;
      if (!current || current.offsetWidth === 0) return;
      if (instant) thumb.style.transition = "none";
      thumb.style.transform = `translate(${current.offsetLeft}px, ${current.offsetTop}px)`;
      thumb.style.width = `${current.offsetWidth}px`;
      thumb.style.height = `${current.offsetHeight}px`;
      thumb.classList.add("is-ready");
      if (instant) {
        void thumb.offsetWidth;
        thumb.style.transition = "";
      }
    };
    window.addEventListener("resize", () => sync(null, true), { passive: true });
    return sync;
  };

  /* ---------- Accessible tabs ---------- */
  const setupTabs = (group) => {
    const tabs = $$('[role="tab"]', group);
    const panels = $$('[role="tabpanel"]', group);
    const vtabs = $(".hn-vtabs", group);
    const syncVSlider = vtabs ? attachVSlider(vtabs) : null;

    const activate = (tab, moveFocus, animate) => {
      const id = tab.getAttribute("aria-controls");
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
      });
      panels.forEach((p) => {
        const show = p.id === id;
        p.hidden = !show;
        if (show && animate) triggerPanelAnim(p);
      });
      if (syncVSlider) syncVSlider(tab, !animate);
      if (moveFocus) tab.focus();
    };

    tabs.forEach((t, i) => {
      t.addEventListener("click", () => activate(t, false, true));
      t.addEventListener("keydown", (e) => {
        const vert = t.closest('[aria-orientation="vertical"]');
        const prev = vert ? "ArrowUp" : "ArrowLeft";
        const next = vert ? "ArrowDown" : "ArrowRight";
        let target = -1;
        if (e.key === next) target = (i + 1) % tabs.length;
        else if (e.key === prev) target = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") target = 0;
        else if (e.key === "End") target = tabs.length - 1;
        if (target >= 0) {
          e.preventDefault();
          activate(tabs[target], true, true);
        }
      });
    });

    const initial = tabs.find((t) => t.getAttribute("aria-selected") === "true") || tabs[0];
    if (initial && syncVSlider) requestAnimationFrame(() => syncVSlider(initial, true));
  };

  /* ---------- Contract graph: per-arrow pulses + inspector tooltips ----------
     Each pulse follows its own arrow with CSS offset-path and fades out just
     before the arrowhead, so it never enters a box. */
  const addPulses = (figure) => {
    if (reduced()) return;
    const wires = $(".hn-dg-wires", figure);
    if (!wires) return;
    const ns = "http://www.w3.org/2000/svg";
    $$(".hn-dg-arrow", wires).forEach((path, idx) => {
      const dot = document.createElementNS(ns, "circle");
      const kind = [...path.classList].find((c) => c.startsWith("k-"));
      dot.setAttribute("class", `hn-dg-pulse ${kind || ""}`);
      dot.setAttribute("r", "2.6");
      dot.style.offsetPath = `path("${path.getAttribute("d")}")`;
      dot.style.setProperty("--d", `${(idx * 0.37).toFixed(2)}s`);
      wires.append(dot);
    });
  };

  const setupDiagram = (figure) => {
    addPulses(figure);
    const tip = document.createElement("div");
    tip.className = "popover";
    tip.hidden = true;
    document.body.appendChild(tip);
    const nodes = $$(".hn-dg-node", figure);

    const show = (node) => {
      tip.innerHTML = `<p class="popover__id">${node.dataset.tipTitle}</p><p style="margin:4px 0 0">${node.dataset.tip}</p>`;
      tip.hidden = false;
      const r = node.getBoundingClientRect();
      const w = tip.offsetWidth;
      tip.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, r.left + scrollX))}px`;
      tip.style.top = `${r.bottom + scrollY + 8}px`;
    };
    const hide = () => {
      tip.hidden = true;
    };

    nodes.forEach((n) => {
      n.addEventListener("pointerenter", () => show(n));
      n.addEventListener("pointerleave", hide);
      n.addEventListener("focus", () => show(n));
      n.addEventListener("blur", hide);
      n.addEventListener("click", () => show(n));
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") hide();
    });
    addEventListener("scroll", hide, { passive: true });
  };

  /* ---------- Rust/Kiso <-> Python signal path ----------
     A packet travels the active trace; the counter ticks each time it crosses
     the FFI horizon (y = 144) and the badge says which side it is on. */
  const setupLanes = (el) => {
    const buttons = $$("[data-lane-view]", el);
    const caption = $("[data-lane-caption]", el);
    const packet = $("[data-sig-packet]", el);
    const halo = $("[data-sig-halo]", el);
    const where = $("[data-ffi-where]", el);
    const now = $("[data-ffi-now]", el);
    const total = $("[data-ffi-total]", el);
    const HORIZON = 144;
    const DURATION = 3600;
    const PAUSE = 700;

    const meta = {
      python: {
        caption:
          "Python pipeline: Kiso owns the listener, HTTP/1.1 framing, host and CORS checks, route matching and JSON encoding in Rust; authentication, binding and the handler run in Python.",
        trace: ".hn-sig__trace--python",
      },
      native: {
        caption:
          "Native handler lane: Kiso also runs authentication, header, query and path binding and JSON Schema validation in Rust, then drives the Python handler coroutine directly.",
        trace: ".hn-sig__trace--native",
      },
      crud: {
        caption:
          "Generated CRUD: an eligible crud() resource (supported model and schemas, no custom hook or assignment rule, no access policy beyond an unmodified ClaimsJWTAuth) runs end to end in Rust: HTTP, routing, the database transaction and JSON encoding, without entering Python.",
        trace: ".hn-sig__trace--crud",
      },
    };

    let path = null;
    let length = 0;
    let crossings = [];
    let start = 0;
    let frame = 0;
    let visible = true;

    const measure = () => {
      length = path.getTotalLength();
      crossings = [];
      let prev = path.getPointAtLength(0).y > HORIZON;
      for (let d = 2; d <= length; d += 2) {
        const below = path.getPointAtLength(d).y > HORIZON;
        if (below !== prev) crossings.push(d / length);
        prev = below;
      }
    };

    const place = (t) => {
      const pt = path.getPointAtLength(t * length);
      [packet, halo].forEach((c) => {
        if (!c) return;
        c.setAttribute("cx", pt.x.toFixed(1));
        c.setAttribute("cy", pt.y.toFixed(1));
      });
      const passed = crossings.filter((f) => f <= t).length;
      const inPy = pt.y > HORIZON;
      el.classList.toggle("is-py", inPy);
      if (now) now.textContent = String(passed);
      if (where) where.textContent = inPy ? "in Python" : "in Rust";
    };

    const tick = (time) => {
      if (!start) start = time;
      const t = Math.min(((time - start) % (DURATION + PAUSE)) / DURATION, 1);
      place(t);
      frame = visible && !document.hidden ? requestAnimationFrame(tick) : 0;
    };

    const run = () => {
      if (reduced() || frame || !visible || document.hidden) return;
      start = 0;
      frame = requestAnimationFrame(tick);
    };

    const setView = (view) => {
      const cfg = meta[view];
      if (!cfg) return;
      el.dataset.view = view;
      buttons.forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.laneView === view)),
      );
      if (caption) caption.textContent = cfg.caption;
      path = $(cfg.trace, el);
      if (!path) return;
      measure();
      if (total) total.textContent = String(crossings.length);
      start = 0;
      place(reduced() ? 1 : 0);
      run();
    };

    buttons.forEach((b) =>
      b.addEventListener("click", () => setView(b.dataset.laneView)),
    );
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((entries) => {
        visible = entries.some((e) => e.isIntersecting);
        run();
      }).observe(el);
    }
    document.addEventListener("visibilitychange", run);
    setView("python");
  };

  /* ---------- Interactive Benchmark Workload Switcher ---------- */
  const setupBench = (el) => {
    const buttons = $$("[data-bench-case]", el);
    const panels = $$("[data-bench-panel]", el);
    const switchEl = $(".hn-switch", el);
    const syncThumb = attachSwitchThumb(switchEl);

    const setCase = (id, animate) => {
      let activeBtn = null;
      buttons.forEach((b) => {
        const on = b.dataset.benchCase === id;
        b.setAttribute("aria-pressed", String(on));
        if (on) activeBtn = b;
      });
      if (activeBtn) syncThumb(activeBtn, !animate);
      panels.forEach((p) => {
        const show = p.dataset.benchPanel === id;
        p.hidden = !show;
        if (show && animate) triggerPanelAnim(p);
      });
    };
    buttons.forEach((b) =>
      b.addEventListener("click", () => setCase(b.dataset.benchCase, true)),
    );
    setCase("json1k", false);
    requestAnimationFrame(() => {
      const activeBtn = buttons.find((b) => b.getAttribute("aria-pressed") === "true");
      if (activeBtn) syncThumb(activeBtn, true);
    });
  };

  /* ---------- Rule explorer ---------- */
  const setupExplorer = (el) => {
    const filters = $$("[data-scope-filter]", el);
    const items = $$("[data-rule-item]", el);
    const details = $$("[data-detail]", el);
    const listEl = $(".hn-explorer__list", el);
    const switchEl = $(".hn-switch", el);
    const syncVSlider = attachVSlider(listEl);
    const syncThumb = attachSwitchThumb(switchEl);

    const select = (id, animate) => {
      let activeLi = null;
      items.forEach((li) => {
        const on = li.dataset.ruleId === id;
        const btn = $("button", li);
        if (btn) btn.setAttribute("aria-pressed", String(on));
        if (on) activeLi = li;
      });
      details.forEach((d) => {
        const show = d.dataset.detail === id;
        d.hidden = !show;
        if (show && animate) triggerPanelAnim(d);
      });
      if (activeLi) syncVSlider(activeLi, !animate);
    };

    const filter = (scope, animate) => {
      let activeBtn = null;
      filters.forEach((f) => {
        const on = f.dataset.scopeFilter === scope;
        f.setAttribute("aria-pressed", String(on));
        if (on) activeBtn = f;
      });
      if (activeBtn) syncThumb(activeBtn, !animate);

      let firstVisible = null;
      let activeLi = null;
      items.forEach((li) => {
        const show = scope === "all" || li.dataset.scope === scope;
        li.hidden = !show;
        if (show && !firstVisible) firstVisible = li.dataset.ruleId;
        const btn = $("button", li);
        if (show && btn && btn.getAttribute("aria-pressed") === "true") {
          activeLi = li;
        }
      });
      if (!activeLi && firstVisible) {
        select(firstVisible, animate);
      } else if (activeLi) {
        syncVSlider(activeLi, !animate);
      }
    };

    filters.forEach((f) =>
      f.addEventListener("click", () => filter(f.dataset.scopeFilter, true)),
    );
    items.forEach((li) => {
      const btn = $("button", li);
      if (btn) btn.addEventListener("click", () => select(li.dataset.ruleId, true));
    });

    filter("all", false);
    select("HNK-ACC-001", false);
    requestAnimationFrame(() => {
      const activeBtn = filters.find((f) => f.getAttribute("aria-pressed") === "true");
      if (activeBtn) syncThumb(activeBtn, true);
      const activeLi = items.find((li) => $("button", li)?.getAttribute("aria-pressed") === "true");
      if (activeLi) syncVSlider(activeLi, true);
    });
  };

  /* ---------- Copy buttons ---------- */
  const setupCopy = (btn) => {
    btn.addEventListener("click", async () => {
      let text = btn.dataset.copyText || "";
      if (!text && btn.dataset.copyFrom) {
        const src = document.getElementById(btn.dataset.copyFrom);
        if (src) text = src.textContent || "";
      }
      if (!text) {
        const box = btn.closest(".hn-console, .hn-codebox, .hn-install__panel");
        const active =
          box &&
          ($("[data-state-view]:not([hidden]) pre", box) ||
            $("pre", box));
        if (active) text = active.textContent || "";
      }
      text = text.trim();
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        const orig = btn.textContent;
        btn.textContent = "Copied";
        btn.dataset.copied = "1";
        toast("Copied to clipboard");
        setTimeout(() => {
          btn.textContent = orig;
          delete btn.dataset.copied;
        }, 1600);
      } catch {
        toast("Copy unavailable");
      }
    });
  };

  /* ---------- Spine scroll highlight ---------- */
  const setupSpine = () => {
    const secs = $$(".hn-sec");
    if (!secs.length || !("IntersectionObserver" in window)) return;
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) e.target.classList.add("is-lit");
        });
      },
      { rootMargin: "-12% 0px -30% 0px", threshold: 0.05 },
    );
    secs.forEach((s) => io.observe(s));
  };

  /* ---------- Hero ambient field: living Hinoki cypress growth rings ----------
     Canvas renderer. Organic concentric rings breathe on slow phase-drifting
     harmonics and a calm radial light wave ripples outward from the core every
     few seconds; luminous arcs glide along the live curves through the part of
     each ring that is not hidden behind the console; komorebi motes drift up
     with a soft pointer response. Pauses off screen and in hidden tabs, and
     renders one static frame under prefers-reduced-motion. */
  const TAU = Math.PI * 2;
  const RING_ASPECT = 0.72;
  const RING_TILT = -0.1;
  const WAVE_PERIOD = 5.6;
  const WAVE_TRAVEL = 4.6;
  const HARMONICS = [
    { m: 2, amp: 0.034, speed: 0.16, lag: 0.16, phase: 0.4 },
    { m: 3, amp: 0.021, speed: -0.22, lag: 0.22, phase: 2.1 },
    { m: 5, amp: 0.01, speed: 0.29, lag: 0.3, phase: 4.4 },
    { m: 7, amp: 0.005, speed: -0.37, lag: 0.4, phase: 1.3 },
  ];
  const STAGE_PALETTES = {
    dark: {
      wood: [216, 180, 138],
      brand: [185, 160, 231],
      ring: 0.5,
      edge: 0.3,
      halo: 0.16,
      arc: 1,
      mote: 0.9,
      wave: 0.085,
      blend: "lighter",
    },
    light: {
      wood: [135, 86, 44],
      brand: [104, 76, 170],
      ring: 0.5,
      edge: 0.36,
      halo: 0.1,
      arc: 0.85,
      mote: 0.62,
      wave: 0.05,
      blend: "source-over",
    },
  };
  const ARC_PREFERENCE = [3, 5, 7, 9, 4, 6, 8, 2, 10, 1];

  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a.toFixed(3)})`;
  const smooth01 = (x) => (x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x));

  const seeded = (seed) => () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let x = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };

  const makeSprite = (c) => {
    const sprite = document.createElement("canvas");
    sprite.width = 64;
    sprite.height = 64;
    const g = sprite.getContext("2d");
    const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, rgba(c, 1));
    grad.addColorStop(0.12, rgba(c, 0.7));
    grad.addColorStop(0.36, rgba(c, 0.16));
    grad.addColorStop(1, rgba(c, 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, 64, 64);
    return sprite;
  };

  const shapeAt = (theta, phases) => {
    let k = 1;
    for (let j = 0; j < HARMONICS.length; j += 1) {
      k += HARMONICS[j].amp * Math.sin(HARMONICS[j].m * theta + phases[j]);
    }
    return k;
  };

  const ringPoint = (ring, theta) => {
    const k = ring.r * shapeAt(theta, ring.phases);
    return { x: k * Math.cos(theta), y: k * Math.sin(theta) * RING_ASPECT };
  };

  const toScreen = (geo, x, y) => ({
    x: geo.cx + x * Math.cos(RING_TILT) - y * Math.sin(RING_TILT),
    y: geo.cy + x * Math.sin(RING_TILT) + y * Math.cos(RING_TILT),
  });

  const toLocal = (x, y) => ({
    x: x * Math.cos(RING_TILT) + y * Math.sin(RING_TILT),
    y: -x * Math.sin(RING_TILT) + y * Math.cos(RING_TILT),
  });

  /* Open = on canvas, above the vertical fade, inside the feathered mask and
     clear of the console panel and the hero copy (headline, lead, actions). */
  const isOpen = (geo, p, reach = 0.7) => {
    if (p.x < 8 || p.x > geo.W - 8 || p.y < 4 || p.y > geo.H * 0.62) return false;
    if (Math.hypot(p.x - geo.cx, p.y - geo.cy) > geo.fade * reach) return false;
    return !geo.blocks.some((b) => p.x > b.l - 12 && p.x < b.r + 12 && p.y > b.t - 12 && p.y < b.b + 12);
  };

  const relRect = (el, sr) => {
    const r = el ? el.getBoundingClientRect() : null;
    if (!r || !r.width) return null;
    return { l: r.left - sr.left, r: r.right - sr.left, t: r.top - sr.top, b: r.bottom - sr.top };
  };

  const stageGeometry = (stage, W, H) => {
    const sr = stage.getBoundingClientRect();
    const con = relRect($(".hn-hero .hn-console"), sr);
    const copy = relRect($(".hn-hero__copy"), sr);
    const title = relRect($(".hn-hero__title"), sr);
    // Narrow screens and the stacked hero (console below the copy) use a compact
    // top-right corner composition instead of centering on the console.
    const corner = W < 900 || !con || con.t > H * 0.5;
    const geo = { W, H, corner, blocks: [] };
    if (con) geo.blocks.push({ ...con, b: Infinity });
    if (copy && title) geo.blocks.push({ ...copy, t: title.t });
    if (corner) {
      geo.s = Math.max(0.42, Math.min(0.8, W / 820));
      geo.fade = Math.min(W * 0.82, 760);
      geo.cx = W - Math.max(20, W * 0.05);
      geo.cy = 26;
    } else {
      geo.s = Math.min(1.25, Math.max(0.78, W / 1440));
      geo.fade = 1150 * geo.s;
      geo.cx = con.l + (con.r - con.l) * 0.72;
      geo.cy = con.t + 6;
    }
    return geo;
  };

  const buildRings = (geo) => {
    const rand = seeded(7);
    const count = geo.corner ? 11 : 13;
    const rings = [];
    let r = 62 * geo.s;
    for (let i = 0; i < count; i += 1) {
      rings.push({ r0: r, r, wood: i % 3 === 0, fade: 1 - 0.55 * (i / (count - 1)), glow: 0, phases: [0, 0, 0, 0] });
      r += (38 + 7.2 * i) * geo.s * (0.86 + 0.28 * rand());
    }
    return rings;
  };

  /* Longest contiguous visible angular run of a ring, unwrapped as [a0, a1]. */
  const visibleRun = (geo, r) => {
    const steps = 180;
    const open = [];
    for (let j = 0; j < steps; j += 1) {
      const th = (j / steps) * TAU;
      open.push(isOpen(geo, toScreen(geo, r * Math.cos(th), r * Math.sin(th) * RING_ASPECT), 0.56));
    }
    let best = [0, 0];
    let start = -1;
    for (let j = 0; j < steps * 2; j += 1) {
      if (!open[j % steps]) {
        start = -1;
        continue;
      }
      if (start < 0) start = j;
      const len = Math.min(steps, j + 1 - start);
      if (len > best[1] - best[0]) best = [start, start + len];
    }
    return [(best[0] / steps) * TAU, (best[1] / steps) * TAU];
  };

  const pickArcs = (geo, rings) => {
    const arcs = [];
    ARC_PREFERENCE.forEach((idx) => {
      const ring = rings[idx];
      if (!ring || arcs.length >= 4) return;
      const [a0, a1] = visibleRun(geo, ring.r0);
      if ((a1 - a0) * ring.r0 < 150 * geo.s) return;
      const n = arcs.length;
      arcs.push({
        ring: idx,
        a0,
        a1,
        dir: n % 2 ? -1 : 1,
        wood: n % 2 === 0,
        len: Math.min(1, (250 * geo.s) / ring.r0),
        speed: ((44 + 6 * n) * geo.s) / ring.r0,
        offset: [0.1, 0.55, 0.3, 0.8][n],
        phase: n * 1.7,
      });
    });
    return arcs;
  };

  const focusPoint = (geo, rings) => {
    let x = 0;
    let y = 0;
    let n = 0;
    rings.forEach((ring) => {
      for (let j = 0; j < 48; j += 1) {
        const th = (j / 48) * TAU;
        const p = toScreen(geo, ring.r0 * Math.cos(th), ring.r0 * Math.sin(th) * RING_ASPECT);
        if (!isOpen(geo, p)) continue;
        x += p.x;
        y += p.y;
        n += 1;
      }
    });
    return n ? { x: x / n, y: y / n } : { x: geo.cx, y: geo.cy };
  };

  const sampleOpen = (geo, rand) => {
    let p = { x: geo.cx, y: geo.cy };
    const reach = geo.fade * 0.62;
    for (let tries = 0; tries < 24; tries += 1) {
      const th = rand() * TAU;
      const d = Math.sqrt(rand()) * reach;
      p = { x: geo.cx + d * Math.cos(th), y: geo.cy + d * Math.sin(th) * 0.8 };
      if (isOpen(geo, p)) return p;
    }
    return p;
  };

  const spawnMote = (m, st, fresh) => {
    const { geo, rand } = st;
    const p = sampleOpen(geo, rand);
    const away = Math.atan2(p.y - geo.cy, p.x - geo.cx);
    Object.assign(m, {
      x: p.x,
      y: p.y,
      vx: Math.cos(away) * (1.5 + 3 * rand()) * geo.s,
      vy: -(5 + 8 * rand()) * geo.s,
      max: 9 + 8 * rand(),
      r: (1.1 + 1.5 * rand()) * Math.max(0.8, geo.s),
      depth: 0.35 + 0.65 * rand(),
      wood: rand() < 0.58,
      phase: rand() * TAU,
      sway: (5 + 9 * rand()) * geo.s,
      freq: 0.25 + 0.3 * rand(),
      ox: m.ox || 0,
      oy: m.oy || 0,
    });
    m.life = fresh ? rand() * m.max : 0;
    m.x += m.vx * m.life;
    m.y += m.vy * m.life;
  };

  const waveAt = (t, reach) => {
    const tau = (t % WAVE_PERIOD) / WAVE_TRAVEL;
    if (tau >= 1) return null;
    return {
      front: Math.sin((tau * Math.PI) / 2) * reach,
      env: (1 - tau) ** 0.7 * smooth01(tau * 6),
    };
  };

  const updateRings = (st) => {
    const { t, rings, geo } = st;
    const wave = st.still ? null : waveAt(t, rings[rings.length - 1].r0 * 1.04);
    rings.forEach((ring, i) => {
      const sigma = 48 * geo.s + 0.07 * ring.r0;
      const g = wave ? wave.env * Math.exp(-(((ring.r0 - wave.front) / sigma) ** 2)) : 0;
      ring.glow = g;
      ring.r = ring.r0 * (1 + 0.016 * Math.sin(0.52 * t - i * 0.42)) + 7 * geo.s * g;
      HARMONICS.forEach((h, j) => {
        ring.phases[j] = h.phase + h.speed * t - i * h.lag;
      });
    });
    st.wave = wave;
  };

  const updatePointer = (st, dt) => {
    const k = 1 - Math.exp(-dt * 2.2);
    st.ptr.x += (st.ptr.tx - st.ptr.x) * k;
    st.ptr.y += (st.ptr.ty - st.ptr.y) * k;
  };

  const updateMote = (st, m, dt) => {
    m.life += dt;
    if (m.life >= m.max) spawnMote(m, st, false);
    m.x += m.vx * dt;
    m.y += m.vy * dt;
    let tx = 0;
    let ty = 0;
    if (st.ptr.on) {
      const dx = st.ptr.px - m.x;
      const dy = st.ptr.py - m.y;
      const f = Math.max(0, 1 - Math.hypot(dx, dy) / 220) ** 2;
      tx = dx * 0.2 * f;
      ty = dy * 0.2 * f;
    }
    const k = 1 - Math.exp(-dt * 2);
    m.ox += (tx - m.ox) * k;
    m.oy += (ty - m.oy) * k;
  };

  const tracePath = (ctx, ring) => {
    const n = Math.min(240, Math.max(72, Math.round(ring.r / 2.4)));
    ctx.beginPath();
    for (let j = 0; j <= n; j += 1) {
      const p = ringPoint(ring, (j / n) * TAU);
      if (j) ctx.lineTo(p.x, p.y);
      else ctx.moveTo(p.x, p.y);
    }
    ctx.closePath();
  };

  const litGradient = (ctx, c, pool, edge) => {
    const grad = ctx.createRadialGradient(pool.x, pool.y, 0, pool.x, pool.y, pool.r);
    grad.addColorStop(0, rgba(c, 1));
    grad.addColorStop(0.55, rgba(c, 0.62));
    grad.addColorStop(1, rgba(c, edge));
    return grad;
  };

  const drawWave = (ctx, st) => {
    const { wave, pal, geo } = st;
    if (!wave || wave.env < 0.01) return;
    const w = 26 * geo.s + 0.1 * wave.front;
    const outer = wave.front + w;
    ctx.save();
    ctx.scale(1, RING_ASPECT);
    const grad = ctx.createRadialGradient(0, 0, Math.max(0, wave.front - w), 0, 0, outer);
    grad.addColorStop(0, rgba(pal.wood, 0));
    grad.addColorStop(0.5, rgba(pal.wood, pal.wave * wave.env));
    grad.addColorStop(1, rgba(pal.wood, 0));
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(0, 0, outer, 0, TAU);
    ctx.fill();
    ctx.restore();
  };

  const drawRings = (ctx, st, pool) => {
    const { pal } = st;
    const lit = {
      wood: litGradient(ctx, pal.wood, pool, pal.edge),
      brand: litGradient(ctx, pal.brand, pool, pal.edge),
    };
    st.rings.forEach((ring) => {
      const g = ring.glow;
      const c = ring.wood ? pal.wood : pal.brand;
      tracePath(ctx, ring);
      if (g > 0.06) {
        ctx.globalAlpha = pal.halo * g * ring.fade;
        ctx.lineWidth = 5;
        ctx.strokeStyle = rgba(c, 1);
        ctx.stroke();
      }
      ctx.globalAlpha = Math.min(1, pal.ring * ring.fade * (1 + 1.5 * g));
      ctx.lineWidth = 1 + 0.9 * g;
      ctx.strokeStyle = ring.wood ? lit.wood : lit.brand;
      ctx.stroke();
    });
    ctx.globalAlpha = 1;
  };

  const arcHead = (arc, t) => {
    const span = arc.a1 - arc.a0 + arc.len;
    const period = span * 1.08;
    const u = (t * arc.speed + arc.offset * period) % period;
    return arc.dir > 0 ? arc.a0 + u : arc.a1 - u;
  };

  const stroke = (ctx, a, b, color, width) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  };

  const drawArc = (ctx, st, arc) => {
    const ring = st.rings[arc.ring];
    const c = arc.wood ? st.pal.wood : st.pal.brand;
    const head = arcHead(arc, st.t);
    const fw = Math.min(0.5, (arc.a1 - arc.a0) * 0.22);
    const edge = (th) => smooth01((th - arc.a0) / fw) * smooth01((arc.a1 - th) / fw);
    const pulse = st.pal.arc * (0.82 + 0.18 * Math.sin(0.9 * st.t + arc.phase));
    const n = 30;
    let prev = null;
    for (let j = 0; j <= n; j += 1) {
      const f = j / n;
      const th = head - arc.dir * arc.len * (1 - f);
      const p = ringPoint(ring, th);
      const a = pulse * edge(th) * f * f;
      if (prev && a > 0.002) {
        stroke(ctx, prev, p, rgba(c, a * 0.22), 7);
        stroke(ctx, prev, p, rgba(c, Math.min(1, a * 1.1)), 1.2 + 1.5 * f);
      }
      prev = p;
    }
    const e = edge(head);
    if (e < 0.01) return;
    const size = 26 * Math.max(0.7, st.geo.s);
    ctx.globalAlpha = pulse * e * 0.7;
    ctx.drawImage(arc.wood ? st.sprites.wood : st.sprites.brand, prev.x - size / 2, prev.y - size / 2, size, size);
    ctx.globalAlpha = 1;
  };

  const drawMotes = (ctx, st) => {
    const { t, ptr, geo, pal } = st;
    st.motes.forEach((m) => {
      const life = Math.sin((Math.PI * m.life) / m.max);
      const a = pal.mote * Math.max(0, life) ** 1.4 * (0.78 + 0.22 * Math.sin(1.1 * t + m.phase));
      if (a < 0.005) return;
      const x = m.x + m.sway * Math.sin(m.freq * t + m.phase) + ptr.x * 26 * geo.s * m.depth + m.ox;
      const y = m.y + ptr.y * 18 * geo.s * m.depth + m.oy;
      const size = m.r * 10;
      ctx.globalAlpha = Math.min(1, a);
      ctx.drawImage(m.wood ? st.sprites.wood : st.sprites.brand, x - size / 2, y - size / 2, size, size);
    });
    ctx.globalAlpha = 1;
  };

  const lightPool = (st, ox, oy) => {
    const { geo, t, ptr } = st;
    const px = geo.focus.x + geo.fade * 0.2 * Math.sin(0.052 * t) + ptr.x * 90;
    const py = geo.focus.y + geo.fade * 0.1 * Math.cos(0.071 * t + 0.8) + ptr.y * 60;
    return { ...toLocal(px - ox, py - oy), r: geo.fade * 0.62 };
  };

  const renderStage = (st) => {
    const { ctx, dpr, geo, pal } = st;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.clearRect(0, 0, st.W, st.H);
    if (!geo) return;
    updateRings(st);
    const ox = geo.cx + st.ptr.x * 16 * geo.s;
    const oy = geo.cy + st.ptr.y * 12 * geo.s;
    ctx.translate(ox, oy);
    ctx.rotate(RING_TILT);
    ctx.globalCompositeOperation = pal.blend;
    drawWave(ctx, st);
    ctx.globalCompositeOperation = "source-over";
    drawRings(ctx, st, lightPool(st, ox, oy));
    ctx.globalCompositeOperation = pal.blend;
    ctx.lineCap = "butt";
    st.arcs.forEach((arc) => drawArc(ctx, st, arc));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawMotes(ctx, st);
    ctx.globalCompositeOperation = "source-over";
  };

  const layoutStage = (st, stage, respawn) => {
    const geo = stageGeometry(stage, st.W, st.H);
    st.geo = geo;
    st.rings = buildRings(geo);
    st.arcs = pickArcs(geo, st.rings);
    geo.focus = focusPoint(geo, st.rings);
    stage.style.setProperty("--hn-cx", `${geo.cx.toFixed(1)}px`);
    stage.style.setProperty("--hn-cy", `${geo.cy.toFixed(1)}px`);
    stage.style.setProperty("--hn-fade", `${geo.fade.toFixed(0)}px`);
    const count = geo.corner ? 16 : 28;
    if (!respawn && st.motes.length === count) return;
    st.rand = seeded(11);
    st.motes = Array.from({ length: count }, () => {
      const m = {};
      spawnMote(m, st, true);
      return m;
    });
  };

  const applyTheme = (st) => {
    st.pal = STAGE_PALETTES[document.documentElement.dataset.theme === "light" ? "light" : "dark"];
    st.sprites = { wood: makeSprite(st.pal.wood), brand: makeSprite(st.pal.brand) };
  };

  const trackPointer = (st, stage) => {
    if (!matchMedia("(pointer: fine)").matches) return;
    addEventListener(
      "pointermove",
      (e) => {
        if (!st.frame) return;
        const r = stage.getBoundingClientRect();
        Object.assign(st.ptr, {
          tx: e.clientX / innerWidth - 0.5,
          ty: e.clientY / innerHeight - 0.5,
          px: e.clientX - r.left,
          py: e.clientY - r.top,
          on: true,
        });
      },
      { passive: true },
    );
    document.addEventListener("pointerout", (e) => {
      if (e.relatedTarget) return;
      Object.assign(st.ptr, { tx: 0, ty: 0, on: false });
    });
  };

  const setupStage = (stage) => {
    const canvas = $("[data-hn-stage-canvas]", stage);
    const ctx = canvas && canvas.getContext ? canvas.getContext("2d") : null;
    if (!ctx) return;
    const motion = matchMedia("(prefers-reduced-motion: reduce)");
    const st = {
      ctx, t: 8.4, last: 0, frame: 0, visible: true, still: motion.matches,
      W: 0, H: 0, dpr: 1, geo: null, rings: [], arcs: [], motes: [],
      ptr: { tx: 0, ty: 0, x: 0, y: 0, px: 0, py: 0, on: false },
    };
    applyTheme(st);

    const tick = (ts) => {
      const dt = st.last ? Math.min(0.05, (ts - st.last) / 1000) : 1 / 60;
      st.last = ts;
      st.t += dt;
      updatePointer(st, dt);
      st.motes.forEach((m) => updateMote(st, m, dt));
      renderStage(st);
      st.frame = requestAnimationFrame(tick);
    };
    const sync = () => {
      st.still = motion.matches;
      const run = st.visible && !document.hidden && !st.still;
      if (run && !st.frame) {
        st.last = 0;
        st.frame = requestAnimationFrame(tick);
      } else if (!run && st.frame) {
        cancelAnimationFrame(st.frame);
        st.frame = 0;
      }
      if (st.still) renderStage(st);
    };
    const resize = (respawn) => {
      const W = stage.clientWidth;
      const H = stage.clientHeight;
      if (!W || !H) return;
      st.dpr = Math.min(2, window.devicePixelRatio || 1);
      st.W = W;
      st.H = H;
      canvas.width = Math.round(W * st.dpr);
      canvas.height = Math.round(H * st.dpr);
      layoutStage(st, stage, respawn);
      renderStage(st);
    };

    resize(true);
    if ("ResizeObserver" in window) new ResizeObserver(() => resize(true)).observe(stage);
    else addEventListener("resize", () => resize(true));
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => resize(false));
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((entries) => {
        st.visible = entries.some((e) => e.isIntersecting);
        sync();
      }).observe(stage);
    }
    new MutationObserver(() => {
      applyTheme(st);
      renderStage(st);
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    document.addEventListener("visibilitychange", sync);
    motion.addEventListener("change", sync);
    trackPointer(st, stage);
    sync();
  };

  /* ---------- Cursor spotlight on cards (fine pointers) ---------- */
  const setupSpotlight = () => {
    if (!matchMedia("(pointer: fine)").matches) return;
    document.addEventListener(
      "pointermove",
      (e) => {
        const card = e.target.closest && e.target.closest(".hn-card");
        if (!card) return;
        const r = card.getBoundingClientRect();
        card.style.setProperty("--mx", `${(e.clientX - r.left).toFixed(0)}px`);
        card.style.setProperty("--my", `${(e.clientY - r.top).toFixed(0)}px`);
      },
      { passive: true },
    );
  };

  /* ---------- The manual: page counts + hover preview / click-pinned popover ---------- */
  const setupIndex = (root) => {
    $$(".hn-index__card", root).forEach((card) => {
      const toggle = $(".hn-index__toggle", card);
      const count = $(".hn-index__count", card);
      const pages = $$(".hn-index__pages a", card).length;
      if (count) count.textContent = `${pages} page${pages === 1 ? "" : "s"}`;
      if (!toggle) return;
      let pinned = false;
      const set = (open) => {
        card.classList.toggle("is-open", open);
        toggle.setAttribute("aria-expanded", String(open));
      };
      const close = () => {
        pinned = false;
        set(false);
      };
      toggle.addEventListener("click", () => {
        pinned = !pinned;
        set(pinned);
      });
      card.addEventListener("pointerenter", (e) => e.pointerType === "mouse" && set(true));
      card.addEventListener("pointerleave", (e) => e.pointerType === "mouse" && !pinned && set(false));
      card.addEventListener("focusout", (e) => !card.contains(e.relatedTarget) && close());
      card.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
          close();
          toggle.focus();
        }
      });
    });
  };

  /* ---------- Decorative loops run only while visible ---------- */
  const setupIdle = (targets) => {
    if (!targets.length) return;
    const visible = new Map(targets.map((t) => [t, true]));
    const apply = () =>
      targets.forEach((t) => t.classList.toggle("is-idle", document.hidden || !visible.get(t)));
    if ("IntersectionObserver" in window) {
      const io = new IntersectionObserver((entries) => {
        entries.forEach((e) => visible.set(e.target, e.isIntersecting));
        apply();
      });
      targets.forEach((t) => io.observe(t));
    }
    document.addEventListener("visibilitychange", apply);
  };

  const init = () => {
    $$("[data-hn-stage]").forEach(setupStage);
    setupIdle($$(".hn-stage, .hn-hero, .hn-arch"));
    setupSpotlight();
    $$("[data-hn-index]").forEach(setupIndex);
    $$("[data-hn-console]").forEach(setupConsole);
    $$("[data-hn-tabs]").forEach(setupTabs);
    $$("[data-hn-diagram]").forEach(setupDiagram);
    $$("[data-hn-lanes]").forEach(setupLanes);
    $$("[data-hn-bench]").forEach(setupBench);
    $$("[data-hn-explorer]").forEach(setupExplorer);
    $$("[data-hn-copy]").forEach(setupCopy);
    setupSpine();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
