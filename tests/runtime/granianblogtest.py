"""The reference blog served by a real Granian process over a real socket."""

import asyncio
import json
import os
import signal
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from examples.blog.models import User
from hinoki.db import Database
from hinoki.orm import use_database
from tests.blogschema import drop, reset
from tests.runtime.harness import free_port, spawn, wait_ready

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.runtime,
    pytest.mark.skipif(URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"),
]
ROOT = Path(__file__).resolve().parents[2]


def call(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method=method
    )
    if body is not None:
        request.add_header("content-type", "application/json")
    if token is not None:
        request.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


async def test_blog_flow_over_http_and_clean_shutdown(tmp_path: Path) -> None:
    assert URL is not None
    admin = await Database.connect(URL, max_size=1)
    await reset(admin)
    with use_database(admin):
        await User.acreate(name="Ada", token="editor-token", is_editor=True)
    port = free_port()
    proc = spawn(
        "examples.blog.main:create_app",
        tmp_path / "events.log",
        port,
        factory=True,
        cwd=ROOT,
        BLOG_DATABASE_URL=URL,
        PYTHONPATH=str(ROOT),
    )
    try:
        await asyncio.to_thread(wait_ready, port, "/health")
        body = json.dumps({"title": "Hi", "slug": "hi", "body": "b"}).encode()
        status, created = await asyncio.to_thread(
            call, port, "POST", "/posts", body, "editor-token"
        )
        assert status == 201
        path = f"/posts/{created['id']}/publish"
        status, _ = await asyncio.to_thread(
            call, port, "POST", path, None, "editor-token"
        )
        assert status == 200
        status, listing = await asyncio.to_thread(call, port, "GET", "/posts")
        assert (status, listing["total"]) == (200, 1)
        status, _ = await asyncio.to_thread(call, port, "POST", "/posts", body)
        assert status == 401
        proc.send_signal(signal.SIGTERM)
        assert await asyncio.to_thread(proc.wait, 20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        await drop(admin)
        admin.close()
