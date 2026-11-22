"""Swagger UI and ReDoc served from the package."""

import hashlib
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from hinoki import Hinoki, Request, http
from hinoki.docsui import STATIC, asset, redoc_html, swagger_html
from tests.harness import Client, Pool


@http.get("/ping")
def ping(request: Request) -> dict[str, str]:
    return {"pong": "yes"}


def app(**options: str | None) -> Hinoki:
    result = Hinoki(openapi_url="/openapi.json", **options)  # type: ignore[arg-type]
    result.include(ping)
    return result


async def raw(client: Client, path: str) -> tuple[int, dict[str, str], str]:
    reply = await client.request("GET", path)
    body = reply.body if isinstance(reply.body, str) else str(reply.body)
    return reply.status, reply.headers, body


async def test_swagger_ui_page_and_assets(pool: Pool) -> None:
    client = await pool.start(app(docs_url="/docs"))
    status, headers, html = await raw(client, "/docs")
    assert status == 200 and headers["content-type"] == "text/html; charset=utf-8"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "<script>" not in html
    assert f"/docs/assets/{asset('swagger-ui-bundle.js')}" in html
    status, headers, init = await raw(client, "/docs/assets/init.js")
    assert 'url: "/openapi.json"' in init
    assert headers["content-type"].startswith("text/javascript")
    status, headers, _ = await raw(client, f"/docs/assets/{asset('swagger-ui.css')}")
    assert status == 200 and headers["content-type"].startswith("text/css")
    assert headers["cache-control"] == "public, max-age=86400"
    status, _, _ = await raw(client, "/docs/assets/../../etc/passwd")
    assert status == 404
    assert (await raw(client, "/redoc"))[0] == 404  # only what was asked for


async def test_redoc_page_and_bundle(pool: Pool) -> None:
    client = await pool.start(app(redoc_url="/redoc"))
    status, _, html = await raw(client, "/redoc")
    assert status == 200 and 'type="module" src="/redoc/assets/init.js"' in html
    assert "<script>" not in html and "googleapis" not in html
    status, headers, init = await raw(client, "/redoc/assets/init.js")
    assert status == 200 and headers["content-type"].startswith("text/javascript")
    assert 'init("/openapi.json", {disableTelemetry: true}' in init
    name = asset("redoc.standalone.js")
    assert f'from "./{name}"' in init
    status, _, bundle = await raw(client, f"/redoc/assets/{name}")
    assert status == 200 and len(bundle) > 100_000
    assert (await raw(client, "/redoc/assets/redoc.standalone.js"))[0] == 404


async def test_both_viewers_can_live_side_by_side(pool: Pool) -> None:
    client = await pool.start(app(docs_url="/docs", redoc_url="/redoc"))
    assert (await raw(client, "/docs"))[0] == 200
    assert (await raw(client, "/redoc"))[0] == 200
    assert (await client.request("GET", "/ping")).body == {"pong": "yes"}
    assert (await client.request("GET", "/openapi.json")).status == 200


async def test_head_matches_openapi_viewers_and_assets(pool: Pool) -> None:
    client = await pool.start(app(docs_url="/docs", redoc_url="/redoc"))
    for path in (
        "/openapi.json",
        "/docs",
        f"/docs/assets/{asset('swagger-ui.css')}",
        f"/docs/assets/{asset('swagger-ui-bundle.js')}",
        "/docs/assets/init.js",
        "/redoc",
        f"/redoc/assets/{asset('redoc.standalone.js')}",
        "/redoc/assets/init.js",
    ):
        get = await client.request("GET", path)
        head = await client.request("HEAD", path)
        assert head.status == get.status == 200, path
        assert head.body is None, path
        assert head.headers["content-length"] == get.headers["content-length"], path
        assert {
            key: value for key, value in head.headers.items() if key != "x-request-id"
        } == {
            key: value for key, value in get.headers.items() if key != "x-request-id"
        }, path


def test_viewers_need_the_document_they_display() -> None:
    with pytest.raises(ValueError, match="need openapi_url"):
        Hinoki(docs_url="/docs")
    with pytest.raises(ValueError, match="need openapi_url"):
        Hinoki(redoc_url="/redoc")


def test_vendored_files_match_the_recorded_hashes() -> None:
    notice = (Path(STATIC).parent / "THIRD-PARTY.md").read_text()
    recorded = set(re.findall(r"`([0-9a-f]{64})`", notice))
    for name in ("swagger-ui-bundle.js", "swagger-ui.css", "redoc.standalone.js"):
        digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
        assert digest in recorded, f"{name} changed; update THIRD-PARTY.md"
    assert (STATIC / "LICENSE-swagger-ui").is_file() and (
        STATIC / "LICENSE-redoc"
    ).is_file()


@pytest.mark.parametrize("page", [swagger_html, redoc_html])
def test_a_documentation_url_cannot_break_out_of_the_page_attribute(
    page: Callable[[str], str],
) -> None:
    html = page('/d"><script>alert(1)</script>')
    assert "<script>alert" not in html
    assert "&quot;&gt;&lt;script&gt;" in html
