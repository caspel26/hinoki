"""Access policies: public by default, 401 vs 403, principal injection."""

import pytest

from hinoki import (
    Access,
    APIKeyAuth,
    BearerAuth,
    Caller,
    Hinoki,
    Permission,
    Schema,
    core,
    http,
)
from hinoki.auth import Credentials, _LazyHeaders
from hinoki.errors import RegistrationError
from hinoki.http.request import Request
from tests.harness import Client, Pool
from tests.providers import TokenAuth

auth = TokenAuth()
editor = Access(auth=auth, permissions=["posts:write"])
any_user = Access(auth=auth)


class PostPermission(Permission):
    WRITE = "posts:write"
    READ = "posts:read"


class Draft(Schema):
    title: str


@http.get("/public")
async def public(request: Request) -> dict[str, bool]:
    return {"ok": True}


@http.get("/secure", access=editor)
async def secure(request: Request) -> dict[str, bool]:
    return {"ok": True}


@http.get("/whoami", access=any_user)
async def whoami(request: Request, principal: Caller[str]) -> dict[str, object]:
    return {"subject": principal.id, "scopes": sorted(principal.permissions)}


@http.post("/drafts", access=editor)
async def create(request: Request, data: Draft) -> dict[str, str]:
    return {"title": data.title}


@http.get("/tagged", access=any_user)
async def tagged(request: Request, page: int = 1) -> dict[str, object]:
    return {"page": page, "seen": request.GET.getlist("tag")}


@http.get("/leak", access=any_user)
async def leak(request: Request) -> None:
    raise RuntimeError("secret")


@http.get("/anonymous-principal")
async def anonymous(request: Request, principal: Caller[str]) -> None: ...


def authorization(token: str) -> list[tuple[bytes, bytes]]:
    return [(b"Authorization", token.encode())]


async def start(pool: Pool) -> Client:
    app = Hinoki()
    app.include(public, secure, whoami, create, leak, tagged)
    return await pool.start(app)


async def test_actions_without_a_policy_are_public(pool: Pool) -> None:
    client = await start(pool)
    assert (await client.request("GET", "/public")).status == 200
    bearer = {"authorization": "Bearer nonsense"}
    assert (await client.request("GET", "/public", headers=bearer)).status == 200


async def test_a_protected_request_parses_its_query_string_once(
    pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bytes] = []

    def counting(raw: bytes, limit: int) -> dict[str, list[str]]:
        calls.append(raw)
        return original(raw, limit)

    original = core.parse_query_string
    monkeypatch.setattr(core, "parse_query_string", counting)
    client = await start(pool)
    bearer = {"authorization": "Bearer editor-token"}
    reply = await client.request(
        "GET", "/tagged", query="tag=a&tag=b&page=3", headers=bearer
    )
    assert reply.status == 200 and reply.body == {"page": 3, "seen": ["a", "b"]}
    assert calls == [b"tag=a&tag=b&page=3"]
    calls.clear()
    assert (await client.request("GET", "/public", query="x=1")).status == 200
    assert calls == [b"x=1"]
    # A malformed query is still refused before credentials are examined.
    broken = await client.request("GET", "/tagged", query="a=%ff")
    assert broken.status == 400 and broken.body["error"] == "invalid_query"


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
        Hinoki().include(anonymous)


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
        async def resolve(self, value: str) -> Caller[str] | None:
            return None

    assert Keys().challenge == "ApiKey"
    assert Keys().header == "x-api-key"


def test_caller_and_access_use_permissions() -> None:
    who = Caller(id="u1", user={"name": "Ada"}, permissions=frozenset({"w"}))
    assert (who.id, who.user, who.permissions) == (
        "u1",
        {"name": "Ada"},
        frozenset({"w"}),
    )
    assert Caller("u2", "me", ["a"]).permissions == frozenset({"a"})  # any iterable
    assert Caller("u3", "me").permissions == frozenset()
    with pytest.raises(TypeError):
        Caller()  # type: ignore[call-arg]
    assert not hasattr(Caller("u", "me"), "scopes")
    auth = TokenAuth()
    assert Access(auth, permissions=["w", "r"]).permissions == frozenset({"w", "r"})
    assert Access(auth, ["w"]).permissions == frozenset({"w"})
    with pytest.raises(TypeError):
        Access(auth, scopes=["w"])  # type: ignore[call-arg]


class SyncTokens(BearerAuth[str]):
    """A provider written with plain ``def``: it runs on the sync executor."""

    def resolve_token(self, token: str) -> Caller[str] | None:
        if token != "sync-token":  # noqa: S105 - a fixture, not a secret
            return None
        return Caller(id="u-sync", user="sync", permissions=frozenset({"w"}))


sync_access = Access(auth=SyncTokens(), permissions=["w"])


@http.get("/sync-secure", access=sync_access)
async def sync_secure(request: Request, caller: Caller[str]) -> dict[str, str]:
    return {"id": caller.id}


async def test_a_provider_written_with_plain_def_authenticates(pool: Pool) -> None:
    app = Hinoki()
    app.include(sync_secure)
    client = await pool.start(app)
    good = await client.request(
        "GET", "/sync-secure", headers={"authorization": "Bearer sync-token"}
    )
    assert (good.status, good.body) == (200, {"id": "u-sync"})
    bad = await client.request(
        "GET", "/sync-secure", headers={"authorization": "Bearer nope"}
    )
    assert bad.status == 401
    assert (await client.request("GET", "/sync-secure")).status == 401


