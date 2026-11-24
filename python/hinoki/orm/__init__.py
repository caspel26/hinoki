"""Active Record models over the Rust query compiler.

Single-table models with equality/comparison/membership filters, ordering,
counting and pagination, plus create/get/update/delete, and foreign keys that
are loaded only on request (``select_related`` joins, ``prefetch_related``
runs one extra query). SQL is produced by the native compiler from validated
field names; values are always bound parameters.

The package: ``model`` (``Model``, ``field``, ``pk``, ``foreign_key``),
``options`` (``table``, ``unique``, ``indexed``, ``ordered_by``,
``timestamps``), ``queryset`` (``QuerySet``), ``relations`` (``Related``),
``context`` (the active database), ``migrations`` and ``migrator`` (planning
and applying SQL).
Everything most code needs is importable from ``hinoki.orm`` itself.
"""

from hinoki.orm.context import current_database, use_database
from hinoki.orm.links import LinkChange, LinkKey, LinkSet, ManyToMany, many_to_many
from hinoki.orm.mixins import SoftDeleteModel, TimestampedModel, UUIDModel
from hinoki.orm.model import (
    Change,
    Model,
    StatusTrack,
    field,
    foreign_key,
    pk,
    urlsafe_token,
    uuid_pk,
)
from hinoki.orm.options import (
    indexed,
    ordered_by,
    soft_delete,
    table,
    timestamps,
    track_status,
    unique,
)
from hinoki.orm.queryset import (
    Loading,
    QuerySet,
)
from hinoki.orm.relations import OnDelete, Related, Relation
from hinoki.utils import uuid7

__all__ = [
    "Change",
    "LinkChange",
    "LinkKey",
    "LinkSet",
    "Loading",
    "ManyToMany",
    "Model",
    "OnDelete",
    "QuerySet",
    "Related",
    "Relation",
    "SoftDeleteModel",
    "StatusTrack",
    "TimestampedModel",
    "UUIDModel",
    "current_database",
    "field",
    "foreign_key",
    "indexed",
    "many_to_many",
    "ordered_by",
    "pk",
    "soft_delete",
    "table",
    "timestamps",
    "track_status",
    "unique",
    "urlsafe_token",
    "use_database",
    "uuid7",
    "uuid_pk",
]
