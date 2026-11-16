"""In-process database awaitables: concurrent fetches on a temporary SQLite file.

Run with the extension under test; compare builds by alternating runs.
    uv run --locked python benchmarks/nativeawaitbench.py [--rounds 15]
Prints one JSON object: median calls per second per case.
"""

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from hinoki.db import Database

CONCURRENCY = 64
CALLS = 4000


async def timed(db: Database, sql: str, expected_rows: int) -> float:
    started = time.perf_counter()
    per_task = CALLS // CONCURRENCY

    async def worker() -> None:
        for _ in range(per_task):
            rows = await db.fetch(sql)
            if len(rows) != expected_rows:
                raise AssertionError(f"{len(rows)} rows, expected {expected_rows}")

    await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
    return per_task * CONCURRENCY / (time.perf_counter() - started)


async def measure(rounds: int) -> dict[str, dict[str, float]]:
    with tempfile.TemporaryDirectory() as tmp:
        db = await Database.connect(f"sqlite://{Path(tmp) / 'b.sqlite3'}", max_size=8)
        await db.execute("create table t (id integer primary key, v text)")
        for n in range(100):
            await db.execute("insert into t (v) values (?)", f"value-{n}")
        cases = {
            "fetch_1": ("select id, v from t where id = 1", 1),
            "fetch_100": ("select id, v from t", 100),
        }
        out: dict[str, dict[str, float]] = {}
        for name, (sql, expected) in cases.items():
            await timed(db, sql, expected)  # warm-up
            samples = [await timed(db, sql, expected) for _ in range(rounds)]
            out[name] = {
                "median_per_second": statistics.median(samples),
                "cv_percent": 100
                * statistics.pstdev(samples)
                / statistics.mean(samples),
            }
        db.close()
        return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=15)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(measure(args.rounds))))
