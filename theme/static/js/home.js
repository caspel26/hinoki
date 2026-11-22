/* Homepage behaviour: scenario switcher, workbench tabs, rule explorer,
   contents index, copy buttons, diagram tips and the hero glow that follows
   the pointer. Nothing changes content without user input. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const toast = (t) => window.HN && window.HN.toast && window.HN.toast(t);

  /* ---------- Hero panel: scenarios ---------- */
  const setupPanel = (panel) => {
    const buttons = $$("[data-state]", panel);
    const lines = $$(".hn-ln[data-line]", panel);

    const files = $$("[data-hn-file]", panel);
    const problems = $("[data-hn-problems]", panel);
    const count = $("[data-hn-count]", panel);
    const show = (button) => {
      const hero = panel.closest(".hn-hero");
      if (hero) hero.dataset.hnState = button.dataset.state;
      buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      $$("[data-state-view]", panel).forEach((v) => {
        v.hidden = v.dataset.stateView !== button.dataset.state;
      });
      files.forEach((f) => {
        f.textContent = button.dataset.file;
      });
      const bump = (el) => {
        el.classList.remove("is-bump");
        void el.offsetWidth;
        el.classList.add("is-bump");
      };
      if (problems) {
        problems.textContent = button.dataset.problems;
        problems.className = `ide__prob is-${button.dataset.tone}`;
        bump(problems);
      }
      if (count) {
        count.textContent = button.dataset.count;
        bump(count);
      }
    };

    buttons.forEach((button) => button.addEventListener("click", () => show(button)));

    const link = (diag, on) => {
      const wanted = (diag.dataset.lines || "").split(" ");
      lines.forEach((l) => l.classList.toggle("is-linked", on && wanted.includes(l.dataset.line)));
    };
    $$(".hn-diag", panel).forEach((diag) => {
      diag.addEventListener("pointerenter", () => link(diag, true));
      diag.addEventListener("pointerleave", () => link(diag, false));
      diag.addEventListener("focus", () => link(diag, true));
      diag.addEventListener("blur", () => link(diag, false));
    });
  };

  /* ---------- Hero glow trails the pointer ---------- */
  /* Fine pointers only. Each disc eases toward the pointer with its own time
     constant, so together they leave a fading trail; the loop stops once every
     disc has settled and the discs fade out when the pointer leaves the hero. */
  const setupGlow = (hero) => {
    const discs = $$(".hn-hero__bg i", hero).map((el, n) => ({
      el,
      tau: 45 + n * 50,
      scale: 1 - n * 0.12,
      x: 0,
      y: 0,
    }));
    const fine = matchMedia("(pointer: fine) and (prefers-reduced-motion: no-preference)");
    if (!discs.length || !fine.matches) return;
    let target = { x: 0, y: 0 };
    let frame = 0;
    let last = 0;
    const step = (time) => {
      const dt = Math.min(time - last || 16, 48);
      last = time;
      let moving = false;
      for (const d of discs) {
        // Frame-rate independent easing: the same feel at 60 and 144 Hz.
        const k = 1 - Math.exp(-dt / d.tau);
        d.x += (target.x - d.x) * k;
        d.y += (target.y - d.y) * k;
        d.el.style.transform = `translate3d(${d.x.toFixed(2)}px, ${d.y.toFixed(2)}px, 0) scale(${d.scale})`;
        moving ||= Math.abs(target.x - d.x) > 0.2 || Math.abs(target.y - d.y) > 0.2;
      }
      frame = moving ? requestAnimationFrame(step) : 0;
      if (!moving) last = 0;
    };
    hero.addEventListener("pointermove", (event) => {
      const box = hero.getBoundingClientRect();
      target = { x: event.clientX - box.left, y: event.clientY - box.top + 40 };
      if (!hero.classList.contains("has-pointer")) {
        for (const d of discs) Object.assign(d, target);
      }
      hero.classList.add("has-pointer");
      if (!frame) frame = requestAnimationFrame(step);
    });
    hero.addEventListener("pointerleave", () => hero.classList.remove("has-pointer"));
  };

  /* ---------- Tabs (workbench and commands) ---------- */
  const setupTabs = (root) => {
    const tabs = $$('[role="tab"]', root);

    const select = (tab, focus) => {
      tabs.forEach((other) => {
        const on = other === tab;
        other.setAttribute("aria-selected", String(on));
        other.tabIndex = on ? 0 : -1;
        const panel = $(`[data-panel="${other.dataset.tab}"]`, root);
        if (panel) panel.hidden = !on;
      });
      if (focus) tab.focus();
    };

    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => select(tab));
      tab.addEventListener("keydown", (event) => {
        const move = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 1, ArrowUp: -1 }[event.key];
        if (event.key === "Home") select(tabs[0], true);
        else if (event.key === "End") select(tabs[tabs.length - 1], true);
        else if (move) {
          event.preventDefault();
          select(tabs[(index + move + tabs.length) % tabs.length], true);
        }
      });
    });
  };

  /* ---------- Rule explorer ---------- */
  const setupExplorer = (root) => {
    const rules = $$("[data-rule]", root);
    const filters = $$("[data-phase]", root).filter((b) => !b.dataset.rule);
    const show = (rule) => {
      rules.forEach((b) => b.setAttribute("aria-pressed", String(b === rule)));
      $$("[data-detail]", root).forEach((d) => {
        d.hidden = d.dataset.detail !== rule.dataset.rule;
      });
    };
    rules.forEach((button) => button.addEventListener("click", () => show(button)));
    filters.forEach((filter) =>
      filter.addEventListener("click", () => {
        filters.forEach((f) => f.setAttribute("aria-pressed", String(f === filter)));
        const phase = filter.dataset.phase;
        let first = null;
        rules.forEach((b) => {
          const visible = phase === "all" || b.dataset.phase === phase;
          b.closest("li").hidden = !visible;
          if (visible && !first) first = b;
        });
        const current = rules.find((b) => b.getAttribute("aria-pressed") === "true");
        if (first && current.closest("li").hidden) show(first);
      }),
    );
  };

  /* ---------- Copy ---------- */
  const setupCopy = (button) => {
    button.addEventListener("click", async () => {
      const code = $("code", button.parentElement);
      const text = $$(".hn-ln", code)
        .map((l) => l.textContent.replace(/^\$ /, ""))
        .join("\n");
      try {
        await navigator.clipboard.writeText(text || code.textContent);
      } catch (e) {
        return;
      }
      button.textContent = "Copied";
      toast("Copied to clipboard");
      setTimeout(() => {
        button.textContent = "Copy";
      }, 1400);
    });
  };

  /* ---------- Diagram pulses ---------- */
  /* A dot travels along each wide-diagram arrow. Skipped for reduced motion. */
  const addPulses = (figure) => {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const svg = $(".hn-dg--wide", figure);
    if (!svg) return;
    const ns = "http://www.w3.org/2000/svg";
    $$(".hn-dg-arrow", svg).forEach((path) => {
      const i = Number.parseInt(path.style.getPropertyValue("--i"), 10);
      const dur = path.classList.contains("hn-dg-lane-arrow") ? 1.6 : 3.2;
      const begin = `${((i * 0.37) % dur).toFixed(2)}s`;
      const dot = document.createElementNS(ns, "circle");
      dot.setAttribute("class", "hn-dg-pulse");
      dot.setAttribute("data-i", String(i));
      dot.setAttribute("r", "3");
      const motion = document.createElementNS(ns, "animateMotion");
      motion.setAttribute("path", path.getAttribute("d"));
      motion.setAttribute("dur", `${dur}s`);
      motion.setAttribute("begin", begin);
      motion.setAttribute("repeatCount", "indefinite");
      motion.setAttribute("calcMode", "spline");
      motion.setAttribute("keyTimes", "0;1");
      motion.setAttribute("keySplines", "0.4 0 0.2 1");
      const fade = document.createElementNS(ns, "animate");
      fade.setAttribute("attributeName", "r");
      fade.setAttribute("values", "0;3;3;0");
      fade.setAttribute("keyTimes", "0;0.12;0.88;1");
      fade.setAttribute("dur", `${dur}s`);
      fade.setAttribute("begin", begin);
      fade.setAttribute("repeatCount", "indefinite");
      dot.append(motion, fade);
      svg.append(dot);
    });
  };

  /* ---------- Diagram tips ---------- */
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
    document.addEventListener("keydown", (e) => e.key === "Escape" && hide());
    addEventListener("scroll", hide, { passive: true });
  };

  /* ---------- Contents index ---------- */
  const setupIndex = (root) => {
    $$(".hn-index__card", root).forEach((card) => {
      const toggle = $(".hn-index__toggle", card);
      const pages = $$(".hn-index__pages a", card).length;
      $(".hn-index__count", card).textContent = `${pages} page${pages === 1 ? "" : "s"}`;
      /* Hover previews a card; a click pins it open, and a second click closes it. */
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
    if (!targets.length || !("IntersectionObserver" in window)) return;
    const visible = new Map(targets.map((t) => [t, true]));
    const apply = () => targets.forEach((t) => t.classList.toggle("is-idle", document.hidden || !visible.get(t)));
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => visible.set(e.target, e.isIntersecting));
      apply();
    });
    targets.forEach((t) => io.observe(t));
    document.addEventListener("visibilitychange", apply);
  };

  const init = () => {
    setupIdle($$(".hn-hero, .hn-ticker"));
    $$("[data-hn-index]").forEach(setupIndex);
    $$("[data-hn-panel]").forEach(setupPanel);
    $$(".hn-hero").forEach(setupGlow);
    $$("[data-hn-tabs]").forEach(setupTabs);
    $$("[data-hn-explorer]").forEach(setupExplorer);
    $$("[data-hn-copy]").forEach(setupCopy);
    $$("[data-hn-diagram]").forEach(setupDiagram);
  };

  init();
})();
