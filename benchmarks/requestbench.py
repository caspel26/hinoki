"""Micro-benchmark: JSON decode/encode and query parsing, Python vs Rust.

Measures only the parse/serialise call on identical payloads (not end to end,
no network). Run: ``uv run --locked python benchmarks/requestbench.py``.
Prints median and coefficient of variation over repeated samples.
"""

import argparse
import json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

import hinoki
from hinoki import core

SAMPLES = 7
LOOPS = 20000
PAYLOAD = {
    "id": "7f9c4a5e-3c1d-4a2b-9b0d-1e2f3a4b5c6d",
    "title": "A reasonably typical title for a post",
    "body": "lorem ipsum " * 40,
    "tags": ["a", "bb", "ccc", "dddd"],
    "meta": {"views": 12345, "score": 4.5, "draft": False, "parent": None},
    "items": [{"n": i, "label": f"item-{i}"} for i in range(20)],
}
RAW = json.dumps(PAYLOAD).encode()
QUERY = b"page=2&page_size=50&q=hello%20world&tag=a&tag=b&flag="


def sample(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    for _ in range(LOOPS):
        fn()
    return (time.perf_counter() - start) / LOOPS * 1e6


def report(name: str, python: Callable[[], object], rust: Callable[[], object]) -> dict:
    py = [sample(python) for _ in range(SAMPLES)]
    rs = [sample(rust) for _ in range(SAMPLES)]
    results = {}
    for label, data in (("python", py), ("rust", rs)):
        median = statistics.median(data)
        cv = statistics.stdev(data) / statistics.mean(data) * 100
        print(f"{name:14} {label:7} {median:8.2f} us/call  cv {cv:5.1f}%")
        results[label] = {
            "samples_us": data,
            "median_us": median,
            "cv_percent": cv,
        }
    results["ratio"] = statistics.median(py) / statistics.median(rs)
    print(f"{name:14} ratio   {results['ratio']:8.2f}x")
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    results = {
        "decode_json": report(
            "decode json", lambda: json.loads(RAW), lambda: core.decode_json(RAW)
        ),
        "encode_json": report(
            "encode json",
            lambda: json.dumps(PAYLOAD).encode(),
            lambda: core.encode_json(PAYLOAD),
        ),
        "parse_query": report(
            "parse query",
            lambda: parse_qs(
                QUERY.decode("ascii"), keep_blank_values=True, errors="strict"
            ),
            lambda: core.parse_query_string(QUERY, 1000),
        ),
    }
    if args.output:
        output = {
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "processor": platform.processor(),
                "hinoki": hinoki.__version__,
            },
            "parameters": {"samples": SAMPLES, "loops_per_sample": LOOPS},
            "payload_bytes": len(RAW),
            "results": results,
        }
        args.output.write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()
