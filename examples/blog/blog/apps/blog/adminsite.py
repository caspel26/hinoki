"""The blog's admin: the admin's own users, with a sign-in page and per-model grants.

Nothing here handles authentication. ``Admin()`` without ``access=`` keeps its own
users (``hinoki admin create-superuser`` makes the first); a superuser adds the
editors in the Users section and ticks what each may do. The blog's own ``User``
rows are the authors of posts, a different thing from the people who sign in.
"""

import datetime
from typing import Any, ClassVar

from hinoki import Caller
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
from hinoki.utils import utc_now

from .models import Post, User

DAYS = 7


def per_day(rows: list[Post]) -> tuple[float, ...]:
    """Posts created per day over the last week, oldest first (real data)."""
    today = utc_now().date()
    days = [today - datetime.timedelta(days=n) for n in range(DAYS - 1, -1, -1)]
    return tuple(float(sum(1 for r in rows if r.created_at.date() == d)) for d in days)


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


# Signed with the app's ``admin.secret`` (BLOG_ADMIN__SECRET) when it is included.
site = Admin(
    theme="studio",
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


@site.admin(Post)
class PostAdmin(ModelAdmin[Post]):
    slug = "posts"
    icon = "file-text"
    columns = ("title", "published", "author_id", "created_at")
    ordering = ("-created_at", "-id")
    readonly = ("created_at",)
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
        await Post.filter(id__in=[p.id for p in posts]).aupdate(
            {"published": True}, returning=False
        )

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


@site.admin(User)
class UserAdmin(ModelAdmin[User]):
    """Authors can be listed and renamed, never created or deleted here; their
    token digests are neither shown nor editable, and editor rights are read-only."""

    slug = "authors"
    label = "Authors"
    icon = "users"
    columns = ("name", "is_editor")
    exclude = ("token_hash", "password_hash")
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
