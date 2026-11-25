"""Drives the blog's application MCP endpoint over HTTP with the official client.

    uvx --with-editable . --from mcp python tests/mcpclient/verifyapp.py

Starts the blog under Granian on a disposable SQLite file, creates its tables, one
publication, one editor and one draft, then lists tools and calls one with the
editor's bearer token. Not part of pytest.
"""

import asyncio
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
PORT = 8807
SETUP = """
import asyncio, os
from pathlib import Path
from blog.apps.blog.models import Post, Publication, User
from blog.apps.blog.security import hash_token
from hinoki.orm import migrator
from hinoki.db import Database
from hinoki.orm import use_database

async def main():
    db = await Database.connect(os.environ["BLOG_DATABASE__URL"], max_size=1)
    await migrator.apply(db, Path("blog/apps/blog/migrations"))
    with use_database(db):
        press = await Publication.acreate(name="Press", slug="press")
        ann = await User.acreate(
            publication_id=press.id, name="Ann",
            token_hash=hash_token("ann-token"), is_editor=True,
        )
        await Post.acreate(
            publication_id=press.id, author_id=ann.id,
            title="write the MCP guide", slug="mcp-guide", body="b",
        )
    db.close()

asyncio.run(main())
"""


async def main() -> None:
    project = ROOT / "examples" / "blog"
    database = Path(tempfile.mkdtemp()) / "blog.sqlite3"
    env = {
        **os.environ,
        "BLOG_DATABASE__URL": f"sqlite://{database}",
        "BLOG_ADMIN__SECRET": "m" * 40,
        "PYTHONPATH": str(project),
    }
    subprocess.run(
        ["uv", "run", "--locked", "python", "-c", SETUP],
        cwd=project,
        env=env,
        check=True,
    )
    server = subprocess.Popen(
        [
            *("uv", "run", "--locked", "granian", "--interface", "asgi"),
            *("--port", str(PORT), "blog.main:app"),
        ],
        cwd=project,
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
