"""Password hashing and JWT authentication, checked against independent oracles.

Oracles: the standard library's hmac/hashlib for HMAC tokens; a hash made by
argon2-cffi (reference C implementation) and an Ed25519 token signed by the
`cryptography` library, both generated once and pinned here.
"""

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Iterator, Sequence
from typing import Any, ClassVar, cast, overload

import pytest

from hinoki import Access, Caller, Hinoki, Request, core, http
from hinoki.crypto import (
    HashingBusy,
    JWTAuth,
    JWTKey,
    PasswordHasher,
    _exact_rules,
    random_token,
)
from tests.harness import Pool

SEED = "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A"  # RFC 8037 A.4 private key
PUBLIC = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
ED_TOKEN = (
    "eyJhbGciOiJFZERTQSIsImtpZCI6ImsxIiwidHlwIjoiSldUIn0."
    "eyJzdWIiOiJhbm4iLCJleHAiOjQxMDI0NDQ4MDB9."
    "46VHE-Hnx3V23jI7wHDJ7ECiRFWGG2lTMyGd6PxnTN4nzsrqKzEx6QU76KeXzXVashUZNYcIZvEPHgUPKc26AA"
)
REFERENCE_HASH = (
    "$argon2id$v=19$m=19456,t=2,p=1$RUsa/wKHFbeIgOtihzpADg$"
    "OnIL2myF0FPARWAsayXYKHT/iFLonYc+7S9kAEMUsx4"
)
SECRET = b"k" * 40
WEAK: dict[str, Any] = {"memory_kib": 64, "time_cost": 1, "allow_weak": True}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def oracle_hs256(claims: dict[str, Any], key: bytes = SECRET, **header: Any) -> str:
    """A token built only with the standard library."""
    head = b64(
        json.dumps(
            {"alg": "HS256", "typ": "JWT", **header}, separators=(",", ":")
        ).encode()
    )
    body = b64(json.dumps(claims, separators=(",", ":")).encode())
    mac = hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64(mac)}"


class Tokens(JWTAuth[str]):
    keys: ClassVar[Sequence[JWTKey]] = (JWTKey("k1", "HS256", SECRET),)
    issuer: ClassVar[str | None] = "https://api.test"
    audience: ClassVar[str | None] = "blog"
    clock: int = 1_000

    def now(self) -> int:
        return self.clock

    async def resolve_claims(self, claims: dict[str, Any]) -> Caller[str] | None:
        subject, raw = claims.get("sub"), claims.get("scopes", [])
        if not isinstance(subject, str) or not isinstance(raw, list):
            return None
        scopes = frozenset(s for s in raw if isinstance(s, str))
        return Caller(id=subject, user=subject, permissions=scopes)


@dataclasses.dataclass(frozen=True, slots=True)
class CountingJWTKey(JWTKey):
    spec_calls: list[None] = dataclasses.field(default_factory=list, repr=False)

    def spec(self) -> tuple[str | None, str, bytes, bool]:
        self.spec_calls.append(None)
        return super().spec()


@dataclasses.dataclass(frozen=True, slots=True)
class MutatingJWTKey(JWTKey):
    mutation: Callable[[], None] = dataclasses.field(
        default=lambda: None, repr=False, compare=False
    )

    def spec(self) -> tuple[str | None, str, bytes, bool]:
        self.mutation()
        return super().spec()


class _SlottedMetadata:
    __slots__ = ("label",)
    label: str


class _DescriptorCacheTokens(Tokens):
    @property
    def _hinoki_private_jwt_verifier(self) -> object:
        raise AssertionError("private cache descriptor must not run")

    @_hinoki_private_jwt_verifier.setter
    def _hinoki_private_jwt_verifier(self, value: object) -> None:
        del value
        raise AssertionError("private cache descriptor must not run")


class _DescriptorNamespaceTokens(Tokens):
    @property
    def __dict__(self) -> dict[str, object]:  # type: ignore[override]
        raise AssertionError("custom namespace descriptor must not run")


class _ForeignNamespaceTokens(Tokens):
    __dict__ = type.__dict__["__dict__"]


