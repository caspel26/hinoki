"""Many-to-many relations over a generated or adopted bridge model.

``tags: ClassVar[ManyToMany[Tag]] = many_to_many(Tag)`` declares the relation.
Registration builds a bridge ``Model`` (one row per link) and registers it, so
migrations create its table. Reading and writing links goes through ``LinkSet``
(``post.tags``): the native layer runs the writes (scope, ownership and the
transaction included) and the compiler turns filters across the relation into
subqueries.
"""

from __future__ import annotations

import builtins
import dataclasses
import re
import types
import typing
import weakref
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self, cast, overload
from uuid import UUID, uuid4

from hinoki import core, events
from hinoki.blocking import run_sync
from hinoki.errors import (
    ErrorDetail,
    FieldError,
    LinkLimitExceeded,
    ModelError,
    ModelValidationError,
    OwnerOutOfScope,
    RegistrationError,
    RelationNotLoaded,
    StoredLinksOverLimit,
    UnknownLinkTarget,
)
from hinoki.fields import FieldInfo
from hinoki.orm.context import current_database
from hinoki.orm.model import MARK, FieldMeta, Model, pk
from hinoki.orm.naming import snake_case
from hinoki.orm.queryset import (
    IN_CHUNK,
    Loading,
    QuerySet,
    SubQuery,
    condition,
    write_transaction,
)
from hinoki.orm.relations import STORE, OnDelete, store

if TYPE_CHECKING:
    from hinoki.events import EventBus

LinkKey = int | str | UUID
MAX_LINK_ITEMS = 10_000
DEFAULT_MAX_ITEMS = 1000
MAX_IDENTIFIER = 63
ALIAS = "rel_far"
LINK_ALIAS = "hinoki_link"
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*\Z")
KEY_KINDS = {"uuid": "uuid", "int": "int", "str": "text"}
RULES = (OnDelete.CASCADE, OnDelete.RESTRICT)
AUTO_TABLES: weakref.WeakSet[type[Model]] = weakref.WeakSet()  # named after the owner
# Bridges declared by name, completed when that model is defined.
PENDING: dict[tuple[str, str], tuple[type[Model], str]] = {}


