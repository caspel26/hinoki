"""An explicit HTTP answer: ``return Reply(303, headers={"location": "/x"})``.

A handler returns a ``Reply`` when it needs a status, headers (redirects,
cookies) or a body of its own; it bypasses the renderers. Everything is
validated when the ``Reply`` is built, so a bad header fails in the handler
(a 500 with a request id) instead of corrupting the response.

An answer that signals failure should be an exception, not a ``Reply``: a
``transaction=True`` action commits when the handler returns, whatever the
status of the ``Reply``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from hinoki.runtime.responses import BODYLESS

TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*")
CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
RESERVED = {"content-length", "transfer-encoding", "connection", "x-request-id"}
SAME_SITE = {"Strict", "Lax", "None"}
MIN_STATUS, MAX_STATUS = 200, 599

Headers = Mapping[str, str] | Sequence[tuple[str, str]]


def encode_header(name: str, value: str) -> tuple[bytes, bytes]:
    lowered = name.lower()
    if not TOKEN.fullmatch(name):
        raise ValueError(f"invalid header name {name!r}")
    if lowered in RESERVED:
        raise ValueError(f"header {name!r} is set by the server")
    if CONTROL.search(value):
        raise ValueError(f"invalid characters in the value of header {name!r}")
    try:
        return lowered.encode("ascii"), value.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError(f"header {name!r} must be Latin-1") from None


class Reply:
    """Status, headers and body chosen by the handler."""

    __slots__ = ("body", "headers", "status")

    def __init__(
        self,
        status: int = 200,
        body: bytes | str = b"",
        headers: Headers = (),
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
    """A ``("set-cookie", ...)`` header for ``Reply(headers=[...])``. The name
    must be a token and the value cookie-safe (no space, quote, comma,
    semicolon or backslash); encode richer data yourself."""
    if not TOKEN.fullmatch(name) or not COOKIE_VALUE.fullmatch(value):
        raise ValueError("cookie name or value contains forbidden characters")
    if same_site not in SAME_SITE or (same_site == "None" and not secure):
        raise ValueError("same_site must be Strict, Lax, or None together with secure")
    if not path.startswith("/") or any(c in path for c in ";\r\n"):
        raise ValueError("invalid cookie path")
    parts = [f"{name}={value}", f"Path={path}", f"SameSite={same_site}"]
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    parts += [flag for flag, on in (("HttpOnly", http_only), ("Secure", secure)) if on]
    return "set-cookie", "; ".join(parts)