class _KeySequence(Sequence[JWTKey]):
    def __init__(self, keys: list[JWTKey]) -> None:
        self.keys = keys
        self.iterations = 0

    @overload
    def __getitem__(self, index: int) -> JWTKey: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[JWTKey]: ...

    def __getitem__(self, index: int | slice) -> JWTKey | Sequence[JWTKey]:
        return self.keys[index]

    def __len__(self) -> int:
        return len(self.keys)

    def __iter__(self) -> Iterator[JWTKey]:
        self.iterations += 1
        return iter(self.keys)


class _MixedTokens(_SlottedMetadata, Tokens):
    pass


class _HostileEquality:
    __hash__ = object.__hash__

    def __eq__(self, other: object) -> bool:
        del other
        raise AssertionError("untrusted cache equality must not run")


@dataclasses.dataclass
class _AppendKeyOnce:
    keys: list[JWTKey]
    key: JWTKey
    appended: bool = False

    def __call__(self) -> None:
        if not self.appended:
            self.keys.append(self.key)
            self.appended = True


class _LegacyDecoderSpy:
    def __init__(self) -> None:
        self.called = False

    def __call__(
        self,
        token: str,
        keys: list[tuple[str | None, str, bytes, bool]],
        rules: tuple[int, int, str | None, str | None, bool],
    ) -> bytes:
        self.called = True
        return _ORIGINAL_JWT_DECODER(token, keys, rules)


_ORIGINAL_JWT_DECODER = core.jwt_decode
_PATCHED_SPEC_CALLS: list[JWTKey] = []


def _record_jwt_key_spec(key: JWTKey) -> tuple[str | None, str, bytes, bool]:
    _PATCHED_SPEC_CALLS.append(key)
    return _ORIGINAL_JWT_KEY_SPEC(key)


_ORIGINAL_JWT_KEY_SPEC = JWTKey.spec


def _resolved_jwt_claims(claims: dict[str, Any]) -> Caller[str] | None:
    subject = claims.get("sub")
    if subject == "denied":
        return None
    permissions = {"read"} if subject == "sync" else set()
    return Caller(str(subject), str(subject), permissions)


async def _async_resolved_jwt_claims(
    claims: dict[str, Any],
) -> Caller[str] | None:
    return _resolved_jwt_claims(claims)


class SyncClaimTokens(JWTAuth[str]):
    keys: ClassVar[Sequence[JWTKey]] = (JWTKey("sync", "HS256", b"s" * 40),)
    issuer: ClassVar[str | None] = None
    audience: ClassVar[str | None] = None

    def now(self) -> int:
        return 1_000

    def resolve_claims(self, claims: dict[str, Any]) -> Caller[str] | None:
        return _resolved_jwt_claims(claims)


class AwaitableClaimTokens(JWTAuth[str]):
    keys: ClassVar[Sequence[JWTKey]] = (JWTKey("awaitable", "HS256", b"a" * 40),)
    issuer: ClassVar[str | None] = None
    audience: ClassVar[str | None] = None

    def now(self) -> int:
        return 1_000

    def resolve_claims(self, claims: dict[str, Any]) -> Awaitable[Caller[str] | None]:
        return _async_resolved_jwt_claims(claims)


sync_claim_access = Access(auth=SyncClaimTokens(), permissions={"read"})
awaitable_claim_access = Access(auth=AwaitableClaimTokens(), permissions={"read"})


@http.get("/jwt-sync-claims", access=sync_claim_access)
async def jwt_sync_claims(request: Request, caller: Caller[str]) -> dict[str, str]:
    return {"id": caller.id}


@http.get("/jwt-awaitable-claims", access=awaitable_claim_access)
async def jwt_awaitable_claims(request: Request, caller: Caller[str]) -> dict[str, str]:
    return {"id": caller.id}


GOOD = {
    "sub": "ann",
    "exp": 2000,
    "iss": "https://api.test",
    "aud": "blog",
    "scopes": ["w"],
}


async def verdict(token: str, auth: Tokens | None = None) -> bool:
    return await (auth or Tokens()).resolve_token(token) is not None


type VerifierCache = tuple[
    tuple[tuple[str | None, str, bytes, bool], ...], core._JWTVerifier
]


def verifier_cache(auth: JWTAuth[Any]) -> VerifierCache | None:
    return cast(VerifierCache | None, auth.__dict__.get("_hinoki_private_jwt_verifier"))


# --- passwords ------------------------------------------------------------


