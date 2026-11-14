"""Password login and JWT sessions: Argon2id hashes and signed tokens, in Rust.

``POST /auth/login`` checks a username and password and answers with a short
lived JWT; ``GET /auth/me`` shows who a token belongs to. The opaque
``blog_token`` of the demo data keeps working for the posts API and the admin.

Built by a factory so that nothing (hash pool, keys) exists at import time.
Not included, on purpose: login rate limiting and refresh tokens. Put a limiter
in front of ``/auth/login`` before exposing it.
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Any, ClassVar
from uuid import UUID

from examples.blog.models import User
from examples.blog.security import WRITE
from hinoki import Access, Caller, Field, Schema, Unauthorized, http
from hinoki.crypto import JWTAuth, JWTKey, PasswordHasher

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


def build_accounts(secret: str) -> tuple[list[Callable[..., Any]], PasswordHasher]:
    """The account actions and the hasher they own (close it at shutdown)."""
    hasher = PasswordHasher()
    unknown_user = hasher.hash_sync("not-a-real-password")  # equalises timing

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

    @http.post("/auth/login")
    async def login(data: LoginIn) -> SessionOut:
        """Exchange a username and password for a short-lived bearer token."""
        user = await User.aget_or_none(username=data.username)
        stored = user.password_hash if user and user.password_hash else unknown_user
        genuine = await hasher.verify(data.password, stored)
        if user is None or not user.password_hash or not genuine:
            raise Unauthorized("Invalid credentials.")  # one answer for every cause
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

    return [login, me], hasher
