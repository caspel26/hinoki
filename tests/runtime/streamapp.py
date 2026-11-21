"""A Hinoki application with streamed responses for the Granian tests; events go
to EXP_LOG."""

import asyncio
import os
import time
from collections.abc import AsyncIterator

from hinoki import Hinoki, http
from hinoki.http import Event, EventStream, File, Stream

LOG = os.environ.get("EXP_LOG", os.devnull)
FILE = os.environ.get("EXP_FILE", "")


def record(event: str) -> None:
    with open(LOG, "a") as handle:
        handle.write(f"{time.time():.3f} {os.getpid()} {event}\n")


@http.get("/pieces")
async def pieces() -> Stream:
    async def source() -> AsyncIterator[str]:
        yield "first;"
        await asyncio.sleep(1.0)
        yield "last;"

    return Stream(source(), content_type="text/plain")


@http.get("/endless")
async def endless() -> Stream:
    async def source() -> AsyncIterator[bytes]:
        try:
            while True:
                yield b"x" * 65536
        finally:
            record("endless:closed")

    return Stream(source())


@http.get("/events")
async def events() -> EventStream:
    async def source() -> AsyncIterator[Event]:
        yield Event("hello")
        await asyncio.sleep(30)

    return EventStream(source(), keepalive=0.2)


@http.get("/file")
async def file() -> File:
    return File(FILE, filename="data.bin")


@http.get("/ready")
async def ready() -> dict[str, str]:
    return {"ok": "yes"}


app = Hinoki()
app.include(pieces, endless, events, file, ready)
