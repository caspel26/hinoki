"""Valid schema usage: must type-check cleanly under mypy --strict."""

from collections.abc import Iterable
from typing import Annotated
from uuid import UUID

from hinoki import UNSET, Field, Schema, SchemaConfig, SchemaExtra, Unset


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


class VendorWebhook(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    event_id: str


def convert_many(rows: Iterable[object]) -> list[PostOut]:
    return PostOut.from_models(rows)


def build() -> str:
    data = PostCreate(title="Hello", body="text")
    patched: PostPatch = PostPatch()
    parsed: PostCreate = PostCreate.validate({"title": "x", "body": "y"})
    out = PostOut(id=UUID(int=1), title=parsed.title)
    webhook = VendorWebhook(event_id="evt-1")
    tags: list[str] = data.tags
    return data.title + out.title + webhook.event_id + str(patched.title) + str(tags)
