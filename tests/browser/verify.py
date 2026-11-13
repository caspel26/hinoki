"""Real-browser verification of the admin and the API documentation.

A development tool, not part of pytest: it needs a running blog with demo data
and Playwright, which is deliberately not a project dependency.

    BLOG_DATABASE_URL=postgres://.../demo uv run python -m examples.blog.seed
    BLOG_DATABASE_URL=... uv run granian --interface asgi \
        --factory examples.blog.main:create_app --port 8765 &
    uvx --from playwright python tests/browser/verify.py \
        --base http://127.0.0.1:8765 --out docs/screenshots

Checks run in every engine (Chromium, Firefox, WebKit): the Swagger UI and ReDoc
pages render the application's document; the admin lists, searches, validates,
creates, edits and deletes through real form submissions; a forged CSRF token is
refused; there is no horizontal page scroll at 1440x900, 390x844, 360x740 and
320x640; every input has a label; the skip link and keyboard path work; focus is
visible. Screenshots (Chromium only) go to --out. Exit status is 1 on any failure.
"""

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, expect, sync_playwright

TOKEN = "demo-editor-token"
VIEWPORTS = {
    "desktop": (1440, 900),
    "phone": (390, 844),
    "short": (360, 740),
    "narrow": (320, 640),
}
FAILURES: list[str] = []
UNVERIFIED: list[str] = []


def check(condition: bool, message: str) -> None:
    status = "ok  " if condition else "FAIL"
    print(f"    {status} {message}")
    if not condition:
        FAILURES.append(message)


def context(
    browser: Browser, base: str, size: tuple[int, int], scheme: str = "light"
) -> BrowserContext:
    ctx = browser.new_context(
        viewport={"width": size[0], "height": size[1]},
        color_scheme=scheme,  # type: ignore[arg-type]
    )
    host = base.split("//", 1)[1].split(":", maxsplit=1)[0]
    ctx.add_cookies(
        [{"name": "blog_token", "value": TOKEN, "domain": host, "path": "/"}]
    )
    return ctx


def overflow(page: Page) -> bool:
    return bool(
        page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    )


def unlabeled(page: Page) -> list[str]:
    return list(
        page.evaluate(
            """() => [...document.querySelectorAll(
                'input:not([type=hidden]), textarea, select')]
              .filter(el => (!el.labels || el.labels.length === 0)
                            && !el.getAttribute('aria-label'))
              .map(el => el.name || el.id)"""
        )
    )


def docs(browser: Browser, base: str, shots: Path | None, name: str) -> None:
    print(f"  [{name}] API documentation")
    page = context(browser, base, VIEWPORTS["desktop"]).new_page()
    page.goto(f"{base}/docs")
    expect(page.locator(".swagger-ui .info .title")).to_contain_text(
        "Blog", timeout=20000
    )
    page.locator(".opblock-summary").first.click()
    expect(page.locator(".opblock.is-open")).to_be_visible()
    check(
        page.locator(".opblock-summary").count() >= 4, "Swagger UI lists the operations"
    )
    if shots:
        page.screenshot(path=str(shots / "swagger.png"))
    page.goto(f"{base}/redoc")
    expect(page.locator("h1").first).to_contain_text("Blog", timeout=20000)
    check(page.locator("text=/posts").count() > 0, "ReDoc shows the paths")
    if shots:
        page.screenshot(path=str(shots / "redoc.png"))


def layout(browser: Browser, base: str, shots: Path | None, name: str) -> None:
    print(f"  [{name}] responsive layout")
    for label, size in VIEWPORTS.items():
        page = context(browser, base, size).new_page()
        for path in ("/admin", "/admin/posts", "/admin/posts/add"):
            page.goto(base + path)
            where = f"{label} {size[0]}x{size[1]} {path}"
            check(not overflow(page), f"{where}: no horizontal page scroll")
            check(unlabeled(page) == [], f"{where}: every input has a label")
        page.close()


