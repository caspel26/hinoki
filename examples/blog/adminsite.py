"""The blog's admin: editors only, authenticated by a session cookie.

Hinoki has no login UI yet, so the cookie ``blog_token`` is set by whatever
signs the editor in (here: by hand or by the browser test). The admin itself
only needs an ``Access`` policy; this one reads the cookie instead of a header.
"""

from typing import Any

from examples.blog.models import Post, User
from examples.blog.security import WRITE
from hinoki import Access, Principal, native
from hinoki.admin import Admin, ModelAdmin
from hinoki.auth import BaseAuth, Credentials
from hinoki.httperrors import Unauthorized

COOKIE = "blog_token"


class CookieAuth(BaseAuth[User]):
    """Resolves the editor from the ``blog_token`` cookie."""

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "apiKey", "in": "cookie", "name": COOKIE}

    async def authenticate(self, credentials: Credentials) -> Principal[User] | None:
        cookies = native.parse_cookie_header(credentials.headers.get("cookie", ""))
        token = cookies.get(COOKIE)
        if not token:
            return None
        user = await User.aget_or_none(token=token)
        if user is None:
            raise Unauthorized("Invalid credentials.")
        scopes = frozenset({WRITE}) if user.is_editor else frozenset()
        return Principal(subject=user.name, identity=user, scopes=scopes)


class PostAdmin(ModelAdmin[Post]):
    columns = ("title", "slug", "published", "created_at")
    search = ("title",)
    per_page = 8


class UserAdmin(ModelAdmin[User]):
    """Users can be listed and renamed, never created or deleted here, and
    their tokens are neither shown nor editable."""

    columns = ("name", "is_editor")
    search = ("name",)
    exclude = ("token",)

    def can(
        self, principal: Principal[Any], action: str, row: User | None = None
    ) -> bool:
        return action in {"view", "change"}


def build_admin() -> Admin:
    editors = Access(auth=CookieAuth(), scopes=[WRITE])
    admin = Admin(access=editors, prefix="/admin", site="Blog admin")
    admin.register(Post, PostAdmin, slug="posts")
    admin.register(User, UserAdmin, slug="users")
    return admin
