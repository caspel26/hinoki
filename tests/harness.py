"""Drives an ASGI application in-process through the lifespan and http protocols."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from hinoki import HinokiAPI
from hinoki.runtime.asgi import ASGIAdapter


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: Any


class Client:
    """Drives one adapter through the ASGI lifespan and http protocols."""

    def __init__(self, adapter: ASGIAdapter) -> None:
        self.adapter = adapter
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def lifespan_event(self, event: str) -> str:
        if self.task is None:
            scope = {"type": "lifespan"}
            self.task = asyncio.create_task(
                self.adapter(scope, self.inbox.get, self.outbox.put)
            )
        await self.inbox.put({"type": f"lifespan.{event}"})
        message = await self.outbox.get()
        return str(message["type"])

    async def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        raw_path: bytes | None = None,
        query: str = "",
        content_type: str | None = None,
    ) -> Reply:
        incoming = {"type": "http.request", "body": body, "more_body": False}
        scope: dict[str, Any] = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode(),
            "headers": [(b"content-type", content_type.encode())]
            if content_type
            else [],
        }
        if raw_path is not None:
            scope["raw_path"] = raw_path
        sink = Sink()
        await self.adapter(scope, Feed(incoming), sink)
        start, end = sink.sent
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        raw = end["body"]
        return Reply(start["status"], headers, json.loads(raw) if raw else None)


class Feed:
    """``receive`` callable that always yields one message."""

    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message

    async def __call__(self) -> dict[str, Any]:
        return self.message


class Sink:
    """``send`` callable that records every message."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


class Pool:
    """Starts clients and shuts every one of them down after the test."""

    def __init__(self) -> None:
        self.clients: list[Client] = []

    async def start(self, app: HinokiAPI) -> Client:
        client = Client(app.asgi())
        self.clients.append(client)
        assert await client.lifespan_event("startup") == "lifespan.startup.complete"
        return client
