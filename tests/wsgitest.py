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
from tests.runtime.wsgiapp import create_app

ROOT = Path(__file__).resolve().parents[1]


def call(
    adapter: WSGIAdapter, method: str, path: str, body: bytes = b"", query: str = ""
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
        "CONTENT_TYPE": "application/json",
    }
    payload = b"".join(adapter(environ, start_response))
    return seen["status"], seen["headers"], json.loads(payload) if payload else None


def test_async_sync_and_body_handlers_answer_in_process() -> None:
    adapter = WSGIAdapter(create_app())
    try:
        assert call(adapter, "GET", "/ping")[::2] == ("200 OK", {"pong": "async"})
        assert call(adapter, "GET", "/sync")[::2] == ("200 OK", {"pong": "sync"})
        status, headers, body = call(adapter, "POST", "/echo", b'{"text":"hi"}')
        assert (status, body) == ("201 Created", {"text": "hi"})
        assert "x-request-id" in headers
        assert call(adapter, "GET", "/nope")[0] == "404 Not Found"
        assert call(adapter, "POST", "/echo", b"x" * (1024 * 1024 + 5))[0].startswith(
            "413"
        )
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
            assert json.load(reply) == {"pong": "sync"}
        request = urllib.request.Request(
            f"{base}/echo",
            data=b'{"text":"x"}',
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(request) as reply:
            assert (reply.status, json.load(reply)) == (201, {"text": "x"})
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/nope")
        assert missing.value.code == 404
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
