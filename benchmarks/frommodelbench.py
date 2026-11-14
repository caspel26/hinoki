"""``Schema.from_model``: the Rust path against the former pure-Python loop.

Same schema, same row; prints median and CV over several samples.
Run: ``PYTHONPATH=. uv run --locked python benchmarks/frommodelbench.py``.
"""

import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from hinoki import Schema
from hinoki.orm import Model, field
from hinoki.schema import from_row

SAMPLES = 7
LOOPS = 20000


class Post(Model, table="bench_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=160)
    slug: str = field(max_length=180)
    body: str = ""
    published: bool = False
    views: int = 0
    rating: float = 0.0
    created: datetime = field(default_factory=lambda: datetime.now(UTC))


class PostOut(Schema, model=Post):
    pass


def python_from_model(cls: type[Schema], row: object) -> Schema:
    """The previous implementation, kept here only to compare."""
    values: dict[str, Any] = {}
    for name, spec in cls.specs().items():
        values[name] = from_row(spec.ftype, getattr(row, name))
    return cls(**values)


def measure(call: Callable[[], object]) -> tuple[float, float]:
    medians = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        for _ in range(LOOPS):
            call()
        medians.append((time.perf_counter() - started) / LOOPS * 1e6)
    mean = statistics.mean(medians)
    return statistics.median(medians), statistics.stdev(medians) / mean * 100


def main() -> None:
    row = Post(title="t", slug="s", body="b")
    native_us, native_cv = measure(lambda: PostOut.from_model(row))
    python_us, python_cv = measure(lambda: python_from_model(PostOut, row))
    print(f"native  {native_us:6.2f} us  (CV {native_cv:.1f}%)")
    print(f"python  {python_us:6.2f} us  (CV {python_cv:.1f}%)")
    print(f"speed-up {python_us / native_us:.2f}x")


main()
