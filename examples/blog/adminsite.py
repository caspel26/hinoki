"""The blog's admin: editors only, authenticated by a session cookie.

Hinoki has no login UI yet, so the cookie ``blog_token`` is set by whatever
signs the editor in (here: by hand or by the browser test). The admin itself
only needs an ``Access`` policy; this one reads the cookie instead of a header.
"""

import datetime
from typing import Any, ClassVar

from examples.blog.models import Post, User
from examples.blog.security import WRITE
from hinoki import Access, Caller, native
from hinoki.admin import (
    Admin,
    Avatar,
    Badge,
    Event,
    Fieldset,
    Link,
    ModelAdmin,
    Stat,
    Tab,
)
from hinoki.auth import BaseAuth, Credentials
from hinoki.httperrors import Unauthorized

COOKIE = "blog_token"
DAYS = 7


class CookieAuth(BaseAuth[User]):
    """Resolves the editor from the ``blog_token`` cookie."""

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "apiKey", "in": "cookie", "name": COOKIE}

    async def authenticate(self, credentials: Credentials) -> Caller[User] | None:
        cookies = native.parse_cookie_header(credentials.headers.get("cookie", ""))
        token = cookies.get(COOKIE)
        if not token:
            return None
        user = await User.aget_or_none(token=token)
        if user is None:
            raise Unauthorized("Invalid credentials.")
        scopes = frozenset({WRITE}) if user.is_editor else frozenset()
        return Caller(id=user.name, user=user, permissions=scopes)


def per_day(rows: list[Post]) -> tuple[float, ...]:
    """Posts created per day over the last week, oldest first (real data)."""
    today = datetime.datetime.now(datetime.UTC).date()
    days = [today - datetime.timedelta(days=n) for n in range(DAYS - 1, -1, -1)]
    return tuple(float(sum(1 for r in rows if r.created_at.date() == d)) for d in days)


class PostAdmin(ModelAdmin[Post]):
    label = "Posts"
    icon = "file-text"
    columns = ("title", "published", "author_id", "created_at")
    search = ("title",)
    ordering = ("-created_at", "-id")
    per_page = 8
    show_count = True
    bulk_delete = True
    readonly = ("created_at",)
    assign: ClassVar[dict[str, Any]] = {
        "author_id": lambda principal: principal.user.id
    }
    modified_field = "created_at"
    modified_label = "Created"
    badges: ClassVar[dict[str, Any]] = {
        "published": {
            True: Badge("Published", "ok", "circle-check"),
            False: Badge("Draft", "warn", "pencil"),
        }
    }
    tabs = (Tab("Published", {"published": True}), Tab("Drafts", {"published": False}))
    fieldsets = (Fieldset("Organization", ("author_id", "published")),)

    def queryset(self, principal: Caller[Any]) -> Any:
        return Post.all().select_related("author")

    def display(self, row: Post, column: str) -> Any:
        if column == "author_id":
            return Avatar(row.author.name)
        return super().display(row, column)

    async def stats(self, principal: Caller[Any]) -> list[Stat]:
        """Counts come from the database; only the last week's rows are loaded
        (for the trend lines), so the cost does not grow with the blog."""
        total = await self.queryset(principal).acount()
        published = await self.queryset(principal).filter(published=True).acount()
        since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=DAYS)
        week = await Post.filter(created_at__gte=since).aall()
        return [
            Stat("Posts", total, "file-text", "accent", per_day(week)),
            Stat(
                "Published",
                published,
                "circle-check",
                "ok",
                per_day([r for r in week if r.published]),
            ),
            Stat(
                "Drafts",
                total - published,
                "pencil",
                "warn",
                per_day([r for r in week if not r.published]),
            ),
        ]


class UserAdmin(ModelAdmin[User]):
    """Authors can be listed and renamed, never created or deleted here; their
    tokens are neither shown nor editable, and editor rights are read-only."""

    label = "Authors"
    icon = "users"
    columns = ("name", "is_editor")
    search = ("name",)
    exclude = ("token",)
    readonly = ("is_editor",)
    badges: ClassVar[dict[str, Any]] = {
        "is_editor": {
            True: Badge("Editor", "accent", "shield-check"),
            False: Badge("Reader", "neutral", "user"),
        }
    }

    def can(self, principal: Caller[Any], action: str, row: User | None = None) -> bool:
        return action in {"view", "change"}

    async def stats(self, principal: Caller[Any]) -> list[Stat]:
        return []


def verb(row: Post) -> str:
    return "published" if row.published else "drafted"


async def recent_activity(principal: Caller[Any]) -> list[Event]:
    """The latest posts, as activity (derived from real rows, not invented)."""
    rows = (
        await Post.all()
        .select_related("author")
        .order_by("-created_at")
        .limit(3)
        .aall()
    )
    return [
        Event(
            f"{row.author.name} {verb(row)} “{row.title}”",
            row.created_at.strftime("%Y-%m-%d %H:%M"),
            "circle-check" if row.published else "pencil",
            "ok" if row.published else "warn",
        )
        for row in rows
    ]


def build_admin(secret: str, theme: str = "studio") -> Admin:
    editors = Access(auth=CookieAuth(), permissions=[WRITE])
    admin = Admin(
        access=editors,
        secret=secret,
        prefix="/admin",
        theme=theme,
        site="Blog Studio",
        tagline="My blog",
        links=(
            Link("API docs", "/docs", "file-text"),
            Link("API reference", "/redoc", "layers"),
        ),
        activity=recent_activity,
        dashboard=("posts",),
    )
    admin.register(Post, PostAdmin, slug="posts")
    admin.register(User, UserAdmin, slug="users")
    return admin
