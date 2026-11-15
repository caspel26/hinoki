"""ASGI adapter: the lifespan protocol and the HTTP message exchange for one
worker.

Everything that decides an answer (routing, binding, policies, handlers, error
mapping) lives in ``Pipeline``. The ASGI boundary crosses Python objects; this
is not a native-only path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from hinoki.errors import ShutdownError, StartupError
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.requests import Scope, read_body

if TYPE_CHECKING:
    from hinoki.app import Hinoki

log = logging.getLogger("hinoki.runtime")


class ASGIBody:
    """The request body read through the ASGI ``receive`` callable."""

    __slots__ = ("receive",)

    def __init__(self, receive: Any) -> None:
        self.receive = receive

    async def read(self, limit: int) -> bytes:
        return await read_body(self.receive, limit)


class ASGIAdapter:
    """One adapter per worker process; it owns that worker's pipeline."""

    def __init__(self, app: Hinoki) -> None:
        self.pipeline = Pipeline(app)
        self.lifespan = self.pipeline.lifespan

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "http":
            await self._http(scope, receive, send)
        else:
            raise RuntimeError(f"unsupported scope type {scope['type']!r}")

    async def _lifespan(self, receive: Any, send: Any) -> None:
        finished = False
        while not finished:
            kind = (await receive())["type"]
            if kind == "lifespan.startup":
                finished = await self._startup(send)
            elif kind == "lifespan.shutdown":
                await self._shutdown(send)
                finished = True

    async def _startup(self, send: Any) -> bool:
        """Answer startup; return True when the protocol loop must end."""
        if self.pipeline.logging is not None:
            self.pipeline.logging.apply()
        if self.pipeline.debug:
            log.warning("debug is on: error responses include tracebacks")
        try:
            await self.lifespan.startup()
        except StartupError as exc:
            log.error("startup failed", exc_info=exc.cause)
            await send({"type": "lifespan.startup.failed", "message": str(exc)})
            return True
        await send({"type": "lifespan.startup.complete"})
        return False

    async def _shutdown(self, send: Any) -> None:
        try:
            await self.lifespan.shutdown()
        except ShutdownError as exc:
            log.error("shutdown errors", exc_info=exc)
            await send({"type": "lifespan.shutdown.failed", "message": str(exc)})
        else:
            await send({"type": "lifespan.shutdown.complete"})

    async def _http(self, scope: Scope, receive: Any, send: Any) -> None:
        response = await self.pipeline.handle(scope, ASGIBody(receive))
        start = {"type": "http.response.start", "status": response.status}
        await send({**start, "headers": response.headers})
        body = b"" if scope["method"] == "HEAD" else response.body
        await send({"type": "http.response.body", "body": body})
