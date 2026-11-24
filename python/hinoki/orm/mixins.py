"""Composable abstract models for common lifecycle columns."""

from __future__ import annotations

from datetime import datetime

from hinoki.orm.model import Model, field
from hinoki.orm.options import soft_delete, timestamps
from hinoki.utils import utc_now


@timestamps()
class TimestampedModel(Model, abstract=True):
    """Abstract base with ordinary ``created_at`` and ``updated_at`` columns."""

    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@soft_delete()
class SoftDeleteModel(Model, abstract=True):
    """Abstract base with an indexed nullable ``deleted_at`` column."""

    deleted_at: datetime | None = field(default=None, index=True)
