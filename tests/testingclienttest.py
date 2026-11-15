"""The public in-process test client."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from hinoki import Hinoki, Request, Schema, http
from hinoki.errors import Conflict
from hinoki.modules import Module
from hinoki.settings import AppSettings, CorsSettings
from hinoki.testing import ASGIClient, ManualClock, SyncClient


class Item(Schema):
    name: str


@http.get("/ping")
async def ping(request: Request) -> dict[str, str]:
    return {"pong": "yes"}


@http.post("/items", status=201)
async def create(request: Request, data: Item) -> Item:
    return data


async def test_the_client_runs_lifespan_and_requests() -> None:
    app = Hinoki()
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

    app = Hinoki()
    app.install(Broken())
    with pytest.raises(RuntimeError, match="startup failed"):
        async with ASGIClient(app):
            pass


async def test_strict_errors_demand_declared_business_errors() -> None:
    class Taken(Conflict):
        code = "taken"

    @http.get("/declared", errors=(Taken,))
    async def declared() -> None:
        raise Taken()

    @http.get("/undeclared")
    async def undeclared() -> None:
        raise Taken()

    app = Hinoki()
    app.include(declared, undeclared)
    async with ASGIClient(app, strict_errors=True) as client:
        assert (await client.get("/declared")).status == 409
        with pytest.raises(AssertionError, match="does not declare"):
            await client.get("/undeclared")
        assert (await client.get("/nowhere")).status == 404  # unrouted: not checked


@http.get("/headers")
async def headers(request: Request) -> dict[str, str]:
    return {"trace": request.headers["x-trace"], "host": request.host}


async def test_async_client_methods_repeated_headers_and_host_defaults() -> None:
    app = Hinoki(
        settings=AppSettings(
            allowed_hosts=("api.example.com", "other.example.com"),
            cors=CorsSettings(origins=("https://client.test",)),
        ),
        include=[ping, headers],
    )
    async with ASGIClient(app, host="api.example.com") as client:
        head = await client.head("/ping")
        assert head.status == 200
        assert head.raw == b""

        repeated = await client.get(
            "/headers", headers=[("X-Trace", "one"), ("x-trace", "two")]
        )
        assert repeated.body == {"trace": "one, two", "host": "api.example.com"}

        override = await client.get(
            "/headers", headers={"host": "other.example.com", "x-trace": "x"}
        )
        assert override.body["host"] == "other.example.com"

        options = await client.options(
            "/ping",
            headers={
                "origin": "https://client.test",
                "access-control-request-method": "GET",
            },
        )
        assert options.status == 204


def test_sync_client_has_head_options_and_default_host() -> None:
    app = Hinoki(
        settings=AppSettings(
            allowed_hosts=("api.example.com",),
            cors=CorsSettings(origins=("https://client.test",)),
        ),
        include=[ping],
    )
    with SyncClient(app, host="api.example.com") as client:
        assert client.head("/ping").status == 200
        assert (
            client.options(
                "/ping",
                headers={
                    "origin": "https://client.test",
                    "access-control-request-method": "GET",
                },
            ).status
            == 204
        )


def test_manual_clock_normalizes_and_controls_aware_instants() -> None:
    clock = ManualClock(datetime(2026, 1, 2, 3, 4, tzinfo=timezone(timedelta(hours=2))))
    assert clock() == datetime(2026, 1, 2, 1, 4, tzinfo=UTC)
    assert Hinoki().now(clock) == datetime(2026, 1, 2, 1, 4, tzinfo=UTC)
    assert clock.advance(seconds=90) == datetime(2026, 1, 2, 1, 5, 30, tzinfo=UTC)

    clock.set(datetime(2026, 1, 3, 12, tzinfo=UTC))
    assert clock() == datetime(2026, 1, 3, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2026, 1, 3))
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(datetime(2026, 1, 3))
