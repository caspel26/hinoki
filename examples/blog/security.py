"""Authentication: an opaque bearer token resolved through the database."""

from examples.blog.models import User
from hinoki import Access, BearerAuth, Principal

WRITE = "posts:write"


class TokenAuth(BearerAuth[User]):
    """Looks the token up in ``blog_users``; editors get the write scope."""

    async def resolve_token(self, token: str) -> Principal[User] | None:
        user = await User.aget_or_none(token=token)
        if user is None:
            return None
        scopes = frozenset({WRITE}) if user.is_editor else frozenset()
        return Principal(subject=str(user.id), identity=user, scopes=scopes)


auth = TokenAuth()
editor = Access(auth=auth, scopes=[WRITE])
reader = Access(auth=auth)
