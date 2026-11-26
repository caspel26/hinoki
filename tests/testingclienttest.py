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
        assert client.put("/ping").status == 405
        assert client.patch("/ping").status == 405
        assert client.delete("/ping").status == 405


async def test_client_rejects_conflicting_body_encodings_and_multipart_headers() -> (
    None
):
    async with ASGIClient(Hinoki(include=[ping])) as client:
        assert (await client.put("/ping")).status == 405
        assert (await client.patch("/ping")).status == 405
        assert (await client.delete("/ping")).status == 405
        with pytest.raises(ValueError, match=r"json=.*body"):
            await client.post("/ping", json={"x": 1}, body=b"raw")
        with pytest.raises(ValueError, match="multipart data"):
            await client.post("/ping", json={"x": 1}, data={"name": "x"})
        with pytest.raises(ValueError, match="multipart data"):
            await client.post("/ping", body=b"raw", files=[])
        with pytest.raises(ValueError, match="content-type is set"):
            await client.post(
                "/ping",
                data={"name": "x"},
                headers={"content-type": "text/plain"},
            )
        with pytest.raises(ValueError, match="line breaks"):
            await client.post(
                "/ping",
                files=[("upload", "x.txt", b"x", "text/plain\r\ninjected: yes")],
            )
        with pytest.raises(ValueError, match="cannot contain controls"):
            await client.post("/ping", data={"bad\nname": "x"})
        response = await client.post(
            "/ping",
            data={'quoted"field': "x"},
            files=[("upload", 'quoted".txt', b"x", "text/plain")],
        )
        assert response.status == 405


async def test_async_client_token_query_and_strict_success_paths() -> None:
    @http.query("/lookup")
    async def lookup() -> dict[str, str]:
        return {"result": "ok"}

    app = Hinoki(include=[ping, lookup])
    async with ASGIClient(app, strict_errors=True) as client:
        assert (await client.get("/ping", token="test" + "-token")).status == 200
        assert (await client.query("/lookup")).body == {"result": "ok"}


def test_sync_client_cleans_up_after_startup_failure() -> None:
    class Broken(Module):
        name = "broken_sync"

        def lifespan(self, ctx):  # type: ignore[no-untyped-def]
            raise RuntimeError("startup refused")

    app = Hinoki()
    app.install(Broken())
    with pytest.raises(RuntimeError, match="startup failed"), SyncClient(app):
        pytest.fail("failed startup must not enter the client")


def test_sync_client_state_and_all_method_helpers() -> None:
    client = SyncClient(Hinoki(include=[ping]))
    with client:
        assert client.get("/ping").status == 200
        assert client.post("/ping").status == 405
        assert client.query("/ping").status == 405
        with pytest.raises(RuntimeError, match="entered once"):
            client.__enter__()
    client.__exit__(None, None, None)
    with pytest.raises(RuntimeError, match="inside its `with` block"):
        client.request("GET", "/ping")


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


@http.get("/b")
async def bounded(request: Request) -> dict[str, str]:
    return {"found": "yes"}


@pytest.mark.parametrize("path", ["/b?n=999", "/b#top", "/b?n=1#top"])
async def test_a_query_or_fragment_in_the_path_is_refused(path: str) -> None:
    app = Hinoki()
    app.include(bounded)
    async with ASGIClient(app) as client:
        for call in (client.get, client.post, client.query):
            with pytest.raises(ValueError, match=r"query="):
                await call(path)
        with pytest.raises(ValueError, match=r"query="):
            async with client.stream("GET", path):
                pass
        assert (await client.get("/b", query="n=999")).status == 200


@pytest.mark.parametrize("path", ["/b?n=999", "/b#top"])
def test_the_sync_client_refuses_a_query_or_fragment_in_the_path(path: str) -> None:
    app = Hinoki()
    app.include(bounded)
    with SyncClient(app) as client:
        with pytest.raises(ValueError, match=r"query="):
            client.get(path)
        with (
            pytest.raises(ValueError, match=r"query="),
            client.stream("GET", path),
        ):
            pass
        assert client.get("/b", query="n=999").status == 200
