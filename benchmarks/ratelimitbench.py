"""In-process cost of the built-in rate limiter on one GET through the ASGI adapter.

Three routes that do the same trivial work: no limit, ``MemoryLimits`` and
``DatabaseLimits`` on a SQLite file in the working directory. The limit is far
above the request count, so every request is admitted. No server, no network:
it answers "what does one limiter decision add to the request path", and says
nothing about a deployment's database.
Run: ``uv run --locked python benchmarks/ratelimitbench.py``.
"""

import asyncio
import json
import shutil
import statistics
import time
from pathlib import Path
from typing import Any

from hinoki import Hinoki, RateLimit, http
from hinoki import ratelimit_store as store
from hinoki.db import Database, DatabaseModule
from hinoki.orm import migrator
from hinoki.ratelimit import DatabaseLimits, MemoryLimits

SAMPLES = 7
LOOPS = 5000
HUGE = 100_000_000
WORK = Path("benchmarks/.ratelimitbench")


@http.get("/plain")
async def plain() -> dict[str, bool]:
    return {"ok": True}


@http.get("/memory", rate_limit=RateLimit(HUGE, 3600, storage=MemoryLimits()))
async def memory() -> dict[str, bool]:
    return {"ok": True}


@http.get("/database", rate_limit=RateLimit(HUGE, 3600, storage=DatabaseLimits()))
async def database() -> dict[str, bool]:
    return {"ok": True}


def scope(path: str) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "scheme": "http",
        "query_string": b"",
        "headers": [(b"host", b"example.test")],
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", 8000),
    }


async def receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def discard(message: dict[str, Any]) -> None:
    del message


async def sample(adapter: Any, request_scope: dict[str, Any]) -> float:
    start = time.perf_counter()
    for _ in range(LOOPS):
        await adapter(request_scope, receive, discard)
    return (time.perf_counter() - start) / LOOPS * 1e6


async def prepare(file: Path) -> None:
    connected = await Database.connect(f"sqlite://{file}")
    migrations = Path(store.__file__).parent / "migrations"
    await migrator.apply(connected, migrations, namespace="ratelimit")
    connected.close()


async def main() -> None:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    file = WORK / "limits.sqlite3"
    await prepare(file)
    app = Hinoki(
        include=[plain, memory, database],
        modules=[DatabaseModule(f"sqlite://{file}", max_size=4)],
    )
    adapter = app.asgi()
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    lifespan = asyncio.create_task(adapter({"type": "lifespan"}, inbox.get, outbox.put))
    await inbox.put({"type": "lifespan.startup"})
    await outbox.get()
    result: dict[str, Any] = {}
    for name in ("plain", "memory", "database"):
        request_scope = scope(f"/{name}")
        await sample(adapter, request_scope)  # warm-up
        data = [await sample(adapter, request_scope) for _ in range(SAMPLES)]
        cv = statistics.stdev(data) / statistics.mean(data) * 100
        result[name] = {
            "median_us": round(statistics.median(data), 3),
            "cv_pct": round(cv, 2),
        }
    await inbox.put({"type": "lifespan.shutdown"})
    await outbox.get()
    await lifespan
    shutil.rmtree(WORK, ignore_errors=True)
    print(json.dumps(result))


asyncio.run(main())