@dataclasses.dataclass(frozen=True, slots=True)
class LinkChange:
    """The target keys a write linked and unlinked, in ascending order."""

    added: tuple[LinkKey, ...] = ()
    removed: tuple[LinkKey, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class Declaration:
    target: type[Model] | str
    related_name: str
    on_delete: OnDelete
    target_on_delete: OnDelete
    bridge: str
    columns: tuple[str, str] | None
    table: str
    ids_name: str
    max_items: int


@dataclasses.dataclass(frozen=True, slots=True)
class LinkSide:
    """One end of a relation: ``source`` holds the attribute, ``near`` is the
    bridge column that points at it and ``far`` the one at ``target``."""

    name: str
    source: type[Model]
    target: type[Model]
    bridge: type[Model]
    near: str
    far: str
    forward: bool
    max_items: int
    ids_name: str
    declarer: type[Model]
    declared: str

    def spec(self) -> core.LinkSpec:
        return core.LinkSpec(
            self.bridge.__table__,
            self.bridge.__primary_key__,
            key_end(self.source, self.near),
            key_end(self.target, self.far),
            self.max_items,
            None if self.forward else self.declarer.__links__[self.declared].max_items,
            self.target.__soft_delete__,
            self.source.__soft_delete__,
        )

    def subquery(self, items: list[tuple[str, Any]]) -> SubQuery:
        """``source.pk IN (SELECT near FROM bridge ...)`` for the conditions
        ``items`` (``name__op`` and value) on the target."""
        target = self.target
        key = target.__primary_key__
        joins: list[tuple[str, str, str, str, bool, list[str], str | None]] = []
        filters: list[tuple[str, str, list[Any]]] = []
        checks: list[tuple[str, str, list[Any]]] = []
        negated = False
        for rest, value in items:
            if rest == "isnull":
                negated = bool(value)
                continue
            if not rest:
                raise FieldError(
                    f"filter {self.name} with a field: {self.name}__<field>"
                )
            column, op, values = condition(target, rest, value)
            if column == key:
                filters.append((self.far, op, values))
                continue
            if not joins:
                joins.append((target.__table__, ALIAS, self.far, key, True, [], None))
            filters.append((f"{ALIAS}.{column}", op, values))
            checks.append((column, op, values))
        if negated and (filters or len(items) > 1):
            raise FieldError(
                f"{self.name}__isnull cannot be combined with other conditions"
            )
        if target.__soft_delete__ is not None:
            # a link to a soft-deleted target does not count as a link
            if not joins:
                joins.append((target.__table__, ALIAS, self.far, key, True, [], None))
            filters.append((f"{ALIAS}.{target.__soft_delete__}", "isnull", []))
        return SubQuery(
            self.source.__primary_key__,
            negated,
            self.bridge.__table__,
            self.near,
            target,
            tuple(joins),
            tuple(filters),
            tuple(checks),
        )

    def membership(self, owner_key: object) -> SubQuery:
        """The targets linked to one owner, as a condition on the target."""
        return SubQuery(
            self.target.__primary_key__,
            False,
            self.bridge.__table__,
            self.far,
            self.bridge,
            filters=((self.near, "eq", [owner_key]),),
        )

    def check_within(self, query: QuerySet[Any]) -> None:
        """A scope is a query of the target made only of conditions."""
        loading = query.loading
        if query.model is not self.target:
            raise FieldError(
                f"{self.name}: the scope must be a {self.target.__name__} query"
            )
        if (
            query.limit_to is not None
            or query.offset_by is not None
            or loading.selected
            or loading.prefetched
        ):
            raise FieldError(
                f"{self.name}: the scope may only filter (no limit, offset, "
                "select_related or prefetch_related)"
            )

    def scope(
        self, within: QuerySet[Any] | None
    ) -> tuple[list[Any], list[Any], list[core._RawSub]]:
        if within is None:
            if self.target.__soft_delete__ is None:
                return ([], [], [])
            # soft-deleted targets cannot be linked, unlinked or replaced
            within = self.target.all()
        self.check_within(within)
        return raw_scope(within)

    def owner_scope(
        self, owner_within: QuerySet[Any] | None
    ) -> tuple[list[Any], list[Any], list[core._RawSub]] | None:
        """The conditions an owner row must meet, checked once it is locked."""
        if owner_within is None:
            return None
        if owner_within.model is not self.source:
            raise FieldError(
                f"{self.name}: the owner scope must be a {self.source.__name__} query"
            )
        if owner_within.limit_to is not None or owner_within.offset_by is not None:
            raise FieldError(f"{self.name}: the owner scope may only filter")
        return raw_scope(owner_within)

    def base_query(self, within: QuerySet[Any] | None) -> QuerySet[Any]:
        if within is None:
            return self.target.all()
        self.check_within(within)
        query: QuerySet[Any] = QuerySet(
            self.target,
            filters=within.filters,
            loading=Loading(joined=within.loading.joined),
        )
        return query.with_subfilters(within.subfilters) if within.subfilters else query

    async def prefetch(
        self, instances: list[Any], within: QuerySet[Any] | None
    ) -> None:
        """Load ``self.name`` with one joined query per chunk of owners."""
        key = self.source.__primary_key__
        owners = [getattr(item, key) for item in instances]
        query = self.base_query(within)
        if within is not None and self.target.__ordering__:
            query.order = within.order
        groups: dict[str, list[Any]] = {}
        targets: dict[str, Any] = {}
        for start in range(0, len(owners), IN_CHUNK):
            chunk = owners[start : start + IN_CHUNK]
            chunk_groups = await self.prefetch_chunk(query, chunk)
            for owner, items in chunk_groups.items():
                for item in items:
                    target_key = str(getattr(item, self.target.__primary_key__))
                    existing = targets.setdefault(target_key, item)
                    groups.setdefault(owner, []).append(existing)
        for item in instances:
            found = tuple(groups.get(str(getattr(item, key)), ()))
            store(item, self.name, (found, tuple(ids_of(found, self.target))))

    async def prefetch_chunk(
        self, query: QuerySet[Any], owners: list[Any]
    ) -> dict[str, list[Any]]:
        database = current_database()
        alias = f"{LINK_ALIAS}__0"
        join: core._RawJoin = (
            self.bridge.__table__,
            LINK_ALIAS,
            self.target.__primary_key__,
            self.far,
            False,
            [self.near],
            None,
        )
        sql, params = query.compile_through(
            join,
            (f"{LINK_ALIAS}.{self.near}", "in", owners),
            len(owners) * self.max_items + 1,
            database.dialect,
        )
        plan = query.model.row_plan() if query.uses_stock_builders() else None
        fetch_models = getattr(database, "fetch_models", None)
        if plan is not None and fetch_models is not None:
            result: Any = await fetch_models(
                sql, params, plan.linked(alias, self.max_items)
            )
        else:
            rows = await database.fetch(sql, *params)
            result = self.prefetch_fallback(query, rows, alias)
        if result is None:
            raise StoredLinksOverLimit(self.name, self.max_items)
        return cast(dict[str, list[Any]], result)

    def prefetch_fallback(
        self, query: QuerySet[Any], rows: list[dict[str, Any]], alias: str
    ) -> dict[str, list[Any]] | None:
        groups: dict[str, list[Any]] = {}
        for row in rows:
            group = groups.setdefault(str(row[alias]), [])
            group.append(query.build(row))
            if len(group) > self.max_items:
                return None
        return groups


def raw_scope(
    query: QuerySet[Any],
) -> tuple[list[Any], list[Any], list[core._RawSub]]:
    """The joins, filters and subfilters of ``query`` in the form Rust takes."""
    return (
        list(query.joins(counting=True)),
        list(query.filters),
        [sub.raw() for sub in query.subfilters],
    )


def ids_of(targets: Iterable[Any], model: type[Model]) -> list[Any]:
    key = model.__primary_key__
    return [getattr(target, key) for target in targets]


def key_end(model: type[Model], column: str) -> tuple[str, str, str, str, int | None]:
    key = model.__primary_key__
    kind = model.__field_types__[key]
    return (
        model.__table__,
        key,
        column,
        KEY_KINDS[kind.kind],
        kind.info.max_length,
    )


class LinkSet[T: Model]:
    """``post.tags``: the links of one owner. Writes run in one transaction in
    the native layer; ``within`` narrows which targets the call may touch."""

    def __init__(self, owner: Model, side: LinkSide) -> None:
        self.owner = owner
        self.side = side

    @property
    def loaded(self) -> tuple[T, ...]:
        """The targets ``prefetch_related`` loaded."""
        return typing.cast("tuple[T, ...]", self.stored()[0])

    @property
    def loaded_ids(self) -> tuple[LinkKey, ...]:
        return typing.cast("tuple[LinkKey, ...]", self.stored()[1])

    def stored(self) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        found = self.owner.__dict__.get(STORE, {}).get(self.side.name)
        if found is None:
            raise RelationNotLoaded(
                f"{type(self.owner).__name__}.{self.side.name} was not loaded: "
                f"query with .prefetch_related({self.side.name!r})"
            )
        return typing.cast("tuple[tuple[Any, ...], tuple[Any, ...]]", found)

    def owner_key(self) -> Any:
        if self.owner.is_new:
            raise ModelError(
                f"save {type(self.owner).__name__} before linking {self.side.name}"
            )
        return getattr(self.owner, self.side.source.__primary_key__)

    def query(self, *, within: QuerySet[T] | None = None) -> QuerySet[T]:
        """The linked targets as a query (filter, order, count, paginate)."""
        base = self.side.base_query(within)
        query = base.with_subfilters((self.side.membership(self.owner_key()),))
        if not self.side.target.__ordering__:
            query = query.order_by(self.side.target.__primary_key__)
        return typing.cast("QuerySet[T]", query)

    async def alist(self, *, within: QuerySet[T] | None = None) -> builtins.list[T]:
        return await self.query(within=within)

    async def acount(self, *, within: QuerySet[T] | None = None) -> int:
        return await self.query(within=within).acount()

    async def aexists(self, *, within: QuerySet[T] | None = None) -> bool:
        return await self.query(within=within).aexists()

    async def aadd(
        self, *targets: T | LinkKey, within: QuerySet[T] | None = None
    ) -> LinkChange:
        """Link the targets; already linked ones are kept. A target that does
        not exist or is outside ``within`` raises ``UnknownLinkTarget`` and
        nothing is written."""
        return await self.write("add", targets, within)

    async def aremove(
        self, *targets: T | LinkKey, within: QuerySet[T] | None = None
    ) -> LinkChange:
        """Unlink the targets; unknown or out-of-scope ones are ignored."""
        return await self.write("remove", targets, within)

    async def aset(
        self, targets: Iterable[T | LinkKey], *, within: QuerySet[T] | None = None
    ) -> LinkChange:
        """Make the targets within ``within`` exactly the given ones; links to
        targets outside it are left alone."""
        return await self.write("set", tuple(targets), within)

    def add(
        self, *targets: T | LinkKey, within: QuerySet[T] | None = None
    ) -> LinkChange:
        return run_sync(self.aadd(*targets, within=within))

    def remove(
        self, *targets: T | LinkKey, within: QuerySet[T] | None = None
    ) -> LinkChange:
        return run_sync(self.aremove(*targets, within=within))

    def set(
        self, targets: Iterable[T | LinkKey], *, within: QuerySet[T] | None = None
    ) -> LinkChange:
        return run_sync(self.aset(targets, within=within))

    def list(self, *, within: QuerySet[T] | None = None) -> builtins.list[T]:
        return run_sync(self.alist(within=within))

    def count(self, *, within: QuerySet[T] | None = None) -> int:
        return run_sync(self.acount(within=within))

    def exists(self, *, within: QuerySet[T] | None = None) -> bool:
        return run_sync(self.aexists(within=within))

    def keys(self, targets: Iterable[object]) -> builtins.list[LinkKey]:
        """The primary keys of ``targets`` (rows or keys), validated natively."""
        side = self.side
        raw: list[Any] = []
        for item in targets:
            value = item
            if isinstance(item, Model):
                if not isinstance(item, side.target):
                    raise FieldError(
                        f"{side.name} links {side.target.__name__} rows, "
                        f"not {type(item).__name__}"
                    )
                value = getattr(item, side.target.__primary_key__)
            raw.append(value)
        result: tuple[object, ...] = core.coerce_link_keys(side.spec(), raw, True)
        if result[0] == "invalid":
            label = side.ids_name or side.name
            raise ModelValidationError(
                [
                    ErrorDetail(
                        f"{label}[{result[1]}]", "invalid_key", "is not a valid id"
                    )
                ]
            )
        if result[0] == "too_many":
            raise LinkLimitExceeded(side.name, side.max_items)
        return typing.cast("builtins.list[LinkKey]", result[1])

    async def write(
        self,
        op: str,
        targets: Iterable[object],
        within: QuerySet[T] | None,
        owner_within: QuerySet[Any] | None = None,
    ) -> LinkChange:
        """Write the links. ``owner_within`` (internal to generated CRUD and the
        admin) is a query of the owner model: once the owner row is locked it
        must still match it, else the owner counts as missing."""
        side = self.side
        owner = self.owner_key()
        keys = self.keys(targets)
        scope = side.scope(within)
        owner_scope = side.owner_scope(owner_within)
        if op != "set" and not keys:
            return LinkChange()
        database = current_database()
        async with write_transaction(database):
            active = database.active()
            if active is None:
                raise core.DatabaseError("link writes run inside a transaction")
            outcome = await active.native.link_write(
                side.spec(), database.dialect, op, (owner, keys), scope, owner_scope
            )
            change = self.checked(outcome)
            await self.announce(owner, change)
        return change

    def checked(self, outcome: tuple[Any, ...]) -> LinkChange:
        side = self.side
        match outcome[0]:
            case "done":
                return LinkChange(tuple(sorted(outcome[1])), tuple(sorted(outcome[2])))
            case "missing":
                raise UnknownLinkTarget(side.ids_name or side.name, outcome[1])
            case "limit":
                raise LinkLimitExceeded(side.name, side.max_items)
            case "owner_missing":
                raise OwnerOutOfScope(f"{side.source.__name__} left the caller's scope")
            case _:
                raise side.source.NotFound(f"{side.source.__name__} not found")

    async def announce(self, owner: object, change: LinkChange) -> None:
        """Emit ``Linked`` and ``Unlinked`` for the declaring model's rows."""
        side = self.side
        for kind, keys in (
            (events.Linked, change.added),
            (events.Unlinked, change.removed),
        ):
            bus = events.listener(kind, side.declarer) if keys else None
            if bus is not None:
                await self.emit(bus, kind, owner, keys)

    async def emit(
        self,
        bus: EventBus,
        kind: type[events.Linked[Any]] | type[events.Unlinked[Any]],
        owner: object,
        keys: tuple[LinkKey, ...],
    ) -> None:
        side = self.side
        if side.forward:
            await bus.emit(kind(side.declarer, side.declared, owner, keys))
            return
        for key in keys:  # one event per declaring row
            await bus.emit(kind(side.declarer, side.declared, key, (owner,)))


class LinkIds:
    """``post.tag_ids``: the ids of the prefetched targets."""

    def __init__(self, name: str) -> None:
        self.name = name

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Self: ...

    @overload
    def __get__(
        self, instance: object, owner: type | None = None
    ) -> tuple[LinkKey, ...]: ...

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        found = instance.__dict__.get(STORE, {}).get(self.name)
        if found is None:
            raise RelationNotLoaded(
                f"{type(instance).__name__}.{self.name} was not loaded: "
                f"query with .prefetch_related({self.name!r})"
            )
        return found[1]

    def __set__(self, instance: Any, value: Any) -> None:
        raise AttributeError("a link id list is read-only; use the LinkSet")


class ManyToMany[T: Model]:
    """The declaration (``many_to_many(Tag)``) and, once bound, the attribute
    that gives each instance its ``LinkSet``."""

    __hinoki_link__: ClassVar[bool] = True
    """Lets model registration leave a missing ``ClassVar`` to ``bind``."""
    side: LinkSide | None

    def __init__(self, declaration: Declaration | None = None) -> None:
        self.declaration = declaration
        self.side = None

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Self: ...

    @overload
    def __get__(self, instance: object, owner: type | None = None) -> LinkSet[T]: ...

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        if self.side is None:
            raise RegistrationError(
                "this many-to-many relation is waiting for its bridge model "
                "to be defined"
            )
        return LinkSet(instance, self.side)

    def __set__(self, instance: Any, value: Any) -> None:
        raise AttributeError("a many-to-many relation is changed with add/remove/set")


@overload
def many_to_many[T: Model](
    target: type[T],
    *,
    related_name: str = "",
    on_delete: OnDelete = OnDelete.CASCADE,
    target_on_delete: OnDelete = OnDelete.CASCADE,
    bridge: str = "",
    columns: tuple[str, str] | None = None,
    table: str = "",
    ids_name: str = "",
    max_items: int = DEFAULT_MAX_ITEMS,
) -> ManyToMany[T]: ...
@overload
def many_to_many[T: Model](
    target: Literal["self"],
    *,
    related_name: str = "",
    on_delete: OnDelete = OnDelete.CASCADE,
    target_on_delete: OnDelete = OnDelete.CASCADE,
    bridge: str = "",
    columns: tuple[str, str] | None = None,
    table: str = "",
    ids_name: str = "",
    max_items: int = DEFAULT_MAX_ITEMS,
) -> ManyToMany[T]: ...
def many_to_many(  # noqa: PLR0913 - each keyword is an independent option
    target: type[Model] | str,
    *,
    related_name: str = "",
    on_delete: OnDelete = OnDelete.CASCADE,
    target_on_delete: OnDelete = OnDelete.CASCADE,
    bridge: str = "",
    columns: tuple[str, str] | None = None,
    table: str = "",
    ids_name: str = "",
    max_items: int = DEFAULT_MAX_ITEMS,
) -> ManyToMany[Any]:
    """Declare a many-to-many relation on a model::

        tags: ClassVar[ManyToMany[Tag]] = many_to_many(Tag, related_name="posts")

    A bridge model (table ``<owner table>_<name>``) is generated and migrated;
    ``bridge="PostTag"`` adopts a model of the same module instead, and
    ``many_to_many("self")`` relates a model to itself (it needs ``columns``
    or the generated ``from_*_id`` / ``to_*_id`` names).
    """
    if not 1 <= max_items <= MAX_LINK_ITEMS or type(max_items) is not int:
        raise RegistrationError(f"max_items must be between 1 and {MAX_LINK_ITEMS}")
    for rule in (on_delete, target_on_delete):
        if rule not in RULES:
            raise RegistrationError(
                "a many-to-many link is deleted with its row (OnDelete.CASCADE) "
                "or blocks the delete (OnDelete.RESTRICT)"
            )
    if bridge and (
        table
        or on_delete is not OnDelete.CASCADE
        or target_on_delete is not OnDelete.CASCADE
    ):
        raise RegistrationError(
            "bridge= adopts a model: its table and foreign keys are its own, "
            "so table=, on_delete and target_on_delete do not apply"
        )
    if (
        not (isinstance(target, type) and issubclass(target, Model))
        and target != "self"
    ):
        raise RegistrationError("many_to_many takes a Model class or 'self'")
    return ManyToMany(
        Declaration(
            target,
            related_name,
            on_delete,
            target_on_delete,
            bridge,
            columns,
            table,
            ids_name,
            max_items,
        )
    )


def bind(cls: type[Model], *, abstract: bool) -> None:
    """Register the relations ``cls`` declares and complete the adoptions that
    were waiting for ``cls``; called by ``Model.__init_subclass__``."""
    for item in dataclasses.fields(typing.cast("Any", cls)):
        if isinstance(item.default, ManyToMany):
            raise RegistrationError(
                f"{cls.__name__}.{item.name}: annotate a many_to_many relation "
                "as ClassVar[ManyToMany[Target]]; without ClassVar it is a column"
            )
    declared = [
        (name, value.declaration)
        for name, value in cls.__dict__.items()
        if isinstance(value, ManyToMany) and value.declaration is not None
    ]
    if declared and abstract:
        raise RegistrationError(
            f"{cls.__name__}: an abstract model cannot declare many_to_many "
            "relations; declare them on the concrete model"
        )
    waiting = PENDING.pop((cls.__module__, cls.__name__), None)
    if waiting is not None:
        owner, name = waiting
        declaration = owner.__dict__[name].declaration
        adopt(owner, name, declaration, cls)
    for name, declaration in declared:
        register(cls, name, declaration)


def check_resolved() -> None:
    """Refuse a ``many_to_many(bridge="Name")`` whose bridge model was never
    defined: the relation would otherwise stay unusable without a word."""
    if not PENDING:
        return
    waiting = ", ".join(
        f"{owner.__name__}.{name} (bridge {module}.{bridge})"
        for (module, bridge), (owner, name) in sorted(PENDING.items())
    )
    raise RegistrationError(f"many_to_many bridges never defined: {waiting}")


def resolved_target(owner: type[Model], decl: Declaration) -> type[Model]:
    """The target model; ``many_to_many`` already refused anything but a Model
    class or ``"self"``."""
    return owner if decl.target == "self" else typing.cast("type[Model]", decl.target)


def register(owner: type[Model], name: str, decl: Declaration) -> None:
    target = resolved_target(owner, decl)
    if decl.bridge:
        key = (owner.__module__, decl.bridge)
        if key in PENDING:
            raise RegistrationError(
                f"{owner.__name__}.{name}: {decl.bridge} is already the bridge of "
                f"{PENDING[key][0].__name__}.{PENDING[key][1]}"
            )
        PENDING[key] = (owner, name)
        return
    near, far = bridge_columns(owner, target, decl)
    reserve_names(owner, name, target, decl)
    table = decl.table or f"{owner.__table__}_{name}"
    check_table_name(owner, table)
    bridge = build_bridge(owner, target, name, decl, near, far, table)
    owner.__bridges__ = (*owner.__bridges__, bridge)
    wire(owner, name, decl, target, bridge, near, far)


def check_table_name(owner: type[Model], table: str) -> None:
    if len(table) > MAX_IDENTIFIER:
        raise RegistrationError(
            f"{owner.__name__}: the bridge table name {table!r} is longer than "
            f"{MAX_IDENTIFIER} characters; give many_to_many(table=...) a shorter one"
        )


def bridge_columns(
    owner: type[Model], target: type[Model], decl: Declaration
) -> tuple[str, str]:
    if decl.columns is not None:
        near, far = decl.columns
    else:
        left, right = snake_case(owner.__name__), snake_case(target.__name__)
        near, far = (
            (f"from_{left}_id", f"to_{right}_id")
            if owner is target or left == right
            else (f"{left}_id", f"{right}_id")
        )
    for column in (near, far):
        if not IDENTIFIER.fullmatch(column) or not column.endswith("_id"):
            raise RegistrationError(
                f"{owner.__name__}: bridge column {column!r} must be a lower-case "
                "identifier ending in _id"
            )
    if near == far or "id" in (near, far):
        raise RegistrationError(f"{owner.__name__}: the bridge columns must differ")
    return near, far


def reserve_names(
    owner: type[Model], name: str, target: type[Model], decl: Declaration
) -> None:
    """The attribute names the relation will add, each checked for a clash."""
    wanted = [(owner, decl.ids_name or f"{name}_ids")]
    if decl.related_name:
        wanted.append((target, decl.related_name))
    for model, attribute in wanted:
        if (
            attribute in model.__columns__
            or attribute in model.__relations__
            or attribute in model.__links__
            or hasattr(model, attribute)
        ):
            raise RegistrationError(
                f"{model.__name__} already has an attribute {attribute!r}"
            )
    if len({attribute for _, attribute in wanted} | {name}) != len(wanted) + 1:
        raise RegistrationError(f"{owner.__name__}.{name}: names must differ")


def key_field(model: type[Model], rule: OnDelete, index: bool) -> Any:
    info = model.__field_types__[model.__primary_key__]
    if info.kind not in KEY_KINDS:
        raise RegistrationError(
            f"{model.__name__}: a many-to-many relation needs a UUID, int or "
            "text primary key"
        )
    length = info.info.max_length
    if info.kind == "str" and length is None:
        raise RegistrationError(
            f"{model.__name__}: a text primary key needs a max_length to be linked"
        )
    meta = FieldMeta(
        references=model,
        on_delete=rule.value,
        max_length=length,
        validation=FieldInfo(max_length=length) if length else FieldInfo(),
        index=index,
    )
    return dataclasses.field(metadata={MARK: meta})


def build_bridge(
    owner: type[Model],
    target: type[Model],
    name: str,
    decl: Declaration,
    near: str,
    far: str,
    table: str,
) -> type[Model]:
    camel = "".join(word.capitalize() for word in name.split("_"))
    annotations = {
        "id": UUID,
        near: owner.__field_annotations__[owner.__primary_key__],
        far: target.__field_annotations__[target.__primary_key__],
    }

    def fill(namespace: dict[str, Any]) -> None:
        namespace["__module__"] = owner.__module__
        namespace["__qualname__"] = f"{owner.__qualname__}.{name}.bridge"
        namespace["__annotations__"] = annotations
        namespace["id"] = pk(default_factory=uuid4)
        namespace[near] = key_field(owner, decl.on_delete, False)
        namespace[far] = key_field(target, decl.target_on_delete, True)

    bridge = types.new_class(
        f"{owner.__name__}{camel}",
        (Model,),
        {"table": table, "unique": [(near, far)]},
        fill,
    )
    if not decl.table:
        AUTO_TABLES.add(bridge)
    return bridge


def wire(
    owner: type[Model],
    name: str,
    decl: Declaration,
    target: type[Model],
    bridge: type[Model],
    near: str,
    far: str,
) -> None:
    ids_name = decl.ids_name or f"{name}_ids"
    forward = LinkSide(
        name, owner, target, bridge, near, far, True, decl.max_items,
        ids_name, owner, name,
    )  # fmt: skip
    descriptor = owner.__dict__[name]
    descriptor.side = forward
    owner.__links__ = {**owner.__links__, name: forward}
    setattr(owner, ids_name, LinkIds(name))
    if decl.related_name:
        reverse = LinkSide(
            decl.related_name, target, owner, bridge, far, near, False,
            MAX_LINK_ITEMS, "", owner, name,
        )  # fmt: skip
        holder: ManyToMany[Any] = ManyToMany()
        holder.side = reverse
        target.__links__ = {**target.__links__, decl.related_name: reverse}
        setattr(target, decl.related_name, holder)


def adopt(
    owner: type[Model], name: str, decl: Declaration, bridge: type[Model]
) -> None:
    """Use ``bridge``, a model of the same module, as the link table."""
    target = resolved_target(owner, decl)
    near, far = adopted_columns(owner, target, name, decl, bridge)
    check_adopted(bridge, near, far, owner, target)
    reserve_names(owner, name, target, decl)
    wire(owner, name, decl, target, bridge, near, far)


def adopted_columns(
    owner: type[Model],
    target: type[Model],
    name: str,
    decl: Declaration,
    bridge: type[Model],
) -> tuple[str, str]:
    if decl.columns is not None:
        return decl.columns
    refs: dict[str, type[Model]] = {}
    for item in dataclasses.fields(typing.cast("Any", bridge)):
        meta: FieldMeta = item.metadata.get(MARK, FieldMeta())
        if meta.references is not None:
            refs[item.name] = meta.references
    near = [c for c, model in refs.items() if model is owner]
    far = [c for c, model in refs.items() if model is target]
    if owner is target or len(near) != 1 or len(far) != 1:
        raise RegistrationError(
            f"{owner.__name__}.{name}: {bridge.__name__} needs exactly one foreign "
            "key to each side, or many_to_many(columns=(owner_column, "
            "target_column)) names them"
        )
    return near[0], far[0]


def check_adopted(
    bridge: type[Model],
    near: str,
    far: str,
    owner: type[Model],
    target: type[Model],
) -> None:
    """The bridge must hold only what a link write can fill in: a UUID key, the
    two distinct, required foreign keys (to the owner and the target) under a
    unique pair, and nullable extras."""
    pair = {near, far}
    label = bridge.__name__
    for column in pair:
        if column not in bridge.__columns__:
            raise RegistrationError(f"{label} has no column {column!r}")
        if column == bridge.__primary_key__:
            raise RegistrationError(f"{label}.{column} cannot be a link column")
        if bridge.__field_types__[column].nullable:
            raise RegistrationError(
                f"{label}.{column} must not be nullable: a NULL row would "
                "escape the unique pair"
            )
    check_references(bridge, near, far, owner, target)
    check_extras(bridge, near, far)


def check_extras(bridge: type[Model], near: str, far: str) -> None:
    pair = {near, far}
    label = bridge.__name__
    if bridge.__field_types__[bridge.__primary_key__].kind != "uuid":
        raise RegistrationError(f"{label}: the primary key of a bridge is a UUID")
    if not any(set(group) == pair for group in bridge.__unique__):
        raise RegistrationError(
            f"{label} must declare unique=[({near!r}, {far!r})] so a link exists once"
        )
    for column in bridge.__columns__:
        if column in pair or column == bridge.__primary_key__:
            continue
        if not bridge.__field_types__[column].nullable:
            raise RegistrationError(
                f"{label}.{column} must be nullable: link writes cannot fill it in"
            )
    if any(bridge.__field_validators__.values()) or bridge.write_hooks():
        raise RegistrationError(
            f"{label}: a bridge model cannot have validators or write hooks"
        )


def check_references(
    bridge: type[Model],
    near: str,
    far: str,
    owner: type[Model],
    target: type[Model],
) -> None:
    """``near`` must be a foreign key to ``owner`` and ``far`` to ``target``."""
    for column, model in ((near, owner), (far, target)):
        item = next(
            f
            for f in dataclasses.fields(typing.cast("Any", bridge))
            if f.name == column
        )
        meta: FieldMeta = item.metadata.get(MARK, FieldMeta())
        if meta.references is not model:
            raise RegistrationError(
                f"{bridge.__name__}.{column} must be a foreign key to {model.__name__}"
            )


def retable(owner: type[Model]) -> None:
    """``@db.table`` renamed ``owner``: generated bridges follow it."""
    for side in owner.__links__.values():
        renamed = side.source is owner and side.forward and side.bridge in AUTO_TABLES
        if renamed:
            table = f"{owner.__table__}_{side.name}"
            check_table_name(owner, table)
            side.bridge.__table__ = table


__all__ = [
    "LinkChange",
    "LinkKey",
    "LinkSet",
    "ManyToMany",
    "bind",
    "many_to_many",
]
