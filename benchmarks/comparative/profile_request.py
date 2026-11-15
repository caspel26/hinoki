"""Where the time of one Hinoki request goes, with a real PostgreSQL.

One process, no HTTP server and no network between client and app: the ASGI
adapter is called directly, as Granian would, against the benchmark service
(``hinoki_app.py``) plus one bearer-protected route and a larger page.

For each scenario it reports, per request:

* ``wall``: elapsed time, one request at a time;
* ``py_cpu``: CPU of the thread that runs the event loop (Python code plus the
  Rust calls made synchronously on it: routing, parsing, validation);
* ``other_cpu``: CPU of every other thread (the Rust ORM runtime and driver);
* ``wait``: what is left, i.e. waiting for PostgreSQL;
* ``rps_64``, ``py_cpu_64``, ``other_cpu_64``: the same process with 64 requests in
  flight. One request at a time lets the CPU idle between steps, which on some hosts
  lowers the clock and inflates the serial CPU figures; the 64-wide figures are the
  ones that bound a busy worker (``1e6 / py_cpu_64`` requests per second).

``--profile`` adds a cProfile split of the event-loop thread. cProfile inflates
Python call costs; use it to locate hot spots, not to quote microseconds.

    PROFILE_DATABASE_URL=postgres://... uv run --locked python profile_request.py
"""

import argparse
import asyncio
import cProfile
import json
import os
import platform
import pstats
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import hinoki_app as base
import psycopg
from seed import author_id, post_id

from hinoki import Access, BearerAuth, Caller, Hinoki, Request, http

DB = os.environ.get(
    "PROFILE_DATABASE_URL", "postgres://postgres:bench-only@127.0.0.1:54402/bench"
)
os.environ.setdefault("BENCH_DATABASE__URL", DB)
os.environ.setdefault("BENCH_DATABASE__MAX_SIZE", "10")

USER = Caller(id="u", user="u", permissions=frozenset())
POST = post_id(1)  # published: 1 % 5 != 0
NEW_BODY = json.dumps(
    {"author_id": str(author_id(0)), "title": "profile-new", "body": "Created."}
).encode()


class Auth(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        return USER if token == "t" else None


@http.get("/secure/posts/{post_id}", access=Access(auth=Auth()))
async def secure_one(request: Request, post_id: UUID) -> base.PostOut:
    del request
    post = (
        await base.Post.filter(id=post_id, published=True)
        .select_related("author")
        .aget()
    )
    return base.with_author(post)


app = Hinoki(
    settings=base.BenchSettings,
    include=[base.health, base.listing, base.one, base.create, secure_one],
    modules=[base.DatabaseModule],
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)


def scope(
    method: str, path: str, query: str = "", token: bool = False
) -> dict[str, Any]:
    headers = [
        (b"host", b"127.0.0.1:8801"),
        (b"user-agent", b"oha/1.17"),
        (b"accept", b"*/*"),
        (b"content-type", b"application/json"),
    ]
    if token:
        headers.append((b"authorization", b"Bearer t"))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "scheme": "http",
        "query_string": query.encode(),
        "headers": headers,
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", 8801),
    }


# name -> (scope, body, expected status, requests per block)
SCENARIOS: dict[str, tuple[dict[str, Any], bytes, int, int]] = {
    "health": (scope("GET", "/health"), b"", 200, 20000),
    "detail": (scope("GET", f"/posts/{POST}"), b"", 200, 2000),
    "detail_protected": (
        scope("GET", f"/secure/posts/{POST}", token=True),
        b"",
        200,
        2000,
    ),
    "list_20": (scope("GET", "/posts", "page=1&page_size=20"), b"", 200, 1500),
    "list_100": (scope("GET", "/posts", "page=1&page_size=100"), b"", 200, 600),
    "create": (scope("POST", "/posts"), NEW_BODY, 201, 1000),
}


