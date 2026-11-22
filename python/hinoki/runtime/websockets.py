"""Admitting and running a WebSocket action, for both servers.

The handshake is an HTTP request: access, subprotocol and parameters are
judged before the connection is upgraded, so a refusal is an ordinary HTTP
answer. After the upgrade the handler runs on the application's event loop,
and the connection is closed with the code that says how it ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from hinoki.auth import Caller
from hinoki.runtime.binding import parse_query
from hinoki.runtime.requests import authorize
from hinoki.runtime.responses import HTTPError
from hinoki.websocket import (
    CloseCode,
    Param,
    Transport,
    WebSocket,
    WebSocketClosed,
    WebSocketRoute,
    convert,
)

log = logging.getLogger("hinoki.server")


@dataclass(frozen=True, slots=True)
class Admitted:
    """A handshake that passed: the handler arguments and the subprotocol."""

    kwargs: dict[str, object]
    subprotocol: str | None


def offered(headers: dict[str, str]) -> list[str]:
    """The subprotocols the client offered, in its order of preference."""
    value = headers.get("sec-websocket-protocol", "")
    return [name.strip() for name in value.split(",") if name.strip()]


def choose(route: WebSocketRoute, headers: dict[str, str]) -> tuple[str | None, Any]:
    """The first offered subprotocol the action speaks, with its ``Access``. An
    action that declares some refuses a client that offers none of them."""
    if not route.subprotocols:
        return None, None
    speaks = dict(route.subprotocols)
    for name in offered(headers):
        if name in speaks:
            return name, speaks[name]
    raise HTTPError(HTTPStatus.BAD_REQUEST, "unsupported_subprotocol")


def invalid(name: str, why: str) -> HTTPError:
    return HTTPError(
        HTTPStatus.BAD_REQUEST, "invalid_parameter", parameter=name, message=why
    )


def value_of(
    param: Param, params: dict[str, str], query: dict[str, list[str]]
) -> object:
    source = params if param.path else query
    found = source.get(param.name)
    if isinstance(found, list):
        if len(found) > 1:
            raise invalid(param.name, "given more than once")
        found = found[0] if found else None
    if found is None:
        if param.required:
            raise invalid(param.name, "is required")
        return param.default
    try:
        return convert(param, found)
    except ValueError:
        raise invalid(param.name, f"is not a valid {param.hint.__name__}") from None


async def admit(
    route: WebSocketRoute,
    headers: dict[str, str],
    query_string: bytes,
    params: dict[str, str],
) -> Admitted:
    """Authenticate, choose the subprotocol and bind the arguments. Raises what
    an HTTP action would: ``Unauthorized``, ``Forbidden``, ``HTTPError``."""
    query = parse_query(query_string)
    caller: Caller[Any] | None = None
    if route.access is not None:
        caller = await authorize(route.access, headers, query)
    subprotocol, sub_access = choose(route, headers)
    if sub_access is not None:
        caller = await authorize(sub_access, headers, query)
    kwargs = {param.name: value_of(param, params, query) for param in route.params}
    if route.caller is not None:
        kwargs[route.caller] = caller
    return Admitted(kwargs, subprotocol)


async def run(
    route: WebSocketRoute, transport: Transport, admitted: Admitted, path: str
) -> None:
    """Run the handler, then close with the code that fits how it ended: 1000 on
    return, 1011 when it raised. A peer that went away (``WebSocketClosed``) is
    no error."""
    socket = WebSocket(
        transport,
        path=path,
        subprotocol=admitted.subprotocol,
        message=route.message,
        reply=route.reply,
    )
    code, reason = CloseCode.NORMAL, ""
    try:
        await route.handler(**admitted.kwargs, **{route.socket: socket})
    except WebSocketClosed:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("WebSocket %s failed", route.name)
        code, reason = CloseCode.INTERNAL_ERROR, "internal error"
    with contextlib.suppress(Exception):
        await transport.close(int(code), reason)
