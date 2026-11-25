"""Password hashing and JWT authentication, checked against independent oracles.

Oracles: the standard library's hmac/hashlib for HMAC tokens; a hash made by
argon2-cffi (reference C implementation) and an Ed25519 token signed by the
`cryptography` library, both generated once and pinned here.
"""

import asyncio
import base64
import dataclasses
import gc
import hashlib
import hmac
import json
from collections.abc import Sequence
from typing import Any, ClassVar

import pytest

from hinoki import Access, Caller, Hinoki, Request, Secret, core, http
from hinoki.crypto import (
    ClaimsJWTAuth,
    HashingBusy,
    JWTAuth,
    JWTKey,
    PasswordHasher,
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
class AppendingJWTKey(JWTKey):
    keys: list[JWTKey] = dataclasses.field(repr=False, compare=False, kw_only=True)
    appended_key: JWTKey = dataclasses.field(repr=False, compare=False, kw_only=True)

    def spec(self) -> tuple[str | None, str, bytes, bool]:
        if self.appended_key not in self.keys:
            self.keys.append(self.appended_key)
        return JWTKey.spec(self)


GOOD = {
    "sub": "ann",
    "exp": 2000,
    "iss": "https://api.test",
    "aud": "blog",
    "scopes": ["w"],
}


async def verdict(token: str, auth: Tokens | None = None) -> bool:
    return await (auth or Tokens()).resolve_token(token) is not None


async def test_jwt_key_spec_list_mutation_keeps_python_iteration_semantics() -> None:
    new_key = JWTKey("new", "HS256", b"n" * 40)
    keys: list[JWTKey] = []
    keys.append(
        AppendingJWTKey("old", "HS256", b"o" * 40, keys=keys, appended_key=new_key)
    )
    auth = Tokens()
    object.__setattr__(auth, "keys", keys)
    token = oracle_hs256(GOOD, b"n" * 40, kid="new")

    assert await auth.resolve_token(token) is not None
    assert keys == [keys[0], new_key]


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


async def test_hasher_threads_start_on_use_and_stop_on_close() -> None:
    async with PasswordHasher(workers=1, **WEAK) as hasher:
        assert hasher._pool is None
        stored = await hasher.hash("pw")
        pool = hasher._pool
        assert pool is not None
    assert hasher._pool is None and pool._shutdown
    hasher.close()  # idempotent
    assert await hasher.verify("pw", stored)  # a closed hasher starts again
    hasher.close()
    with PasswordHasher(**WEAK) as sync_hasher:
        assert await sync_hasher.verify("pw", stored)
    assert sync_hasher._pool is None
    forgotten = PasswordHasher(**WEAK)
    await forgotten.hash("pw")
    orphan = forgotten._pool
    del forgotten
    gc.collect()
    assert orphan is not None and orphan._shutdown


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


def test_a_leeway_beyond_the_bound_is_refused_when_the_provider_is_built() -> None:
    for leeway in (-1, 121, 10_000):

        class Slack(Tokens):
            pass

        Slack.leeway = leeway
        with pytest.raises(ValueError, match="leeway must be between 0 and 120"):
            Slack()


async def test_leeway_and_audience_lists_work() -> None:
    class Slack(Tokens):
        leeway = 120

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


def test_private_jwt_verifier_binding_reuses_keys_with_current_rules() -> None:
    specs: list[tuple[str | None, str, bytes, bool]] = [("k1", "HS256", SECRET, False)]
    token = oracle_hs256({**GOOD, "exp": 1_000}, kid="k1")
    verifier = core._JWTVerifier(specs)
    valid_rules = (999, 0, "https://api.test", "blog", True)
    assert verifier.verify(token, valid_rules) == core.jwt_decode(
        token, specs, valid_rules
    )

    expired_rules = (1_000, 0, "https://api.test", "blog", True)
    with pytest.raises(ValueError, match="expired") as expected:
        core.jwt_decode(token, specs, expired_rules)
    with pytest.raises(ValueError, match="expired") as actual:
        verifier.verify(token, expired_rules)
    assert str(actual.value) == str(expected.value)
    assert not hasattr(verifier, "__dict__")
    attribute = "keys"
    with pytest.raises(AttributeError):
        setattr(verifier, attribute, specs)


class ClaimsOnly(ClaimsJWTAuth):
    keys: ClassVar[Sequence[JWTKey]] = (JWTKey("k1", "HS256", SECRET),)

    def now(self) -> int:
        return 1_000


def test_claims_auth_builds_the_caller_from_a_valid_subject_and_scopes() -> None:
    caller = ClaimsOnly().resolve_claims({"sub": "ann", "scopes": ["r", 7]})
    assert caller is not None
    assert (caller.id, caller.user, caller.permissions) == (
        "ann",
        None,
        frozenset({"r"}),
    )


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"sub": ""},
        {"sub": 5},
        {"sub": "ann", "scopes": "r"},
    ],
)
def test_claims_auth_refuses_an_unusable_subject_or_scope_claim(
    claims: dict[str, Any],
) -> None:
    assert ClaimsOnly().resolve_claims(claims) is None