def test_argon2_matches_the_reference_implementation() -> None:
    hasher = PasswordHasher()
    assert hasher.verify_sync(
        "correct horse battery", REFERENCE_HASH
    )  # made by argon2-cffi
    assert not hasher.verify_sync("wrong", REFERENCE_HASH)
    mine = hasher.hash_sync("correct horse battery")
    assert mine.startswith(
        "$argon2id$v=19$m=19456,t=2,p=1$"
    ) and mine != hasher.hash_sync("correct horse battery")
    assert not hasher.needs_rehash(mine) and PasswordHasher(
        memory_kib=32768
    ).needs_rehash(mine)


def test_malformed_hashes_are_data_errors_and_parameters_have_a_floor() -> None:
    hasher = PasswordHasher(**WEAK)
    for bad in (
        "",
        "plain",
        "$2b$12$abc",
        "$argon2id$v=19$m=4194304,t=1,p=1$c2FsdHNhbHQ$aGFzaGhhc2g",
    ):
        with pytest.raises(ValueError):  # noqa: PT011
            hasher.verify_sync("x", bad)
    with pytest.raises(ValueError, match="refused"):
        PasswordHasher(memory_kib=64)
    with pytest.raises(ValueError, match="at least 1"):
        PasswordHasher(workers=0, **WEAK)
    assert (
        hasher.verify_sync("x" * 5000, hasher.hash_sync("x")) is False
    )  # over-long: no work
    with pytest.raises(ValueError, match="too long"):
        hasher.hash_sync("x" * 5000)


async def test_hashing_runs_off_the_loop_and_is_bounded() -> None:
    hasher = PasswordHasher(workers=1, max_pending=2, **WEAK)
    stored = await hasher.hash("pw")
    assert await hasher.verify("pw", stored) and not await hasher.verify("no", stored)
    first = asyncio.create_task(hasher.hash("a"))
    second = asyncio.create_task(hasher.hash("b"))
    await asyncio.sleep(0)
    with pytest.raises(HashingBusy):
        await hasher.hash("c")
    await asyncio.gather(first, second)
    hasher.close()


def test_random_tokens() -> None:
    token = random_token()
    assert len(token) == 43 and token != random_token()
    for bad in (0, 1025):
        with pytest.raises(ValueError, match="between"):
            random_token(bad)
    assert core.constant_time_equal(b"a", b"a") and not core.constant_time_equal(
        b"a", b"b"
    )


# --- JWT ------------------------------------------------------------------


async def test_a_standard_library_token_verifies_and_ours_verifies_with_hmac() -> None:
    assert await verdict(oracle_hs256(GOOD, kid="k1"))
    issued = Tokens().issue({"sub": "ann"}, ttl=500)
    head, body, sig = issued.split(".")
    assert hmac.compare_digest(
        unb64(sig), hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest()
    )
    claims = json.loads(unb64(body))
    assert (claims["iss"], claims["aud"], claims["exp"], claims["iat"]) == (
        "https://api.test",
        "blog",
        1500,
        1000,
    )
    assert json.loads(unb64(head)) == {"alg": "HS256", "typ": "JWT", "kid": "k1"}
    assert await verdict(issued)


async def test_a_ed25519_token_signed_by_another_library_verifies() -> None:
    class EdAuth(Tokens):
        keys = (JWTKey("k1", "EdDSA", unb64(PUBLIC), public=True),)
        issuer = None
        audience = None

    assert await verdict(ED_TOKEN, EdAuth())
    tampered = ED_TOKEN[:-3] + "AAA"
    assert not await verdict(tampered, EdAuth())

    class Signer(EdAuth):
        keys = (JWTKey("k1", "EdDSA", unb64(SEED)),)

    token = Signer().issue({"sub": "ann"})
    assert await verdict(token, Signer()) and await verdict(token, EdAuth())
    with pytest.raises(RuntimeError, match="signing key"):
        EdAuth().issue({"sub": "x"})


