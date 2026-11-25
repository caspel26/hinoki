"""Reading and authorising one request."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, MutableMapping
from http import HTTPStatus
from typing import Any, cast

from hinoki import core
from hinoki.access import Access
from hinoki.auth import Caller, Credentials
from hinoki.blocking import call_hook
from hinoki.context import request_id
from hinoki.errors import Forbidden, InvalidBody, Unauthorized
from hinoki.events import AuthRejected, listener
from hinoki.http.request import Headers, Request
from hinoki.parsers import (
    JSONParser,
    Parsed,
    UploadedFile,
    choose,
    is_native_parser,
)
from hinoki.runtime.binding import (
    MISSING,
    Binding,
    RequestData,
    TypedBody,
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
    if not params:
        return {}
    try:
        return core.decode_path_params(params)
    except ValueError:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_path_encoding") from None


def header_map(scope: Scope) -> dict[str, str]:
    """Header values by lower-case name (joined in Rust). Repeated headers are
    joined with ``", "`` (HTTP list semantics), so a doubled credential header
    never resolves to just one of the values."""
    return core.header_dict(scope.get("headers", []))


async def authorize(
    access: Access,
    headers: Mapping[str, str],
    query: dict[str, list[str]],
    *,
    route: str = "",
    method: str = "",
    session: object | None = None,
) -> Caller[Any]:
    """Authenticate and check permissions for a protected action. ``query`` is
    the parsed query string, shared with the request built afterwards. ``route``
    and ``method`` name the action in an ``AuthRejected`` event."""
    auth = access.auth
    creds = Credentials(headers, query, session)
    try:
        if (
            access._auth_is_async
            and type(auth).authenticate is access._auth_fn
            and "authenticate" not in auth.__dict__
        ):
            principal = await cast(
                "Awaitable[Caller[Any] | None]", auth.authenticate(creds)
            )
        else:
            principal = await call_hook(auth.authenticate, creds)
    except Unauthorized:
        await rejected("invalid", route, method)
        raise
    if principal is None:
        await rejected("missing", route, method)
        challenge = access.auth.challenge
        raise Unauthorized(headers={"www-authenticate": challenge} if challenge else {})
    if not access.permissions <= principal.permissions:
        await rejected("forbidden", route, method)
        raise Forbidden()
    return principal


async def rejected(reason: str, route: str, method: str) -> None:
    """Tell the application's ``AuthRejected`` handlers, when it has any; the
    answer to the request does not depend on them."""
    bus = listener(AuthRejected)
    if bus is not None:
        await bus.notify(AuthRejected(reason, route, method))


def body_value(request: Request, text: bool) -> object:
    """What the body parameter reads: the parsed data, or for a form-sourced
    one the text fields of a form body."""
    if text and request.accepts_form():
        return request.parsed().fields
    return request.data


def plain_data(
    scope: Scope,
    path: dict[str, str],
    principal: Caller[Any] | None,
    query: dict[str, list[str]] | None = None,
) -> RequestData:
    """What the parameters of an action bind when it never reads the request
    object (``Binding.needs_request`` is false): no body, no files."""
    if query is None:
        query = parse_query(scope.get("query_string", b""))
    return RequestData(path, query, MISSING, principal, {})


def build_request(
    scope: Scope,
    binding: Binding,
    path: dict[str, str],
    headers: dict[str, str],
    raw_body: bytes,
    principal: Caller[Any] | None,
    query: dict[str, list[str]] | None = None,
) -> tuple[Request, RequestData]:
    """The request object for the handler and the data its parameters bind.
    ``headers`` come from ``header_map``; ``query`` is passed when
    authentication already parsed it."""
    if query is None:
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
        headers=Headers._wrap(headers),
        query=query,
        body=raw_body,
        path_params=path,
        user=principal,
        request_id=request_id.get() or "",
        parsers=binding.route.parsers,
    )
    body: object = MISSING
    if binding.has_files:
        body, files = upload_body(request, binding.takes_text)
        return request, RequestData(path, query, body, principal, files)
    if binding.needs_body:
        body = typed_body(binding, request)
        if body is None:
            body = body_value(request, binding.takes_text)
    return request, RequestData(path, query, body, principal, {})


def upload_body(
    request: Request, takes_text: bool
) -> tuple[object, dict[str, list[UploadedFile]]]:
    """The body and the files of an action that takes uploads."""
    content_type = request.content_type
    if not request.body and not content_type:
        # A bodyless optional upload is a useful empty input. Cache it so
        # Request.data/POST/FILES observe the same parsed result.
        request.cached = Parsed({})
    elif content_type != "multipart/form-data":
        raise InvalidBody("unsupported_media_type", "expected multipart/form-data")
    body = body_value(request, takes_text)
    return body, {name: request.FILES.getlist(name) for name in request.FILES}


def typed_body(binding: Binding, request: Request) -> TypedBody | None:
    """The ``Schema`` body read from the bytes in one Rust pass, or ``None``
    when the parser, the source or the input needs the parse-then-validate
    path (which then reports the same errors)."""
    param = binding.body_param
    if param is None or param.schema is None or param.text:
        return None
    parser = choose(request.parsers, request.content_type)
    if type(parser) is not JSONParser or not is_native_parser(parser):
        return None
    value = param.schema.validate_json(request.body)
    return None if value is None else TypedBody(value)
