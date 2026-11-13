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
              .filter(el => !el.labels || el.labels.length === 0)
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
            check(
                not overflow(page),
                f"{label} {size[0]}x{size[1]} {path}: no horizontal page scroll",
            )
            check(unlabeled(page) == [], f"{label} {path}: every input has a label")
        page.goto(f"{base}/admin/posts")
        if shots and label in {"desktop", "phone"}:
            page.screenshot(path=str(shots / f"admin-posts-{label}.png"))
        page.close()


def flow(browser: Browser, base: str, shots: Path | None, name: str) -> None:
    print(f"  [{name}] admin round trip")
    ctx = context(browser, base, VIEWPORTS["desktop"])
    page = ctx.new_page()
    page.goto(f"{base}/admin")
    expect(page.locator("h1")).to_have_text("Dashboard")
    if shots:
        page.screenshot(path=str(shots / "admin-dashboard.png"))
    page.get_by_role("navigation", name="Models").get_by_role(
        "link", name="Post"
    ).click()
    expect(page.locator("h1")).to_have_text("Post")
    page.get_by_label("Search").fill("native")
    page.get_by_role("button", name="Search").click()
    check(page.locator("tbody tr").count() == 1, "search narrows the list to one row")
    page.get_by_label("Search").fill("")
    page.get_by_role("button", name="Search").click()
    check("page 1 of" in page.locator(".pager").inner_text(), "the list is paginated")
    page.get_by_role("link", name="Next").click()
    check("page 2 of" in page.locator(".pager").inner_text(), "next page works")

    page.goto(f"{base}/admin/posts/add")
    page.get_by_label("Title").fill("x" * 200)
    page.get_by_label("Slug").fill("s")
    page.get_by_label("Author id").fill("not-a-uuid")
    page.get_by_label("Body").fill("<script>window.__xss = 1</script>")
    page.get_by_role("button", name="Save").click()
    expect(page.locator("[role=alert]").first).to_be_visible()
    check(
        page.locator("[aria-invalid=true]").count() == 1,
        "the invalid field is marked (maxlength already truncated the title)",
    )
    check(page.evaluate("window.__xss") is None, "submitted markup is not executed")
    check(
        page.get_by_label("Body").input_value().startswith("<script>"),
        "input is kept after an error",
    )
    if shots:
        page.screenshot(path=str(shots / "admin-form-error.png"))

    stamp = str(int(time.time()))
    page.goto(f"{base}/admin/users")
    first_user = page.locator("tbody a").first.get_attribute("href") or ""
    user_id = first_user.rsplit("/", 1)[-1]
    page.goto(f"{base}/admin/posts/add")
    page.get_by_label("Title").fill(f"Browser post {stamp}")
    page.get_by_label("Slug").fill(f"browser-{stamp}")
    page.get_by_label("Author id").fill(user_id)
    page.get_by_label("Body").fill("created by the browser check")
    page.get_by_role("button", name="Save").click()
    expect(page.locator(".notice-ok")).to_have_text("Created.")

    page.get_by_label("Search").fill(f"Browser post {stamp}")
    page.get_by_role("button", name="Search").click()
    page.locator("tbody a").first.click()
    page.get_by_label("Title").fill(f"Browser post {stamp} edited")
    page.get_by_role("button", name="Save").click()
    expect(page.locator(".notice-ok")).to_have_text("Saved.")

    page.get_by_label("Search").fill(stamp)
    page.get_by_role("button", name="Search").click()
    check("edited" in page.locator("tbody").inner_text(), "the edit is listed")
    row_link = page.locator("tbody a").first.get_attribute("href") or ""
    page.goto(f"{base}{row_link}/delete")
    expect(page.get_by_text("cannot be undone")).to_be_visible()
    if shots:
        page.screenshot(path=str(shots / "admin-delete.png"))
    page.get_by_role("button", name="Delete").click()
    expect(page.locator(".notice-ok")).to_have_text("Deleted.")

    page.goto(f"{base}/admin/posts/add")
    page.locator("input[name=csrf]").evaluate("el => el.value = '0'.repeat(32)")
    page.get_by_label("Title").fill("forged")
    page.get_by_role("button", name="Save").click()
    check(
        "expired or is invalid" in page.locator("[role=alert]").inner_text(),
        "a forged CSRF token is refused",
    )
    page.goto(f"{base}/admin/users/add")
    check(
        page.locator("h1").inner_text() == "404",
        "users cannot be added (can() denies it)",
    )
    ctx.close()


def keyboard(browser: Browser, base: str, shots: Path | None, name: str) -> None:
    del shots  # no screenshots for this step
    print(f"  [{name}] keyboard")
    page = context(browser, base, VIEWPORTS["desktop"]).new_page()
    page.goto(f"{base}/admin/posts")
    page.keyboard.press("Tab")
    check(
        page.evaluate("document.activeElement.className") == "skip",
        "first Tab reaches the skip link",
    )
    check(
        page.evaluate("getComputedStyle(document.activeElement).left") != "-999px",
        "the skip link appears on focus",
    )
    page.keyboard.press("Enter")
    check(
        page.evaluate("document.activeElement.id") == "main",
        "the skip link moves focus to the content",
    )
    reached = False
    for _ in range(12):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.name") == "q":
            reached = True
            break
    check(reached, "the search field is reachable by Tab")
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    check(outline not in {"none", ""}, "the focused control has a visible outline")
    page.keyboard.type("Typed")
    page.keyboard.press("Enter")
    page.wait_for_url("**/admin/posts?q=Typed*")
    check("q=Typed" in page.url, "Enter submits the search form")


def dark(browser: Browser, base: str, shots: Path | None, name: str) -> None:
    print(f"  [{name}] dark scheme")
    page = context(browser, base, VIEWPORTS["desktop"], "dark").new_page()
    page.goto(f"{base}/admin/posts")
    background = page.evaluate("getComputedStyle(document.body).backgroundColor")
    check(background != "rgb(251, 251, 251)", "dark mode changes the background")
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
            for step in (docs, layout, flow, keyboard, dark):
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
