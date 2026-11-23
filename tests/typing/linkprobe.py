"""Many-to-many typing: the link set is generic in its target, so a type checker
rejects rows of another model and wrong result types (mypy --strict)."""

from typing import ClassVar
from uuid import UUID, uuid4

from hinoki.orm import LinkKey, ManyToMany, Model, field, many_to_many


class LinkTag(Model, table="typing_link_tags"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    label: str


class LinkPost(Model, table="typing_link_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str
    tags: ClassVar[ManyToMany[LinkTag]] = many_to_many(LinkTag, ids_name="tag_ids")
    tag_ids: ClassVar[tuple[LinkKey, ...]]


async def links(post: LinkPost, tag: LinkTag) -> None:
    await post.tags.aadd(tag, tag.id)
    await post.tags.aset([tag], within=LinkTag.filter(label="x"))
    found: list[LinkTag] = await post.tags.alist()
    count: int = await post.tags.acount()
    ids: tuple[LinkKey, ...] = post.tag_ids
    print(found[0].label.upper(), count, ids)

    await post.tags.aadd(post)  # type: ignore[arg-type]
    await post.tags.aset([post])  # type: ignore[list-item]
    wrong: list[LinkPost] = await post.tags.alist()  # type: ignore[assignment]
    print(wrong)
    post.tags = []  # type: ignore[misc]
