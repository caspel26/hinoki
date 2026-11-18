"""The blog's admin: editors only, authenticated by a session cookie.

Hinoki has no login UI yet, so the cookie ``blog_token`` is set by whatever
signs the editor in (here: by hand or by the browser test). The admin itself
only needs an ``Access`` policy; this one reads the cookie instead of a header.
"""

import datetime
from typing import Any, ClassVar

from hinoki import Access, Caller, core
from hinoki.admin import (
    Admin,
    Avatar,
    Event,
    Link,
    ModelAdmin,
    Stat,
    Tone,
    action,
)
from hinoki.auth import BaseAuth, Credentials
from hinoki.errors import Unauthorized
from hinoki.utils import utc_now

from .models import Post, User
from .security import WRITE

COOKIE = "blog_token"
DAYS = 7


class CookieAuth(BaseAuth[User]):
    """Resolves the editor from the ``blog_token`` cookie."""

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "apiKey", "in": "cookie", "name": COOKIE}

    async def authenticate(self, credentials: Credentials) -> Caller[User] | None:
        cookies = core.parse_cookie_header(credentials.headers.get("cookie", ""))
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
    today = utc_now().date()
    days = [today - datetime.timedelta(days=n) for n in range(DAYS - 1, -1, -1)]
    return tuple(float(sum(1 for r in rows if r.created_at.date() == d)) for d in days)


class PostAdmin(ModelAdmin, model=Post):  # type: ignore[type-arg]
    slug = "posts"
    icon = "file-text"
    columns = ("title", "published", "author_id", "created_at")
    ordering = ("-created_at", "-id")
    readonly = ("created_at",)
    assign: ClassVar[dict[str, Any]] = {
        "author_id": lambda principal: principal.user.id
    }
    modified_field = "created_at"
    modified_label = "Created"
    badges: ClassVar[dict[str, Any]] = {
        "published": {
            True: ("Published", Tone.OK, "circle-check"),
            False: ("Draft", Tone.WARN, "pencil"),
        }
    }
    tabs = ("published",)  # one tab for each badge: Published | Draft
    fieldsets = {"Organization": ("author_id", "published")}  # noqa: RUF012

    @action(confirm="Publish the selected posts?")
    async def publish(self, posts: tuple[Post, ...], principal: Caller[Any]) -> None:
        await Post.filter(id__in=[p.id for p in posts]).aupdate({"published": True})

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
        since = utc_now() - datetime.timedelta(days=DAYS)
        week = await Post.filter(created_at__gte=since)
        return [
            Stat("Posts", total, "file-text", Tone.ACCENT, per_day(week)),
            Stat(
                "Published",
                published,
                "circle-check",
                Tone.OK,
                per_day([r for r in week if r.published]),
            ),
            Stat(
                "Drafts",
                total - published,
                "pencil",
                Tone.WARN,
                per_day([r for r in week if not r.published]),
            ),
        ]


class UserAdmin(ModelAdmin, model=User):  # type: ignore[type-arg]
    """Authors can be listed and renamed, never created or deleted here; their
    tokens are neither shown nor editable, and editor rights are read-only."""

    slug = "users"
    label = "Authors"
    icon = "users"
    columns = ("name", "is_editor")
    exclude = ("token",)
    readonly = ("is_editor",)
    badges: ClassVar[dict[str, Any]] = {
        "is_editor": {
            True: ("Editor", Tone.ACCENT, "shield-check"),
            False: ("Reader", Tone.NEUTRAL, "user"),
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
    rows = await Post.all().select_related("author").order_by("-created_at").limit(3)
    return [
        Event(
            f"{row.author.name} {verb(row)} “{row.title}”",
            row.created_at.strftime("%Y-%m-%d %H:%M"),
            "circle-check" if row.published else "pencil",
            Tone.OK if row.published else Tone.WARN,
        )
        for row in rows
    ]


def build_admin(theme: str = "studio") -> Admin:
    """The site, signed with the app's ``admin.secret``; one line per model."""
    editors = Access(auth=CookieAuth(), permissions=[WRITE])
    site = Admin(
        access=editors,
        theme=theme,
        site="Blog Studio",
        tagline="My blog",
        links=(
            Link("API docs", "/docs", "file-text"),
            Link("API reference", "/redoc", "layers"),
        ),
        activity=recent_activity,
        dashboard=("posts",),
        per_page=8,
        show_count=True,
    )
    site.register(PostAdmin)
    site.register(UserAdmin)
    return site
