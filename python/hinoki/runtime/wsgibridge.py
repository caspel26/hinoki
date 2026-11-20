"""The Python side of Kiso's WSGI host (``core.WsgiServer``).

A Rust worker thread calls :class:`Bridge` once per request with a
``core.WsgiCall``: it builds the PEP 3333 ``environ`` from the request, calls
the application, and hands the response back through the call, piece by piece.
``call.send`` waits while the client is slow (without the GIL) and returns
``False`` once the client is gone, so a long or endless iterable stops being
read; the iterable is always closed, whatever happened.

Nothing here is native: every request and response piece is a Python object,
and the application runs on a worker thread that holds a slot of the bounded
pool for as long as it runs.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable, Iterable
from typing import Any, Protocol
from urllib.parse import unquote_to_bytes

Headers = list[tuple[str, str]]
StartResponse = Callable[..., Callable[[bytes], object]]
WSGIApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]

# PEP 3333: an application must not set these; the server owns the framing.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
DEFAULT_PORTS = {"http": 80, "https": 443}
CODE = 3  # digits of a status code
MIN_STATUS, MAX_STATUS = 100, 999
PAIR = 2


class Call(Protocol):
    """What the bridge uses of ``core.WsgiCall``."""

    @property
    def method(self) -> str: ...

    @property
    def path(self) -> str: ...

    @property
    def query(self) -> bytes: ...

    @property
    def headers(self) -> list[tuple[bytes, bytes]]: ...

    @property
    def client(self) -> tuple[str, int]: ...

    @property
    def secure(self) -> bool: ...

    @property
    def http_version(self) -> str: ...

    @property
    def body(self) -> bytes: ...

    def start(self, status: int, headers: list[tuple[bytes, bytes]]) -> bool: ...

    def send(self, chunk: bytes) -> bool: ...

    def finish(self) -> None: ...


class ClientGone(OSError):
    """The client left before the response was written."""


def split_host(value: str, scheme: str) -> tuple[str, str]:
    """``SERVER_NAME`` and ``SERVER_PORT`` from a ``Host`` header value."""
    default = str(DEFAULT_PORTS[scheme])
    if value.startswith("["):
        name, _, rest = value.partition("]")
        return f"{name}]", rest.removeprefix(":") or default
    name, _, port = value.partition(":")
    return name, port or default


def build_environ(
    call: Call, server: tuple[str, int], errors: io.TextIOBase | None = None
) -> dict[str, Any]:
    """The PEP 3333 environment of one request."""
    scheme = "https" if call.secure else "http"
    environ: dict[str, Any] = {
        "REQUEST_METHOD": call.method,
        "SCRIPT_NAME": "",
        # Rust passes the target still percent-encoded; PEP 3333 wants it decoded
        # and carried as latin-1.
        "PATH_INFO": unquote_to_bytes(call.path).decode("latin-1"),
        "QUERY_STRING": call.query.decode("latin-1"),
        "SERVER_NAME": server[0],
        "SERVER_PORT": str(server[1]),
        "SERVER_PROTOCOL": f"HTTP/{call.http_version}",
        "REMOTE_ADDR": call.client[0],
        "REMOTE_PORT": str(call.client[1]),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scheme,
        "wsgi.input": io.BytesIO(call.body),
        "wsgi.errors": errors or sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    add_headers(environ, call)
    return environ


def add_headers(environ: dict[str, Any], call: Call) -> None:
    """Request headers as ``HTTP_*`` (repeats joined by a comma), with the
    body's length known: the body is decoded and complete before the call."""
    for raw_name, raw_value in call.headers:
        name, value = raw_name.decode("latin-1").lower(), raw_value.decode("latin-1")
        if name in {"transfer-encoding", "content-length"}:
            continue
        key = name.upper().replace("-", "_")
        key = key if name == "content-type" else f"HTTP_{key}"
        environ[key] = f"{environ[key]},{value}" if key in environ else value
    if call.body or "CONTENT_TYPE" in environ:
        environ["CONTENT_LENGTH"] = str(len(call.body))
    host = environ.get("HTTP_HOST")
    if host:
        scheme = environ["wsgi.url_scheme"]
        environ["SERVER_NAME"], environ["SERVER_PORT"] = split_host(host, scheme)


