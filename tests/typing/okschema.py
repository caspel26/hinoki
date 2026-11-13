"""Valid schema usage: must type-check cleanly under mypy --strict."""

from typing import Annotated
from uuid import UUID

from hinoki import UNSET, Field, Schema, Unset


class PostCreate(Schema):
    title: Annotated[str, Field(min_length=1, max_length=160)]
    body: str
    tags: list[str] = Field(default_factory=list)
    subtitle: str | None = None


class PostPatch(Schema):
    title: str | Unset = UNSET


class PostOut(Schema):
    id: UUID
    title: str


def build() -> str:
    data = PostCreate(title="Hello", body="text")
    patched: PostPatch = PostPatch()
    parsed: PostCreate = PostCreate.validate({"title": "x", "body": "y"})
    out = PostOut(id=UUID(int=1), title=parsed.title)
    tags: list[str] = data.tags
    return data.title + out.title + str(patched.title) + str(tags)
