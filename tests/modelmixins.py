"""Mixins and abstract models declared away from the models that use them."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal
from typing import ClassVar
from uuid import UUID, uuid4

from hinoki.orm import Model, field, indexed, ordered_by, timestamps
from hinoki.utils import utc_now


class Stamped:
    """A plain mixin: annotations only, one with ``field``."""

    registry: ClassVar[list[str]] = []
    created_at: datetime = field(default_factory=utc_now)
    note: str = ""

    def describe(self) -> str:
        return f"{self.note} @ {self.created_at:%Y}"


class Priced:
    """Its annotation names a type that only this module imports."""

    price: Decimal = Decimal(0)


@dataclasses.dataclass(kw_only=True)
class Ranked:
    rank: int = 0


class Keyed(Model, abstract=True):
    id: UUID = field(primary_key=True, default_factory=uuid4)


@timestamps()
@indexed("updated_at")
@ordered_by("-updated_at")
class Tracked(Model, abstract=True):
    updated_at: datetime = field(default_factory=utc_now)