def request_pair(body: bytes) -> tuple[Callable[[], Awaitable[dict[str, Any]]], Any]:
    state = {"sent": False}

    async def receive() -> dict[str, Any]:
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    status: list[int] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    return receive, (send, status)


async def one_request(adapter: Any, name: str) -> int:
    sc, body, _, _ = SCENARIOS[name]
    receive, (send, status) = request_pair(body)
    await adapter(sc, receive, send)
    return status[0]


async def serial_block(adapter: Any, name: str, count: int) -> dict[str, float]:
    wall0, thread0, proc0 = time.perf_counter(), time.thread_time(), time.process_time()
    for _ in range(count):
        await one_request(adapter, name)
    wall = time.perf_counter() - wall0
    thread = time.thread_time() - thread0
    proc = time.process_time() - proc0
    to_us = 1e6 / count
    return {
        "wall": wall * to_us,
        "py_cpu": thread * to_us,
        "other_cpu": (proc - thread) * to_us,
        "wait": (wall - proc) * to_us,
    }


async def concurrent_block(
    adapter: Any, name: str, count: int, width: int = 64
) -> dict[str, float]:
    per_task = count // width

    async def worker() -> None:
        for _ in range(per_task):
            await one_request(adapter, name)

    wall0, thread0, proc0 = time.perf_counter(), time.thread_time(), time.process_time()
    await asyncio.gather(*(worker() for _ in range(width)))
    total = per_task * width
    wall = time.perf_counter() - wall0
    thread = time.thread_time() - thread0
    proc = time.process_time() - proc0
    return {
        "rps": total / wall,
        "py_cpu": thread / total * 1e6,
        "other_cpu": (proc - thread) / total * 1e6,
    }


def spread(values: list[float]) -> dict[str, float]:
    mean = statistics.mean(values)
    cv = statistics.stdev(values) / mean * 100 if len(values) > 1 and mean else 0.0
    return {"median": round(statistics.median(values), 1), "cv_pct": round(cv, 1)}


