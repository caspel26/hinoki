"""Constructed-value validation and deferred field normalization."""

from __future__ import annotations

import inspect
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

from hinoki.errors import ErrorDetail, ValidationError
from hinoki.fields import UNSET, FieldInfo
from hinoki.fieldtypes import FieldType, normalize_text
from hinoki.schema.decorators import Invalid, stop_awaitable
from hinoki.schema.hooks import (
    graph_has_checks,
    graph_has_normalizers,
    run_checks,
)
from hinoki.schema.validate import COLLECTION_TYPES
from hinoki.schemafast import instance_accepts

if TYPE_CHECKING:
    from hinoki.schema.base import Schema

_NORMALIZATION_QUEUE: ContextVar[list[tuple[Schema, bool, str]] | None] = ContextVar(
    "hinoki_schema_normalization_queue", default=None
)


def validate_constructed(
    instance: Schema, *, from_model: bool = False, use_aliases: bool = False
) -> None:
    """Validate values and custom checks after construction has completed."""
    cls = type(instance)
    if not instance_accepts(cls, instance):
        cls._validate_instance(
            instance,
            allow_naive_datetime=from_model,
            use_aliases=use_aliases,
        )
    normalize_floats(instance)
    queue = _NORMALIZATION_QUEUE.get()
    if queue is not None:
        queue_normalization(queue, instance, use_aliases, "")
        return
    normalize_constructed(instance, use_aliases=use_aliases)
    if graph_has_checks(cls):
        found = run_checks(instance, "", use_aliases=use_aliases)
        if found:
            raise ValidationError(found)


def normalize_constructed(
    instance: Schema, *, use_aliases: bool, path_prefix: str = ""
) -> None:
    """Stage and validate canonical strings before committing any field."""
    proposed = prepare_normalized_values(instance, use_aliases, path_prefix)
    commit_normalized_values(instance, proposed)


def contains_float(ftype: FieldType) -> bool:
    """Whether a field holds ``float`` values directly or inside a container."""
    if ftype.kind == "float":
        return True
    if ftype.kind == "tuple":
        return any(contains_float(item) for item in ftype.items)
    return ftype.item is not None and contains_float(ftype.item)


def floated(ftype: FieldType, value: Any) -> Any:
    """``value`` with every ``int`` where the type says ``float`` made a ``float``."""
    if ftype.kind == "float":
        return float(value) if type(value) is int else value
    if ftype.kind == "dict" and isinstance(value, dict) and ftype.item is not None:
        return {key: floated(ftype.item, item) for key, item in value.items()}
    return floated_sequence(ftype, value)


def floated_sequence(ftype: FieldType, value: Any) -> Any:
    if ftype.kind == "tuple" and isinstance(value, tuple):
        return tuple(
            floated(kind, item) for kind, item in zip(ftype.items, value, strict=False)
        )
    if (
        ftype.kind == "list"
        and ftype.item is not None
        and isinstance(value, tuple | list | set | frozenset)
    ):
        container = COLLECTION_TYPES.get(ftype.collection or "", list)
        return container(floated(ftype.item, item) for item in value)
    return value


def float_fields(cls: type[Schema]) -> tuple[tuple[str, FieldType], ...]:
    """The fields that hold floats, cached per class."""
    cached = cls.__dict__.get("__float_fields__")
    if cached is None:
        cached = tuple(
            (name, spec.ftype)
            for name, spec in cls.specs().items()
            if contains_float(spec.ftype)
        )
        cls.__float_fields__ = cached
    return cached


def normalize_floats(instance: Schema) -> None:
    """Store an ``int`` given for a ``float`` field as a ``float``, as Pydantic's
    Python mode does; the wire path already returns floats."""
    for name, ftype in float_fields(type(instance)):
        current = getattr(instance, name)
        converted = floated(ftype, current)
        if converted is not current:
            object.__setattr__(instance, name, converted)


