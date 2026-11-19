"""The native handler lane against the asyncio path, over HTTP on loopback.

The Rust server runs in this process with ``native_dispatch`` on or off; the
load comes from a separate process of keep-alive connections, so the client
does not share the server's event loop. Scenarios (SQLite file, 100 seeded rows):

* ``one``      a handler that reads one row (served by the lane),
* ``list_20``  and ``list_100`` handlers that read a list (served by the lane),
* ``mixed``    a read followed by an ``asyncio.sleep`` (refused by the proof,
               so it stays on asyncio in both modes: the control),
* ``write``    an ``atomic`` POST (stays on asyncio in both modes: the control).

    uv run --locked python benchmarks/nativelanebench.py [--rounds 5] [--seconds 3]

Prints one JSON object: per scenario and mode the median requests per second
and the median p50/p99 latency in milliseconds over the rounds, with the
coefficient of variation of the throughput. Modes alternate every round so
drift in the host hits both. Compare runs on an otherwise idle machine; the
numbers say nothing about other frameworks.
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from hinoki import Hinoki, RuntimeConfig, http
from hinoki.db import Database, DatabaseModule
from hinoki.modules import Module, ModuleContext
from hinoki.orm import Model, field
from hinoki.orm.options import table
from hinoki.runtime.rustserver import RustWorker

CONNECTIONS = 32
SCENARIOS = {
    "one": ("GET", "/bench/one", b""),
    "list_20": ("GET", "/bench/list/20", b""),
    "list_100": ("GET", "/bench/list/100", b""),
    "mixed": ("GET", "/bench/mixed", b""),
    "write": ("POST", "/bench/write", b'{"title":"x"}'),
}


@table("bench_notes")
class BenchNote(Model):
    id: int = field(primary_key=True)
    title: str


class Seed(Module):
    name = "bench-seed"
    requires = (DatabaseModule,)

    def __init__(self) -> None:
        self.database: Database | None = None

    async def setup(self, ctx: ModuleContext) -> None:
        database = ctx.get(Database)
        await database.execute(
            "create table if not exists bench_notes "
            "(id integer primary key, title text not null)"
        )
        for n in range(1, 101):
            await database.execute(
                "insert or replace into bench_notes (id, title) values (?, ?)",
                n,
                f"note {n}",
            )


@http.get("/bench/one")
async def one() -> dict[str, Any]:
    found = await BenchNote.aget(id=1)
    return {"id": found.id, "title": found.title}


@http.get("/bench/list/{count}")
async def listing(count: int) -> list[dict[str, Any]]:
    rows = await BenchNote.all().order_by("id").limit(count)
    return [{"id": row.id, "title": row.title} for row in rows]


@http.get("/bench/mixed")
async def mixed() -> dict[str, Any]:
    found = await BenchNote.aget(id=1)
    await asyncio.sleep(0)
    return {"id": found.id}


@http.post("/bench/write", atomic=True)
async def write(title: str) -> dict[str, str]:
    await BenchNote.acreate(title=title)
    return {"title": title}


LOAD = r"""
import asyncio, json, sys, time

port, method, target, body, seconds, connections = (
    int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4].encode(),
    float(sys.argv[5]), int(sys.argv[6]),
)
head = (
    f"{method} {target} HTTP/1.1\r\nhost: b\r\ncontent-type: application/json\r\n"
    f"content-length: {len(body)}\r\n\r\n"
).encode() + body
latencies = []
errors = 0

async def worker(stop):
    global errors
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    while time.perf_counter() < stop:
        started = time.perf_counter()
        writer.write(head)
        raw = await reader.readuntil(b"\r\n\r\n")
        lines = raw.decode("latin-1").split("\r\n")
        length = next(
            (
                int(x.split(": ")[1])
                for x in lines
                if x.lower().startswith("content-length")
            ),
            0,
        )
        await reader.readexactly(length)
        if not lines[0].split()[1].startswith("2"):
            errors += 1
        latencies.append(time.perf_counter() - started)
    writer.close()

async def main():
    stop = time.perf_counter() + seconds
    began = time.perf_counter()
    await asyncio.gather(*(worker(stop) for _ in range(connections)))
    elapsed = time.perf_counter() - began
    latencies.sort()
    pick = lambda q: latencies[min(len(latencies) - 1, int(len(latencies) * q))] * 1000
    print(json.dumps({
        "rps": len(latencies) / elapsed, "p50_ms": pick(0.50), "p99_ms": pick(0.99),
        "errors": errors,
    }))

asyncio.run(main())
"""


async def run_mode(
    url: str, native: bool, seconds: float
) -> dict[str, dict[str, float]]:
    app = Hinoki(runtime=RuntimeConfig())
    app.include(one, listing, mixed, write)
    app.install(DatabaseModule(url), Seed())
    worker = RustWorker(app, "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    results: dict[str, dict[str, float]] = {}
    try:
        for name, (method, target, body) in SCENARIOS.items():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                LOAD,
                str(port),
                method,
                target,
                body.decode(),
                str(seconds),
                str(CONNECTIONS),
                stdout=subprocess.PIPE,
            )
            out, _ = await process.communicate()
            results[name] = json.loads(out)
    finally:
        await worker.stop()
    return results


def summarize(samples: list[dict[str, float]]) -> dict[str, float]:
    rps = [s["rps"] for s in samples]
    return {
        "rps_median": statistics.median(rps),
        "rps_cv_percent": 100 * statistics.pstdev(rps) / statistics.mean(rps),
        "p50_ms_median": statistics.median(s["p50_ms"] for s in samples),
        "p99_ms_median": statistics.median(s["p99_ms"] for s in samples),
        "errors": sum(s["errors"] for s in samples),
    }


async def measure(rounds: int, seconds: float) -> dict[str, Any]:
    collected: dict[str, dict[str, list[dict[str, float]]]] = {
        mode: {name: [] for name in SCENARIOS} for mode in ("lane", "asyncio")
    }
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite://{Path(tmp) / 'bench.sqlite3'}"
        await run_mode(url, True, 0.5)  # warm-up
        for index in range(rounds):
            order = (True, False) if index % 2 == 0 else (False, True)
            for native in order:
                mode = "lane" if native else "asyncio"
                for name, sample in (await run_mode(url, native, seconds)).items():
                    collected[mode][name].append(sample)
    return {
        "rounds": rounds,
        "seconds": seconds,
        "connections": CONNECTIONS,
        "measured_at": time.strftime("%Y-%m-%d"),
        "results": {
            mode: {name: summarize(s) for name, s in scenarios.items()}
            for mode, scenarios in collected.items()
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--seconds", type=float, default=3.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(measure(args.rounds, args.seconds)), indent=2))
