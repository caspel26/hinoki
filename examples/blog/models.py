"""Persistent models. Tables are created by ``blog.tables.SQL`` (no migrations yet)."""

from datetime import datetime
from typing import ClassVar
from uuid import UUID, uuid4

from hinoki.orm import Model, field, foreign_key
from hinoki.orm.relations import Related
from hinoki.utils import utc_now


class User(Model, table="blog_users"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=80)
    token: str = field(unique=True)
    is_editor: bool = False
    username: str | None = field(unique=True, max_length=40, default=None)
    password_hash: str | None = None  # Argon2id PHC string, never a password
    posts: ClassVar["Related[list[Post]]"]


class Post(Model, table="blog_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    author_id: UUID = foreign_key(User, on_delete="restrict", related_name="posts")
    title: str = field(max_length=160)
    slug: str = field(unique=True, max_length=180)
    body: str
    published: bool = False
    created_at: datetime = field(default_factory=utc_now)
    author: ClassVar[Related[User]]
