"""An in-process client for tests: drives the application through the ASGI
lifespan and http protocols, with no sockets and no server process.

    async with ASGIClient(create_app()) as client:
        reply = await client.post("/tasks", json={"title": "x"}, token="abc")
        assert reply.status == 201

A sync twin, ``SyncClient``, exists for tests without ``async``. The lifespan
runs, so a configured ``DatabaseModule`` opens its pool: point the
app at a disposable database. ``reply.body`` is decoded JSON when the response
is JSON, else text.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from hinoki.app import HinokiAPI


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: dict[str, str]
    body: Any


class Feed:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __call__(self) -> dict[str, Any]:
        return {"type": "http.request", "body": self.body, "more_body": False}


class ASGIClient:
    """Use as an async context manager so startup and shutdown always run."""

    def __init__(self, app: HinokiAPI) -> None:
        self.adapter = app.asgi()
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.lifespan: asyncio.Task[None] | None = None

    async def __aenter__(self) -> ASGIClient:
        self.lifespan = asyncio.create_task(
            self.adapter({"type": "lifespan"}, self.inbox.get, self.outbox.put)
        )
        await self.inbox.put({"type": "lifespan.startup"})
        started = await self.outbox.get()
        if started["type"] != "lifespan.startup.complete":
            await self.lifespan
            raise RuntimeError(f"startup failed: {started.get('message')}")
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self.lifespan is not None:
            await self.inbox.put({"type": "lifespan.shutdown"})
            await self.outbox.get()
            await self.lifespan

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        body: bytes = b"",
        query: str = "",
        headers: dict[str, str] | None = None,
        token: str | None = None,
    ) -> Response:
        """One request. ``token`` sends ``Authorization: Bearer <token>``;
        ``json`` sends a JSON body with its content type."""
        sent = {k.lower(): v for k, v in (headers or {}).items()}
        if token is not None:
            sent["authorization"] = f"Bearer {token}"
        if json is not None:
            body = _dumps(json)
            sent.setdefault("content-type", "application/json")
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode(),
            "headers": [(k.encode(), v.encode()) for k, v in sent.items()],
        }
        sink: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sink.append(message)

        await self.adapter(scope, Feed(body), send)
        start = next(m for m in sink if m["type"] == "http.response.start")
        raw = b"".join(m.get("body", b"") for m in sink if m["type"].endswith("body"))
        reply_headers = {k.decode(): v.decode() for k, v in start["headers"]}
        text: Any = raw.decode("utf-8", "replace") or None
        if raw and reply_headers.get("content-type", "").endswith("json"):
            text = _loads(raw)
        return Response(start["status"], reply_headers, text)

    async def get(self, path: str, **options: Any) -> Response:
        return await self.request("GET", path, **options)

    async def post(self, path: str, **options: Any) -> Response:
        return await self.request("POST", path, **options)

    async def patch(self, path: str, **options: Any) -> Response:
        return await self.request("PATCH", path, **options)

    async def delete(self, path: str, **options: Any) -> Response:
        return await self.request("DELETE", path, **options)


def _dumps(value: Any) -> bytes:
    return json.dumps(value).encode()


def _loads(raw: bytes) -> Any:
    return json.loads(raw)


class SyncClient:
    """The same client for tests written without ``async``: the application runs
    on its own event loop in a background thread and every call blocks until the
    answer arrives (at most ``timeout`` seconds).

        with SyncClient(create_app()) as client:
            assert client.get("/health").status == 200

    Use it only inside the ``with`` block. Sync model calls (``Model.get``,
    ``create``) work inside the application's sync handlers, not in the test
    body itself: seed and inspect data with an async fixture or your own
    connection."""

    def __init__(self, app: HinokiAPI, timeout: float = 60.0) -> None:
        self.inner = ASGIClient(app)
        self.timeout = timeout
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="hinoki-test-loop", daemon=True
        )
        self.state = "new"  # new -> open -> closed

    def __enter__(self) -> SyncClient:
        if self.state != "new":
            raise RuntimeError("a SyncClient can be entered once")
        self.thread.start()
        try:
            start = self.inner.__aenter__()
            asyncio.run_coroutine_threadsafe(start, self.loop).result(self.timeout)
        except BaseException:
            self.stop()
            raise
        self.state = "open"
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self.state != "open":
            return  # never entered, or already closed: nothing to stop twice
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.inner.__aexit__(kind, error, trace), self.loop
            )
            future.result(self.timeout)
        finally:
            self.stop()

    def stop(self) -> None:
        self.state = "closed"
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        if not self.thread.is_alive():
            self.loop.close()

    def request(self, method: str, path: str, **options: Any) -> Response:
        if self.state != "open":
            raise RuntimeError("use the SyncClient inside its `with` block")
        future = asyncio.run_coroutine_threadsafe(
            self.inner.request(method, path, **options), self.loop
        )
        return future.result(self.timeout)

    def get(self, path: str, **options: Any) -> Response:
        return self.request("GET", path, **options)

    def post(self, path: str, **options: Any) -> Response:
        return self.request("POST", path, **options)

    def patch(self, path: str, **options: Any) -> Response:
        return self.request("PATCH", path, **options)

    def delete(self, path: str, **options: Any) -> Response:
        return self.request("DELETE", path, **options)
