"""Active Record models over the Rust query compiler.

Single-table models with equality/comparison/membership filters, ordering,
counting and pagination, plus create/get/update/delete, and foreign keys that
are loaded only on request (``select_related`` joins, ``prefetch_related``
runs one extra query). SQL is produced by the native compiler from validated
field names; values are always bound parameters.

The package: ``model`` (``Model``, ``field``, ``foreign_key``), ``queryset``
(``QuerySet``), ``relations`` (``Related``), ``context`` (the active
database), ``migrations`` and ``migrator`` (planning and applying SQL).
Everything most code needs is importable from ``hinoki.orm`` itself.
"""

from hinoki.orm.context import current_database, database_var, use_database
from hinoki.orm.model import MARK, SNAPSHOT, FieldMeta, Model, field, foreign_key
from hinoki.orm.queryset import (
    IN_CHUNK,
    OPERATORS,
    SEPARATOR,
    Loading,
    QuerySet,
    condition,
    fetch_in,
)
from hinoki.orm.relations import Related, Relation

__all__ = [
    "IN_CHUNK",
    "MARK",
    "OPERATORS",
    "SEPARATOR",
    "SNAPSHOT",
    "FieldMeta",
    "Loading",
    "Model",
    "QuerySet",
    "Related",
    "Relation",
    "condition",
    "current_database",
    "database_var",
    "fetch_in",
    "field",
    "foreign_key",
    "use_database",
]
