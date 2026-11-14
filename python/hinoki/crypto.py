"""Password hashing (Argon2id), JWT authentication and random tokens, in Rust.

* ``PasswordHasher``: Argon2id with PHC strings, run on a small bounded thread
  pool with the GIL released, so hashing never stalls the event loop.
* ``JWTAuth``: a bearer-token provider that verifies compact JWTs against keys
  *you* configure; the token's ``alg`` is compared with the key's algorithm and
  never chooses it. ``HS256/384/512`` and ``EdDSA`` (Ed25519).
* ``random_token``: URL-safe text from the operating system's CSPRNG.

Secrets come from your settings or environment; nothing here has defaults for
keys. Failure reasons are logged at debug level (never with the token) and never
reach the client, who only sees 401 ``Invalid credentials``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from abc import abstractmethod
from collections.abc import Awaitable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

from hinoki import core
from hinoki.auth import BearerAuth, Caller
from hinoki.blocking import call_hook
from hinoki.httperrors import APIError

log = logging.getLogger("hinoki.crypto")

MIN_MEMORY_KIB = 19 * 1024  # OWASP floor for Argon2id with t=2, p=1
MIN_TIME_COST = 2
HS_MIN_BYTES = {"HS256": 32, "HS384": 48, "HS512": 64}
ALGORITHMS = (*HS_MIN_BYTES, "EdDSA")
ED25519_BYTES = 32
MAX_KID = 64
MAX_TTL = 366 * 24 * 3600  # a year


class HashingBusy(APIError):
    """Too many password hashes are waiting (HTTP 503)."""

    status = 503
    code = "overloaded"
    message = "The server is busy; try again shortly."


class PasswordHasher:
    """Argon2id hashes in PHC format, computed off the event loop.

    Parameters below the OWASP floor are refused unless ``allow_weak=True``
    (tests only). ``workers`` bounds concurrent hashing, ``max_pending`` the
    queue; beyond that ``hash``/``verify`` raise ``HashingBusy`` (503). A task
    cancelled while its hash runs frees its slot but the thread finishes, so at
    most ``workers`` extra hashes can be in flight; ``hash_sync``/``verify_sync``
    are not bounded. Only argon2id hashes are verified. For unknown users, verify
    against a fixed dummy hash so timing does not reveal who exists.
    """

    def __init__(
        self,
        *,
        memory_kib: int = MIN_MEMORY_KIB,
        time_cost: int = MIN_TIME_COST,
        lanes: int = 1,
        workers: int = 2,
        max_pending: int = 64,
        allow_weak: bool = False,
    ) -> None:
        if not allow_weak and (
            memory_kib < MIN_MEMORY_KIB or time_cost < MIN_TIME_COST
        ):
            raise ValueError(
                f"Argon2id parameters below memory_kib={MIN_MEMORY_KIB}, "
                f"time_cost={MIN_TIME_COST} are refused"
            )
        if workers < 1 or max_pending < 1 or lanes < 1:
            raise ValueError("workers, max_pending and lanes must be at least 1")
        self.memory_kib = memory_kib
        self.time_cost = time_cost
        self.lanes = lanes
        self.max_pending = max_pending
        self.pending = 0
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="hinoki-hash")

    def hash_sync(self, password: str) -> str:
        """Blocking hash (for scripts and sync handlers)."""
        return core.hash_password(password, self.memory_kib, self.time_cost, self.lanes)

    def verify_sync(self, password: str, stored: str) -> bool:
        """``False`` for a wrong password; ``ValueError`` for a malformed hash."""
        return core.verify_password(password, stored)

    async def run(self, work: Any, *arguments: Any) -> Any:
        if self.pending >= self.max_pending:
            raise HashingBusy
        self.pending += 1
        try:
            return await asyncio.get_running_loop().run_in_executor(
                self.pool, work, *arguments
            )
        finally:
            self.pending -= 1

    async def hash(self, password: str) -> str:
        return str(await self.run(self.hash_sync, password))

    async def verify(self, password: str, stored: str) -> bool:
        return bool(await self.run(self.verify_sync, password, stored))

    def needs_rehash(self, stored: str) -> bool:
        """True when ``stored`` was made with other parameters than these."""
        return core.password_needs_rehash(
            stored, self.memory_kib, self.time_cost, self.lanes
        )

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def random_token(size: int = 32) -> str:
    """``size`` random bytes (1 to 1024) as URL-safe text, from the OS CSPRNG."""
    return core.make_token(size)


@dataclasses.dataclass(frozen=True, slots=True)
class JWTKey:
    """One verification (and optionally signing) key.

    HMAC secrets must be at least as long as the hash (32/48/64 bytes). For
    ``EdDSA`` the key is the 32-byte private seed, or with ``public=True`` the
    32-byte public key of a verify-only deployment. ``kid`` is required when
    several keys are configured. The key is excluded from ``repr``.
    """

    kid: str | None
    algorithm: str
    key: bytes | str = dataclasses.field(repr=False)  # never printed or logged
    public: bool = False

    def __post_init__(self) -> None:
        if self.algorithm not in ALGORITHMS:
            raise ValueError(f"algorithm must be one of {ALGORITHMS}")
        material = self.material()
        if self.algorithm in HS_MIN_BYTES:
            if self.public or len(material) < HS_MIN_BYTES[self.algorithm]:
                raise ValueError(
                    f"{self.algorithm} needs a secret of at least "
                    f"{HS_MIN_BYTES[self.algorithm]} bytes"
                )
        elif len(material) != ED25519_BYTES:
            raise ValueError("EdDSA keys are 32 bytes")
        if self.kid is not None and (
            not self.kid or len(self.kid) > MAX_KID or not self.kid.isprintable()
        ):
            raise ValueError("kid must be 1 to 64 printable characters")

    @classmethod
    def verify_only(cls, kid: str | None, public_key: bytes) -> JWTKey:
        """An Ed25519 *public* key (32 bytes) that can verify but never sign. Use
        this, not the plain constructor, wherever you hold only the public key:
        a public key passed as a seed would let anyone who knows it forge tokens."""
        return cls(kid, "EdDSA", public_key, public=True)

    def material(self) -> bytes:
        return self.key.encode() if isinstance(self.key, str) else bytes(self.key)

    def spec(self) -> tuple[str | None, str, bytes, bool]:
        return (self.kid, self.algorithm, self.material(), self.public)


def check_key_set(keys: Sequence[JWTKey]) -> None:
    """Fail when the provider is built, not on the first request."""
    if not keys:
        raise ValueError("a JWT provider needs at least one key")
    kids = [k.kid for k in keys]
    if len(keys) > 1 and (None in kids or len(set(kids)) != len(kids)):
        raise ValueError("with several keys every key needs a distinct kid")
    signer = next((k for k in keys if not k.public), None)
    if len(keys) > 1 and signer is not None and signer.kid is None:
        raise ValueError("the signing key needs a kid")


class JWTAuth[T](BearerAuth[T]):
    """Bearer-token provider for JWTs. Subclass, set ``keys`` (and usually
    ``issuer`` and ``audience``) and implement ``resolve_claims``::

        class Tokens(JWTAuth[User]):
            keys = [JWTKey("2026-11", "HS256", settings.jwt_secret)]
            issuer = "https://api.example.com"
            audience = "blog"

            async def resolve_claims(self, claims):
                subject, raw = claims.get("sub"), claims.get("scopes", [])
                if not isinstance(subject, str) or not isinstance(raw, list):
                    return None  # claims are signed, but still untyped JSON
                try:
                    user = await User.aget_or_none(id=UUID(subject))
                except ValueError:  # a signed token can still carry any text
                    return None
                if user is None:
                    return None
                scopes = frozenset(s for s in raw if isinstance(s, str))
                return Caller(subject, user, scopes)
    """

    keys: ClassVar[Sequence[JWTKey]] = ()
    issuer: ClassVar[str | None] = None
    audience: ClassVar[str | None] = None
    leeway: ClassVar[int] = 0
    require_exp: ClassVar[bool] = True

    def __init__(self) -> None:
        check_key_set(self.keys)

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}

    def now(self) -> int:
        """The clock (override in tests)."""
        return int(time.time())

    @abstractmethod
    def resolve_claims(
        self, claims: dict[str, Any]
    ) -> Caller[T] | None | Awaitable[Caller[T] | None]:
        """The principal for verified claims, or ``None`` to refuse; ``async def``
        or plain ``def``."""

    async def resolve_token(self, token: str) -> Caller[T] | None:
        rules = (self.now(), self.leeway, self.issuer, self.audience, self.require_exp)
        try:
            payload = core.jwt_decode(token, [k.spec() for k in self.keys], rules)
        except ValueError as reason:
            log.debug("token rejected: %s", reason)
            return None
        try:
            claims = core.decode_json(payload)
        except ValueError:
            log.debug("token rejected: payload is not decodable")
            return None
        found: Caller[T] | None = await call_hook(self.resolve_claims, claims)
        return found

    def issue(self, claims: dict[str, Any], ttl: int = 900) -> str:
        """A signed token for ``claims`` valid for ``ttl`` seconds, signed with the
        first non-public key. ``iat``/``exp`` (and ``iss``/``aud`` when
        configured) are added unless already present."""
        signer = next((k for k in self.keys if not k.public), None)
        if signer is None:
            raise RuntimeError("no signing key is configured")
        if not 1 <= ttl <= MAX_TTL:
            raise ValueError(f"ttl must be between 1 and {MAX_TTL} seconds")
        now = self.now()
        body: dict[str, Any] = {**claims, "iat": now, "exp": now + ttl}  # server wins
        if self.issuer is not None:
            body.setdefault("iss", self.issuer)
        if self.audience is not None:
            body.setdefault("aud", self.audience)
        return core.jwt_encode(core.encode_json(body), signer.spec())
