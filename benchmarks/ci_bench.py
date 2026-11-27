"""Representative workload benchmark and profiler for Hinoki CI.

Runs five synthetic workloads on a disposable SQLite database over ``KisoWorker``
loopback keep-alive connections (separate client subprocess) or in-process
profiling mode:

1. ``health``: minimal ``GET /health`` route.
2. ``crud_scoped_list``: ``@crud`` list with ``ClaimsJWTAuth`` and ``RowScope``.
3. ``projected_list``: ``select_related(...).apaginate_as(...)`` projection.
4. ``atomic_write``: ``@http.post(..., atomic=True)`` row creation.
5. ``handwritten_get``: manual ``@http.get`` with ``aget()`` + ``Schema`` output.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import itertools
import json
import os
import platform
import pstats
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import hinoki
from hinoki import (
    Access,
    Caller,
    Hinoki,
    Request,
    RowScope,
    RuntimeConfig,
    Schema,
    crud,
    http,
)
from hinoki.cli.routes import lanes_of
from hinoki.crypto import ClaimsJWTAuth, JWTKey
from hinoki.db import Database, DatabaseModule
from hinoki.modules import Module, ModuleContext
from hinoki.orm import Model, OnDelete, field, foreign_key
from hinoki.orm.relations import Related
from hinoki.pagination import Page, PageParams
from hinoki.runtime.kiso import KisoWorker

TENANTS = ("tenant-0", "tenant-1", "tenant-2", "tenant-3")
OWNER_COUNT = 20
TASK_COUNT = 200
HTTP_OK_MIN = 200
HTTP_OK_MAX = 300
NEW_IDS = itertools.count(1_000_000)

LOAD_DRIVER = r"""
import asyncio, json, sys, time

port = int(sys.argv[1])
method = sys.argv[2]
target = sys.argv[3]
body = sys.argv[4].encode()
auth_header = sys.argv[5]
seconds = float(sys.argv[6])
connections = int(sys.argv[7])

headers = [
    f"{method} {target} HTTP/1.1",
    "host: 127.0.0.1",
    "content-type: application/json",
    f"content-length: {len(body)}",
]
if auth_header:
    headers.append(f"authorization: {auth_header}")
raw_req = ("\r\n".join(headers) + "\r\n\r\n").encode() + body

latencies = []
completed = 0
wrong = 0


async def worker(stop: float) -> None:
    global completed, wrong
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        while time.perf_counter() < stop:
            started = time.perf_counter()
            writer.write(raw_req)
            await writer.drain()
            raw_head = await reader.readuntil(b"\r\n\r\n")
            lines = raw_head.decode("latin-1").split("\r\n")
            length = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
                    break
            if length:
                await reader.readexactly(length)
            status = lines[0].split(" ", 2)[1] if " " in lines[0] else ""
            completed += 1
            if not status.startswith("2"):
                wrong += 1
            latencies.append(time.perf_counter() - started)
    finally:
        writer.close()


async def main() -> None:
    stop = time.perf_counter() + seconds
    began = time.perf_counter()
    await asyncio.gather(*(worker(stop) for _ in range(connections)))
    elapsed = max(time.perf_counter() - began, 1e-9)
    latencies.sort()
    count = len(latencies)
    def pick(q: float) -> float:
        if count == 0:
            return 0.0
        idx = min(count - 1, int(count * q))
        return latencies[idx] * 1000.0
    print(json.dumps({
        "rps": count / elapsed,
        "p50_ms": pick(0.50),
        "p95_ms": pick(0.95),
        "p99_ms": pick(0.99),
        "completed": completed,
        "wrong": wrong,
    }))


