"""JSON decoding: ``hinoki.core.decode_json`` against ``json.loads``.

Five sizes (about 60 B, 145 B, 740 B, 20 KB and 1 MB), each decoded repeatedly;
prints the median per call, the coefficient of variation and the min/max over
the samples. Build the extension in release mode first:
``uv run --locked --with maturin maturin develop --release``.
Run: ``uv run --locked python benchmarks/jsondecodebench.py [--output FILE]``.
"""

import argparse
import json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from typing import Any

from hinoki import core

SAMPLES = 7


def _small() -> Any:
    return {"id": 17, "name": "Ada", "ok": True, "tags": ["a", "b"]}


def _medium() -> Any:
    return {
        "id": 1234567,
        "title": "A short title",
        "slug": "a-short-title",
        "published": True,
        "rating": 4.5,
        "author": {"name": "Ada Lovelace", "email": "ada@example.org"},
        "tags": ["python", "rust", "json"],
        "note": None,
    }


def _mixed(items: int) -> Any:
    return [
        {
            "id": index,
            "name": f"item-{index}",
            "active": index % 2 == 0,
            "score": index / 7,
            "tags": ["x", "y"],
            "meta": {"owner": "team", "note": None},
        }
        for index in range(items)
    ]


def _numbers(n: int) -> Any:
    return [
        [i, -i * 1_000_003, i / 3.0, i * 1.5e-7, 123456789012 + i] for i in range(n)
    ]


def _cases() -> list[tuple[str, bytes]]:
    def dump(value: Any) -> bytes:
        return json.dumps(value, separators=(",", ":")).encode()

    return [
        ("~60 B object", dump(_small())),
        ("~145 B object", dump(_medium())),
        ("~740 B list of objects", dump(_mixed(6))),
        ("~20 KB list of objects", dump(_mixed(160))),
        ("~1 MB list of objects", dump(_mixed(8200))),
        ("~20 KB list of numbers", dump(_numbers(500))),
        ("~1 MB list of numbers", dump(_numbers(26000))),
    ]


def _sample(call: Callable[[bytes], Any], raw: bytes, loops: int) -> float:
    start = time.perf_counter_ns()
    for _ in range(loops):
        call(raw)
    return (time.perf_counter_ns() - start) / loops


def _measure(call: Callable[[bytes], Any], raw: bytes, loops: int) -> list[float]:
    call(raw)
    return [_sample(call, raw, loops) for _ in range(SAMPLES)]


def _row(samples: list[float]) -> dict[str, float]:
    median = statistics.median(samples)
    return {
        "median_ns": median,
        "cv_percent": statistics.stdev(samples) / median * 100,
        "min_ns": min(samples),
        "max_ns": max(samples),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="write every sample as JSON")
    args = parser.parse_args()
    report: dict[str, Any] = {
        "python": sys.version,
        "platform": platform.platform(),
        "results": {},
    }
    print("payload, bytes, json.loads ns, decode_json ns, ratio, cv% (loads/native)")
    for name, raw in _cases():
        loops = max(5, 4_000_000 // (len(raw) + 400))
        loads = _measure(json.loads, raw, loops)
        native = _measure(core.decode_json, raw, loops)
        assert json.loads(raw) == core.decode_json(raw)
        a, b = _row(loads), _row(native)
        report["results"][name] = {
            "bytes": len(raw),
            "json_loads": loads,
            "decode_json": native,
        }
        ratio = b["median_ns"] / a["median_ns"]
        print(
            f"{name}, {len(raw)}, {a['median_ns']:.0f}, {b['median_ns']:.0f}, "
            f"{ratio:.2f}, {a['cv_percent']:.1f}/{b['cv_percent']:.1f}, "
            f"native range {b['min_ns']:.0f}-{b['max_ns']:.0f}"
        )
    if args.output:
        with open(args.output, "w") as handle:
            json.dump(report, handle, indent=2)


if __name__ == "__main__":
    main()
