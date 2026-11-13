"""The application MCP: selected actions served as tools with the caller's identity."""

import json
from typing import Any
from uuid import UUID

import pytest

from hinoki import (
    Access,
    Caller,
    Conflict,
    HinokiAPI,
    Request,
    Schema,
    delete,
    get,
    post,
)
from hinoki.mcpapp import ExposureError, expose_as_mcp
from hinoki.testing import ASGIClient
from tests.providers import TokenAuth

auth = TokenAuth()
reader = Access(auth=auth)
writer = Access(auth=auth, permissions=["posts:write"])
ITEM = UUID("00000000-0000-0000-0000-000000000001")
HEADERS = {"authorization": "Bearer editor-token"}
JSON = {"content-type": "application/json"}


class NoteIn(Schema):
    title: str


class NoteOut(Schema):
    title: str
    owner: str


@get("/notes/{note_id}", access=reader)
async def read_note(request: Request, note_id: str, caller: Caller[str]) -> NoteOut:
    """Read one note."""
    return NoteOut(title=note_id, owner=caller.id)


@get("/search", access=reader)
async def search(request: Request, q: str, limit: int = 5) -> dict[str, Any]:
    return {"q": q, "limit": limit}


@post("/notes", status=201, access=writer)
async def add_note(request: Request, data: NoteIn, caller: Caller[str]) -> NoteOut:
    return NoteOut(title=data.title, owner=caller.id)


@delete("/notes/{note_id}", status=204, access=writer)
async def drop_note(request: Request, note_id: str) -> None: ...


@get("/secret")
async def public_secret(request: Request) -> dict[str, str]:
    return {"hidden": "yes"}


def build(**options: Any) -> HinokiAPI:
    app = HinokiAPI()
    app.include(read_note, search, add_note, drop_note, public_secret)
    tools = options.pop(
        "tools", ["*.read_note", "*.search", "*.add_note", "*.drop_note"]
    )
    app.include(*expose_as_mcp(app, tools=tools, access=reader, **options))
    return app


def rpc(
    method: str, params: dict[str, Any] | None = None, ident: int = 1
) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}


async def call(
    client: ASGIClient,
    message: dict[str, Any],
    headers: dict[str, str] | None = HEADERS,
) -> Any:
    return await client.post("/mcp", json=message, headers=headers)


async def tool(client: ASGIClient, name: str, **arguments: Any) -> dict[str, Any]:
    reply = await call(
        client, rpc("tools/call", {"name": name, "arguments": arguments})
    )
    assert reply.status == 200, reply.body
    return reply.body["result"]  # type: ignore[no-any-return]


async def test_initialize_and_listing_show_only_the_allowlisted_reads() -> None:
    async with ASGIClient(build()) as client:
        hello = (await call(client, rpc("initialize"))).body["result"]
        assert hello["capabilities"] == {"tools": {"listChanged": False}}
        listed = (await call(client, rpc("tools/list"))).body["result"]["tools"]
        names = {t["name"] for t in listed}
        assert any("read_note" in n for n in names) and any(
            "search" in n for n in names
        )
        assert not any(
            "add_note" in n or "drop_note" in n or "secret" in n for n in names
        )
        schema = next(t for t in listed if "search" in t["name"])["inputSchema"]
        assert set(schema["properties"]) == {"q", "limit"} and schema["required"] == [
            "q"
        ]
        assert all(t["annotations"]["readOnlyHint"] for t in listed)


async def test_writes_are_exposed_only_when_allowed_and_say_so() -> None:
    async with ASGIClient(build(allow_writes=True)) as client:
        listed = (await call(client, rpc("tools/list"))).body["result"]["tools"]
        by_name = {t["name"]: t for t in listed}
        add = next(t for n, t in by_name.items() if "add_note" in n)
        drop = next(t for n, t in by_name.items() if "drop_note" in n)
        assert add["annotations"]["readOnlyHint"] is False
        assert drop["annotations"]["destructiveHint"] is True
        assert "body" in add["inputSchema"]["properties"]
        assert "$defs" in add["inputSchema"]


async def test_calls_run_the_real_action_with_the_callers_identity() -> None:
    async with ASGIClient(build(allow_writes=True)) as client:
        names = {
            t["name"]
            for t in (await call(client, rpc("tools/list"))).body["result"]["tools"]
        }
        read = next(n for n in names if "read_note" in n)
        add = next(n for n in names if "add_note" in n)
        search_name = next(n for n in names if "search" in n)
        got = await tool(client, read, note_id="n1")
        assert got["isError"] is False
        assert got["structuredContent"] == {"title": "n1", "owner": "u-editor"}
        found = await tool(client, search_name, q="a b&c", limit=2)
        assert found["structuredContent"] == {"q": "a b&c", "limit": 2}
        made = await tool(client, add, body={"title": "t"})
        assert (
            made["structuredContent"]["owner"] == "u-editor"
            and made["_meta"]["status"] == 201
        )
        invalid = await tool(client, add, body={})
        assert invalid["isError"] is True and invalid["_meta"]["status"] == 422


