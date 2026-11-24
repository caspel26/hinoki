"""bombardier command line and result parsing, shared by run.py and report.py.

bombardier (https://github.com/codesenberg/bombardier) is run with a fixed number
of requests (``-n``) or a fixed duration (``-d``) and prints one JSON document;
latency values are in microseconds. Requests per second are the completed
responses divided by the elapsed time of the whole sample, not bombardier's mean
of one-second rates.
"""

import json
from pathlib import Path

PERCENTILES = {"p50": "50", "p95": "95", "p99": "99"}
CLASSES = ("req1xx", "req2xx", "req3xx", "req4xx", "req5xx", "others")


def command(  # noqa: PLR0913 - one value per bombardier option
    binary: str,
    url: str,
    method: str,
    body: Path | None,
    connections: int,
    *,
    requests: int | None = None,
    seconds: int | None = None,
    headers: tuple[str, ...] = (),
) -> list[str]:
    if (requests is None) == (seconds is None):
        raise ValueError("give exactly one of requests or seconds")
    stop = ["-n", str(requests)] if requests is not None else ["-d", f"{seconds}s"]
    cmd = [binary, "-c", str(connections), *stop, "-m", method, "-l", "-o", "json",
           "-p", "r"]  # fmt: skip
    for header in headers:
        cmd += ["-H", header]
    if body:
        cmd += ["-H", "content-type: application/json", "-f", str(body)]
    return [*cmd, url]


def parse(text: str) -> dict:
    """The decoded bombardier result; anything else is a failed run."""
    try:
        document = json.loads(text)
        summarize(document)
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        raise ValueError("output is not a bombardier JSON result") from None
    return document


def summarize(document: dict) -> dict:
    """Requests per second, latency percentiles in ms and non-2xx/failed count."""
    result = document["result"]
    completed = sum(int(result.get(name, 0)) for name in CLASSES)
    percentiles = result["latency"]["percentiles"]
    return {
        "completed": completed,
        "rps": completed / float(result["timeTakenSeconds"]),
        "latency_ms": {
            name: float(percentiles[key]) / 1000 for name, key in PERCENTILES.items()
        },
        "wrong": completed - int(result.get("req2xx", 0)),
    }
