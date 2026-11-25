"""``Model``, ``field``, ``pk`` and ``foreign_key``: single-table Active Record models.

Typing note: construction (``Post(title=...)``) is checked statically;
keyword-style helpers such as ``Post.acreate(**values)`` are not (see
docs/typing.md) until generated stubs exist.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import datetime
import functools
import inspect
import re
import sys
import types
import typing
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from decimal import Decimal
from fractions import Fraction
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Self,
    TypeGuard,
    cast,
    dataclass_transform,
    get_args,
    get_origin,
    overload,
)
from uuid import UUID, uuid4

from hinoki import core, events
from hinoki.blocking import refuse_event_loop, run_sync
from hinoki.db import Database
from hinoki.errors import (
    ErrorDetail,
    FieldError,
    ModelError,
    ModelValidationError,
    ObjectNotFound,
    RegistrationError,
)
from hinoki.fields import UNSET, FieldInfo
from hinoki.fieldtypes import (
    MAX_STEP_EXPONENT,
    TEXT_STORED,
    FieldType,
    normalize_text,
    parse_annotation,
    split_annotated,
)
from hinoki.orm.columntypes import unstorable
from hinoki.orm.context import current_database
from hinoki.orm.naming import resolve_model_names
from hinoki.orm.queryset import (
    QuerySet,
    _projection_hooks,
    delete_keys,
    write_statement,
    write_transaction,
    written_rows,
)
from hinoki.orm.relations import LinkEnd, OnDelete, Related, Relation
from hinoki.utils import Clock, _read_utc, utc_now, uuid7

if sys.version_info >= (3, 14):
    import annotationlib

if TYPE_CHECKING:
    from hinoki.events import EventBus
    from hinoki.schema import Schema

MARK = "hinoki_field"
SNAPSHOT = "hinoki_snapshot"
# Keyword-only options of the ``**values`` write methods (``acreate``,
# ``aupdate``): a column with one of these names could not be passed to them.
WRITE_OPTIONS = frozenset({"returning"})
# The model methods ``insert`` and ``asave`` call around a write, in this order.
HOOKS = ("before_save", "before_create", "after_create", "after_save")
# Values that cannot change in place, so a snapshot may keep the same object.
_IMMUTABLE = frozenset(
    {
        str,
        int,
        float,
        bool,
        bytes,
        type(None),
        UUID,
        datetime.datetime,
        datetime.date,
        datetime.time,
        datetime.timedelta,
        Decimal,
        *TEXT_STORED.values(),
    }
)
PLACEHOLDER = "{model}"
DICT_GENERIC_ARGS = 2


@dataclasses.dataclass(frozen=True, slots=True)
class StatusTrack:
    """A status column and the datetime column recording its last change."""

    field: str
    changed_at: str
    clock: Clock | None = None


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
    if kind in (dict, list, set, frozenset, tuple) or origin in (
        dict,
        list,
        set,
        frozenset,
        tuple,
    ):
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


def _constraint_passes(rule: str, value: Any, bound: int | float) -> bool:
    if rule == "ge":
        return bool(value >= bound)
    if rule == "le":
        return bool(value <= bound)
    if rule == "gt":
        return bool(value > bound)
    return bool(value < bound)


def _read_decimal(text: Any) -> Decimal:
    if not isinstance(text, str):
        raise TypeError("a Decimal column holds text")
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError("not finite")
    return value


def _read_duration(micros: Any) -> datetime.timedelta:
    if not isinstance(micros, int) or isinstance(micros, bool):
        raise TypeError("a timedelta column holds whole microseconds")
    return datetime.timedelta(microseconds=micros)


# Readers for the columns that store a plain type: date, time and Decimal as
# text, timedelta as microseconds.
STORED_READERS: dict[str, Callable[[Any], Any]] = {
    "date": datetime.date.fromisoformat,
    "time": datetime.time.fromisoformat,
    "decimal": _read_decimal,
    "timedelta": _read_duration,
}
READ_ERRORS = (ValueError, TypeError, ArithmeticError, OverflowError)


def column_reader(field_type: FieldType) -> tuple[str, Callable[[Any], Any]] | None:
    """``(label, reader)`` turning a stored value back into the annotated type
    (an Enum member, an address, a date), or ``None`` for a column read as is."""
    if field_type.enum_class is not None:
        return field_type.enum_class.__name__, field_type.enum_class
    if field_type.kind in TEXT_STORED:
        stored = TEXT_STORED[field_type.kind]
        return stored.__name__, stored
    if field_type.kind in STORED_READERS:
        return field_type.kind, STORED_READERS[field_type.kind]
    return None


def _time_problem(value: Any) -> str | None:
    return "A time column does not take a timezone." if value.tzinfo else None


def _date_problem(value: Any) -> str | None:
    if isinstance(value, datetime.datetime):
        return "Expected a date, not a datetime."
    return None


def _decimal_problem(value: Any) -> str | None:
    return None if value.is_finite() else "A Decimal column needs a finite value."


def _duration_problem(value: Any) -> str | None:
    micros = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
    if -(2**63) <= micros < 2**63:
        return None
    return "The duration is out of range for a 64-bit column."


STORED_PROBLEMS: dict[str, Callable[[Any], str | None]] = {
    "date": _date_problem,
    "time": _time_problem,
    "decimal": _decimal_problem,
    "timedelta": _duration_problem,
}


def stored_value_problem(kind: str, value: Any) -> str | None:
    """Why a value of the right type still cannot be stored, or ``None``."""
    check = STORED_PROBLEMS.get(kind)
    return None if check is None else check(value)


def _validate_field_value(
    name: str,
    value: Any,
    field_type: FieldType,
    annotation: object,
    validators: tuple[Callable[..., None], ...],
    problems: list[ErrorDetail],
) -> None:
    if not _matches_model_annotation(value, annotation):
        problems.append(
            ErrorDetail(
                name, "type", f"Expected a value compatible with {annotation!r}."
            )
        )
        return
    if field_type.enum_class is None and (
        reason := stored_value_problem(field_type.kind, value)
    ):
        problems.append(ErrorDetail(name, "type", reason))
        return
    info = field_type.info
    if field_type.enum_class is not None:
        # the instance check above already limits the value to the members
        info = dataclasses.replace(info, one_of=None)
    _validate_lengths(name, value, info, problems)
    _validate_pattern(name, value, info, problems)
    _validate_bounds(name, value, info, problems)
    _validate_custom_and_choices(name, value, info, validators, problems)


def _matches_model_annotation(value: Any, annotation: object) -> bool:
    if isinstance(annotation, str):
        matched = True
    else:
        base, _ = split_annotated(annotation)
        origin = typing.get_origin(base)
        args = typing.get_args(base)
        if origin in {typing.Union, types.UnionType}:
            matched = any(_matches_model_annotation(value, part) for part in args)
        elif origin is list or base is list:
            matched = _matches_model_list(value, args)
        elif origin is dict or base is dict:
            matched = _matches_model_dict(value, args)
        else:
            matched = _matches_model_scalar(value, base)
    return matched


def _matches_model_list(value: Any, args: tuple[Any, ...]) -> bool:
    return isinstance(value, list) and (
        not args or all(_matches_model_annotation(item, args[0]) for item in value)
    )


def _matches_model_dict(value: Any, args: tuple[Any, ...]) -> bool:
    return isinstance(value, dict) and (
        len(args) != DICT_GENERIC_ARGS
        or all(
            _matches_model_annotation(key, args[0])
            and _matches_model_annotation(item, args[1])
            for key, item in value.items()
        )
    )


def _matches_model_scalar(value: Any, base: Any) -> bool:
    if base in {Any, object}:
        return True
    if base is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if base is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return not isinstance(base, type) or isinstance(value, base)


def _length_message(
    value: str | list[Any] | tuple[Any, ...], comparison: str, bound: int
) -> str:
    unit = "character" if isinstance(value, str) else "item"
    plural = "" if bound == 1 else "s"
    return f"Must contain {comparison} {bound} {unit}{plural}."


def _validate_lengths(
    name: str, value: Any, info: FieldInfo, problems: list[ErrorDetail]
) -> None:
    if not isinstance(value, str | list | tuple):
        return
    for rule in ("min_length", "max_length"):
        bound = getattr(info, rule)
        if bound is None:
            continue
        failed = len(value) < bound if rule == "min_length" else len(value) > bound
        if failed:
            comparison = "at least" if rule == "min_length" else "at most"
            problems.append(
                ErrorDetail(name, rule, _length_message(value, comparison, bound))
            )


def _validate_pattern(
    name: str, value: Any, info: FieldInfo, problems: list[ErrorDetail]
) -> None:
    if (
        info.pattern is not None
        and isinstance(value, str)
        and re.fullmatch(info.pattern, value) is None
    ):
        problems.append(
            ErrorDetail(name, "pattern", "Does not match the required pattern.")
        )


def _validate_bounds(
    name: str, value: Any, info: FieldInfo, problems: list[ErrorDetail]
) -> None:
    if not isinstance(value, int | float | Decimal) or isinstance(value, bool):
        return
    if isinstance(value, Decimal) and not value.is_finite():
        problems.append(ErrorDetail(name, "type", "Expected a finite number."))
        return
    for rule in ("ge", "le", "gt", "lt"):
        bound = getattr(info, rule)
        if bound is not None and not _constraint_passes(rule, value, bound):
            problems.append(ErrorDetail(name, rule, f"Must satisfy {rule} {bound}."))
    step = info.multiple_of
    if (
        step is not None
        and not isinstance(value, float)
        and not _is_multiple(value, step)
    ):
        problems.append(
            ErrorDetail(name, "multiple_of", f"Must be a multiple of {step}.")
        )


def _is_multiple(value: int | Decimal, step: int | Decimal) -> bool:
    """Exact: ``Decimal`` arithmetic rounds to the context precision. A value
    whose exponent is out of the range a step may have is never a multiple of
    one (zero aside), which also keeps the exact arithmetic small."""
    if not value:
        return True
    if isinstance(value, Decimal) and abs(value.adjusted()) > MAX_STEP_EXPONENT:
        return False
    return Fraction(value) % Fraction(step) == 0


def _validate_custom_and_choices(
    name: str,
    value: Any,
    info: FieldInfo,
    validators: tuple[Callable[..., None], ...],
    problems: list[ErrorDetail],
) -> None:
    if info.one_of is not None and value not in info.one_of:
        problems.append(
            ErrorDetail(name, "one_of", "Value is not one of the allowed values.")
        )
    for validator in validators:
        try:
            result = validator(value)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                elif isinstance(result, asyncio.Future):
                    result.cancel()
                else:
                    _stop_custom_awaitable(result)
                raise TypeError("Model validators must be synchronous")
        except ValueError as error:
            problems.append(
                ErrorDetail(name, "invalid", str(error) or "Invalid value.")
            )


def _stop_custom_awaitable(result: Any) -> None:
    cancel = getattr(type(result), "cancel", None)
    if callable(cancel):
        cancel(result)
    close = getattr(type(result), "close", None)
    if callable(close):
        close(result)


def holds_mapping(field_type: FieldType) -> bool:
    """Whether a field type is a dict or holds one in its list items."""
    if field_type.kind == "dict":
        return True
    return field_type.item is not None and holds_mapping(field_type.item)


def stays_json(parsed: FieldType, values: dict[str, Any]) -> bool:
    """Whether a parsed column annotation is stored as JSON: a dict, or a list
    of dicts, an Enum or a Literal (a plain ``one_of`` constraint is not), or a
    set or tuple."""
    listed = (
        parsed.info.one_of is not None
        and "one_of" not in values
        and parsed.enum_class is None
    )
    grouped = parsed.kind in {"tuple", "anyof", "any"} or parsed.collection is not None
    return listed or grouped or holds_mapping(parsed)


def resolve_model_field_type(
    cls: type[Model], name: str, hint: object, meta: FieldMeta
) -> FieldType:
    base, annotated = split_annotated(hint)
    if annotated.alias is not None or meta.validation.alias is not None:
        raise RegistrationError(
            f"{cls.__name__}.{name}: aliases are supported on Schema fields, "
            "not Model columns"
        )
    values = {**annotated.constraints(), **meta.validation.constraints()}
    info = FieldInfo(**cast(Any, values))
    try:
        parsed = parse_annotation(
            typing.Annotated[base, info], f"{cls.__name__}.{name}"
        )
    except RegistrationError:
        if values:
            raise
    else:
        if parsed.kind == "bytes" and values:
            raise RegistrationError(
                f"{cls.__name__}.{name}: constraints do not apply to a bytes column"
            )
        if not stays_json(parsed, values):
            return parsed
        # A column holding a dict (also inside lists) stays a JSON column: Model
        # writes do not validate it as a Schema dict field would. So do the
        # ``Enum`` and ``Literal`` annotations, which only Schema fields check.
        if values:
            raise RegistrationError(
                f"{cls.__name__}.{name}: constraints do not apply to a JSON column"
            )
    members = (
        typing.get_args(base)
        if typing.get_origin(base) in {typing.Union, types.UnionType}
        else ()
    )
    return FieldType("json", nullable=type(None) in members)


def check_storable(
    cls: type[Model], column: dataclasses.Field[Any], hint: object
) -> None:
    """Refuse a column no migration can create. An annotation that is still
    text (imported only for type checking) is left to the migration planner,
    and a ``many_to_many`` without ``ClassVar`` to the link registration."""
    if isinstance(hint, str) or getattr(column.default, "__hinoki_link__", False):
        return
    if reason := unstorable(hint):
        raise RegistrationError(f"{cls.__name__}.{column.name}: {reason}")


def check_model_validators(
    cls: type[Model], name: str, validators: tuple[Callable[..., None], ...]
) -> None:
    for validator in validators:
        if not callable(validator):
            raise RegistrationError(
                f"{cls.__name__}.{name}: each Model validator must be callable"
            )
        callback = type(validator).__call__
        async_callback = inspect.iscoroutinefunction(validator) or (
            inspect.iscoroutinefunction(callback)
        )
        if async_callback:
            raise RegistrationError(
                f"{cls.__name__}.{name}: Model validators must be synchronous"
            )


@dataclasses.dataclass(frozen=True, slots=True)
class FieldMeta:
    primary_key: bool = False
    unique: bool = False
    max_length: int | None = None
    references: type[Model] | None = None
    on_delete: str = ""
    related_name: str = ""
    validation: FieldInfo = dataclasses.field(default_factory=FieldInfo)
    validators: tuple[Callable[..., None], ...] = ()
    index: bool = False


def field(  # noqa: PLR0913 - each keyword is an independent public field option
    *,
    primary_key: bool = False,
    unique: bool = False,
    index: bool = False,
    max_length: int | None = None,
    constraints: FieldInfo | None = None,
    validators: Sequence[Callable[..., None]] = (),
    default: Any = dataclasses.MISSING,
    default_factory: Callable[[], Any] | Any = dataclasses.MISSING,
) -> Any:
    """Declare a column and, optionally, write-time constraints and validators.

    ``id: UUID = field(primary_key=True, default_factory=uuid4)``

    ``name: str = field(constraints=Field(min_length=1), validators=(check_name,))``

    ``created_at: datetime = field(index=True, default_factory=utc_now)`` adds a
    single-column index, the same index as ``@db.indexed("created_at")``.

    Constraints run before ORM writes. Custom validators are synchronous and
    raise ``ValueError`` to refuse a value; they must not perform I/O.
    """
    if type(index) is not bool:
        raise RegistrationError("field(index=...) must be True or False")
    if index and (primary_key or unique):
        raise RegistrationError(
            "field(index=True) is redundant: a primary key or unique column "
            "already has an index"
        )
    if constraints is not None and not isinstance(constraints, FieldInfo):
        raise RegistrationError("field constraints must be a Field(...) value")
    declared = constraints or FieldInfo()
    if declared.alias is not None:
        raise RegistrationError("Field(alias=...) is supported on Schemas, not Models")
    if max_length is not None:
        if declared.max_length is not None and declared.max_length != max_length:
            raise RegistrationError(
                "field max_length conflicts with constraints.max_length"
            )
        declared = dataclasses.replace(declared, max_length=max_length)
    meta = FieldMeta(
        primary_key,
        unique,
        declared.max_length,
        validation=declared,
        validators=tuple(validators),
        index=index,
    )
    return dataclasses.field(
        default=default, default_factory=default_factory, metadata={MARK: meta}
    )


def pk(
    *,
    max_length: int | None = None,
    constraints: FieldInfo | None = None,
    validators: Sequence[Callable[..., None]] = (),
    default: Any = dataclasses.MISSING,
    default_factory: Callable[[], Any] | Any = dataclasses.MISSING,
) -> Any:
    """Declare the primary key: shorthand for ``field(primary_key=True, ...)``.

    ``id: UUID = pk(default_factory=uuid4)``

    It takes the options of ``field`` that make sense for a key; ``unique``
    and ``index`` are implied by the primary key.
    """
    return field(
        primary_key=True,
        max_length=max_length,
        constraints=constraints,
        validators=validators,
        default=default,
        default_factory=default_factory,
    )


URLSAFE_PATTERN = r"^[A-Za-z0-9_-]+$"
MAX_URLSAFE_BYTES = 1024
UUID_FACTORIES: dict[int, Callable[[], UUID]] = {4: uuid4, 7: uuid7}


def uuid_pk(
    *,
    version: Literal[4, 7] | None = None,
    default_factory: Callable[[], UUID] | None = None,
    constraints: FieldInfo | None = None,
    validators: Sequence[Callable[..., None]] = (),
) -> Any:
    """Declare a UUID primary key.

    Defaults to ``uuid4``. Pass ``version=7`` for time-ordered RFC 9562 UUIDv7,
    or ``default_factory=...`` for any custom UUID generator.
    """
    if version is not None and default_factory is not None:
        raise RegistrationError(
            "uuid_pk() accepts either version or default_factory, not both"
        )
    if default_factory is not None:
        if not callable(default_factory):
            raise RegistrationError("uuid_pk(default_factory=...) must be callable")
        factory: Callable[[], UUID] = default_factory
    else:
        selected = 4 if version is None else version
        if type(selected) is not int or selected not in UUID_FACTORIES:
            raise RegistrationError(
                f"uuid_pk(version=...) must be 4 or 7, got {version!r}"
            )
        factory = UUID_FACTORIES[selected]
    return pk(
        constraints=constraints,
        validators=validators,
        default_factory=factory,
    )


def urlsafe_token(
    *,
    nbytes: int = 32,
    unique: bool = True,
    index: bool = False,
    max_length: int | None = None,
    constraints: FieldInfo | None = None,
    validators: Sequence[Callable[..., None]] = (),
) -> Any:
    """Declare a URL-safe random token column backed by Rust's CSPRNG
    (``hinoki.core.make_token``).

    Suitable for opaque public identifiers or share codes. For authentication
    secrets, store a hash instead of the raw token.
    """
    if type(nbytes) is not int or not (1 <= nbytes <= MAX_URLSAFE_BYTES):
        raise RegistrationError(
            f"urlsafe_token(nbytes=...) must be an int between 1 and "
            f"{MAX_URLSAFE_BYTES}"
        )
    generated_length = (nbytes * 4 + 2) // 3
    declared = constraints or FieldInfo()
    effective_max = (
        max_length
        if max_length is not None
        else (
            declared.max_length if declared.max_length is not None else generated_length
        )
    )
    if type(effective_max) is not int or effective_max < generated_length:
        raise RegistrationError(
            f"urlsafe_token(max_length=...) must be at least {generated_length} "
            f"for nbytes={nbytes}"
        )
    if declared.pattern is not None and declared.pattern != URLSAFE_PATTERN:
        raise RegistrationError(
            "urlsafe_token() cannot override the URL-safe character pattern"
        )
    merged = dataclasses.replace(
        declared,
        min_length=1 if declared.min_length is None else declared.min_length,
        pattern=URLSAFE_PATTERN,
    )
    return field(
        unique=unique,
        index=index,
        max_length=effective_max if declared.max_length is None else max_length,
        constraints=merged,
        validators=validators,
        default_factory=functools.partial(core.make_token, nbytes),
    )


def foreign_key(
    model: type[Model],
    *,
    on_delete: OnDelete,
    related_name: str = "",
    unique: bool = False,
    validators: Sequence[Callable[..., None]] = (),
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
        validators=tuple(validators),
    )
    return dataclasses.field(default=default, metadata={MARK: meta})


def run_bulk[T](write: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run a sync bulk method's writes in one transaction from a sync handler's
    thread. With none open, the scope opens in the task that writes, so a hook
    may nest its own scope; inside an open one, the savepoint opens on the
    thread, as ``with db.atomic():`` does, because only the transaction's owner
    may nest a scope from the loop."""
    database = current_database()
    if database.active() is None:
        return run_sync(atomically(database, write))
    with database.atomic():
        return run_sync(write())


