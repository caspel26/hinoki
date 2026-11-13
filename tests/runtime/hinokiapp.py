"""Real Hinoki application used by the server-level runtime tests.

Behaviour is driven by environment variables; events go to EXP_LOG.
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from hinoki import Hinoki, Module, ModuleContext, RuntimeConfig, get
from hinoki.request import Request

LOG = os.environ.get("EXP_LOG", os.devnull)
FAIL = os.environ.get("EXP_FAIL", "")


def record(event: str) -> None:
    with open(LOG, "a") as handle:
        handle.write(f"{time.time():.3f} {os.getpid()} {event}\n")


class Recording(Module):
    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        record(f"start:{self.name}")
        if self.name == FAIL:
            raise RuntimeError("boom")
        try:
            yield
        finally:
            record(f"stop:{self.name}")


class Alpha(Recording):
    name = "alpha"


class Beta(Recording):
    name = "beta"
    requires = (Alpha,)


class Gamma(Recording):
    name = "gamma"
    requires = (Beta,)


@get("/fast")
async def fast(request: Request) -> dict[str, int]:
    return {"pid": os.getpid()}


@get("/echo/{name}")
async def echo(request: Request, name: str) -> dict[str, str]:
    return {"name": name}


@get("/sync-slow")
def sync_slow(request: Request) -> dict[str, int]:
    time.sleep(1.0)
    return {"pid": os.getpid()}


@get("/async-slow")
async def async_slow(request: Request) -> dict[str, int]:
    record("slow-start")
    await asyncio.sleep(1.5)
    record("slow-end")
    return {"pid": os.getpid()}


app = Hinoki(runtime=RuntimeConfig(sync_workers=2, sync_queue=0))
app.include(fast, echo, sync_slow, async_slow)
app.install(Gamma(), Alpha(), Beta())
