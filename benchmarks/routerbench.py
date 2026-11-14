"""Router micro-benchmark: Python -> Rust call cost vs a pure-Python baseline.

Measures only ``find`` on a fixed route table with fixed request lines. It does
not include HTTP, ASGI, parsing or validation, so it is not an end-to-end
number. Samples, machine and interpreter are recorded next to the results.

    uv run python benchmarks/routerbench.py [--samples 5] [--iterations 200000]
"""

import argparse
import json
import platform
import re
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

import hinoki
from hinoki.core import Router

ROUTES = [
    ("GET", "/health"),
    ("GET", "/posts"),
    ("POST", "/posts"),
    ("GET", "/posts/{post_id}"),
    ("PUT", "/posts/{post_id}"),
    ("DELETE", "/posts/{post_id}"),
    ("POST", "/posts/{post_id}/publish"),
    ("GET", "/tags"),
    ("GET", "/users/{user_id}/posts/{post_id}"),
]
REQUESTS = [
    ("GET", "/health"),
    ("GET", "/posts"),
    ("GET", "/posts/42"),
    ("POST", "/posts/42/publish"),
    ("GET", "/users/7/posts/42"),
    ("GET", "/missing"),
]


class RegexBaseline:
    """Linear regex router: a deliberately simple Python reference."""

    def __init__(self) -> None:
        self.table = [
            (m, re.compile("^" + re.sub(r"\{[^}]+\}", "([^/]+)", t) + "$"), i)
            for i, (m, t) in enumerate(ROUTES)
        ]

    def find(self, method: str, path: str) -> int | None:
        for route_method, pattern, action in self.table:
            if route_method == method and pattern.match(path):
                return action
        return None


def native_router() -> Router:
    router = Router()
    for index, (method, template) in enumerate(ROUTES):
        router.add(method, template, index)
    return router


def sample(find: Callable[[str, str], object], iterations: int) -> float:
    """Nanoseconds per lookup for one sample."""
    start = time.perf_counter_ns()
    for _ in range(iterations // len(REQUESTS)):
        for method, path in REQUESTS:
            find(method, path)
    return (time.perf_counter_ns() - start) / (
        (iterations // len(REQUESTS)) * len(REQUESTS)
    )


def summarise(values: list[float]) -> dict[str, float]:
    mean = statistics.fmean(values)
    return {
        "median_ns": statistics.median(values),
        "mean_ns": mean,
        "stdev_ns": statistics.stdev(values) if len(values) > 1 else 0.0,
        "cv_percent": 100 * statistics.stdev(values) / mean if len(values) > 1 else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=200_000)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results.json"))
    args = parser.parse_args()

    candidates: dict[str, Callable[[str, str], object]] = {
        "native_router": native_router().find,
        "python_regex_baseline": RegexBaseline().find,
    }
    for find in candidates.values():  # warm-up
        sample(find, args.iterations // 10)
    results = {
        name: summarise([sample(find, args.iterations) for _ in range(args.samples)])
        for name, find in candidates.items()
    }
    report = {
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor(),
            "hinoki": hinoki.__version__,
        },
        "parameters": {"samples": args.samples, "iterations": args.iterations},
        "routes": len(ROUTES),
        "results": results,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    for name, stats in results.items():
        print(
            f"{name:24} median {stats['median_ns']:8.0f} ns/lookup  "
            f"cv {stats['cv_percent']:.1f}%"
        )


if __name__ == "__main__":
    main()
