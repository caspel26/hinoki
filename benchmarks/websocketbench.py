"""Benchmark: WebSocket echo round trips and plain HTTP GETs through Kiso.

The server (``KisoWorker``) and the client share one process and one event
loop, so the figures compare runs of the same script on the same host; they say
nothing about a deployed system. ``--mode http`` runs without touching the
WebSocket code, which is how a build that has it is compared with one that
does not. Run: ``uv run --locked python benchmarks/websocketbench.py``.
Prints, for each of ``--samples`` runs, the figures of that run and then the
median and the spread.
"""

import argparse
import asyncio
import base64
import os
import platform
import statistics
import sys
import time

from hinoki import Hinoki, http
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings

# A build without WebSocket support is the http baseline.
HAS_WEBSOCKET = hasattr(http, "websocket")

TEXT = 1


@http.get("/plain")
async def plain() -> dict[str, str]:
    return {"ok": "yes"}


def application() -> Hinoki:
    routes = [plain]
    if HAS_WEBSOCKET:

        @http.websocket("/echo")
        async def echo(socket: http.WebSocket) -> None:
            async for message in socket:
                await socket.send_text(message)

        routes.append(echo)
    # A served WebSocket app refuses allowed_hosts "*"; the client sends 127.0.0.1.
    return Hinoki(settings=AppSettings(allowed_hosts=("127.0.0.1",)), include=routes)


def frame(payload: bytes) -> bytes:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes([0x80 | TEXT, 0x80 | len(payload)]) + mask + masked


async def handshake(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write(
        (
            f"GET /echo HTTP/1.1\r\nhost: 127.0.0.1:{port}\r\nconnection: upgrade\r\n"
            f"upgrade: websocket\r\nsec-websocket-version: 13\r\n"
            f"sec-websocket-key: {key}\r\n\r\n"
        ).encode()
    )
    head = await reader.readuntil(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 101"), head
    return reader, writer


async def echo_once(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.write(frame(b"x" * 64))
    head = await reader.readexactly(2)
    await reader.readexactly(head[1] & 0x7F)


async def echo_sample(port: int, clients: int, rounds: int) -> tuple[float, float]:
    """Messages per second over all clients, and the median round trip in us."""
    sockets = [await handshake(port) for _ in range(clients)]
    latencies: list[float] = []

    async def drive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        for _ in range(rounds):
            start = time.perf_counter()
            await echo_once(reader, writer)
            latencies.append(time.perf_counter() - start)

    start = time.perf_counter()
    await asyncio.gather(*(drive(r, w) for r, w in sockets))
    elapsed = time.perf_counter() - start
    for _, writer in sockets:
        writer.close()
    return clients * rounds / elapsed, statistics.median(latencies) * 1e6


async def get_once(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.write(b"GET /plain HTTP/1.1\r\nhost: bench\r\n\r\n")
    head = await reader.readuntil(b"\r\n\r\n")
    length = int(
        next(
            line.split(b":")[1]
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"content-length")
        )
    )
    await reader.readexactly(length)


async def http_sample(port: int, clients: int, rounds: int) -> tuple[float, float]:
    """Requests per second over keep-alive connections, and the median in us."""
    sockets = [await asyncio.open_connection("127.0.0.1", port) for _ in range(clients)]
    latencies: list[float] = []

    async def drive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        for _ in range(rounds):
            start = time.perf_counter()
            await get_once(reader, writer)
            latencies.append(time.perf_counter() - start)

    start = time.perf_counter()
    await asyncio.gather(*(drive(r, w) for r, w in sockets))
    elapsed = time.perf_counter() - start
    for _, writer in sockets:
        writer.close()
    return clients * rounds / elapsed, statistics.median(latencies) * 1e6


async def main(args: argparse.Namespace) -> None:
    if args.mode == "ws" and not HAS_WEBSOCKET:
        sys.exit("this build has no WebSocket support: use --mode http")
    worker = KisoWorker(application(), "127.0.0.1", 0)
    port = await worker.start()
    run = echo_sample if args.mode == "ws" else http_sample
    try:
        await run(port, args.clients, 200)  # warm-up, not counted
        rates: list[float] = []
        medians: list[float] = []
        for number in range(1, args.samples + 1):
            rate, median = await run(port, args.clients, args.rounds)
            rates.append(rate)
            medians.append(median)
            print(f"sample {number}: {rate:,.0f}/s, median {median:,.0f} us")
    finally:
        await worker.stop()
    print(
        f"{args.mode}: median {statistics.median(rates):,.0f}/s "
        f"(min {min(rates):,.0f}, max {max(rates):,.0f}) over {len(rates)} samples; "
        f"{args.clients} clients, {args.rounds} rounds each; "
        f"python {platform.python_version()}, {platform.system()} {platform.machine()}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["ws", "http"], default="ws")
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--rounds", type=int, default=500)
    asyncio.run(main(parser.parse_args()))
