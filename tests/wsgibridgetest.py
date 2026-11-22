"""The PEP 3333 bridge Kiso hosts a WSGI callable with, against a fake call."""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from hinoki.runtime.wsgibridge import (
    Bridge,
    ClientGone,
    Responder,
    build_environ,
    checked_headers,
    parse_status,
    split_host,
)

StartResponse = Callable[..., Callable[[bytes], object]]


@dataclass
class FakeCall:
    method: str = "GET"
    path: str = "/a%20b/%C3%A9"
    query: bytes = b"x=1"
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    client: tuple[str, int] = ("203.0.113.9", 4242)
    secure: bool = False
    http_version: str = "1.1"
    body: bytes = b""
    open: bool = True
    head: tuple[int, list[tuple[bytes, bytes]]] | None = None
    sent: list[bytes] = field(default_factory=list)
    finished: bool = False

    def start(self, status: int, headers: list[tuple[bytes, bytes]]) -> bool:
        self.head = (status, headers)
        return self.open

    def send(self, chunk: bytes) -> bool:
        self.sent.append(chunk)
        return self.open

    def finish(self) -> None:
        self.finished = True


def test_environ_follows_pep_3333() -> None:
    call = FakeCall(
        method="POST",
        body=b"hello",
        headers=[
            (b"Content-Type", b"text/plain"),
            (b"Content-Length", b"99"),
            (b"Transfer-Encoding", b"chunked"),
            (b"X-Repeat", b"a"),
            (b"x-repeat", b"b"),
            (b"Host", b"example.test:8443"),
        ],
        secure=True,
    )
    environ = build_environ(call, ("0.0.0.0", 80))
    assert environ["PATH_INFO"] == "/a b/\xc3\xa9"  # decoded, carried as latin-1
    assert environ["QUERY_STRING"] == "x=1"
    assert environ["CONTENT_LENGTH"] == "5"  # of the decoded body, not the header
    assert environ["CONTENT_TYPE"] == "text/plain"
    assert "HTTP_TRANSFER_ENCODING" not in environ
    assert environ["HTTP_X_REPEAT"] == "a,b"
    assert (environ["SERVER_NAME"], environ["SERVER_PORT"]) == ("example.test", "8443")
    assert environ["wsgi.url_scheme"] == "https"
    assert environ["wsgi.input"].read() == b"hello"
    assert environ["REMOTE_ADDR"] == "203.0.113.9"


def test_server_name_falls_back_to_the_listener() -> None:
    environ = build_environ(FakeCall(), ("10.0.0.1", 9000))
    assert (environ["SERVER_NAME"], environ["SERVER_PORT"]) == ("10.0.0.1", "9000")
    assert "CONTENT_LENGTH" not in environ


def test_repeated_cookie_headers_are_joined_by_a_semicolon() -> None:
    call = FakeCall(headers=[(b"cookie", b"a=1"), (b"cookie", b"b=2")])
    assert build_environ(call, ("h", 80))["HTTP_COOKIE"] == "a=1; b=2"


def test_header_names_with_an_underscore_are_dropped() -> None:
    call = FakeCall(
        headers=[
            (b"x-foo", b"dashed"),
            (b"X_Foo", b"spoofed"),
            (b"Remote_User", b"admin"),
            (b"Content_Type", b"text/evil"),
        ]
    )
    environ = build_environ(call, ("h", 80))
    assert environ["HTTP_X_FOO"] == "dashed"
    assert "HTTP_REMOTE_USER" not in environ
    assert "CONTENT_TYPE" not in environ
    assert "HTTP_CONTENT_TYPE" not in environ


@pytest.mark.parametrize(
    ("value", "scheme", "expected"),
    [
        ("example.test", "http", ("example.test", "80")),
        ("example.test", "https", ("example.test", "443")),
        ("[::1]:8080", "http", ("[::1]", "8080")),
        ("[::1]", "https", ("[::1]", "443")),
        ("example.test:abc", "http", ("example.test", "80")),
        ("[::1]:x", "https", ("[::1]", "443")),
    ],
)
def test_split_host(value: str, scheme: str, expected: tuple[str, str]) -> None:
    assert split_host(value, scheme) == expected


@pytest.mark.parametrize("status", [None, 200, "OK", "99 Low", "099 Low", "20"])
def test_an_invalid_status_is_refused(status: object) -> None:
    with pytest.raises(AssertionError):
        parse_status(status)


def test_a_status_line_gives_its_code() -> None:
    assert parse_status("404 Not Found") == 404


@pytest.mark.parametrize(
    "headers",
    [
        "not a list",
        [("a",)],
        [(b"a", "b")],
        [("Connection", "close")],
        [("Transfer-Encoding", "chunked")],
        [("X-Evil", "a\r\nset-cookie: b=1")],
    ],
)
def test_invalid_response_headers_are_refused(headers: object) -> None:
    with pytest.raises(AssertionError):
        checked_headers(headers)