async def measure(adapter: Any, name: str, blocks: int) -> dict[str, Any]:
    _, _, expected, count = SCENARIOS[name]
    if await one_request(adapter, name) != expected:
        raise SystemExit(f"{name}: unexpected status")
    await serial_block(adapter, name, count // 4)  # warm-up
    serial = [await serial_block(adapter, name, count) for _ in range(blocks)]
    await concurrent_block(adapter, name, count)  # warm-up the 64-wide pool
    rps = [await concurrent_block(adapter, name, count) for _ in range(blocks)]
    result: dict[str, Any] = {
        key: spread([block[key] for block in serial]) for key in serial[0]
    }
    result["rps_64"] = spread([block["rps"] for block in rps])
    result["py_cpu_64"] = spread([block["py_cpu"] for block in rps])
    result["other_cpu_64"] = spread([block["other_cpu"] for block in rps])
    return result


MARKERS = (  # first match wins
    ("hinoki_app", "application code"),
    ("profile_request", "application code"),
    ("/hinoki/", "Hinoki Python"),
    ("asyncio", "asyncio"),
    ("selectors", "asyncio"),
)


def classify(path: str, func: str) -> str:
    if path == "~":
        native = "hinoki" in func or "core." in func
        return "Rust call (hinoki.core)" if native else "Python builtins"
    for marker, label in MARKERS:
        if marker in path:
            return label
    return "other stdlib / libraries"


async def profile(adapter: Any, name: str, count: int) -> dict[str, Any]:
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(count):
        await one_request(adapter, name)
    profiler.disable()
    stats = pstats.Stats(profiler)
    entries = [
        (key, entry[2])
        for key, entry in stats.stats.items()  # type: ignore[attr-defined]
        if not (key[0] == "~" and ("kqueue" in key[2] or "epoll" in key[2]))
    ]  # the event loop blocked in the selector is waiting, not working
    total = sum(tottime for _, tottime in entries)
    groups: dict[str, float] = {}
    top: list[tuple[float, str]] = []
    for (path, line, func), tottime in entries:
        groups[classify(path, func)] = groups.get(classify(path, func), 0.0) + tottime
        top.append((tottime, f"{path.rsplit('/', 1)[-1]}:{line} {func}"))
    top.sort(reverse=True)
    return {
        "share_pct": {
            key: round(value / total * 100, 1)
            for key, value in sorted(groups.items(), key=lambda kv: -kv[1])
        },
        "top": [
            {"pct": round(value / total * 100, 1), "where": where[:110]}
            for value, where in top[:12]
        ],
    }


async def driver_floor(blocks: int) -> dict[str, Any]:
    """The same SQL straight through psycopg (C), one connection, one at a time."""
    detail = (
        "SELECT p.id, p.title, p.body, p.published, p.created_at, a.id, a.name "
        "FROM posts p JOIN authors a ON a.id = p.author_id WHERE p.id = %s AND p.published"
    )
    count = "SELECT count(*) FROM posts WHERE published"
    page = (
        "SELECT p.id, p.title, p.body, p.published, p.created_at, a.id, a.name "
        "FROM posts p JOIN authors a ON a.id = p.author_id WHERE p.published "
        "ORDER BY p.created_at DESC, p.id DESC LIMIT %s OFFSET 0"
    )
    out: dict[str, Any] = {}
    async with await psycopg.AsyncConnection.connect(DB, autocommit=True) as conn:

        async def timed(
            label: str, statements: list[tuple[str, tuple[Any, ...]]]
        ) -> None:
            for _ in range(100):
                for sql, params in statements:
                    await (await conn.execute(sql, params)).fetchall()
            samples = []
            for _ in range(blocks):
                start = time.perf_counter()
                for _ in range(1000):
                    for sql, params in statements:
                        await (await conn.execute(sql, params)).fetchall()
                samples.append((time.perf_counter() - start) / 1000 * 1e6)
            out[label] = spread(samples)

        await timed("select_1", [("SELECT 1", ())])
        await timed("detail_query", [(detail, (POST,))])
        await timed("list_20_queries", [(count, ()), (page, (20,))])
        await timed("list_100_queries", [(count, ()), (page, (100,))])
    return out


async def start() -> tuple[Any, Callable[[], Awaitable[None]]]:
    adapter = app.asgi()
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(adapter({"type": "lifespan"}, inbox.get, outbox.put))
    await inbox.put({"type": "lifespan.startup"})
    message = await outbox.get()
    if message["type"] != "lifespan.startup.complete":
        raise SystemExit(f"startup failed: {message}")

    async def stop() -> None:
        await inbox.put({"type": "lifespan.shutdown"})
        await outbox.get()
        await task

    return adapter, stop


def cleanup() -> None:
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute("DELETE FROM posts WHERE title = 'profile-new'")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocks", type=int, default=5)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--scenario", nargs="+", choices=tuple(SCENARIOS))
    parser.add_argument("--output")
    args = parser.parse_args()
    names = args.scenario or list(SCENARIOS)
    report: dict[str, Any] = {
        "python": platform.python_version(),
        "machine": platform.platform(),
        "loadavg_at_start": [round(v, 2) for v in os.getloadavg()],
        "blocks": args.blocks,
        "units": "microseconds per request, median of blocks; rps_64 is requests per second",
        "scenarios": {},
    }
    adapter, stop = await start()
    try:
        for name in names:
            report["scenarios"][name] = await measure(adapter, name, args.blocks)
            if args.profile:
                _, _, _, count = SCENARIOS[name]
                report["scenarios"][name]["profile"] = await profile(
                    adapter, name, max(count // 2, 500)
                )
            print(name, json.dumps(report["scenarios"][name].get("wall")), flush=True)
    finally:
        await stop()
        cleanup()
    report["driver_floor"] = await driver_floor(args.blocks)
    text = json.dumps(report, indent=2)
    if args.output:
        with open(args.output, "w") as handle:
            handle.write(text + "\n")
    print(text)


asyncio.run(main())
