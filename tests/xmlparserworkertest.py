"""``XMLParser`` over the real Rust server, on both handler lanes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from hinoki import XML, Hinoki, Request, Schema, XMLParser, http
from hinoki.parsers import MAX_XML_DEPTH
from hinoki.runtime.kiso import KisoWorker
from tests.kisoservertest import Received, fetch, record_lanes


class Entry(Schema):
    title: str
    count: int = 0


@http.post("/xmlp/native", status=201)
async def xmlp_native(data: XML[Entry]) -> dict[str, object]:
    return {"title": data.title, "count": data.count}


@http.post("/xmlp/python", status=201)
async def xmlp_python(request: Request, data: XML[Entry]) -> dict[str, object]:
    return {"title": data.title, "count": data.count}


@http.post("/xmlp/tree", parsers=[XMLParser(max_elements=20_000)])
async def xmlp_tree(request: Request) -> dict[str, object]:
    data = request.data
    assert isinstance(data, dict)
    keys = list(data)
    return {"n": len(keys), "first": keys[:3], "last": keys[-1], "a7": data["a07"]}


@http.post(
    "/xmlp/deep",
    parsers=[XMLParser(max_depth=MAX_XML_DEPTH, max_elements=50_000)],
)
async def xmlp_deep(request: Request) -> dict[str, object]:
    return {"parsed": request.data is not None}


@asynccontextmanager
async def serving() -> AsyncIterator[tuple[int, list[bool]]]:
    app = Hinoki()
    app.include(xmlp_native, xmlp_python, xmlp_tree, xmlp_deep)
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        yield port, record_lanes(worker)
    finally:
        await worker.stop()


async def post(port: int, path: str, body: bytes) -> Received:
    extra = f"content-type: application/xml\r\ncontent-length: {len(body)}\r\n"
    return await fetch(port, "POST", path, extra, body=body)


def error_of(received: Received) -> tuple[int, str]:
    return received.status, json.loads(received.body)["error"]


async def both_lanes(body: bytes) -> list[Received]:
    async with serving() as (port, lanes):
        got = [await post(port, f"/xmlp/{lane}", body) for lane in ("native", "python")]
    assert lanes == [True, False]
    return got


@pytest.mark.parametrize(
    "text",
    [
        b"\x00",
        b"\x01",
        b"a\x08b",
        b"\x0b",
        b"\x1f",
        b"<![CDATA[\x01]]>",
        b"&#1;",
        b"&#xFFFE;",
        b"\xef\xbf\xbf",
    ],
)
async def test_characters_xml_forbids_are_invalid_xml(text: bytes) -> None:
    body = b"<i><title>" + text + b"</title></i>"
    for got in await both_lanes(body):
        assert error_of(got) == (400, "invalid_xml")


async def test_tab_carriage_return_and_line_feed_stay_allowed() -> None:
    body = b"<i><title>\ta\r\nb&#9;</title><count>2</count></i>"
    for got in await both_lanes(body):
        assert got.status == 201
        assert json.loads(got.body) == {"title": "\ta\nb\t", "count": 2}


@pytest.mark.parametrize(
    "declaration",
    [
        b'<?xml version="1.0" encoding="UTF-16"?>',
        b"<?xml version='1.0' encoding='ISO-8859-1'?>",
    ],
)
async def test_a_declared_encoding_other_than_utf8_is_invalid_xml(
    declaration: bytes,
) -> None:
    for got in await both_lanes(declaration + b"<i><title>a</title></i>"):
        assert error_of(got) == (400, "invalid_xml")


async def test_a_utf8_declaration_is_accepted() -> None:
    body = b'<?xml version="1.0" encoding="utf-8"?><i><title>a</title></i>'
    for got in await both_lanes(body):
        assert (got.status, json.loads(got.body)["title"]) == (201, "a")


async def test_many_distinct_siblings_keep_first_seen_order() -> None:
    names = [f"a{i:02d}" if i < 100 else f"a{i:05d}" for i in range(9_999)]
    body = ("<r>" + "".join(f"<{n}/>" for n in names) + "<a07>x</a07></r>").encode()
    async with serving() as (port, _):
        got = await post(port, "/xmlp/tree", body)
    assert got.status == 200
    assert json.loads(got.body) == {
        "n": 9_999,
        "first": ["a00", "a01", "a02"],
        "last": "a09998",
        "a7": [None, "x"],
    }


async def test_nesting_up_to_the_largest_allowed_depth_is_parsed() -> None:
    depth = MAX_XML_DEPTH
    body = ("<a>" * depth + "</a>" * depth).encode()
    async with serving() as (port, _):
        got = await post(port, "/xmlp/deep", body)
    assert (got.status, json.loads(got.body)) == (200, {"parsed": True})


async def test_far_deeper_nesting_is_invalid_xml_and_the_worker_survives() -> None:
    depth = 30_000
    body = ("<a>" * depth + "</a>" * depth).encode()
    async with serving() as (port, _):
        deep = await post(port, "/xmlp/deep", body)
        after = await post(port, "/xmlp/native", b"<i><title>a</title></i>")
    assert error_of(deep) == (400, "invalid_xml")
    assert after.status == 201


def test_max_depth_is_capped() -> None:
    assert XMLParser(max_depth=MAX_XML_DEPTH).max_depth == MAX_XML_DEPTH
    with pytest.raises(ValueError, match=f"at most {MAX_XML_DEPTH}"):
        XMLParser(max_depth=MAX_XML_DEPTH + 1)
