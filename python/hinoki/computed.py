"""Computed fields: read-only values a ``Schema`` adds to its output."""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, overload

from hinoki.errors import RegistrationError
from hinoki.fieldtypes import FieldType, parse_annotation

COMPUTED_MARK = "__hinoki_computed_field__"
COMPUTED_TABLE = "__hinoki_computed__"
"""The class attribute Rust reads: ``((name, wire_name), ...)``."""


@dataclass(frozen=True, slots=True)
class ComputedSpec:
    """One computed field: the property it reads and how it is documented."""

    name: str
    alias: str | None
    function: Callable[..., Any]
    description: str | None = None
    examples: tuple[Any, ...] | None = None

    @property
    def wire_name(self) -> str:
        return self.alias or self.name

    def field_type(self, owner: type) -> FieldType:
        """The type of the value, from the return annotation of the method."""
        where = f"{owner.__name__}.{self.name}"
        try:
            hints = typing.get_type_hints(self.function, include_extras=True)
        except NameError as exc:
            raise RegistrationError(f"{where}: {exc}") from exc
        return parse_annotation(hints["return"], where, input_bounds=False)

    def guide_spec(self, owner: type) -> tuple[str, Any] | None:
        """What the return annotation declares about nested Schemas, as the spec
        of ``core._ReturnGuide`` (``None`` when it declares none): a method that
        returns a subclass instance is written as the declared class.
        ``RegistrationError`` for a name that does not resolve: the value would
        be written whole."""
        from hinoki.runtime.binding import guide_spec  # noqa: PLC0415 - runtime imports schema

        where = f"{owner.__name__}.{self.name}"
        try:
            hints = typing.get_type_hints(self.function)
        except NameError as exc:
            raise RegistrationError(f"{where}: {exc}") from exc
        return guide_spec(hints["return"])


class ComputedMark:
    """What ``@computed_field`` records on the method it decorates."""

    __slots__ = ("alias", "description", "examples")

    def __init__(
        self,
        alias: str | None,
        description: str | None,
        examples: Sequence[Any] | None,
    ) -> None:
        self.alias = alias
        self.description = description
        self.examples = None if examples is None else tuple(examples)


@overload
def computed_field[T](function: Callable[[Any], T], /) -> T: ...
@overload
def computed_field[T](
    *,
    alias: str | None = None,
    description: str | None = None,
    examples: Sequence[Any] | None = None,
) -> Callable[[Callable[[Any], T]], T]: ...
def computed_field(
    function: Callable[..., Any] | None = None,
    /,
    *,
    alias: str | None = None,
    description: str | None = None,
    examples: Sequence[Any] | None = None,
) -> Any:
    """Declare a read-only value derived from the other fields of a ``Schema``.

    ``@computed_field def full_name(self) -> str`` reads like an attribute, is
    written by ``dump()`` and by JSON responses after the declared fields, and
    is documented as a ``readOnly`` property. It is never accepted as input and
    never stored. The return annotation gives its OpenAPI type; the method must
    be synchronous, take no arguments and not do I/O."""

    def mark(method: Callable[..., Any]) -> property:
        setattr(method, COMPUTED_MARK, ComputedMark(alias, description, examples))
        return property(method)

    return mark if function is None else mark(function)


def own_computed(cls: type) -> list[ComputedSpec]:
    """The computed fields declared in the body of ``cls`` itself."""
    found = []
    for name, value in cls.__dict__.items():
        mark = getattr(getattr(value, "fget", None), COMPUTED_MARK, None)
        if isinstance(value, property) and isinstance(mark, ComputedMark):
            found.append(check_method(cls, name, value.fget, mark))
    return found


def check_method(
    cls: type, name: str, function: Callable[..., Any] | None, mark: ComputedMark
) -> ComputedSpec:
    where = f"{cls.__name__}.{name}"
    if function is None or "return" not in getattr(function, "__annotations__", {}):
        raise RegistrationError(f"{where}: a computed field needs a return annotation")
    if len(inspect.signature(function).parameters) != 1:
        raise RegistrationError(f"{where}: a computed field takes no arguments")
    for label, text in (("alias", mark.alias), ("description", mark.description)):
        if text is not None and not (isinstance(text, str) and text.strip()):
            raise RegistrationError(f"{where}: {label} must be non-empty text")
    if mark.examples is not None and not mark.examples:
        raise RegistrationError(f"{where}: examples must not be empty")
    return ComputedSpec(name, mark.alias, function, mark.description, mark.examples)


def collect_computed(
    cls: type, inherited: Sequence[ComputedSpec], field_names: set[str]
) -> dict[str, ComputedSpec]:
    """Inherited then own computed fields, in order; none may be a field."""
    specs = {spec.name: spec for spec in inherited}
    for spec in own_computed(cls):
        specs[spec.name] = spec
    for name in specs:
        if name in field_names:
            raise RegistrationError(
                f"{cls.__name__}.{name}: already a field; rename one of them"
            )
    return specs


def check_wire_names(
    cls: type, specs: dict[str, ComputedSpec], taken: set[str]
) -> None:
    """A computed field may not reuse the wire name of a field or of another one."""
    for spec in specs.values():
        if spec.wire_name in taken:
            raise RegistrationError(
                f"{cls.__name__}.{spec.name}: wire name {spec.wire_name!r} is "
                "already used by another field"
            )
        taken.add(spec.wire_name)