async def test_permissions_and_credentials_are_the_callers_not_the_tools() -> None:
    async with ASGIClient(build(allow_writes=True)) as client:
        assert (await call(client, rpc("tools/list"), headers=None)).status == 401
        reader_headers = {"authorization": "Bearer reader-token"}
        listed = (await call(client, rpc("tools/list"), headers=reader_headers)).body
        add = next(
            t["name"] for t in listed["result"]["tools"] if "add_note" in t["name"]
        )
        denied = await call(
            client,
            rpc("tools/call", {"name": add, "arguments": {"body": {"title": "t"}}}),
            headers=reader_headers,
        )
        assert denied.body["result"]["_meta"]["status"] == 403
        assert denied.body["result"]["isError"] is True


async def test_arguments_are_checked_and_path_values_cannot_escape() -> None:
    async with ASGIClient(build()) as client:
        listed = (await call(client, rpc("tools/list"))).body["result"]["tools"]
        read = next(t["name"] for t in listed if "read_note" in t["name"])
        for arguments in ({}, {"note_id": "x", "extra": 1}, {"note_id": {"a": 1}}):
            result = await tool(client, read, **arguments)
            assert result["isError"] is True and "_meta" not in result
        sneaky = await tool(client, read, note_id="../secret")
        assert sneaky["structuredContent"]["title"] == "../secret"  # one segment
        unknown = await call(
            client, rpc("tools/call", {"name": "nope", "arguments": {}})
        )
        assert unknown.body["error"]["code"] == -32602
        assert (await call(client, rpc("nothing"))).body["error"]["code"] == -32601


async def test_notifications_bad_json_and_oversized_messages() -> None:
    async with ASGIClient(build()) as client:
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert (await call(client, note)).status == 202
        bad = await client.post(
            "/mcp", body=b"{", headers={**HEADERS, "content-type": "application/json"}
        )
        assert bad.body["error"]["code"] == -32700
        huge = await client.post(
            "/mcp",
            body=b" " * 1_000_001,
            headers={**HEADERS, "content-type": "application/json"},
        )
        assert huge.status == 413


def test_exposure_must_be_explicit() -> None:
    app = HinokiAPI()
    app.include(read_note)
    with pytest.raises(ExposureError, match="tools"):
        expose_as_mcp(app, tools=[], access=reader)
    with pytest.raises(ExposureError, match="access"):
        expose_as_mcp(app, tools=["*"], access=None)
    assert expose_as_mcp(app, tools=["*"], access=None, public=True)


async def test_the_endpoint_never_exposes_itself_or_matches_nothing() -> None:
    app = HinokiAPI()
    app.include(read_note)
    app.include(*expose_as_mcp(app, tools=["*"], access=reader))
    async with ASGIClient(app) as client:
        listed = (await call(client, rpc("tools/list"))).body["result"]["tools"]
        assert len(listed) == 1 and "mcp" not in listed[0]["name"]
    other = HinokiAPI()
    other.include(read_note)
    with pytest.raises(ExposureError, match="no included action"):
        expose_as_mcp(other, tools=["no.such.*"], access=reader)  # fails when built


async def test_browser_style_requests_are_refused() -> None:
    async with ASGIClient(build()) as client:
        message = rpc("tools/list")
        for headers, expected in (
            ({**HEADERS, "content-type": "text/plain"}, 415),
            ({**HEADERS, **JSON, "origin": "http://evil.example"}, 403),
            ({**HEADERS, **JSON, "sec-fetch-site": "cross-site"}, 403),
        ):
            reply = await client.post(
                "/mcp", body=json.dumps(message).encode(), headers=headers
            )
            assert reply.status == expected, headers
        ok = await client.post("/mcp", json=message, headers=HEADERS)
        assert ok.status == 200
    allowed = HinokiAPI()
    allowed.include(read_note)
    allowed.include(
        *expose_as_mcp(
            allowed, tools=["*"], access=reader, allowed_origins=["http://app.example"]
        )
    )
    async with ASGIClient(allowed) as client:
        trusted = await client.post(
            "/mcp", json=message, headers={**HEADERS, "origin": "http://app.example"}
        )
        assert trusted.status == 200


async def test_the_nested_request_sees_the_client_and_a_second_mount_is_hidden() -> (
    None
):
    seen: dict[str, Any] = {}

    @get("/who", access=reader)
    async def who(request: Request) -> dict[str, str]:
        seen["client"], seen["host"] = request.client, request.headers.get("host")
        return {}

    app = HinokiAPI()
    app.include(who)
    app.include(*expose_as_mcp(app, tools=["*.who"], access=reader))
    app.include(*expose_as_mcp(app, tools=["*"], access=reader, path="/mcp2"))
    async with ASGIClient(app) as client:
        listed = (await call(client, rpc("tools/list"))).body["result"]["tools"]
        assert len(listed) == 1 and listed[0]["name"].endswith("_who")
        # the in-process client has no peer: the field is forwarded as it is
        result = await tool(client, listed[0]["name"])
        assert result["isError"] is False and seen["client"] is None


async def test_declared_errors_appear_in_the_tool_description() -> None:
    class Gone(Conflict):
        code = "gone_already"

    @get("/gone", access=reader, errors=(Gone,))
    async def gone() -> None: ...

    app = HinokiAPI()
    app.include(gone)
    app.include(*expose_as_mcp(app, tools=["*.gone"], access=reader))
    async with ASGIClient(app) as client:
        listed = (await call(client, rpc("tools/list"))).body["result"]["tools"]
        assert "May fail with: 409 gone_already" in listed[0]["description"]
