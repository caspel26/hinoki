"""XMLRenderer on the native handler lane, over the real Rust server."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest

from hinoki import Hinoki, JSONRenderer, Request, XMLRenderer, http
from hinoki.runtime.kiso import KisoWorker
from tests.kisoservertest import fetch

XML = "application/xml; charset=utf-8"
BODY = (
    '<?xml version="1.0" encoding="UTF-8"?><r><id>3</id><ids>'
    f"<item>{UUID(int=1)}</item></ids><none/></r>"
)


@http.get("/lane/xml/{item_id}", renderers=[JSONRenderer(), XMLRenderer(root="r")])
async def lane_xml(item_id: int) -> dict[str, object]:
    return {"id": item_id, "ids": (UUID(int=1),), "none": None}


@http.get("/lane/request/{item_id}", renderers=[JSONRenderer(), XMLRenderer(root="r")])
async def lane_request(request: Request, item_id: int) -> dict[str, object]:
    return {"id": item_id, "ids": (UUID(int=1),), "none": None}


def recording(lanes: list[str], lane: str, call: Callable[..., Any]) -> Any:
    async def recorded(*args: object, **kwargs: object) -> object:
        lanes.append(lane)
        return await call(*args, **kwargs)

    return recorded


@asynccontextmanager
async def serving(lanes: list[str]) -> AsyncIterator[int]:
    """A started worker whose every request records the lane that served it."""
    app = Hinoki()
    app.include(lane_xml, lane_request)
    worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=False)
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle = recording(lanes, "python", pipeline.handle)  # type: ignore[method-assign]
    port = await worker.start()
    try:
        yield port
    finally:
        await worker.stop()


async def test_xml_is_rendered_on_the_native_lane() -> None:
    lanes: list[str] = []
    async with serving(lanes) as port:
        got = await fetch(port, "GET", "/lane/xml/3", "accept: application/xml\r\n")
    assert lanes == ["native"]
    assert got.status == 200
    assert got.header("content-type") == XML
    assert got.body.decode() == BODY


async def test_a_request_parameter_keeps_the_python_lane_with_the_same_xml() -> None:
    lanes: list[str] = []
    async with serving(lanes) as port:
        got = await fetch(port, "GET", "/lane/request/3", "accept: application/xml\r\n")
    assert lanes == ["python"]
    assert got.body.decode() == BODY


@pytest.mark.parametrize(
    ("accept", "media_type"),
    [("application/xml", XML), ("application/json", "application/json")],
)
async def test_head_on_a_multi_renderer_native_route(
    accept: str, media_type: str
) -> None:
    lanes: list[str] = []
    async with serving(lanes) as port:
        extra = f"accept: {accept}\r\n"
        full = await fetch(port, "GET", "/lane/xml/3", extra)
        head = await fetch(port, "HEAD", "/lane/xml/3", extra)
    assert lanes == ["native", "native"]
    assert head.status == full.status == 200
    assert head.body == b""
    assert head.header("content-type") == full.header("content-type") == media_type
    assert head.header("content-length") == str(len(full.body))
    assert head.header("vary") == full.header("vary")
