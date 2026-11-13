"""Run one HTTP request against the application in-process (no sockets).

Used by the development MCP tool ``run_scenario``, which is off unless the owner
starts the server with ``--scenarios read`` (GET/HEAD/OPTIONS only) or
``--scenarios write`` (any method). The application's lifespan runs, so a
configured database is opened and handlers really execute: point it at a
disposable database, never at real data.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from hinoki.app import Hinoki

SAFE = frozenset({"GET", "HEAD", "OPTIONS"})
METHODS = SAFE | {"POST", "PUT", "PATCH", "DELETE"}
MAX_BODY = 20_000
TIMEOUT = 15
TOKEN = frozenset(
    "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)
FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding"})


class ScenarioError(ValueError):
    """The request cannot be run; the message says why."""


class Feed:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __call__(self) -> dict[str, Any]:
        return {"type": "http.request", "body": self.body, "more_body": False}


def has_control(text: str) -> bool:
    return any(c in text for c in "\r\n\0")


def check_request(method: Any, path: Any, mode: str) -> None:
    if not isinstance(method, str) or method not in METHODS:
        raise ScenarioError(f"method must be one of {sorted(METHODS)}")
    if method not in SAFE and mode != "write":
        raise ScenarioError(
            f"{method} changes state; start the server with --scenarios write "
            "against a disposable database to allow it"
        )
    if not isinstance(path, str) or not path.startswith("/") or "?" in path:
        raise ScenarioError("path starts with '/' and has no query (use 'query')")
    if path.startswith("//") or has_control(path) or " " in path:
        raise ScenarioError("path is not a plain URL path")


def build_scope(
    method: Any, path: Any, headers: Any, query: Any, mode: str
) -> dict[str, Any]:
    check_request(method, path, mode)
    if headers is None:
        headers = {}
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        raise ScenarioError("headers is an object of strings")
    for name, value in headers.items():
        if (
            name.lower() in FORBIDDEN_HEADERS
            or not name
            or not set(name) <= TOKEN
            or has_control(value)
        ):
            raise ScenarioError(f"header {name!r} is not allowed")
    if query is not None and (
        not isinstance(query, str) or has_control(query) or "#" in query
    ):
        raise ScenarioError("query is a plain string such as 'page=2'")
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": (query or "").encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }


async def next_message(
    outbox: asyncio.Queue[dict[str, Any]], lifespan: asyncio.Task[None]
) -> dict[str, Any] | None:
    """The lifespan's next message, or ``None`` if its task ended first."""
    getter = asyncio.ensure_future(outbox.get())
    await asyncio.wait({getter, lifespan}, return_when=asyncio.FIRST_COMPLETED)
    if getter.done():
        return getter.result()
    getter.cancel()
    return None


async def drive(app: Hinoki, scope: dict[str, Any], body: bytes) -> dict[str, Any]:
    adapter = app.asgi()
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    lifespan = asyncio.create_task(adapter({"type": "lifespan"}, inbox.get, outbox.put))
    started = False
    try:
        await inbox.put({"type": "lifespan.startup"})
        first = await next_message(outbox, lifespan)
        if first is None or first["type"] != "lifespan.startup.complete":
            # The reason can hold connection strings: it stays on the server's stderr.
            raise ScenarioError("the application failed to start; see the server log")
        started = True
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await adapter(scope, Feed(body), send)
    finally:
        if started and not lifespan.done():
            await inbox.put({"type": "lifespan.shutdown"})
            await next_message(outbox, lifespan)
        if not lifespan.done():
            lifespan.cancel()
        await asyncio.gather(lifespan, return_exceptions=True)
    start = next(m for m in sent if m["type"] == "http.response.start")
    chunks = [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]
    raw = b"".join(chunks)
    return {
        "status": start["status"],
        "headers": {k.decode(): v.decode() for k, v in start["headers"]},
        "body": raw[:MAX_BODY].decode("utf-8", "replace"),
        "truncated": len(raw) > MAX_BODY,
    }


async def drive_with_timeout(
    app: Hinoki, scope: dict[str, Any], body: bytes
) -> dict[str, Any]:
    try:
        async with asyncio.timeout(TIMEOUT):
            return await drive(app, scope, body)
    except TimeoutError:
        raise ScenarioError(
            f"the scenario took longer than {TIMEOUT} seconds"
        ) from None


def run_scenario(app: Hinoki, args: dict[str, Any], mode: str) -> dict[str, Any]:
    scope = build_scope(
        args.get("method", "GET"),
        args.get("path"),
        args.get("headers"),
        args.get("query"),
        mode,
    )
    json_body = args.get("json")
    headers = dict(args.get("headers") or {})
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        if not any(k.lower() == "content-type" for k in headers):
            scope["headers"].append((b"content-type", b"application/json"))
    result = asyncio.run(drive_with_timeout(app, scope, body))
    kind = result["headers"].get("content-type", "")
    if kind.endswith("json") and not result["truncated"]:
        with contextlib.suppress(ValueError):
            result["json"] = json.loads(result["body"])
    return result
