"""The parameterized decorator for fields derived from an ORM model."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from functools import cached_property
from types import CellType, FunctionType

from hinoki.errors import RegistrationError
from hinoki.orm import Model
from hinoki.schema import Schema

Derivation = tuple[
    type, tuple[str, ...] | None, tuple[str, ...], bool | tuple[str, ...]
]


def derive(
    model: type[Model] | None = None,
    *,
    only: tuple[str, ...] | None = None,
    exclude: tuple[str, ...] | None = None,
    partial: bool | tuple[str, ...] = False,
) -> Callable[[type[Schema]], type[Schema]]:
    """Derive selected fields from ``model`` into a Schema subclass.

    The keyword-only form is retained for one compatibility cycle and is
    deprecated; it requires the legacy ``model=`` Schema class keyword.
    """
    _validate_options(only, exclude, partial)

    def apply(cls: type[Schema]) -> type[Schema]:
        source = model if model is not None else cls.__dict__.get("__derived_model__")
        if source is None:
            raise RegistrationError(
                f"{cls.__name__}: @derive requires a Model argument"
            )
        if model is None:
            warnings.warn(
                "@derive(...), with model= on the Schema class, is deprecated; "
                "pass the Model to @derive(Model, ...)",
                DeprecationWarning,
                stacklevel=2,
            )
        elif cls.__dict__.get("__derived_model__") is not None:
            raise RegistrationError(
                f"{cls.__name__}: pass the Model to @derive or to Schema, not both"
            )
        return rebuild(
            cls,
            (source, only, exclude or (), True if partial is True else partial),
        )

    return apply


def _validate_options(
    only: tuple[str, ...] | None,
    exclude: tuple[str, ...] | None,
    partial: bool | tuple[str, ...],
) -> None:
    if only is not None and not isinstance(only, tuple):
        raise RegistrationError("derive: only must be a tuple of field names")
    if exclude is not None and not isinstance(exclude, tuple):
        raise RegistrationError("derive: exclude must be a tuple of field names")
    names = (
        *(only if only is not None else ()),
        *(exclude if exclude is not None else ()),
    )
    if any(not isinstance(name, str) for name in names):
        raise RegistrationError("derive: field names must be strings")
    if only is not None and exclude is not None:
        raise RegistrationError("derive: only and exclude cannot be combined")
    if only == ():
        raise RegistrationError("derive: only=() selects no columns")
    if not isinstance(partial, (bool, tuple)):
        raise RegistrationError("derive: partial must be bool or a tuple of names")
    if isinstance(partial, tuple) and any(
        not isinstance(name, str) for name in partial
    ):
        raise RegistrationError("derive: field names must be strings")


def rebuild(cls: type[Schema], plan: Derivation) -> type[Schema]:
    """Recreate a schema from its source class, keeping compiler-created cells."""
    body = {**cls.__source__, "__derivation__": plan}
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
    rebuilt: type[Schema] = type.__call__(
        type,
        cls.__name__,
        cls.__bases__,
        body,
        config=cls.__schema_config__,
    )
    return rebuilt


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
        return _function_class_cells(value, cls)
    else:
        return ()
    return tuple(
        cell
        for member in members
        if member is not None
        for cell in _class_cells_for(member, cls)
    )


def _function_class_cells(
    value: FunctionType, cls: type[Schema]
) -> tuple[CellType, ...]:
    cell = _class_cell(value, cls)
    wrapped = getattr(value, "__wrapped__", None)
    nested = _class_cells_for(wrapped, cls) if wrapped is not None else ()
    return (cell, *nested) if cell is not None else nested


def _class_cell(value: FunctionType, cls: type[Schema]) -> CellType | None:
    names = value.__code__.co_freevars
    if "__class__" not in names:
        return None
    cell = (value.__closure__ or ())[names.index("__class__")]
    try:
        return cell if cell.cell_contents is cls else None
    except ValueError:
        return None
