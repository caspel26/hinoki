"""Drives the tracker's application MCP endpoint over HTTP with the official client.

    uvx --with-editable . --from mcp python tests/mcpclient/verifyapp.py

Needs a disposable PostgreSQL in HINOKI_TEST_DATABASE_URL. Starts the tracker
under Granian, creates its tables, one member and one task, then lists tools and
calls one with the member's bearer token. Not part of pytest.
"""

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ["HINOKI_TEST_DATABASE_URL"]
PORT = 8791
SETUP = """
import asyncio, os
from pathlib import Path
from examples.tracker.models import Member, Task
from examples.tracker import operations
from examples.tracker.schemas import TaskIn
from hinoki.orm import migrator
from hinoki.db import Database
from hinoki.orm import use_database

async def main():
    db = await Database.connect(os.environ["HINOKI_TEST_DATABASE_URL"], max_size=2)
    for s in ("DROP TABLE IF EXISTS tracker_tasks", "DROP TABLE IF EXISTS tracker_members",
              "DROP TABLE IF EXISTS hinoki_migrations"):
        await db.execute(s)
    await migrator.apply(db, Path("examples/tracker/migrations"))
    with use_database(db):
        ann = await Member.acreate(name="Ann", token="ann-token")
        await operations.create_task(ann.id, TaskIn(title="write the MCP guide"))
    db.close()

asyncio.run(main())
"""


async def main() -> None:
    env = {**os.environ, "TRACKER_DATABASE_URL": URL}
    subprocess.run(
        ["uv", "run", "--locked", "python", "-c", SETUP], cwd=ROOT, env=env, check=True
    )
    server = subprocess.Popen(
        [
            *("uv", "run", "--locked", "granian", "--interface", "asgi", "--factory"),
            *("--port", str(PORT), "examples.tracker.app:create_app"),
        ],
        cwd=ROOT,
        env=env,
        start_new_session=True,  # so the whole process group can be stopped
    )
    try:
        time.sleep(4)
        headers = {"Authorization": "Bearer ann-token"}
        async with (
            httpx2.AsyncClient(headers=headers) as http,
            streamable_http_client(
                f"http://127.0.0.1:{PORT}/mcp", http_client=http
            ) as (
                reader,
                writer,
                *_,
            ),
            ClientSession(reader, writer) as session,
        ):
            hello = await session.initialize()
            print("server:", hello.server_info.name, hello.protocol_version)
            tools = (await session.list_tools()).tools
            print("tools:", [t.name for t in tools])
            assert len(tools) == 1
            result = await session.call_tool(tools[0].name, {})
            print("is_error:", result.is_error, result.structured_content)
            assert not result.is_error
            assert "write the MCP guide" in str(result.structured_content)
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=15)


asyncio.run(main())
