"""Typed client filters, ordering and search for a generated CRUD list.

A ``FilterSet`` declares, once, which query parameters a list accepts and how
each one is read::

    class InvoiceFilters(FilterSet):
        paid: Eq[bool]
        status: In[Literal["open", "paid", "overdue"]]
        issued: Range[date]
        search = Search("reference", "customer_name")
        ordering = ("issued", "reference")

Only what is declared is accepted, each name only in the operator its
declaration gives it. Checking, coercion to the declared type and the
conditions themselves are built in Rust (``core._FilterPlan``); this module
only declares. Values reach the database as bound parameters and column names
come from the declaration, never from the request.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import types
import typing
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar, Literal, Self

from hinoki import core
from hinoki.errors import ErrorDetail, RegistrationError, ValidationError
from hinoki.orm.keyset import KEY_KINDS

if TYPE_CHECKING:
    from hinoki.orm.model import Model
    from hinoki.orm.queryset import QuerySet

MAX_IN_ITEMS = 50
MAX_TEXT_CHARS = 256
MAX_SEARCH_CHARS = 128
KINDS: dict[object, str] = {
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
    uuid.UUID: "uuid",
    datetime.date: "date",
    datetime.datetime: "datetime",
    decimal.Decimal: "decimal",
}
# Column kinds a client may order by (and range over).
ORDERABLE = frozenset(KINDS.values())


@dataclasses.dataclass(frozen=True, slots=True)
class Bounds[T]:
    """Both ends, inclusive, of a ``Range`` filter; a missing end is ``None``."""

    from_: T | None = None
    to: T | None = None


type Eq[T] = T | None
"""``?name=value``; the value is ``None`` when the client did not send it."""
type In[T] = list[T] | None
"""``?name=a,b``: a comma-separated list, at most ``MAX_IN_ITEMS`` values."""
type Range[T] = Bounds[T] | None
"""``?name.from=...&name.to=...``, either end optional and inclusive."""

SHAPES: dict[object, str] = {Eq: "eq", In: "in", Range: "range"}


@dataclasses.dataclass(frozen=True, slots=True)
class Search:
    """``?search=text``: a case-insensitive "contains" over these text columns,
    any of them matching."""

    columns: tuple[str, ...]

    def __init__(self, *columns: str) -> None:
        object.__setattr__(self, "columns", columns)


@dataclasses.dataclass(frozen=True, slots=True)
class FilterField:
    """One declared filter, as the plan and the OpenAPI document read it."""

    name: str
    kind: str
    shape: str
    nullable: bool
    choices: tuple[str, ...] = ()


def unwrap_nullable(argument: object) -> tuple[object, bool]:
    """``T | None`` -> ``(T, True)``; ``T`` -> ``(T, False)``."""
    if typing.get_origin(argument) not in {typing.Union, types.UnionType}:
        return argument, False
    members = [m for m in typing.get_args(argument) if m is not type(None)]
    if len(members) != 1:
        return argument, False
    return members[0], True


def kind_of(where: str, argument: object) -> tuple[str, tuple[str, ...]]:
    if typing.get_origin(argument) is Literal:
        choices = typing.get_args(argument)
        if choices and all(type(choice) is str for choice in choices):
            return "str", tuple(choices)
        raise RegistrationError(f"{where}: Literal choices must all be str")
    kind = KINDS.get(argument)
    if kind is None:
        raise RegistrationError(
            f"{where}: {argument!r} is not a filter type; use bool, int, float, "
            "str, a Literal of str, UUID, date, datetime or Decimal"
        )
    return kind, ()


def declared_field(owner: str, name: str, annotation: object) -> FilterField:
    where = f"{owner}.{name}"
    shape = SHAPES.get(typing.get_origin(annotation))
    if shape is None:
        raise RegistrationError(
            f"{where}: a filter is declared as Eq[T], In[T] or Range[T]"
        )
    (argument,) = typing.get_args(annotation)
    inner, nullable = unwrap_nullable(argument)
    kind, choices = kind_of(where, inner)
    if shape == "range" and kind in {"bool", "uuid"}:
        raise RegistrationError(f"{where}: a {kind} has no range; use Eq or In")
    if shape == "range" and choices:
        raise RegistrationError(f"{where}: a Literal has no range; use Eq or In")
    return FilterField(name, kind, shape, nullable, choices)


# Attributes of an instance; a filter cannot take one of these names.
INSTANCE_NAMES = frozenset(
    {"conditions", "order", "search_term", "scope", "arranged", "nulls"}
)


def is_class_variable(annotation: object) -> bool:
    return annotation is ClassVar or typing.get_origin(annotation) is ClassVar


class FilterSet:
    """Base class of a list's declared filters. Subclass it and annotate each
    filter; use it on its own with ``InvoiceFilters.parse(query)``.

    Instances hold what the client sent: ``params.paid`` is ``False`` or
    ``None`` (not sent), ``params.status`` a list or ``None``, ``params.issued``
    a ``Bounds`` or ``None``, ``params.search_term`` the text or ``None`` and
    ``params.order`` the client's ordering as column names, ``-`` for
    descending. ``params.is_null("closed")`` answers a ``closed.null`` filter,
    declared by a ``T | None`` type argument.
    """

    search: ClassVar[Search | None] = None
    ordering: ClassVar[tuple[str, ...]] = ()
    fields: ClassVar[tuple[FilterField, ...]] = ()
    plan: ClassVar[core._FilterPlan]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        found = cls.collect()
        search = cls.search.columns if isinstance(cls.search, Search) else ()
        if not (found or search or cls.ordering):
            raise RegistrationError(f"{cls.__name__} declares no filter")
        cls.fields = found
        cls.plan = cls.build(found, search)

    @classmethod
    def collect(cls) -> tuple[FilterField, ...]:
        try:
            hints = typing.get_type_hints(cls, include_extras=True)
        except NameError as exc:
            raise RegistrationError(f"{cls.__name__}: {exc}") from exc
        found = []
        for name, annotation in hints.items():
            if name.startswith("_") or is_class_variable(annotation):
                continue
            if hasattr(FilterSet, name) or name in INSTANCE_NAMES:
                raise RegistrationError(
                    f"{cls.__name__}.{name} is a name FilterSet uses; rename the filter"
                )
            found.append(declared_field(cls.__name__, name, annotation))
        return tuple(found)

    @classmethod
    def build(
        cls, found: tuple[FilterField, ...], search: tuple[str, ...]
    ) -> core._FilterPlan:
        if cls.search is not None and not isinstance(cls.search, Search):
            raise RegistrationError(f"{cls.__name__}.search must be Search(...)")
        if cls.search is not None and not search:
            raise RegistrationError(f"{cls.__name__}.search names no column")
        if not isinstance(cls.ordering, tuple) or not all(
            isinstance(name, str) for name in cls.ordering
        ):
            raise RegistrationError(f"{cls.__name__}.ordering must be a tuple of names")
        declared = [
            (f.name, f.kind, f.shape, f.nullable, list(f.choices)) for f in found
        ]
        try:
            return core._FilterPlan(
                declared,
                list(search),
                list(cls.ordering),
                MAX_IN_ITEMS,
                MAX_TEXT_CHARS,
                MAX_SEARCH_CHARS,
            )
        except ValueError as exc:
            raise RegistrationError(f"{cls.__name__}: {exc}") from exc

    @classmethod
    def check(
        cls, model: type[Model], *, cursor: bool = False, reserved: Iterable[str] = ()
    ) -> None:
        """Fail at declaration when a filter does not match the model's
        columns. ``cursor`` also requires ordering columns a cursor can carry.
        ``reserved`` is the pagination parameter names this FilterSet must not
        shadow."""
        types = model.__field_types__
        for item in cls.fields:
            cls.check_column(model, item.name, item.kind)
            if item.nullable and not types[item.name].nullable:
                raise RegistrationError(
                    f"{cls.__name__}.{item.name}: {model.__name__}.{item.name} is "
                    "not nullable, so it has no .null filter; drop '| None'"
                )
        for column in cls.search.columns if cls.search else ():
            cls.check_column(model, column, "str")
        for column in cls.ordering:
            cls.check_orderable(model, column, cursor=cursor)
        cls.check_reserved(reserved)

    @classmethod
    def produced_names(cls) -> frozenset[str]:
        """Every query parameter name this FilterSet reads: each field's own
        name and its ``.from``/``.to``/``.null`` suffixes, plus ``search`` and
        ``ordering`` when declared."""
        names: set[str] = set()
        for item in cls.fields:
            names.add(item.name)
            if item.shape == "range":
                names.add(f"{item.name}.from")
                names.add(f"{item.name}.to")
            if item.nullable:
                names.add(f"{item.name}.null")
        if cls.search is not None:
            names.add("search")
        if cls.ordering:
            names.add("ordering")
        return frozenset(names)

    @classmethod
    def check_reserved(cls, reserved: Iterable[str]) -> None:
        """A produced name that collides with a pagination parameter (``page``,
        ``page_size``, ``cursor`` or ``limit``) cannot be told apart from it."""
        collisions = cls.produced_names() & set(reserved)
        if collisions:
            name = min(collisions)
            raise RegistrationError(
                f"{cls.__name__}.{name} collides with a pagination parameter; "
                "rename the filter, search or ordering"
            )

    @classmethod
    def check_column(cls, model: type[Model], column: str, kind: str) -> None:
        if column not in model.__columns__:
            raise RegistrationError(
                f"{cls.__name__}: {model.__name__} has no column {column!r}"
            )
        actual = model.__field_types__[column].kind
        if actual != kind:
            raise RegistrationError(
                f"{cls.__name__}.{column} is a {kind} filter but "
                f"{model.__name__}.{column} is a {actual} column"
            )

    @classmethod
    def check_orderable(cls, model: type[Model], column: str, *, cursor: bool) -> None:
        if column not in model.__columns__:
            raise RegistrationError(
                f"{cls.__name__}.ordering: {model.__name__} has no column {column!r}"
            )
        field_type = model.__field_types__[column]
        if field_type.kind not in ORDERABLE:
            raise RegistrationError(
                f"{cls.__name__}.ordering: {column!r} is a {field_type.kind} column "
                "and cannot be ordered"
            )
        if cursor:
            cls.check_cursor_key(model, column)

    @classmethod
    def check_cursor_key(cls, model: type[Model], column: str) -> None:
        field_type = model.__field_types__[column]
        if field_type.nullable or field_type.kind not in KEY_KINDS:
            raise RegistrationError(
                f"{cls.__name__}.ordering: {model.__name__}.{column} cannot order "
                f"a cursor page; only non-null {', '.join(sorted(KEY_KINDS))} "
                "columns can"
            )

    @classmethod
    def parse(cls, query: dict[str, list[str]], reserved: Iterable[str] = ()) -> Self:
        """The filters of a parsed query string; names in ``reserved`` (the
        pagination parameters) are left alone. A parameter that is not
        declared, is repeated or has a wrong value raises ``ValidationError``."""
        problems, parsed = cls.plan.parse(query, list(reserved))
        if parsed is None:
            raise ValidationError([ErrorDetail(*item) for item in problems])
        return cls(parsed)

    def __init__(self, parsed: core.FilterParsed) -> None:
        entries, conditions, order, term, scope = parsed
        self.conditions = conditions
        self.order = tuple(
            f"-{name}" if descending else name for name, descending in order or ()
        )
        self.search_term = term
        self.scope = scope
        self.arranged = order
        self.nulls: dict[str, bool] = {}
        for item in self.fields:
            setattr(self, item.name, None)
        for name, eq, many, low, high, null in entries:
            self.assign(name, eq, many, low, high)
            if null is not None:
                self.nulls[name] = null

    def assign(
        self,
        name: str,
        eq: object,
        many: list[object] | None,
        low: object,
        high: object,
    ) -> None:
        if many is not None:
            setattr(self, name, many)
        elif eq is not None:
            setattr(self, name, eq)
        elif low is not None or high is not None:
            setattr(self, name, Bounds(low, high))

    def is_null(self, name: str) -> bool | None:
        """``True``/``False`` for ``?name.null=true|false``; ``None`` when not sent."""
        return self.nulls.get(name)

    def apply[M: Model](self, query: QuerySet[M]) -> QuerySet[M]:
        """``query`` narrowed by the client's filters, ANDed: never wider."""
        if not self.conditions:
            return query
        return query.derive(filters=(*query.filters, *self.conditions))

    def arrange[M: Model](self, query: QuerySet[M]) -> QuerySet[M]:
        """``query`` ordered as the client asked; its own order stays when the
        client did not ask. The primary key ends the order, so equal sort values
        keep one position from page to page."""
        if self.arranged is None:
            return query
        return query.derive(order=tuple(self.arranged)).stably_ordered()
