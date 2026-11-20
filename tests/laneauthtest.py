"""Claims-token refusals of guarded routes are answered by Rust, identically to
the Python pipeline."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import pytest

from hinoki import Access, Caller, Hinoki, Schema, core, http
from hinoki.crypto import ClaimsJWTAuth, JWTKey
from hinoki.errors import APIError, Forbidden, TooManyRequests, Unauthorized
from hinoki.ratelimit import RateLimitContext
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.servercore import route_auth_of
from tests.kisoservertest import Received, fetch, record_lanes

SECRET = b"k" * 32


class Tokens(ClaimsJWTAuth):
    keys = (JWTKey(None, "HS256", SECRET),)
    issuer = "https://issuer.example"
    audience = "api"
    leeway = 5


class Refuse:
    async def check(self, context: RateLimitContext) -> None:
        raise TooManyRequests(9)


@http.get("/guarded", access=Access(Tokens(), permissions=["items:read"]))
async def guarded(who: Caller[None]) -> dict[str, Any]:
    return {"id": who.id, "permissions": sorted(who.permissions)}


class Payload(Schema):
    title: str


@http.post("/guarded-post", access=Access(Tokens(), permissions=["items:write"]))
async def guarded_post(data: Payload, who: Caller[None]) -> dict[str, Any]:
    return {"id": who.id, "title": data.title}


@http.get(
    "/guarded-limited",
    access=Access(Tokens(), permissions=["items:read"]),
    rate_limit=Refuse(),
)
async def guarded_limited() -> dict[str, str]:
    return {"ok": "yes"}


def token(claims: dict[str, Any], key: bytes = SECRET, kid: str | None = None) -> str:
    return core.jwt_encode(core.encode_json(claims), (kid, "HS256", key, False))


def good(**changes: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "u1",
        "scopes": ["items:read", "items:write"],
        "iss": Tokens.issuer,
        "aud": Tokens.audience,
        "iat": now,
        "exp": now + 600,
    }
    claims.update(changes)
    return {k: v for k, v in claims.items() if v is not None}


def bearer(value: str) -> str:
    return f"authorization: Bearer {value}\r\n"


def cases() -> dict[str, str]:
    now = int(time.time())
    return {
        "missing": "",
        "valid": bearer(token(good())),
        "lowercase-scheme": f"authorization: bearer {token(good())}\r\n",
        "basic": "authorization: Basic dTE6cA==\r\n",
        "empty-bearer": "authorization: Bearer \r\n",
        "garbage": bearer("not-a-token"),
        "two-parts": bearer("a.b"),
        "bad-signature": bearer(token(good(), key=b"z" * 32)),
        "expired": bearer(token(good(exp=now - 600))),
        "within-leeway": bearer(token(good(exp=now - 2))),
        "not-yet-valid": bearer(token(good(nbf=now + 600))),
        "wrong-issuer": bearer(token(good(iss="https://other.example"))),
        "wrong-audience": bearer(token(good(aud="elsewhere"))),
        "no-subject": bearer(token(good(sub=None))),
        "empty-subject": bearer(token(good(sub=""))),
        "numeric-subject": bearer(token(good(sub=7))),
        "no-scope": bearer(token(good(scopes=None))),
        "string-scope": bearer(token(good(scopes="items:read items:write"))),
        "mixed-scope": bearer(token(good(scopes=["items:read", 3, "items:write"]))),
        "read-only": bearer(token(good(scopes=["items:read"]))),
        "write-only": bearer(token(good(scopes=["items:write"]))),
        "unknown-kid": bearer(token(good(), kid="nope")),
        "two-headers": bearer(token(good())) + bearer(token(good())),
        "padded": f"authorization:   Bearer {token(good())}  \r\n",
    }


def normalized(received: Received) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = [(k, v) for k, v in received.headers if k not in {"x-request-id", "date"}]
    body = re.sub(rb"[0-9a-f]{32}", b"<id>", received.body)
    return received.status, headers, body


def make_app() -> Hinoki:
    return Hinoki(include=[guarded, guarded_post, guarded_limited])


async def serve(
    *,
    native: bool,
    late: Callable[[Hinoki], None] | None = None,
    only: set[str] | None = None,
) -> tuple[dict[str, list[tuple[int, list[tuple[str, str]], bytes]]], int]:
    app = make_app()
    worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    if late is not None:
        late(app)
    lanes = record_lanes(worker)
    answers: dict[str, list[tuple[int, list[tuple[str, str]], bytes]]] = {}
    try:
        for name, extra in cases().items():
            if only is not None and name not in only:
                continue
            answers[name] = [
                normalized(await fetch(port, "GET", "/guarded", extra)),
                normalized(await fetch(port, "HEAD", "/guarded", extra)),
                normalized(await fetch(port, "GET", "/guarded?x=1", extra)),
                normalized(
                    await fetch(
                        port,
                        "POST",
                        "/guarded-post",
                        extra
                        + "content-length: 2\r\ncontent-type: application/json\r\n",
                        body=b"{}",
                    )
                ),
            ]
    finally:
        await worker.stop()
    return answers, len(lanes)


@pytest.mark.asyncio
async def test_token_refusals_match_the_python_path() -> None:
    native, reached_python = await serve(native=True)
    python, _ = await serve(native=False)
    assert native == python
    statuses = {name: [item[0] for item in found] for name, found in native.items()}
    assert statuses["missing"] == [401, 401, 401, 401]
    assert statuses["valid"][0] == 200
    assert statuses["read-only"][:1] == [200]
    assert statuses["read-only"][3] == 403
    assert statuses["garbage"][0] == 401
    assert statuses["expired"][0] == 401
    assert statuses["within-leeway"][0] == 200
    refused = sum(1 for found in statuses.values() for status in found if status >= 400)
    admitted = sum(1 for found in statuses.values() for status in found if status < 400)
    assert refused > 0
    assert reached_python >= admitted


REFUSED_BY_RUST = {
    "missing",
    "garbage",
    "two-parts",
    "bad-signature",
    "expired",
    "not-yet-valid",
    "wrong-issuer",
    "wrong-audience",
    "no-subject",
    "empty-subject",
    "numeric-subject",
    "no-scope",
    "string-scope",
    "unknown-kid",
}


@pytest.mark.asyncio
async def test_refused_requests_never_reach_python() -> None:
    answers, reached_python = await serve(native=True, only=REFUSED_BY_RUST)
    assert {item[0] for found in answers.values() for item in found} == {401, 403}
    assert reached_python == 0


@pytest.mark.asyncio
async def test_error_handlers_do_not_change_the_refusals() -> None:
    class Denied(APIError):
        status = 418
        code = "denied"
        message = "No."

    def register(app: Hinoki) -> None:
        app.errors.add(Unauthorized, lambda _error: Denied())
        app.errors.add(Forbidden, lambda _error: Denied())
        app.errors.add(Exception, lambda _error: Denied())

    native, _ = await serve(native=True, late=register)
    python, _ = await serve(native=False, late=register)
    assert native == python
    assert native["missing"][0][0] == 401
    assert native["read-only"][3][0] == 403


@pytest.mark.asyncio
async def test_a_rate_limited_guarded_route_answers_401_before_the_limiter() -> None:
    app = make_app()
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        refused = await fetch(port, "GET", "/guarded-limited")
        limited = await fetch(port, "GET", "/guarded-limited", bearer(token(good())))
        forbidden = await fetch(
            port,
            "GET",
            "/guarded-limited",
            bearer(token(good(scopes=["other"]))),
        )
    finally:
        await worker.stop()
    assert refused.status == 401
    assert limited.status == 429
    assert forbidden.status == 403


@pytest.mark.asyncio
async def test_the_admitted_caller_reaches_the_handler() -> None:
    app = make_app()
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        received = await fetch(
            port,
            "GET",
            "/guarded",
            bearer(token(good(sub="someone", scopes=["items:read", 4, "extra"]))),
        )
    finally:
        await worker.stop()
    assert received.status == 200
    assert received.json() == {
        "id": "someone",
        "permissions": ["extra", "items:read"],
    }


def test_only_the_plain_claims_provider_is_authenticated_by_rust() -> None:
    class Custom(Tokens):
        def resolve_claims(self, claims: dict[str, Any]) -> Caller[None] | None:
            return Caller("fixed", None, ["items:read"])

    @http.get("/custom", access=Access(Custom(), permissions=["items:read"]))
    async def custom() -> dict[str, str]:
        return {}

    plain = Pipeline(make_app())
    assert len(route_auth_of(plain)) == 3
    overridden = Pipeline(Hinoki(include=[custom]))
    assert route_auth_of(overridden) == []


@pytest.mark.parametrize("change", ["keys", "issuer", "audience", "leeway"])
async def test_a_changed_provider_sends_requests_back_to_python(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    worker = KisoWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    headers = bearer(token(good()))
    try:
        assert (await fetch(port, "GET", "/guarded", headers)).status == 200
        replacement: Any = {
            "keys": (JWTKey(None, "HS256", b"r" * 32),),
            "issuer": "https://rotated.example",
            "audience": "rotated",
            "leeway": 0,
        }[change]
        monkeypatch.setattr(Tokens, change, replacement)
        worker.housekeeping()
        lanes = record_lanes(worker)
        after = await fetch(port, "GET", "/guarded", headers)
        expected = 200 if change == "leeway" else 401
        assert after.status == expected
        assert lanes  # Python decided it, not Rust
    finally:
        await worker.stop()
