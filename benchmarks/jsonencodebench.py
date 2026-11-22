"""JSON encoding: ``hinoki.core.encode_json`` against ``json.dumps``.

Object-, number-, string- and nesting-heavy payloads at about 740 B, 20 KB and
1 MB, plus a UUID/datetime payload and two small objects; prints the median
per call, the spread over the samples and the ratio to the standard library
(compact separators, ``ensure_ascii=False``, the same bytes). Build the
extension in release mode first:
``uv run --locked --with maturin maturin develop --release``, then run with
``uv run --no-sync python benchmarks/jsonencodebench.py [--output FILE]``.
"""

import argparse
import datetime
import json
import platform
import statistics
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

from hinoki import core

SAMPLES = 7
TARGET_NS = 40_000_000


def _objects(n: int) -> Any:
    return [
        {
            "id": i,
            "name": f"item-{i}",
            "active": i % 2 == 0,
            "score": i / 7,
            "tags": ["x", "y"],
            "meta": {"owner": "team", "note": None},
        }
        for i in range(n)
    ]


def _numbers(n: int) -> Any:
    return [
        [i, -i * 1_000_003, i / 3.0, i * 1.5e-7, 123456789012 + i] for i in range(n)
    ]


def _strings(n: int) -> Any:
    words = [
        "héllo wörld",
        "日本語のテキスト",
        "emoji 🎉 ok",
        "plain ascii text",
        "ελληνικά",
    ]
    return [{"t": words[i % 5] + str(i), "u": words[(i + 2) % 5]} for i in range(n)]


def _nested(n: int) -> Any:
    out = []
    for i in range(n):
        value: Any = {"leaf": i}
        for depth in range(20):
            value = {"k": value, "a": [depth]}
        out.append(value)
    return out


def _dated(n: int) -> Any:
    base = datetime.datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=datetime.UTC)
    return [
        {
            "id": uuid.UUID(int=i * 7919 + 1),
            "at": base + datetime.timedelta(seconds=i),
            "n": i,
        }
        for i in range(n)
    ]


def _default(value: Any) -> Any:
    return str(value) if isinstance(value, uuid.UUID) else value.isoformat()


def _dumps(value: Any) -> bytes:
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=False, default=_default
    ).encode()


def _sized(make: Callable[[int], Any], target: int) -> Any:
    n = 1
    while len(_dumps(make(n))) < target:
        n = max(n + 1, int(n * target / max(1, len(_dumps(make(n))))))
    return make(n)


def _cases() -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = [
        ("60 B obj", {"id": 17, "name": "Ada", "ok": True, "tags": ["a", "b"]}),
        (
            "145 B obj",
            {
                "id": 1234567,
                "title": "A short title",
                "published": True,
                "rating": 4.5,
                "author": {"name": "Ada Lovelace", "email": "ada@example.org"},
                "tags": ["python", "rust", "json"],
                "note": None,
            },
        ),
    ]
    shapes = [
        ("obj", _objects),
        ("num", _numbers),
        ("str", _strings),
        ("nest", _nested),
    ]
    for name, make in shapes:
        for label, size in [("740 B", 740), ("20 KB", 20_000), ("1 MB", 1_000_000)]:
            out.append((f"{label} {name}", _sized(make, size)))
    out.append(("20 KB dated", _sized(_dated, 20_000)))
    return out


def _measure(call: Callable[[Any], Any], value: Any) -> list[float]:
    call(value)
    start = time.perf_counter_ns()
    call(value)
    loops = max(1, TARGET_NS // max(1, time.perf_counter_ns() - start))
    samples = []
    for _ in range(SAMPLES):
        start = time.perf_counter_ns()
        for _ in range(loops):
            call(value)
        samples.append((time.perf_counter_ns() - start) / loops)
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="write every sample as JSON")
    args = parser.parse_args()
    report: dict[str, Any] = {
        "python": sys.version,
        "platform": platform.platform(),
        "results": {},
    }
    print("case, bytes, json.dumps ns, encode_json ns, ratio, spread% (dumps/native)")
    for name, value in _cases():
        raw = _dumps(value)
        native_call = lambda v: core.encode_json(v, default=_default)  # noqa: E731
        assert json.loads(core.encode_json(value, default=_default)) == json.loads(raw)
        loads = _measure(_dumps, value)
        native = _measure(native_call, value)
        report["results"][name] = {
            "bytes": len(raw),
            "json_dumps": loads,
            "encode_json": native,
        }
        a, b = statistics.median(loads), statistics.median(native)
        spreads = [
            (max(s) - min(s)) / statistics.median(s) * 100 for s in (loads, native)
        ]
        print(
            f"{name}, {len(raw)}, {a:.0f}, {b:.0f}, {b / a:.2f}, "
            f"{spreads[0]:.0f}/{spreads[1]:.0f}"
        )
    if args.output:
        with open(args.output, "w") as handle:
            json.dump(report, handle, indent=2)


if __name__ == "__main__":
    main()