def test_a_jwt_key_takes_a_secret_and_never_shows_it() -> None:
    material = "k" * 32
    key = JWTKey(None, "HS256", Secret(material))
    assert key.material() == material.encode()
    assert material not in repr(key)
    assert key.material() == JWTKey(None, "HS256", material).material()


def test_private_jwt_verifier_verify_claims_matches_decode_and_errors() -> None:
    specs: list[tuple[str | None, str, bytes, bool]] = [("k1", "HS256", SECRET, False)]
    verifier = core._JWTVerifier(specs)
    rich = {
        **GOOD,
        "count": 42,
        "big": 10**30,
        "ratio": 1.25,
        "flag": True,
        "empty": None,
        "nested": {"items": [1, "two", False]},
    }
    token = oracle_hs256(rich, kid="k1")
    rules = (1_000, 0, "https://api.test", "blog", True)
    assert verifier.verify_claims(token, rules) == core.decode_json(
        verifier.verify(token, rules)
    )

    with pytest.raises(ValueError, match="expired"):
        verifier.verify_claims(token, (2_000, 0, "https://api.test", "blog", True))

    wrong_sig = oracle_hs256(GOOD, key=b"x" * 40, kid="k1")
    with pytest.raises(ValueError, match="signature"):
        verifier.verify_claims(wrong_sig, rules)

    head_hs512 = b64(b'{"alg":"HS512","kid":"k1"}')
    body_good = b64(json.dumps(GOOD).encode())
    with pytest.raises(ValueError, match="algorithm"):
        verifier.verify_claims(f"{head_hs512}.{body_good}.AAAA", rules)

    deep_json = (
        b'{"exp":2000,"iss":"https://api.test","aud":"blog","d":'
        + b"[" * 140
        + b"1"
        + b"]" * 140
        + b"}"
    )
    head = b64(b'{"alg":"HS256","kid":"k1"}')
    body = b64(deep_json)
    sig = b64(hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest())
    with pytest.raises(ValueError, match="malformed"):
        verifier.verify_claims(f"{head}.{body}.{sig}", rules)


async def test_jwt_verifier_is_cached_and_invalidated_on_key_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CachedTokens(Tokens):
        keys: ClassVar[Sequence[JWTKey]] = (JWTKey("k1", "HS256", SECRET),)

    auth = CachedTokens()
    token1 = oracle_hs256(GOOD, kid="k1")
    assert await auth.resolve_token(token1) is not None
    first_entry = auth._verifier_entry
    assert first_entry is not None
    assert await auth.resolve_token(token1) is not None
    assert auth._verifier_entry is first_entry

    # Class-level reassignment invalidates the cache.
    secret2 = b"m" * 40
    monkeypatch.setattr(CachedTokens, "keys", (JWTKey("k2", "HS256", secret2),))
    token2 = oracle_hs256(GOOD, key=secret2, kid="k2")
    assert await auth.resolve_token(token1) is None
    assert await auth.resolve_token(token2) is not None
    second_entry = auth._verifier_entry
    assert second_entry is not None and second_entry is not first_entry

    # Mutable list keys reuse cache until mutated in-place.
    mutable_keys = [JWTKey("k1", "HS256", SECRET)]
    object.__setattr__(auth, "keys", mutable_keys)
    assert await auth.resolve_token(token1) is not None
    list_entry = auth._verifier_entry
    assert list_entry is not None
    assert await auth.resolve_token(token1) is not None
    assert auth._verifier_entry is list_entry

    mutable_keys.append(JWTKey("k2", "HS256", secret2))
    assert await auth.resolve_token(token2) is not None
    assert auth._verifier_entry is not list_entry

    mutable_keys.clear()
    assert await auth.resolve_token(token1) is None
