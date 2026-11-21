"""Response compression of Kiso, over HTTP on loopback: size and time, on and off.

The Rust server runs in this process with ``compression`` off or on. A blocking
client in a thread sends sequential requests on one keep-alive connection.
Scenarios:

* ``small``   a JSON body of about 13 KB (compressed eagerly),
* ``stream``  sixteen distinct pieces of about 70 KB of generated JSON text
              (compressed as it is read, with no ``content-length``).

    uv run --locked python benchmarks/compressionbench.py [--rounds 5] [--requests 300]

Prints one JSON object: per scenario and coding the body bytes the client
received (chunk framing included for ``stream``),
and per scenario the median time per request in milliseconds over the rounds
with the coefficient of variation. Modes alternate every round, so drift in the
host hits both. The numbers come from a synthetic body and one machine; they
say nothing about other frameworks, bodies or hardware.
"""

import argparse
import asyncio
import json
import logging
import socket
import statistics
import time
from collections.abc import AsyncIterator

from hinoki import Hinoki, http
from hinoki.http import Response, Stream
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings, CompressionSettings

ROWS = [{"id": number, "name": f"item {number}"} for number in range(400)]
PIECES = [
    json.dumps(
        [
            {"id": n, "name": f"item {n}"}
            for n in range(piece * 2000, (piece + 1) * 2000)
        ]
    ).encode()
    for piece in range(16)
]
SCENARIOS = {"small": "/small", "stream": "/stream"}
CODINGS = (None, "gzip", "br")


@http.get("/small")
async def small_route() -> Response:
    return Response(
        200, headers={"content-type": "application/json"}, body=json.dumps(ROWS)
    )


@http.get("/stream")
async def stream_route() -> Stream:
    async def source() -> AsyncIterator[bytes]:
        for piece in PIECES:
            yield piece

    return Stream(source(), content_type="application/json")


def application(enabled: bool) -> tuple[Hinoki, dict[str, object] | None]:
    compression = CompressionSettings(enabled=enabled)
    app = Hinoki(settings=AppSettings(compression=compression))
    app.include(small_route, stream_route)
    return app, compression.native_options()


def read_response(sock: socket.socket, buffer: bytearray) -> int:
    """Read one response from ``sock``; its body size in bytes."""
    while b"\r\n\r\n" not in buffer:
        buffer += sock.recv(65536)
    head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
    lowered = head.lower()
    if b"transfer-encoding: chunked" in lowered:
        while not rest.endswith(b"0\r\n\r\n"):
            rest += sock.recv(65536)
        buffer.clear()
        return len(rest)
    length = int(lowered.split(b"content-length: ")[1].split(b"\r\n")[0])
    while len(rest) < length:
        rest += sock.recv(65536)
    buffer.clear()
    return length


def request_loop(port: int, target: str, coding: str | None, count: int) -> list[float]:
    extra = f"accept-encoding: {coding}\r\n" if coding else ""
    raw = f"GET {target} HTTP/1.1\r\nhost: bench\r\n{extra}\r\n".encode()
    times = []
    with socket.create_connection(("127.0.0.1", port)) as sock:
        buffer = bytearray()
        for _ in range(count):
            started = time.perf_counter()
            sock.sendall(raw)
            read_response(sock, buffer)
            times.append(time.perf_counter() - started)
    return times


def sizes(port: int) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for name, target in SCENARIOS.items():
        found[name] = {}
        for coding in CODINGS:
            extra = f"accept-encoding: {coding}\r\n" if coding else ""
            raw = f"GET {target} HTTP/1.1\r\nhost: bench\r\n{extra}\r\n".encode()
            with socket.create_connection(("127.0.0.1", port)) as sock:
                sock.sendall(raw)
                found[name][coding or "identity"] = read_response(sock, bytearray())
    return found


async def measure(enabled: bool, coding: str | None, requests: int) -> dict[str, float]:
    app, options = application(enabled)
    worker = KisoWorker(app, "127.0.0.1", 0, compression=options)
    port = await worker.start()
    try:
        result = {}
        for name, target in SCENARIOS.items():
            times = await asyncio.to_thread(
                request_loop, port, target, coding, requests
            )
            result[name] = statistics.median(times) * 1000
        return result
    finally:
        await worker.stop()


async def body_sizes() -> dict[str, dict[str, int]]:
    app, options = application(True)
    worker = KisoWorker(app, "127.0.0.1", 0, compression=options)
    port = await worker.start()
    try:
        return await asyncio.to_thread(sizes, port)
    finally:
        await worker.stop()


def summary(samples: list[float]) -> dict[str, float]:
    median = statistics.median(samples)
    spread = statistics.pstdev(samples) / statistics.mean(samples) * 100
    return {"median_ms": round(median, 4), "cv_percent": round(spread, 1)}


async def run(rounds: int, requests: int) -> dict[str, object]:
    modes = (("off", False, None), ("gzip", True, "gzip"), ("br", True, "br"))
    samples: dict[str, dict[str, list[float]]] = {
        mode: {name: [] for name in SCENARIOS} for mode, _, _ in modes
    }
    for _ in range(rounds):
        for mode, enabled, coding in modes:
            once = await measure(enabled, coding, requests)
            for name, value in once.items():
                samples[mode][name].append(value)
    return {
        "rounds": rounds,
        "requests_per_round": requests,
        "body_bytes": await body_sizes(),
        "per_request": {
            mode: {name: summary(values) for name, values in found.items()}
            for mode, found in samples.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--requests", type=int, default=300)
    args = parser.parse_args()
    logging.getLogger("hinoki.runtime").setLevel(logging.ERROR)
    print(json.dumps(asyncio.run(run(args.rounds, args.requests)), indent=2))


if __name__ == "__main__":
    main()
