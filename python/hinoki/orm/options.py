"""Model options as decorators, read like the action decorators.

    @db.table("saved_links")
    @db.unique("owner_id", "url")
    @db.indexed("created_at")
    @db.ordered_by("-created_at")
    @db.timestamps()
    class Bookmark(Model, label="Saved link"):
        ...

The decorators are also importable from ``hinoki.db`` (``db.table``,
``db.unique``, ``db.indexed``, ``db.ordered_by``, ``db.timestamps``), which is
the documented spelling. The table name is derived from the class
name unless ``@table`` or the ``table=`` class keyword names it; ``label`` and
``plural`` are class keywords. Constraints and behaviour are decorators that
validate their column names when applied and return the same class. Apply them
before the model is used: they change what the migration planner, the query
layer and the admin see.

A model inherits the options of its abstract bases. A decorator applied to the
subclass replaces what was inherited for that kind of option; it does not add
to it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import get_type_hints

from hinoki.errors import RegistrationError
from hinoki.orm.model import Model, StatusTrack
from hinoki.utils import Clock, utc_now

MIN_UNIQUE = 2
MAX_CONSTRAINT_NAME = 63  # PostgreSQL truncates longer identifiers
CONSTRAINT_NAME = re.compile(rf"[A-Za-z_][A-Za-z0-9_]{{0,{MAX_CONSTRAINT_NAME - 1}}}")


def known(model: type[Model], columns: tuple[str, ...], option: str) -> None:
    unknown = [c for c in columns if c not in model.__columns__]
    if not columns or len(set(columns)) != len(columns) or unknown:
        raise RegistrationError(
            f"{model.__name__}: @{option}{columns} needs distinct columns of the "
            f"model (unknown: {unknown})"
        )


def unique[M: type[Model]](*columns: str, name: str | None = None) -> Callable[[M], M]:
    """A composite uniqueness constraint over two or more columns (single
    columns use ``field(unique=True)``). ``name`` names the constraint in the
    database; without it the backend chooses one."""

    if name is not None and not is_constraint_name(name):
        raise RegistrationError(
            f"@unique{columns}: constraint name {name!r} must be an identifier "
            f"of at most {MAX_CONSTRAINT_NAME} letters, digits or underscores"
        )

    def apply(model: M) -> M:
        if len(columns) < MIN_UNIQUE:
            raise RegistrationError(
                f"{model.__name__}: @unique needs two or more columns; use "
                "field(unique=True) for one"
            )
        known(model, columns, "unique")
        replaced = model.replace_inherited("unique")
        current = () if replaced else model.__unique__
        if columns in current:
            raise RegistrationError(f"{model.__name__}: @unique{columns} twice")
        names = {} if replaced else dict(model.__unique_names__)
        if name is not None:
            names[columns] = checked_name(model, names, name)
        model.__unique__ = (columns, *current)  # top-to-bottom order
        model.__unique_names__ = names
        return model

    return apply


def checked_name(
    model: type[Model], names: dict[tuple[str, ...], str], name: str
) -> str:
    """A constraint name the model may declare. Names compare without case,
    as MySQL and MariaDB do. An abstract model may not name one: every table
    inheriting it would repeat the name."""
    if model.__abstract__:
        raise RegistrationError(
            f"{model.__name__}: an abstract model cannot name a unique constraint "
            f"({name!r}); name it on each table model instead"
        )
    taken = next((n for n in names.values() if n.casefold() == name.casefold()), None)
    if taken is not None:
        raise RegistrationError(f"{model.__name__}: constraint name {taken!r} twice")
    return name


def is_constraint_name(name: object) -> bool:
    """An identifier every backend accepts without truncating it."""
    return type(name) is str and bool(CONSTRAINT_NAME.fullmatch(name))


def table[M: type[Model]](name: str) -> Callable[[M], M]:
    """Name the model's table; without it the name is derived from the class
    name. ``@table("saved_links")`` is the same as ``table="saved_links"`` in
    the class header; giving both with different names is an error."""

    if type(name) is not str or not name:
        raise RegistrationError("@table needs a non-empty table name")

    def apply(model: M) -> M:
        if model.__abstract__:
            raise RegistrationError(
                f"{model.__name__}: an abstract model has no table; remove "
                "@table or abstract=True"
            )
        if not model.__table_is_implicit__ and model.__table__ != name:
            earlier = (
                f"@table({model.__table__!r})"
                if model.__table_decorated__
                else f"table={model.__table__!r}"
            )
            raise RegistrationError(
                f"{model.__name__}: @table({name!r}) conflicts with {earlier}; "
                "name the table once"
            )
        model.__table__ = name
        model.__table_is_implicit__ = False
        model.__table_decorated__ = True
        model.__table_problem__ = ""  # an explicit name replaces the derived one
        # Imported here: links.py imports the model module this one serves.
        from hinoki.orm.links import retable  # noqa: PLC0415 - circular, see above

        retable(model)
        return model

    return apply


def indexed[M: type[Model]](*columns: str) -> Callable[[M], M]:
    """A (non-unique) index over one or more columns, created by the migration
    planner. A single column can use ``field(index=True)`` instead; naming a
    column both ways creates one index."""

    def apply(model: M) -> M:
        known(model, columns, "indexed")
        current = model.__indexes__
        if model.replace_inherited("indexes"):
            current = model.__field_indexes__
        if columns in model.__field_indexes__:
            if columns in model.__decorated_indexes__:
                raise RegistrationError(f"{model.__name__}: @indexed{columns} twice")
            model.__decorated_indexes__ = (*model.__decorated_indexes__, columns)
            model.__indexes__ = current
            return model  # field(index=True) already declares this index
        if columns in current:
            raise RegistrationError(f"{model.__name__}: @indexed{columns} twice")
        model.__indexes__ = (columns, *current)  # top-to-bottom order
        return model

    return apply


def ordered_by[M: type[Model]](*names: str) -> Callable[[M], M]:
    """The default order of every query that does not call ``order_by``;
    ``"-created_at"`` is descending."""

    if not names:
        raise RegistrationError("@ordered_by needs a column")

    def apply(model: M) -> M:
        known(model, tuple(n.removeprefix("-") for n in names), "ordered_by")
        model.replace_inherited("ordering")
        model.__ordering__ = tuple(names)
        return model

    return apply


def timestamps[M: type[Model]](
    updated_at: str = "updated_at", *, clock: Clock = utc_now
) -> Callable[[M], M]:
    """Update a declared datetime column on each ORM update statement.

    Timestamp columns remain ordinary model fields, visible to type checkers
    and migrations. Declare their creation defaults with ``utc_now``. The
    decorator is always called: ``@db.timestamps()``.
    """

    if type(updated_at) is not str:
        raise RegistrationError(
            "timestamps is called with the column name: write @db.timestamps() "
            "or @db.timestamps('modified_at')"
        )

    def apply(model: M) -> M:
        known(model, (updated_at,), "timestamps")
        if updated_at == model.__primary_key__:
            raise RegistrationError(
                f"{model.__name__}: timestamps field cannot be the primary key"
            )
        if not callable(clock):
            raise RegistrationError(
                f"{model.__name__}: timestamps clock must be callable"
            )
        if updated_at == model.__soft_delete__ or any(
            track.changed_at == updated_at for track in model.__status_tracks__
        ):
            raise RegistrationError(
                f"{model.__name__}: timestamps field {updated_at!r} conflicts "
                "with another managed datetime column"
            )
        try:
            annotation = get_type_hints(model).get(updated_at)
        except (NameError, TypeError) as exc:
            raise RegistrationError(
                f"{model.__name__}: cannot resolve timestamp field types"
            ) from exc
        if annotation is not datetime:
            raise RegistrationError(
                f"{model.__name__}: timestamps field {updated_at!r} must be datetime"
            )
        model.replace_inherited("timestamps")
        model.__updated_at__ = updated_at
        model.__clock__ = clock
        return model

    return apply


def soft_delete[M: type[Model]](
    column: str = "deleted_at", *, clock: Clock | None = None
) -> Callable[[M], M]:
    """Mark a nullable datetime column as the model's soft-delete marker."""
    if type(column) is not str:
        raise RegistrationError("@soft_delete column must be a string")

    def apply(model: M) -> M:
        known(model, (column,), "soft_delete")
        if column == model.__primary_key__:
            raise RegistrationError(
                f"{model.__name__}: soft-delete column cannot be the primary key"
            )
        if column == model.__updated_at__ or any(
            track.changed_at == column for track in model.__status_tracks__
        ):
            raise RegistrationError(
                f"{model.__name__}: soft-delete column {column!r} conflicts with "
                "another managed datetime column"
            )
        if (
            model.__field_types__[column].kind != "datetime"
            or not model.__field_types__[column].nullable
        ):
            raise RegistrationError(
                f"{model.__name__}: soft-delete column {column!r} must be "
                "datetime | None"
            )
        if clock is not None and not callable(clock):
            raise RegistrationError(
                f"{model.__name__}: soft-delete clock must be callable"
            )
        model.replace_inherited("soft_delete")
        model.__soft_delete__ = column
        model.__soft_delete_clock__ = clock
        return model

    return apply


