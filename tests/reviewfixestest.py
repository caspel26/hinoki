"""Regressions for the independent review of request handling."""

from typing import Any

import pytest

from hinoki import (
    FormParser,
    Hinoki,
    JSONRenderer,
    MultiPartParser,
    Renderer,
    Request,
    core,
    get,
    post,
)
from hinoki.parsers import boundary_of
from tests.harness import Client, Pool


class SpacedCSV(Renderer):
    media_type = "text/csv; charset=utf-8"

    def render(self, value: object, request: Request) -> bytes:
        return b"a"


@get("/cookies")
def cookies(request: Request) -> dict[str, Any]:
    return {"cookies": request.COOKIES}


@post("/echo")
def echo(request: Request) -> dict[str, Any]:
    return {"data": request.data}


@post("/probe")
def probe(request: Request) -> dict[str, Any]:
    return {"post": dict(request.POST)}


@post("/form", parsers=[FormParser(max_fields=3)])
def form(request: Request) -> dict[str, Any]:
    return {"n": len(request.POST)}


@get("/typed", renderers=[JSONRenderer(), SpacedCSV()])
def typed(request: Request) -> dict[str, str]:
    return {}


@post("/upload", parsers=[MultiPartParser()])
def upload(request: Request) -> dict[str, Any]:
    return {"filenames": [f.filename for f in request.FILES.getlist("f")]}


async def serve(pool: Pool) -> Client:
    app = Hinoki()
    app.include(cookies, echo, probe, form, typed, upload)
    return await pool.start(app)


async def test_headers_given_as_lists_are_accepted(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request(
        "GET",
        "/cookies",
        raw_headers=[[b"cookie", b"a=1"]],  # type: ignore[list-item]
    )
    assert reply.body == {"cookies": {"a": "1"}}


def test_header_pairs_must_have_two_items() -> None:
    assert core.header_dict([[b"A", b"1"], (b"b", b"2")]) == {"a": "1", "b": "2"}
    with pytest.raises(ValueError, match="pair"):
        core.header_dict([[b"only"]])


async def test_repeated_cookie_headers_are_rejoined_with_semicolons(
    pool: Pool,
) -> None:
    client = await serve(pool)
    reply = await client.request(
        "GET", "/cookies", raw_headers=[(b"cookie", b"a=1"), (b"cookie", b"s=abc")]
    )
    assert reply.body == {"cookies": {"a": "1", "s": "abc"}}


async def test_overflowing_numbers_are_a_400_not_an_echoed_500(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request(
        "POST", "/echo", b'{"x": 1e400}', content_type="application/json"
    )
    assert (reply.status, reply.body["error"]) == (400, "invalid_json")


async def test_a_media_type_with_parameters_still_negotiates(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request("GET", "/typed", headers={"accept": "text/csv"})
    assert reply.status == 200
    assert reply.headers["content-type"] == "text/csv; charset=utf-8"


def test_int_subclasses_cannot_inject_json() -> None:
    class Wicked(int):
        def __str__(self) -> str:
            return '1,"injected":true'

    assert core.encode_json({"a": Wicked(1)}) == b'{"a":1}'
    assert core.encode_json({Wicked(2): 1}) == b'{"2":1}'


async def test_two_content_types_are_refused(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request(
        "POST",
        "/echo",
        b"{}",
        raw_headers=[
            (b"content-type", b"application/json; x=1"),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ],
    )
    assert (reply.status, reply.body["error"]) == (400, "invalid_content_type")


async def test_post_is_empty_for_json_even_when_the_body_is_bad(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request(
        "POST", "/probe", b"{not json", content_type="application/json"
    )
    assert (reply.status, reply.body) == (200, {"post": {}})


async def test_form_fields_are_limited(pool: Pool) -> None:
    client = await serve(pool)
    ok = await client.request(
        "POST",
        "/form",
        b"a=1&b=2&c=3",
        content_type="application/x-www-form-urlencoded",
    )
    assert ok.body == {"n": 3}
    many = await client.request(
        "POST",
        "/form",
        b"a=1&b=2&c=3&d=4",
        content_type="application/x-www-form-urlencoded",
    )
    assert (many.status, many.body["error"]) == (400, "invalid_form")
    with pytest.raises(ValueError, match="positive"):
        FormParser(max_fields=0)


async def test_multipart_headers_with_control_characters_are_refused(
    pool: Pool,
) -> None:
    client = await serve(pool)
    body = (
        b'--B\r\nContent-Disposition: form-data; name="f"; filename="x\nSet-Cookie: y"'
        b"\r\n\r\ndata\r\n--B--\r\n"
    )
    reply = await client.request(
        "POST", "/upload", body, content_type="multipart/form-data; boundary=B"
    )
    assert (reply.status, reply.body["error"]) == (400, "invalid_multipart")
    fine = (
        b'--B\r\nContent-Disposition: Form-Data; name="f"; filename="x.txt"'
        b"\r\n\r\ndata\r\n--B--\r\n"
    )
    ok = await client.request(
        "POST", "/upload", fine, content_type="multipart/form-data; boundary=B"
    )
    assert ok.body == {"filenames": ["x.txt"]}


@pytest.mark.parametrize(
    ("header", "boundary"),
    [
        ("multipart/form-data; boundary=abc", "abc"),
        ('multipart/form-data; boundary="a;b"', "a;b"),
        ("multipart/form-data; charset=x; BOUNDARY=zz ; foo=1", "zz"),
        ("multipart/form-data", ""),
    ],
)
def test_the_boundary_is_read_with_quotes_in_mind(header: str, boundary: str) -> None:
    assert boundary_of(header) == boundary
