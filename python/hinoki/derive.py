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
        rebuilt: type[Schema] = type.__call__(type, cls.__name__, cls.__bases__, body)
        return rebuilt

    return apply
