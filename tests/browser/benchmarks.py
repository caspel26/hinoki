"""Real-browser verification of the benchmark page (docs/benchmarks.md).

A development tool, not part of pytest: it needs Playwright, which is deliberately
not a project dependency.

    uv run --locked --group docs mkdocs build --strict -d /tmp/site
    uvx --from playwright python tests/browser/benchmarks.py \
        --site /tmp/site --out /tmp/shots

Checks run in Chromium, Firefox and WebKit when installed, in the light and dark
themes: no horizontal page scroll at 1440x900, 390x844, 360x740 and 320x740; the
worker-count tabs (8 workers, 1 worker) and request-type tabs work by click, by
touch and by keyboard (arrows, Home, End) and keep a single tab stop; Hinoki (Kiso)
is the first row of the Python frameworks table; no cell overlaps another in a row;
the wide tables are keyboard reachable. Screenshots (Chromium only) go to --out.
Exit status is 1 on any failure.
"""

import argparse
import functools
import http.server
import socketserver
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import BrowserType, Page, sync_playwright

VIEWPORTS = {
    "desktop": (1440, 900),
    "phone": (390, 844),
    "short": (360, 740),
    "narrow": (320, 740),
}
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"    {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        FAILURES.append(message)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def serve(site: Path) -> Iterator[str]:
    handler = functools.partial(QuietHandler, directory=str(site))
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()


VIEW = "#bench-w8-python"
WORKERS = ".bench-tabs--workers"


def selected(page: Page, scope: str) -> str:
    return page.locator(
        f'{scope} [role="tab"][aria-selected="true"]'
    ).first.inner_text()


def visible_panels(page: Page, scope: str) -> int:
    return page.locator(f"{scope} .bench-panel:visible").count()


def check_layout(page: Page, label: str) -> None:
    overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth"
    )
    check(not overflow, f"{label}: no horizontal page scroll")
    for region in page.locator(".bench-scroll").all():
        check(
            region.get_attribute("tabindex") == "0",
            f"{label}: a scrolling table is keyboard focusable",
        )
        break


def check_no_collisions(page: Page, label: str) -> None:
    """In every visible row the stack name, the number and the bar never overlap."""
    overlaps = page.evaluate(
        """() => {
          const hit = (a, b) => a.left < b.right - 1 && b.left < a.right - 1
            && a.top < b.bottom - 1 && b.top < a.bottom - 1;
          let bad = 0;
          const rows = '.bench-route tr, .bench-summary tr';
          document.querySelectorAll(rows).forEach((row) => {
            if (!row.offsetParent) return;
            const cells = Array.from(row.children)
              .map((c) => c.getBoundingClientRect());
            for (let i = 0; i < cells.length; i++)
              for (let j = i + 1; j < cells.length; j++)
                if (hit(cells[i], cells[j])) bad++;
          });
          return bad;
        }"""
    )
    check(overlaps == 0, f"{label}: no cell overlaps another in a table row")


def check_summary(page: Page, label: str) -> None:
    for group, conditions in (("w8", "8 workers, 100 connections"), ("w1", None)):
        if group == "w1":
            page.locator(f'{WORKERS} [role="tab"]').nth(1).click()
        view = page.locator(f"#bench-{group}-summary")
        summary = view.locator(".bench-summary")
        text = " ".join(summary.inner_text().split())
        name = f"{label} ({group})"
        check(
            "faster than" in text or "slower than" in text,
            f"{name}: the summary states a ratio from the data",
        )
        if conditions:
            check(conditions in text, f"{name}: the summary states its conditions")
        rows = summary.locator("tbody tr").count()
        columns = summary.locator("thead th").count() - 2
        check(
            summary.locator(".bench-cmp").count() == rows * columns,
            f"{name}: one comparison cell per row and competitor",
        )
        if group == "w1":
            check(
                summary.locator(".bench-cmp:not(.is-higher):not(.is-missing)").count()
                > 0,
                f"{name}: a cell where Kiso is not clearly ahead is shown",
            )
        words = view.locator(".bench-cmp:not(.is-missing) .bench-word")
        check(
            words.count() > 0 and all(w.strip() for w in words.all_inner_texts()),
            f"{name}: every comparison cell carries its word",
        )
        if page.viewport_size and page.viewport_size["width"] >= 1440:
            fits = view.locator(".bench-scroll").evaluate(
                "el => el.scrollWidth <= el.clientWidth"
            )
            check(fits, f"{name}: the summary fits without scrolling")
    page.locator(f'{WORKERS} [role="tab"]').nth(0).click()