@pytest.mark.parametrize(
    "token",
    [
        "",
        "a.b",
        "a.b.c.d",
        oracle_hs256(GOOD, kid="k1") + "=",
        oracle_hs256(GOOD, key=b"x" * 40, kid="k1"),  # wrong key
        oracle_hs256({**GOOD, "exp": 999}, kid="k1"),  # expired
        oracle_hs256({**GOOD, "nbf": 5000}, kid="k1"),
        oracle_hs256({k: v for k, v in GOOD.items() if k != "exp"}, kid="k1"),
        oracle_hs256({**GOOD, "iss": "evil"}, kid="k1"),
        oracle_hs256({**GOOD, "aud": "other"}, kid="k1"),
        oracle_hs256({**GOOD, "aud": ["blog", 1]}, kid="k1"),
        oracle_hs256({**GOOD, "exp": "2000"}, kid="k1"),
        oracle_hs256(GOOD, kid="unknown"),
        oracle_hs256(GOOD, kid=5),
        oracle_hs256(GOOD, kid="k1", crit=["x"]),
        oracle_hs256(GOOD, kid="k1", zip="DEF"),
        "A" * 9000,
    ],
)
async def test_bad_tokens_are_refused(token: str) -> None:
    assert not await verdict(token)


async def test_the_algorithm_cannot_be_chosen_by_the_token() -> None:
    body = b64(json.dumps(GOOD).encode())
    none = b64(b'{"alg":"none","kid":"k1"}')
    assert not await verdict(f"{none}.{body}.")
    assert not await verdict(f"{none}.{body}.AAAA")
    other = oracle_hs256(GOOD, kid="k1")
    swapped = other.replace(other.split(".")[0], b64(b'{"alg":"HS512","kid":"k1"}'))
    assert not await verdict(swapped)


async def test_duplicate_header_and_claim_keys_are_refused() -> None:
    def raw(header: str, claims: str) -> str:
        head, body = b64(header.encode()), b64(claims.encode())
        mac = hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest()
        return f"{head}.{body}.{b64(mac)}"

    assert await verdict(raw('{"alg":"HS256","kid":"k1"}', json.dumps(GOOD)))
    assert not await verdict(
        raw('{"alg":"HS256","alg":"HS256","kid":"k1"}', json.dumps(GOOD))
    )
    assert not await verdict(
        raw(
            '{"alg":"HS256","kid":"k1"}',
            '{"sub":"a","exp":1,"exp":9999,"iss":"https://api.test","aud":"blog"}',
        )
    )


async def test_leeway_is_capped_and_audience_lists_work() -> None:
    class Slack(Tokens):
        leeway = 10_000  # capped at 120 seconds

    expired = oracle_hs256({**GOOD, "exp": 800, "aud": ["x", "blog"]}, kid="k1")
    assert not await verdict(expired, Slack())
    nearly = oracle_hs256({**GOOD, "exp": 900, "aud": ["x", "blog"]}, kid="k1")
    assert await verdict(nearly, Slack()) and not await verdict(nearly)


def test_keys_are_validated_when_configured() -> None:
    for kwargs in (
        {"algorithm": "none", "key": SECRET},
        {"algorithm": "HS256", "key": b"short"},
        {"algorithm": "HS512", "key": b"k" * 40},
        {"algorithm": "EdDSA", "key": b"k" * 31},
        {"algorithm": "HS256", "key": SECRET, "public": True},
        {"algorithm": "HS256", "key": SECRET, "kid": ""},
        {"algorithm": "HS256", "key": SECRET, "kid": "x" * 65},
    ):
        with pytest.raises(ValueError):  # noqa: PT011
            JWTKey(**{"kid": "k", **kwargs})  # type: ignore[arg-type]


async def test_without_keys_the_provider_refuses_to_run() -> None:
    class Empty(Tokens):
        keys = ()

    with pytest.raises(ValueError, match="at least one key"):
        Empty()


async def test_the_provider_guards_an_action_over_http(pool: Pool) -> None:
    editor = Access(auth=Tokens(), permissions=["w"])

    @http.get("/me", access=editor)
    def me(request: Request, principal: Caller[str]) -> dict[str, str]:
        return {"who": principal.id}

    app = Hinoki()
    app.include(me)
    client = await pool.start(app)
    valid = {
        "authorization": f"Bearer {Tokens().issue({'sub': 'ann', 'scopes': ['w']})}"
    }
    assert (await client.request("GET", "/me", headers=valid)).body == {"who": "ann"}
    no_scope = {"authorization": f"Bearer {Tokens().issue({'sub': 'bob'})}"}
    assert (await client.request("GET", "/me", headers=no_scope)).status == 403
    bad = await client.request(
        "GET", "/me", headers={"authorization": "Bearer nonsense"}
    )
    assert bad.status == 401 and bad.headers["www-authenticate"] == "Bearer"
    assert "expired" not in json.dumps(bad.body) and bad.body["error"] == "unauthorized"
    assert (await client.request("GET", "/me")).status == 401
    assert app.openapi() is not None
    assert Tokens().openapi_scheme() == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }


