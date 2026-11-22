"""The HTTP QUERY method: a safe request that carries a body."""

from typing import Any

import pytest
from openapi_spec_validator import validate

from hinoki import API, Access, Hinoki, Request, RouteGroup, Schema, http
from hinoki.ai.mcp import DevProject, tool_explain_action
from hinoki.ai.mcpapp import expose_as_mcp
from hinoki.ai.samples import request_cases
from hinoki.errors import RegistrationError
from hinoki.rules import RuleSet
from hinoki.scenario import ScenarioError, run_scenario
from hinoki.settings import AppSettings, DocsSettings
from hinoki.testing import ASGIClient, SyncClient
from hinoki.wsgi import WSGIAdapter
from tests.providers import TokenAuth
from tests.wsgitest import call

reader = Access(auth=TokenAuth())
HEADERS = {"authorization": "Bearer reader-token"}


class Search(Schema):
    text: str
    limit: int = 10


class Found(Schema):
    text: str
    limit: int
    via: str


@http.query("/search")
async def search(data: Search) -> Found:
    """Search with a structured body."""
    return Found(text=data.text, limit=data.limit, via="function")


@http.get("/search")
async def search_page(q: str = "") -> dict[str, str]:
    return {"q": q}


@http.query("/raw")
def raw(request: Request) -> dict[str, Any]:
    return {"method": request.method, "data": request.data}


@http.get("/only-get")
async def only_get() -> dict[str, bool]:
    return {"ok": True}


group = RouteGroup("/reports")
atomic = RouteGroup("/ledger", atomic=True)


@group.query("/find")
async def find(data: Search) -> Found:
    return Found(text=data.text, limit=data.limit, via="group")


@group.post("/make")
async def make(data: Search) -> Found:
    return Found(text=data.text, limit=data.limit, via="post")


@atomic.query("/entries")
async def entries(data: Search) -> Found:
    return Found(text=data.text, limit=data.limit, via="atomic")


@atomic.post("/entries")
async def add_entry(data: Search) -> Found:
    return Found(text=data.text, limit=data.limit, via="atomic")


class Library(API):
    prefix = "/library"

    @http.query("/books", access=reader)
    async def books(self, data: Search) -> Found:
        return Found(text=data.text, limit=data.limit, via="class")


def build(**options: Any) -> Hinoki:
    app = Hinoki(**options)
    app.include(search, search_page, raw, only_get, group, Library)
    return app


async def test_query_binds_its_body_like_post_and_reports_the_same_errors() -> None:
    async with ASGIClient(build()) as client:
        ok = await client.query("/search", json={"text": "hinoki", "limit": 3})
        assert ok.status == 200
        assert ok.body == {"text": "hinoki", "limit": 3, "via": "function"}
        # The query string is still the query string.
        assert (await client.get("/search", query="q=x")).body == {"q": "x"}
        invalid = await client.query("/search", json={"limit": "many"})
        assert invalid.status == 422
        assert {d["path"] for d in invalid.body["details"]} == {
            "data.text",
            "data.limit",
        }
        malformed = await client.query(
            "/search", body=b"{", headers={"content-type": "application/json"}
        )
        assert malformed.status == 400
        unsupported = await client.query(
            "/search", body=b"<a/>", headers={"content-type": "application/xml"}
        )
        assert unsupported.status == 415
        untyped = await client.query("/raw", json={"any": [1, 2]})
        assert untyped.body == {"method": "QUERY", "data": {"any": [1, 2]}}


async def test_groups_and_api_classes_declare_query_actions() -> None:
    async with ASGIClient(build()) as client:
        found = await client.query("/reports/find", json={"text": "a"})
        assert found.body["via"] == "group"
        assert (await client.query("/library/books", json={"text": "a"})).status == 401
        allowed = await client.query(
            "/library/books", json={"text": "a"}, headers=HEADERS
        )
        assert allowed.status == 200 and allowed.body["via"] == "class"


def test_an_atomic_group_does_not_wrap_a_query_in_a_transaction() -> None:
    app = Hinoki()
    app.include(atomic)
    routes = {route.name: route for route in app.routes}
    assert routes["entries"].method == "QUERY"
    assert routes["entries"].atomic is False
    assert routes["add_entry"].atomic is True


def test_a_query_action_can_ask_for_a_transaction_explicitly() -> None:
    @http.query("/snapshot", atomic=True)
    async def snapshot(data: Search) -> Found:
        return Found(text=data.text, limit=data.limit, via="snapshot")

    app = Hinoki()
    app.include(snapshot)
    assert app.routes[0].atomic is True


