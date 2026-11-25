"""Password login and JWT sessions: Argon2id hashes and signed tokens, in Rust.

``POST /auth/login`` checks a username and password and answers with a short
lived JWT; ``GET /auth/me`` shows who a token belongs to. The opaque
token of the demo data (stored as a digest) keeps working for the posts API
and the admin.

``build_accounts`` runs only when a JWT secret is configured, so nothing (hash
pool, keys) exists otherwise.
Login is rate limited per client address by the built-in ``RateLimit``, which counts
in the ``hinoki_rate_limits`` table every worker shares (the ``ratelimit`` app of
``hinoki.toml``). Not included, on purpose: refresh tokens.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar
from uuid import UUID

from hinoki import (
    Access,
    Caller,
    Field,
    Module,
    ModuleContext,
    RateLimit,
    Resource,
    Schema,
    Secret,
    errors,
    http,
)
from hinoki.crypto import JWTAuth, JWTKey, PasswordHasher

from .models import User
from .security import WRITE

SESSION_SECONDS = 15 * 60


class LoginIn(Schema):
    username: Annotated[str, Field(min_length=1, max_length=40)]
    password: Annotated[str, Field(min_length=1, max_length=200)]


class SessionOut(Schema):
    access_token: str
    token_type: str
    expires_in: int


class MeOut(Schema):
    username: str | None
    name: str
    is_editor: bool


@dataclass(frozen=True, slots=True)
class PasswordService(Resource):
    """Worker-owned password hasher and dummy hash used to equalise login timing."""

    hasher: PasswordHasher
    unknown_user: str


class Passwords(Module):
    """Owns the Argon2id worker pool for the lifetime of one server worker."""

    name = "passwords"

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        hasher = PasswordHasher()
        unknown_user = await hasher.hash("not-a-real-password")
        ctx.provide(PasswordService, PasswordService(hasher, unknown_user))
        try:
            yield
        finally:
            hasher.close()


def build_accounts(secret: Secret) -> tuple[list[Callable[..., Any]], Module]:
    """The account actions and the module owning their password hasher."""

    class Sessions(JWTAuth[User]):
        keys: ClassVar[Sequence[JWTKey]] = (JWTKey("blog-1", "HS256", secret),)
        issuer = "blog"
        audience = "blog-api"

        async def resolve_claims(
            self, claims: dict[str, object]
        ) -> Caller[User] | None:
            subject = claims.get("sub")
            if not isinstance(subject, str):
                return None  # claims are signed, but still untyped JSON
            try:
                user_id = UUID(subject)
            except ValueError:
                return None
            user = await User.aget_or_none(id=user_id)
            if user is None:
                return None
            scopes = frozenset({WRITE}) if user.is_editor else frozenset()
            return Caller(id=subject, user=user, permissions=scopes)

    sessions = Sessions()
    signed_in = Access(auth=sessions)

    @http.post(
        "/auth/login",
        rate_limit=RateLimit(20, 60, scope="login"),
        errors=(errors.Unauthorized,),
    )
    async def login(data: LoginIn, passwords: PasswordService) -> SessionOut:
        """Exchange a username and password for a short-lived bearer token."""
        user = await User.aget_or_none(username=data.username)
        stored = (
            user.password_hash
            if user and user.password_hash
            else passwords.unknown_user
        )
        genuine = await passwords.hasher.verify(data.password, stored)
        if user is None or not user.password_hash or not genuine:
            raise errors.Unauthorized(
                "Invalid credentials."
            )  # one answer for every cause
        token = sessions.issue({"sub": str(user.id)}, ttl=SESSION_SECONDS)
        return SessionOut(
            access_token=token,
            token_type="Bearer",  # noqa: S106 - the HTTP scheme name
            expires_in=SESSION_SECONDS,
        )

    @http.get("/auth/me", access=signed_in)
    async def me(caller: Caller[User]) -> MeOut:
        """Who the bearer token belongs to."""
        user = caller.user
        return MeOut(username=user.username, name=user.name, is_editor=user.is_editor)

    return [login, me], Passwords()