def test_key_sets_are_validated_when_the_provider_is_built() -> None:
    class Dup(Tokens):
        keys = (JWTKey("a", "HS256", SECRET), JWTKey("a", "HS256", b"z" * 40))

    class NoKid(Tokens):
        keys = (JWTKey(None, "HS256", SECRET), JWTKey("b", "HS256", b"z" * 40))

    for bad in (Dup, NoKid):
        with pytest.raises(ValueError, match="distinct kid"):
            bad()


def test_secrets_never_appear_in_a_repr() -> None:
    key = JWTKey("k", "HS256", b"super-secret-key-material-0123456789")
    assert "super-secret" not in repr(key) and "super-secret" not in repr([key])
    assert JWTKey.verify_only("p", unb64(PUBLIC)).public is True


def test_issue_cannot_be_talked_into_odd_lifetimes() -> None:
    tokens = Tokens()
    for bad in (0, -5, 10**12):
        with pytest.raises(ValueError, match="ttl"):
            tokens.issue({"sub": "a"}, ttl=bad)
    body = json.loads(
        unb64(
            tokens.issue({"sub": "a", "exp": 2**62, "iat": "x"}, ttl=60).split(".")[1]
        )
    )
    assert (body["iat"], body["exp"]) == (1000, 1060)  # the server's, not the caller's


async def test_a_token_for_another_audience_is_refused_without_one_configured() -> None:
    class Open(Tokens):
        audience = None
        issuer = None

    other = oracle_hs256({"sub": "a", "exp": 2000, "aud": "other-service"}, kid="k1")
    assert not await verdict(other, Open())
    assert await verdict(oracle_hs256({"sub": "a", "exp": 2000}, kid="k1"), Open())


async def test_claims_the_json_decoder_cannot_read_are_a_401_not_a_500() -> None:
    claims = b'{"sub":"a","exp":2000,"iss":"https://api.test","aud":"blog","x":1e400}'
    head = b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
    body = b64(claims)
    mac = hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest()
    assert not await verdict(f"{head}.{body}.{b64(mac)}")


async def test_nested_duplicates_and_forged_number_markers_are_refused() -> None:
    for payload in (
        b'{"sub":"a","exp":2000,"iss":"https://api.test","aud":"blog","r":{"a":1,"a":2}}',
        b'{"sub":"a","exp":{"$serde_json::private::Number":"5000"},"iss":"https://api.test","aud":"blog"}',
    ):
        head = b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
        body = b64(payload)
        mac = hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest()
        assert not await verdict(f"{head}.{body}.{b64(mac)}")


def test_argon2_only_verifies_argon2id_and_flags_old_versions() -> None:
    hasher = PasswordHasher(**WEAK)
    argon2i = "$argon2i$v=19$m=65536,t=2,p=1$c29tZXNhbHQ$" + "A" * 43
    with pytest.raises(ValueError, match="argon2id"):
        hasher.verify_sync("x", argon2i)
    old = "$argon2id$v=16$m=64,t=1,p=1$c29tZXNhbHQ$" + "A" * 43
    assert hasher.needs_rehash(old)
    with pytest.raises(ValueError):  # noqa: PT011
        hasher.verify_sync(
            "x", "$argon2id$v=19$m=1048576,t=1,p=1$c29tZXNhbHQ$" + "A" * 43
        )


async def test_a_token_is_refused_exactly_at_its_expiry_second() -> None:
    """RFC 7519: on or after ``exp`` the token must not be accepted."""
    token = oracle_hs256({**GOOD, "exp": 1_000}, kid="k1")
    auth = Tokens()
    auth.clock = 999
    assert await verdict(token, auth)
    auth.clock = 1_000
    assert not await verdict(token, auth)


