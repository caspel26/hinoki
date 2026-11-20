"""The same requests to the same application under Kiso and under Granian: a
middleware answers identically on both."""

import contextlib
import http.client
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.runtime.harness import free_port, wait_ready

ROOT = Path(__file__).resolve().parents[2]
TARGET = "tests.runtime.middlewareapp:create_app"
pytestmark = pytest.mark.runtime

# What each server adds on its own, or what differs per request.
IGNORED = {"server", "date", "x-request-id"}

REQUESTS: list[tuple[str, str, dict[str, str]]] = [
    ("GET", "/plain", {}),
    ("GET", "/tagged", {}),
    ("GET", "/guarded", {}),
    ("GET", "/guarded", {"x-allow": "1"}),
    ("GET", "/missing", {}),
    ("GET", "/boom", {}),
    ("GET", "/raises", {}),
    ("GET", "/head/7?a=1", {}),
    ("GET", "/nowhere", {}),
    ("POST", "/tagged", {}),
    ("GET", "/openapi.json", {}),
]

Answer = tuple[int, list[tuple[str, str]], object]


def start(server: str, port: int) -> subprocess.Popen[bytes]:
    command = [sys.executable, "-m", "hinoki", "serve", TARGET, "--port", str(port)]
    return subprocess.Popen(
        [*command, "--server", server],
        cwd=ROOT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def answer(port: int, method: str, target: str, headers: dict[str, str]) -> Answer:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, target, headers=headers)
        reply = connection.getresponse()
        raw = reply.read()
        kept = sorted(
            (name.lower(), value)
            for name, value in reply.getheaders()
            if name.lower() not in IGNORED
        )
        try:
            body: object = json.loads(raw)
        except ValueError:
            body = raw.decode()
        if isinstance(body, dict):
            body.pop("request_id", None)  # unique per request
        return reply.status, kept, body
    finally:
        connection.close()


def label(method: str, target: str, headers: dict[str, str]) -> str:
    return f"{method} {target}" + (" allowed" if headers else "")


def collect(server: str) -> dict[str, Answer]:
    port = free_port()
    process = start(server, port)
    try:
        wait_ready(port, "/plain")
        return {label(m, t, h): answer(port, m, t, h) for m, t, h in REQUESTS}
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=20)


def test_kiso_and_granian_answer_the_same() -> None:
    kiso, granian = collect("kiso"), collect("granian")
    assert kiso == granian


def test_the_answers_are_the_expected_ones() -> None:
    found = collect("kiso")
    assert found["GET /plain"][0] == 200
    assert ("x-app", "/tagged") in found["GET /tagged"][1]
    assert ("x-route", "/tagged") in found["GET /tagged"][1]
    assert found["GET /guarded"][0] == 403
    assert found["GET /guarded allowed"][0] == 200
    assert found["GET /guarded"][2] == "no"
    assert found["GET /missing"][0] == 404
    assert ("x-route", "/missing") in found["GET /missing"][1]
    assert found["GET /boom"][0] == 500
    assert "secret detail" not in json.dumps(found["GET /boom"][2])
    assert found["GET /raises"][0] == 404
    assert found["GET /head/7?a=1"][2] == "GET|/head/7|/head/{item_id}|a=1"
    assert found["GET /nowhere"][0] == 404
    assert ("x-app", "/tagged") not in found["GET /nowhere"][1]
    assert found["POST /tagged"][0] == 405
    assert found["GET /openapi.json"][0] == 200