def test_response_headers_are_lowercased_bytes() -> None:
    assert checked_headers([("X-A", "1"), ("X-A", "2")]) == [
        (b"x-a", b"1"),
        (b"x-a", b"2"),
    ]


def run(app: Callable[[dict[str, Any], StartResponse], Iterable[bytes]]) -> FakeCall:
    call = FakeCall()
    Bridge(app, "127.0.0.1", 80)(call)
    return call


def test_the_head_goes_out_with_the_first_piece() -> None:
    def app(environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        start_response("201 Created", [("X-A", "1")])
        return [b"one", b"", b"two"]

    call = run(app)
    assert call.head == (201, [(b"x-a", b"1")])
    assert call.sent == [b"one", b"two"]
    assert call.finished


def test_an_empty_body_still_sends_the_head() -> None:
    def app(environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        start_response("204 No Content", [])
        return []

    assert run(app).head == (204, [])


def test_the_iterable_is_closed_even_when_it_fails() -> None:
    closed: list[bool] = []

    class Body:
        def __iter__(self) -> "Body":
            return self

        def __next__(self) -> bytes:
            raise RuntimeError("boom")

        def close(self) -> None:
            closed.append(True)

    def app(environ: dict[str, Any], start_response: StartResponse) -> Body:
        start_response("200 OK", [])
        return Body()

    call = FakeCall()
    with pytest.raises(RuntimeError):
        Bridge(app, "127.0.0.1", 80)(call)
    assert closed == [True]
    assert not call.finished  # the host answers the failure


def test_a_close_that_fails_after_a_complete_body_still_finishes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Body:
        def __iter__(self) -> Iterator[bytes]:
            return iter([b"done"])

        def close(self) -> None:
            raise RuntimeError("close failed")

    def app(environ: dict[str, Any], start_response: StartResponse) -> Body:
        start_response("200 OK", [])
        return Body()

    with caplog.at_level("ERROR", logger="hinoki.runtime"):
        call = run(app)
    assert call.sent == [b"done"]
    assert call.finished
    assert "close failed" in caplog.text


def test_a_vanished_client_stops_the_iteration_and_closes_it() -> None:
    pulled: list[int] = []
    closed: list[bool] = []

    class Body:
        def __iter__(self) -> "Body":
            return self

        def __next__(self) -> bytes:
            pulled.append(1)
            return b"x"

        def close(self) -> None:
            closed.append(True)

    def app(environ: dict[str, Any], start_response: StartResponse) -> Body:
        start_response("200 OK", [])
        return Body()

    call = FakeCall(open=False)
    Bridge(app, "127.0.0.1", 80)(call)
    assert len(pulled) == 1
    assert closed == [True]


def test_start_response_twice_is_refused_unless_exc_info_is_given() -> None:
    responder = Responder(FakeCall())
    responder.start_response("200 OK", [])
    with pytest.raises(AssertionError, match="twice"):
        responder.start_response("200 OK", [])
    try:
        raise ValueError("first")
    except ValueError as error:
        info = (type(error), error, error.__traceback__)
    responder.start_response("500 Error", [], info)  # head not out: replaced
    assert responder.pending is not None
    assert responder.pending[0] == 500


def test_exc_info_reraises_once_the_head_is_out() -> None:
    responder = Responder(FakeCall())
    responder.start_response("200 OK", [])
    assert responder.emit(b"x")
    try:
        raise ValueError("late")
    except ValueError as error:
        info = (type(error), error, error.__traceback__)
    with pytest.raises(ValueError, match="late"):
        responder.start_response("500 Error", [], info)


def test_a_response_without_start_response_is_refused() -> None:
    with pytest.raises(AssertionError, match="start_response"):
        Responder(FakeCall()).emit(b"x")


def test_a_str_piece_is_refused() -> None:
    responder = Responder(FakeCall())
    responder.start_response("200 OK", [])
    with pytest.raises(TypeError, match="bytes"):
        responder.emit("text")  # type: ignore[arg-type]


def test_the_legacy_write_raises_when_the_client_is_gone() -> None:
    responder = Responder(FakeCall(open=False))
    write = responder.start_response("200 OK", [])
    with pytest.raises(ClientGone):
        write(b"x")


def test_a_send_that_fails_mid_body_marks_the_client_gone() -> None:
    call = FakeCall()
    responder = Responder(call)
    responder.start_response("200 OK", [])
    assert responder.emit(b"first")
    call.open = False
    assert not responder.emit(b"second")
    assert responder.gone
    assert not responder.emit(b"third")
    assert call.sent == [b"first", b"second"]  # nothing is sent once it is gone
