"""Explicit replies, cookies and the HTML renderer."""

import pytest

from hinoki import HinokiAPI, HTMLRenderer, Reply, Request, get, post, set_cookie
from tests.harness import Client, Pool


@get("/go")
def go(request: Request) -> Reply:
    return Reply(
        303,
        headers=[("Location", "/there"), set_cookie("sid", "abc", max_age=60)],
    )


@get("/page", renderers=[HTMLRenderer()])
def page(request: Request) -> str:
    return "<h1>hi</h1>"


@get("/bad-page", renderers=[HTMLRenderer()])
def bad_page(request: Request) -> dict[str, str]:
    return {"not": "a string"}


@get("/text")
def text(request: Request) -> Reply:
    return Reply(201, "ünï", content_type="text/plain; charset=utf-8")


@post("/boom")
def boom(request: Request) -> Reply:
    return Reply(200, headers={"x-a": "line\r\nSet-Cookie: x=1"})


async def serve(pool: Pool) -> Client:
    app = HinokiAPI()
    app.include(go, page, bad_page, text, boom)
    return await pool.start(app)


async def test_a_reply_sets_status_headers_and_cookies(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request("GET", "/go")
    assert reply.status == 303
    assert reply.headers["location"] == "/there"
    assert (
        reply.headers["set-cookie"]
        == "sid=abc; Path=/; SameSite=Lax; Max-Age=60; HttpOnly; Secure"
    )
    assert "x-request-id" in reply.headers


async def test_a_reply_body_and_content_type_bypass_the_renderers(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request("GET", "/text")
    assert (reply.status, reply.body) == (201, "ünï")
    assert reply.headers["content-type"] == "text/plain; charset=utf-8"


async def test_html_is_rendered_and_negotiated(pool: Pool) -> None:
    client = await serve(pool)
    html = await client.request("GET", "/page", headers={"accept": "text/html"})
    assert html.body == "<h1>hi</h1>"
    assert html.headers["content-type"] == "text/html; charset=utf-8"
    refused = await client.request(
        "GET", "/page", headers={"accept": "application/json"}
    )
    assert refused.status == 406
    wrong = await client.request("GET", "/bad-page")
    assert wrong.status == 500


async def test_a_bad_header_fails_in_the_handler_not_on_the_wire(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request("POST", "/boom")
    assert reply.status == 500
    assert "set-cookie" not in reply.headers


@pytest.mark.parametrize(
    "arguments",
    [
        {"status": 99},
        {"status": 600},
        {"status": 204, "body": "x"},
        {"headers": {"bad name": "x"}},
        {"headers": {"content-length": "3"}},
        {"headers": {"x-request-id": "forged"}},
        {"headers": {"x-a": "a\nb"}},
        {"headers": {"x-a": "☃"}},
    ],
)
def test_invalid_replies_are_refused_when_built(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        Reply(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "a b", "value": "x"},
        {"name": "a", "value": "x;y"},
        {"name": "a", "value": 'x"y'},
        {"name": "a", "value": "x", "same_site": "Wrong"},
        {"name": "a", "value": "x", "same_site": "None", "secure": False},
        {"name": "a", "value": "x", "path": "x"},
        {"name": "a", "value": "x", "path": "/;evil"},
    ],
)
def test_unsafe_cookies_are_refused(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        set_cookie(**arguments)  # type: ignore[arg-type]
