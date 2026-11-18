"""Route-level rate limiting: the application's policy decides, Hinoki enforces
the boundary (429 with ``Retry-After``, fail-closed 503, before the body)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from hinoki import (
    Access,
    BearerAuth,
    Caller,
    Hinoki,
    Module,
    ModuleContext,
    RateLimitContext,
    RateLimitPolicy,
    Schema,
    errors,
    http,
)
from hinoki.runtime.executor import ExecutorSaturated
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.servercore import native_handler_eligible
from hinoki.settings import AppSettings, ServerSettings
from hinoki.testing import ASGIClient

LEAK = "store-password-hunter2"


class Counter:
    """A worker-owned resource a module provides."""

    def __init__(self) -> None:
        self.seen = 0


class CounterModule(Module):
    name = "counter"

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        ctx.provide(Counter, Counter())
        yield


class Allow:
    def __init__(self) -> None:
        self.contexts: list[RateLimitContext] = []

    async def check(self, context: RateLimitContext) -> None:
        self.contexts.append(context)


class Refuse:
    def __init__(self, seconds: int = 7) -> None:
        self.seconds = seconds

    async def check(self, context: RateLimitContext) -> None:
        raise errors.TooManyRequests(self.seconds)


class SyncRefuse:
    def check(self, context: RateLimitContext) -> None:
        raise errors.TooManyRequests(3)


class Breaks:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def check(self, context: RateLimitContext) -> None:
        raise self.error


class Echo(Schema):
    value: str


class Tokens(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        return Caller(id=token, user=token)


signed_in = Access(auth=Tokens())
HITS: list[str] = []


def build(policy: RateLimitPolicy) -> Hinoki:
    @http.post("/items/{item_id}", rate_limit=policy)
    async def action(item_id: str, data: Echo) -> Echo:
        HITS.append(item_id)
        return data

    return Hinoki(include=[action])


@pytest.fixture(autouse=True)
def reset() -> None:
    HITS.clear()


async def test_an_allowing_policy_lets_the_handler_run_and_sees_a_safe_context() -> (
    None
):
    policy = Allow()
    async with ASGIClient(build(policy)) as client:
        reply = await client.post("/items/5", json={"value": "x"})

    assert reply.status == 200 and HITS == ["5"]
    [context] = policy.contexts
    assert (context.method, context.route, context.client, context.caller) == (
        "POST",
        "/items/{item_id}",
        None,
        None,
    )
    assert LEAK not in repr(context)


async def test_a_refusal_is_a_429_with_retry_after_before_the_body_is_read() -> None:
    async with ASGIClient(build(Refuse(7))) as client:
        reply = await client.post(
            "/items/5", body=b"{not json", headers={"content-type": "application/json"}
        )

    assert reply.status == 429
    assert reply.headers["retry-after"] == "7"
    assert reply.body["error"] == "rate_limited"
    assert HITS == []  # neither parsed (it would be 400) nor handled


async def test_a_sync_policy_runs_on_the_executor() -> None:
    async with ASGIClient(build(SyncRefuse())) as client:
        reply = await client.post("/items/5", json={"value": "x"})

    assert (reply.status, reply.headers["retry-after"]) == (429, "3")


@pytest.mark.parametrize("seconds", [0, -1, 86_401, True, "5", 1.5, None])
def test_retry_after_must_be_whole_seconds_within_a_day(seconds: Any) -> None:
    with pytest.raises(ValueError, match="whole seconds from 1 to 86400"):
        errors.TooManyRequests(seconds)


@pytest.mark.parametrize("seconds", [1, 86_400])
def test_the_bounds_of_retry_after_are_accepted(seconds: int) -> None:
    assert errors.TooManyRequests(seconds).headers == {"retry-after": str(seconds)}


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(LEAK),
        ValueError(LEAK),
        errors.APIError(LEAK, status=429),  # not a TooManyRequests: refused as invalid
        errors.TooManyRequests.__new__(errors.TooManyRequests),  # bypassed constructor
    ],
)
async def test_any_other_failure_fails_closed_without_leaking(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[Exception] = []
    app = build(Breaks(error))

    def offered(exc: RuntimeError) -> errors.APIError:
        seen.append(exc)
        return errors.APIError()

    app.error_handler(RuntimeError)(offered)

    with caplog.at_level(logging.DEBUG):
        async with ASGIClient(app) as client:
            reply = await client.post("/items/5", json={"value": "x"})

    assert reply.status == 503
    assert reply.body["error"] == "rate_limit_unavailable"
    assert reply.headers["retry-after"] == "1"
    assert LEAK not in str(reply.body) and LEAK not in caplog.text
    assert HITS == [] and seen == []  # not offered to the exception registry


@http.post("/x", rate_limit=Breaks(RuntimeError(LEAK)))
async def broken_post() -> dict[str, str]:
    return {}


async def test_debug_mode_shows_nothing_either() -> None:
    app = Hinoki(
        settings=AppSettings(debug=True),
        include=[broken_post],
    )
    async with ASGIClient(app) as client:
        reply = await client.post("/x")

    assert reply.status == 503 and LEAK not in str(reply.body)


async def test_the_policy_runs_after_authentication_and_sees_the_caller() -> None:
    policy = Allow()

    @http.get("/private", access=signed_in, rate_limit=policy)
    async def private() -> dict[str, str]:
        return {}

    async with ASGIClient(Hinoki(include=[private])) as client:
        anonymous = await client.get("/private")
        member = await client.get("/private", headers={"authorization": "Bearer ann"})

    assert anonymous.status == 401
    assert [c.caller.id if c.caller else None for c in policy.contexts] == ["ann"]
    assert member.status == 200


async def test_the_client_is_the_trusted_address_not_a_forwarded_header() -> None:
    policy = Allow()

    @http.get("/where", rate_limit=policy)
    async def where() -> dict[str, str]:
        return {}

    app = Hinoki(
        settings=AppSettings(server=ServerSettings(trusted_proxies=("127.0.0.1",))),
        include=[where],
    )

    async def call(client: ASGIClient, peer: str, forwarded: str) -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/where",
            "query_string": b"",
            "headers": [(b"x-forwarded-for", forwarded.encode())],
            "client": (peer, 5000),
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            return None

        await client.adapter(scope, receive, send)

    async with ASGIClient(app) as client:
        await call(client, "127.0.0.1", "203.0.113.9")
        await call(client, "198.51.100.4", "203.0.113.9")  # untrusted: header ignored

    assert [c.client for c in policy.contexts] == ["203.0.113.9", "198.51.100.4"]


async def test_a_policy_reaches_worker_owned_resources_through_the_context() -> None:
    class UsesCounter:
        async def check(self, context: RateLimitContext) -> None:
            context.resource(Counter).seen += 1

    @http.get("/counted", rate_limit=UsesCounter())
    async def counted() -> dict[str, str]:
        return {}

    app = Hinoki(include=[counted], modules=[CounterModule()])
    async with ASGIClient(app) as client:
        assert (await client.get("/counted")).status == 200
        assert (await client.get("/counted")).status == 200


async def test_a_resource_nobody_provided_fails_closed() -> None:
    class Wants:
        async def check(self, context: RateLimitContext) -> None:
            context.resource(Counter)

    @http.get("/wants", rate_limit=Wants())
    async def wants() -> dict[str, str]:
        return {}

    async with ASGIClient(Hinoki(include=[wants])) as client:
        assert (await client.get("/wants")).status == 503


async def test_a_saturated_executor_is_overload_not_a_policy_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build(SyncRefuse())

    async def saturated(*_args: object, **_kwargs: object) -> object:
        raise ExecutorSaturated

    async with ASGIClient(app) as client:
        monkeypatch.setattr(
            Pipeline, "_rate_limit_executor_saturated", True, raising=False
        )
        monkeypatch.setattr("hinoki.runtime.executor.SyncExecutor.run", saturated)
        reply = await client.post("/items/5", json={"value": "x"})

    assert (reply.status, reply.body["error"]) == (503, "overloaded")


async def test_cancellation_is_never_swallowed() -> None:
    app = build(Breaks(asyncio.CancelledError()))
    async with ASGIClient(app) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.post("/items/5", json={"value": "x"})


def test_openapi_documents_the_429_and_its_required_retry_after_header() -> None:
    document = build(Refuse()).openapi()

    response = document["paths"]["/items/{item_id}"]["post"]["responses"]["429"]
    assert response["headers"]["Retry-After"] == {
        "description": "Seconds to wait before trying again.",
        "required": True,
        "schema": {"type": "integer", "minimum": 1, "maximum": 86_400},
    }
    assert response["content"]["application/json"]["schema"]["allOf"][1][
        "properties"
    ] == {"error": {"enum": ["rate_limited"]}}


def test_a_rate_limited_get_stays_on_the_python_path() -> None:
    @http.get("/plain")
    async def plain() -> dict[str, str]:
        return {}

    @http.get("/limited", rate_limit=Allow())
    async def limited() -> dict[str, str]:
        return {}

    pipeline = Pipeline(Hinoki(include=[plain, limited]))
    by_path = {b.route.path: native_handler_eligible(b) for b in pipeline.bindings}

    assert by_path == {"/plain": True, "/limited": False}


async def test_a_route_group_passes_the_policy_through() -> None:
    from hinoki import RouteGroup  # noqa: PLC0415 - only this test needs it

    group = RouteGroup("/g")

    @group.post("/slow", rate_limit=Refuse())
    async def slow() -> dict[str, str]:
        return {}

    async with ASGIClient(Hinoki(include=[group])) as client:
        assert (await client.post("/g/slow")).status == 429


async def test_a_sync_check_that_returns_an_awaitable_is_awaited() -> None:
    class Hands:
        def check(self, context: RateLimitContext) -> Any:
            async def decide() -> None:
                raise errors.TooManyRequests(9)

            return decide()

    async with ASGIClient(build(Hands())) as client:
        reply = await client.post("/items/5", json={"value": "x"})

    assert (reply.status, reply.headers["retry-after"]) == (429, "9")
