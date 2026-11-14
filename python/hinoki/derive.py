"""Schemas that take their fields from a model, declared with decorators.

    @derive.model(Post)
    @derive.only("title", "slug", "body")
    class PostCreate(Schema): ...

    @derive.model(Post)
    @derive.exclude("author_id")
    class PostOut(Schema):
        author_name: str

    @derive.model(Post)
    @derive.only("title", "body")
    @derive.partial()
    class PostPatch(Schema): ...

``model`` goes on top: it reads what the decorators below it chose and builds
the schema. Without ``only`` or ``exclude`` every column is taken. ``partial()``
makes every derived field optional (``UNSET`` when absent), ``partial("a", "b")``
only those. Fields declared in the class body win over derived ones. Mistakes
(unknown columns, ``only`` with ``exclude``, a selection without ``model``) fail
when the class is declared or first used.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from types import CellType, FunctionType
from typing import Any, cast

from hinoki.errors import RegistrationError
from hinoki.schema import Schema


def pending(cls: type[Schema]) -> dict[str, object]:
    found: Any = cls.__dict__.get("__pending__")
    if found is None:
        found = {}
        setattr(cls, "__pending__", found)  # noqa: B010 - not a declared attribute
    return cast(dict[str, object], found)


def only(*names: str) -> Callable[[type[Schema]], type[Schema]]:
    """Take exactly these model columns."""

    def apply(cls: type[Schema]) -> type[Schema]:
        pending(cls)["fields"] = names
        return cls

    return apply


def exclude(*names: str) -> Callable[[type[Schema]], type[Schema]]:
    """Take every model column except these."""

    def apply(cls: type[Schema]) -> type[Schema]:
        pending(cls)["exclude"] = names
        return cls

    return apply


def partial(*names: str) -> Callable[[type[Schema]], type[Schema]]:
    """Make the derived fields optional: all of them, or only the named ones."""

    def apply(cls: type[Schema]) -> type[Schema]:
        pending(cls)["optional"] = names or True
        return cls

    return apply


def model(source: type) -> Callable[[type[Schema]], type[Schema]]:
    """Derive the schema's fields from ``source``; the decorators below choose
    which columns and which of them are optional."""

    def apply(cls: type[Schema]) -> type[Schema]:
        chosen = cls.__dict__.get("__pending__", {})
        fields = chosen.get("fields")
        omitted = chosen.get("exclude", ())
        if fields is not None and omitted:
            raise RegistrationError(
                f"{cls.__name__}: use derive.only or derive.exclude, not both"
            )
        body = {
            **cls.__source__,
            "__derivation__": (
                source,
                fields,
                tuple(omitted),
                chosen.get("optional", False),
            ),
        }
        class_cells = {
            id(cell): cell
            for value in body.values()
            for cell in _class_cells_for(value, cls)
        }
        if len(class_cells) > 1:
            raise RegistrationError(
                f"{cls.__name__}: methods contain multiple __class__ cells"
            )
        if class_cells:
            body["__classcell__"] = next(iter(class_cells.values()))
        rebuilt: type[Schema] = type.__call__(type, cls.__name__, cls.__bases__, body)
        return rebuilt

    return apply


def _class_cells_for(value: object, cls: type[Schema]) -> tuple[CellType, ...]:
    """Find compiler-created ``__class__`` cells in class methods/descriptors."""
    members: tuple[object | None, ...]
    if isinstance(value, property):
        members = (value.fget, value.fset, value.fdel)
    elif isinstance(value, (classmethod, staticmethod)):
        members = (value.__func__,)
    elif isinstance(value, cached_property):
        members = (value.func,)
    elif isinstance(value, FunctionType):
        cells = value.__closure__ or ()
        names = value.__code__.co_freevars
        if "__class__" in names:
            cell = cells[names.index("__class__")]
            try:
                if cell.cell_contents is cls:
                    wrapped = getattr(value, "__wrapped__", None)
                    nested = (
                        _class_cells_for(wrapped, cls) if wrapped is not None else ()
                    )
                    return (cell, *nested)
            except ValueError:
                pass
        wrapped = getattr(value, "__wrapped__", None)
        return _class_cells_for(wrapped, cls) if wrapped is not None else ()
    else:
        return ()
    return tuple(
        cell
        for member in members
        if member is not None
        for cell in _class_cells_for(member, cls)
    )
