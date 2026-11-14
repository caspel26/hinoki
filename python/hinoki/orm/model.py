"""``Model``, ``field`` and ``foreign_key``: single-table Active Record models.

Typing note: construction (``Post(title=...)``) is checked statically;
keyword-style helpers such as ``Post.acreate(**values)`` are not (see
docs/typing.md) until generated stubs exist.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast, dataclass_transform

from hinoki import core
from hinoki.blocking import run_sync
from hinoki.errors import (
    FieldError,
    ObjectNotFound,
    RegistrationError,
)
from hinoki.orm.context import current_database
from hinoki.orm.queryset import QuerySet
from hinoki.orm.relations import ON_DELETE, Related, Relation
from hinoki.utils import Clock, utc_now

if TYPE_CHECKING:
    from hinoki.schema import Schema

MARK = "hinoki_field"
SNAPSHOT = "hinoki_snapshot"


@dataclasses.dataclass(frozen=True, slots=True)
class FieldMeta:
    primary_key: bool = False
    unique: bool = False
    max_length: int | None = None
    references: type[Model] | None = None
    on_delete: str = ""
    related_name: str = ""


def field(
    *,
    primary_key: bool = False,
    unique: bool = False,
    max_length: int | None = None,
    default: Any = dataclasses.MISSING,
    default_factory: Callable[[], Any] | Any = dataclasses.MISSING,
) -> Any:
    """Declare a column.

    ``id: UUID = field(primary_key=True, default_factory=uuid4)``
    """
    meta = FieldMeta(primary_key, unique, max_length)
    return dataclasses.field(
        default=default, default_factory=default_factory, metadata={MARK: meta}
    )


def foreign_key(
    model: type[Model],
    *,
    on_delete: str,
    related_name: str = "",
    unique: bool = False,
    default: Any = dataclasses.MISSING,
) -> Any:
    """Declare a column that references ``model``'s primary key.

    ``author_id: UUID = foreign_key(User, on_delete="restrict", related_name="posts")``

    ``on_delete`` is required (``restrict``, ``cascade``, ``set_null`` or
    ``no_action``) so a deletion rule is always a decision. The relation is
    named after the column without ``_id`` and is loaded only by
    ``select_related`` / ``prefetch_related``.
    """
    if on_delete not in ON_DELETE:
        raise RegistrationError(f"on_delete must be one of {sorted(ON_DELETE)}")
    meta = FieldMeta(
        unique=unique, references=model, on_delete=on_delete, related_name=related_name
    )
    return dataclasses.field(default=default, metadata={MARK: meta})


MIN_UNIQUE = 2


@dataclass_transform(kw_only_default=True, field_specifiers=(field,))
class Model:
    """Base class: ``class Post(Model, table="posts"): ...``."""

    __table__: ClassVar[str] = ""
    __columns__: ClassVar[tuple[str, ...]] = ()
    __primary_key__: ClassVar[str] = ""
    __relations__: ClassVar[dict[str, Relation]] = {}
    __unique__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __indexes__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __ordering__: ClassVar[tuple[str, ...]] = ()
    __label__: ClassVar[str] = ""
    __plural__: ClassVar[str] = ""
    __updated_at__: ClassVar[str | None] = None
    __clock__: ClassVar[Clock] = utc_now
    NotFound: ClassVar[type[ObjectNotFound]] = ObjectNotFound

    def __init_subclass__(
        cls,
        table: str = "",
        unique: Sequence[Sequence[str]] = (),
        label: str = "",
        plural: str = "",
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if not table:
            raise RegistrationError(f"{cls.__name__}: table=... is required")
        dataclasses.dataclass(kw_only=True)(cls)
        fields = dataclasses.fields(cast(Any, cls))
        keys = [f.name for f in fields if f.metadata.get(MARK, FieldMeta()).primary_key]
        if len(keys) != 1:
            raise RegistrationError(f"{cls.__name__}: exactly one primary_key field")
        cls.__table__ = table
        cls.__columns__ = tuple(f.name for f in fields)
        cls.__primary_key__ = keys[0]
        cls.check_names(cls.__columns__)
        cls.__unique__ = cls.check_unique(unique)
        cls.__indexes__ = ()  # a subclass does not inherit its parent's options
        cls.__ordering__ = ()
        cls.__label__ = label
        cls.__plural__ = plural
        cls.__updated_at__ = None
        cls.__clock__ = utc_now
        cls.__relations__ = {
            name: relation
            for base in cls.__mro__[1:]
            for name, relation in getattr(base, "__relations__", {}).items()
            if relation.forward
        }
        cls.register_relations(fields)
        cls.NotFound = type(
            "NotFound", (ObjectNotFound,), {"__qualname__": f"{cls.__name__}.NotFound"}
        )

    @classmethod
    def check_unique(
        cls, groups: Sequence[Sequence[str]]
    ) -> tuple[tuple[str, ...], ...]:
        """Composite uniqueness, e.g. ``unique=[("post_id", "tag_id")]`` on a
        many-to-many join model. Single columns use ``field(unique=True)``."""
        checked = []
        for group in groups:
            columns = tuple(group)
            unknown = [c for c in columns if c not in cls.__columns__]
            if (
                len(columns) < MIN_UNIQUE
                or len(set(columns)) != len(columns)
                or unknown
            ):
                raise RegistrationError(
                    f"{cls.__name__}: unique={columns} needs two or more distinct "
                    f"columns of the model (unknown: {unknown})"
                )
            checked.append(columns)
        return tuple(checked)

    @classmethod
    def check_names(cls, columns: tuple[str, ...]) -> None:
        """A column named like a Model method would hide it on every instance."""
        reserved = sorted(n for n in columns if n in dir(Model) or n.startswith("__"))
        if reserved:
            raise RegistrationError(
                f"{cls.__name__}: field names {reserved} collide with Model attributes"
            )

    @classmethod
    def register_relations(cls, fields: tuple[dataclasses.Field[Any], ...]) -> None:
        """Foreign keys declared by this class (inherited ones come with the
        base). Every check runs before anything is attached, so a failing
        definition leaves the referenced models untouched."""
        own = cls.__dict__.get("__annotations__", {})
        pending: list[tuple[type[Model], Relation]] = []
        for column in fields:
            meta: FieldMeta = column.metadata.get(MARK, FieldMeta())
            if meta.references is None or column.name not in own:
                continue
            name = column.name.removesuffix("_id")
            if name == column.name:
                raise RegistrationError(
                    f"{cls.__name__}.{column.name}: foreign key columns end in _id"
                )
            pending.append((cls, Relation(name, meta.references, column.name, True)))
            if meta.related_name:
                back = Relation(meta.related_name, cls, column.name, False)
                pending.append((meta.references, back))
        for owner, relation in pending:
            owner.check_attach(relation.name, relation)
        for owner, relation in pending:
            owner.attach(relation.name, relation)

    @classmethod
    def check_attach(cls, name: str, relation: Relation) -> None:
        existing = cls.__relations__.get(name)
        redefined = (
            existing is not None
            and not existing.forward
            and not relation.forward
            and (existing.model.__module__, existing.model.__qualname__)
            == (relation.model.__module__, relation.model.__qualname__)
        )  # the same model defined again (reload, notebook re-run)
        taken = (
            name in cls.__columns__
            or (name in cls.__relations__ and not redefined)
            or (
                hasattr(cls, name)
                and not isinstance(inspect.getattr_static(cls, name), Related)
            )
        )
        if taken:
            raise RegistrationError(f"{cls.__name__} already has an attribute {name!r}")

    @classmethod
    def attach(cls, name: str, relation: Relation) -> None:
        cls.__relations__[name] = relation
        setattr(cls, name, Related(name, relation))

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        instance = cls(**row)
        instance.remember()
        return instance

    def remember(self) -> None:
        """Record the stored values, so ``asave`` writes only what changed."""
        columns = type(self).__columns__
        self.__dict__[SNAPSHOT] = {c: copy.deepcopy(getattr(self, c)) for c in columns}

    async def asave(self) -> Self:
        """Insert a new instance, or update the columns changed since it was
        loaded. Typed: ``Post(title="x", ...)`` is checked by the type checker."""
        cls = type(self)
        stored = self.__dict__.get(SNAPSHOT)
        if stored is None:
            return await self.insert()
        key = cls.__primary_key__
        if getattr(self, key) != stored[key]:
            raise FieldError(f"{cls.__name__}: the primary key cannot be changed")
        changed = {
            c: getattr(self, c)
            for c in cls.__columns__
            if c != key and getattr(self, c) != stored[c]
        }
        if changed:
            updated = await cls.aupdate(**{key: stored[key]}, **changed)
            self.refresh(updated)
        return self

    async def insert(self) -> Self:
        cls = type(self)
        assignments = [(c, getattr(self, c)) for c in cls.__columns__]
        sql, params = core.compile_query(
            "insert",
            cls.__table__,
            assignments=assignments,
            returning=list(cls.__columns__),
        )
        rows = await current_database().fetch(sql, *params)
        self.refresh(cls.from_row(rows[0]))
        return self

    def refresh(self, stored: Model) -> None:
        for column in type(self).__columns__:
            setattr(self, column, getattr(stored, column))
        self.remember()

    def save(self) -> Self:
        return run_sync(self.asave())

    def related(self, name: str) -> Any:
        """The loaded relation ``name`` (``RelationNotLoaded`` if it was not)."""
        relation = self.__relations__.get(name)
        if relation is None:
            raise FieldError(f"{type(self).__name__} has no relation {name!r}")
        return getattr(self, name)

    @classmethod
    def filter(cls, **conditions: Any) -> QuerySet[Self]:
        return QuerySet(cls).filter(**conditions)

    @classmethod
    def all(cls) -> QuerySet[Self]:
        return QuerySet(cls)

    @classmethod
    def from_schema(cls, data: Schema | None = None, /, **values: Any) -> Self:
        """An unsaved instance from a schema's values plus ``values``. Explicit
        keyword values win over the schema's, so a column the server assigns
        (owner, state) can never come from the client."""
        merged = cls.merged_values(data, values)
        return cls(**merged)

    @classmethod
    async def acreate(cls, data: Schema | None = None, /, **values: Any) -> Self:
        """Insert a row (defaults and factories apply) and return it:
        ``await Post.acreate(data, author_id=me)`` or ``acreate(title="x")``."""
        return await cls.from_schema(data, **values).insert()

    def assign(self, data: Schema | None = None, /, **values: Any) -> Self:
        """Set fields from a schema (fields the client left ``UNSET`` are
        untouched) and keyword values, without touching the database."""
        merged = type(self).merged_values(data, values)
        for name, value in merged.items():  # every name is valid: no half-applied state
            setattr(self, name, value)
        return self

    async def aupdate_from(self, data: Schema | None = None, /, **values: Any) -> Self:
        """``assign`` then ``asave``: only the columns that changed are written."""
        before = {c: getattr(self, c) for c in type(self).__columns__}
        try:
            return await self.assign(data, **values).asave()
        except BaseException:
            for name, value in before.items():  # a failed write leaves no dirty object
                setattr(self, name, value)
            raise

    @classmethod
    async def aget(cls, **conditions: Any) -> Self:
        """Exactly one row, else ``Model.NotFound`` / ``MultipleObjectsReturned``."""
        return await cls.filter(**conditions).aget()

    @classmethod
    async def aget_or_none(cls, **conditions: Any) -> Self | None:
        try:
            return await cls.aget(**conditions)
        except cls.NotFound:
            return None

    @classmethod
    async def aupdate(cls, **values: Any) -> Self:
        """Update by primary key (``id=...``) and return the updated row."""
        key = cls.__primary_key__
        if key not in values:
            raise FieldError(f"{cls.__name__}.aupdate needs {key}=...")
        identity = values.pop(key)
        rows = await cls.aupdate_where({key: identity}, values)
        if not rows:
            raise cls.NotFound(f"{cls.__name__} not found")
        return rows[0]

    @classmethod
    async def aupdate_where(
        cls, conditions: dict[str, Any], values: dict[str, Any]
    ) -> list[Self]:
        """Update every row matching ``conditions`` and return the new rows.
        Conditions are part of the statement, so a row that stopped matching
        (for example after a concurrent owner change) is not touched."""
        if not conditions:
            raise FieldError("aupdate_where needs conditions and values")
        return await QuerySet(cls).filter(**conditions).aupdate(values)

    @classmethod
    async def adelete(cls, **conditions: Any) -> int:
        """Delete matching rows; returns how many. An empty condition set is
        refused so a missing filter cannot wipe the table."""
        if not conditions:
            raise FieldError("adelete needs at least one condition")
        return await QuerySet(cls).filter(**conditions).adelete()

    @classmethod
    def create(cls, data: Schema | None = None, /, **values: Any) -> Self:
        """Sync ``acreate`` for sync handlers."""
        return run_sync(cls.acreate(data, **values))

    def update_from(self, data: Schema | None = None, /, **values: Any) -> Self:
        """Sync ``aupdate_from`` for sync handlers."""
        return run_sync(self.aupdate_from(data, **values))

    @classmethod
    def update_where(
        cls, conditions: dict[str, Any], values: dict[str, Any]
    ) -> list[Self]:
        """Sync ``aupdate_where`` for sync handlers."""
        return run_sync(cls.aupdate_where(conditions, values))

    @classmethod
    def get(cls, **conditions: Any) -> Self:
        return run_sync(cls.aget(**conditions))

    @classmethod
    def get_or_none(cls, **conditions: Any) -> Self | None:
        return run_sync(cls.aget_or_none(**conditions))

    @classmethod
    def update(cls, **values: Any) -> Self:
        return run_sync(cls.aupdate(**values))

    @classmethod
    def delete(cls, **conditions: Any) -> int:
        return run_sync(cls.adelete(**conditions))

    @classmethod
    def merged_values(
        cls, data: Schema | None, values: dict[str, Any]
    ) -> dict[str, Any]:
        """The schema's values with explicit ``values`` winning, every name
        checked. A schema may never carry the primary key: the client must not
        choose a row's identity (pass it as a keyword when the server does)."""
        from hinoki.schema import Schema  # noqa: PLC0415 - schema imports are lazy here

        if data is not None and not isinstance(data, Schema):
            raise TypeError(f"expected a Schema, not {type(data).__name__}")
        sent = data.to_dict() if data is not None else {}
        if cls.__primary_key__ in sent and cls.__primary_key__ not in values:
            raise FieldError(
                f"{cls.__name__}: the schema carries the primary key "
                f"{cls.__primary_key__!r}; remove it from the schema"
            )
        merged = {**sent, **values}
        for name in merged:
            cls.check_field(name)
        return merged

    @classmethod
    def check_field(cls, name: str) -> None:
        if name not in cls.__columns__:
            raise FieldError(f"{cls.__name__} has no field {name!r}")
