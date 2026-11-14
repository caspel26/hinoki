"""Measure Python-to-Rust PostgreSQL ORM SQL compilation without a database."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hinoki import core
from hinoki.orm import Model, field

MIN_SAMPLES = 2
MIN_ITERATIONS = 1


class BenchRecord(Model, table="bench_records"):
    id: int = field(primary_key=True)
    tenant_id: int
    slug: str
    title: str
    body: str
    active: bool
    score: int
    note: str | None = None


def git_revision() -> str:
    """Return the source revision, or `unknown` outside a Git worktree."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - fixed local command
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def queries() -> dict[str, Callable[[], Any]]:
    """Build representative query shapes once, outside timed loops."""
    select = (
        BenchRecord.filter(
            tenant_id=17,
            active=True,
            score__gte=4,
            slug__in=["a", "b", "c"],
        )
        .order_by("-score", "title")
        .limit(50)
        .offset(100)
    )
    return {
        "select": select.compile,
        "insert": lambda: core.compile_query(
            "insert",
            "bench_records",
            assignments=[
                ("tenant_id", 17),
                ("slug", "a"),
                ("title", "title"),
                ("body", "body"),
                ("active", True),
                ("score", 4),
            ],
            returning=["id"],
        ),
        "update": lambda: core.compile_query(
            "update",
            "bench_records",
            assignments=[("active", False), ("score", 8)],
            filters=[("id", "eq", [5])],
            returning=["id"],
        ),
        "delete": lambda: BenchRecord.filter(id=5).compile("delete"),
    }


def measure(call: Callable[[], Any], samples: int, iterations: int) -> dict[str, Any]:
    call()  # warm extension and query path
    per_sample_ns: list[float] = []
    for _ in range(samples):
        start = time.perf_counter_ns()
        for _ in range(iterations):
            call()
        per_sample_ns.append((time.perf_counter_ns() - start) / iterations)
    median = statistics.median(per_sample_ns)
    mean = statistics.mean(per_sample_ns)
    stdev = statistics.stdev(per_sample_ns) if len(per_sample_ns) > 1 else 0.0
    return {
        "iterations_per_sample": iterations,
        "sample_ns_per_call": per_sample_ns,
        "median_ns_per_call": median,
        "mean_ns_per_call": mean,
        "cv_percent": 100 * stdev / mean if mean else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=50_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < MIN_SAMPLES or args.iterations < MIN_ITERATIONS:
        parser.error("--samples must be >= 2 and --iterations must be >= 1")

    results = {
        name: measure(call, args.samples, args.iterations)
        for name, call in queries().items()
    }
    report = {
        "benchmark": "orm-sql-compilation",
        "scope": "QuerySet and Rust extension SQL compilation; no database or network",
        "revision": git_revision(),
        "python": sys.version,
        "platform": platform.platform(),
        "results": results,
    }
    serialized = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
