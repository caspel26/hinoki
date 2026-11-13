"""The real ``Model``: construction and ``asave`` are statically checked."""

from uuid import UUID, uuid4

from hinoki.orm import Model, field


class Item(Model, table="items"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=40)
    count: int = 0


async def checked() -> None:
    item = Item(title="Hello")
    saved: Item = await item.asave()
    saved.count += 1
    await saved.asave()
    print(saved.title.upper())

    Item(title=3)  # type: ignore[arg-type]
    Item()  # type: ignore[call-arg]
    Item(title="x", nope=1)  # type: ignore[call-arg]
    print(saved.titel)  # type: ignore[attr-defined]