async def test_jwt_verifier_handle_is_reused_and_replaced_on_key_rotation() -> None:
    auth = Tokens()
    old_key = JWTKey("old", "HS256", b"o" * 40)
    object.__setattr__(auth, "keys", [old_key])
    old_token = auth.issue({"sub": "ann", "scopes": ["w"]})
    assert await auth.resolve_token(old_token) is not None
    first = verifier_cache(auth)
    assert first is not None

    assert await auth.resolve_token(old_token) is not None
    assert verifier_cache(auth) is first

    new_key = JWTKey("new", "HS256", b"n" * 40)
    object.__setattr__(auth, "keys", [new_key])
    new_token = auth.issue({"sub": "ann", "scopes": ["w"]})
    assert await auth.resolve_token(new_token) is not None
    second = verifier_cache(auth)
    assert second is not None and second is not first
    assert first[1].verify(old_token, (1_000, 0, auth.issuer, auth.audience, True))
    with pytest.raises(ValueError, match="key"):
        second[1].verify(old_token, (1_000, 0, auth.issuer, auth.audience, True))


async def test_jwt_verifier_snapshot_tracks_in_place_key_material_changes() -> None:
    auth = Tokens()
    key = JWTKey("mutable", "HS256", b"a" * 40)
    object.__setattr__(auth, "keys", [key])
    first_token = auth.issue({"sub": "ann", "scopes": ["w"]})
    assert await auth.resolve_token(first_token) is not None
    first = verifier_cache(auth)

    object.__setattr__(key, "key", b"b" * 40)
    second_token = auth.issue({"sub": "ann", "scopes": ["w"]})
    assert await auth.resolve_token(second_token) is not None
    second = verifier_cache(auth)
    assert first is not None and second is not None and second is not first
    assert first[1].verify(first_token, (1_000, 0, auth.issuer, auth.audience, True))


async def test_jwt_invalid_rules_keep_legacy_conversion_precedence() -> None:
    auth = Tokens()
    key = JWTKey("precedence", "HS256", b"p" * 40)
    object.__setattr__(auth, "keys", [key])
    token = auth.issue({"sub": "ann", "scopes": ["w"]})
    assert await auth.resolve_token(token) is not None
    assert verifier_cache(auth) is not None

    object.__setattr__(key, "algorithm", "unsupported")
    object.__setattr__(auth, "leeway", 1 << 100)
    with pytest.raises(OverflowError):
        await auth.resolve_token(token)
    assert verifier_cache(auth) is None


async def test_custom_jwt_key_spec_uses_legacy_decoder_once() -> None:
    auth = Tokens()
    key = CountingJWTKey("custom", "HS256", b"c" * 40)
    object.__setattr__(auth, "keys", [key])
    token = auth.issue({"sub": "ann", "scopes": ["w"]})
    key.spec_calls.clear()

    assert await auth.resolve_token(token) is not None
    assert len(key.spec_calls) == 1
    assert verifier_cache(auth) is None


async def test_jwt_verifier_keeps_slotted_mixin_layout_compatible() -> None:
    auth = _MixedTokens()
    auth.label = "metadata"
    token = oracle_hs256(GOOD, kid="k1")
    assert await auth.resolve_token(token) is not None
    assert auth.label == "metadata"


async def test_jwt_verifier_skips_custom_cache_descriptors() -> None:
    auth = _DescriptorCacheTokens()
    token = oracle_hs256(GOOD, kid="k1")
    assert await auth.resolve_token(token) is not None
    assert verifier_cache(auth) is None


async def test_jwt_verifier_skips_custom_namespace_descriptors() -> None:
    auth = _DescriptorNamespaceTokens()
    token = oracle_hs256(GOOD, kid="k1")
    assert await auth.resolve_token(token) is not None


async def test_jwt_verifier_falls_back_for_foreign_namespace_descriptor() -> None:
    auth = _ForeignNamespaceTokens()
    token = oracle_hs256(GOOD, kid="k1")
    assert await auth.resolve_token(token) is not None


async def test_jwt_custom_key_sequence_uses_legacy_path_once() -> None:
    auth = Tokens()
    key = CountingJWTKey("sequence", "HS256", b"q" * 40)
    sequence = _KeySequence([key])
    object.__setattr__(auth, "keys", sequence)
    token = oracle_hs256(GOOD, b"q" * 40, kid="sequence")
    sequence.iterations = 0

    assert await auth.resolve_token(token) is not None
    assert sequence.iterations == 1
    assert key.spec_calls == [None]
    assert verifier_cache(auth) is None


