"""AuthRejected and RateLimited: events about refused requests."""

import asyncio
import dataclasses
import time

import pytest

from hinoki import Access, BearerAuth, Caller, Hinoki, RateLimitContext, errors, http
from hinoki.errors import EventRegistrationError
from hinoki.events import AuthRejected, Handlers, RateLimited
from hinoki.runtime.asgi import ASGIAdapter
from hinoki.settings import AppSettings, EventsSettings
from hinoki.testing import ASGIClient

SECRET = "s3cret-token-value"  # noqa: S105 - a fixture, not a secret


class Tokens(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        if token == "reader":  # noqa: S105
            return Caller(id="u1", user="u1", permissions=frozenset({"read"}))
        return None


writer = Access(auth=Tokens(), permissions=["write"])


class Refuse:
    async def check(self, context: RateLimitContext) -> None:
        raise errors.TooManyRequests(9)


@http.get("/notes/{note_id}", access=writer)
async def read_note(note_id: str) -> dict[str, str]:
    return {"id": note_id}


@http.post("/login", rate_limit=Refuse())
async def login() -> dict[str, str]:
    return {}


def build(*collections: Handlers, timeout: float = 5.0) -> Hinoki:
    return Hinoki(
        include=[read_note, login],
        events=list(collections),
        settings=AppSettings(events=EventsSettings(handler_timeout=timeout)),
    )


async def refusals(client: ASGIClient) -> list[tuple[int, object]]:
    answers: list[tuple[int, object]] = []
    for headers in (
        {},
        {"authorization": f"Bearer {SECRET}"},
        {"authorization": "Bearer reader"},
    ):
        reply = await client.get("/notes/42", query="token=" + SECRET, headers=headers)
        body = {k: v for k, v in reply.body.items() if k != "request_id"}
        answers.append((reply.status, body))
    reply = await client.post("/login")
    answers.append((reply.status, reply.headers["retry-after"]))
    return answers


async def test_refusals_emit_events_with_templates_and_no_secrets() -> None:
    seen: list[object] = []
    handlers = Handlers("audit")

    @handlers.after_commit(AuthRejected)
    async def rejected(event: AuthRejected) -> None:
        seen.append(event)

    @handlers.after_commit(RateLimited)
    def limited(event: RateLimited) -> None:
        seen.append(event)

    async with ASGIClient(build(handlers)) as client:
        await refusals(client)

    assert seen == [
        AuthRejected("missing", "/notes/{note_id}", "GET"),
        AuthRejected("invalid", "/notes/{note_id}", "GET"),
        AuthRejected("forbidden", "/notes/{note_id}", "GET"),
        RateLimited("Refuse", "/login", "POST", 9),
    ]
    text = repr(seen)
    assert SECRET not in text and "42" not in text and "reader" not in text
    for event in seen:
        names = {f.name for f in dataclasses.fields(event)}  # type: ignore[arg-type]
        assert not {"token", "path", "ip", "client", "user", "query"} & names


async def test_listening_changes_no_answer() -> None:
    handlers = Handlers()

    @handlers.after_commit(AuthRejected)
    async def noop(event: AuthRejected) -> None: ...

    async with ASGIClient(build()) as plain:
        expected = await refusals(plain)
    async with ASGIClient(build(handlers)) as listening:
        assert await refusals(listening) == expected
    assert [status for status, _ in expected] == [401, 401, 403, 429]


async def test_a_failing_handler_changes_nothing() -> None:
    handlers = Handlers()

    @handlers.after_commit(AuthRejected)
    async def broken(event: AuthRejected) -> None:
        raise RuntimeError("boom")

    @handlers.after_commit(RateLimited)
    def broken_sync(event: RateLimited) -> None:
        raise RuntimeError("boom")

    async with ASGIClient(build()) as plain:
        expected = await refusals(plain)
    async with ASGIClient(build(handlers)) as client:
        assert await refusals(client) == expected


async def test_slow_handlers_delay_the_answer_by_at_most_the_timeout() -> None:
    handlers = Handlers()

    @handlers.after_commit(AuthRejected)
    async def slow(event: AuthRejected) -> None:
        await asyncio.sleep(30)

    @handlers.after_commit(AuthRejected)
    async def also_slow(event: AuthRejected) -> None:
        await asyncio.sleep(30)

    async with ASGIClient(build(handlers, timeout=0.2)) as client:
        started = time.monotonic()
        reply = await client.get("/notes/1")
        elapsed = time.monotonic() - started

    assert reply.status == 401
    assert elapsed < 1.5


@pytest.mark.parametrize("event", [AuthRejected, RateLimited])
def test_in_transaction_handlers_are_refused_at_registration(event: type) -> None:
    handlers = Handlers()
    with pytest.raises(EventRegistrationError, match="outside any transaction"):
        handlers.in_transaction(event)


@http.websocket("/live", access=writer)
async def live(socket: http.WebSocket) -> None: ...


async def test_a_websocket_handshake_refusal_emits_with_its_own_method() -> None:
    seen: list[AuthRejected] = []
    handlers = Handlers()

    @handlers.after_commit(AuthRejected)
    async def rejected(event: AuthRejected) -> None:
        seen.append(event)

    app = Hinoki(include=[live], events=[handlers])
    scope = {
        "type": "websocket",
        "path": "/live",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "subprotocols": [],
    }
    inbox: list[dict[str, str]] = [{"type": "websocket.connect"}]
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, str]:
        return inbox.pop(0) if inbox else {"type": "websocket.disconnect"}

    async def send(event: dict[str, object]) -> None:
        sent.append(event)

    await ASGIAdapter(app)(scope, receive, send)

    assert [e["type"] for e in sent] == ["websocket.close"]
    assert seen == [AuthRejected("missing", "/live", "WEBSOCKET")]
