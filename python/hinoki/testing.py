"""An in-process client for tests: drives the application through the ASGI
lifespan and http protocols, with no sockets and no server process.

    async with ASGIClient(create_app()) as client:
        reply = await client.post("/tasks", json={"title": "x"}, token="abc")
        assert reply.status == 201

The lifespan runs, so a configured ``DatabaseModule`` opens its pool: point the
app at a disposable database. ``reply.body`` is decoded JSON when the response
is JSON, else text.
"""

from __future__ import annotations

import asyncio
import json
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
