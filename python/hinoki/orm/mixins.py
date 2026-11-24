"""Composable abstract models for common lifecycle columns."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from hinoki.orm.model import Model, field, uuid_pk
from hinoki.orm.options import soft_delete, timestamps
from hinoki.utils import utc_now


class UUIDModel(Model, abstract=True):
    """Abstract base with a UUIDv4 primary key column ``id``.

    Override ``id: UUID = uuid_pk(version=7)`` (or pass a custom
    ``default_factory``) on a subclass or project base model to use another
    UUID generator.
    """

    id: UUID = uuid_pk()


@timestamps()
class TimestampedModel(Model, abstract=True):
    """Abstract base with ordinary ``created_at`` and ``updated_at`` columns."""

    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@soft_delete()
class SoftDeleteModel(Model, abstract=True):
    """Abstract base with an indexed nullable ``deleted_at`` column."""

    deleted_at: datetime | None = field(default=None, index=True)
