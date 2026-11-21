"""Response compression over real loopback sockets with the Rust server."""

import argparse
import asyncio
import json
import re
import zlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from hinoki import Hinoki, core, http
from hinoki.http import Event, EventStream, File, Response, Stream
from hinoki.runtime.kiso import KisoWorker
from hinoki.serve import choose_server
from hinoki.settings import AppSettings, CompressionSettings, ServerSettings

state: dict[str, object] = {}
ROWS = [{"id": number, "name": f"item {number}"} for number in range(400)]


@http.get("/rows")
async def rows_route() -> Response:
    return Response(
        200, headers={"content-type": "application/json"}, body=json.dumps(ROWS)
    )


@http.get("/opaque")
async def opaque_route() -> Response:
    return Response(
        200,
        headers={"content-type": "application/json", "cache-control": "no-transform"},
        body=json.dumps(ROWS),
    )


@http.get("/gated")
async def gated_route() -> Stream:
    gate = state["gate"]
    assert isinstance(gate, asyncio.Event)

    async def source() -> AsyncIterator[str]:
        yield "first;"
        await gate.wait()
        yield "last;"

    return Stream(source(), content_type="text/plain")


@http.get("/events")
async def events_route() -> EventStream:
    async def source() -> AsyncIterator[Event]:
        yield Event("hello")

    return EventStream(source())


@http.get("/file")
async def file_route() -> File:
    return File(str(state["file"]))


def app(**compression: object) -> Hinoki:
    settings = AppSettings(compression=CompressionSettings(**compression))  # type: ignore[arg-type]
    built = Hinoki(settings=settings)
    built.include(rows_route, opaque_route, gated_route, events_route, file_route)
    return built


@pytest.fixture(autouse=True)
def reset(tmp_path: Path) -> None:
    state.clear()
    state["gate"] = asyncio.Event()
    payload = tmp_path / "notes.txt"
    payload.write_text("a line of notes\n" * 2000)
    state["file"] = payload


async def serve(**compression: object) -> tuple[KisoWorker, int]:
    worker = KisoWorker(
        app(**compression),
        "127.0.0.1",
        0,
        compression=CompressionSettings(**compression).native_options(),  # type: ignore[arg-type]
    )
    return worker, await worker.start()


async def get(
    port: int, target: str, encoding: str | None = "gzip", method: str = "GET"
) -> tuple[dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    extra = f"accept-encoding: {encoding}\r\n" if encoding else ""
    request = f"{method} {target} HTTP/1.1\r\nhost: t\r\nconnection: close\r\n"
    writer.write(f"{request}{extra}\r\n".encode())
    raw = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:])}
    if headers.get("transfer-encoding") == "chunked":
        body = dechunk(body)
    return headers, body


def dechunk(data: bytes) -> bytes:
    body = b""
    while data:
        size, _, data = data.partition(b"\r\n")
        length = int(size, 16)
        if not length:
            break
        body, data = body + data[:length], data[length + 2 :]
    return body


async def test_compression_is_off_unless_enabled() -> None:
    worker, port = await serve()
    try:
        headers, body = await get(port, "/rows")
        assert "content-encoding" not in headers
        assert json.loads(body) == ROWS
    finally:
        await worker.stop()


async def test_a_json_body_is_gzipped_and_varies_on_the_request() -> None:
    worker, port = await serve(enabled=True)
    try:
        headers, body = await get(port, "/rows", "gzip")
        assert headers["content-encoding"] == "gzip"
        assert headers["vary"].lower() == "accept-encoding"
        assert int(headers["content-length"]) == len(body) < len(json.dumps(ROWS))
        assert json.loads(zlib.decompress(body, 31)) == ROWS
        headers, body = await get(port, "/rows", "br")
        assert headers["content-encoding"] == "br"
        headers, body = await get(port, "/rows", None)
        assert "content-encoding" not in headers
        assert json.loads(body) == ROWS
    finally:
        await worker.stop()


