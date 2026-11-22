"""Events (the audit trail), MCP, the admin and metrics settings."""

from typing import Any

from blog.apps.blog.models import AuditEntry
from blog.settings import BlogSettings
from blogworld import ADMIN_SECRET, World

from hinoki.db import Database
from hinoki.orm import use_database

MCP_HEADERS = {
    "authorization": "Bearer ada-token",
    "accept": "application/json, text/event-stream",
}


def rpc(method: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}


async def audit_kinds(world: World, post_id: str) -> list[str]:
    database = await Database.connect(world.url, max_size=1)
    try:
        with use_database(database):
            rows = await AuditEntry.filter(post_id=post_id)
    finally:
        database.close()
    return sorted(row.kind for row in rows)


async def test_creating_and_publishing_write_audit_entries(world: World) -> None:
    created = await world.client.post(
        "/posts", json={"title": "T", "slug": "t", "body": "b"}, token="ada-token"
    )
    post_id = created.body["id"]
    assert await audit_kinds(world, post_id) == ["drafted"]
    await world.client.post(f"/posts/{post_id}/publish", token="ada-token")
    assert await audit_kinds(world, post_id) == ["drafted", "published"]


async def test_mcp_exposes_one_read_only_tool_to_an_editor(world: World) -> None:
    listed = await world.client.post(
        "/mcp", json=rpc("tools/list"), headers=MCP_HEADERS
    )
    tools = listed.body["result"]["tools"]
    assert len(tools) == 1 and "drafts" in tools[0]["name"]
    assert (await world.client.post("/mcp", json=rpc("tools/list"))).status == 401


async def test_the_admin_serves_its_sign_in_page(world: World) -> None:
    reply = await world.client.get("/admin/login")
    assert reply.status == 200 and "<form" in reply.raw.decode()


def test_metrics_listen_is_an_explicit_setting() -> None:
    env = {"BLOG_METRICS__LISTEN": "127.0.0.1:9100", "BLOG_ADMIN__SECRET": ADMIN_SECRET}
    assert BlogSettings.load(env=env).metrics.listen == "127.0.0.1:9100"
