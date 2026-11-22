"""Offset pages against keyset (cursor) pages on a large table, in process.

Both strategies run through the ORM (`apaginate` and `apaginate_cursor`) on a
disposable SQLite file, with the same ordering, at growing depths. The offset
page also pays for its COUNT, as it does in a real list. Every depth is measured
``--runs`` times and the table shows the median and the spread. Nothing is
written outside the temporary directory.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hinoki.db import Database  # noqa: E402
from hinoki.orm import Model, field, ordered_by, use_database  # noqa: E402
from hinoki.orm.migrations import create_table_sql, table_state  # noqa: E402
from hinoki.pagination import (  # noqa: E402
    CursorPagination,
    CursorParams,
    PageParams,
)

SECRET = "benchmark-only-secret-0123456789abcdef"  # noqa: S105


@ordered_by("-rank", "id")
class Row(Model, table="cursor_bench"):
    id: int = field(primary_key=True)
    rank: int
    name: str


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument(
        "--no-index", action="store_true", help="leave the sort order unindexed"
    )
    parser.add_argument(
        "--depths", type=float, nargs="+", default=[0.0, 0.01, 0.1, 0.5, 0.9]
    )
    return parser.parse_args()


async def seed(database: Database, rows: int, *, index: bool) -> None:
    await database.execute(create_table_sql(Row.__table__, table_state(Row), "sqlite"))
    await database.execute(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?) "
        "INSERT INTO cursor_bench (id, rank, name) "
        "SELECT i, i % 1000, 'row' || i FROM n",
        rows,
    )
    if index:
        await database.execute(
            "CREATE INDEX cursor_bench_order ON cursor_bench (rank DESC, id ASC)"
        )


async def token_at(rows: int, depth: int, strategy: CursorPagination) -> str | None:
    """The cursor that follows ``depth`` rows, reached by walking in big pages."""
    token: str | None = None
    walked = 0
    while walked < depth:
        step = min(strategy.max_page_size, depth - walked)
        page = await Row.all().apaginate_cursor(CursorParams(step, token, strategy))
        token, walked = page.next, walked + step
    return token


async def timed(runs: int, call) -> list[float]:  # type: ignore[no-untyped-def]
    await call()  # warm the statement cache and the page cache
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        await call()
        samples.append((time.perf_counter() - start) * 1000)
    return samples


def line(label: str, samples: list[float]) -> str:
    median = statistics.median(samples)
    spread = f"min {min(samples):.3f}, max {max(samples):.3f}"
    return f"{label:<8}{median:>10.3f} ms  ({spread})"


async def main() -> None:
    args = arguments()
    size = args.page_size
    strategy = CursorPagination(page_size=size, max_page_size=10_000, secret=SECRET)
    with tempfile.TemporaryDirectory() as directory:
        database = await Database.connect(
            f"sqlite:///{Path(directory) / 'bench.db'}", max_size=1, wait=5
        )
        with use_database(database):
            await seed(database, args.rows, index=not args.no_index)
            print(
                f"rows={args.rows} page_size={size} runs={args.runs} "
                f"index={not args.no_index}"
            )
            for depth in args.depths:
                offset = min(int(args.rows * depth), args.rows - size)
                token = await token_at(args.rows, offset, strategy)
                params = CursorParams(size, token, strategy)
                number = PageParams(offset // size + 1, size)
                got = await Row.all().apaginate_cursor(params)
                want = await Row.all().apaginate(number)
                if [r.id for r in got.items] != [r.id for r in want.items]:
                    raise SystemExit(f"pages differ at depth {depth}")
                print(f"\ndepth {depth:.0%} (offset {offset})")
                by_offset = functools.partial(Row.all().apaginate, number)
                by_cursor = functools.partial(Row.all().apaginate_cursor, params)
                print(line("offset", await timed(args.runs, by_offset)))
                print(line("cursor", await timed(args.runs, by_cursor)))
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
