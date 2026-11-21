"""Responses that are sent while they are produced: ``Stream``, ``EventStream``
and ``File``.

    @http.get("/export.csv")
    async def export() -> Stream:
        async def rows() -> AsyncIterator[str]:
            yield "id,total\\n"
            ...

        return Stream(rows(), content_type="text/csv; charset=utf-8")

The handler returns the object; its status, headers and the source of the body
are fixed then, so access, validation, errors and the request timeout all
happen before the first byte. The action's return annotation must name the
class (``-> Stream``, ``-> EventStream``, ``-> File``, alone or in a union).
They are ``Response`` objects with an empty ``body``; middleware can add
headers with ``with_header`` but never sees the pieces.
"""

from __future__ import annotations

import mimetypes
import os
from collections.abc import AsyncIterable, Iterable
from types import EllipsisType

from hinoki import core
from hinoki.http.response import Headers, Response
from hinoki.runtime.responses import BODYLESS

Source = AsyncIterable[object] | Iterable[object]
EVENT_STREAM_HEADERS = (
    ("cache-control", "no-store"),
    ("x-accel-buffering", "no"),
)


class Streamed(Response):
    """What a handler returns to answer with a stream; the base of ``Stream``,
    ``EventStream`` and ``File``."""

    __slots__ = ()

    def __init__(
        self,
        status: int,
        headers: Headers,
        content_type: str | None,
        default_type: str,
    ) -> None:
        if status in BODYLESS:
            raise ValueError(f"status {status} cannot have a body")
        items = _items(headers)
        declared = any(name.lower() == "content-type" for name, _ in items)
        if content_type is not None and declared:
            raise ValueError("content-type is given twice")
        chosen = content_type or (None if declared else default_type)
        super().__init__(status, b"", items, chosen)

    def same_body(self, other: Streamed) -> bool:
        """Whether ``other`` sends the same body: a ``with_header`` copy does."""
        raise NotImplementedError


class Sourced(Streamed):
    """A streamed answer whose body comes from an iterable of pieces."""

    __slots__ = ("keepalive", "length", "source")

    def __init__(
        self,
        source: Source,
        status: int,
        headers: Headers,
        content_type: str | None,
        default_type: str,
        length: int | None,
    ) -> None:
        if isinstance(source, str | bytes | bytearray) or not (
            hasattr(source, "__aiter__") or hasattr(source, "__iter__")
        ):
            raise TypeError(
                "the body must be an iterable or async iterable of pieces; "
                "return Response(...) for a body that is already complete"
            )
        if length is not None and (
            isinstance(length, bool) or not isinstance(length, int) or length < 0
        ):
            raise ValueError("length must be a non-negative integer")
        super().__init__(status, headers, content_type, default_type)
        self.source = source
        self.length = length
        self.keepalive: float | EllipsisType | None = None

    def same_body(self, other: Streamed) -> bool:
        return isinstance(other, Sourced) and other.source is self.source

    @property
    def preamble(self) -> bytes:
        """Bytes sent before the first piece."""
        return b""

    def encode(self, piece: object) -> bytes:
        """One piece of the source as bytes."""
        if isinstance(piece, bytes | bytearray | memoryview):
            return bytes(piece)
        if isinstance(piece, str):
            return piece.encode()
        raise TypeError(
            f"a stream piece must be bytes or str, not {type(piece).__name__}"
        )


class Stream(Sourced):
    """A body of ``bytes`` or ``str`` pieces (a ``str`` is sent as UTF-8).

    ``body`` is an iterable or an async iterable; a plain iterable is advanced
    in the bounded sync executor. ``length`` declares the exact size when it is
    known, which gives the answer a ``content-length``; without it the body is
    sent with chunked framing (HTTP/1.1) or until the stream ends (HTTP/2).
    """

    __slots__ = ()

    def __init__(
        self,
        body: Source,
        *,
        status: int = 200,
        headers: Headers = (),
        content_type: str | None = None,
        length: int | None = None,
    ) -> None:
        super().__init__(
            body, status, headers, content_type, "application/octet-stream", length
        )