def check_workers(page: Page, label: str) -> None:
    tabs = page.locator(f'{WORKERS} [role="tab"]')
    check(tabs.count() == 2, f"{label}: a tab per worker count")
    check(
        selected(page, WORKERS).startswith("8 workers"),
        f"{label}: 8 workers is shown first",
    )
    check(
        page.locator(".bench-workerpanel:visible").count() == 1,
        f"{label}: one worker count is shown",
    )
    tabs.nth(0).focus()
    page.keyboard.press("ArrowRight")
    check(
        selected(page, WORKERS).startswith("1 worker")
        and page.locator("#bench-w1-summary:visible").count() == 1
        and page.locator("#bench-w8-summary:visible").count() == 0,
        f"{label}: ArrowRight shows the 1-worker results",
    )
    check_no_collisions(page, f"{label} (1 worker)")
    page.keyboard.press("ArrowLeft")
    check(
        page.locator("#bench-w8-summary:visible").count() == 1,
        f"{label}: ArrowLeft comes back to 8 workers",
    )


def check_tabs(page: Page, label: str) -> None:
    tabs = page.locator(f'{VIEW} [role="tab"]')
    check(
        visible_panels(page, VIEW) == 1, f"{label}: exactly one request type is shown"
    )
    check(
        page.locator(f'{VIEW} [role="tab"][tabindex="0"]').count() == 1,
        f"{label}: one tab stop",
    )
    tabs.nth(3).click()
    check(
        selected(page, VIEW) == "Articles" and visible_panels(page, VIEW) == 1,
        f"{label}: click selects a tab",
    )
    page.keyboard.press("ArrowRight")
    check(selected(page, VIEW) == "Article", f"{label}: ArrowRight moves on")
    page.keyboard.press("End")
    check(
        selected(page, VIEW) == "Auth: articles", f"{label}: End goes to the last tab"
    )
    page.keyboard.press("ArrowRight")
    check(selected(page, VIEW) == "JSON 1 KB", f"{label}: ArrowRight wraps around")
    page.keyboard.press("ArrowLeft")
    check(selected(page, VIEW) == "Auth: articles", f"{label}: ArrowLeft wraps around")
    page.keyboard.press("Home")
    check(selected(page, VIEW) == "JSON 1 KB", f"{label}: Home goes to the first tab")
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement.getAttribute('role')")
    check(focused == "tabpanel", f"{label}: Tab moves from the tab into its panel")
    check_no_collisions(page, label)


def check_touch(page: Page, label: str) -> None:
    page.locator(f'{VIEW} [role="tab"]').nth(5).tap()
    check(
        selected(page, VIEW) == "Auth: me" and visible_panels(page, VIEW) == 1,
        f"{label}: a tap selects a tab",
    )


def check_views(page: Page, label: str) -> None:
    for view in ("python", "same-granian", "other-runtimes"):
        check(
            page.locator(f"#bench-w8-{view} .bench-route:visible").count() == 1,
            f"{label}: the {view} view shows its table",
        )
    kiso = page.locator(
        f'{VIEW} .bench-panel:visible .bench-route tr[data-stack="hinoki-kiso"]'
    )
    check(
        kiso.count() == 1, f"{label}: Hinoki (Kiso) sits in the Python frameworks table"
    )
    first = page.locator(f"{VIEW} .bench-panel:visible .bench-route tbody tr").first
    check(
        first.get_attribute("data-stack") == "hinoki-kiso",
        f"{label}: Hinoki (Kiso) is the first row, the reference",
    )


def run_engine(engine: BrowserType, base: str, out: Path | None, name: str) -> None:
    browser = engine.launch()
    for scheme in ("light", "dark"):
        for size, (width, height) in VIEWPORTS.items():
            label = f"{name} {scheme} {size} {width}x{height}"
            print(f"  {label}")
            touch = size != "desktop"
            context = browser.new_context(
                viewport={"width": width, "height": height},
                color_scheme=scheme,
                has_touch=touch,
            )  # type: ignore[arg-type]
            page = context.new_page()
            page.goto(f"{base}/benchmarks/")
            page.wait_for_selector(".bench-explorer.is-enhanced")
            check_layout(page, label)
            check_summary(page, label)
            check_views(page, label)
            check_tabs(page, label)
            check_workers(page, label)
            if touch:
                check_touch(page, label)
            if out and name == "chromium":
                out.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out / f"benchmarks-{scheme}-{width}.png"))
                if width in (1440, 390):
                    page.screenshot(
                        path=str(out / f"benchmarks-{scheme}-{width}-full.png"),
                        full_page=True,
                    )
            context.close()
    browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    with serve(args.site) as base, sync_playwright() as playwright:
        for name in ("chromium", "firefox", "webkit"):
            engine = getattr(playwright, name)
            try:
                run_engine(engine, base, args.out, name)
            except Exception as error:
                if "Executable doesn't exist" not in str(error):
                    raise
                print(f"  {name}: not installed, not verified")
    print(f"{len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
