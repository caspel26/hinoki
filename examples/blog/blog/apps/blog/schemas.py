"""Public input/output contracts, separate from the persistent models."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from hinoki import UNSET, Field, Schema, Unset

Slug = Annotated[
    str, Field(min_length=1, max_length=180, pattern=r"[a-z0-9]+(-[a-z0-9]+)*")
]


class PostCreate(Schema):
    """What an editor may set. Author and publication state are server-side."""

    title: Annotated[str, Field(min_length=1, max_length=160)]
    slug: Slug
    body: Annotated[str, Field(max_length=20_000)]
    tag_ids: Annotated[set[UUID], Field(max_length=10)] | Unset = UNSET


class TagCreate(Schema):
    name: Annotated[str, Field(min_length=1, max_length=40)]


class TagOut(Schema):
    id: UUID
    name: str


class PostOut(Schema):
    id: UUID
    author_id: UUID
    author_name: str
    title: str
    slug: str
    body: str
    published: bool
    created_at: datetime
    tag_ids: Annotated[set[UUID], Field(max_length=10)]


class HealthOut(Schema):
    status: str
