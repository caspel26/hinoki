"""Drives the development MCP server with the official `mcp` client package.

    uvx --from mcp python tests/mcpclient/verify.py

Not part of pytest (it needs the third-party client); run it after changing
`hinoki/mcp.py`. Exits non-zero when an expectation fails.
"""

import asyncio
import os
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
SERVER = StdioServerParameters(
    command="uv",
    args=[
        "run",
        "--locked",
        "hinoki",
        "mcp",
        "tests.projects.rulesapp:create_app",
        "--rules",
        "tests.projects.rulesapp:rules",
        "--scenarios",
        "read",
    ],
    cwd=str(ROOT),
    env={**os.environ, "PYTHONPATH": str(ROOT)},
)
FIELDS = [{"name": "title", "max_length": 80}]
# (tool, arguments, expect an error result, key the structured result must have)
CALLS: list[tuple[str, dict[str, Any], bool, str]] = [
    ("check_project", {}, False, "diagnostics"),
    ("explain_error", {"code": "HNK-ACC-001"}, False, "meaning"),
    ("propose_model", {"name": "Article", "fields": FIELDS}, False, "source"),
    ("run_scenario", {"path": "/health"}, False, "status"),
    ("run_scenario", {"method": "POST", "path": "/items"}, True, "error"),
]


async def main() -> None:
    async with (
        stdio_client(SERVER) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        hello = await session.initialize()
        print("server:", hello.server_info.name, hello.protocol_version)
        listed = {tool.name for tool in (await session.list_tools()).tools}
        assert {"check_project", "propose_model", "run_scenario"} <= listed
        for name, arguments, wants_error, key in CALLS:
            result = await session.call_tool(name, arguments)
            content = result.structured_content or {}
            print(name, "error" if result.is_error else "ok", sorted(content))
            assert result.is_error == wants_error, name
            assert key in content, (name, key)


asyncio.run(main())