async def atomically[T](
    database: Database, write: Callable[[], Coroutine[Any, Any, T]]
) -> T:
    async with database.atomic():
        return await write()


MIN_UNIQUE = 2


@dataclasses.dataclass(frozen=True, slots=True)
class Change:
    """One column's stored and current value; ``old`` is ``UNSET`` for a row
    that has not been written yet."""

    old: Any
    new: Any


@dataclass_transform(kw_only_default=True, field_specifiers=(field, pk, foreign_key))
class Model:
    """Base class: ``class Post(Model): ...`` with an optional ``table=`` override."""

    __table__: ClassVar[str] = ""
    __table_is_implicit__: ClassVar[bool] = False
    __table_decorated__: ClassVar[bool] = False
    __table_problem__: ClassVar[str] = ""
    __columns__: ClassVar[tuple[str, ...]] = ()
    __json_columns__: ClassVar[tuple[str, ...]] = ()
    __column_readers__: ClassVar[dict[str, tuple[str, Callable[[Any], Any]]]] = {}
    __row_plan__: ClassVar[core._ModelRows | None]
    __field_types__: ClassVar[dict[str, FieldType]] = {}
    __field_validators__: ClassVar[dict[str, tuple[Callable[..., None], ...]]] = {}
    __field_annotations__: ClassVar[dict[str, object]] = {}
    __field_validation_fields__: ClassVar[frozenset[str]] = frozenset()
    __primary_key__: ClassVar[str] = ""
    __relations__: ClassVar[dict[str, Relation]] = {}
    __unique__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __unique_names__: ClassVar[dict[tuple[str, ...], str]] = {}
    __indexes__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __field_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __decorated_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __ordering__: ClassVar[tuple[str, ...]] = ()
    __label__: ClassVar[str] = ""
    __plural__: ClassVar[str] = ""
    __updated_at__: ClassVar[str | None] = None
    __clock__: ClassVar[Clock] = utc_now
    __soft_delete__: ClassVar[str | None] = None
    __soft_delete_clock__: ClassVar[Clock | None] = None
    __status_tracks__: ClassVar[tuple[StatusTrack, ...]] = ()
    __abstract__: ClassVar[bool] = False
    __inherited_options__: ClassVar[frozenset[str]] = frozenset()
    NotFound: ClassVar[type[ObjectNotFound]] = ObjectNotFound
    __links__: ClassVar[dict[str, LinkEnd]] = {}
    __bridges__: ClassVar[tuple[type[Model], ...]] = ()

    def __init_subclass__(
        cls,
        table: str = "",
        unique: Sequence[Sequence[str]] = (),
        label: str = "",
        plural: str = "",
        abstract: bool = False,
        app: str = "",
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        table, label, plural, implicit_table, problem = resolve_model_names(
            cls.__name__,
            table,
            label,
            plural,
            abstract=abstract,
            module=cls.__module__,
            app=app,
        )
        cls.__table_is_implicit__ = implicit_table
        cls.__table_decorated__ = False
        # A derived name that cannot be used is reported only if no @db.table
        # names the table: the decorator runs after this method.
        cls.__table_problem__ = problem
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
        cls.__field_types__ = cls.check_field_validations(fields, annotations)
        if keys and cls.__field_types__[keys[0]].nullable:
            raise RegistrationError(
                f"{cls.__name__}: the primary key cannot be nullable"
            )
        cls.__field_annotations__ = annotations
        cls.__field_validators__ = {
            item.name: item.metadata.get(MARK, FieldMeta()).validators
            for item in fields
        }
        cls.__column_readers__ = {
            name: reader
            for name, field_type in cls.__field_types__.items()
            if (reader := column_reader(field_type)) is not None
        }
        cls.__field_validation_fields__ = frozenset(
            name
            for name, field_type in cls.__field_types__.items()
            if field_type.info.constraints()
            or cls.__field_validators__[name]
            or name in cls.__column_readers__
        )
        cls.__primary_key__ = keys[0] if keys else ""
        cls.check_names(cls.__columns__)
        cls.__field_indexes__ = tuple(
            (item.name,)
            for item in fields
            if item.metadata.get(MARK, FieldMeta()).index
        )
        cls.__decorated_indexes__ = ()
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
        cls.__links__ = {}
        cls.__bridges__ = ()
        # Imported here: links.py builds Models and imports this module.
        from hinoki.orm.links import bind  # noqa: PLC0415 - circular, see above

        bind(cls, abstract=abstract)

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
    def check_field_validations(
        cls, columns: tuple[dataclasses.Field[Any], ...], annotations: dict[str, object]
    ) -> dict[str, FieldType]:
        """Validate constraints against annotations, refuse a column type no
        migration can create and reject async callbacks."""
        found = {}
        for column in columns:
            meta: FieldMeta = column.metadata.get(MARK, FieldMeta())
            hint = resolve_annotation(
                cls, column.name, annotations.get(column.name, column.type)
            )
            check_storable(cls, column, hint)
            found[column.name] = resolve_model_field_type(cls, column.name, hint, meta)
            check_model_validators(cls, column.name, meta.validators)
        return found

    @classmethod
    def validate_values(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Run declared field checks on values about to be written and return
        them as they are stored: ``strip``, ``lower`` and ``upper`` applied."""
        if not cls.__field_validation_fields__:
            return values
        problems: list[ErrorDetail] = []
        values = dict(values)
        checked = cls.__field_validation_fields__
        for name in [n for n in values if n in checked]:
            field_type = cls.__field_types__[name]
            if values[name] is None and field_type.nullable:
                continue
            if isinstance(values[name], str):
                values[name] = normalize_text(values[name], field_type.info)
            _validate_field_value(
                name,
                values[name],
                field_type,
                cls.__field_annotations__[name],
                cls.__field_validators__[name],
                problems,
            )
        if problems:
            raise ModelValidationError(problems)
        return values

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
            cls._adopt_mixin_base(base, decided, adopted)
        if adopted:
            cls.__annotations__ = {**adopted, **own}

    @classmethod
    def _adopt_mixin_base(
        cls: type[Model],
        base: type,
        decided: set[str],
        adopted: dict[str, object],
    ) -> None:
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
        was inherited for its kind (see ``hinoki.orm.options``). Indexes declared
        with ``field(index=True)`` belong to their column: they stay while the
        column keeps ``index=True`` and go when this class redeclares the column
        without it, whether or not a decorator is applied."""
        bases = cls.option_bases()
        cls.__unique__ = without_repeats([g for b in bases for g in b.__unique__])
        cls.__unique_names__ = {}  # names belong to one table, never inherited
        indexes = [g for b in bases for g in b.__indexes__ if cls.keeps_index(b, g)]
        inherited_indexes = [g for g in indexes if g not in cls.__field_indexes__]
        cls.__indexes__ = without_repeats([*indexes, *cls.__field_indexes__])
        cls.__ordering__ = next((b.__ordering__ for b in bases if b.__ordering__), ())
        timed = next((b for b in bases if b.__updated_at__ is not None), None)
        cls.__updated_at__ = timed.__updated_at__ if timed else None
        cls.__clock__ = timed.__clock__ if timed else utc_now
        deleted = next((b for b in bases if b.__soft_delete__ is not None), None)
        cls.__soft_delete__ = deleted.__soft_delete__ if deleted else None
        cls.__soft_delete_clock__ = deleted.__soft_delete_clock__ if deleted else None
        cls.__status_tracks__ = cls.inherited_status_tracks(bases)
        cls.validate_inherited_managed_columns()
        inherited = {
            "unique": bool(cls.__unique__),
            "indexes": bool(inherited_indexes),
            "ordering": bool(cls.__ordering__),
            "timestamps": timed is not None,
            "soft_delete": deleted is not None,
            "status_tracks": bool(cls.__status_tracks__),
        }
        if unique:
            cls.__unique__ = cls.check_unique(unique)
            inherited["unique"] = False
        cls.__inherited_options__ = frozenset(k for k, v in inherited.items() if v)

    @classmethod
    def validate_inherited_managed_columns(cls) -> None:
        """Reject independently inherited lifecycle options sharing a column."""
        columns = [
            column
            for column in (
                cls.__updated_at__,
                cls.__soft_delete__,
                *(track.changed_at for track in cls.__status_tracks__),
            )
            if column is not None
        ]
        if len(columns) != len(set(columns)):
            raise RegistrationError(
                f"{cls.__name__}: inherited managed datetime columns must be distinct"
            )

    @classmethod
    def inherited_status_tracks(
        cls, bases: Sequence[type[Model]]
    ) -> tuple[StatusTrack, ...]:
        """Merge independent abstract-base status tracks, rejecting collisions."""
        tracks: list[StatusTrack] = []
        for base in bases:
            for track in base.__status_tracks__:
                cls.add_inherited_track(tracks, track)
        return tuple(tracks)

    @classmethod
    def add_inherited_track(cls, tracks: list[StatusTrack], track: StatusTrack) -> None:
        """Add one track once, and reject overlapping inherited declarations."""
        collisions = [
            current
            for current in tracks
            if current.field == track.field or current.changed_at == track.changed_at
        ]
        if not collisions:
            tracks.append(track)
            return
        if collisions[0] != track:
            raise RegistrationError(
                f"{cls.__name__}: conflicting inherited status tracks"
            )

    @classmethod
    def keeps_index(cls, base: type[Model], columns: tuple[str, ...]) -> bool:
        """Whether an index of ``base`` carries over: a decorator index always
        does, a ``field(index=True)`` index only while this class's column still
        declares it."""
        if columns not in base.__field_indexes__:
            return True
        return columns in base.__decorated_indexes__ or columns in cls.__field_indexes__

    @classmethod
    def check_table(cls) -> None:
        """Raise the deferred error of a derived table name that cannot be
        used: the class name gave no portable or free name and no
        ``@db.table``/``table=`` replaced it."""
        if cls.__table_problem__:
            raise RegistrationError(cls.__table_problem__)

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
        cls.check_table()
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
        options = sorted(n for n in columns if n in WRITE_OPTIONS)
        if options:
            raise RegistrationError(
                f"{cls.__name__}: field names {options} are reserved for the "
                "keyword options of acreate/aupdate; rename the field "
                "(for example 'returns')"
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
            meta.references.check_table()
            cls.check_set_null(column, meta)
            if column.name in copied:
                # The parent owns a fixed reverse name; a placeholder gives
                # this model one of its own.
                if PLACEHOLDER in meta.related_name:
                    back_name = cls.reverse_name(column.name, meta.related_name)
                    back = Relation(back_name, cls, column.name, False, meta.unique)
                    pending.append((meta.references, back))
                continue
            name = column.name.removesuffix("_id")
            if name == column.name:
                raise RegistrationError(
                    f"{cls.__name__}.{column.name}: foreign key columns end in _id"
                )
            pending.append(
                (
                    cls,
                    Relation(name, meta.references, column.name, True, meta.unique),
                )
            )
            if meta.related_name:
                back_name = cls.reverse_name(column.name, meta.related_name)
                back = Relation(back_name, cls, column.name, False, meta.unique)
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
    def row_plan(cls) -> core._ModelRows | None:
        """The native row builder for this model, or ``None`` when its
        instances must go through ``from_row`` (a custom constructor or
        allocator, columns that need a reader, or an overridden or patched
        ``from_row``, ``remember`` or ``__post_init__``). The builder is made
        once per class; the cheap override checks run on every call."""
        if "__row_plan__" not in cls.__dict__:
            cls.__row_plan__ = cls._native_row_plan()
        stock = (
            getattr(cls.from_row, "__func__", None)
            is Model.__dict__["from_row"].__func__
            and cls.remember is Model.remember
            and not hasattr(cls, "__post_init__")
        )
        return cls.__row_plan__ if stock else None

    @classmethod
    def _native_row_plan(cls) -> core._ModelRows | None:
        # Imported here because hinoki.schema imports this module.
        from hinoki.schema import uses_generated_init  # noqa: PLC0415

        if not uses_generated_init(cls) or cls.__column_readers__:
            return None
        return core._ModelRows(
            cls, list(cls.__columns__), list(cls.__json_columns__), SNAPSHOT
        )

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        changed = {**cls._stored_values(row), **cls._json_values(row)}
        instance = cls(**({**row, **changed} if changed else row))
        instance.remember()
        return instance

    @classmethod
    def _stored_values(cls, row: dict[str, Any]) -> dict[str, Any]:
        """Objects for the columns that store a plain form: addresses, dates,
        times, durations, decimals and enum members."""
        found: dict[str, Any] = {}
        for name, (label, reader) in cls.__column_readers__.items():
            if (stored := row.get(name)) is None:
                continue
            try:
                found[name] = reader(stored)
            except READ_ERRORS as error:
                raise FieldError(
                    f"{cls.__name__}.{name}: database value is not a valid {label}"
                ) from error
        return found

    @classmethod
    def _json_values(cls, row: dict[str, Any]) -> dict[str, Any]:
        decoded: dict[str, Any] = {}
        for name in cls.__json_columns__:
            value = row.get(name)
            if not isinstance(value, (str, bytes, bytearray)):
                continue
            raw = value.encode() if isinstance(value, str) else bytes(value)
            try:
                decoded[name] = core.decode_json(raw)
            except core.JsonDecodeError as error:
                raise FieldError(
                    f"{cls.__name__}.{name}: database value is not valid JSON"
                ) from error
        return decoded

    def remember(self) -> None:
        """Record the stored values, so ``asave`` writes only what changed."""
        columns = type(self).__columns__
        self.__dict__[SNAPSHOT] = {
            c: value
            if type(value := getattr(self, c)) in _IMMUTABLE
            else copy.deepcopy(value)
            for c in columns
        }

    @property
    def is_new(self) -> bool:
        """``True`` until the row has been written or loaded."""
        return SNAPSHOT not in self.__dict__

    @property
    def is_dirty(self) -> bool:
        """``True`` for a new instance and for one whose columns differ from
        the stored values."""
        return self.is_new or bool(self.changes)

    @property
    def changes(self) -> dict[str, Change]:
        """The columns that differ from the stored row, as ``Change(old, new)``.
        A new instance reports every column with ``old=UNSET``. Computed when
        read: nothing is tracked on assignment."""
        stored = self.__dict__.get(SNAPSHOT)
        columns = type(self).__columns__
        if stored is None:
            return {c: Change(UNSET, getattr(self, c)) for c in columns}
        return {
            c: Change(stored[c], current)
            for c in columns
            if (current := getattr(self, c)) != stored[c]
        }

    @property
    def changed_fields(self) -> tuple[str, ...]:
        """Names of columns that differ from the stored snapshot."""
        return tuple(self.changes)

    def previous(self, name: str) -> Any:
        """Return a column's stored value, or ``UNSET`` before its first save."""
        type(self).check_field(name)
        if self.is_new:
            return UNSET
        return self.__dict__[SNAPSHOT][name]

    @property
    def is_deleted(self) -> bool:
        """Whether this row has a soft-delete timestamp."""
        column = type(self).__soft_delete__
        return column is not None and getattr(self, column) is not None

    def was_changed(self, name: str) -> bool:
        """Whether column ``name`` differs from the stored row (always ``True``
        for a new instance)."""
        if name not in type(self).__columns__:
            raise FieldError(f"{type(self).__name__} has no column {name!r}")
        stored = self.__dict__.get(SNAPSHOT)
        return stored is None or getattr(self, name) != stored[name]

    def after_commit(self, callback: Callable[[], object]) -> None:
        """Run ``callback`` (sync or async) once the surrounding transaction
        commits; never after a rollback. Meant for ``after_*`` hooks."""
        current_database().after_commit(callback)

    def before_save(self) -> Awaitable[None] | None:
        """Hook: runs before every write of this instance; values may still change."""

    def before_create(self) -> Awaitable[None] | None:
        """Hook: runs before a new instance is first written, after ``before_save``."""

    def after_create(self) -> Awaitable[None] | None:
        """Hook: runs after the row is inserted, before ``after_save``."""

    def after_save(self) -> Awaitable[None] | None:
        """Hook: runs after the row is written, inside the transaction."""

    @classmethod
    def write_hooks(cls) -> tuple[str, ...]:
        """The hooks this model overrides, looked up on each write so a hook
        assigned after the class was created is not missed."""
        return tuple(
            name for name in HOOKS if getattr(cls, name) is not getattr(Model, name)
        )

    @classmethod
    def has_save_effects(cls) -> bool:
        """Whether saving an instance must go through ``asave``: a hook is
        overridden or the current bus wants ``Updated`` for this model. Code
        that would otherwise write with one scoped UPDATE checks it first."""
        return (
            bool(cls.write_hooks()) or events.listener(events.Updated, cls) is not None
        )

    @staticmethod
    def hook_transaction() -> AbstractAsyncContextManager[Any]:
        """The transaction a hooked write runs in: a savepoint when one is open
        (see ``write_transaction``)."""
        return write_transaction(current_database())

    async def run_hooks(self, *names: str) -> None:
        """Run the overridden hooks among ``names``, ``def`` or ``async def``."""
        overridden = type(self).write_hooks()
        for name in names:
            if name in overridden:
                result = getattr(self, name)()
                if inspect.isawaitable(result):
                    await result

    async def asave(self) -> Self:
        """Insert a new instance, or update the columns changed since it was
        loaded. Typed: ``Post(title="x", ...)`` is checked by the type checker."""
        return await self.save_with(None)

    async def save_with(self, scope: QuerySet[Any] | None) -> Self:
        """``asave``; with ``scope`` the UPDATE carries that query's conditions, so a
        row that left the scope since it was read is refused (``Model.NotFound``)
        instead of written."""
        if self.is_new:
            return await self.insert()
        bus = events.listener(events.Updated, type(self))
        if bus is not None:
            return await self.save_observed(scope, bus)
        if not type(self).write_hooks():
            if await self.write_update(scope):
                self.remember()
            return self
        async with self.hook_transaction():
            await self.run_hooks("before_save")
            if await self.write_update(scope):
                await self.run_hooks("after_save")
            self.remember()
        return self

    async def asoft_delete(self) -> Self:
        """Soft-delete this saved row, running its normal save hooks."""
        cls = type(self)
        column = cls.__soft_delete__
        if column is None:
            raise ModelError(f"{cls.__name__} does not support soft deletion")
        if self.is_new:
            raise FieldError(f"{cls.__name__}: cannot soft-delete an unsaved instance")
        if self.is_deleted:
            return self
        previous = getattr(self, column)
        setattr(self, column, _read_utc(cls.__soft_delete_clock__ or cls.__clock__))
        try:
            return await self.asave()
        except BaseException:
            setattr(self, column, previous)
            raise

    async def arestore(self) -> Self:
        """Restore this saved row, retaining normal hooks and timestamp behavior."""
        cls = type(self)
        column = cls.__soft_delete__
        if column is None:
            raise ModelError(f"{cls.__name__} does not support soft deletion")
        if self.is_new:
            raise FieldError(f"{cls.__name__}: cannot restore an unsaved instance")
        if not self.is_deleted:
            return self
        previous = getattr(self, column)
        setattr(self, column, None)
        key = cls.__primary_key__
        try:
            return await self.save_with(
                cls.with_deleted().filter(**{key: getattr(self, key)})
            )
        except BaseException:
            setattr(self, column, previous)
            raise

    async def ahard_delete(self) -> int:
        """Physically delete this saved row."""
        if self.is_new:
            raise FieldError(
                f"{type(self).__name__}: cannot delete an unsaved instance"
            )
        cls = type(self)
        key = cls.__primary_key__
        query = cls.with_deleted() if cls.__soft_delete__ is not None else cls.filter()
        return await query.filter(**{key: getattr(self, key)}).ahard_delete()

    def soft_delete(self) -> Self:
        """Sync ``asoft_delete`` for sync handlers."""
        return run_sync(self.asoft_delete())

    def restore(self) -> Self:
        """Sync ``arestore`` for sync handlers."""
        return run_sync(self.arestore())

    def hard_delete(self) -> int:
        """Sync ``ahard_delete`` for sync handlers."""
        return run_sync(self.ahard_delete())

    async def save_observed(self, scope: QuerySet[Any] | None, bus: EventBus) -> Self:
        """``save_with`` for a write that emits ``Updated``: inside a
        transaction when a hook or an ``in_transaction`` handler may fail it."""
        cls = type(self)
        async with cls.event_transaction(bus, events.Updated):
            await self.run_hooks("before_save")
            changed = tuple(self.changes)
            wrote = await self.write_update(scope)
            if wrote:
                await self.run_hooks("after_save")
            self.remember()
            if wrote:
                await bus.emit(events.Updated(cls, self, changed))
        return self

    @classmethod
    def event_transaction(
        cls, bus: EventBus, kind: type
    ) -> AbstractAsyncContextManager[Any]:
        """The transaction of a write that emits ``kind``: one when a hook or
        an ``in_transaction`` handler may fail it, else none (``after_commit``
        handlers are queued on whatever transaction is open)."""
        if cls.write_hooks() or bus.wants_transaction(kind, cls):
            return cls.hook_transaction()
        return nullcontext()

    async def aupdate_within(self, scope: QuerySet[Any], /, **values: Any) -> Self:
        """``assign`` then a save whose UPDATE carries ``scope``'s conditions (the
        generated CRUD update uses it so ownership and visibility hold at write
        time). A failed write leaves the instance as it was."""
        before = {c: getattr(self, c) for c in type(self).__columns__}
        try:
            return await self.assign(**values).save_with(scope)
        except BaseException:
            for name, value in before.items():
                setattr(self, name, value)
            raise

    async def write_update(self, scope: QuerySet[Any] | None = None) -> bool:
        """Write the changed columns and load the stored row, without taking a
        new snapshot; ``False`` when nothing changed."""
        cls = type(self)
        stored = self.__dict__[SNAPSHOT]
        key = cls.__primary_key__
        if getattr(self, key) != stored[key]:
            raise FieldError(f"{cls.__name__}: the primary key cannot be changed")
        changed = {
            c: getattr(self, c)
            for c in cls.__columns__
            if c != key and getattr(self, c) != stored[c]
        }
        for track in cls.__status_tracks__:
            if track.field in changed and track.changed_at not in changed:
                value = _read_utc(track.clock or cls.__clock__)
                setattr(self, track.changed_at, value)
                changed[track.changed_at] = value
        if not changed:
            return False
        if scope is None:
            self.load_stored(await cls.aupdate(**{key: stored[key]}, **changed))
            return True
        rows = await scope.aupdate(changed)
        if not rows:
            raise cls.NotFound(f"{cls.__name__} not found")
        self.load_stored(rows[0])
        return True

    async def insert(self, *, returning: bool = True) -> Self:
        """Insert this instance and load the stored row into it. With
        ``returning=False`` nothing is read back: the instance keeps the values
        it sent (every column is sent, defaults and factories applied)."""
        bus = events.listener(events.Created, type(self))
        if bus is not None:
            return await self.insert_observed(bus, returning)
        if not type(self).write_hooks():
            await self.write_insert(returning)
            self.remember()
            return self
        async with self.hook_transaction():
            await self.run_hooks("before_save", "before_create")
            await self.write_insert(returning)
            await self.run_hooks("after_create", "after_save")
            self.remember()
        return self

    async def insert_observed(self, bus: EventBus, returning: bool) -> Self:
        """``insert`` for a write that emits ``Created``."""
        cls = type(self)
        async with cls.event_transaction(bus, events.Created):
            await self.run_hooks("before_save", "before_create")
            await self.write_insert(returning)
            await self.run_hooks("after_create", "after_save")
            self.remember()
            await bus.emit(events.Created(cls, self))
        return self

    async def write_insert(self, returning: bool) -> None:
        cls = type(self).concrete()
        values = cls.validate_values({c: getattr(self, c) for c in cls.__columns__})
        for column, value in values.items():
            if value is not getattr(self, column):
                setattr(self, column, value)
        assignments = list(values.items())
        database = current_database()
        sql, params = write_statement(
            cls,
            "insert",
            assignments,
            [],
            returning=returning,
            dialect=database.dialect,
        )
        if not returning:
            await database.execute(sql, *params)
            return
        key = cls.__primary_key__
        # The Hinoki primary key is materialized before the write, so the
        # MySQL family can read the row back by it.
        rows = await written_rows(cls, sql, params, [(key, "eq", [getattr(self, key)])])
        self.load_stored(cls.from_row(rows[0]))

    def load_stored(self, stored: Model) -> None:
        for column in type(self).__columns__:
            setattr(self, column, getattr(stored, column))

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
        return cls.all().filter(**conditions)

    @classmethod
    def all(cls) -> QuerySet[Self]:
        query = QuerySet(cls.concrete())
        if cls.__soft_delete__ is not None:
            query = query.filter(**{f"{cls.__soft_delete__}__isnull": True})
            query = query.derive(implicit_filter_count=1)
        return query

    @classmethod
    def with_deleted(cls) -> QuerySet[Self]:
        """Query every row, including rows marked as deleted."""
        if cls.__soft_delete__ is None:
            raise ModelError(f"{cls.__name__} does not support soft deletion")
        return QuerySet(cls.concrete())

    @classmethod
    def only_deleted(cls) -> QuerySet[Self]:
        """Query only rows marked as deleted."""
        if cls.__soft_delete__ is None:
            raise ModelError(f"{cls.__name__} does not support soft deletion")
        query = QuerySet(cls.concrete()).filter(
            **{f"{cls.__soft_delete__}__isnull": False}
        )
        return query.derive(implicit_filter_count=1)

    @classmethod
    def from_schema(cls, data: Schema | None = None, /, **values: Any) -> Self:
        """An unsaved instance from a schema's values plus ``values``. Explicit
        keyword values win over the schema's, so a column the server assigns
        (owner, state) can never come from the client."""
        merged = cls.merged_values(data, values)
        return cls(**merged)

    @classmethod
    async def acreate(
        cls, data: Schema | None = None, /, *, returning: bool = True, **values: Any
    ) -> Self:
        """Insert a row (defaults and factories apply) and return it:
        ``await Post.acreate(data, author_id=me)`` or ``acreate(title="x")``.
        ``returning=False``: the instance holds the sent values, not re-read."""
        instance = cls.from_schema(data, **values)
        if not returning:
            return await instance.insert(returning=False)
        return await instance.insert()

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
        # NotFound is an exception class the model metaclass builds at class creation.
        except cls.NotFound:  # NOSONAR
            return None

    @classmethod
    def _simple_get_conditions(cls, conditions: dict[str, Any]) -> bool:
        if (
            cls.__abstract__
            or getattr(cls.all, "__func__", None)
            is not getattr(Model.all, "__func__", None)
            or getattr(cls.filter, "__func__", None)
            is not getattr(Model.filter, "__func__", None)
        ):
            return False
        columns = cls.__columns__
        field_types = cls.__field_types__
        return all(
            value is not None
            and "__" not in key
            and key in columns
            and field_types[key].kind != "decimal"
            for key, value in conditions.items()
        )

    @classmethod
    async def aget_as[S: Schema](cls, schema: type[S], **conditions: Any) -> S:
        """Exactly one row projected to ``schema``."""
        if cls._simple_get_conditions(conditions):
            _, _, _, project_model_one, fallback = _projection_hooks()
            result = await project_model_one(
                cls, schema, conditions, current_database(), allow_none=False
            )
            if result is not fallback:
                return cast("S", result)
        return await cls.filter(**conditions).aget_as(schema)

    @classmethod
    async def aget_or_none_as[S: Schema](
        cls, schema: type[S], **conditions: Any
    ) -> S | None:
        """At most one row projected to ``schema``, or ``None``."""
        if cls._simple_get_conditions(conditions):
            _, _, _, project_model_one, fallback = _projection_hooks()
            result = await project_model_one(
                cls, schema, conditions, current_database(), allow_none=True
            )
            if result is not fallback:
                return cast("S | None", result)
        return await cls.filter(**conditions).aget_or_none_as(schema)

    @overload
    @classmethod
    async def aupdate(
        cls, *, returning: Literal[True] = ..., **values: Any
    ) -> Self: ...
    @overload
    @classmethod
    async def aupdate(cls, *, returning: Literal[False], **values: Any) -> int: ...
    @overload
    @classmethod
    async def aupdate(cls, *, returning: bool, **values: Any) -> Self | int: ...
    @classmethod
    async def aupdate(cls, *, returning: bool = True, **values: Any) -> Self | int:
        """Update by primary key (``id=...``) and return the updated row.
        ``returning=False`` reads nothing back and returns the row count the
        database reports (no ``NotFound``: 0 means no row was written)."""
        key = cls.__primary_key__
        if key not in values:
            raise FieldError(f"{cls.__name__}.aupdate needs {key}=...")
        identity = values.pop(key)
        if not returning:
            return await cls.aupdate_where({key: identity}, values, returning=False)
        rows = await cls.aupdate_where({key: identity}, values)
        if not rows:
            raise cls.NotFound(f"{cls.__name__} not found")
        return rows[0]

    @overload
    @classmethod
    async def aupdate_where(
        cls,
        conditions: dict[str, Any],
        values: dict[str, Any],
        *,
        returning: Literal[True] = ...,
    ) -> list[Self]: ...
    @overload
    @classmethod
    async def aupdate_where(
        cls,
        conditions: dict[str, Any],
        values: dict[str, Any],
        *,
        returning: Literal[False],
    ) -> int: ...
    @overload
    @classmethod
    async def aupdate_where(
        cls, conditions: dict[str, Any], values: dict[str, Any], *, returning: bool
    ) -> list[Self] | int: ...
    @classmethod
    async def aupdate_where(
        cls,
        conditions: dict[str, Any],
        values: dict[str, Any],
        *,
        returning: bool = True,
    ) -> list[Self] | int:
        """Update every row matching ``conditions`` and return the new rows
        (or, with ``returning=False``, the row count the database reports).
        Conditions are part of the statement, so a row that stopped matching
        (for example after a concurrent owner change) is not touched."""
        if not conditions:
            raise FieldError("aupdate_where needs conditions and values")
        query = cls.filter(**conditions)
        if not returning:
            return await query.aupdate(values, returning=False)
        return await query.aupdate(values)

    @classmethod
    async def adelete(cls, **conditions: Any) -> int:
        """Delete matching rows; returns how many. An empty condition set is
        refused so a missing filter cannot wipe the table."""
        if not conditions:
            raise FieldError("adelete needs at least one condition")
        return await cls.filter(**conditions).adelete()

    @classmethod
    async def abulk_create(
        cls, items: Iterable[Schema | dict[str, Any]], /, *, returning: bool = True
    ) -> list[Self]:
        """Insert many rows in one transaction: all of them or none. Each item is a
        ``Schema`` or a dict of field values; defaults, factories, validation and
        write hooks apply exactly as for ``acreate``. Returns the rows in order."""
        instances = cls.bulk_instances(items)
        if not instances:
            return []
        async with current_database().atomic():
            return await cls.insert_all(instances, returning=returning)

    @classmethod
    def bulk_instances(cls, items: Iterable[Schema | dict[str, Any]]) -> list[Self]:
        """The unsaved instances ``abulk_create`` inserts, checked before writing."""
        return [
            cls.from_schema(**item) if isinstance(item, dict) else cls.from_schema(item)
            for item in items
        ]

    @staticmethod
    async def insert_all[M: Model](instances: list[M], *, returning: bool) -> list[M]:
        """Insert ``instances`` in the caller's transaction."""
        return [await row.insert(returning=returning) for row in instances]

    @classmethod
    async def abulk_update(cls, instances: Iterable[Self], /) -> list[Self]:
        """Save the changes of many loaded instances in one transaction: all of
        them or none. Unchanged instances write nothing; an unsaved one is refused."""
        rows = cls.saved_rows(instances)
        if not rows:
            return []
        async with current_database().atomic():
            return await cls.save_all(rows)

    @classmethod
    def saved_rows(cls, instances: Iterable[Self]) -> list[Self]:
        """The instances ``abulk_update`` saves; an unsaved one is refused."""
        rows = list(instances)
        for row in rows:
            if row.is_new:
                raise FieldError(
                    f"{cls.__name__}: abulk_update got an unsaved instance; "
                    "use abulk_create"
                )
        return rows

    @staticmethod
    async def save_all[M: Model](rows: list[M]) -> list[M]:
        """Save ``rows`` in the caller's transaction."""
        return [await row.asave() for row in rows]

    @classmethod
    async def abulk_delete(cls, items: Iterable[Self | Any], /) -> int:
        """Delete many rows by instance or primary-key value in one transaction;
        returns how many existed. Nothing is deleted for an empty list."""
        keys = [cls.delete_key(item) for item in items]
        if not keys:
            return 0
        async with current_database().atomic():
            return await delete_keys(cls.concrete(), keys)

    @classmethod
    def delete_key(cls, item: Self | Any) -> Any:
        """The primary-key value ``abulk_delete`` removes for ``item``."""
        if not isinstance(item, cls):
            return item
        if item.is_new:
            raise FieldError(f"{cls.__name__}: abulk_delete got an unsaved instance")
        return getattr(item, cls.__primary_key__)

    @classmethod
    def create(
        cls, data: Schema | None = None, /, *, returning: bool = True, **values: Any
    ) -> Self:
        """Sync ``acreate`` for sync handlers."""
        if not returning:
            return run_sync(cls.acreate(data, returning=False, **values))
        return run_sync(cls.acreate(data, **values))

    def update_from(self, data: Schema | None = None, /, **values: Any) -> Self:
        """Sync ``aupdate_from`` for sync handlers."""
        return run_sync(self.aupdate_from(data, **values))

    @overload
    @classmethod
    def update_where(
        cls,
        conditions: dict[str, Any],
        values: dict[str, Any],
        *,
        returning: Literal[True] = ...,
    ) -> list[Self]: ...
    @overload
    @classmethod
    def update_where(
        cls,
        conditions: dict[str, Any],
        values: dict[str, Any],
        *,
        returning: Literal[False],
    ) -> int: ...
    @overload
    @classmethod
    def update_where(
        cls, conditions: dict[str, Any], values: dict[str, Any], *, returning: bool
    ) -> list[Self] | int: ...
    @classmethod
    def update_where(
        cls,
        conditions: dict[str, Any],
        values: dict[str, Any],
        *,
        returning: bool = True,
    ) -> list[Self] | int:
        """Sync ``aupdate_where`` for sync handlers."""
        if not returning:
            return run_sync(cls.aupdate_where(conditions, values, returning=False))
        return run_sync(cls.aupdate_where(conditions, values))

    @classmethod
    def get(cls, **conditions: Any) -> Self:
        return run_sync(cls.aget(**conditions))

    @classmethod
    def get_or_none(cls, **conditions: Any) -> Self | None:
        return run_sync(cls.aget_or_none(**conditions))

    @classmethod
    def get_as[S: Schema](cls, schema: type[S], **conditions: Any) -> S:
        """Sync ``aget_as``."""
        return run_sync(cls.aget_as(schema, **conditions))

    @classmethod
    def get_or_none_as[S: Schema](cls, schema: type[S], **conditions: Any) -> S | None:
        """Sync ``aget_or_none_as``."""
        return run_sync(cls.aget_or_none_as(schema, **conditions))

    @overload
    @classmethod
    def update(cls, *, returning: Literal[True] = ..., **values: Any) -> Self: ...
    @overload
    @classmethod
    def update(cls, *, returning: Literal[False], **values: Any) -> int: ...
    @overload
    @classmethod
    def update(cls, *, returning: bool, **values: Any) -> Self | int: ...
    @classmethod
    def update(cls, *, returning: bool = True, **values: Any) -> Self | int:
        """Sync ``aupdate`` for sync handlers."""
        if not returning:
            return run_sync(cls.aupdate(returning=False, **values))
        return run_sync(cls.aupdate(returning=True, **values))

    @classmethod
    def delete(cls, **conditions: Any) -> int:
        return run_sync(cls.adelete(**conditions))

    @classmethod
    def bulk_create(
        cls, items: Iterable[Schema | dict[str, Any]], /, *, returning: bool = True
    ) -> list[Self]:
        """Sync ``abulk_create`` for sync handlers; inside an open transaction
        it is a savepoint of that transaction (see ``run_bulk``)."""
        refuse_event_loop()
        instances = cls.bulk_instances(items)
        if not instances:
            return []
        return run_bulk(lambda: cls.insert_all(instances, returning=returning))

    @classmethod
    def bulk_update(cls, instances: Iterable[Self], /) -> list[Self]:
        """Sync ``abulk_update`` for sync handlers; scoped as ``bulk_create``."""
        refuse_event_loop()
        rows = cls.saved_rows(instances)
        if not rows:
            return []
        return run_bulk(lambda: cls.save_all(rows))

    @classmethod
    def bulk_delete(cls, items: Iterable[Self | Any], /) -> int:
        """Sync ``abulk_delete`` for sync handlers; scoped as ``bulk_create``."""
        refuse_event_loop()
        keys = [cls.delete_key(item) for item in items]
        if not keys:
            return 0
        return run_bulk(lambda: delete_keys(cls.concrete(), keys))

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
