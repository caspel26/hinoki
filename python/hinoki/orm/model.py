"""``Model``, ``field`` and ``foreign_key``: single-table Active Record models.

Typing note: construction (``Post(title=...)``) is checked statically;
keyword-style helpers such as ``Post.acreate(**values)`` are not (see
docs/typing.md) until generated stubs exist.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import sys
import types
import typing
from collections.abc import Callable, Sequence
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Self,
    TypeGuard,
    cast,
    dataclass_transform,
    get_args,
    get_origin,
)

from hinoki import core
from hinoki.blocking import run_sync
from hinoki.errors import (
    FieldError,
    ModelError,
    ObjectNotFound,
    RegistrationError,
)
from hinoki.orm.context import current_database
from hinoki.orm.queryset import QuerySet
from hinoki.orm.relations import OnDelete, Related, Relation
from hinoki.utils import Clock, utc_now

if sys.version_info >= (3, 14):
    import annotationlib

if TYPE_CHECKING:
    from hinoki.schema import Schema

MARK = "hinoki_field"
SNAPSHOT = "hinoki_snapshot"
PLACEHOLDER = "{model}"


def own_annotations(cls: type) -> dict[str, Any]:
    """The annotations written in ``cls``'s own body, on every supported
    Python. Since 3.14 they are evaluated lazily and are no longer an entry of
    ``cls.__dict__``; a name that cannot be resolved yet stays a reference."""
    if sys.version_info >= (3, 14):
        return annotationlib.get_annotations(
            cls, format=annotationlib.Format.FORWARDREF
        )
    return inspect.get_annotations(cls)


def is_class_variable(kind: object) -> bool:
    if isinstance(kind, str):
        return kind.split("[", maxsplit=1)[0].rpartition(".")[2] == "ClassVar"
    return kind is ClassVar or typing.get_origin(kind) is ClassVar


def resolve_annotation(base: type, name: str, raw: object) -> object:
    """``raw`` evaluated in ``base``'s module when it is source text; the text
    itself when that fails (a name imported only for type checking)."""
    if not isinstance(raw, str):
        return raw
    holder = AnnotationHolder()
    holder.__annotations__ = {name: raw}
    module = sys.modules.get(base.__module__)
    try:
        hints = typing.get_type_hints(
            holder,
            globalns=dict(vars(module)) if module else {},
            localns=dict(vars(base)),
            include_extras=True,
        )
    except Exception:
        return raw
    return hints[name]


class AnnotationHolder:
    """Carries one annotation for ``typing.get_type_hints``."""

    __annotations__: dict[str, Any]


def field_types(base: type) -> dict[str, object]:
    """The field annotations of ``base``'s own body, each resolved in its
    module when it can be (so it still means the same in a subclass
    elsewhere). One unresolvable annotation does not affect the others."""
    found = {}
    for name, raw in own_annotations(base).items():
        kind = resolve_annotation(base, name, raw)
        if not is_class_variable(kind):
            found[name] = kind
    return found


def is_json_field(kind: object) -> bool:
    origin = get_origin(kind)
    if kind in (dict, list) or origin in (dict, list):
        return True
    if origin in (typing.Union, types.UnionType):
        return any(
            is_json_field(arg) for arg in get_args(kind) if arg is not type(None)
        )
    return False


def same_type(first: object, second: object) -> bool:
    """Whether two bases agree on a field's type. Source text that could not
    be resolved is only comparable with other source text."""
    if isinstance(first, str) != isinstance(second, str):
        return True
    return first == second


def is_model(base: type) -> TypeGuard[type[Model]]:
    return base is not Model and issubclass(base, Model)


def without_repeats(groups: list[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    return tuple(dict.fromkeys(groups))


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
    on_delete: OnDelete,
    related_name: str = "",
    unique: bool = False,
    default: Any = dataclasses.MISSING,
) -> Any:
    """Declare a column that references ``model``'s primary key.

    ``author_id: UUID = foreign_key(User, on_delete=OnDelete.RESTRICT)``

    ``on_delete`` is required and is an ``OnDelete`` member (``RESTRICT``,
    ``CASCADE``, ``SET_NULL`` or ``NO_ACTION``), so a deletion rule is always a
    decision; text such as ``"cascade"`` is refused. ``SET_NULL`` needs a
    column annotated ``| None``. The relation is
    named after the column without ``_id`` and is loaded only by
    ``select_related`` / ``prefetch_related``.
    """
    if not isinstance(on_delete, OnDelete):
        raise RegistrationError(
            "on_delete takes an OnDelete member, such as OnDelete.RESTRICT "
            "(from hinoki.orm import OnDelete)"
        )
    if getattr(model, "__abstract__", False):
        raise RegistrationError(
            f"foreign_key: {model.__name__} is abstract and has no table to "
            "reference; point at a model with table=..."
        )
    meta = FieldMeta(
        unique=unique,
        references=model,
        on_delete=on_delete.value,
        related_name=related_name,
    )
    return dataclasses.field(default=default, metadata={MARK: meta})


MIN_UNIQUE = 2


@dataclass_transform(kw_only_default=True, field_specifiers=(field,))
class Model:
    """Base class: ``class Post(Model, table="posts"): ...``."""

    __table__: ClassVar[str] = ""
    __columns__: ClassVar[tuple[str, ...]] = ()
    __json_columns__: ClassVar[tuple[str, ...]] = ()
    __primary_key__: ClassVar[str] = ""
    __relations__: ClassVar[dict[str, Relation]] = {}
    __unique__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __indexes__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __ordering__: ClassVar[tuple[str, ...]] = ()
    __label__: ClassVar[str] = ""
    __plural__: ClassVar[str] = ""
    __updated_at__: ClassVar[str | None] = None
    __clock__: ClassVar[Clock] = utc_now
    __abstract__: ClassVar[bool] = False
    __inherited_options__: ClassVar[frozenset[str]] = frozenset()
    NotFound: ClassVar[type[ObjectNotFound]] = ObjectNotFound

    def __init_subclass__(
        cls,
        table: str = "",
        unique: Sequence[Sequence[str]] = (),
        label: str = "",
        plural: str = "",
        abstract: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if abstract and table:
            raise RegistrationError(
                f"{cls.__name__}: an abstract model has no table; remove table=... "
                "or abstract=True"
            )
        if not abstract and not table:
            raise RegistrationError(
                f"{cls.__name__}: table=... is required (or abstract=True for a "
                "base class that only carries fields and options)"
            )
        cls.check_field_conflicts()
        cls.adopt_mixin_fields()
        dataclasses.dataclass(kw_only=True)(cls)
        fields = dataclasses.fields(cast(Any, cls))
        keys = [f.name for f in fields if f.metadata.get(MARK, FieldMeta()).primary_key]
        if len(keys) > 1 or (not abstract and not keys):
            raise RegistrationError(f"{cls.__name__}: exactly one primary_key field")
        cls.__abstract__ = abstract
        cls.__table__ = table
        cls.__columns__ = tuple(f.name for f in fields)
        annotations = {
            name: kind
            for base in reversed(cls.__mro__)
            for name, kind in field_types(base).items()
        }
        cls.__json_columns__ = tuple(
            name for name in cls.__columns__ if is_json_field(annotations.get(name))
        )
        cls.__primary_key__ = keys[0] if keys else ""
        cls.check_names(cls.__columns__)
        cls.inherit_options(unique)
        cls.__label__ = label
        cls.__plural__ = plural
        cls.__relations__ = {
            name: relation
            for base in cls.__mro__[1:]
            for name, relation in getattr(base, "__relations__", {}).items()
            if relation.forward
        }
        if not abstract:  # an abstract model attaches nothing to other models
            cls.register_relations(fields)
        cls.NotFound = type(
            "NotFound", (ObjectNotFound,), {"__qualname__": f"{cls.__name__}.NotFound"}
        )

    @classmethod
    def check_field_conflicts(cls) -> None:
        """Two bases may not give one field name different types: which one the
        model would get depends on base order, and the other would be lost. A
        base that overrides its own parent, or the class redeclaring the field,
        is a decision and is accepted."""
        own = own_annotations(cls)
        seen: dict[str, tuple[type, object]] = {}
        for base in cls.__mro__[1:]:
            if base is Model or base is object:
                continue
            for name, kind in field_types(base).items():
                first, first_kind = seen.setdefault(name, (base, kind))
                if (
                    name in own
                    or same_type(first_kind, kind)
                    or issubclass(first, base)
                ):
                    continue
                raise RegistrationError(
                    f"{cls.__name__}: field {name!r} is {first_kind!r} in "
                    f"{first.__name__} and {kind!r} in {base.__name__}; keep one, "
                    f"or declare {name!r} on {cls.__name__} itself"
                )

    @classmethod
    def adopt_mixin_fields(cls) -> None:
        """Field annotations of plain (non-model, non-dataclass) bases become
        fields of this class. ``dataclasses`` reads only a class's own
        annotations and its dataclass bases, so without this a mixin's fields
        would be dropped without a word."""
        own = own_annotations(cls)
        adopted: dict[str, object] = {}
        decided: set[str] = set(own)
        for base in cls.__mro__[1:]:
            if base is Model or base is object:
                continue
            plain = not (is_model(base) or dataclasses.is_dataclass(base))
            for name, kind in field_types(base).items():
                if name in decided:
                    continue  # an earlier base in the MRO (or the class) declares it
                decided.add(name)
                if not plain:
                    continue  # dataclasses inherits this one by itself
                adopted[name] = kind
                default = base.__dict__.get(name, dataclasses.MISSING)
                if default is not dataclasses.MISSING:
                    # Each model owns its field object: dataclasses rewrites it.
                    setattr(cls, name, copy.copy(default))
        if adopted:
            cls.__annotations__ = {**adopted, **own}

    @classmethod
    def option_bases(cls) -> list[type[Model]]:
        """The abstract models this class is declared on. Each already holds
        what it inherited and what it replaced, so nothing further up is read
        again; a concrete parent keeps its options to itself."""
        return [base for base in cls.__bases__ if is_model(base) and base.__abstract__]

    @classmethod
    def inherit_options(cls, unique: Sequence[Sequence[str]]) -> None:
        """Options come from the abstract bases the class lists: every such
        base's constraints and indexes, and the first one that sets an order or
        a timestamp column. A decorator applied to this class later replaces what
        was inherited for its kind (see ``hinoki.orm.options``)."""
        bases = cls.option_bases()
        cls.__unique__ = without_repeats([g for b in bases for g in b.__unique__])
        cls.__indexes__ = without_repeats([g for b in bases for g in b.__indexes__])
        cls.__ordering__ = next((b.__ordering__ for b in bases if b.__ordering__), ())
        timed = next((b for b in bases if b.__updated_at__ is not None), None)
        cls.__updated_at__ = timed.__updated_at__ if timed else None
        cls.__clock__ = timed.__clock__ if timed else utc_now
        inherited = {
            "unique": bool(cls.__unique__),
            "indexes": bool(cls.__indexes__),
            "ordering": bool(cls.__ordering__),
            "timestamps": timed is not None,
        }
        if unique:
            cls.__unique__ = cls.check_unique(unique)
            inherited["unique"] = False
        cls.__inherited_options__ = frozenset(k for k, v in inherited.items() if v)

    @classmethod
    def replace_inherited(cls, kind: str) -> bool:
        """True the first time an option of ``kind`` is applied to a class that
        inherited that kind: the caller then starts from nothing."""
        if kind not in cls.__inherited_options__:
            return False
        cls.__inherited_options__ = cls.__inherited_options__ - {kind}
        return True

    @classmethod
    def concrete(cls) -> type[Self]:
        """This class, refusing an abstract model: it has no table to query."""
        if cls.__abstract__:
            raise ModelError(
                f"{cls.__name__} is abstract and has no table; query or save a "
                "model that subclasses it with table=..."
            )
        return cls

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
    def check_set_null(cls, column: dataclasses.Field[Any], meta: FieldMeta) -> None:
        """``set_null`` on a column that cannot hold NULL fails every delete of
        the referenced row, so it is refused when the model is declared. An
        annotation that cannot be resolved yet is left to the migration plan."""
        if meta.on_delete != OnDelete.SET_NULL.value:
            return
        hint = resolve_annotation(cls, column.name, column.type)
        if isinstance(hint, str) or type(None) in typing.get_args(hint):
            return
        raise RegistrationError(
            f"{cls.__name__}.{column.name}: on_delete=OnDelete.SET_NULL needs a column "
            "that can hold NULL; annotate it `<type> | None`"
        )

    @classmethod
    def register_relations(cls, fields: tuple[dataclasses.Field[Any], ...]) -> None:
        """Foreign keys this class declares or takes from an abstract base or a
        mixin (those of a concrete parent come with it). Every check runs
        before anything is attached, so a failing definition leaves the
        referenced models untouched."""
        own = own_annotations(cls)
        parents = [b for b in cls.__mro__[1:] if is_model(b) and not b.__abstract__]
        copied = {name for base in parents for name in base.__columns__} - set(own)
        pending: list[tuple[type[Model], Relation]] = []
        for column in fields:
            meta: FieldMeta = column.metadata.get(MARK, FieldMeta())
            if meta.references is None:
                continue
            cls.check_set_null(column, meta)
            if column.name in copied:
                # The parent owns a fixed reverse name; a placeholder gives
                # this model one of its own.
                if PLACEHOLDER in meta.related_name:
                    back_name = cls.reverse_name(column.name, meta.related_name)
                    back = Relation(back_name, cls, column.name, False)
                    pending.append((meta.references, back))
                continue
            name = column.name.removesuffix("_id")
            if name == column.name:
                raise RegistrationError(
                    f"{cls.__name__}.{column.name}: foreign key columns end in _id"
                )
            pending.append((cls, Relation(name, meta.references, column.name, True)))
            if meta.related_name:
                back_name = cls.reverse_name(column.name, meta.related_name)
                back = Relation(back_name, cls, column.name, False)
                pending.append((meta.references, back))
        for owner, relation in pending:
            owner.check_attach(relation.name, relation)
        for owner, relation in pending:
            owner.attach(relation.name, relation)

    @classmethod
    def reverse_name(cls, column: str, related_name: str) -> str:
        """``related_name`` with ``{model}`` replaced by this class's name in
        lower case, so one declaration in an abstract base or a mixin gives
        each concrete model its own reverse relation."""
        name = related_name.replace(PLACEHOLDER, cls.__name__.lower())
        if "{" in name or "}" in name:
            raise RegistrationError(
                f"{cls.__name__}.{column}: related_name={related_name!r} may only "
                f"use the {PLACEHOLDER} placeholder"
            )
        return name

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
        if (
            taken
            and existing is not None
            and not (existing.forward or relation.forward)
        ):
            raise RegistrationError(
                f"{cls.__name__} already has a reverse relation {name!r} from "
                f"{existing.model.__name__}.{existing.column}; "
                f"{relation.model.__name__}.{relation.column} needs another "
                f"related_name (in an abstract base or a mixin, use the "
                f"{PLACEHOLDER} placeholder, for example '{PLACEHOLDER}_set')"
            )
        if taken:
            raise RegistrationError(f"{cls.__name__} already has an attribute {name!r}")

    @classmethod
    def attach(cls, name: str, relation: Relation) -> None:
        cls.__relations__[name] = relation
        setattr(cls, name, Related(name, relation))

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        values = row
        if cls.__json_columns__:
            for name in cls.__json_columns__:
                if name not in values:
                    continue
                value = values[name]
                if not isinstance(value, (str, bytes, bytearray)):
                    continue
                try:
                    decoded = json.loads(value)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise FieldError(
                        f"{cls.__name__}.{name}: database value is not valid JSON"
                    ) from error
                if values is row:
                    values = dict(row)
                values[name] = decoded
        instance = cls(**values)
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
        cls = type(self).concrete()
        assignments = [(c, getattr(self, c)) for c in cls.__columns__]
        database = current_database()
        returning = (
            [] if database.dialect in {"mysql", "mariadb"} else list(cls.__columns__)
        )
        sql, params = core.compile_query(
            "insert",
            cls.__table__,
            assignments=assignments,
            returning=returning,
            dialect=database.dialect,
        )
        if database.dialect in {"mysql", "mariadb"}:
            # MySQL has no INSERT ... RETURNING. Read the row by its already
            # materialized Hinoki primary key inside the same transaction.
            async with database.transaction():
                await database.execute(sql, *params)
                select_sql, select_params = core.compile_query(
                    "select",
                    cls.__table__,
                    columns=list(cls.__columns__),
                    filters=[
                        (
                            cls.__primary_key__,
                            "eq",
                            [getattr(self, cls.__primary_key__)],
                        )
                    ],
                    dialect=database.dialect,
                )
                rows = await database.fetch(select_sql, *select_params)
        else:
            rows = await database.fetch(sql, *params)
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
        return QuerySet(cls.concrete()).filter(**conditions)

    @classmethod
    def all(cls) -> QuerySet[Self]:
        return QuerySet(cls.concrete())

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
        return await QuerySet(cls.concrete()).filter(**conditions).aupdate(values)

    @classmethod
    async def adelete(cls, **conditions: Any) -> int:
        """Delete matching rows; returns how many. An empty condition set is
        refused so a missing filter cannot wipe the table."""
        if not conditions:
            raise FieldError("adelete needs at least one condition")
        return await QuerySet(cls.concrete()).filter(**conditions).adelete()

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
