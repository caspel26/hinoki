"""The real ``Model``: construction and ``asave`` are statically checked."""

from uuid import UUID, uuid4

from hinoki.orm import Model, OnDelete, field, foreign_key


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


class Keyed(Model, abstract=True):
    id: UUID = field(primary_key=True, default_factory=uuid4)


class Named(Model, abstract=True):
    name: str = field(max_length=40)


class Product(Keyed, Named, table="products"):
    price: int = 0


def inherited() -> None:
    product = Product(name="Desk", price=3)
    print(product.id.hex, product.name.upper(), product.price + 1)

    Product(price=3)  # type: ignore[call-arg]
    Product(name=3)  # type: ignore[arg-type]


class Line(Model, table="lines"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    item_id: UUID = foreign_key(Item, on_delete=OnDelete.CASCADE)
    spare_id: UUID | None = foreign_key(Item, on_delete=OnDelete.SET_NULL, default=None)


def deletion_rules(rule: OnDelete, text: str) -> None:
    foreign_key(Item, on_delete=rule)
    foreign_key(Item, on_delete="cascade")  # type: ignore[arg-type]
    foreign_key(Item, on_delete=text)  # type: ignore[arg-type]
    foreign_key(Item, on_delete=OnDelete.DELETE)  # type: ignore[attr-defined]
    foreign_key(Item)  # type: ignore[call-arg]