def track_status[M: type[Model]](
    field: str = "status",
    *,
    changed_at: str = "status_changed_at",
    clock: Clock | None = None,
) -> Callable[[M], M]:
    """Record when a declared status column changes; decorators may be stacked."""
    if type(field) is not str or type(changed_at) is not str:
        raise RegistrationError("@track_status field and changed_at must be strings")

    def apply(model: M) -> M:
        known(model, (field, changed_at), "track_status")
        current = (
            ()
            if "status_tracks" in model.__inherited_options__
            else model.__status_tracks__
        )
        if field == changed_at:
            raise RegistrationError(
                f"{model.__name__}: track_status field and changed_at must differ"
            )
        conflicts = {model.__primary_key__, model.__updated_at__, model.__soft_delete__}
        if changed_at in conflicts or any(
            track.changed_at == changed_at or track.field == field for track in current
        ):
            raise RegistrationError(
                f"{model.__name__}: duplicate or conflicting status track"
            )
        if model.__field_types__[changed_at].kind != "datetime":
            raise RegistrationError(
                f"{model.__name__}: tracked timestamp {changed_at!r} must be datetime"
            )
        if clock is not None and not callable(clock):
            raise RegistrationError(f"{model.__name__}: status clock must be callable")
        model.replace_inherited("status_tracks")
        model.__status_tracks__ = (*current, StatusTrack(field, changed_at, clock))
        return model

    return apply