asyncio.run(main())
"""


class BenchOwner(Model, table="bench_owners"):
    id: int = field(primary_key=True)
    name: str
    email: str


class BenchTask(Model, table="bench_tasks"):
    id: int = field(primary_key=True)
    tenant_id: str
    title: str
    status: str
    owner_id: int = foreign_key(
        BenchOwner, on_delete=OnDelete.RESTRICT, related_name="tasks"
    )
    owner: ClassVar[Related[BenchOwner]]


class HealthOut(Schema):
    status: str


class OwnerOut(Schema):
    id: int
    name: str


class TaskOut(Schema):
    id: int
    tenant_id: str
    title: str
    status: str
    owner_id: int


class TaskWithOwnerOut(Schema):
    id: int
    tenant_id: str
    title: str
    status: str
    owner: OwnerOut


class TaskCreateIn(Schema):
    title: str
    owner_id: int = 1


class SeedModule(Module):
    name = "bench-ci-seed"
    requires = (DatabaseModule,)

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        await _seed_database(ctx.get(Database))
        yield


async def _seed_database(database: Database) -> None:
    await database.execute(
        "create table if not exists bench_owners "
        "(id integer primary key, name text not null, email text not null)"
    )
    await database.execute(
        "create table if not exists bench_tasks ("
        "id integer primary key, "
        "tenant_id text not null, "
        "title text not null, "
        "status text not null, "
        "owner_id integer not null references bench_owners(id)"
        ")"
    )
    await database.execute(
        "create index if not exists idx_bench_tasks_tenant "
        "on bench_tasks(tenant_id, id)"
    )
    for owner_id in range(1, OWNER_COUNT + 1):
        await database.execute(
            "insert or replace into bench_owners (id, name, email) values (?, ?, ?)",
            owner_id,
            f"Owner {owner_id}",
            f"owner{owner_id}@example.test",
        )
    for task_id in range(1, TASK_COUNT + 1):
        tenant_id = TENANTS[(task_id - 1) % len(TENANTS)]
        owner_id = ((task_id - 1) % OWNER_COUNT) + 1
        status = "done" if task_id % 5 == 0 else "open"
        await database.execute(
            "insert or replace into bench_tasks "
            "(id, tenant_id, title, status, owner_id) values (?, ?, ?, ?, ?)",
            task_id,
            tenant_id,
            f"Task {task_id}",
            status,
            owner_id,
        )


@dataclass(frozen=True, slots=True)
class WorkloadSpec:
    name: str
    method: str
    target: str
    body: str
    auth: bool


WORKLOADS: tuple[WorkloadSpec, ...] = (
    WorkloadSpec("health", "GET", "/health", "", False),
    WorkloadSpec("crud_scoped_list", "GET", "/tasks?page=1&page_size=20", "", True),
    WorkloadSpec(
        "projected_list", "GET", "/projected/tasks?page=1&page_size=20", "", True
    ),
    WorkloadSpec(
        "atomic_write",
        "POST",
        "/tasks/atomic",
        '{"title":"bench-created","owner_id":1}',
        True,
    ),
    WorkloadSpec("handwritten_get", "GET", "/manual/tasks/1", "", True),
)


def _make_auth() -> tuple[ClaimsJWTAuth, str]:
    signing_key = JWTKey("bench-1", "HS256", os.urandom(32))

    class BenchTokens(ClaimsJWTAuth):
        keys: ClassVar[tuple[JWTKey, ...]] = (signing_key,)
        issuer: ClassVar[str | None] = "hinoki-bench"
        audience: ClassVar[str | None] = "bench"

    auth = BenchTokens()
    token = auth.issue(
        {"sub": "bench-user-1", "tenant_id": "tenant-0", "scopes": ["tasks:rw"]},
        ttl=3600,
    )
    return auth, f"Bearer {token}"


def _build_app(
    db_url: str,
    slowdown_ms: float = 0.0,
) -> tuple[Hinoki, str, dict[str, tuple[str, str]]]:
    auth, bearer = _make_auth()
    protected = Access(auth=auth)

    @http.get("/health")
    async def health() -> HealthOut:
        return HealthOut(status="ok")

    @http.get("/projected/tasks", access=protected)
    async def projected_list(
        caller: Caller[None], page: int = 1, page_size: int = 20
    ) -> Page[TaskWithOwnerOut]:
        tenant_id = str(caller.claims["tenant_id"])
        return (
            await BenchTask.filter(tenant_id=tenant_id)
            .order_by("id")
            .select_related("owner")
            .apaginate_as(TaskWithOwnerOut, PageParams(page, page_size))
        )

    @http.post("/tasks/atomic", access=protected, atomic=True)
    async def atomic_write(caller: Caller[None], payload: TaskCreateIn) -> TaskOut:
        tenant_id = str(caller.claims["tenant_id"])
        created = await BenchTask.acreate(
            id=next(NEW_IDS),
            tenant_id=tenant_id,
            title=payload.title,
            status="open",
            owner_id=payload.owner_id,
        )
        return TaskOut(
            id=created.id,
            tenant_id=created.tenant_id,
            title=created.title,
            status=created.status,
            owner_id=created.owner_id,
        )

    @http.get("/manual/tasks/{task_id}", access=protected)
    async def handwritten_get(caller: Caller[None], task_id: int) -> TaskWithOwnerOut:
        tenant_id = str(caller.claims["tenant_id"])
        row = (
            await BenchTask.filter(tenant_id=tenant_id, id=task_id)
            .select_related("owner")
            .aget()
        )
        return TaskWithOwnerOut(
            id=row.id,
            tenant_id=row.tenant_id,
            title=row.title,
            status=row.status,
            owner=OwnerOut(id=row.owner.id, name=row.owner.name),
        )

    tasks_crud = crud(
        BenchTask,
        path="/tasks",
        read=TaskOut,
        access=protected,
        scope=RowScope.by_claim(BenchTask, "tenant_id", "tenant_id"),
    )

    middlewares: tuple[Any, ...] = ()
    if slowdown_ms > 0.0:

        class _SlowdownMiddleware:
            def __init__(self, delay_s: float) -> None:
                self.delay_s = delay_s

            async def __call__(
                self,
                request: Request,
                call_next: Callable[[], Awaitable[Any]],
            ) -> Any:
                del request
                await asyncio.sleep(self.delay_s)
                return await call_next()

        middlewares = (_SlowdownMiddleware(slowdown_ms / 1000.0),)

    app = Hinoki(
        runtime=RuntimeConfig(),
        include=[health, projected_list, atomic_write, handwritten_get, tasks_crud],
        modules=[DatabaseModule(db_url), SeedModule()],
        middleware=middlewares,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    lane_by_key = _workload_lanes(app)
    return app, bearer, lane_by_key


def _workload_lanes(app: Hinoki) -> dict[str, tuple[str, str]]:
    raw_lanes = lanes_of(app)
    targets = {
        "health": ("GET", "/health"),
        "crud_scoped_list": ("GET", "/tasks"),
        "projected_list": ("GET", "/projected/tasks"),
        "atomic_write": ("POST", "/tasks/atomic"),
        "handwritten_get": ("GET", "/manual/tasks/{task_id}"),
    }
    matched: dict[str, tuple[str, str]] = {}
    for route in app.routes:
        pair = (route.method, route.path)
        for name, target in targets.items():
            if pair == target:
                matched[name] = raw_lanes.get(id(route), ("unknown", "unknown"))
    return matched


async def _run_load(
    port: int,
    spec: WorkloadSpec,
    bearer: str,
    seconds: float,
    connections: int,
) -> dict[str, Any]:
    auth_header = bearer if spec.auth else ""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        LOAD_DRIVER,
        str(port),
        spec.method,
        spec.target,
        spec.body,
        auth_header,
        str(seconds),
        str(connections),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"load driver failed for {spec.name} ({proc.returncode}): "
            f"{stderr.decode(errors='replace')}"
        )
    result: dict[str, Any] = json.loads(stdout.decode())
    return result


async def _bench_async(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="hinoki-ci-bench-") as tmp:
        db_url = f"sqlite://{Path(tmp) / 'bench.sqlite3'}"
        app, bearer, lane_map = _build_app(db_url, slowdown_ms=args.slowdown_ms)
        worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=True)
        port = await worker.start()
        workloads_out: dict[str, Any] = {}
        try:
            for spec in WORKLOADS:
                await _run_load(
                    port, spec, bearer, args.warmup, min(args.connections, 4)
                )
                metrics = await _run_load(
                    port, spec, bearer, args.seconds, args.connections
                )
                lane, reason = lane_map.get(spec.name, ("unknown", "unknown"))
                workloads_out[spec.name] = {
                    **metrics,
                    "lane": lane,
                    "lane_reason": reason,
                }
        finally:
            await worker.stop()

    return {
        "schema_version": 1,
        "sha": args.sha or _git_head_sha(),
        "hinoki_file": str(Path(hinoki.__file__).resolve()),
        "env": _env_info(),
        "config": {
            "seconds": args.seconds,
            "warmup": args.warmup,
            "connections": args.connections,
            "slowdown_ms": args.slowdown_ms,
        },
        "workloads": workloads_out,
    }


def _asgi_scope(spec: WorkloadSpec, bearer: str) -> dict[str, Any]:
    path, _, query = spec.target.partition("?")
    headers: list[tuple[bytes, bytes]] = [
        (b"host", b"127.0.0.1"),
        (b"content-type", b"application/json"),
    ]
    if spec.auth:
        headers.append((b"authorization", bearer.encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": spec.method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "scheme": "http",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }


async def _call_asgi(
    adapter: Any, scope: dict[str, Any], body: bytes
) -> tuple[int, int]:
    status = 0
    size = 0
    sent_body = False

    async def receive() -> dict[str, Any]:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, size
        if message["type"] == "http.response.start":
            status = int(message["status"])
        elif message["type"] == "http.response.body":
            size += len(message.get("body", b""))

    await adapter(scope, receive, send)
    return status, size


async def _profile_one(
    adapter: Any,
    spec: WorkloadSpec,
    bearer: str,
    iterations: int,
) -> dict[str, Any]:
    scope = _asgi_scope(spec, bearer)
    body = spec.body.encode("utf-8")
    for _ in range(10):
        await _call_asgi(adapter, scope, body)

    wall_samples: list[float] = []
    py_cpu_samples: list[float] = []
    proc_cpu_samples: list[float] = []
    wrong = 0
    last_size = 0
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        py0 = time.thread_time_ns()
        proc0 = time.process_time_ns()
        status, last_size = await _call_asgi(adapter, scope, body)
        proc1 = time.process_time_ns()
        py1 = time.thread_time_ns()
        t1 = time.perf_counter_ns()
        if not HTTP_OK_MIN <= status < HTTP_OK_MAX:
            wrong += 1
        wall_samples.append((t1 - t0) / 1000.0)
        py_cpu_samples.append((py1 - py0) / 1000.0)
        proc_cpu_samples.append((proc1 - proc0) / 1000.0)

    wall_us = _median(wall_samples)
    py_cpu_us = _median(py_cpu_samples)
    proc_cpu_us = _median(proc_cpu_samples)
    other_cpu_us = max(0.0, proc_cpu_us - py_cpu_us)
    wait_us = max(0.0, wall_us - proc_cpu_us)

    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(min(iterations, 100)):
        await _call_asgi(adapter, scope, body)
    profiler.disable()
    buf = io.StringIO()
    pstats.Stats(profiler, stream=buf).sort_stats("cumtime").print_stats(10)

    return {
        "wall_us": round(wall_us, 2),
        "py_cpu_us": round(py_cpu_us, 2),
        "other_cpu_us": round(other_cpu_us, 2),
        "wait_us": round(wait_us, 2),
        "response_bytes": last_size,
        "wrong": wrong,
        "cprofile_top": [
            line.strip() for line in buf.getvalue().splitlines()[5:15] if line.strip()
        ],
    }


@asynccontextmanager
async def _start_asgi(app: Hinoki) -> AsyncIterator[Any]:
    adapter = app.asgi()
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(adapter({"type": "lifespan"}, inbox.get, outbox.put))
    await inbox.put({"type": "lifespan.startup"})
    msg = await outbox.get()
    if msg["type"] != "lifespan.startup.complete":
        raise RuntimeError(f"ASGI lifespan startup failed: {msg}")
    try:
        yield adapter
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        await outbox.get()
        await task


async def _profile_async(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="hinoki-ci-profile-") as tmp:
        db_url = f"sqlite://{Path(tmp) / 'profile.sqlite3'}"
        app, bearer, lane_map = _build_app(db_url)
        async with _start_asgi(app) as adapter:
            workloads_out: dict[str, Any] = {}
            for spec in WORKLOADS:
                lane, reason = lane_map.get(spec.name, ("unknown", "unknown"))
                metrics = await _profile_one(adapter, spec, bearer, args.iterations)
                workloads_out[spec.name] = {
                    **metrics,
                    "lane": lane,
                    "lane_reason": reason,
                }
    return {
        "schema_version": 1,
        "sha": args.sha or _git_head_sha(),
        "hinoki_file": str(Path(hinoki.__file__).resolve()),
        "env": _env_info(),
        "iterations": args.iterations,
        "workloads": workloads_out,
    }


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _git_head_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return proc.stdout.strip()


def _env_info() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hinoki CI benchmark and profiler")
    parser.add_argument("--mode", choices=("bench", "profile"), default="bench")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--sha", default="")
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--warmup", type=float, default=0.5)
    parser.add_argument("--connections", type=int, default=16)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--slowdown-ms", type=float, default=0.0)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = (
        asyncio.run(_profile_async(args))
        if args.mode == "profile"
        else asyncio.run(_bench_async(args))
    )
    rendered = json.dumps(payload, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
