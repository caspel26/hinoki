"""Persistent models. Tables are created by ``blog.tables.SQL`` (no migrations yet)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, field


def now() -> datetime:
    return datetime.now(UTC)


class User(Model, table="blog_users"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=80)
    token: str = field(unique=True)
    is_editor: bool = False


class Post(Model, table="blog_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    author_id: UUID
    title: str = field(max_length=160)
    slug: str = field(unique=True, max_length=180)
    body: str
    published: bool = False
    created_at: datetime = field(default_factory=now)