async def test_allow_lists_query_and_head_does_not_fall_back_to_it() -> None:
    async with ASGIClient(build()) as client:
        refused = await client.query("/only-get", json={})
        assert refused.status == 405
        assert refused.headers["allow"] == "GET, HEAD"
        wrong = await client.delete("/search")
        assert wrong.status == 405
        assert set(wrong.headers["allow"].split(", ")) == {"GET", "HEAD", "QUERY"}
        head = await client.request("HEAD", "/raw")
        assert head.status == 405 and head.headers["allow"] == "QUERY"


def test_a_schema_parameter_is_still_refused_on_a_get_action() -> None:
    @http.get("/bad")
    async def bad(data: Search) -> None: ...

    app = Hinoki()
    with pytest.raises(RegistrationError, match="POST/PUT/PATCH/QUERY"):
        app.include(bad)


def test_openapi_32_describes_query_as_a_first_class_method() -> None:
    document = build().openapi()
    validate(document)
    assert document["openapi"] == "3.2.0"
    item = document["paths"]["/search"]
    assert set(item) == {"get", "query"}
    operation = item["query"]
    assert operation["summary"] == "Search with a structured body."
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/Search"}
    assert "200" in operation["responses"] and "422" in operation["responses"]
    assert not any("x-query" in path for path in document["paths"].values())


def test_openapi_31_describes_query_under_an_extension() -> None:
    document = build(openapi_version="3.1").openapi()
    validate(document)
    assert document["openapi"] == "3.1.0"
    item = document["paths"]["/search"]
    assert set(item) == {"get", "x-query"}
    assert item["x-query"] == build().openapi()["paths"]["/search"]["query"]


def test_both_versions_name_every_query_operation_once() -> None:
    def ids(document: dict[str, Any], key: str) -> list[str]:
        return [
            item[key]["operationId"]
            for item in document["paths"].values()
            if key in item
        ]

    new = ids(build().openapi(), "query")
    old = ids(build(openapi_version="3.1").openapi(), "x-query")
    assert new == old and len(new) == len(set(new)) == 4


async def test_the_application_mcp_exposes_query_as_a_read() -> None:
    for options in ({}, {"openapi_version": "3.1"}):
        await check_mcp_exposes_query(build(**options))


async def check_mcp_exposes_query(app: Hinoki) -> None:
    app.include(*expose_as_mcp(app, tools=["*.books", "*.make"], access=reader))
    async with ASGIClient(app) as client:

        async def rpc(method: str, params: dict[str, Any]) -> Any:
            message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            reply = await client.post("/mcp", json=message, headers=HEADERS)
            return reply.body["result"]

        tools = (await rpc("tools/list", {}))["tools"]
        assert len(tools) == 1  # the POST needs allow_writes; the QUERY does not
        assert tools[0]["annotations"]["readOnlyHint"] is True
        assert "body" in tools[0]["inputSchema"]["properties"]
        arguments = {"body": {"text": "x", "limit": 2}}
        result = await rpc(
            "tools/call", {"name": tools[0]["name"], "arguments": arguments}
        )
        assert result["isError"] is False
        assert result["structuredContent"] == {"text": "x", "limit": 2, "via": "class"}


def test_read_only_scenarios_may_send_a_query() -> None:
    args = {"method": "QUERY", "path": "/search", "json": {"text": "s"}}
    result = run_scenario(build(), args, "read")
    assert result["status"] == 200 and result["json"]["text"] == "s"
    with pytest.raises(ScenarioError, match="changes state"):
        run_scenario(build(), {**args, "method": "POST"}, "read")


def test_the_sync_client_and_the_wsgi_adapter_carry_query() -> None:
    with SyncClient(build()) as client:
        assert client.query("/search", json={"text": "sync"}).body["text"] == "sync"
    adapter = WSGIAdapter(build())
    try:
        status, _, body = call(adapter, "QUERY", "/search", b'{"text":"wsgi"}')
        assert status == "200 OK" and body["text"] == "wsgi"
        status, headers, _ = call(adapter, "QUERY", "/only-get")
        assert status.startswith("405") and headers["allow"] == "GET, HEAD"
    finally:
        adapter.close()


@pytest.mark.parametrize("version", ["3.1", "3.2"])
def test_explain_action_and_request_cases_find_query_in_either_version(
    version: str,
) -> None:
    app = build(openapi_version=version)
    project = DevProject(app, RuleSet())
    described = tool_explain_action(project, {"action": "search"})
    assert described["operation"]["operationId"] == "search"
    cases = [c for c in request_cases(app.openapi()) if c.method == "QUERY"]
    assert "/search" in {c.path for c in cases}


def test_the_docs_setting_chooses_the_version_and_the_argument_wins() -> None:
    settings = AppSettings(docs=DocsSettings(openapi_version="3.1"))
    assert Hinoki(settings=settings).openapi()["openapi"] == "3.1.0"
    explicit = Hinoki(settings=settings, openapi_version="3.2")
    assert explicit.openapi()["openapi"] == "3.2.0"
