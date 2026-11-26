"""Authentication: an opaque bearer token resolved through the database."""

import hashlib

from hinoki import Access, BearerAuth, Caller, Permission

from .models import User


def hash_token(token: str) -> str:
    """The SHA-256 hex digest stored instead of the token (high-entropy, so no KDF)."""
    return hashlib.sha256(token.encode()).hexdigest()


class BlogPermission(Permission):
    WRITE = "posts:write"


WRITE = BlogPermission.WRITE


class TokenAuth(BearerAuth[User]):
    """Looks the token's digest up in ``blog_users``; editors get the write scope."""

    async def resolve_token(self, token: str) -> Caller[User] | None:
        user = await User.aget_or_none(token_hash=hash_token(token))
        if user is None:
            return None
        scopes = frozenset({BlogPermission.WRITE}) if user.is_editor else frozenset()
        return Caller(id=str(user.id), user=user, permissions=scopes)


auth = TokenAuth()
editor = Access(auth=auth, permissions=[BlogPermission.WRITE])
reader = Access(auth=auth)
