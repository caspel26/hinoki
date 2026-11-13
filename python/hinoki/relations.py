"""Relation metadata and the descriptor that reads loaded relations.

A relation is never fetched by attribute access: the descriptor returns what
``select_related`` / ``prefetch_related`` stored on the instance and raises
``RelationNotLoaded`` otherwise.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Self, overload

from hinoki.errors import RelationNotLoaded

if TYPE_CHECKING:
    from hinoki.orm import Model

ON_DELETE = {
    "restrict": "RESTRICT",
    "cascade": "CASCADE",
    "set_null": "SET NULL",
    "no_action": "NO ACTION",
}
ALIAS_PREFIX = "rel_"
STORE = "hinoki_related"


@dataclasses.dataclass(frozen=True, slots=True)
class Relation:
    """One side of a foreign key.

    Forward (``forward=True``): ``model`` is the referenced model and ``column``
    the local foreign-key column. Reverse: ``model`` is the model that holds
    ``column``, pointing back at the owner.
    """

    name: str
    model: type[Model]
    column: str
    forward: bool


class Related[T]:
    """Class attribute that serves a loaded relation from the instance.

    The framework installs it for every relation. To have the type checker
    know the attribute, declare it on the model without a value::

        writer: ClassVar[Related[Writer]]
        articles: ClassVar[Related[list[Article]]]
    """

    def __init__(self, name: str, forward: bool) -> None:
        self.name = name
        self.forward = forward

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Self: ...

    @overload
    def __get__(self, instance: object, owner: type | None = None) -> T: ...

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        loaded = instance.__dict__.get(STORE, {})
        if self.name not in loaded:
            how = "select_related" if self.forward else "prefetch_related"
            raise RelationNotLoaded(
                f"{type(instance).__name__}.{self.name} was not loaded: "
                f"query with .{how}({self.name!r})"
            )
        return loaded[self.name]


def store(instance: Any, name: str, value: Any) -> None:
    instance.__dict__.setdefault(STORE, {})[name] = value
