"""Real-browser verification of the benchmark page (docs/benchmarks.md).

A development tool, not part of pytest: it needs Playwright, which is deliberately
not a project dependency.

    uv run --locked --group docs mkdocs build --strict -d /tmp/site
    uvx --from playwright python tests/browser/benchmarks.py \
        --site /tmp/site --out /tmp/shots

Checks run in Chromium, Firefox and WebKit when installed, in the light and dark
themes: no horizontal page scroll at 1440x900, 390x844, 360x740 and 320x740; the
request-type tabs work by click, by touch and by keyboard (arrows, Home, End) and
keep a single tab stop; the legend hides and shows a stack in the overview and in
the open panel and never lets the last stack go; the wide tables are keyboard
reachable. Screenshots (Chromium only) go to --out. Exit status is 1 on any failure.
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


def selected(page: Page) -> str:
    return page.locator('[role="tab"][aria-selected="true"]').inner_text()


def visible_panels(page: Page) -> int:
    return page.locator('[role="tabpanel"]:visible').count()


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


def check_tabs(page: Page, label: str) -> None:
    tabs = page.locator('[role="tab"]')
    check(visible_panels(page) == 1, f"{label}: exactly one request type is shown")
    check(
        page.locator('[role="tab"][tabindex="0"]').count() == 1,
        f"{label}: one tab stop",
    )
    tabs.nth(3).click()
    check(
        selected(page) == "List" and visible_panels(page) == 1,
        f"{label}: click selects a tab",
    )
    tabs.nth(3).focus()
    page.keyboard.press("ArrowRight")
    check(selected(page) == "Detail", f"{label}: ArrowRight moves on")
    page.keyboard.press("End")
    check(selected(page) == "Create", f"{label}: End goes to the last tab")
    page.keyboard.press("ArrowRight")
    check(selected(page) == "Health", f"{label}: ArrowRight wraps around")
    page.keyboard.press("ArrowLeft")
    check(selected(page) == "Create", f"{label}: ArrowLeft wraps around")
    page.keyboard.press("Home")
    check(selected(page) == "Health", f"{label}: Home goes to the first tab")
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement.getAttribute('role')")
    check(focused == "tabpanel", f"{label}: Tab moves from the tab into its panel")


def check_touch(page: Page, label: str) -> None:
    page.locator('[role="tab"]').nth(5).tap()
    check(
        selected(page) == "Create" and visible_panels(page) == 1,
        f"{label}: a tap selects a tab",
    )


def check_legend(page: Page, label: str) -> None:
    chip = page.locator('.bench-chip[data-stack="hinoki-kiso"]')
    cells = '#bench-overview [data-stack="hinoki-kiso"]:visible'
    check(page.locator(cells).count() > 0, f"{label}: the Kiso column starts visible")
    chip.click()
    check(
        chip.get_attribute("aria-pressed") == "false",
        f"{label}: the chip reports it is off",
    )
    check(page.locator(cells).count() == 0, f"{label}: the Kiso column is hidden")
    check(
        page.locator(
            '.bench-panel:visible .bench-row[data-stack="hinoki-kiso"]:visible'
        ).count()
        == 0,
        f"{label}: the open chart follows the legend",
    )
    chip.click()
    check(page.locator(cells).count() > 0, f"{label}: the Kiso column comes back")
    chips = page.locator(".bench-chip")
    for index in range(chips.count() - 1):
        chips.nth(index).click()
    chips.nth(chips.count() - 1).click()
    check(
        page.locator('.bench-chip[aria-pressed="true"]').count() == 1,
        f"{label}: the last stack cannot be hidden",
    )
    for index in range(chips.count()):
        if chips.nth(index).get_attribute("aria-pressed") == "false":
            chips.nth(index).click()


def check_details_tabs(page: Page, label: str) -> None:
    second = page.locator(".tabbed-labels label").nth(1)
    second.click()
    check(
        page.locator(".tabbed-block:visible .bench-matrix").count() == 1,
        f"{label}: the second server view opens",
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
            check_tabs(page, label)
            check_legend(page, label)
            check_details_tabs(page, label)
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
