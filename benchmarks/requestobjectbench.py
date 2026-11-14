"""Cost of building the Request object per call (Python class), in isolation.

Builds the request from a typical ASGI scope the way the adapter does, with and
without the cookie header, and reports median/CV. It decides whether a native
``#[pyclass]`` Request is worth its complexity (REQUEST-DESIGN S6).
Run: ``uv run --locked python benchmarks/requestobjectbench.py``.
"""

import statistics
import time

from hinoki import Hinoki, Request, http
from hinoki.runtime.binding import bind
from hinoki.runtime.requests import build_request, header_map

SAMPLES = 7
LOOPS = 20000


@http.get("/items/{item_id}")
def item(request: Request, item_id: int) -> dict[str, int]:
    return {"id": item_id}


APP = Hinoki()
APP.include(item)
ROUTE = APP.resolve("GET", "/items/1")
assert ROUTE is not None
BINDING = bind(ROUTE[0])
HEADERS = [
    (b"host", b"example.test"),
    (b"user-agent", b"bench/1.0"),
    (b"accept", b"application/json"),
    (b"accept-encoding", b"gzip"),
    (b"cookie", b"session=abc123; theme=dark"),
]
SCOPE = {
    "type": "http",
    "method": "GET",
    "path": "/items/1",
    "scheme": "http",
    "query_string": b"expand=author&page=2",
    "headers": HEADERS,
    "client": ("127.0.0.1", 5000),
    "server": ("127.0.0.1", 8000),
}


def build() -> object:
    return build_request(SCOPE, BINDING, {"item_id": "1"}, header_map(SCOPE), b"", None)


def sample() -> float:
    start = time.perf_counter()
    for _ in range(LOOPS):
        build()
    return (time.perf_counter() - start) / LOOPS * 1e6


data = [sample() for _ in range(SAMPLES)]
cv = statistics.stdev(data) / statistics.mean(data) * 100
print(
    f"build Request + RequestData: {statistics.median(data):.2f} us/call  cv {cv:.1f}%"
)
