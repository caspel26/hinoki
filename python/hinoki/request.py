"""The request a handler receives as its first argument.

Django-shaped and read-only: ``request.method``, ``request.headers``,
``request.GET``, ``request.COOKIES``, ``request.body``, ``request.data`` and
``request.user``. The body is parsed on first use of ``data`` and once.
Values come from the ASGI scope the runtime already holds; nothing here does
I/O.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from hinoki import native

UNREAD = object()


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


class QueryDict(Mapping[str, str]):
    """Query parameters. ``query["a"]`` and ``get`` return the last value, as in
    Django; ``getlist`` returns all of them."""

    __slots__ = ("lists",)

    def __init__(self, lists: Mapping[str, list[str]]) -> None:
        self.lists = dict(lists)

    def __getitem__(self, name: str) -> str:
        return self.lists[name][-1]

    def __iter__(self) -> Iterator[str]:
        return iter(self.lists)

    def __len__(self) -> int:
        return len(self.lists)

    def getlist(self, name: str) -> list[str]:
        return list(self.lists.get(name, []))


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
        self.cached: object = UNREAD

    @property
    def content_type(self) -> str:
        """Media type without parameters, lower-case ("" when absent)."""
        raw = self.headers.get("content-type", "")
        return raw.split(";", maxsplit=1)[0].strip().lower()

    @property
    def data(self) -> object:
        """The parsed body; raises ``InvalidBody`` (415/400) when unusable."""
        if self.cached is UNREAD:
            # Local import: the binding module imports this one for Request.
            from hinoki.runtime.binding import decode_body  # noqa: PLC0415

            self.cached = decode_body(self.body, self.content_type)
        return self.cached
