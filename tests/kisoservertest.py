"""The Rust HTTP/1.1 server over real loopback sockets."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import inspect
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

import pytest

from hinoki import (
    XML,
    Access,
    Caller,
    Form,
    FormParser,
    Hinoki,
    HTMLRenderer,
    JSONParser,
    JSONRenderer,
    MultiPartParser,
    Query,
    RateLimitContext,
    RuntimeConfig,
    Schema,
    UploadedFile,
    XMLRenderer,
    core,
    http,
)
from hinoki.api import API
from hinoki.context import request_id
from hinoki.errors import TooManyRequests
from hinoki.http.request import Request
from hinoki.http.response import Response
from hinoki.modules import ModuleContext
from hinoki.parsers import TextParser
from hinoki.renderers import is_native_renderer
from hinoki.runtime import kiso, responses
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.servercore import build_server_core
from hinoki.runtime.supervisor import Supervisor
from hinoki.settings import AppSettings, CorsSettings, FrameOptions, SecuritySettings
from tests.harness import Client
from tests.providers import TokenAuth

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Probe:
    """What the handlers share with the test that drives them."""

    gate: asyncio.Event = field(default_factory=asyncio.Event)
    entered: int = 0
    cancelled: int = 0
    shutdown_ran: bool = False
    sync_entered: threading.Event = field(default_factory=threading.Event)
    sync_gate: threading.Event = field(default_factory=threading.Event)
    native_input_calls: int = 0
    ran: list[str] = field(default_factory=list)
    late_coroutine: Coroutine[Any, Any, dict[str, str]] | None = None


PROBE = Probe()
DEFAULT_VIEW = "".join(("sum", "mary"))


@pytest.fixture(autouse=True)
def fresh_probe() -> None:
    global PROBE  # noqa: PLW0603 - handlers are module level and read it
    PROBE = Probe()


@http.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@http.get("/private", access=Access(auth=TokenAuth()))
async def private() -> dict[str, str]:
    return {"secret": "value"}


@http.get("/context-async")
async def context_async() -> dict[str, str]:
    return {"request_id": request_id.get() or "missing"}


@http.get("/context-sync")
def context_sync() -> dict[str, str]:
    return {"request_id": request_id.get() or "missing"}


async def sync_awaitable_result() -> dict[str, str]:
    await asyncio.sleep(0)
    return {"request_id": request_id.get() or "missing"}


@http.get("/context-sync-awaitable", validate_response=False)
def context_sync_awaitable() -> object:
    return sync_awaitable_result()


@http.get("/native/echo/{item_id}")
async def native_echo(item_id: str, term: str) -> dict[str, str]:
    PROBE.native_input_calls += 1
    return {"item_id": item_id, "term": term}


@http.delete("/native/remove/{item_id}", status=204)
async def native_remove(item_id: int) -> None:
    return None


@http.delete("/native/remove-sync/{item_id}")
def native_remove_sync(item_id: str, force: bool = False) -> dict[str, object]:
    return {"item_id": item_id, "force": force}


class Item(Schema):
    title: str
    count: int = 1


@http.post("/native/items/{item_id}", status=201)
async def native_item(item_id: int, data: Item, dry: bool = False) -> dict[str, object]:
    return {"id": item_id, "title": data.title, "count": data.count, "dry": dry}


@http.patch("/native/items-sync/{item_id}")
def native_item_sync(data: Item, item_id: int) -> dict[str, object]:
    return {"id": item_id, "title": data.title}


WRITERS = Access(auth=TokenAuth(), permissions=["posts:write"])


@http.get("/native/me/{item_id}", access=WRITERS)
async def native_me(
    item_id: int, who: Caller[str], limit: int = 5
) -> dict[str, object]:
    PROBE.ran.append("native_me")
    return {"id": item_id, "who": who.id, "limit": limit}


@http.post("/native/guarded", status=201, access=Access(auth=TokenAuth()))
def native_guarded(data: Item, who: Caller[str]) -> dict[str, object]:
    PROBE.ran.append("native_guarded")
    return {"title": data.title, "who": who.id}


@http.get("/native/html/{item_id}", renderers=[HTMLRenderer()])
async def native_html(item_id: int) -> str:
    return f"<p>{item_id}</p>"


@http.get("/native/html-bad", renderers=[HTMLRenderer()])
async def native_html_bad() -> dict[str, str]:
    return {"not": "a string"}


@http.get("/native/multi/{item_id}", renderers=[JSONRenderer(), XMLRenderer(root="r")])
async def native_multi(item_id: int) -> dict[str, object]:
    return {"id": item_id, "tags": ["a", "b"], "none": None}


@http.get("/native/pretty", renderers=[JSONRenderer(indent=2, sort_keys=True)])
async def native_pretty() -> dict[str, int]:
    return {"b": 1, "a": 2}


@http.post(
    "/native/multi-body",
    status=201,
    renderers=[XMLRenderer(), JSONRenderer()],
)
async def native_multi_body(data: Item) -> dict[str, object]:
    return {"title": data.title}


@http.post("/native/form", status=201, parsers=[JSONParser(), FormParser(max_fields=3)])
async def native_form(data: Item) -> dict[str, object]:
    return {"title": data.title}


@http.post("/native/form-source", status=201)
async def native_form_source(data: Form[Item]) -> dict[str, object]:
    return {"title": data.title, "count": data.count}


@http.post("/native/xml-source", status=201)
async def native_xml_source(data: XML[Item]) -> dict[str, object]:
    return {"title": data.title, "count": data.count}


class Search(Schema):
    term: str
    limit: int = 10
    ids: list[int] = dataclasses.field(default_factory=list)


@http.get("/native/query-source")
async def native_query_source(filters: Query[Search]) -> dict[str, object]:
    return {"term": filters.term, "limit": filters.limit, "ids": filters.ids}


@http.delete("/native/query-source/{item_id}")
def native_query_source_delete(
    item_id: int, filters: Query[Search]
) -> dict[str, object]:
    return {"id": item_id, "term": filters.term}


@http.post("/native/text", parsers=[TextParser()])
async def native_text(data: Item) -> dict[str, object]:
    return {"title": data.title}


class RefuseAll:
    async def check(self, context: RateLimitContext) -> None:
        raise TooManyRequests(7)


class RefuseSync:
    def check(self, context: RateLimitContext) -> None:
        raise TooManyRequests(3)


class AdmitEditors:
    """Admits only a caller the access policy has already named."""

    async def check(self, context: RateLimitContext) -> None:
        if context.caller is None or context.caller.id != "editor":
            raise TooManyRequests(5)


@http.get("/native/limited/{item_id}", rate_limit=RefuseAll())
async def native_limited(item_id: int) -> dict[str, object]:
    PROBE.ran.append("native_limited")
    return {"id": item_id}


@http.get("/native/limited-sync/{item_id}", rate_limit=RefuseSync())
def native_limited_sync(item_id: int) -> dict[str, object]:
    PROBE.ran.append("native_limited_sync")
    return {"id": item_id}


@http.post("/native/limited-body", status=201, rate_limit=RefuseAll())
async def native_limited_body(data: Item) -> dict[str, object]:
    PROBE.ran.append("native_limited_body")
    return {"title": data.title}


@http.get("/native/admitted/{item_id}", access=WRITERS, rate_limit=AdmitEditors())
async def native_admitted(item_id: int, who: Caller[str]) -> dict[str, object]:
    PROBE.ran.append("native_admitted")
    return {"id": item_id, "who": who.id}


@http.get("/native/typed/{ident}")
async def native_typed(
    ident: UUID, count: int, ratio: float = 1.5, flag: bool = False
) -> dict[str, object]:
    return {"ident": str(ident), "count": count, "ratio": ratio, "flag": flag}


@http.get("/pipeline/echo/{item_id}")
async def pipeline_echo(request: Request, item_id: str) -> dict[str, str]:
    PROBE.entered += 1
    return {"item_id": item_id}


@http.get("/pipeline/integer/{item_id}")
async def pipeline_integer(request: Request, item_id: int) -> dict[str, int]:
    return {"item_id": item_id}


@http.get("/pipeline/uuid/{item_id}")
async def pipeline_uuid(request: Request, item_id: UUID) -> dict[str, str]:
    return {"item_id": str(item_id)}


@http.get("/pipeline/sync/{item_id}")
def pipeline_sync(request: Request, item_id: str) -> dict[str, str]:
    PROBE.entered += 1
    return {"item_id": item_id}


@http.get("/pipeline/private/{item_id}", access=Access(auth=TokenAuth()))
async def pipeline_private(item_id: str) -> dict[str, str]:
    PROBE.entered += 1
    return {"item_id": item_id}


@http.get("/native/sync-echo/{item_id}")
def native_sync_echo(item_id: str, term: str) -> dict[str, str]:
    PROBE.native_input_calls += 1
    return {"item_id": item_id, "term": term}


async def observe_default_kwargs_impl(
    handler: Any, **kwargs: object
) -> dict[str, object]:
    """Expose the exact keyword order and default object seen by a wrapper."""
    del handler
    return {
        "keys": list(kwargs),
        "same_default": kwargs["view"] is DEFAULT_VIEW,
        "term": kwargs["term"],
        "view": kwargs["view"],
    }


def observe_default_kwargs(handler: Any) -> Any:
    """A top-level async partial that retains the wrapped route signature."""
    wrapper: Any = functools.partial(observe_default_kwargs_impl, handler)
    wrapper.__signature__ = inspect.signature(handler)
    wrapper.__annotations__ = handler.__annotations__
    wrapper.__wrapped__ = handler
    wrapper.__name__ = handler.__name__
    wrapper.__qualname__ = handler.__qualname__
    return wrapper


@http.get("/native/default-query", validate_response=False)
@observe_default_kwargs
async def native_default_query(view: str = DEFAULT_VIEW, *, term: str) -> object:
    raise AssertionError("the observing wrapper should replace this body")


@http.get("/native/default-query-sync")
def native_default_query_sync(view: str = DEFAULT_VIEW) -> dict[str, object]:
    return {"same_default": view is DEFAULT_VIEW, "view": view}


@http.get("/native/sync-late-awaitable", validate_response=False)
def native_sync_late_awaitable() -> object:
    PROBE.sync_entered.set()
    PROBE.sync_gate.wait(2)
    PROBE.late_coroutine = sync_awaitable_result()
    return PROBE.late_coroutine


@http.get("/context-overlap")
async def context_overlap() -> dict[str, str]:
    await asyncio.sleep(0.01)
    return {"request_id": request_id.get() or "missing"}


@http.get("/native-none")
async def native_none() -> None:
    return None


@http.get("/native-reply")
async def native_reply() -> Response:
    return Response(202, b"accepted", [("x-native", "yes")])


@http.get("/native-error")
async def native_error() -> dict[str, str]:
    raise ValueError("native failure")


@http.get("/native-timeout-error")
async def native_timeout_error() -> dict[str, str]:
    raise TimeoutError("handler timeout")


@http.get("/native-slow")
async def native_slow() -> dict[str, str]:
    await asyncio.sleep(5)
    return {"status": "late"}


@http.get("/native-sync-block")
def native_sync_block() -> dict[str, str]:
    PROBE.sync_entered.set()
    PROBE.sync_gate.wait(2)
    return {"status": "released"}


@http.post("/upload", parsers=[MultiPartParser(max_part_bytes=8)])
async def upload(file: UploadedFile) -> dict[str, object]:
    return {"filename": file.filename, "content": file.content.decode()}


@http.post("/upload-auto")
def upload_auto(file: UploadedFile) -> dict[str, object]:
    return {"filename": file.filename, "content": file.content.decode()}


@http.get("/big")
async def big() -> dict[str, str]:
    return {"data": "x" * (64 * 1024 * 1024)}


@http.get("/scheme")
async def scheme_of(request: Request) -> dict[str, str]:
    return {"scheme": request.scheme}


@http.get("/host")
async def host_of(request: Request) -> dict[str, str]:
    return {"host": request.host}


@http.post("/made", status=201)
async def made(request: Request) -> dict[str, bool]:
    return {"ok": True}


class Note(Schema):
    title: str


@http.post("/notes", status=201)
async def note(request: Request, data: Note) -> dict[str, str]:
    return {"title": data.title}


@http.post("/secured", access=Access(auth=TokenAuth()))
async def secured(request: Request, data: Note) -> dict[str, str]:
    PROBE.entered += 1
    return {"title": data.title}


@http.get("/block")
async def block() -> dict[str, str]:
    PROBE.entered += 1
    try:
        await PROBE.gate.wait()
    except asyncio.CancelledError:
        PROBE.cancelled += 1
        raise
    return {"released": "yes"}


async def note_shutdown(ctx: ModuleContext) -> None:
    PROBE.shutdown_ran = True


async def fail_startup(ctx: ModuleContext) -> None:
    raise RuntimeError("no database")


def make_app(**config: Any) -> Hinoki:
    return Hinoki(
        include=[
            health,
            context_async,
            context_sync,
            context_sync_awaitable,
            native_echo,
            native_remove,
            native_remove_sync,
            native_item,
            native_item_sync,
            native_me,
            native_guarded,
            native_limited,
            native_html,
            native_html_bad,
            native_multi,
            native_pretty,
            native_multi_body,
            native_form,
            native_form_source,
            native_xml_source,
            native_query_source,
            native_query_source_delete,
            native_text,
            native_limited_sync,
            native_limited_body,
            native_admitted,
            pipeline_echo,
            pipeline_integer,
            pipeline_uuid,
            pipeline_sync,
            pipeline_private,
            native_sync_echo,
            native_default_query,
            native_default_query_sync,
            native_sync_late_awaitable,
            context_overlap,
            native_none,
            native_reply,
            native_error,
            native_timeout_error,
            native_slow,
            native_sync_block,
            upload,
            upload_auto,
            big,
            scheme_of,
            host_of,
            made,
            note,
            secured,
            block,
        ],
        runtime=RuntimeConfig(**config),
        on_shutdown=[note_shutdown],
    )


@pytest.fixture
async def served() -> AsyncIterator[tuple[KisoWorker, int]]:
    worker = KisoWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    yield worker, port
    await worker.stop()


@asynccontextmanager
async def rust_probe(
    settings: AppSettings | None = None,
) -> AsyncIterator[tuple[KisoWorker, int]]:
    worker = KisoWorker(
        Hinoki(startup_url="/health", settings=settings), "127.0.0.1", 0
    )
    port = await worker.start()
    try:
        yield worker, port
    finally:
        await worker.stop()


@asynccontextmanager
async def asgi_probe(settings: AppSettings | None = None) -> AsyncIterator[Client]:
    client = Client(Hinoki(startup_url="/health", settings=settings).asgi())
    assert await client.lifespan_event("startup") == "lifespan.startup.complete"
    try:
        yield client
    finally:
        assert await client.lifespan_event("shutdown") == "lifespan.shutdown.complete"


async def compare_probe(
    port: int,
    client: Client,
    method: str,
    *,
    query: str = "",
    headers: dict[str, str] | None = None,
    scheme: str | None = None,
) -> Received:
    target = "/health" + (f"?{query}" if query else "")
    request_headers = headers or {}
    host = next(
        (value for name, value in request_headers.items() if name.lower() == "host"),
        "t",
    )
    extra = "".join(
        f"{name}: {value}\r\n"
        for name, value in request_headers.items()
        if name.lower() != "host"
    )
    actual = await fetch(port, method, target, extra=extra, host=host)
    expected = await client.request(
        method,
        "/health",
        raw_path=b"/health",
        query=query,
        headers=request_headers,
        scheme=scheme,
    )
    assert_pipeline_parity(actual, expected)
    return actual


async def test_startup_probe_answers_get_and_head_natively() -> None:
    async with rust_probe() as (worker, port), asgi_probe() as client:
        assert worker.core.ready
        get = await compare_probe(port, client, "GET")
        assert get.status == 200 and get.json() == {"status": "ok"}
        head = await compare_probe(port, client, "HEAD")
        assert head.status == 200 and head.body == b""
        assert head.header("content-length") == get.header("content-length")
    assert not worker.core.ready


async def test_startup_url_cannot_be_changed_to_shadow_a_protected_route() -> None:
    app = Hinoki(startup_url="/health")
    app.include(private)
    client = Client(app.asgi())
    startup_option = "startup_url"
    with pytest.raises(AttributeError):
        setattr(app, startup_option, "/private")
    assert await client.lifespan_event("startup") == "lifespan.startup.complete"
    try:
        response = await client.request("GET", "/private", raw_path=b"/private")
        assert response.status == 401
        assert response.body["error"] == "unauthorized"
    finally:
        assert await client.lifespan_event("shutdown") == "lifespan.shutdown.complete"


async def test_startup_probe_refuses_invalid_requests_with_pipeline_parity() -> None:
    async with rust_probe() as (_worker, port), asgi_probe() as client:
        wrong_method = await compare_probe(port, client, "POST")
        assert wrong_method.status == 405
        assert wrong_method.header("allow") == "GET, HEAD"
        unacceptable = await compare_probe(
            port,
            client,
            "GET",
            headers={"accept": "text/plain"},
        )
        assert unacceptable.status == 406
        assert unacceptable.json()["available"] == "application/json"
        malformed = await compare_probe(port, client, "GET", query="x=%FF")
        assert malformed.status == 400
        assert malformed.json()["error"] == "invalid_query"


async def test_startup_probe_applies_host_policy_before_answering() -> None:
    settings = AppSettings(allowed_hosts=("example.com",))
    async with rust_probe(settings) as (_worker, port), asgi_probe(settings) as client:
        rejected = await compare_probe(port, client, "GET")
        assert rejected.status == 400
        accepted = await compare_probe(
            port, client, "GET", headers={"host": "example.com"}
        )
        assert accepted.status == 200 and accepted.json() == {"status": "ok"}


async def test_startup_probe_answers_cors_preflight_natively() -> None:
    settings = AppSettings(cors=CorsSettings(origins=("https://client.example",)))
    async with rust_probe(settings) as (_worker, port), asgi_probe(settings) as client:
        headers = {
            "origin": "https://client.example",
            "access-control-request-method": "GET",
        }
        preflight = await compare_probe(
            port,
            client,
            "OPTIONS",
            headers=headers,
        )
        assert preflight.status == 204
        assert (
            preflight.header("access-control-allow-origin") == "https://client.example"
        )


async def test_startup_probe_applies_https_redirect_before_answering() -> None:
    settings = AppSettings(
        allowed_hosts=("example.com",),
        security=SecuritySettings(redirect_https=True),
    )
    async with rust_probe(settings) as (_worker, port), asgi_probe(settings) as client:
        redirect = await compare_probe(
            port,
            client,
            "GET",
            headers={"host": "example.com"},
            scheme="http",
        )
        assert redirect.status == 308
        assert redirect.header("location") == "https://example.com/health"


@dataclass
class Received:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str:
        return next(v for k, v in self.headers if k == name)

    def json(self) -> Any:
        return json.loads(self.body)


async def read_response(
    reader: asyncio.StreamReader, head_only: bool = False
) -> Received:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = [tuple(line.split(": ", 1)) for line in lines[1:] if line]
    length = next((int(v) for k, v in headers if k == "content-length"), 0)
    body = b"" if head_only else await asyncio.wait_for(reader.readexactly(length), 5)
    return Received(status, headers, body)  # type: ignore[arg-type]


async def open_connection(
    port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


async def fetch(
    port: int,
    method: str,
    target: str,
    extra: str = "",
    host: str = "t",
    body: bytes = b"",
) -> Received:
    reader, writer = await open_connection(port)
    head_only = method == "HEAD"
    head = f"{method} {target} HTTP/1.1\r\nhost: {host}\r\n{extra}\r\n"
    writer.write(head.encode() + body)
    received = await read_response(reader, head_only)
    writer.close()
    return received


async def pipeline_fetch(
    method: str,
    target: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> Any:
    path, _, query = target.partition("?")
    client = Client(make_app().asgi())
    await client.lifespan_event("startup")
    try:
        return await client.request(
            method,
            path,
            query=query,
            raw_path=path.encode("ascii"),
            headers=headers,
            body=body,
        )
    finally:
        await client.lifespan_event("shutdown")


def assert_pipeline_parity(received: Received, expected: Any) -> None:
    assert received.status == expected.status
    actual_headers = comparable(received.headers)
    pipeline_headers = [
        (name, value)
        for name, value in expected.headers.items()
        if name not in {"x-request-id", "date"}
    ]
    assert dict(actual_headers) == dict(pipeline_headers)
    if (
        not dict(received.headers)
        .get("content-type", "")
        .startswith("application/json")
    ):
        assert (received.body.decode() or None) == expected.body
        return
    actual_body = received.json() if received.body else None
    expected_body = expected.body
    if (
        isinstance(expected_body, dict)
        and "request_id" in expected_body
        and actual_body is not None
    ):
        expected_body = {**expected_body, "request_id": received.header("x-request-id")}
    assert actual_body == expected_body


async def eof(reader: asyncio.StreamReader, timeout: float = 5) -> bool:
    return await asyncio.wait_for(reader.read(1), timeout) == b""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def comparable(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Headers without the ones that differ per request (id, date)."""
    return [(k, v) for k, v in headers if k not in {"x-request-id", "date"}]


