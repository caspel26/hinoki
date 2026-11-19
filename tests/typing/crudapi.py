"""Valid class-based CRUD declarations and operation-hook signatures."""

from typing import Any
from uuid import UUID, uuid4

from hinoki import API, Caller, Schema, crud
from hinoki.orm import Model, QuerySet, field


class Post(Model, table="typing_crud_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str


class PostOut(Schema):
    id: UUID
    title: str


class PostCreate(Schema):
    title: str


@crud(Post, path="/posts", read=PostOut, create=PostCreate, public_writes=True)
class Posts(API):
    def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Post]:
        del principal
        return Post.all()

    async def store(
        self, values: dict[str, Any], principal: Caller[Any] | None
    ) -> Post:
        del principal
        return await Post.acreate(**values)

    async def to_schema(self, item: Post) -> PostOut:
        return PostOut(id=item.id, title=item.title)
