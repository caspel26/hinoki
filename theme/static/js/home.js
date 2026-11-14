/* Homepage behaviour: scenario playback, workbench tabs, rule explorer,
   copy buttons and diagram tips. Autoplay stops at the first user input and
   never runs with reduced motion. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const toast = (t) => window.HN && window.HN.toast && window.HN.toast(t);

  const whenVisible = (el, on, off) => {
    if (!("IntersectionObserver" in window)) return;
    new IntersectionObserver((entries) => entries.forEach((e) => (e.isIntersecting ? on() : off())), { threshold: 0.35 }).observe(el);
  };

  /* ---------- Hero panel: scenarios ---------- */
  const setupPanel = (panel) => {
    const buttons = $$("[data-state]", panel);
        const lines = $$(".hn-ln[data-line]", panel);
    let stopped = reduced;
    let timer = 0;
    let index = 0;

    const files = $$("[data-hn-file]", panel);
    const problems = $("[data-hn-problems]", panel);
    const count = $("[data-hn-count]", panel);
    const show = (button) => {
      buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      $$("[data-state-view]", panel).forEach((v) => {
        v.hidden = v.dataset.stateView !== button.dataset.state;
      });
      files.forEach((f) => {
        f.textContent = button.dataset.file;
      });
      if (problems) {
        problems.textContent = button.dataset.problems;
        problems.className = `ide__prob is-${button.dataset.tone}`;
      }
      if (count) count.textContent = button.dataset.count;
    };

    buttons.forEach((button, i) =>
      button.addEventListener("click", () => {
        stopped = true;
        clearInterval(timer);
        index = i;
        show(button);
      }),
    );

    const link = (diag, on) => {
      const wanted = (diag.dataset.lines || "").split(" ");
      lines.forEach((l) => l.classList.toggle("is-linked", on && wanted.includes(l.dataset.line)));
    };
    $$(".hn-diag", panel).forEach((diag) => {
      diag.addEventListener("pointerenter", () => {
        stopped = true;
        link(diag, true);
      });
      diag.addEventListener("pointerleave", () => link(diag, false));
      diag.addEventListener("focus", () => link(diag, true));
      diag.addEventListener("blur", () => link(diag, false));
    });

    if (panel.hasAttribute("data-autoplay") && !reduced) {
      const tick = () => {
        if (stopped) return;
        index = (index + 1) % buttons.length;
        show(buttons[index]);
      };
      whenVisible(
        panel,
        () => {
          clearInterval(timer);
          timer = setInterval(tick, 5200);
        },
        () => clearInterval(timer),
      );
    }
  };

  /* ---------- Tabs (workbench and commands) ---------- */
  const setupTabs = (root) => {
    const tabs = $$('[role="tab"]', root);
    const bars = tabs.map((t) => $(".hn-walk__bar", t));
    let stopped = reduced || !root.hasAttribute("data-autoplay");
    let timer = 0;
    let current = 0;
    const DUR = 7000;

    const select = (tab, focus) => {
      current = tabs.indexOf(tab);
      tabs.forEach((other, i) => {
        const on = other === tab;
        other.setAttribute("aria-selected", String(on));
        other.tabIndex = on ? 0 : -1;
        const panel = $(`[data-panel="${other.dataset.tab}"]`, root);
        if (panel) panel.hidden = !on;
        if (bars[i]) {
          bars[i].classList.remove("is-run");
          bars[i].style.setProperty("--dur", `${DUR}ms`);
        }
      });
      if (focus) tab.focus();
    };

    const run = () => {
      clearTimeout(timer);
      const bar = bars[current];
      if (stopped || !bar) return;
      void bar.offsetWidth;
      bar.classList.add("is-run");
      timer = setTimeout(() => {
        select(tabs[(current + 1) % tabs.length]);
        run();
      }, DUR);
    };

    const stop = () => {
      stopped = true;
      clearTimeout(timer);
      bars.forEach((b) => b && b.classList.remove("is-run"));
    };

    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => {
        stop();
        select(tab);
      });
      tab.addEventListener("keydown", (event) => {
        const move = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 1, ArrowUp: -1 }[event.key];
        if (event.key === "Home") select(tabs[0], true);
        else if (event.key === "End") select(tabs[tabs.length - 1], true);
        else if (move) {
          event.preventDefault();
          stop();
          select(tabs[(index + move + tabs.length) % tabs.length], true);
        }
      });
    });

    if (!stopped) {
      whenVisible(
        root,
        () => run(),
        () => clearTimeout(timer),
      );
    }
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

  /* ---------- Diagram tips ---------- */
  const setupDiagram = (figure) => {
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
      const set = (open) => {
        card.classList.toggle("is-open", open);
        toggle.setAttribute("aria-expanded", String(open));
      };
      toggle.addEventListener("click", () => set(!card.classList.contains("is-open")));
      card.addEventListener("pointerenter", (e) => e.pointerType === "mouse" && set(true));
      card.addEventListener("pointerleave", (e) => e.pointerType === "mouse" && set(false));
      card.addEventListener("focusout", (e) => !card.contains(e.relatedTarget) && set(false));
      card.addEventListener("keydown", (e) => e.key === "Escape" && set(false));
    });
  };

  const init = () => {
    $$("[data-hn-index]").forEach(setupIndex);
    $$("[data-hn-panel]").forEach(setupPanel);
    $$("[data-hn-tabs]").forEach(setupTabs);
    $$("[data-hn-explorer]").forEach(setupExplorer);
    $$("[data-hn-copy]").forEach(setupCopy);
    $$("[data-hn-diagram]").forEach(setupDiagram);
  };

  init();
})();
