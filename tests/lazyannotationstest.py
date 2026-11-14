"""Models in a module without ``from __future__ import annotations``.

Since Python 3.14 such a class has no ``__annotations__`` entry in its
``__dict__`` (annotations are evaluated lazily). Reading it directly made the
model lose its foreign-key relations on 3.14; this module keeps that from
coming back, so it must not gain the future import.
"""

from uuid import UUID, uuid4

from hinoki.orm import Model, field, foreign_key


class LazyOwner(Model, table="lazy_owners"):
    id: UUID = field(primary_key=True, default_factory=uuid4)


class Carried:
    owner_id: UUID = foreign_key(
        LazyOwner, on_delete="cascade", related_name="{model}_set"
    )
    label: str = ""


class LazyThing(Model, table="lazy_things"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID = foreign_key(LazyOwner, on_delete="cascade", related_name="things")


class LazyCarrier(Carried, Model, table="lazy_carriers"):
    id: UUID = field(primary_key=True, default_factory=uuid4)


def test_foreign_keys_are_registered_without_the_future_import() -> None:
    assert sorted(LazyThing.__relations__) == ["owner"]
    assert {"things", "lazycarrier_set"} <= set(LazyOwner.__relations__)


def test_mixin_fields_are_collected_without_the_future_import() -> None:
    assert LazyCarrier.__columns__ == ("owner_id", "label", "id")
    assert sorted(LazyCarrier.__relations__) == ["owner"]
    owner = LazyOwner()
    # A type checker does not see a plain mixin's fields as constructor arguments.
    carrier = LazyCarrier(owner_id=owner.id, label="x")  # type: ignore[call-arg]
    assert carrier.label == "x"
