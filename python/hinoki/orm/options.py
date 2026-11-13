"""Model options as decorators, read like the action decorators.

    @unique("owner_id", "url")
    @indexed("created_at")
    @ordered_by("-created_at")
    class Bookmark(Model, table="bookmarks", label="Bookmark", plural="Bookmarks"):
        ...

``table`` (and the optional ``label``/``plural``) are class keywords because
they are needed when the class is created; constraints and behaviour are
decorators that validate their column names when applied and return the same
class. Apply them before the model is used: they change what the migration
planner, the query layer and the admin see.
"""

from __future__ import annotations

from collections.abc import Callable

from hinoki.errors import RegistrationError
from hinoki.orm.model import Model

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
        if columns in model.__unique__:
            raise RegistrationError(f"{model.__name__}: @unique{columns} twice")
        model.__unique__ = (columns, *model.__unique__)  # top-to-bottom order
        return model

    return apply


def indexed[M: type[Model]](*columns: str) -> Callable[[M], M]:
    """A (non-unique) index over one or more columns, created by the migration
    planner."""

    def apply(model: M) -> M:
        known(model, columns, "indexed")
        if columns in model.__indexes__:
            raise RegistrationError(f"{model.__name__}: @indexed{columns} twice")
        model.__indexes__ = (columns, *model.__indexes__)  # top-to-bottom order
        return model

    return apply


def ordered_by[M: type[Model]](*names: str) -> Callable[[M], M]:
    """The default order of every query that does not call ``order_by``;
    ``"-created_at"`` is descending."""

    if not names:
        raise RegistrationError("@ordered_by needs a column")

    def apply(model: M) -> M:
        known(model, tuple(n.removeprefix("-") for n in names), "ordered_by")
        model.__ordering__ = tuple(names)
        return model

    return apply
