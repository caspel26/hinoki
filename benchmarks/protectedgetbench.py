"""In-process cost of one protected GET through the ASGI adapter.

Routing, bearer authentication, query parsing, building the request, binding
and a trivial handler, with no server and no network. It answers "did a change
to the per-request path cost or save anything", not how fast Hinoki serves.
Run: ``uv run --locked python benchmarks/protectedgetbench.py``.
"""

import asyncio
import json
import statistics
import time
from typing import Any

from hinoki import Access, BearerAuth, Caller, Hinoki, Request, http

SAMPLES = 7
LOOPS = 20000
USER = Caller(id="u", user="u", permissions=frozenset())


class Auth(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        return USER if token == "t" else None  # noqa: S105 - fixed benchmark token


@http.get("/items/{item_id}", access=Access(auth=Auth()))
async def item(request: Request, item_id: int, page: int = 1) -> dict[str, int]:
    return {"id": item_id, "page": page}


SCOPE = {
    "type": "http",
    "method": "GET",
    "path": "/items/1",
    "raw_path": b"/items/1",
    "scheme": "http",
    "query_string": b"expand=author&page=2",
    "headers": [
        (b"host", b"example.test"),
        (b"user-agent", b"bench/1.0"),
        (b"accept", b"application/json"),
        (b"accept-encoding", b"gzip"),
        (b"authorization", b"Bearer t"),
        (b"cookie", b"session=abc123; theme=dark"),
    ],
    "client": ("127.0.0.1", 5000),
    "server": ("127.0.0.1", 8000),
}


async def receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def discard(message: dict[str, Any]) -> None:
    del message


class Collector:
    """An ASGI ``send`` that keeps the messages."""

    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self.seen = seen

    async def __call__(self, message: dict[str, Any]) -> None:
        self.seen.append(message)


async def sample(adapter: Any) -> float:
    start = time.perf_counter()
    for _ in range(LOOPS):
        await adapter(SCOPE, receive, discard)
    return (time.perf_counter() - start) / LOOPS * 1e6


async def main() -> None:
    app = Hinoki()
    app.include(item)
    adapter = app.asgi()
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    lifespan = asyncio.create_task(adapter({"type": "lifespan"}, inbox.get, outbox.put))
    await inbox.put({"type": "lifespan.startup"})
    await outbox.get()
    seen: list[dict[str, Any]] = []
    await adapter(SCOPE, receive, Collector(seen))
    assert seen[0]["status"] == 200  # noqa: PLR2004 - measure only a working route
    await sample(adapter)  # warm-up
    data = [await sample(adapter) for _ in range(SAMPLES)]
    await inbox.put({"type": "lifespan.shutdown"})
    await outbox.get()
    await lifespan
    cv = statistics.stdev(data) / statistics.mean(data) * 100
    print(
        json.dumps(
            {
                "median_us": round(statistics.median(data), 3),
                "cv_pct": round(cv, 2),
                "samples_us": [round(value, 3) for value in data],
            }
        )
    )


asyncio.run(main())