class Event:
    """One server-sent event. ``data`` that is not a ``str`` (a ``Schema``, a
    ``dict``, a ``list`` or a scalar) is sent as JSON; ``None`` sends no ``data``
    field. Line breaks in ``data`` and ``comment`` become separate lines;
    ``event`` and ``id`` with CR, LF or NUL raise ``ValueError`` here."""

    __slots__ = ("wire",)

    def __init__(
        self,
        data: object = None,
        *,
        event: str | None = None,
        id: str | None = None,
        retry: int | None = None,
        comment: str | None = None,
    ) -> None:
        if retry is not None and (
            isinstance(retry, bool) or not isinstance(retry, int) or retry < 0
        ):
            raise ValueError("retry must be a non-negative number of milliseconds")
        text = (
            data
            if data is None or isinstance(data, str)
            else core.encode_json(data).decode()
        )
        self.wire: bytes = core.sse_frame(text, event, id, retry, comment)


class EventStream(Sourced):
    """Server-sent events (``text/event-stream``) from an iterable of ``Event``
    or ``str`` (a ``str`` is the ``data`` of one event). ``retry`` is the
    reconnection delay in milliseconds sent to the client first.
    ``keepalive`` is the seconds of silence after which a comment is sent: the
    default is ``execution.sse_keepalive``, ``None`` turns it off. The answer
    has ``cache-control: no-store`` and ``x-accel-buffering: no``."""

    __slots__ = ("retry_frame",)

    def __init__(
        self,
        events: AsyncIterable[Event | str] | Iterable[Event | str],
        *,
        status: int = 200,
        headers: Headers = (),
        keepalive: float | EllipsisType | None = ...,
        retry: int | None = None,
    ) -> None:
        if keepalive is not None and keepalive is not ...:
            valid = isinstance(keepalive, float | int) and not isinstance(
                keepalive, bool
            )
            if not valid or keepalive <= 0:
                raise ValueError(
                    "keepalive must be a positive number of seconds or None"
                )
        items = _items(headers)
        given = {name.lower() for name, _ in items}
        extra = [item for item in EVENT_STREAM_HEADERS if item[0] not in given]
        super().__init__(
            events,
            status,
            [*items, *extra],
            None,
            "text/event-stream; charset=utf-8",
            None,
        )
        self.retry_frame = None if retry is None else Event(retry=retry).wire
        self.keepalive = keepalive

    @property
    def preamble(self) -> bytes:
        return self.retry_frame or b""

    def encode(self, piece: object) -> bytes:
        if isinstance(piece, Event):
            return piece.wire
        if isinstance(piece, str):
            return Event(piece).wire
        raise TypeError(
            f"an event stream piece must be an Event or str, not {type(piece).__name__}"
        )


class File(Streamed):
    """A file sent by the server: ``File("reports/q3.pdf", root=REPORTS)``.

    A relative ``path`` is taken from ``root``. With ``root`` the file must be
    inside it once symbolic links are resolved; a missing file, a directory or
    a path that leaves ``root`` answers ``404``. ``filename`` adds a
    ``content-disposition: attachment`` header. The ``content-type`` is guessed
    from the name unless given. The whole file is sent with its size as
    ``content-length`` (ranges are not supported: ``accept-ranges: none``).
    """

    __slots__ = ("path", "root")

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        root: str | os.PathLike[str] | None = None,
        filename: str | None = None,
        content_type: str | None = None,
        status: int = 200,
        headers: Headers = (),
    ) -> None:
        extra = [("accept-ranges", "none")]
        if filename is not None:
            extra.append(("content-disposition", core.content_disposition(filename)))
        super().__init__(
            status,
            [*_items(headers), *extra],
            content_type,
            mimetypes.guess_type(os.fspath(path))[0] or "application/octet-stream",
        )
        self.path = os.fspath(path)
        self.root = None if root is None else os.fspath(root)

    def same_body(self, other: Streamed) -> bool:
        return isinstance(other, File) and (other.path, other.root) == (
            self.path,
            self.root,
        )


def _items(headers: Headers) -> list[tuple[str, str]]:
    return list(headers.items() if hasattr(headers, "items") else headers)


__all__ = ["Event", "EventStream", "File", "Stream", "Streamed"]
