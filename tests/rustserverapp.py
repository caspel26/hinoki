"""An importable application for running the Rust server in a subprocess."""

import asyncio

from hinoki import Hinoki, http
from hinoki.http.request import Request


@http.get("/health")
async def health(request: Request) -> dict[str, str]:
    return {"status": "ok"}


@http.get("/slow")
async def slow(request: Request) -> dict[str, str]:
    await asyncio.sleep(0.5)
    return {"slow": "done"}


app = Hinoki()
app.include(health, slow)