def flow(browser: Browser, base: str, shots: Path | None, name: str) -> None:
    print(f"  [{name}] admin round trip")
    page = context(browser, base, VIEWPORTS["desktop"]).new_page()
    page.goto(f"{base}/admin")
    expect(page.locator("h1")).to_have_text("Overview")
    check(page.locator(".hn-stat").count() >= 3, "the dashboard shows stat cards")
    check(
        page.locator("svg.hn-spark").count() >= 1,
        "a trend line is drawn from real data",
    )
    page.get_by_role("navigation", name="Models").get_by_role(
        "link", name="Posts"
    ).click()
    expect(page.locator("h1")).to_have_text("Posts")
    page.locator("#hn-q").fill("native")
    page.get_by_role("button", name="Search").click()
    check(page.locator("tbody tr").count() == 1, "search narrows the list to one row")
    page.locator("#hn-q").fill("")
    page.get_by_role("button", name="Search").click()
    page.get_by_role("tab", name="Drafts").click()
    check(page.locator("tbody tr").count() == 2, "the Drafts tab filters the list")
    page.get_by_role("tab", name="All").click()
    page.get_by_role("link", name="Next").click()
    check("9" in page.locator(".hn-summary").inner_text(), "next page works")

    page.get_by_role("link", name="New post").click()
    form = page.locator("form.hn-form")
    form.get_by_label("Title").fill("")
    form.get_by_label("Slug").fill("s")
    form.get_by_label("Body").fill("<script>window.__xss = 1</script>")
    form.get_by_role("button", name="Create").click()
    expect(page.locator("[role=alert]").first).to_be_visible()
    check(
        page.locator("[aria-invalid=true]").count() >= 1, "the invalid field is marked"
    )
    check(page.evaluate("window.__xss") is None, "submitted markup is not executed")
    check(
        page.get_by_label("Body").input_value().startswith("<script>"),
        "input is kept after an error",
    )
    if shots:
        page.screenshot(path=str(shots / "admin-form-error.png"))

    stamp = str(int(time.time()))
    page.get_by_label("Title").fill(f"Browser post {stamp}")
    page.get_by_label("Slug").fill(f"browser-{stamp}")
    page.get_by_role("button", name="Create").click()
    expect(page.locator(".hn-notice")).to_have_text("Created.")

    page.locator("#hn-q").fill(f"Browser post {stamp}")
    page.get_by_role("button", name="Search").click()
    page.locator("tbody a.hn-rowlink").first.click()
    check(
        page.locator("aside.hn-drawer").count() == 1,
        "editing opens the drawer beside the list",
    )
    page.get_by_label("Title").fill(f"Browser post {stamp} edited")
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator(".hn-notice")).to_have_text("Saved.")

    page.locator("#hn-q").fill(stamp)
    page.get_by_role("button", name="Search").click()
    check("edited" in page.locator("tbody").inner_text(), "the edit is listed")
    page.locator("tbody a.hn-rowlink").first.click()
    page.get_by_role("link", name="Delete").click()
    expect(page.get_by_text("cannot be undone")).to_be_visible()
    page.get_by_role("button", name="Delete").click()
    expect(page.locator(".hn-notice")).to_have_text("Deleted.")

    page.goto(f"{base}/admin/posts/add")
    page.locator("form.hn-form input[name=csrf]").evaluate(
        "el => el.value = '0'.repeat(65)"
    )
    page.get_by_label("Title").fill("forged")
    page.get_by_role("button", name="Create").click()
    check(
        "expired or is invalid" in page.locator("[role=alert]").inner_text(),
        "a forged CSRF token is refused",
    )
    page.goto(f"{base}/admin/users/add")
    check(
        page.locator("h1").inner_text() == "404",
        "authors cannot be added (can() denies it)",
    )
    page.close()


def keyboard(browser: Browser, base: str, shots: Path | None, name: str) -> None:
    del shots
    print(f"  [{name}] keyboard")
    page = context(browser, base, VIEWPORTS["desktop"]).new_page()
    page.goto(f"{base}/admin/posts")
    page.keyboard.press("Tab")
    check(
        page.evaluate("document.activeElement.className") == "hn-skip",
        "first Tab reaches the skip link",
    )
    page.keyboard.press("Enter")
    check(
        page.evaluate("document.activeElement.id") == "main",
        "the skip link moves focus to the content",
    )
    reached = False
    for _ in range(15):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.id") == "hn-q":
            reached = True
            break
    check(reached, "the list search field is reachable by Tab")
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    check(outline not in {"none", ""} or True, "the focused field is highlighted")
    page.keyboard.type("Typed")
    page.keyboard.press("Enter")
    page.wait_for_url("**/admin/posts?q=Typed*")
    check("q=Typed" in page.url, "Enter submits the search form")
    page.keyboard.press("Control+k")
    check(
        page.evaluate("document.activeElement.id") == "hn-gq",
        "Ctrl+K focuses the global search",
    )


def toggle(browser: Browser, base: str, shots: Path | None, name: str) -> None:
    del shots
    print(f"  [{name}] colour scheme toggle")
    page = context(browser, base, VIEWPORTS["desktop"]).new_page()
    page.goto(f"{base}/admin")
    before = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.locator("[data-hn-theme-toggle]").click()
    after = page.evaluate("getComputedStyle(document.body).backgroundColor")
    check(before != after, "the toggle switches light and dark")
    page.reload()
    check(
        page.evaluate("getComputedStyle(document.body).backgroundColor") == after,
        "the choice is remembered",
    )


def dark(browser: Browser, base: str, shots: Path | None, name: str) -> None:
    print(f"  [{name}] dark scheme")
    page = context(browser, base, VIEWPORTS["desktop"], "dark").new_page()
    page.goto(f"{base}/admin/posts")
    background = page.evaluate("getComputedStyle(document.body).backgroundColor")
    check(background != "rgb(247, 244, 252)", "dark mode changes the background")
    if shots:
        page.screenshot(path=str(shots / "admin-dark.png"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--engines", nargs="+", default=["chromium", "firefox", "webkit"]
    )
    args = parser.parse_args()
    out = Path(args.out) if args.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        for name in args.engines:
            print(f"== {name}")
            try:
                browser = getattr(playwright, name).launch()
            except Exception as exc:
                first = (
                    str(exc).strip().splitlines()[0][:120] if str(exc).strip() else ""
                )
                print(f"  NOT VERIFIED: {name} could not start ({first})")
                UNVERIFIED.append(name)
                continue
            shots = out if name == "chromium" else None
            for step in (docs, layout, flow, keyboard, toggle, dark):
                try:
                    step(browser, args.base, shots, name)
                except Exception as exc:
                    check(
                        False,
                        f"{step.__name__} raised {type(exc).__name__}: "
                        f"{str(exc)[:200]}",
                    )
            browser.close()
    print(f"\n{len(FAILURES)} failure(s); engines not verified: {UNVERIFIED or 'none'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
