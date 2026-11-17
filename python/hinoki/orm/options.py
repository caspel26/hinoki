"""Model options as decorators, read like the action decorators.

    @db.table("saved_links")
    @unique("owner_id", "url")
    @db.indexed("created_at")
    @db.ordered_by("-created_at")
    class Bookmark(Model, label="Saved link"):
        ...

The decorators are also importable from ``hinoki.db`` (``db.table``,
``db.indexed``, ``db.ordered_by``). The table name is derived from the class
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

from collections.abc import Callable
from datetime import datetime
from typing import get_type_hints

from hinoki.errors import RegistrationError
from hinoki.orm.model import Model
from hinoki.utils import Clock, utc_now

MIN_UNIQUE = 2


def known(model: type[Model], columns: tuple[str, ...], option: str) -> None:
    unknown = [c for c in columns if c not in model.__columns__]
    if not columns or len(set(columns)) != len(columns) or unknown:
        raise RegistrationError(
            f"{model.__name__}: @{option}{columns} needs distinct columns of the "
            f"model (unknown: {unknown})"
        )


def unique[M: type[Model]](*columns: str) -> Callable[[M], M]:
    """A composite uniqueness constraint over two or more columns (single
    columns use ``field(unique=True)``)."""

    def apply(model: M) -> M:
        if len(columns) < MIN_UNIQUE:
            raise RegistrationError(
                f"{model.__name__}: @unique needs two or more columns; use "
                "field(unique=True) for one"
            )
        known(model, columns, "unique")
        current = () if model.replace_inherited("unique") else model.__unique__
        if columns in current:
            raise RegistrationError(f"{model.__name__}: @unique{columns} twice")
        model.__unique__ = (columns, *current)  # top-to-bottom order
        return model

    return apply


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
    and migrations. Declare their creation defaults with ``utc_now``.
    """

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