def test_permissions_must_be_a_collection_not_one_string() -> None:
    with pytest.raises(TypeError, match="not one string"):
        Access(auth=TokenAuth(), permissions="posts:write")
    with pytest.raises(TypeError, match="not one string"):
        Caller("u", None, "admin")


def test_permission_is_a_typed_string_enum_base() -> None:
    assert PostPermission.WRITE.value == "posts:write"
    assert str(PostPermission.WRITE) == "posts:write"
    assert {PostPermission.WRITE} == {"posts:write"}


MIXED_TOKENS = {
    "typed-token": Caller(
        id="u-typed", user="typed", permissions=[PostPermission.WRITE]
    ),
    "both-token": Caller(
        id="u-both", user="both", permissions=[PostPermission.WRITE, "posts:read"]
    ),
    "plain-token": Caller(id="u-plain", user="plain", permissions=["posts:read"]),
}


class MixedTokens(BearerAuth[str]):
    """Callers whose permissions mix ``Permission`` members and strings."""

    async def resolve_token(self, token: str) -> Caller[str] | None:
        return MIXED_TOKENS.get(token)


mixed = MixedTokens()
typed_writer = Access(auth=mixed, permissions=[PostPermission.WRITE])
mixed_editor = Access(auth=mixed, permissions=["posts:write", PostPermission.READ])


@http.get("/typed-write", access=typed_writer)
async def typed_write(request: Request, caller: Caller[str]) -> dict[str, object]:
    return {"id": caller.id, "permissions": sorted(caller.permissions)}


@http.get("/mixed-edit", access=mixed_editor)
async def mixed_edit(request: Request, caller: Caller[str]) -> dict[str, str]:
    return {"id": caller.id}


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def test_caller_and_access_store_permission_members_as_their_strings() -> None:
    caller = MIXED_TOKENS["both-token"]
    assert caller.permissions == {"posts:write", "posts:read"}
    assert all(type(p) is str for p in caller.permissions)
    assert all(type(p) is str for p in mixed_editor.permissions)
    assert "PostPermission" not in repr(caller) + repr(mixed_editor)


async def test_permission_members_and_strings_share_the_401_and_403_paths(
    pool: Pool,
) -> None:
    app = Hinoki()
    app.include(typed_write, mixed_edit)
    client = await pool.start(app)
    assert (await client.request("GET", "/typed-write")).status == 401
    unknown = await client.request("GET", "/typed-write", headers=bearer("nope"))
    assert unknown.status == 401
    typed = await client.request("GET", "/typed-write", headers=bearer("typed-token"))
    assert (typed.status, typed.body) == (
        200,
        {"id": "u-typed", "permissions": ["posts:write"]},
    )
    plain = await client.request("GET", "/typed-write", headers=bearer("plain-token"))
    assert (plain.status, plain.body["error"]) == (403, "forbidden")
    both = await client.request("GET", "/mixed-edit", headers=bearer("both-token"))
    assert (both.status, both.body) == (200, {"id": "u-both"})
    partial = await client.request("GET", "/mixed-edit", headers=bearer("typed-token"))
    assert (partial.status, partial.body["error"]) == (403, "forbidden")


def test_caller_empty_permissions_reuses_singleton_and_rejects_empty_string() -> None:
    a = Caller("u1", None)
    b = Caller("u2", None, [])
    c = Caller("u3", None, frozenset())
    assert a.permissions is b.permissions is c.permissions
    empty_iter: tuple[str, ...] = ()
    assert Caller("u4", None, (p for p in empty_iter)).permissions == frozenset()
    assert Caller("u5", None, [PostPermission.WRITE]).permissions == frozenset(
        {"posts:write"}
    )
    for bad in ("", b""):
        with pytest.raises(TypeError, match="not one string"):
            Caller("u", None, bad)  # type: ignore[arg-type]


def test_lazy_credentials_headers_avoids_full_dict_until_iterated() -> None:
    dict_calls = 0
    lookup_calls: list[str] = []

    class FakePrepared:
        def header(self, name: str) -> str | None:
            lookup_calls.append(name)
            return {"cookie": "sid=1", "authorization": "Bearer tok"}.get(name)

        @property
        def header_dict(self) -> dict[str, str]:
            nonlocal dict_calls
            dict_calls += 1
            return {"cookie": "sid=1", "authorization": "Bearer tok", "host": "a"}

    creds = Credentials(_LazyHeaders(FakePrepared()), {})  # type: ignore[arg-type]
    assert creds.headers.get("cookie") == "sid=1"
    assert creds.headers.get("missing", "fallback") == "fallback"
    assert creds.headers["authorization"] == "Bearer tok"
    assert "cookie" in creds.headers
    assert "missing" not in creds.headers
    non_str: object = 123
    assert non_str not in creds.headers
    with pytest.raises(KeyError):
        _ = creds.headers["missing"]
    assert dict_calls == 0
    assert lookup_calls == [
        "cookie",
        "missing",
        "authorization",
        "cookie",
        "missing",
        "missing",
    ]

    # Full mapping operations lazily materialize header_dict once and cache it.
    assert len(creds.headers) == 3
    assert dict(creds.headers) == {
        "cookie": "sid=1",
        "authorization": "Bearer tok",
        "host": "a",
    }
    assert creds.headers == {
        "cookie": "sid=1",
        "authorization": "Bearer tok",
        "host": "a",
    }
    assert "cookie" in repr(creds.headers)
    assert creds.headers.get("host") == "a"
    assert creds.headers["host"] == "a"
    assert "host" in creds.headers
    assert dict_calls == 1
