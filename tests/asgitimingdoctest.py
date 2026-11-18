"""The ASGI timing wrapper of the serving guide works as written."""

import time
from typing import Any

from hinoki import Hinoki, http


class Timing:
    """Report each request's method, status and duration to ``record``."""

    def __init__(self, app: Any, record: Any) -> None:
        self.app, self.record = app, record

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started, status = time.monotonic(), None

        async def watch(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            self.record(scope["method"], status, time.monotonic() - started)


@http.get("/ping")
async def ping() -> dict[str, str]:
    return {"ok": "yes"}


async def run(app: Any, method: str, path: str) -> int:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [],
        },
        receive,
        send,
    )
    return int(next(m["status"] for m in sent if m["type"] == "http.response.start"))


async def test_the_wrapper_reports_method_status_and_a_duration() -> None:
    seen: list[tuple[str, int | None, float]] = []
    inner = Hinoki(include=[ping]).asgi()
    app = Timing(inner, lambda *args: seen.append(args))
    lifespan: list[dict[str, Any]] = []

    async def receive_startup() -> dict[str, Any]:
        return {"type": "lifespan.startup"}

    async def send_lifespan(message: dict[str, Any]) -> None:
        lifespan.append(message)

    import asyncio  # noqa: PLC0415 - the lifespan needs its own task

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(inner({"type": "lifespan"}, inbox.get, send_lifespan))
    await inbox.put({"type": "lifespan.startup"})
    while not lifespan:
        await asyncio.sleep(0)

    assert await run(app, "GET", "/ping") == 200
    assert await run(app, "GET", "/missing") == 404

    await inbox.put({"type": "lifespan.shutdown"})
    await task
    assert [(method, status) for method, status, _ in seen] == [
        ("GET", 200),
        ("GET", 404),
    ]
    assert all(duration >= 0 for _, _, duration in seen)
