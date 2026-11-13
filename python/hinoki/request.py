"""The request a handler receives as its first argument.

Django-shaped and read-only: ``request.method``, ``request.headers``,
``request.GET``, ``request.COOKIES``, ``request.body``, ``request.data`` and
``request.user``. The body is parsed on first use of ``data`` and once.
Values come from the ASGI scope the runtime already holds; nothing here does
I/O.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from hinoki import native
from hinoki.errors import InvalidBody
from hinoki.parsers import (
    DEFAULT_PARSERS,
    MultiDict,
    Parsed,
    Parser,
    QueryDict,
    UploadedFile,
    choose,
)


class Headers(Mapping[str, str]):
    """Case-insensitive header mapping. Repeated headers arrive already joined
    with ``", "``."""

    __slots__ = ("items_",)

    def __init__(self, items: Mapping[str, str]) -> None:
        self.items_ = {name.lower(): value for name, value in items.items()}

    def __getitem__(self, name: str) -> str:
        return self.items_[name.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self.items_)

    def __len__(self) -> int:
        return len(self.items_)


class Request:
    """One HTTP request. Never mutate it; it is shared with the whole call."""

    __slots__ = (
        "COOKIES",
        "GET",
        "body",
        "cached",
        "client",
        "headers",
        "host",
        "method",
        "parsers",
        "path",
        "path_params",
        "request_id",
        "scheme",
        "user",
    )

    def __init__(  # noqa: PLR0913 - one value per documented attribute
        self,
        *,
        method: str,
        path: str,
        scheme: str,
        host: str,
        client: tuple[str, int] | None,
        headers: Mapping[str, str],
        query: Mapping[str, list[str]],
        body: bytes,
        path_params: Mapping[str, str],
        user: Any,
        request_id: str,
        parsers: Sequence[Parser] = DEFAULT_PARSERS,
    ) -> None:
        self.method = method
        self.path = path
        self.scheme = scheme
        self.host = host
        self.client = client
        self.headers = Headers(headers)
        self.GET = QueryDict(query)
        self.COOKIES = native.parse_cookie_header(self.headers.get("cookie", ""))
        self.body = body
        self.path_params = dict(path_params)
        self.user = user
        self.request_id = request_id
        self.parsers = tuple(parsers)
        self.cached: Parsed | None = None

    @property
    def content_type(self) -> str:
        """Media type without parameters, lower-case ("" when absent)."""
        raw = self.headers.get("content-type", "")
        if "," in raw:
            # Repeated headers arrive joined with ", ": which value counts is
            # ambiguous, and proxies may pick a different one.
            raise InvalidBody("invalid_content_type", "several Content-Type values")
        return raw.split(";", maxsplit=1)[0].strip().lower()

    def parsed(self) -> Parsed:
        """The body through the parser for its content type, once; raises
        ``InvalidBody`` (415 for an unlisted type, 400 for a malformed body)."""
        if self.cached is None:
            self.cached = choose(self.parsers, self.content_type).parse(
                self.body, self.headers.get("content-type", "")
            )
        return self.cached

    @property
    def data(self) -> object:
        """The parsed body (JSON value, or form fields as text)."""
        return self.parsed().data

    def accepts_form(self) -> bool:
        return any(p.forms and p.media_type == self.content_type for p in self.parsers)

    @property
    def POST(self) -> QueryDict:
        """Form fields; empty when the body is not a form."""
        fields = self.parsed().fields if self.accepts_form() else {}
        return QueryDict(fields)

    @property
    def FILES(self) -> MultiDict[UploadedFile]:
        """Uploaded files of a multipart body; empty otherwise."""
        files = self.parsed().files if self.accepts_form() else {}
        return MultiDict(files)