async def test_no_transform_and_head_follow_the_rules() -> None:
    worker, port = await serve(enabled=True)
    try:
        headers, body = await get(port, "/opaque")
        assert "content-encoding" not in headers
        assert json.loads(body) == ROWS
        headers, body = await get(port, "/rows", method="HEAD")
        assert headers["content-encoding"] == "gzip"
        assert "content-length" not in headers
        assert body == b""
    finally:
        await worker.stop()


async def test_a_stream_is_flushed_when_its_producer_pauses() -> None:
    worker, port = await serve(enabled=True, min_size=0)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /gated HTTP/1.1\r\nhost: t\r\naccept-encoding: gzip\r\n"
            b"connection: close\r\n\r\n"
        )
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert b"content-encoding: gzip" in head.lower()
        decoder = zlib.decompressobj(31)
        seen = b""
        while not seen:  # the gzip header may arrive in a chunk of its own
            size = int((await asyncio.wait_for(reader.readuntil(b"\r\n"), 5)), 16)
            data = await asyncio.wait_for(reader.readexactly(size + 2), 5)
            seen = decoder.decompress(data[:-2])
        assert seen == b"first;"
        gate = state["gate"]
        assert isinstance(gate, asyncio.Event)
        gate.set()
        rest = await asyncio.wait_for(reader.read(), 5)
        assert decoder.decompress(dechunk(rest)) == b"last;"
        writer.close()
    finally:
        await worker.stop()


async def test_event_streams_are_compressed_only_when_asked() -> None:
    worker, port = await serve(enabled=True, min_size=0)
    try:
        headers, _ = await get(port, "/events")
        assert "content-encoding" not in headers
    finally:
        await worker.stop()
    worker, port = await serve(enabled=True, min_size=0, event_streams=True)
    try:
        headers, body = await get(port, "/events")
        assert headers["content-encoding"] == "gzip"
        assert b"data: hello" in zlib.decompress(body, 31)
    finally:
        await worker.stop()


async def test_a_file_is_compressed_and_can_be_left_alone() -> None:
    worker, port = await serve(enabled=True)
    try:
        headers, body = await get(port, "/file")
        assert headers["content-encoding"] == "gzip"
        assert zlib.decompress(body, 31) == b"a line of notes\n" * 2000
    finally:
        await worker.stop()
    worker, port = await serve(enabled=True, files=False)
    try:
        headers, _ = await get(port, "/file")
        assert "content-encoding" not in headers
    finally:
        await worker.stop()


def test_the_default_types_match_the_native_policy() -> None:
    source = Path(__file__).parents[1] / "crates" / "kiso" / "src" / "compress.rs"
    text = source.read_text()
    block = re.search(r"DEFAULT_TYPES: \[&str; \d+\] = \[(.*?)\];", text, re.S)
    assert block is not None
    found = tuple(re.findall(r'"([^"]+)"', block.group(1)))
    assert found == CompressionSettings().types


@pytest.mark.parametrize(
    "bad",
    [
        {"encodings": ()},
        {"encodings": ("br", "br")},
        {"encodings": ("deflate",)},
        {"min_size": -1},
        {"types": ("json",)},
        {"gzip_level": 0},
        {"brotli_quality": 12},
        {"max_concurrent": 0},
    ],
)
def test_invalid_compression_settings_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=next(iter(bad))):
        CompressionSettings(**bad).validate()  # type: ignore[arg-type]


def test_compression_is_native_options_only_when_enabled() -> None:
    assert CompressionSettings().native_options() is None
    options = CompressionSettings(enabled=True, gzip_level=1).native_options()
    assert options is not None and options["gzip_level"] == 1


def test_the_native_server_refuses_an_invalid_policy() -> None:
    with pytest.raises(ValueError, match="gzip level"):
        core.KisoServer.start(
            KisoWorker(Hinoki(), "127.0.0.1", 0).core,
            "127.0.0.1",
            0,
            compression={"gzip_level": 12},
        )


def test_granian_refuses_enabled_compression() -> None:
    args = argparse.Namespace(server="granian")
    assert choose_server(args, ServerSettings(), compression=True) == 2
    assert choose_server(args, ServerSettings(), compression=False) == "granian"
