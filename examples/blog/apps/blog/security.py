"""Authentication: an opaque bearer token resolved through the database."""

from hinoki import Access, BearerAuth, Caller, Permission

from .models import User


class BlogPermission(Permission):
    WRITE = "posts:write"


WRITE = BlogPermission.WRITE


class TokenAuth(BearerAuth[User]):
    """Looks the token up in ``blog_users``; editors get the write scope."""

    async def resolve_token(self, token: str) -> Caller[User] | None:
        user = await User.aget_or_none(token=token)
        if user is None:
            return None
        scopes = frozenset({BlogPermission.WRITE}) if user.is_editor else frozenset()
        return Caller(id=str(user.id), user=user, permissions=scopes)


auth = TokenAuth()
editor = Access(auth=auth, permissions=[BlogPermission.WRITE])
reader = Access(auth=auth)
