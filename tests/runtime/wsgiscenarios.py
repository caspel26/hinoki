"""A plain WSGI callable with one route per behaviour the Kiso/Granian parity
test compares. ``WSGI_SCENARIO_LOG`` names a file that receives a line when a
response iterable is closed."""

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

Environ = dict[str, Any]
StartResponse = Callable[..., Any]
CHUNKS = 3
PAUSE = 0.3


def note(text: str) -> None:
    path = os.environ.get("WSGI_SCENARIO_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


class Tracked:
    """An iterable that reports being closed and, if endless, keeps producing."""

    def __init__(self, name: str, *, endless: bool) -> None:
        self.name = name
        self.endless = endless

    def __iter__(self) -> Iterator[bytes]:
        count = 0
        while self.endless or count < CHUNKS:
            count += 1
            yield b"x" * 1024
            time.sleep(0.05 if self.endless else PAUSE)

    def close(self) -> None:
        note(f"closed {self.name}")


def reply(
    start_response: StartResponse,
    status: str,
    body: bytes,
    extra: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    start_response(
        status,
        [
            ("content-type", "text/plain"),
            ("content-length", str(len(body))),
            *(extra or []),
        ],
    )
    return [body]


def environ_report(environ: Environ) -> bytes:
    keys = (
        "REQUEST_METHOD",
        "PATH_INFO",
        "QUERY_STRING",
        "SCRIPT_NAME",
        "SERVER_PROTOCOL",
        "CONTENT_TYPE",
        "CONTENT_LENGTH",
        "HTTP_X_REPEAT",
        "wsgi.url_scheme",
    )
    return json.dumps({key: environ.get(key) for key in keys}, sort_keys=True).encode()


def status_route(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
    extra = [("x-a", "1"), ("x-a", "2"), ("x-b", "3")]
    return reply(start_response, "202 Accepted", b"accepted", extra)


def environ_route(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
    return reply(start_response, "200 OK", environ_report(environ))


def echo_route(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
    body = environ["wsgi.input"].read()
    digest = hashlib.sha256(body).hexdigest()
    return reply(start_response, "200 OK", f"{len(body)} {digest}".encode())


def stream_route(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
    start_response("200 OK", [("content-type", "application/octet-stream")])
    path = environ["PATH_INFO"]
    return Tracked(path, endless=path == "/endless")


def boom_route(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
    raise RuntimeError("scenario failure")


ROUTES = {
    "/status": status_route,
    "/echo": echo_route,
    "/stream": stream_route,
    "/endless": stream_route,
    "/boom": boom_route,
}


def app(environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
    path = environ["PATH_INFO"]
    route = environ_route if path.startswith("/environ") else ROUTES.get(path)
    if route is None:
        return reply(start_response, "404 Not Found", b"no")
    return route(environ, start_response)
