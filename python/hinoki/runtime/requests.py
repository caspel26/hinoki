"""Reading and authorising one request."""

from __future__ import annotations

from collections.abc import MutableMapping
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from hinoki import core
from hinoki.auth import Caller, Credentials
from hinoki.blocking import call_hook
from hinoki.context import request_id
from hinoki.errors import InvalidBody
from hinoki.httperrors import Forbidden, Unauthorized
from hinoki.request import Request
from hinoki.runtime.binding import (
    MISSING,
    Binding,
    RequestData,
    parse_query,
)
from hinoki.runtime.responses import HTTPError

MAX_BODY = 1024 * 1024
Scope = MutableMapping[str, Any]


async def read_body(receive: Any, limit: int = MAX_BODY) -> bytes:
    """Collect the body, refusing anything above ``limit`` bytes."""
    chunks: list[bytes] = []
    size = 0
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            raise InvalidBody("disconnected", "the client disconnected")
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large")
        chunks.append(chunk)
        more = message.get("more_body", False)
    return b"".join(chunks)


def decode_params(params: dict[str, str]) -> dict[str, str]:
    """Percent-decode parameter values after routing on the raw path, so an
    encoded ``/`` stays inside one segment."""
    try:
        return {k: unquote(v, errors="strict") for k, v in params.items()}
    except UnicodeDecodeError:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_path_encoding") from None


def header_map(scope: Scope) -> dict[str, str]:
    """Header values by lower-case name (joined in Rust). Repeated headers are
    joined with ``", "`` (HTTP list semantics), so a doubled credential header
    never resolves to just one of the values."""
    return core.header_dict(scope.get("headers", []))


async def authorize(
    scope: Scope, binding: Binding, headers: dict[str, str]
) -> Caller[Any] | None:
    """Authenticate and check scopes; public actions skip both."""
    access = binding.route.access
    if access is None:
        return None
    query = parse_query(scope.get("query_string", b""))
    principal: Caller[Any] | None = await call_hook(
        access.auth.authenticate, Credentials(headers, query)
    )
    if principal is None:
        challenge = access.auth.challenge
        raise Unauthorized(headers={"www-authenticate": challenge} if challenge else {})
    if not access.permissions <= principal.permissions:
        raise Forbidden()
    return principal


def build_request(
    scope: Scope,
    binding: Binding,
    path: dict[str, str],
    headers: dict[str, str],
    raw_body: bytes,
    principal: Caller[Any] | None,
) -> tuple[Request, RequestData]:
    """The request object for the handler and the data its parameters bind."""
    query = parse_query(scope.get("query_string", b""))
    client = scope.get("client")
    host = headers.get("host") or ":".join(
        str(part) for part in scope.get("server") or ()
    )
    request = Request(
        method=scope["method"],
        path=scope["path"],
        scheme=scope.get("scheme", "http"),
        host=host,
        client=tuple(client) if client else None,
        headers=headers,
        query=query,
        body=raw_body,
        path_params=path,
        user=principal,
        request_id=request_id.get() or "",
        parsers=binding.route.parsers,
    )
    body: object = request.data if binding.needs_body else MISSING
    return request, RequestData(path, query, body, principal)
