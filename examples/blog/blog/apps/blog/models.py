"""Persistent models; checked-in SQL migrations define their table state.

A ``Publication`` is the tenant: users and posts belong to exactly one, and an
editor works only inside theirs.
"""

from datetime import datetime
from typing import ClassVar
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, OnDelete, field, foreign_key, pk
from hinoki.orm.relations import Related
from hinoki.utils import utc_now


class Publication(Model, table="blog_publications"):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=80)
    slug: str = field(unique=True, max_length=80)


@db.indexed("publication_id")  # a foreign key is always indexed (HNK-DB-002)
class User(Model, table="blog_users"):
    id: UUID = pk(default_factory=uuid4)
    publication_id: UUID = foreign_key(
        Publication, on_delete=OnDelete.RESTRICT, related_name="users"
    )
    name: str = field(max_length=80)
    token: str = field(unique=True)
    is_editor: bool = False
    username: str | None = field(unique=True, max_length=40, default=None)
    password_hash: str | None = None  # Argon2id PHC string, never a password
    posts: ClassVar["Related[list[Post]]"]


@db.indexed("author_id")
@db.indexed("publication_id")
class Post(Model, table="blog_posts"):
    id: UUID = pk(default_factory=uuid4)
    publication_id: UUID = foreign_key(
        Publication, on_delete=OnDelete.RESTRICT, related_name="posts"
    )
    author_id: UUID = foreign_key(
        User, on_delete=OnDelete.RESTRICT, related_name="posts"
    )
    title: str = field(max_length=160)
    slug: str = field(unique=True, max_length=180)
    body: str
    published: bool = False
    created_at: datetime = field(default_factory=utc_now)
    author: ClassVar[Related[User]]


@db.indexed("post_id")
@db.ordered_by("-created_at", "-id")
class AuditEntry(Model, table="blog_audit"):
    """What happened to a post, written by the ``audit`` event handlers."""

    id: UUID = pk(default_factory=uuid4)
    post_id: UUID = foreign_key(Post, on_delete=OnDelete.CASCADE, related_name="audit")
    kind: str = field(max_length=40)
    created_at: datetime = field(default_factory=utc_now)
