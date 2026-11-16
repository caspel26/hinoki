"""The WSGI adapter, in process and under a real WSGI server (Granian)."""

import io
import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from hinoki import Hinoki, Module, http
from hinoki.modules import ModuleContext
from hinoki.wsgi import WSGIAdapter
from tests.runtime.harness import free_port, wait_ready
from tests.runtime.wsgiapp import PROTECTED_UPLOAD_PARSER, create_app

ROOT = Path(__file__).resolve().parents[1]


def call(
    adapter: WSGIAdapter,
    method: str,
    path: str,
    body: bytes = b"",
    query: str = "",
    content_type: str = "application/json",
) -> tuple[str, dict[str, str], Any]:
    seen: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        seen["status"], seen["headers"] = status, dict(headers)

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "t",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": content_type,
    }
    payload = b"".join(adapter(environ, start_response))
    return seen["status"], seen["headers"], json.loads(payload) if payload else None


def test_async_sync_and_body_handlers_answer_in_process() -> None:
    adapter = WSGIAdapter(create_app())
    try:
        get = call(adapter, "GET", "/ping")
        head = call(adapter, "HEAD", "/ping")
        assert head[0] == get[0] == "200 OK"
        assert get[2] == {"pong": "async"}
        assert head[2] is None
        assert head[1]["content-length"] == get[1]["content-length"]
        assert {
            key: value for key, value in head[1].items() if key != "x-request-id"
        } == {key: value for key, value in get[1].items() if key != "x-request-id"}
        assert call(adapter, "GET", "/sync")[::2] == ("200 OK", {"pong": "sync"})
        status, headers, body = call(adapter, "POST", "/echo", b'{"text":"hi"}')
        assert (status, body) == ("201 Created", {"text": "hi"})
        assert "x-request-id" in headers
        boundary = "wsgi-upload-test"
        multipart = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="note.txt"\r\n'
            "Content-Type: text/plain\r\n\r\nhello\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        status, _, body = call(
            adapter,
            "POST",
            "/upload",
            multipart,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        assert (status, body) == (
            "200 OK",
            {"filename": "note.txt", "content": "hello"},
        )
        assert call(adapter, "GET", "/nope")[0] == "404 Not Found"
        assert call(adapter, "POST", "/echo", b"x" * (1024 * 1024 + 5))[0].startswith(
            "413"
        )
    finally:
        adapter.close()


def test_wsgi_auth_denial_buffers_but_does_not_parse_the_upload() -> None:
    PROTECTED_UPLOAD_PARSER.calls = 0
    adapter = WSGIAdapter(create_app())
    try:
        status, _, body = call(
            adapter,
            "POST",
            "/protected-upload",
            b"malformed private upload",
            content_type="multipart/form-data; boundary=private",
        )
        assert status == "401 Unauthorized"
        assert body["error"] == "unauthorized"
        assert PROTECTED_UPLOAD_PARSER.calls == 0
    finally:
        adapter.close()


class Broken(Module):
    name = "broken"

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        raise RuntimeError("no database")
        yield


class Recorder(Module):
    name = "recorder"
    events: ClassVar[list[str]] = []

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        self.events.append("up")
        yield
        self.events.append("down")


def test_startup_failure_answers_503_and_close_is_safe() -> None:
    app = Hinoki(modules=[Broken()])
    app.include(http.get("/x")(lambda: None))
    adapter = WSGIAdapter(app)
    assert call(adapter, "GET", "/x")[0] == "503 Service Unavailable"
    assert call(adapter, "GET", "/x")[0] == "503 Service Unavailable"  # stays failed
    adapter.close()


def test_module_startup_and_shutdown_run_once_around_the_requests() -> None:
    Recorder.events.clear()
    app = Hinoki(modules=[Recorder()])
    app.include(http.get("/x")(lambda: None))
    adapter = WSGIAdapter(app)
    assert Recorder.events == []  # lazy: nothing starts before the first request
    call(adapter, "GET", "/x")
    call(adapter, "GET", "/x")
    assert Recorder.events == ["up"]
    adapter.close()
    adapter.close()
    assert Recorder.events == ["up", "down"]


def test_a_real_wsgi_server_serves_the_application() -> None:
    port = free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "granian",
            "--interface",
            "wsgi",
            "--port",
            str(port),
            "--no-log",
            "tests.runtime.wsgiapp:wsgi_app",
        ],
        cwd=ROOT,
        env={**os.environ},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_ready(port, "/ping")
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{base}/sync") as reply:
            get_body = reply.read()
            get_headers = dict(reply.headers.items())
            assert json.loads(get_body) == {"pong": "sync"}
        request = urllib.request.Request(f"{base}/sync", method="HEAD")
        with urllib.request.urlopen(request) as reply:
            assert (reply.status, reply.read()) == (200, b"")
            head_headers = dict(reply.headers.items())
        assert head_headers["content-length"] == str(len(get_body))
        assert {
            key: value
            for key, value in head_headers.items()
            if key.lower() not in {"date", "x-request-id"}
        } == {
            key: value
            for key, value in get_headers.items()
            if key.lower() not in {"date", "x-request-id"}
        }
        request = urllib.request.Request(
            f"{base}/echo",
            data=b'{"text":"x"}',
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(request) as reply:
            assert (reply.status, json.load(reply)) == (201, {"text": "x"})
        request = urllib.request.Request(
            f"{base}/find",
            data=b'{"text":"q"}',
            headers={"content-type": "application/json"},
            method="QUERY",
        )
        with urllib.request.urlopen(request) as reply:
            assert (reply.status, json.load(reply)) == (200, {"text": "q"})
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/nope")
        assert missing.value.code == 404
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)


def path_app() -> WSGIAdapter:
    @http.get("/items/{name}")
    def item(name: str) -> dict[str, str]:
        return {"name": name}

    app = Hinoki()
    app.include(item)
    return WSGIAdapter(app)


def test_the_path_is_decoded_once_like_under_asgi() -> None:
    adapter = path_app()
    try:
        # PATH_INFO arrives decoded: "%2520" from the client is "%20" here
        assert call(adapter, "GET", "/items/a%20b")[2] == {"name": "a%20b"}
        assert call(adapter, "GET", "/items/cafÃ©")[2] == {"name": "café"}
    finally:
        adapter.close()


def test_a_bad_content_length_is_a_400_not_a_crash() -> None:
    adapter = WSGIAdapter(create_app())
    try:
        seen: dict[str, str] = {}
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/echo",
            "wsgi.input": io.BytesIO(b""),
            "CONTENT_LENGTH": "abc",
            "SERVER_PROTOCOL": "HTTP/1.1",
        }

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            seen["status"] = status

        b"".join(adapter(environ, start_response))
        assert seen["status"].startswith("400")
    finally:
        adapter.close()