def parse_status(status: object) -> int:
    """The code of a ``"200 OK"`` status line."""
    if not isinstance(status, str) or not status[:3].isdigit() or len(status) < CODE:
        raise AssertionError(f"invalid status: {status!r}")
    code = int(status[:CODE])
    if not MIN_STATUS <= code <= MAX_STATUS:
        raise AssertionError(f"invalid status: {status!r}")
    return code


def checked_headers(headers: object) -> list[tuple[bytes, bytes]]:
    """The response headers as bytes, refusing what PEP 3333 forbids."""
    if not isinstance(headers, list):
        raise AssertionError("headers must be a list of (name, value) tuples")
    checked: list[tuple[bytes, bytes]] = []
    for item in headers:
        if not (isinstance(item, tuple) and len(item) == PAIR):
            raise AssertionError(f"invalid header: {item!r}")
        name, value = item
        if not isinstance(name, str) or not isinstance(value, str):
            raise AssertionError(f"header names and values are str: {item!r}")
        if name.lower() in HOP_BY_HOP:
            raise AssertionError(f"hop-by-hop header: {name}")
        checked.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    return checked


class Responder:
    """``start_response`` and the response of one call. Headers go out with the
    first piece (or at the end), as PEP 3333 allows ``start_response`` to be
    called until then."""

    def __init__(self, call: Call) -> None:
        self.call = call
        self.pending: tuple[int, list[tuple[bytes, bytes]]] | None = None
        self.started = False
        self.gone = False

    def start_response(
        self,
        status: str,
        headers: Headers,
        exc_info: tuple[Any, Any, Any] | None = None,
    ) -> Callable[[bytes], object]:
        if exc_info is not None:
            try:
                if self.started:
                    raise exc_info[1].with_traceback(exc_info[2])
            finally:
                exc_info = None
        elif self.pending is not None:
            raise AssertionError("start_response was called twice")
        self.pending = (parse_status(status), checked_headers(headers))
        return self.write

    def write(self, data: bytes) -> None:
        """The legacy ``write`` callable; raises once the client is gone."""
        if not self.emit(data):
            raise ClientGone("the client closed the connection")

    def emit(self, data: bytes) -> bool:
        """Send the head if it is not out, then ``data``. ``False`` once the
        client is gone."""
        if self.gone:
            return False
        if not isinstance(data, bytes):
            raise TypeError(f"the response body is bytes, not {type(data).__name__}")
        if not self.started and not self.begin():
            return False
        if data and not self.call.send(data):
            self.gone = True
        return not self.gone

    def begin(self) -> bool:
        if self.pending is None:
            raise AssertionError("the application did not call start_response")
        self.started = True
        if not self.call.start(*self.pending):
            self.gone = True
        return not self.gone


def stream(responder: Responder, result: Iterable[bytes]) -> None:
    """Write the iterable to the client, stopping when the client is gone."""
    for piece in result:
        if not responder.emit(piece):
            return
    responder.emit(b"")


class Bridge:
    """What ``core.WsgiServer`` calls on a worker thread for each request."""

    def __init__(self, app: WSGIApp, host: str, port: int) -> None:
        self.app = app
        self.server = (host, port)

    def __call__(self, call: Call) -> None:
        responder = Responder(call)
        result: Iterable[bytes] | None = None
        try:
            result = self.app(
                build_environ(call, self.server), responder.start_response
            )
            stream(responder, result)
        finally:
            close = getattr(result, "close", None)
            if close is not None:
                close()
        # Not reached when the application failed: the host then answers 500,
        # or resets the stream if the head is already out.
        call.finish()
