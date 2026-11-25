"""Persistent models; checked-in SQL migrations define their table state.

A ``Publication`` is the tenant: users and posts belong to exactly one, and an
editor works only inside theirs.
"""

from datetime import datetime
from typing import ClassVar
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import (
    ManyToMany,
    Model,
    OnDelete,
    field,
    foreign_key,
    many_to_many,
    pk,
)
from hinoki.orm.relations import Related
from hinoki.utils import utc_now


@db.table("blog_publications")
class Publication(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=80)
    slug: str = field(unique=True, max_length=80)


@db.table("blog_users")
@db.indexed("publication_id")  # a foreign key is always indexed (HNK-DB-002)
class User(Model):
    id: UUID = pk(default_factory=uuid4)
    publication_id: UUID = foreign_key(
        Publication, on_delete=OnDelete.RESTRICT, related_name="users"
    )
    name: str = field(max_length=80)
    token_hash: str = field(unique=True)  # SHA-256 hex digest, never the token
    is_editor: bool = False
    username: str | None = field(unique=True, max_length=40, default=None)
    password_hash: str | None = None  # Argon2id PHC string, never a password
    posts: ClassVar["Related[list[Post]]"]


@db.table("blog_tags")
@db.indexed("publication_id")
@db.unique("publication_id", "name")
class Tag(Model):
    """A label of one publication; posts link to tags of their own publication."""

    id: UUID = pk(default_factory=uuid4)
    publication_id: UUID = foreign_key(
        Publication, on_delete=OnDelete.RESTRICT, related_name="tags"
    )
    name: str = field(max_length=40)


@db.table("blog_posts")
@db.indexed("author_id")
@db.indexed("publication_id")
class Post(Model):
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
    tags: ClassVar[ManyToMany[Tag]] = many_to_many(
        Tag, related_name="posts", max_items=10, ids_name="tag_ids"
    )


@db.table("blog_audit")
@db.indexed("post_id")
@db.ordered_by("-created_at", "-id")
class AuditEntry(Model):
    """What happened to a post, written by the ``audit`` event handlers."""

    id: UUID = pk(default_factory=uuid4)
    post_id: UUID = foreign_key(Post, on_delete=OnDelete.CASCADE, related_name="audit")
    kind: str = field(max_length=40)
    created_at: datetime = field(default_factory=utc_now)
