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
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

from hinoki import native
from hinoki.auth import BearerAuth, Principal
from hinoki.httperrors import APIError

log = logging.getLogger("hinoki.crypto")

MIN_MEMORY_KIB = 19 * 1024  # OWASP floor for Argon2id with t=2, p=1
MIN_TIME_COST = 2
HS_MIN_BYTES = {"HS256": 32, "HS384": 48, "HS512": 64}
ALGORITHMS = (*HS_MIN_BYTES, "EdDSA")
ED25519_BYTES = 32
MAX_KID = 64


class HashingBusy(APIError):
    """Too many password hashes are waiting (HTTP 503)."""

    status = 503
    code = "overloaded"
    message = "The server is busy; try again shortly."


class PasswordHasher:
    """Argon2id hashes in PHC format, computed off the event loop.

    Parameters below the OWASP floor are refused unless ``allow_weak=True``
    (tests only). ``workers`` bounds concurrent hashing, ``max_pending`` the
    queue; beyond that ``hash``/``verify`` raise ``HashingBusy`` (503).
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
        return native.hash_password(
            password, self.memory_kib, self.time_cost, self.lanes
        )

    def verify_sync(self, password: str, stored: str) -> bool:
        """``False`` for a wrong password; ``ValueError`` for a malformed hash."""
        return native.verify_password(password, stored)

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
        return native.password_needs_rehash(
            stored, self.memory_kib, self.time_cost, self.lanes
        )

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def random_token(size: int = 32) -> str:
    """``size`` random bytes (1 to 1024) as URL-safe text, from the OS CSPRNG."""
    return native.make_token(size)


@dataclasses.dataclass(frozen=True, slots=True)
class JWTKey:
    """One verification (and optionally signing) key.

    HMAC secrets must be at least as long as the hash (32/48/64 bytes). For
    ``EdDSA`` the key is the 32-byte private seed, or with ``public=True`` the
    32-byte public key of a verify-only deployment. ``kid`` is required when
    several keys are configured.
    """

    kid: str | None
    algorithm: str
    key: bytes | str
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

    def material(self) -> bytes:
        return self.key.encode() if isinstance(self.key, str) else bytes(self.key)

    def spec(self) -> tuple[str | None, str, bytes, bool]:
        return (self.kid, self.algorithm, self.material(), self.public)


class JWTAuth[T](BearerAuth[T]):
    """Bearer-token provider for JWTs. Subclass, set ``keys`` (and usually
    ``issuer`` and ``audience``) and implement ``resolve_claims``::

        class Tokens(JWTAuth[User]):
            keys = [JWTKey("2026-11", "HS256", settings.jwt_secret)]
            issuer = "https://api.example.com"
            audience = "blog"

            async def resolve_claims(self, claims):
                user = await User.aget_or_none(id=claims["sub"])
                if user is None:
                    return None
                scopes = frozenset(claims.get("scopes", []))
                return Principal(claims["sub"], user, scopes)
    """

    keys: ClassVar[Sequence[JWTKey]] = ()
    issuer: ClassVar[str | None] = None
    audience: ClassVar[str | None] = None
    leeway: ClassVar[int] = 0
    require_exp: ClassVar[bool] = True

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}

    def now(self) -> int:
        """The clock (override in tests)."""
        return int(time.time())

    @abstractmethod
    async def resolve_claims(self, claims: dict[str, Any]) -> Principal[T] | None:
        """The principal for verified claims, or ``None`` to refuse."""

    async def resolve_token(self, token: str) -> Principal[T] | None:
        if not self.keys:
            raise RuntimeError(f"{type(self).__name__} has no keys configured")
        rules = (self.now(), self.leeway, self.issuer, self.audience, self.require_exp)
        try:
            payload = native.jwt_decode(token, [k.spec() for k in self.keys], rules)
        except ValueError as reason:
            log.debug("token rejected: %s", reason)
            return None
        claims = native.decode_json(payload)
        return await self.resolve_claims(claims)

    def issue(self, claims: dict[str, Any], ttl: int = 900) -> str:
        """A signed token for ``claims`` valid for ``ttl`` seconds, signed with the
        first non-public key. ``iat``/``exp`` (and ``iss``/``aud`` when
        configured) are added unless already present."""
        signer = next((k for k in self.keys if not k.public), None)
        if signer is None:
            raise RuntimeError("no signing key is configured")
        now = self.now()
        body: dict[str, Any] = {"iat": now, "exp": now + ttl, **claims}
        if self.issuer is not None:
            body.setdefault("iss", self.issuer)
        if self.audience is not None:
            body.setdefault("aud", self.audience)
        return native.jwt_encode(native.encode_json(body), signer.spec())
