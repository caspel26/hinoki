"""The public in-process test client."""

import pytest

from hinoki import Conflict, HinokiAPI, Request, Schema, get, post
from hinoki.modules import Module
from hinoki.testing import ASGIClient


class Item(Schema):
    name: str


@get("/ping")
async def ping(request: Request) -> dict[str, str]:
    return {"pong": "yes"}


@post("/items", status=201)
async def create(request: Request, data: Item) -> Item:
    return data


async def test_the_client_runs_lifespan_and_requests() -> None:
    app = HinokiAPI()
    app.include(ping, create)
    async with ASGIClient(app) as client:
        assert (await client.get("/ping")).body == {"pong": "yes"}
        made = await client.post("/items", json={"name": "a"})
        assert (made.status, made.body) == (201, {"name": "a"})
        assert (await client.post("/items", json={})).status == 422
        assert (await client.get("/missing")).status == 404


async def test_a_failing_startup_is_an_error_not_a_hang() -> None:
    class Broken(Module):
        name = "broken"

        def lifespan(self, ctx):  # type: ignore[no-untyped-def]
            raise RuntimeError("nope")

    app = HinokiAPI()
    app.install(Broken())
    with pytest.raises(RuntimeError, match="startup failed"):
        async with ASGIClient(app):
            pass


async def test_strict_errors_demand_declared_business_errors() -> None:
    class Taken(Conflict):
        code = "taken"

    @get("/declared", errors=(Taken,))
    async def declared() -> None:
        raise Taken()

    @get("/undeclared")
    async def undeclared() -> None:
        raise Taken()

    app = HinokiAPI()
    app.include(declared, undeclared)
    async with ASGIClient(app, strict_errors=True) as client:
        assert (await client.get("/declared")).status == 409
        with pytest.raises(AssertionError, match="does not declare"):
            await client.get("/undeclared")
        assert (await client.get("/nowhere")).status == 404  # unrouted: not checked