def test_jwt_reuse_gate_refuses_malformed_rule_shapes() -> None:
    assert _exact_rules((1, 0, None, None)) is False


async def test_jwt_verifier_falls_back_to_preserve_unicode_conversion_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = Tokens()
    key = JWTKey("unicode", "HS256", b"u" * 40)
    object.__setattr__(auth, "keys", [key])
    token = auth.issue({"sub": "ann", "scopes": ["w"]})
    object.__setattr__(key, "algorithm", "unsupported")
    object.__setattr__(auth, "issuer", "\ud800")
    spy = _LegacyDecoderSpy()
    monkeypatch.setattr(core, "jwt_decode", spy)
    assert await auth.resolve_token(token) is None
    assert spy.called


def test_jwt_default_clock_returns_integer_seconds() -> None:
    assert type(JWTAuth.now(Tokens())) is int


async def test_jwt_verifier_rejects_malformed_cached_snapshot_without_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = Tokens()
    token = oracle_hs256(GOOD, kid="k1")
    assert await auth.resolve_token(token) is not None
    cache = verifier_cache(auth)
    assert cache is not None

    auth.__dict__["_hinoki_private_jwt_verifier"] = (
        (_HostileEquality(),),
        cache[1],
    )
    assert await auth.resolve_token(token) is not None
    assert verifier_cache(auth) is not None


async def test_jwtkey_spec_monkeypatch_after_provider_creation_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = Tokens()
    token = oracle_hs256(GOOD, kid="k1")
    _PATCHED_SPEC_CALLS.clear()
    monkeypatch.setattr(JWTKey, "spec", _record_jwt_key_spec)
    assert await auth.resolve_token(token) is not None
    assert len(_PATCHED_SPEC_CALLS) == 1
    assert verifier_cache(auth) is None


async def test_jwt_key_list_mutation_does_not_change_an_inflight_snapshot() -> None:
    auth = Tokens()
    keys: list[JWTKey] = []
    new_key = CountingJWTKey("new", "HS256", b"n" * 40)
    append_new = _AppendKeyOnce(keys, new_key)
    old_key = MutatingJWTKey("old", "HS256", b"o" * 40, mutation=append_new)
    keys.append(old_key)
    object.__setattr__(auth, "keys", keys)
    old_token = oracle_hs256(GOOD, b"o" * 40, kid="old")

    assert await auth.resolve_token(old_token) is not None
    assert len(keys) == 2
    assert new_key.spec_calls == []

    new_token = oracle_hs256(GOOD, b"n" * 40, kid="new")
    assert await auth.resolve_token(new_token) is not None
    assert len(new_key.spec_calls) == 1


async def test_jwt_resolver_sync_and_sync_returning_awaitable_keep_auth_semantics(
    pool: Pool,
) -> None:
    app = Hinoki(include=[jwt_sync_claims, jwt_awaitable_claims])
    client = await pool.start(app)
    sync_auth = SyncClaimTokens()
    awaitable_auth = AwaitableClaimTokens()

    sync_token = sync_auth.issue({"sub": "sync"})
    response = await client.request(
        "GET", "/jwt-sync-claims", headers={"authorization": f"Bearer {sync_token}"}
    )
    assert (response.status, response.body) == (200, {"id": "sync"})

    awaitable_token = awaitable_auth.issue({"sub": "sync"})
    response = await client.request(
        "GET",
        "/jwt-awaitable-claims",
        headers={"authorization": f"Bearer {awaitable_token}"},
    )
    assert (response.status, response.body) == (200, {"id": "sync"})

    denied_token = sync_auth.issue({"sub": "denied"})
    denied = await client.request(
        "GET",
        "/jwt-sync-claims",
        headers={"authorization": f"Bearer {denied_token}"},
    )
    assert denied.status == 401
    limited_token = sync_auth.issue({"sub": "limited"})
    limited = await client.request(
        "GET",
        "/jwt-sync-claims",
        headers={"authorization": f"Bearer {limited_token}"},
    )
    assert limited.status == 403
