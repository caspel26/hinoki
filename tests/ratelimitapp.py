"""An application with the built-in limiter on a shared SQLite file, for tests that
serve it. ``RATE_DATABASE`` names the file; its table is created by the test, never
by the application. Every ``create_app()`` call builds fresh limiters."""

import os
from collections.abc import Sequence
from pathlib import Path

from hinoki import Hinoki, RateLimit, Request, http
from hinoki.db import DatabaseModule
from hinoki.events import Handlers
from hinoki.ratelimit import DatabaseLimits, MemoryLimits
from hinoki.settings import AppSettings


def create_app(
    settings: AppSettings | None = None, handlers: Sequence[Handlers] = ()
) -> Hinoki:
    absent = DatabaseLimits(table="absent_table")

    @http.get("/shared", rate_limit=RateLimit(3, 3600, headers=True))
    async def shared() -> dict[str, bool]:
        return {"ok": True}

    @http.get("/python", rate_limit=RateLimit(2, 3600, headers=True))
    async def python(request: Request) -> dict[str, bool]:
        return {"ok": True}

    @http.get("/closed", rate_limit=RateLimit(3, 3600, storage=absent))
    async def closed() -> dict[str, bool]:
        return {"ok": True}

    @http.get("/open", rate_limit=RateLimit(3, 3600, storage=absent, on_error="open"))
    async def open_route() -> dict[str, bool]:
        return {"ok": True}

    @http.get("/memory", rate_limit=RateLimit(2, 3600, storage=MemoryLimits()))
    async def memory() -> dict[str, bool]:
        return {"ok": True}

    @http.get("/sync", rate_limit=RateLimit(2, 3600, storage=MemoryLimits()))
    def blocking() -> dict[str, bool]:
        return {"ok": True}

    url = f"sqlite://{Path(os.environ['RATE_DATABASE'])}"
    return Hinoki(
        include=[shared, python, closed, open_route, memory, blocking],
        modules=[DatabaseModule(url, max_size=4)],
        events=list(handlers),
        settings=settings or AppSettings(),
    )
