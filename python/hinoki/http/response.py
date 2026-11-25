"""An explicit HTTP answer: ``return Response(303, headers={"location": "/x"})``.

A handler returns a ``Response`` when it needs a status, headers (redirects,
cookies) or a body of its own; it bypasses the renderers. Everything is
validated when the ``Response`` is built, so a bad header fails in the handler
(a 500 with a request id) instead of corrupting the response.

An answer that signals failure should be an exception, not a ``Response``: a
``atomic=True`` action commits when the handler returns, whatever the
status of the ``Response``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import copy

from hinoki import core
from hinoki.runtime.responses import BODYLESS

RESERVED = {"content-length", "transfer-encoding", "connection", "x-request-id"}
SAME_SITE = {"Strict", "Lax", "None"}
MIN_STATUS, MAX_STATUS = 200, 599

ResponseHeaders = Mapping[str, str] | Sequence[tuple[str, str]]


def encode_header(name: str, value: str) -> tuple[bytes, bytes]:
    return core.encode_header(name, value)


class Response:
    """Status, headers and body chosen by the handler."""

    __slots__ = ("body", "headers", "status")

    def __init__(
        self,
        status: int = 200,
        body: bytes | str = b"",
        headers: ResponseHeaders = (),
        content_type: str | None = None,
    ) -> None:
        if not MIN_STATUS <= status <= MAX_STATUS:
            raise ValueError(f"invalid HTTP status {status}")
        raw = body.encode() if isinstance(body, str) else bytes(body)
        if raw and status in BODYLESS:
            raise ValueError(f"status {status} cannot have a body")
        items = list(headers.items() if isinstance(headers, Mapping) else headers)
        if content_type is not None:
            items.append(("content-type", content_type))
        self.status = status
        self.body = raw
        self.headers = [encode_header(name, value) for name, value in items]

    @classmethod
    def raw(
        cls, status: int, body: bytes, headers: Sequence[tuple[bytes, bytes]]
    ) -> Response:
        """A response from encoded parts, checked like one built from text: a
        framing or server header, a bad name or a CR/LF in a value raise
        ``ValueError``. Header names are lowercased."""
        if not MIN_STATUS <= status <= MAX_STATUS:
            raise ValueError(f"invalid HTTP status {status}")
        if body and status in BODYLESS:
            raise ValueError(f"status {status} cannot have a body")
        response = cls.__new__(cls)
        response.status = status
        response.body = bytes(body)
        response.headers = [
            encode_header(name.decode("latin-1"), value.decode("latin-1"))
            for name, value in headers
        ]
        return response

    def with_header(self, name: str, value: str) -> Response:
        """A copy with one more header. It keeps the class and, for a streamed
        answer, the source of the body."""
        header = encode_header(name, value)
        duplicate = copy(self)
        duplicate.headers = [*self.headers, header]
        return duplicate


def set_cookie(
    name: str,
    value: str,
    *,
    path: str = "/",
    max_age: int | None = None,
    http_only: bool = True,
    secure: bool = True,
    same_site: str = "Lax",
) -> tuple[str, str]:
    """A ``("set-cookie", ...)`` header for ``Response(headers=[...])``. The name
    must be a token and the value cookie-safe (no space, quote, comma,
    semicolon or backslash); encode richer data yourself."""
    return core.set_cookie_header(
        name,
        value,
        path=path,
        max_age=None if max_age is None else int(max_age),
        http_only=http_only,
        secure=secure,
        same_site=same_site,
    )


__all__ = ["Response", "ResponseHeaders", "set_cookie"]