def declared_forms(cls: type[Schema]) -> tuple[tuple[str, FieldInfo], ...]:
    """The ``str`` fields declared with ``strip``, ``lower`` or ``upper``."""
    cached = cls.__dict__.get("__declared_forms__")
    if cached is None:
        cached = tuple(
            (name, spec.ftype.info)
            for name, spec in cls.specs().items()
            if spec.ftype.kind == "str"
            and (
                spec.ftype.info.strip or spec.ftype.info.lower or spec.ftype.info.upper
            )
        )
        cls.__declared_forms__ = cached
    return cached


def prepare_normalized_values(
    instance: Schema, use_aliases: bool, path_prefix: str
) -> dict[str, str]:
    cls = type(instance)
    declared = declared_forms(cls)
    if not cls.__normalizers__ and not declared:
        return {}
    proposed: dict[str, str] = {}
    for field_name, info in declared:
        value = getattr(instance, field_name)
        if isinstance(value, str):
            proposed[field_name] = normalize_text(value, info)
    for field_name, method_name in cls.__normalizers__:
        value = proposed.get(field_name, getattr(instance, field_name))
        if value is UNSET or value is None:
            continue
        proposed[field_name] = normalized_value(instance, method_name, value)
    if not proposed:
        return {}
    validate_normalized_values(cls, proposed, use_aliases, path_prefix)
    return proposed


def commit_normalized_values(instance: Schema, proposed: dict[str, str]) -> None:
    for field_name, value in proposed.items():
        object.__setattr__(instance, field_name, value)


def queue_normalization(
    queue: list[tuple[Schema, bool, str]],
    instance: Schema,
    use_aliases: bool,
    path_prefix: str,
) -> None:
    queue.append((instance, use_aliases, path_prefix))


def defer_or_normalize(
    instance: Schema, use_aliases: bool, path_prefix: str = ""
) -> None:
    queue = _NORMALIZATION_QUEUE.get()
    if queue is None:
        normalize_constructed(
            instance, use_aliases=use_aliases, path_prefix=path_prefix
        )
    else:
        queue_normalization(queue, instance, use_aliases, path_prefix)


def apply_normalization_queue(queue: list[tuple[Schema, bool, str]]) -> None:
    """Validate every new object's proposals before committing any of them."""
    prepared = [
        (
            instance,
            prepare_normalized_values(instance, use_aliases, path_prefix),
        )
        for instance, use_aliases, path_prefix in queue
    ]
    for instance, proposed in prepared:
        commit_normalized_values(instance, proposed)


def begin_normalization_queue(
    cls: type[Schema],
) -> tuple[list[tuple[Schema, bool, str]] | None, Token[Any] | None, bool]:
    """Share one deferred finalization queue across nested Schema construction."""
    current = _NORMALIZATION_QUEUE.get()
    if current is not None:
        return current, None, False
    if not graph_has_normalizers(cls):
        return None, None, False
    queue: list[tuple[Schema, bool, str]] = []
    return queue, _NORMALIZATION_QUEUE.set(queue), True


def normalized_value(instance: Schema, method_name: str, value: str) -> str:
    try:
        result = getattr(instance, method_name)(value)
    except Invalid:
        raise TypeError(
            "field normalizers must return a string; use @field_check to reject"
        ) from None
    if inspect.isawaitable(result):
        stop_awaitable(result)
        raise TypeError("field normalizers must be synchronous")
    if type(result) is not str:
        raise TypeError("field normalizers must return a string")
    return result


def validate_normalized_values(
    cls: type[Schema],
    values: dict[str, str],
    use_aliases: bool,
    path_prefix: str,
) -> None:
    """Re-run compiled constraints only for staged normalized fields."""
    skip = sorted(set(cls.specs()) - values.keys())
    data = {
        cls.specs()[name].wire_name if use_aliases else name: value
        for name, value in values.items()
    }
    _, problems = cls.native_schema().validate(
        data,
        construct=False,
        use_aliases=use_aliases,
        skip_fields=skip,
    )
    if problems:
        raise ValidationError(
            [
                ErrorDetail(
                    f"{path_prefix}.{path}"
                    if path_prefix and path
                    else path_prefix or path,
                    code,
                    message,
                )
                for path, code, message in problems
            ]
        )
