"""Schema validation: the Rust engine against the pure-Python oracle.

Same schema, same decoded JSON, repeated validate calls; prints median and CV.
Run: ``PYTHONPATH=. uv run --locked python benchmarks/schemabench.py``.
"""

import json
import statistics
import time
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from hinoki import Field, Schema
from hinoki import native as nat
from hinoki.fieldtypes import FieldType
from hinoki.schema import encode
from tests.oracle.validation import Validator

SAMPLES = 7
LOOPS = 5000


class Tag(Schema):
    label: Annotated[str, Field(min_length=1, max_length=20)]
    weight: Annotated[int, Field(ge=0, le=100)]


class Post(Schema):
    id: UUID
    title: Annotated[str, Field(min_length=1, max_length=160)]
    slug: Annotated[str, Field(pattern=r"[a-z0-9]+(-[a-z0-9]+)*")]
    body: str
    published: bool
    created: datetime
    rating: float
    tags: list[Tag]


DATA: dict[str, Any] = {
    "id": "7f9c4a5e-3c1d-4a2b-9b0d-1e2f3a4b5c6d",
    "title": "A reasonably typical title for a post",
    "slug": "a-reasonably-typical-title",
    "body": "lorem ipsum " * 40,
    "published": True,
    "created": "2026-11-13T10:00:00Z",
    "rating": 4.5,
    "tags": [{"label": f"tag{i}", "weight": i} for i in range(10)],
}


def oracle() -> object:
    return Validator().schema(FieldType("schema", schema=Post), DATA, "")


def native() -> object:
    return Post.validate(DATA)


def sample(call: Callable[[], object]) -> float:
    start = time.perf_counter()
    for _ in range(LOOPS):
        call()
    return (time.perf_counter() - start) / LOOPS * 1e6


def report(name: str, call: Callable[[], object]) -> float:
    data = [sample(call) for _ in range(SAMPLES)]
    median = statistics.median(data)
    cv = statistics.stdev(data) / statistics.mean(data) * 100
    print(f"{name:8} {median:8.2f} us/call  cv {cv:5.1f}%")
    return median


INSTANCE = Post.validate(DATA)


def encode_python() -> object:
    return json.dumps(encode(INSTANCE)).encode()


def encode_native() -> object:
    return nat.encode_json(INSTANCE)


native()  # compile once, outside the timing
slow = report("python", oracle)
fast = report("rust", native)
print(f"ratio    {slow / fast:8.2f}x")
slow = report("enc py", encode_python)
fast = report("enc rust", encode_native)
print(f"ratio    {slow / fast:8.2f}x")
