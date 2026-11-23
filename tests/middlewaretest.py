"""Middleware: order, answers, errors, rollback and the early policies."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from hinoki import Access, Hinoki, Schema, http
from hinoki.auth import BearerAuth, Caller
from hinoki.cli.routes import lanes_of
from hinoki.db import Database, DatabaseModule
from hinoki.errors import NotFound
from hinoki.http import Middleware, MiddlewareRequest, Next, Response
from hinoki.orm import current_database, use_database
from hinoki.testing import ASGIClient

TABLE = "middleware_items"
events: list[str] = []


class Item(Schema):
    name: str


class Tag(Middleware):
    def __init__(self, label: str) -> None:
        self.label = label

    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        events.append(f"{self.label}:in")
        response = await call_next()
        events.append(f"{self.label}:out")
        return response.with_header(f"x-{self.label}", "1")


class Guard(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        if "x-allow" not in request.headers:
            return Response(403, "no", content_type="text/plain")
        return await call_next()


class Twice(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        await call_next()
        return await call_next()


class Raises(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        raise NotFound()


class Wrong(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        return None  # type: ignore[return-value]


class Head(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        seen = (request.method, request.path, request.route, request.query)
        return Response(200, "|".join(seen), content_type="text/plain")


class Forge(Middleware):
    """Tries to authenticate the request on its behalf."""

    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        try:
            request.headers["authorization"] = "Bearer good"  # type: ignore[index]
        except TypeError:
            events.append("read-only")
        return await call_next()


class TokenAuth(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        return Caller(id="u", user="u") if token == "good" else None  # noqa: S105


class Rows(Middleware):
    """Reads the table once the action has answered."""

    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        response = await call_next()
        rows = await current_database().fetch(f"SELECT name FROM {TABLE}")  # noqa: S608
        events.append(f"status={response.status} rows={len(rows)}")
        return response


@http.get("/plain")
async def plain() -> dict[str, int]:
    return {"n": 1}


@http.get("/tagged", middleware=[Tag("route")])
async def tagged() -> dict[str, int]:
    events.append("handler")
    return {"n": 1}


@http.get("/guarded", middleware=[Guard()])
async def guarded() -> dict[str, int]:
    events.append("handler")
    return {"n": 1}


@http.get("/twice", middleware=[Twice()])
async def twice() -> dict[str, int]:
    return {"n": 1}


@http.get("/raises", middleware=[Raises()])
async def raises() -> dict[str, int]:
    return {"n": 1}


@http.get("/wrong", middleware=[Wrong()])
async def wrong() -> dict[str, int]:
    return {"n": 1}


@http.get("/head/{item_id}", middleware=[Head()])
async def head(item_id: int) -> dict[str, int]:
    return {"n": item_id}


@http.get("/missing", middleware=[Tag("route")])
async def missing() -> dict[str, int]:
    raise NotFound()


@http.get("/private", access=Access(auth=TokenAuth()), middleware=[Forge()])
async def private() -> dict[str, int]:
    events.append("handler")
    return {"n": 1}


@http.post("/items", atomic=True, middleware=[Rows()])
async def create(body: Item, fail: bool = False) -> Item:
    await current_database().execute(
        f"INSERT INTO {TABLE} (name) VALUES (?)",  # noqa: S608
        body.name,
    )
    if fail:
        raise ZeroDivisionError
    return body


@pytest.fixture(autouse=True)
def clear_events() -> None:
    events.clear()


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite://{tmp_path / 'm.sqlite3'}"


@pytest.fixture
async def database(url: str) -> AsyncIterator[Database]:
    opened = await Database.connect(url, max_size=2)
    await opened.execute(f"CREATE TABLE {TABLE} (name text PRIMARY KEY)")
    try:
        with use_database(opened):
            yield opened
    finally:
        opened.close()


def app_with(
    *routes: Callable[..., object], middleware: list[Middleware] | None = None
) -> Hinoki:
    return Hinoki(
        include=list(routes), middleware=middleware or [], openapi_url="/openapi.json"
    )


async def test_a_route_without_middleware_is_unchanged() -> None:
    async with ASGIClient(app_with(plain)) as client:
        reply = await client.get("/plain")
    assert (reply.status, reply.body) == (200, {"n": 1})
    assert events == []


async def test_application_middleware_runs_before_the_route_s_around_the_action() -> (
    None
):
    app = app_with(tagged, middleware=[Tag("app")])
    async with ASGIClient(app) as client:
        reply = await client.get("/tagged")
    assert reply.status == 200
    assert events == ["app:in", "route:in", "handler", "route:out", "app:out"]
    assert reply.headers["x-app"] == reply.headers["x-route"] == "1"


async def test_a_middleware_can_answer_without_calling_the_action() -> None:
    async with ASGIClient(app_with(guarded)) as client:
        refused = await client.get("/guarded")
        allowed = await client.get("/guarded", headers={"x-allow": "1"})
    assert refused.status == 403
    assert allowed.status == 200
    assert events == ["handler"]


async def test_the_answer_still_gets_the_final_headers() -> None:
    async with ASGIClient(app_with(guarded)) as client:
        refused = await client.get("/guarded")
    assert "x-request-id" in refused.headers
    assert refused.headers["content-length"] == "2"


async def test_an_error_of_the_action_reaches_the_middleware_as_an_answer() -> None:
    async with ASGIClient(app_with(missing)) as client:
        reply = await client.get("/missing")
    assert reply.status == 404
    assert events == ["route:in", "route:out"]
    assert reply.headers["x-route"] == "1"


async def test_a_middleware_that_raises_is_translated_like_an_action_error() -> None:
    async with ASGIClient(app_with(raises)) as client:
        reply = await client.get("/raises")
    assert reply.status == 404


async def test_calling_next_twice_is_a_500() -> None:
    async with ASGIClient(app_with(twice)) as client:
        reply = await client.get("/twice")
    assert reply.status == 500


async def test_a_middleware_must_return_a_response() -> None:
    async with ASGIClient(app_with(wrong)) as client:
        reply = await client.get("/wrong")
    assert reply.status == 500


async def test_the_middleware_sees_the_head_and_the_route_template() -> None:
    async with ASGIClient(app_with(head)) as client:
        reply = await client.get("/head/7", query="a=1")
    assert reply.body == "GET|/head/7|/head/{item_id}|a=1"


async def test_a_middleware_cannot_change_the_request_the_access_policy_sees() -> None:
    async with ASGIClient(app_with(private)) as client:
        refused = await client.get("/private")
        allowed = await client.get("/private", headers={"authorization": "Bearer good"})
    assert (refused.status, allowed.status) == (401, 200)
    assert events == ["read-only", "read-only", "handler"]


async def test_the_server_s_own_answers_are_outside_the_middleware() -> None:
    app = app_with(tagged, middleware=[Tag("app")])
    async with ASGIClient(app) as client:
        unknown = await client.get("/nowhere")
        wrong_method = await client.post("/tagged")
        document = await client.get("/openapi.json")
    assert (unknown.status, wrong_method.status, document.status) == (404, 405, 200)
    assert events == []


async def test_the_transaction_is_rolled_back_before_the_middleware_sees_the_answer(
    database: Database, url: str
) -> None:
    app = Hinoki(include=[create], modules=[DatabaseModule(url)])
    async with ASGIClient(app) as client:
        failed = await client.post("/items", json={"name": "a"}, query="fail=true")
        kept = await client.post("/items", json={"name": "b"})
    assert failed.status == 500
    assert kept.status == 200
    assert events == ["status=500 rows=0", "status=200 rows=1"]


def test_routes_with_middleware_leave_the_native_lane() -> None:
    app = app_with(plain, tagged)
    lanes = {route.path: lanes_of(app)[id(route)] for route in app.routes}
    assert lanes["/plain"] == ("native", "native_dispatch")
    assert lanes["/tagged"] == ("python", "middleware")


@pytest.mark.parametrize("bad", [object(), lambda *_: None])
def test_middleware_must_have_an_async_call(bad: object) -> None:
    with pytest.raises(TypeError, match="async def __call__"):
        Hinoki(middleware=[bad])  # type: ignore[list-item]


async def test_the_base_middleware_passes_the_request_through() -> None:
    @http.get("/bare", middleware=[Middleware()])
    async def bare() -> dict[str, str]:
        return {"ok": "yes"}

    app = Hinoki()
    app.include(bare)
    async with ASGIClient(app) as client:
        assert (await client.get("/bare")).body == {"ok": "yes"}