async def test_get_matches_the_asgi_answer(served: tuple[KisoWorker, int]) -> None:
    _, port = served
    over_socket = await fetch(port, "GET", "/health")
    client = Client(make_app().asgi())
    await client.lifespan_event("startup")
    try:
        in_process = await client.request("GET", "/health")
    finally:
        await client.lifespan_event("shutdown")
    assert over_socket.status == in_process.status == 200
    assert over_socket.json() == in_process.body
    assert dict(comparable(over_socket.headers)) == {
        k: v for k, v in in_process.headers.items() if k not in {"x-request-id"}
    }
    assert len(over_socket.header("x-request-id")) == 32


@pytest.mark.parametrize(
    "path", ["/context-async", "/context-sync", "/context-sync-awaitable"]
)
async def test_native_handler_keeps_the_request_context(
    served: tuple[KisoWorker, int], path: str
) -> None:
    _, port = served
    received = await fetch(port, "GET", path)
    assert received.status == 200
    assert received.json()["request_id"] == received.header("x-request-id")


async def test_native_handler_context_is_isolated_between_concurrent_requests(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    replies = await asyncio.gather(
        fetch(port, "GET", "/context-overlap"),
        fetch(port, "GET", "/context-overlap"),
    )
    ids = [reply.header("x-request-id") for reply in replies]
    assert ids[0] != ids[1]
    assert [reply.json()["request_id"] for reply in replies] == ids


async def test_native_string_inputs_match_pipeline_and_head() -> None:
    app = make_app()
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        target = "/native/echo/a%2Fb+c?term=green+tea"
        received = await fetch(port, "GET", target)
        expected = await pipeline_fetch("GET", target)
        assert_pipeline_parity(received, expected)
        assert received.json()["item_id"] == "a/b+c"
        assert received.json()["term"] == "green tea"

        sync_target = "/native/sync-echo/value?term=sync"
        sync_received = await fetch(port, "GET", sync_target)
        sync_expected = await pipeline_fetch("GET", sync_target)
        assert_pipeline_parity(sync_received, sync_expected)

        head = await fetch(port, "HEAD", target)
        get = await fetch(port, "GET", target)
        assert head.status == get.status == 200
        assert head.body == b""
        assert head.header("content-length") == get.header("content-length")
    finally:
        await worker.stop()


@pytest.mark.parametrize(
    ("method", "target", "native"),
    [
        ("DELETE", "/native/remove/7", True),
        ("DELETE", "/native/remove/x", True),
        ("DELETE", "/native/remove/7?extra=1", True),
        ("DELETE", "/native/remove-sync/a%2Fb?force=1", True),
        ("DELETE", "/native/remove-sync/a?force=maybe", True),
        ("DELETE", "/native/remove-sync/a?force=1&force=0", True),
        ("GET", "/native/remove/7", False),
        ("HEAD", "/native/remove-sync/a", False),
    ],
)
async def test_native_delete_matches_pipeline(
    method: str, target: str, native: bool
) -> None:
    worker = KisoWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    try:
        lanes = record_lanes(worker)
        received = await fetch(port, method, target)
        # A method the route does not serve is refused before Python is asked.
        assert lanes == ([True] if native else [])
        assert_pipeline_parity(received, await pipeline_fetch(method, target))
    finally:
        await worker.stop()


JSON = "content-type: application/json\r\n"
FORM = "content-type: application/x-www-form-urlencoded\r\n"
XMLT = "content-type: application/xml\r\n"
JSON_HEADER = {"content-type": "application/json"}


@pytest.mark.parametrize(
    ("method", "target", "extra", "headers", "body"),
    [
        ("POST", "/native/items/7?dry=true", JSON, JSON_HEADER, b'{"title":"a"}'),
        ("POST", "/native/items/7", JSON, JSON_HEADER, b'{"title":"a","count":3}'),
        ("PATCH", "/native/items-sync/7", JSON, JSON_HEADER, b'{"title":"a"}'),
        (
            "POST",
            "/native/items/7",
            "content-type: application/json; charset=utf-8\r\n",
            {"content-type": "application/json; charset=utf-8"},
            b'{"title":"a"}',
        ),
        ("POST", "/native/items/7", "", {}, b'{"title":"a"}'),
        (
            "POST",
            "/native/items/7",
            "content-type: text/plain\r\n",
            {"content-type": "text/plain"},
            b'{"title":"a"}',
        ),
        ("POST", "/native/items/7", JSON, JSON_HEADER, b"{nope"),
        ("POST", "/native/items/7", JSON, JSON_HEADER, b""),
        ("POST", "/native/items/7", JSON, JSON_HEADER, b'{"title":5}'),
        ("POST", "/native/items/x?dry=maybe", JSON, JSON_HEADER, b'{"title":5}'),
        ("POST", "/native/items/x", JSON, JSON_HEADER, b"{nope"),
        ("POST", "/native/items/7?dry=%ff", JSON, JSON_HEADER, b"{nope"),
        ("PATCH", "/native/items-sync/x", JSON, JSON_HEADER, b'{"count":"z"}'),
    ],
)
async def test_native_body_handlers_match_pipeline(
    method: str, target: str, extra: str, headers: dict[str, str], body: bytes
) -> None:
    worker = KisoWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    try:
        lanes = record_lanes(worker)
        length = f"content-length: {len(body)}\r\n"
        received = await fetch(port, method, target, extra + length, body=body)
        assert lanes == [True]
        assert_pipeline_parity(
            received, await pipeline_fetch(method, target, headers, body)
        )
    finally:
        await worker.stop()


async def test_native_body_over_the_limit_is_refused_like_the_pipeline() -> None:
    worker = KisoWorker(make_app(max_body_bytes=16), "127.0.0.1", 0)
    port = await worker.start()
    try:
        body = b'{"title":"' + b"a" * 64 + b'"}'
        length = f"content-length: {len(body)}\r\n"
        received = await fetch(
            port, "POST", "/native/items/7", JSON + length, body=body
        )
        assert received.status == 413
        assert received.json()["error"] == "payload_too_large"
    finally:
        await worker.stop()


async def test_native_body_early_refusal_closes_like_the_pipeline() -> None:
    worker = KisoWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    try:
        body = b'{"title":"a"}'
        extra = f"accept: text/html\r\n{JSON}content-length: {len(body)}\r\n"
        native = await fetch(port, "POST", "/native/items/7", extra, body=body)
        python = await fetch(port, "POST", "/notes", extra, body=body)
        assert native.status == python.status == 406
        assert native.header("connection") == python.header("connection")
    finally:
        await worker.stop()


def bearer(token: str) -> tuple[str, dict[str, str]]:
    return f"authorization: Bearer {token}\r\n", {"authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("method", "target", "auth", "extra", "body"),
    [
        ("GET", "/native/me/3?limit=9", "editor-token", "", b""),
        ("GET", "/native/me/3", "reader-token", "", b""),
        ("GET", "/native/me/3", "nobody", "", b""),
        ("GET", "/native/me/3", None, "", b""),
        ("GET", "/native/me/x?limit=y", None, "", b""),
        ("GET", "/native/me/x?limit=y", "editor-token", "", b""),
        ("GET", "/native/me/3", None, "accept: text/html\r\n", b""),
        ("GET", "/native/me/3", "editor-token", "accept: text/html\r\n", b""),
        ("GET", "/native/me/3?limit=%ff", None, "", b""),
        ("GET", "/native/me/3?limit=%ff", "editor-token", "", b""),
        ("GET", "/native/me/%ff", None, "", b""),
        ("POST", "/native/guarded", "reader-token", JSON, b'{"title":"a"}'),
        ("POST", "/native/guarded", None, JSON, b'{"title":"a"}'),
        ("POST", "/native/guarded", None, JSON, b"{nope"),
        ("POST", "/native/guarded", "reader-token", JSON, b"{nope"),
        ("POST", "/native/guarded", "nobody", JSON, b'{"title":"a"}'),
        ("POST", "/native/guarded", "reader-token", JSON, b'{"title":5}'),
    ],
)
async def test_native_access_matches_pipeline(
    method: str, target: str, auth: str | None, extra: str, body: bytes
) -> None:
    await check_native_parity(method, target, auth, extra, body)


async def check_native_parity(
    method: str, target: str, auth: str | None, extra: str, body: bytes
) -> None:
    worker = KisoWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    try:
        raw, headers = bearer(auth) if auth else ("", {})
        for item in extra.strip().split("\r\n"):
            if item:
                name, _, value = item.partition(": ")
                headers[name] = value
        length = f"content-length: {len(body)}\r\n" if body else ""
        sent = raw + extra + length
        lanes = record_lanes(worker)
        received = await fetch(port, method, target, sent, body=body)
        assert lanes == [True]
        if received.status >= 400:
            # A refused or invalid request never reaches the handler.
            assert PROBE.ran == []
        if method == "POST" and "connection" in dict(received.headers):
            # The ASGI client has no connection to close; the Python lane on the
            # same socket is the reference for it.
            same = await fetch(port, method, "/secured", sent, body=body)
            assert [v for k, v in received.headers if k == "connection"] == [
                v for k, v in same.headers if k == "connection"
            ]
            received.headers[:] = [
                (k, v) for k, v in received.headers if k != "connection"
            ]
        assert_pipeline_parity(
            received, await pipeline_fetch(method, target, headers, body)
        )
    finally:
        await worker.stop()


@pytest.mark.parametrize(
    ("method", "target", "auth", "extra", "body"),
    [
        ("GET", "/native/limited/3", None, "", b""),
        ("GET", "/native/limited/x", None, "", b""),
        ("GET", "/native/limited/3", None, "accept: text/html\r\n", b""),
        ("GET", "/native/limited-sync/3", None, "", b""),
        ("POST", "/native/limited-body", None, JSON, b'{"title":"a"}'),
        ("POST", "/native/limited-body", None, JSON, b"{nope"),
        ("GET", "/native/admitted/3", "editor-token", "", b""),
        ("GET", "/native/admitted/3", "reader-token", "", b""),
        ("GET", "/native/admitted/3", None, "", b""),
        ("GET", "/native/admitted/x", "reader-token", "", b""),
        ("GET", "/native/admitted/3?limit=%ff", None, "", b""),
    ],
)
async def test_native_rate_limit_matches_pipeline(
    method: str, target: str, auth: str | None, extra: str, body: bytes
) -> None:
    await check_native_parity(method, target, auth, extra, body)


@pytest.mark.parametrize(
    ("method", "target", "extra", "body"),
    [
        ("GET", "/native/html/3", "", b""),
        ("GET", "/native/html/3", "accept: text/html\r\n", b""),
        ("GET", "/native/html/3", "accept: application/json\r\n", b""),
        ("GET", "/native/html/x", "", b""),
        ("GET", "/native/html-bad", "", b""),
        ("GET", "/native/multi/3", "", b""),
        ("GET", "/native/multi/3", "accept: application/xml\r\n", b""),
        ("GET", "/native/multi/3", "accept: application/json\r\n", b""),
        ("GET", "/native/multi/3", "accept: */*;q=0.1\r\n", b""),
        (
            "GET",
            "/native/multi/3",
            "accept: application/xml;q=0.9, application/json;q=0.2\r\n",
            b"",
        ),
        ("GET", "/native/multi/3", "accept: text/csv\r\n", b""),
        ("GET", "/native/multi/x", "accept: text/csv\r\n", b""),
        ("GET", "/native/pretty", "", b""),
        ("POST", "/native/multi-body", JSON, b'{"title":"a"}'),
        ("POST", "/native/form", FORM, b"title=a"),
        ("POST", "/native/form", FORM, b"title=a&title=b&count=x"),
        ("POST", "/native/form", FORM, b"title=a&b=1&c=2&d=3"),
        ("POST", "/native/form", FORM, b"title=%ff"),
        ("POST", "/native/form", JSON, b'{"title":"a"}'),
        ("POST", "/native/form", "content-type: text/plain\r\n", b"title=a"),
        ("POST", "/native/form-source", FORM, b"title=a&count=4"),
        ("POST", "/native/form-source", FORM, b"title=a&count=x"),
        ("POST", "/native/form-source", FORM, b"title=a&count=1&count=2"),
        ("POST", "/native/form-source", FORM, b"count=3"),
        ("POST", "/native/form-source", JSON, b'{"title":"a"}'),
        (
            "POST",
            "/native/xml-source",
            XMLT,
            b"<i><title>t</title><count>4</count></i>",
        ),
        (
            "POST",
            "/native/xml-source",
            XMLT,
            b"<i><title>t</title><count>x</count></i>",
        ),
        ("POST", "/native/xml-source", XMLT, b"<i><count>3</count></i>"),
        ("POST", "/native/xml-source", XMLT, b"<!DOCTYPE i><i/>"),
        ("POST", "/native/xml-source", XMLT, b"<i><title>a"),
        ("POST", "/native/xml-source", JSON, b'{"title":"a"}'),
        ("GET", "/native/query-source?term=a&limit=3&ids=1&ids=2", "", b""),
        ("GET", "/native/query-source?term=a", "", b""),
        ("GET", "/native/query-source", "", b""),
        ("GET", "/native/query-source?term=a&limit=x", "", b""),
        ("GET", "/native/query-source?term=a&limit=1&limit=2", "", b""),
        ("GET", "/native/query-source?term=a&zzz=1", "", b""),
        ("GET", "/native/query-source?term=%ff", "", b""),
        ("DELETE", "/native/query-source/4?term=a", "", b""),
        ("DELETE", "/native/query-source/x?term=a", "", b""),
        ("DELETE", "/native/query-source/x", "", b""),
        ("POST", "/native/text", "content-type: text/plain\r\n", b"hello"),
        ("POST", "/native/text", "content-type: text/plain\r\n", b"\xff"),
        (
            "POST",
            "/native/multi-body",
            JSON + "accept: application/json\r\n",
            b'{"title":"a"}',
        ),
    ],
)
async def test_native_renderers_match_pipeline(
    method: str, target: str, extra: str, body: bytes
) -> None:
    await check_native_parity(method, target, None, extra, body)


class JSONShape(JSONRenderer):
    pass


class OwnRenderer(HTMLRenderer):
    pass


def test_only_builtin_renderers_with_their_options_are_native() -> None:
    assert all(
        is_native_renderer(r)
        for r in (JSONRenderer(indent=2), HTMLRenderer(), XMLRenderer(item="x"))
    )
    assert not is_native_renderer(JSONShape())
    assert not is_native_renderer(OwnRenderer())
    changed = HTMLRenderer()
    changed.media_type = "text/x-other"  # type: ignore[misc]
    assert not is_native_renderer(changed)


TYPED_ID = "123e4567-E89b-12d3-a456-426614174000"


@pytest.mark.parametrize(
    "target",
    [
        f"/native/typed/{TYPED_ID}?count=-7&ratio=2e1&flag=true",
        f"/native/typed/{TYPED_ID}?count=0007",
        f"/native/typed/{TYPED_ID}?count=999999999999999999",
        f"/native/typed/{TYPED_ID}?count=1000000000000000000",
        f"/native/typed/{TYPED_ID}?count={'9' * 5000}",
        f"/native/typed/{TYPED_ID}?count=1&ratio=1e999",
        f"/native/typed/{TYPED_ID}?count=1&ratio=.5&flag=TRUE",
        f"/native/typed/{TYPED_ID}?count=1.0&count=2",
        f"/native/typed/{TYPED_ID}",
        "/native/typed/not-a-uuid?count=x&ratio=nan&flag=2",
        "/native/typed/123e4567e89b12d3a456426614174000?count=1",
    ],
)
async def test_native_typed_inputs_match_pipeline(target: str) -> None:
    worker = KisoWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    try:
        assert_pipeline_parity(
            await fetch(port, "GET", target), await pipeline_fetch("GET", target)
        )
    finally:
        await worker.stop()


async def test_native_defaulted_string_queries_match_pipeline() -> None:
    app = make_app()
    native = build_server_core(app.asgi().pipeline)
    native.set_ready(True)
    native.submit("GET", "/native/default-query", b"term=tail", [])
    [prepared] = native.drain(1)
    assert prepared.native_handler is True
    assert prepared.native_args == {"term": "tail"}
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        for target in (
            "/native/default-query?term=tail",
            "/native/default-query?term=&view=",
            "/native/default-query?term=tail&view=custom",
            "/native/default-query?term=tail&view=one&view=two",
            "/native/default-query?view=one&view=two",
            "/native/default-query",
            "/native/default-query-sync",
            "/native/default-query-sync?view=custom",
        ):
            received = await fetch(port, "GET", target)
            expected = await pipeline_fetch("GET", target)
            assert_pipeline_parity(received, expected)
        defaulted = await fetch(port, "GET", "/native/default-query?term=tail")
        assert defaulted.json() == {
            "keys": ["view", "term"],
            "same_default": True,
            "term": "tail",
            "view": DEFAULT_VIEW,
        }
        empty = await fetch(port, "GET", "/native/default-query?term=&view=")
        assert empty.json() == {
            "keys": ["view", "term"],
            "same_default": False,
            "term": "",
            "view": "",
        }
        head = await fetch(port, "HEAD", "/native/default-query?term=tail")
        assert head.status == defaulted.status == 200
        assert head.body == b""
        assert head.header("content-length") == defaulted.header("content-length")
    finally:
        await worker.stop()


async def test_python_pipeline_routes_use_prepared_path_values_once() -> None:
    app = make_app()
    client = Client(app.asgi())
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        for target, expected in (
            ("/pipeline/echo/plain", {"item_id": "plain"}),
            ("/pipeline/echo/a%2Fb", {"item_id": "a/b"}),
            ("/pipeline/echo/a%252Fb", {"item_id": "a%2Fb"}),
            ("/pipeline/echo/a+b", {"item_id": "a+b"}),
            ("/pipeline/echo/caf%C3%A9", {"item_id": "café"}),
            ("/pipeline/echo/%G1", {"item_id": "%G1"}),
            ("/pipeline/echo/%4", {"item_id": "%4"}),
            ("/pipeline/integer/%34", {"item_id": 4}),
            (
                "/pipeline/uuid/12345678-1234-5678-1234-567812345678",
                {"item_id": "12345678-1234-5678-1234-567812345678"},
            ),
            ("/pipeline/sync/a%2Fb", {"item_id": "a/b"}),
        ):
            path = target.partition("?")[0]
            expected_asgi = await client.request(
                "GET", path, raw_path=path.encode("ascii")
            )
            received = await fetch(port, "GET", path)
            assert_pipeline_parity(received, expected_asgi)
            assert received.json() == expected

        invalid = "/pipeline/echo/%FF"
        expected_asgi = await client.request(
            "GET", invalid, raw_path=invalid.encode("ascii")
        )
        received = await fetch(port, "GET", invalid)
        assert_pipeline_parity(received, expected_asgi)
        assert received.status == 400
        assert received.json()["error"] == "bad_path_encoding"

        invalid_accept = await fetch(
            port, "GET", invalid, extra="accept: text/plain\r\n"
        )
        assert invalid_accept.status == 400
        assert invalid_accept.json()["error"] == "bad_path_encoding"

        protected = await fetch(port, "GET", "/pipeline/private/%FF")
        assert protected.status == 400
        assert protected.json()["error"] == "bad_path_encoding"
        assert PROBE.entered == 16
    finally:
        await worker.stop()


async def test_invalid_prepared_path_keeps_early_policy_precedence() -> None:
    settings = AppSettings(
        allowed_hosts=("allowed.example",),
        cors=CorsSettings(origins=("https://client.example",)),
        security=SecuritySettings(redirect_https=True),
    )
    app = Hinoki(include=[pipeline_echo], settings=settings)
    client = Client(app.asgi())
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    invalid = "/pipeline/echo/%FF"
    try:
        wrong_host = await fetch(port, "GET", invalid, host="wrong.example")
        expected_wrong_host = await client.request(
            "GET",
            invalid,
            raw_path=invalid.encode("ascii"),
            headers={"host": "wrong.example"},
        )
        assert_pipeline_parity(wrong_host, expected_wrong_host)
        assert wrong_host.status == 400
        assert wrong_host.json()["error"] == "disallowed_host"

        redirected = await fetch(port, "GET", invalid, host="allowed.example")
        expected_redirected = await client.request(
            "GET",
            invalid,
            raw_path=invalid.encode("ascii"),
            headers={"host": "allowed.example"},
            scheme="http",
        )
        assert_pipeline_parity(redirected, expected_redirected)
        assert redirected.status == 308
        assert redirected.header("location") == "https://allowed.example" + invalid

        extra = (
            "origin: https://client.example\r\naccess-control-request-method: GET\r\n"
        )
        preflight = await fetch(
            port, "OPTIONS", invalid, extra=extra, host="allowed.example"
        )
        expected_preflight = await client.request(
            "OPTIONS",
            invalid,
            raw_path=invalid.encode("ascii"),
            headers={
                "host": "allowed.example",
                "origin": "https://client.example",
                "access-control-request-method": "GET",
            },
        )
        assert_pipeline_parity(preflight, expected_preflight)
        assert preflight.status == 204
        assert (
            preflight.header("access-control-allow-origin") == "https://client.example"
        )
    finally:
        await worker.stop()


@pytest.mark.parametrize(
    ("target", "extra", "status", "error"),
    [
        (
            "/native/echo/bad%FF?term=x",
            "accept: text/plain\r\n",
            400,
            "bad_path_encoding",
        ),
        ("/native/echo/x?term=%FF", "accept: text/plain\r\n", 406, "not_acceptable"),
        ("/native/echo/x", "", 422, "validation_error"),
        ("/native/echo/x?term=a&term=b", "", 422, "validation_error"),
        ("/native/echo/x?unknown=%FF", "", 400, "invalid_query"),
    ],
)
async def test_native_input_errors_match_pipeline(
    served: tuple[KisoWorker, int], target: str, extra: str, status: int, error: str
) -> None:
    _, port = served
    before = PROBE.native_input_calls
    received = await fetch(port, "GET", target, extra)
    headers = {"accept": "text/plain"} if extra else None
    expected = await pipeline_fetch("GET", target, headers)
    assert_pipeline_parity(received, expected)
    assert received.status == status
    assert received.json()["error"] == error
    assert PROBE.native_input_calls == before


async def test_native_none_and_reply_results_keep_pipeline_semantics(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    empty = await fetch(port, "GET", "/native-none")
    custom = await fetch(port, "GET", "/native-reply")
    assert (empty.status, empty.body) == (204, b"")
    assert not any(name == "content-length" for name, _ in empty.headers)
    assert (custom.status, custom.body, custom.header("x-native")) == (
        202,
        b"accepted",
        "yes",
    )


async def test_native_handler_errors_keep_the_request_id(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    received = await fetch(port, "GET", "/native-error")
    assert received.status == 500
    assert received.json()["error"] == "internal_error"
    assert received.json()["request_id"] == received.header("x-request-id")


async def test_native_handler_timeout_differs_from_handler_timeout_error() -> None:
    app = Hinoki(
        include=[native_slow, native_timeout_error],
        runtime=RuntimeConfig(request_timeout=0.05),
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        expired = await fetch(port, "GET", "/native-slow")
        raised = await fetch(port, "GET", "/native-timeout-error")
    finally:
        await worker.stop()
    assert (expired.status, expired.json()["error"]) == (504, "timeout")
    assert expired.json()["request_id"] == expired.header("x-request-id")
    assert raised.status == 500
    assert raised.json()["request_id"] == raised.header("x-request-id")


async def test_native_sync_handler_saturation_answers_503() -> None:
    app = Hinoki(
        include=[native_sync_block],
        runtime=RuntimeConfig(sync_workers=1, sync_queue=0),
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    first = asyncio.create_task(fetch(port, "GET", "/native-sync-block"))
    try:
        entered = await asyncio.to_thread(PROBE.sync_entered.wait, 2)
        assert entered
        saturated = await fetch(port, "GET", "/native-sync-block")
        assert saturated.status == 503
        assert saturated.json()["error"] == "overloaded"
    finally:
        PROBE.sync_gate.set()
        await first
        await worker.stop()


async def test_native_timeout_closes_awaitable_returned_by_abandoned_sync_handler() -> (
    None
):
    worker = KisoWorker(
        Hinoki(
            include=[native_sync_late_awaitable],
            runtime=RuntimeConfig(request_timeout=0.02, sync_workers=1, sync_queue=0),
        ),
        "127.0.0.1",
        0,
    )
    port = await worker.start()
    pending = asyncio.create_task(fetch(port, "GET", "/native/sync-late-awaitable"))
    try:
        assert await asyncio.to_thread(PROBE.sync_entered.wait, 1)
        expired = await pending
        assert (expired.status, expired.json()["error"]) == (504, "timeout")
        PROBE.sync_gate.set()
        await worker.pipeline.executor.drain(2)
        assert PROBE.late_coroutine is not None
        assert inspect.getcoroutinestate(PROBE.late_coroutine) == inspect.CORO_CLOSED
    finally:
        PROBE.sync_gate.set()
        if not pending.done():
            await pending
        await worker.stop()


async def test_native_handler_negotiates_accept_before_calling_handler() -> None:
    app = Hinoki(include=[context_async])
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        received = await fetch(port, "GET", "/context-async", "accept: text/plain\r\n")
    finally:
        await worker.stop()
    assert received.status == 406
    assert received.json()["error"] == "not_acceptable"
    assert received.json()["request_id"] == received.header("x-request-id")


async def test_inline_dispatch_serves_direct_suspending_head_and_errors() -> None:
    @http.get("/inline-fast")
    async def inline_fast() -> dict[str, int]:
        return {"ok": 1}

    @http.get("/inline-suspend")
    async def inline_suspend() -> dict[str, str]:
        await asyncio.sleep(0)
        return {"mode": "suspended"}

    @http.get("/inline-boom")
    async def inline_boom() -> dict[str, str]:
        raise RuntimeError("boom")

    app = Hinoki(include=[inline_fast, inline_suspend, inline_boom])
    worker = KisoWorker(app, "127.0.0.1", 0, inline_dispatch=True)
    port = await worker.start()
    try:
        fast = await fetch(port, "GET", "/inline-fast")
        head = await fetch(port, "HEAD", "/inline-fast")
        suspended = await fetch(port, "GET", "/inline-suspend")
        boom = await fetch(port, "GET", "/inline-boom")
    finally:
        await worker.stop()

    assert fast.status == 200
    assert fast.header("content-type") == "application/json"
    assert fast.header("content-length") == str(len(fast.body))
    assert fast.header("x-request-id")
    assert fast.json() == {"ok": 1}

    assert head.status == 200
    assert head.body == b""
    assert head.header("content-length") == str(len(fast.body))

    assert suspended.status == 200
    assert suspended.json() == {"mode": "suspended"}

    assert boom.status == 500
    assert boom.json()["error"] == "internal_error"
    assert boom.json()["request_id"] == boom.header("x-request-id")


def test_supervisor_does_not_enable_inline_dispatch_by_default() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        single = Supervisor("app:app", listener, 1, 5.0)
        multi = Supervisor("app:app", listener, 4, 5.0)
        assert "--inline-dispatch" not in single.command()
        assert "--inline-dispatch" not in multi.command()


@pytest.mark.parametrize("query", ["x=%FF", "&".join(["x=1"] * 1001)])
async def test_native_handler_preserves_query_validation(
    query: str,
) -> None:
    app = Hinoki(include=[health])
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        received = await fetch(port, "GET", f"/health?{query}")
    finally:
        await worker.stop()
    assert received.status == 400
    assert received.json()["error"] == "invalid_query"
    assert received.json()["request_id"] == received.header("x-request-id")


async def test_native_finalizer_keeps_security_headers() -> None:
    app = Hinoki(
        include=[health],
        settings=AppSettings(
            security=SecuritySettings(
                hsts_seconds=31_536_000, frame_options=FrameOptions.DENY
            )
        ),
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        received = await fetch(port, "GET", "/health")
    finally:
        await worker.stop()
    assert received.header("x-frame-options") == "DENY"


@http.get("/native-own-origin")
async def native_own_origin() -> Response:
    return Response(
        200, b"own", [("access-control-allow-origin", "https://own.example")]
    )


class NoBody:
    async def read(self, limit: int) -> bytes:
        return b""


def final_answer(
    status: int, headers: list[tuple[str, str]], body: bytes
) -> tuple[int, list[tuple[str, str]], object]:
    """Status, the final headers in order (request id masked, no date) and the
    body with its request id masked."""
    rid = next(value for name, value in headers if name == "x-request-id")
    pairs = [
        (name, "<id>" if name == "x-request-id" else value)
        for name, value in headers
        if name != "date"
    ]
    decoded: object = json.loads(body) if body.startswith(b"{") else body
    if isinstance(decoded, dict) and "request_id" in decoded:
        assert decoded.pop("request_id") == rid
    return status, pairs, decoded


async def pipeline_answer(
    worker: KisoWorker, method: str, target: str, headers: list[tuple[bytes, bytes]]
) -> tuple[int, list[tuple[str, str]], object]:
    """The same request served by the Python pipeline, as the ASGI adapter
    (Granian) serves it."""
    path, _, query = target.partition("?")
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "headers": headers,
        "scheme": "http",
    }
    response = await worker.pipeline.handle(scope, NoBody())
    body = b"" if method == "HEAD" else response.body
    pairs = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in response.headers]
    return final_answer(response.status, pairs, body)


def record_lanes(worker: KisoWorker) -> list[bool]:
    """Record, for every request the worker serves from now on, whether the
    real queued request took the native handler lane."""
    lanes: list[bool] = []
    serve = worker._pipeline_response

    async def recorded(
        request: core.PreparedRequest,
    ) -> tuple[responses.Response, bool]:
        lanes.append(request.native_handler)
        return await serve(request)

    worker._pipeline_response = recorded  # type: ignore[method-assign]
    if worker.lane is not None:
        begin = worker.lane.start

        def driven(request: core.PreparedRequest) -> bool:
            taken = begin(request)
            if taken:
                lanes.append(True)  # Rust drives the handler: a native lane too
            return taken

        worker.lane.start = driven  # type: ignore[method-assign]
    return lanes


@pytest.mark.parametrize(
    "cors",
    [
        CorsSettings(
            origins=("https://client.example",),
            credentials=True,
            expose_headers=("x-total",),
        ),
        CorsSettings(origins=("*",)),
    ],
)
@pytest.mark.parametrize(
    "origin", ["https://client.example", "https://evil.example", ""]
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_native_handler_answers_carry_the_pipeline_cors_headers(
    cors: CorsSettings, origin: str, method: str
) -> None:
    app = Hinoki(
        include=[native_echo, native_own_origin, native_slow],
        settings=AppSettings(cors=cors),
        runtime=RuntimeConfig(request_timeout=0.05),
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    headers = [(b"host", b"t")] + ([(b"origin", origin.encode())] if origin else [])
    extra = f"origin: {origin}\r\n" if origin else ""
    targets = {
        "/native/echo/a?term=b": 200,
        "/native/echo/a": 422,
        "/native-own-origin": 200,
        "/native-slow": 504,
    }
    try:
        for target, status in targets.items():
            lanes = record_lanes(worker)
            received = await fetch(port, method, target, extra=extra)
            assert lanes == [True]
            native = final_answer(received.status, received.headers, received.body)
            assert native[0] == status
            assert native == await pipeline_answer(worker, method, target, headers)
    finally:
        await worker.stop()


class Branded(API):
    response_headers = (("x-brand", "kiso"), ("x-frame-options", "ALLOWALL"))

    @http.get("/branded")
    def show(self) -> dict[str, str]:
        return {"brand": "kiso"}


@pytest.mark.parametrize("method", ["GET", "HEAD", "DELETE"])
@pytest.mark.parametrize(
    "origin", ["https://client.example", "https://evil.example", ""]
)
async def test_unrouted_answers_under_policies_match_the_pipeline(
    origin: str, method: str
) -> None:
    app = Hinoki(
        include=[Branded],
        settings=AppSettings(
            allowed_hosts=("t",),
            cors=CorsSettings(origins=("https://client.example",), credentials=True),
        ),
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    headers = [(b"host", b"t")] + ([(b"origin", origin.encode())] if origin else [])
    extra = f"origin: {origin}\r\n" if origin else ""
    try:
        lanes = record_lanes(worker)
        for target, status in {"/missing": 404, "/branded": 405}.items():
            if method != "DELETE" and target == "/branded":
                continue
            received = await fetch(port, method, target, extra=extra)
            served = final_answer(received.status, received.headers, received.body)
            assert served[0] == status
            assert served == await pipeline_answer(worker, method, target, headers)
        assert lanes == []
    finally:
        await worker.stop()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("origin", ["https://client.example", ""])
async def test_documentation_pages_are_answered_without_python(
    origin: str, method: str
) -> None:
    app = Hinoki(
        include=[Branded],
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        settings=AppSettings(
            cors=CorsSettings(origins=("https://client.example",), credentials=True)
        ),
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    headers = [(b"host", b"t")] + ([(b"origin", origin.encode())] if origin else [])
    extra = f"origin: {origin}\r\n" if origin else ""
    targets = [app.openapi_url, *worker.pipeline.docs, "/openapi%2Ejson"]
    try:
        assert len(targets) > 3
        lanes = record_lanes(worker)
        for target in targets:
            assert target is not None
            received = await fetch(port, method, target, extra=extra)
            served = final_answer(received.status, received.headers, received.body)
            assert served[0] == 200
            oracle = target.replace("%2E", ".")
            assert served == await pipeline_answer(worker, method, oracle, headers)
        assert lanes == []
    finally:
        await worker.stop()


@pytest.mark.parametrize(
    "origin", ["https://client.example", "https://evil.example", ""]
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_pipeline_answers_carry_the_headers_the_adapter_gives(
    origin: str, method: str
) -> None:
    app = Hinoki(
        include=[Branded, context_sync, private, native_limited],
        settings=AppSettings(
            cors=CorsSettings(origins=("https://client.example",), credentials=True)
        ),
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    headers = [(b"host", b"t")] + ([(b"origin", origin.encode())] if origin else [])
    extra = f"origin: {origin}\r\n" if origin else ""
    targets = {
        "/branded": 200,
        "/context-sync": 200,
        "/private": 401,
        "/native/limited/1": 429,
    }
    try:
        for target, status in targets.items():
            lanes = record_lanes(worker)
            received = await fetch(port, method, target, extra=extra)
            if target == "/branded":
                assert lanes == [False]
            served = final_answer(received.status, received.headers, received.body)
            assert served[0] == status
            assert served == await pipeline_answer(worker, method, target, headers)
    finally:
        await worker.stop()


@pytest.mark.parametrize("path", ["/upload", "/upload-auto"])
async def test_multipart_file_binding_works_over_the_native_server(
    served: tuple[KisoWorker, int], path: str
) -> None:
    _, port = served
    boundary = "rust-upload-test"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="note.txt"\r\n'
        "Content-Type: text/plain\r\n\r\nnative\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    request = (
        f"POST {path} HTTP/1.1\r\nhost: t\r\n"
        f"content-type: multipart/form-data; boundary={boundary}\r\n"
        f"content-length: {len(body)}\r\n\r\n"
    ).encode() + body
    received = await raw(port, request)
    assert (received.status, received.json()) == (
        200,
        {"filename": "note.txt", "content": "native"},
    )


async def test_the_native_404_and_405_carry_the_request_id(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    missing = await fetch(port, "GET", "/nope")
    assert missing.status == 404
    assert missing.json() == {
        "error": "not_found",
        "request_id": missing.header("x-request-id"),
    }
    wrong = await fetch(port, "DELETE", "/health")
    assert wrong.status == 405
    assert wrong.header("allow") == "GET, HEAD"
    assert wrong.header("x-request-id") != missing.header("x-request-id")


async def test_head_keeps_the_length_and_sends_no_body(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    writer.write(b"HEAD /health HTTP/1.1\r\nhost: t\r\n\r\n")
    head = await read_response(reader, head_only=True)
    assert head.status == 200
    assert head.header("content-length") == "15"
    writer.write(b"GET /health HTTP/1.1\r\nhost: t\r\n\r\n")  # framing still intact
    assert (await read_response(reader)).json() == {"status": "ok"}
    writer.close()


async def test_a_non_ascii_target_gets_the_pipelines_400(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    writer.write("GET /caf\u00e9 HTTP/1.1\r\nhost: t\r\n\r\n".encode())
    refused = await read_response(reader)
    assert refused.status == 400
    assert refused.json()["error"] == "bad_path_encoding"
    assert refused.json()["request_id"] == refused.header("x-request-id")
    writer.close()


async def test_keep_alive_and_pipelining(served: tuple[KisoWorker, int]) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    request = b"GET /health HTTP/1.1\r\nhost: t\r\n\r\n"
    writer.write(request + request + b"GET /nope HTTP/1.1\r\nhost: t\r\n\r\n")
    statuses = [(await read_response(reader)).status for _ in range(3)]
    assert statuses == [200, 200, 404]
    writer.close()


async def test_a_post_without_a_body_reaches_the_handler(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    created = await fetch(port, "POST", "/made", "content-length: 0\r\n")
    assert (created.status, created.json()) == (201, {"ok": True})


def post(path: str, body: bytes, extra: str = "") -> bytes:
    head = f"POST {path} HTTP/1.1\r\nhost: t\r\ncontent-type: application/json\r\n"
    return f"{head}content-length: {len(body)}\r\n{extra}\r\n".encode() + body


async def test_a_json_body_reaches_the_handler_and_the_connection_is_reused(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    body = json.dumps({"title": "caf\u00e9\nline"}, indent=2).encode()
    for _ in range(2):
        writer.write(post("/notes", body))
        created = await read_response(reader)
        assert (created.status, created.json()) == (201, {"title": "caf\u00e9\nline"})
        assert not any(k == "connection" for k, _ in created.headers)
    writer.close()


async def test_a_chunked_json_body_reaches_the_handler(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    body = b'{"title": "chunky"}'
    chunks = b"".join(
        f"{len(c):x}\r\n".encode() + c + b"\r\n" for c in (body[:7], body[7:])
    )
    head = "POST /notes HTTP/1.1\r\nhost: t\r\ncontent-type: application/json\r\n"
    writer.write(
        f"{head}transfer-encoding: chunked\r\n\r\n".encode() + chunks + b"0\r\n\r\n"
    )
    created = await read_response(reader)
    assert (created.status, created.json()) == (201, {"title": "chunky"})
    assert created.header("connection") == "close"
    assert await eof(reader)
    writer.close()


async def asgi_answer(method: str, path: str, **kwargs: Any) -> Any:
    client = Client(make_app(max_body_bytes=64).asgi())
    await client.lifespan_event("startup")
    try:
        return await client.request(method, path, **kwargs)
    finally:
        await client.lifespan_event("shutdown")


async def send_streaming(port: int, head: bytes, body: bytes) -> tuple[Received, bool]:
    """Send `body` while reading the answer, as a real client does."""
    reader, writer = await open_connection(port)
    writer.write(head)

    async def push() -> None:
        try:
            writer.write(body)
            await writer.drain()
        except ConnectionError:
            pass  # the server answered and closed first

    sender = asyncio.create_task(push())
    received = await read_response(reader)
    closed = await eof(reader)
    sender.cancel()
    writer.close()
    return received, closed


async def test_a_declared_length_over_the_limit_is_a_413_like_asgi() -> None:
    worker = KisoWorker(make_app(max_body_bytes=64), "127.0.0.1", 0)
    port = await worker.start()
    try:
        size = 256 * 1024
        head = f"POST /notes HTTP/1.1\r\nhost: t\r\ncontent-length: {size}\r\n\r\n"
        refused, closed = await send_streaming(port, head.encode(), b"x" * size)
    finally:
        await worker.stop()
    expected = await asgi_answer(
        "POST",
        "/notes",
        body=b"x" * 65,
        content_type="application/json",
        headers={"content-length": str(size)},
    )
    assert refused.status == expected.status == 413
    assert refused.json() == {
        **expected.body,
        "request_id": refused.json()["request_id"],
    }
    assert refused.header("connection") == "close"
    assert closed


async def test_a_chunked_total_over_the_limit_is_a_413() -> None:
    worker = KisoWorker(make_app(max_body_bytes=64), "127.0.0.1", 0)
    port = await worker.start()
    try:
        head = b"POST /notes HTTP/1.1\r\nhost: t\r\ntransfer-encoding: chunked\r\n\r\n"
        chunk = b"80\r\n" + b"y" * 128 + b"\r\n"
        refused, closed = await send_streaming(port, head, chunk * 2048 + b"0\r\n\r\n")
    finally:
        await worker.stop()
    assert refused.status == 413
    assert refused.json()["error"] == "payload_too_large"
    assert closed


async def test_the_limit_itself_is_accepted() -> None:
    body = json.dumps({"title": "a" * 40}).encode()
    worker = KisoWorker(make_app(max_body_bytes=len(body)), "127.0.0.1", 0)
    port = await worker.start()
    try:
        accepted = await raw(port, post("/notes", body))
    finally:
        await worker.stop()
    assert accepted.status == 201


async def test_authentication_comes_before_the_size_check_and_no_continue_is_sent(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    head = (
        "POST /secured HTTP/1.1\r\nhost: t\r\ncontent-length: 4\r\n"
        "expect: 100-continue\r\n\r\n"
    )
    writer.write(head.encode())
    refused = await read_response(reader)  # a 100 would be read here instead
    assert (refused.status, refused.json()["error"]) == (401, "unauthorized")
    assert PROBE.entered == 0
    assert refused.header("connection") == "close"
    assert await eof(reader)
    writer.close()


async def test_continue_is_sent_once_the_request_is_authorized(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    body = b'{"title": "ok"}'
    head = (
        "POST /secured HTTP/1.1\r\nhost: t\r\ncontent-type: application/json\r\n"
        f"authorization: Bearer editor-token\r\ncontent-length: {len(body)}\r\n"
        "expect: 100-continue\r\n\r\n"
    )
    writer.write(head.encode())
    interim = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert interim.startswith(b"HTTP/1.1 100")
    writer.write(body)
    done = await read_response(reader)
    assert (done.status, done.json()) == (200, {"title": "ok"})
    writer.close()


async def test_an_oversized_declared_body_is_refused_without_a_continue() -> None:
    worker = KisoWorker(make_app(max_body_bytes=64), "127.0.0.1", 0)
    port = await worker.start()
    try:
        reader, writer = await open_connection(port)
        head = (
            "POST /notes HTTP/1.1\r\nhost: t\r\ncontent-length: 9999\r\n"
            "expect: 100-continue\r\n\r\n"
        )
        writer.write(head.encode())
        refused = await read_response(reader)
        writer.close()
    finally:
        await worker.stop()
    assert (refused.status, refused.json()["error"]) == (413, "payload_too_large")


async def test_a_stalled_body_is_a_408_before_the_request_timeout() -> None:
    worker = KisoWorker(
        make_app(request_timeout=5), "127.0.0.1", 0, body_read_timeout=0.3
    )
    port = await worker.start()
    try:
        reader, writer = await open_connection(port)
        writer.write(post("/notes", b'{"title": "slow"}')[:-6])
        stalled = await read_response(reader)
        assert await eof(reader)
        writer.close()
    finally:
        await worker.stop()
    assert (stalled.status, stalled.json()["error"]) == (408, "request_timeout")
    assert stalled.header("connection") == "close"


TLS_DIR = Path(__file__).parent / "tls"


async def test_the_worker_serves_https_with_its_own_certificate() -> None:
    worker = KisoWorker(
        make_app(),
        "127.0.0.1",
        0,
        tls_cert_file=str(TLS_DIR / "localhost-cert.pem"),
        tls_key_file=str(TLS_DIR / "localhost-key.pem"),
    )
    port = await worker.start()
    try:
        context = ssl.create_default_context(cafile=str(TLS_DIR / "localhost-cert.pem"))
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=context, server_hostname="localhost"
        )
        writer.write(b"GET /scheme HTTP/1.1\r\nhost: localhost\r\n\r\n")
        secure = await read_response(reader)
        writer.close()
        plain_reader, plain_writer = await open_connection(port)
        plain_writer.write(b"GET /scheme HTTP/1.1\r\nhost: localhost\r\n\r\n")
        refused = await asyncio.wait_for(plain_reader.read(100), 5)
        assert not refused.startswith(b"HTTP/")  # a TLS alert, never an answer
        plain_writer.close()
    finally:
        await worker.stop()
    assert (secure.status, secure.json()) == (200, {"scheme": "https"})


REAL_SCOPE_OF = kiso.scope_of
SCOPES: list[dict[str, Any]] = []


def recording_scope_of(*args: Any, **kwargs: Any) -> dict[str, Any]:
    scope = REAL_SCOPE_OF(*args, **kwargs)
    SCOPES.append(scope)
    return scope


async def curl_status(port: int, protocol: str, path: str = "/health") -> str:
    process = await asyncio.create_subprocess_exec(
        "curl",
        "-sk",
        protocol,
        "-o",
        "/dev/null",
        "-w",
        "%{http_version} %{http_code}",
        f"https://127.0.0.1:{port}{path}",
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(process.communicate(), 20)
    return out.decode()


def curl_speaks_http2() -> bool:
    curl = shutil.which("curl")
    if curl is None:
        return False
    version = subprocess.run(
        [curl, "--version"], capture_output=True, text=True, check=False
    )
    return "HTTP2" in version.stdout


@pytest.mark.skipif(not curl_speaks_http2(), reason="curl without HTTP/2")
async def test_http2_reaches_the_pipeline_with_http_version_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    SCOPES.clear()
    monkeypatch.setattr(kiso, "scope_of", recording_scope_of)
    worker = KisoWorker(
        make_app(),
        "127.0.0.1",
        0,
        tls_cert_file=str(TLS_DIR / "localhost-cert.pem"),
        tls_key_file=str(TLS_DIR / "localhost-key.pem"),
        http2=True,
    )
    port = await worker.start()
    try:
        over_h2 = await curl_status(port, "--http2", "/scheme")
        over_h1 = await curl_status(port, "--http1.1", "/scheme")
    finally:
        await worker.stop()
    assert (over_h2, over_h1) == ("2 200", "1.1 200")
    assert [(s["http_version"], s["scheme"]) for s in SCOPES] == [
        ("2", "https"),
        ("1.1", "https"),
    ]


async def test_a_client_that_stops_reading_frees_its_connection_slot() -> None:
    worker = KisoWorker(
        make_app(), "127.0.0.1", 0, max_connections=1, write_timeout=0.3
    )
    port = await worker.start()
    try:
        _, stalled = await open_connection(port)
        stalled.write(b"GET /big HTTP/1.1\r\nhost: t\r\n\r\n")
        served = await fetch(port, "GET", "/health")
        stalled.close()
    finally:
        await worker.stop()
    assert served.status == 200


async def test_a_body_timeout_above_the_request_timeout_is_clamped() -> None:
    worker = KisoWorker(
        make_app(request_timeout=1), "127.0.0.1", 0, body_read_timeout=60
    )
    port = await worker.start()
    try:
        reader, writer = await open_connection(port)
        writer.write(post("/notes", b'{"title": "slow"}')[:-6])
        stalled = await read_response(reader)
        writer.close()
    finally:
        await worker.stop()
    assert (stalled.status, stalled.json()["error"]) == (408, "request_timeout")


async def test_a_client_that_leaves_mid_body_does_not_reach_the_handler(
    served: tuple[KisoWorker, int],
) -> None:
    worker, port = served
    _, writer = await open_connection(port)
    writer.write(
        post(
            "/secured", b'{"title": "gone"}', "authorization: Bearer editor-token\r\n"
        )[:-5]
    )
    await writer.drain()
    writer.close()
    # The server may not have taken the request yet: wait for the permit to
    # appear and go away again before reading the counter.
    seen = False
    for _ in range(200):
        seen = seen or bool(worker.core.in_flight)
        if seen and not worker.core.in_flight and not worker.core.pending:
            break
        await asyncio.sleep(0.025)
    assert worker.core.in_flight == 0
    assert PROBE.entered == 0
    assert (await fetch(port, "GET", "/health")).status == 200


async def test_a_disconnected_body_is_the_asgi_invalid_body_error() -> None:
    failure = kiso.body_failure("disconnected")
    assert (failure.code, failure.message) == (  # type: ignore[attr-defined]
        "disconnected",
        "the client disconnected",
    )


async def test_malformed_body_framing_keeps_the_disconnected_code() -> None:
    failure = kiso.body_failure("malformed")
    assert (failure.code, failure.message) == (  # type: ignore[attr-defined]
        "disconnected",
        "the request body is malformed",
    )


async def test_a_bare_lf_in_a_header_is_refused_before_the_body(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    attack = (
        b"POST /secured HTTP/1.1\r\nhost: t\r\nx: a\n"
        b"authorization: Bearer editor-token\r\ncontent-length: 5\r\n\r\nabcde"
    )
    reader, writer = await open_connection(port)
    writer.write(attack)
    refused = await read_response(reader)
    assert refused.status == 400
    assert await eof(reader)
    writer.close()
    assert PROBE.entered == 0


async def test_content_length_with_transfer_encoding_is_refused(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    smuggle = (
        b"POST /notes HTTP/1.1\r\nhost: t\r\ncontent-length: 4\r\n"
        b"transfer-encoding: chunked\r\n\r\n0\r\n\r\n"
        b"GET /health HTTP/1.1\r\nhost: t\r\n\r\n"
    )
    reader, writer = await open_connection(port)
    writer.write(smuggle)
    refused = await read_response(reader)
    assert refused.status == 400
    assert await eof(reader)
    writer.close()


async def test_a_request_pipelined_after_a_refused_body_is_never_answered(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    body = b'{"title": "x"}'
    head = f"POST /secured HTTP/1.1\r\nhost: t\r\ncontent-length: {len(body)}\r\n\r\n"
    writer.write(head.encode() + body + b"GET /health HTTP/1.1\r\nhost: t\r\n\r\n")
    refused = await read_response(reader)
    assert refused.status == 401
    assert refused.header("connection") == "close"
    assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()


async def test_oversized_query_and_headers_get_the_json_refusals() -> None:
    worker = KisoWorker(
        make_app(max_query_bytes=64, max_header_bytes=2048), "127.0.0.1", 0
    )
    port = await worker.start()
    try:
        long_query = await fetch(port, "GET", "/health?" + "q=1&" * 40)
        assert (long_query.status, long_query.json()["error"]) == (414, "uri_too_long")
        big_header = await fetch(port, "GET", "/health", f"x: {'a' * 3000}\r\n")
        assert big_header.status == 431
        assert big_header.json()["error"] == "headers_too_large"
    finally:
        await worker.stop()


async def test_the_in_flight_bound_answers_503_without_running_the_handler() -> None:
    worker = KisoWorker(
        make_app(sync_workers=1, sync_queue=0), "127.0.0.1", 0
    )  # in-flight bound 4
    port = await worker.start()
    try:
        held = [await open_connection(port) for _ in range(4)]
        for _, writer in held:
            writer.write(b"GET /block HTTP/1.1\r\nhost: t\r\n\r\n")
        while PROBE.entered < 4:
            await asyncio.sleep(0.01)
        busy = await fetch(port, "GET", "/block")
        assert busy.status == 503
        assert busy.json()["error"] == "overloaded"
        assert busy.header("retry-after") == "1"
        assert PROBE.entered == 4
        PROBE.gate.set()
        for reader, _ in held:
            assert (await read_response(reader)).status == 200
        assert worker.core.in_flight == 0
        for _, writer in held:
            writer.close()
    finally:
        await worker.stop()


async def test_the_loop_drains_until_the_queue_is_empty(
    served: tuple[KisoWorker, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, port = served
    monkeypatch.setattr(kiso, "DRAIN_BATCH", 2)
    answers = await asyncio.gather(*(fetch(port, "GET", "/health") for _ in range(25)))
    assert [a.status for a in answers] == [200] * 25


async def test_stop_lets_a_running_request_finish() -> None:
    worker = KisoWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    reader, writer = await open_connection(port)
    writer.write(b"GET /block HTTP/1.1\r\nhost: t\r\n\r\n")
    while PROBE.entered < 1:
        await asyncio.sleep(0.01)
    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0.2)
    assert not stopping.done()
    with pytest.raises(ConnectionRefusedError):
        await asyncio.wait_for(open_connection(port), 2)
    PROBE.gate.set()
    answer = await read_response(reader)
    assert (answer.status, answer.json()) == (200, {"released": "yes"})
    assert await eof(reader)
    assert await stopping == 0
    assert PROBE.shutdown_ran
    assert worker.core.in_flight == 0
    writer.close()


async def test_stop_aborts_what_outlives_the_shutdown_timeout() -> None:
    worker = KisoWorker(make_app(shutdown_timeout=0.3), "127.0.0.1", 0)
    port = await worker.start()
    reader, writer = await open_connection(port)
    writer.write(b"GET /block HTTP/1.1\r\nhost: t\r\n\r\n")
    while PROBE.entered < 1:
        await asyncio.sleep(0.01)
    assert await worker.stop() == 1
    assert await eof(reader)
    assert PROBE.cancelled == 1
    assert PROBE.shutdown_ran
    writer.close()


async def test_a_failing_startup_binds_nothing() -> None:
    app = Hinoki(include=[health], on_startup=[fail_startup])
    port = free_port()
    worker = KisoWorker(app, "127.0.0.1", port)
    with pytest.raises(RuntimeError, match="startup failed"):
        await worker.start()
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_a_busy_port_fails_and_closes_the_lifespan() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        worker = KisoWorker(make_app(), "127.0.0.1", taken.getsockname()[1])
        with pytest.raises(OSError, match="in use"):
            await worker.start()
    assert PROBE.shutdown_ran


def spawn_server(port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hinoki.runtime.kiso",
            "tests.kisoserverapp:app",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        text=True,
    )


def blocking_get(port: int, path: str) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nhost: t\r\nconnection: close\r\n\r\n".encode()
        )
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


async def test_sigterm_stops_the_process_after_the_running_request() -> None:
    port = free_port()
    process = spawn_server(port)
    try:
        assert process.stdout is not None
        line = await asyncio.to_thread(process.stdout.readline)
        assert line.startswith("listening on"), line
        health_answer = await asyncio.to_thread(blocking_get, port, "/health")
        assert health_answer.startswith(b"HTTP/1.1 200")
        slow = asyncio.create_task(asyncio.to_thread(blocking_get, port, "/slow"))
        await asyncio.sleep(0.2)
        process.send_signal(signal.SIGTERM)
        answer = await slow
        assert answer.startswith(b"HTTP/1.1 200")
        assert answer.endswith(b'{"slow":"done"}')
        assert await asyncio.to_thread(process.wait, 10) == 0
    finally:
        process.kill()
        process.wait()


async def raw(port: int, request: bytes) -> Received:
    reader, writer = await open_connection(port)
    writer.write(request)
    received = await read_response(reader)
    writer.close()
    return received


async def test_a_client_that_closes_its_write_side_still_gets_the_answer(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    writer.write(b"GET /block HTTP/1.1\r\nhost: t\r\n\r\n")
    writer.write_eof()
    while PROBE.entered < 1:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)  # the server has seen the EOF
    PROBE.gate.set()
    answer = await read_response(reader)
    assert (answer.status, answer.json()) == (200, {"released": "yes"})
    writer.close()


@pytest.mark.parametrize(
    "head", ["", "host: a\r\nhost: b\r\n"], ids=["missing", "repeated"]
)
async def test_http_1_1_needs_exactly_one_host(
    served: tuple[KisoWorker, int], head: str
) -> None:
    _, port = served
    refused = await raw(port, f"GET /health HTTP/1.1\r\n{head}\r\n".encode())
    assert refused.status == 400
    assert refused.json() == {
        "error": "bad_host",
        "request_id": refused.header("x-request-id"),
    }


async def test_an_absolute_form_target_makes_its_authority_the_host(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    answer = await raw(
        port, b"GET http://evil.example/host HTTP/1.1\r\nhost: other\r\n\r\n"
    )
    assert answer.json() == {"host": "evil.example"}


async def test_http_1_0_without_host_falls_back_to_the_listening_address(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    answer = await raw(port, b"GET /host HTTP/1.0\r\n\r\n")
    assert answer.json() == {"host": f"127.0.0.1:{port}"}


class FlakyCore:
    """The worker's core, with ``drain`` failing once."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.failed = False

    def drain(self, limit: int) -> Any:
        if not self.failed:
            self.failed = True
            raise RuntimeError("drain failed")
        return self.inner.drain(limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def test_a_failing_drain_is_retried_instead_of_stranding_the_queue(
    served: tuple[KisoWorker, int],
) -> None:
    worker, port = served
    worker.core = FlakyCore(worker.core)  # type: ignore[assignment]
    assert (await fetch(port, "GET", "/health")).status == 200


class FailingSpawn:
    """Stands in for ``KisoWorker._spawn``: fails the first request, then
    restores the real method."""

    def __init__(self, worker: KisoWorker, monkeypatch: pytest.MonkeyPatch) -> None:
        self.worker = worker
        self.monkeypatch = monkeypatch
        self.original = worker._spawn

    def __call__(self, request: Any) -> None:
        self.monkeypatch.setattr(self.worker, "_spawn", self.original)
        raise RuntimeError("no task")


async def test_a_request_that_cannot_be_started_gets_a_500_and_the_rest_flow(
    served: tuple[KisoWorker, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, port = served
    monkeypatch.setattr(worker, "_spawn", FailingSpawn(worker, monkeypatch))
    failed = await fetch(port, "GET", "/health")
    assert failed.status == 500
    assert failed.json() == {
        "error": "internal_error",
        "request_id": failed.header("x-request-id"),
    }
    assert (await fetch(port, "GET", "/health")).status == 200


class CountingWake:
    """A wake callback that counts its calls."""

    def __init__(self, worker: KisoWorker) -> None:
        self.worker = worker
        self.calls = 0

    def __call__(self, server: Any) -> None:
        self.calls += 1
        self.worker._wake(server)


async def test_the_wake_reader_is_removed_when_the_server_ends() -> None:
    worker = KisoWorker(make_app(), "127.0.0.1", 0)
    await worker.start()
    server = worker.server
    assert server is not None
    # Replace the reader with a counting one, then end the server without stop().
    counting = CountingWake(worker)
    loop = asyncio.get_running_loop()
    loop.remove_reader(server.wake_fd)
    loop.add_reader(server.wake_fd, counting, server)
    try:
        await server.shutdown()
        await asyncio.sleep(0.3)
        assert counting.calls <= 2
    finally:
        await worker.stop()


async def test_a_connection_limit_beyond_the_semaphore_is_a_value_error() -> None:
    worker = KisoWorker(make_app(), "127.0.0.1", 0, max_connections=2**62)
    with pytest.raises(ValueError, match="max_connections"):
        await worker.start()
    assert PROBE.shutdown_ran


class BrokenStop:
    async def wait(self) -> None:
        raise RuntimeError("interrupted")


async def test_run_stops_the_server_when_it_fails_after_start() -> None:
    worker = KisoWorker(make_app(), "127.0.0.1", 0)

    worker.stopping = BrokenStop()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="interrupted"):
        await worker.run()
    assert worker.server is None
    assert PROBE.shutdown_ran


BARE_LF = b" HTTP/1.1\r\nhost: t\r\nx: a\ncontent-length: 5\r\n\r\nhello"
CL_AND_TE = (
    b" HTTP/1.1\r\nhost: t\r\ncontent-length: 4\r\ntransfer-encoding: chunked\r\n\r\n"
    b"0\r\n\r\nGET /health HTTP/1.1\r\nhost: t\r\n\r\n"
)


@pytest.mark.parametrize("size", [41_000, 45_000, 48_000])
@pytest.mark.parametrize("tail", [BARE_LF, CL_AND_TE], ids=["bare_lf", "cl_te"])
async def test_an_oversized_head_cannot_hide_smuggling(
    served: tuple[KisoWorker, int], size: int, tail: bytes
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    writer.write(b"POST /secured/" + b"a" * size + tail)  # one write
    answers = []
    try:
        while True:
            answers.append(await read_response(reader))
    except (asyncio.IncompleteReadError, ConnectionError):
        pass  # the connection ended
    writer.close()
    assert [a.status for a in answers] in ([], [400], [431])
    assert PROBE.entered == 0


async def test_a_head_under_the_limits_still_works(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    ok = await fetch(port, "GET", "/health", f"x: {'a' * 30_000}\r\n")
    assert ok.status == 200
    plan = await fetch(port, "GET", "/health", f"x: {'a' * 33_000}\r\n")
    assert plan.status == 431
    assert plan.json()["error"] == "headers_too_large"


async def test_a_refused_head_is_answered_before_the_client_stops_sending(
    served: tuple[KisoWorker, int],
) -> None:
    _, port = served
    head = b"POST /notes HTTP/1.1\r\nhost: t\r\nx: a\ncontent-length: 262144\r\n\r\n"
    refused, closed = await send_streaming(port, head, b"x" * 262_144)
    assert refused.status == 400
    assert closed


async def test_malformed_class_response_headers_answer_500_not_503() -> None:
    class Items(API):
        response_headers: ClassVar[Any] = [["x-a", "b"]]

        @http.get("/items")
        def list(self) -> dict[str, int]:
            return {"n": 1}

    worker = KisoWorker(Hinoki(include=[Items]), "127.0.0.1", 0)
    port = await worker.start()
    try:
        assert (await fetch(port, "GET", "/items")).status == 500
    finally:
        await worker.stop()
