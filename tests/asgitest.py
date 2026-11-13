"""In-process tests of the ASGI adapter (no sockets)."""

import asyncio
import json
import threading
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest

from hinoki import API, HinokiAPI, RegistrationError, RuntimeConfig, context, get, post
from hinoki.request import Request
from tests.harness import Client, Pool

release = threading.Event()


@get("/ping")
def ping(request: Request) -> dict[str, str]:
    return {"pong": "yes"}


@get("/items/{item_id}")
async def item(request: Request, item_id: int) -> dict[str, int]:
    return {"id": item_id}


@get("/rid")
def request_id_in_thread(request: Request) -> dict[str, str | None]:
    return {"rid": context.request_id.get()}


@get("/boom")
async def boom(request: Request) -> None:
    raise RuntimeError("secret internal detail")


@get("/slow-async")
async def slow_async(request: Request) -> None:
    await asyncio.sleep(5)


@get("/block")
def block(request: Request) -> None:
    release.wait(5)


@post("/made", status=201)
async def made(request: Request) -> dict[str, bool]:
    return {"ok": True}


@get("/nothing")
async def nothing(request: Request) -> None:
    return None


@get("/echo/{name}")
async def echo(request: Request, name: str) -> dict[str, str]:
    return {"name": name}


@get("/gone", status=204)
async def gone(request: Request) -> dict[str, str]:
    return {"ignored": "body"}


@get("/ratio/{value}")
async def ratio(request: Request, value: float) -> dict[str, float]:
    return {"value": value}


class Things(API, path="/things"):
    @get("/{thing_id}")
    def one(self, request: Request, thing_id: UUID) -> dict[str, str]:
        return {"id": str(thing_id)}


@get("/p/{p}")
def wrong_name(request: Request, q: str) -> None: ...


@get("/t/{t}")
def bad_type(request: Request, t: list[int]) -> None: ...


def make_app(**config: float) -> HinokiAPI:
    app = HinokiAPI(runtime=RuntimeConfig(**config))  # type: ignore[arg-type]
    app.include(
        ping,
        item,
        request_id_in_thread,
        boom,
        slow_async,
        block,
        made,
        nothing,
        echo,
        gone,
        ratio,
        Things,
    )
    return app


@pytest.fixture(autouse=True)
def reset_release() -> Iterator[None]:
    release.clear()
    yield
    release.set()


async def serve(pool: Pool, **config: float) -> Client:
    return await pool.start(make_app(**config))


async def test_dispatch_status_codes_and_bodies(pool: Pool) -> None:
    client = await serve(pool)
    ok = await client.request("GET", "/ping")
    assert (ok.status, ok.body) == (200, {"pong": "yes"})
    assert (await client.request("GET", "/items/42")).body == {"id": 42}
    assert (await client.request("POST", "/made")).status == 201
    empty = await client.request("GET", "/nothing")
    assert (empty.status, empty.body) == (204, None)
    assert (await client.request("GET", "/nope")).status == 404
    wrong = await client.request("DELETE", "/ping")
    assert (wrong.status, wrong.headers["allow"]) == (405, "GET")
    bad = await client.request("GET", "/items/abc")
    assert bad.status == 422
    assert bad.body["details"][0]["path"] == "item_id"
    thing = UUID(int=5)
    assert (await client.request("GET", f"/things/{thing}")).body == {"id": str(thing)}
    assert (await client.request("GET", "/things/not-a-uuid")).status == 422


async def test_internal_errors_do_not_leak_and_carry_request_id(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request("GET", "/boom")
    assert reply.status == 500
    assert reply.body["request_id"] == reply.headers["x-request-id"]
    assert "secret" not in json.dumps(reply.body)


async def test_request_id_reaches_sync_handler_thread(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request("GET", "/rid")
    assert reply.body["rid"] == reply.headers["x-request-id"]
    assert context.request_id.get() is None  # reset after the request


async def test_request_timeout_maps_to_504(pool: Pool) -> None:
    client = await serve(pool, request_timeout=0.1)
    assert (await client.request("GET", "/slow-async")).status == 504


async def test_saturated_executor_sheds_sync_routes_but_async_routes_keep_working(
    pool: Pool,
) -> None:
    client = await serve(pool, sync_workers=1, sync_queue=0, request_timeout=5)
    first = asyncio.create_task(client.request("GET", "/block"))
    await asyncio.sleep(0.1)
    shed = await client.request("GET", "/rid")
    assert (shed.status, shed.body["error"]) == (503, "overloaded")
    assert shed.headers["retry-after"] == "1"
    assert (await client.request("GET", "/items/1")).status == 200  # async route
    release.set()
    assert (await first).status == 204


async def test_not_ready_before_startup_and_after_shutdown(pool: Pool) -> None:
    before = Client(make_app().asgi())
    assert (await before.request("GET", "/ping")).status == 503
    client = await serve(pool)
    assert await client.lifespan_event("shutdown") == "lifespan.shutdown.complete"
    assert (await client.request("GET", "/ping")).status == 503


@pytest.mark.parametrize("handler", [wrong_name, bad_type])
def test_unsupported_signatures_fail_when_included(handler: Any) -> None:
    with pytest.raises(RegistrationError):
        HinokiAPI().include(handler)


async def test_204_has_no_body_and_no_content_length(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request("GET", "/gone")
    assert (reply.status, reply.body) == (204, None)
    assert "content-length" not in reply.headers


async def test_routing_uses_the_raw_path_and_decodes_parameters(pool: Pool) -> None:
    client = await serve(pool)
    slash = await client.request("GET", "/echo/a/b", raw_path=b"/echo/a%2Fb")
    assert (slash.status, slash.body) == (200, {"name": "a/b"})
    unicode = await client.request("GET", "/echo/x", raw_path=b"/echo/%E4%BD%A0")
    assert unicode.body == {"name": "\u4f60"}
    bad = await client.request("GET", "/echo/x", raw_path=b"/echo/%FF")
    assert bad.status == 400
    assert (await client.request("GET", "/echo/a%2Fb")).status == 200  # no raw_path


@pytest.mark.parametrize("value", [" 42", "nan", "inf", "1_0", "\u0664\u0662"])
async def test_numeric_parameters_are_strict(pool: Pool, value: str) -> None:
    client = await serve(pool)
    assert (await client.request("GET", f"/ratio/{value}")).status == 422
    assert (await client.request("GET", "/ratio/2.5")).body == {"value": 2.5}
