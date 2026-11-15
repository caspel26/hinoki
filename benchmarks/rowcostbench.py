"""Per-row cost of building and serializing a model row, without a database.

Question: what do the per-row steps of a list response cost in isolation? One
``PostOut`` with a nested ``AuthorOut`` (two UUIDs, a datetime, text and a
boolean), as ``hinoki_app`` returns it. 7 samples of 20 000 calls after warm-up.

* ``dump``: ``Schema.dump()`` (JSON-compatible dict), what the page uses per row;
* ``encode``: ``encode_json`` of the same instance;
* ``uuid_text``: ``str(UUID)`` for one UUID, the cost the encoder pays per id;
* ``from_row``: ``Post.from_row`` for one row of already-decoded values.

Run from ``benchmarks/comparative`` with its environment (it imports
``hinoki_app``): ``PYTHONPATH=. uv run python ../rowcostbench.py [--output FILE]``.
"""

import argparse
import json
import os
import platform
import statistics
import time
from typing import Any
from uuid import uuid4

os.environ.setdefault("BENCH_DATABASE__URL", "postgres://u:p@127.0.0.1:1/none")

import hinoki_app as app

from hinoki import core
from hinoki.utils import utc_now

SAMPLES = 7
LOOPS = 20_000


def timed(call: Any) -> dict[str, float]:
    for _ in range(LOOPS // 4):
        call()
    samples = []
    for _ in range(SAMPLES):
        start = time.perf_counter()
        for _ in range(LOOPS):
            call()
        samples.append((time.perf_counter() - start) / LOOPS * 1e6)
    mean = statistics.mean(samples)
    return {
        "median_us": round(statistics.median(samples), 3),
        "cv_pct": round(statistics.stdev(samples) / mean * 100, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    author = app.AuthorOut(id=uuid4(), name="Author 001")
    post = app.PostOut(
        id=uuid4(),
        title="Post number 1",
        body="Body of post 1. " * 12,
        published=True,
        created_at=utc_now(),
        author=author,
    )
    values = {
        "id": uuid4(),
        "author_id": uuid4(),
        "title": "Post number 1",
        "body": "Body of post 1. " * 12,
        "published": True,
        "created_at": utc_now(),
    }
    one_id = uuid4()
    result = {
        "python": platform.python_version(),
        "machine": platform.platform(),
        "samples": SAMPLES,
        "loops": LOOPS,
        "dump": timed(post.dump),
        "encode": timed(lambda: core.encode_json(post)),
        "uuid_text": timed(lambda: str(one_id)),
        "from_row": timed(lambda: app.Post.from_row(values)),
    }
    text = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w") as handle:
            handle.write(text + "\n")
    print(text)


main()
