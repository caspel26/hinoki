"""Access policies: public by default, 401 vs 403, principal injection."""

import pytest

from hinoki import (
    Access,
    APIKeyAuth,
    HinokiAPI,
    Principal,
    RegistrationError,
    Schema,
    get,
    post,
)
from tests.harness import Client, Pool
from tests.providers import TokenAuth

auth = TokenAuth()
editor = Access(auth=auth, scopes=["posts:write"])
any_user = Access(auth=auth)


class Draft(Schema):
    title: str


@get("/public")
async def public() -> dict[str, bool]:
    return {"ok": True}


@get("/secure", access=editor)
async def secure() -> dict[str, bool]:
    return {"ok": True}


@get("/whoami", access=any_user)
async def whoami(principal: Principal[str]) -> dict[str, object]:
    return {"subject": principal.subject, "scopes": sorted(principal.scopes)}


@post("/drafts", access=editor)
async def create(data: Draft) -> dict[str, str]:
    return {"title": data.title}


@get("/leak", access=any_user)
async def leak() -> None:
    raise RuntimeError("secret")


@get("/anonymous-principal")
async def anonymous(principal: Principal[str]) -> None: ...


def authorization(token: str) -> list[tuple[bytes, bytes]]:
    return [(b"Authorization", token.encode())]


async def start(pool: Pool) -> Client:
    app = HinokiAPI()
    app.include(public, secure, whoami, create, leak)
    return await pool.start(app)


async def test_actions_without_a_policy_are_public(pool: Pool) -> None:
    client = await start(pool)
    assert (await client.request("GET", "/public")).status == 200
    bearer = {"authorization": "Bearer nonsense"}
    assert (await client.request("GET", "/public", headers=bearer)).status == 200


async def test_missing_credentials_give_401_with_a_challenge(pool: Pool) -> None:
    client = await start(pool)
    reply = await client.request("GET", "/secure")
    assert reply.status == 401
    assert reply.headers["www-authenticate"] == "Bearer"
    assert reply.body["error"] == "unauthorized"


@pytest.mark.parametrize(
    "header",
    ["Bearer nope", "Basic editor-token", "Bearer", "Bearer   ", "editor-token"],
)
async def test_invalid_credentials_give_401(pool: Pool, header: str) -> None:
    client = await start(pool)
    reply = await client.request("GET", "/secure", headers={"authorization": header})
    assert reply.status == 401
    assert reply.headers["www-authenticate"] == "Bearer"


async def test_valid_identity_without_the_scope_gives_403(pool: Pool) -> None:
    client = await start(pool)
    headers = {"authorization": "Bearer reader-token"}
    reply = await client.request("GET", "/secure", headers=headers)
    assert (reply.status, reply.body["error"]) == (403, "forbidden")


async def test_sufficient_scope_is_allowed_and_header_name_is_case_insensitive(
    pool: Pool,
) -> None:
    client = await start(pool)
    reply = await client.request(
        "GET", "/secure", headers={"AUTHORIZATION": "bearer editor-token"}
    )
    assert reply.status == 200


async def test_the_principal_is_injected_into_the_handler(pool: Pool) -> None:
    client = await start(pool)
    headers = {"authorization": "Bearer editor-token"}
    reply = await client.request("GET", "/whoami", headers=headers)
    assert reply.body == {"subject": "u-editor", "scopes": ["posts:write"]}


async def test_authentication_precedes_body_validation(pool: Pool) -> None:
    client = await start(pool)
    anonymous_reply = await client.request(
        "POST", "/drafts", b"{}", content_type="application/json"
    )
    assert anonymous_reply.status == 401
    signed = {"authorization": "Bearer editor-token"}
    invalid = await client.request(
        "POST", "/drafts", b"{}", content_type="application/json", headers=signed
    )
    assert invalid.status == 422


async def test_unexpected_errors_in_protected_actions_do_not_leak(pool: Pool) -> None:
    client = await start(pool)
    headers = {"authorization": "Bearer reader-token"}
    reply = await client.request("GET", "/leak", headers=headers)
    assert reply.status == 500
    assert "secret" not in str(reply.body)


def test_principal_parameter_needs_an_access_policy() -> None:
    with pytest.raises(RegistrationError, match="access policy"):
        HinokiAPI().include(anonymous)


def test_access_needs_a_provider() -> None:
    with pytest.raises(TypeError):
        Access(auth=object())  # type: ignore[arg-type]


async def test_repeated_credential_headers_never_resolve_to_one_of_them(
    pool: Pool,
) -> None:
    client = await start(pool)
    sent = [
        (b"authorization", b"Bearer reader-token"),
        (b"authorization", b"Bearer editor-token"),
    ]
    reply = await client.request("GET", "/secure", raw_headers=sent)
    assert reply.status == 401


async def test_unauthenticated_requests_are_refused_before_the_body_is_read(
    pool: Pool,
) -> None:
    client = await start(pool)
    huge = b"{" * 2_000_000
    reply = await client.request(
        "POST", "/drafts", huge, content_type="application/json"
    )
    assert reply.status == 401


def test_api_key_provider_has_a_challenge() -> None:
    class Keys(APIKeyAuth[str]):
        async def resolve(self, value: str) -> Principal[str] | None:
            return None

    assert Keys().challenge == "ApiKey"
    assert Keys().header == "x-api-key"
