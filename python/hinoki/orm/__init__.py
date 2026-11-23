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

from hinoki.orm.context import current_database, database_var, use_database
from hinoki.orm.links import LinkChange, LinkKey, LinkSet, ManyToMany, many_to_many
from hinoki.orm.model import (
    MARK,
    SNAPSHOT,
    Change,
    FieldMeta,
    Model,
    field,
    foreign_key,
    pk,
)
from hinoki.orm.options import indexed, ordered_by, table, timestamps, unique
from hinoki.orm.queryset import (
    IN_CHUNK,
    OPERATORS,
    SEPARATOR,
    Loading,
    QuerySet,
    condition,
    fetch_in,
)
from hinoki.orm.relations import OnDelete, Related, Relation

__all__ = [
    "IN_CHUNK",
    "MARK",
    "OPERATORS",
    "SEPARATOR",
    "SNAPSHOT",
    "Change",
    "FieldMeta",
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
    "condition",
    "current_database",
    "database_var",
    "fetch_in",
    "field",
    "foreign_key",
    "indexed",
    "many_to_many",
    "ordered_by",
    "pk",
    "table",
    "timestamps",
    "unique",
    "use_database",
]
