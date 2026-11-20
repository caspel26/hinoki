"""Response values and the mapping from exceptions to HTTP answers."""

from __future__ import annotations

import logging
import traceback
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from typing import Any

from hinoki import core
from hinoki.context import request_id
from hinoki.errors import APIError, ErrorRegistry, InvalidBody, ValidationError

log = logging.getLogger("hinoki.runtime")

Headers = list[tuple[bytes, bytes]]
RETRY_LATER: Headers = [(b"retry-after", b"1")]
BODYLESS = {HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED}
INVALID_BODY_STATUS = {
    "payload_too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    "unsupported_media_type": HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
}


@dataclass(slots=True)
class Response:
    status: int
    body: bytes = b""
    headers: Headers = field(default_factory=list)

    @classmethod
    def rendered(
        cls, status: int, body: bytes, media_type: str, vary: bool
    ) -> Response:
        head: Headers = [(b"content-type", media_type.encode())]
        if vary:
            head.append((b"vary", b"accept"))
        return cls(status, body, head)

    @classmethod
    def json(
        cls, status: int, payload: object, headers: Headers | None = None
    ) -> Response:
        head: Headers = [(b"content-type", b"application/json"), *(headers or [])]
        return cls(status, core.encode_json(payload), head)


class HTTPError(Exception):
    """Raised inside request handling to answer with a specific response."""

    def __init__(
        self, status: int, error: str, headers: Headers | None = None, **extra: object
    ) -> None:
        super().__init__(error)
        body = {"error": error, "request_id": request_id.get(), **extra}
        self.response = Response.json(status, body, headers)


def public_error(error: APIError) -> Response:
    extra: dict[str, Any] = {"message": error.message}
    if error.details is not None:
        extra["details"] = error.details
    return HTTPError(
        error.status, error.code, checked_headers(error.headers), **extra
    ).response


def checked_headers(headers: dict[str, str]) -> list[tuple[bytes, bytes]]:
    """The headers of an ``APIError`` that pass the same checks as a ``Response``'s
    (no control characters, no framing headers); the others are dropped and logged."""
    # A local import: hinoki.http.response imports this module.
    from hinoki.http.response import encode_header  # noqa: PLC0415

    kept: list[tuple[bytes, bytes]] = []
    for name, value in headers.items():
        try:
            kept.append(encode_header(name, value))
        except ValueError as problem:
            log.warning("error header dropped: %s", problem)
    return kept


def builtin_error(exc: Exception) -> Response | None:
    """Answers for the framework's own request errors."""
    if isinstance(exc, HTTPError):
        return exc.response
    if isinstance(exc, ValidationError):
        details = [asdict(d) for d in exc.details]
        status = HTTPStatus.UNPROCESSABLE_ENTITY
        return HTTPError(status, "validation_error", details=details).response
    if isinstance(exc, InvalidBody):
        status = INVALID_BODY_STATUS.get(exc.code, HTTPStatus.BAD_REQUEST)
        return HTTPError(status, exc.code, message=exc.message).response
    return None


def debug_details(exc: Exception) -> dict[str, Any]:
    """What ``debug=True`` adds to the answer for an unhandled exception."""
    kind = type(exc)
    return {
        "exception": f"{kind.__module__}.{kind.__qualname__}",
        "message": str(exc),
        "traceback": traceback.format_exception(exc),
    }


async def error_response(
    exc: Exception, rid: str, registry: ErrorRegistry, debug: bool = False
) -> Response:
    """The HTTP answer for any exception raised while serving a request. With
    ``debug`` an unhandled exception is also described in the body."""
    known = builtin_error(exc)
    if known is not None:
        return known
    public = exc if isinstance(exc, APIError) else await registry.translate(exc)
    if public is not None:
        return public_error(public)
    log.error("unhandled error in request %s", rid, exc_info=exc)
    status = HTTPStatus.INTERNAL_SERVER_ERROR
    if debug:
        details = debug_details(exc)
        return HTTPError(
            status, "internal_error", request_id=rid, debug=details
        ).response
    return HTTPError(status, "internal_error", request_id=rid).response
