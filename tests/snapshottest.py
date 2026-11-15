"""Row snapshots keep immutable values as they are and copy mutable ones."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from hinoki.orm import Model, field
from hinoki.orm.model import SNAPSHOT


class Reading(Model, table="snapshot_readings"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    label: str = ""
    taken_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    amount: Decimal = Decimal(0)
    tags: list[str] = field(default_factory=list)


def test_snapshot_shares_immutable_values_and_copies_mutable_ones() -> None:
    reading = Reading(label="a", amount=Decimal("1.5"), tags=["x"])
    reading.remember()
    snapshot = reading.__dict__[SNAPSHOT]

    assert snapshot["id"] is reading.id
    assert snapshot["label"] is reading.label
    assert snapshot["taken_at"] is reading.taken_at
    assert snapshot["amount"] is reading.amount
    assert snapshot["tags"] == ["x"]
    assert snapshot["tags"] is not reading.tags

    reading.tags.append("y")
    assert snapshot["tags"] == ["x"]
