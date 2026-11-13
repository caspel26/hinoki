"""Public input/output contracts, separate from the persistent models."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from hinoki import Field, Schema

Slug = Annotated[
    str, Field(min_length=1, max_length=180, pattern=r"[a-z0-9]+(-[a-z0-9]+)*")
]


class PostCreate(Schema):
    """What an editor may set. Author and publication state are server-side."""

    title: Annotated[str, Field(min_length=1, max_length=160)]
    slug: Slug
    body: str


class PostOut(Schema):
    id: UUID
    author_id: UUID
    author_name: str
    title: str
    slug: str
    body: str
    published: bool
    created_at: datetime


class HealthOut(Schema):
    status: str
