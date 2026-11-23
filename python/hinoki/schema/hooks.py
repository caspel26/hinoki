"""Selectors, input preparation, @before_validate traversal, and check execution."""

from __future__ import annotations

import inspect
import typing
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Annotated, Any, cast

from hinoki.errors import ErrorDetail, RegistrationError, ValidationError
from hinoki.fields import UNSET, Field
from hinoki.fieldtypes import FieldType
from hinoki.schema.decorators import (
    BEFORE_ATTR,
    CHECK_ATTR,
    MAX_BEFORE_DEPTH,
    NORMALIZE_ATTR,
    Invalid,
    descriptor_function,
    stop_awaitable,
)
from hinoki.schema.spec import FieldSpec
from hinoki.schema.validate import typed_entries

if TYPE_CHECKING:
    from hinoki.schema.base import Schema


def normalize_selector(
    values: Iterable[str] | None, label: str, specs: Mapping[str, object]
) -> list[str] | None:
    """Validate one shallow Python-name field selector."""
    if values is None:
        if label == "include":
            return None
        raise TypeError(f"{label} must be an iterable of field names")
    if isinstance(values, str):
        raise TypeError(f"{label} must be an iterable of field names, not str")
    try:
        names = list(values)
    except TypeError as exc:
        raise TypeError(f"{label} must be an iterable of field names") from exc
    if any(not isinstance(name, str) for name in names):
        raise TypeError(f"{label} members must be strings")
    unknown = sorted(set(names) - specs.keys())
    if unknown:
        raise ValueError(f"unknown {label} fields: {unknown}")
    return names


def link_id_fields(model: Any) -> dict[str, Any]:
    """The ids field of each many-to-many relation ``model`` declares, with its
    type: a set of target keys bounded by the relation's ``max_items``."""
    found: dict[str, Any] = {}
    for side in getattr(model, "__links__", {}).values():
        if side.forward and side.ids_name:
            target = side.target
            key = typing.get_type_hints(target)[target.__primary_key__]
            keys = cast(Any, set)[key]
            found[side.ids_name] = Annotated[keys, Field(max_length=side.max_items)]
    return found


def chosen_columns(
    cls: type,
    model: Any,
    fields: Iterable[str] | None,
    exclude: tuple[str, ...],
    optional: bool | Iterable[str],
) -> tuple[list[str], set[str], bool]:
    """The columns to take, the names made optional, and whether all are."""
    if fields is not None and exclude:
        raise RegistrationError(f"{cls.__name__}: use fields= or exclude=, not both")
    columns = list(model.__columns__)
    chosen = (
        list(fields) if fields is not None else [c for c in columns if c not in exclude]
    )
    known = {*columns, *link_id_fields(model)}
    unknown = [n for n in [*chosen, *exclude] if n not in known]
    if unknown:
        raise RegistrationError(
            f"{cls.__name__}: {model.__name__} has no columns {unknown}"
        )
    some = set() if isinstance(optional, bool) else set(optional)
    stray = sorted(some - set(chosen))
    if stray:
        raise RegistrationError(
            f"{cls.__name__}: optional names unknown fields {stray}"
        )
    return chosen, some, optional is True


def collect_checks(cls: type) -> tuple[tuple[str, tuple[str, ...], str], ...]:
    """``(kind, field names, method name)`` of every check, base classes first;
    a subclass method with the same name replaces the inherited one."""
    found: dict[str, tuple[str, tuple[str, ...], str]] = {}
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            marker = check_marker(value)
            if marker is None:
                found.pop(name, None)
                continue
            function = descriptor_function(value)
            ensure_sync_check(cls, name, function)
            found[name] = (marker[0], marker[1], name)
    return tuple(found.values())


def check_marker(value: Any) -> Any:
    marker = getattr(value, CHECK_ATTR, None)
    return (
        marker
        if marker is not None
        else getattr(descriptor_function(value), CHECK_ATTR, None)
    )


def ensure_sync_check(cls: type, name: str, function: Any) -> None:
    if inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function):
        raise RegistrationError(
            f"{cls.__name__}.{name}: Schema checks must be synchronous"
        )


def collect_normalizers(cls: type) -> tuple[tuple[str, str], ...]:
    """Collect inherited field normalizers, honoring method overrides."""
    found: dict[str, tuple[str, str]] = {}
    for klass in reversed(cls.__mro__):
        for method_name, value in vars(klass).items():
            function = (
                value.__func__
                if isinstance(value, (staticmethod, classmethod))
                else value
            )
            field_name = getattr(value, NORMALIZE_ATTR, None)
            if field_name is None:
                field_name = getattr(function, NORMALIZE_ATTR, None)
            if field_name is not None:
                found[method_name] = (field_name, method_name)
            elif method_name in found:
                del found[method_name]
    return tuple(found.values())


def collect_before(cls: type) -> tuple[str, ...]:
    """Names of the ``@before_validate`` hooks, base classes first; a subclass
    method with the same name replaces the inherited one."""
    found: dict[str, None] = {}
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            function = descriptor_function(value)
            if getattr(function, BEFORE_ATTR, False) is True:
                ensure_sync_check(cls, name, function)
                found[name] = None
            else:
                found.pop(name, None)
    return tuple(found)


def graph_has_before(cls: type[Schema]) -> bool:
    """Whether this schema graph declares a before hook (answer cached)."""
    cached = cls.__dict__.get("__graph_before__")
    if cached is None:
        cached = reaches_before(cls)
        cls.__graph_before__ = cached
    return bool(cached)


def reaches_before(cls: type[Schema]) -> bool:
    pending = [cls]
    visited: set[type] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        if current.__before__:
            return True
        pending.extend(nested_schemas(current))
    return False


def prepare_root(cls: type[Schema], data: object) -> object:
    return prepare_input(cls, data, "", 0) if graph_has_before(cls) else data


def refuse_hooked_unions(cls: type[Schema]) -> None:
    """Refuse, when ``cls`` is defined, a plain union holding a hooked schema."""
    for spec in cls.specs().values():
        pending = [spec.ftype]
        while pending:
            ftype = pending.pop()
            if ftype.kind == "anyof":
                refuse_hooked_union(ftype)
            pending.extend(ftype.children())


def prepare_input(cls: type[Schema], data: object, path: str, depth: int) -> object:
    """Apply the before hooks of ``cls`` and of the schemas nested in ``data``."""
    if depth > MAX_BEFORE_DEPTH:
        raise ValidationError(
            [ErrorDetail(path, "too_deep", "input is nested too deep")]
        )
    if not isinstance(data, dict):
        return data
    for name in cls.__before__:
        data = run_before(cls, name, data, path)
    return prepare_fields(cls, data, path, depth) if graph_has_before(cls) else data


def prepare_fields(
    cls: type[Schema], data: dict[str, Any], path: str, depth: int
) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    for spec in cls.specs().values():
        if spec.wire_name in data and holds_schema(spec.ftype):
            where = f"{path}.{spec.wire_name}" if path else spec.wire_name
            value = data[spec.wire_name]
            prepared = prepare_value(spec.ftype, value, where, depth + 1)
            if prepared is not value:
                changed[spec.wire_name] = prepared
    return {**data, **changed} if changed else data


def run_before(
    cls: type[Schema], name: str, data: dict[str, Any], path: str
) -> dict[str, Any]:
    try:
        result = getattr(cls, name)(dict(data))
    except Invalid as refusal:
        raise ValidationError(
            [
                ErrorDetail(
                    joined_path(path, refusal.path), refusal.code, refusal.message
                )
            ]
        ) from None
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise ValidationError(
            [ErrorDetail(path, "invalid_input", "input could not be prepared")]
        ) from None
    if not isinstance(result, dict):
        raise TypeError(
            f"{cls.__name__}.{name}: a before hook must return a mapping, "
            f"not {type(result).__name__}"
        )
    return result


def joined_path(base: str, extra: str) -> str:
    return f"{base}.{extra}" if base and extra else base or extra


def prepare_value(ftype: FieldType, value: object, where: str, depth: int) -> object:
    """``value`` with the before hooks applied through lists, dicts and tuples."""
    if ftype.variants:
        return prepare_variant(ftype, value, where, depth)
    if ftype.schema is not None:
        return prepare_input(ftype.schema, value, where, depth)
    if ftype.kind == "anyof":
        refuse_hooked_union(ftype)
        return value
    return prepare_container(ftype, value, where, depth)


def prepare_container(
    ftype: FieldType, value: object, where: str, depth: int
) -> object:
    if isinstance(value, list):
        return prepare_sequence(ftype, value, where, depth)
    if isinstance(value, dict) and ftype.item is not None:
        return {
            key: prepare_value(ftype.item, item, f"{where}.{key}", depth)
            for key, item in value.items()
        }
    return value


def prepare_sequence(
    ftype: FieldType, value: list[Any], where: str, depth: int
) -> list[Any]:
    if ftype.kind == "tuple":
        children: tuple[FieldType | None, ...] = ftype.items
    else:
        children = (ftype.item,) * len(value)
    return [
        item if child is None else prepare_value(child, item, f"{where}.{index}", depth)
        for index, (child, item) in enumerate(zip(children, value, strict=False))
    ] + value[len(children) :]


def prepare_variant(ftype: FieldType, value: object, where: str, depth: int) -> object:
    """The hooks of the variant a discriminated value selects; an unknown tag is
    left for the structural validation to report."""
    if not isinstance(value, dict):
        return value
    tag = value.get(ftype.discriminator_wire_name or ftype.discriminator or "")
    for name, schema in ftype.variants:
        if name == tag:
            return prepare_input(schema, value, where, depth)
    return value


def type_reaches_before(ftype: FieldType) -> bool:
    if ftype.schema is not None and graph_has_before(ftype.schema):
        return True
    if any(graph_has_before(schema) for _, schema in ftype.variants):
        return True
    return any(type_reaches_before(child) for child in ftype.children())


def refuse_hooked_union(ftype: FieldType) -> None:
    for child in ftype.items:
        if type_reaches_before(child):
            raise RegistrationError(
                "a plain union holds a schema that uses @before_validate: "
                "declare a discriminator so the hook can be applied"
            )


def graph_has_checks(cls: type[Schema], seen: set[type] | None = None) -> bool:
    """Whether this schema graph declares a check; recursive-safe lookup."""
    cached = cls.__dict__.get("__graph_checks__")
    if cached is not None:
        return bool(cached)
    pending = [cls]
    visited: set[type] = set() if seen is None else set(seen)
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        known = current.__dict__.get("__graph_checks__")
        if known is True:
            return True
        if known is False:
            continue
        if current.__checks__:
            cls.__graph_checks__ = True
            return True
        pending.extend(nested_schemas(current))
    if seen is None:
        remember_graph_without(visited, "__graph_checks__")
    return False


def remember_graph_without(visited: set[type], marker: str) -> None:
    """Cache a negative walk. Sound because a finished walk resolved every
    reachable schema's specs (which are never rebuilt) and ``__checks__`` /
    ``__normalizers__`` are fixed when a class is created, so a graph that
    held none cannot gain one; every visited class reaches only visited ones.
    A walk that started from a partial ``seen`` set proves nothing and is not
    cached."""
    for member in visited:
        setattr(member, marker, False)


def graph_has_normalizers(cls: type[Schema], seen: set[type] | None = None) -> bool:
    """Whether a schema graph needs a side-effect-free structural preflight."""
    # Recursive schema graphs make recursive memoization unsafe: a node first
    # reached through the active cycle can appear to have no normalizers even
    # though a later field in that same graph contains one. An iterative walk
    # visits each class once; a negative answer is cached only after the whole
    # walk finished (see remember_graph_without).
    cached = cls.__dict__.get("__graph_normalizers__")
    if cached is not None:
        return bool(cached)
    pending = [cls]
    visited: set[type] = set() if seen is None else set(seen)
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        known = current.__dict__.get("__graph_normalizers__")
        if known is True:
            return True
        if known is False:
            continue
        if current.__normalizers__:
            cls.__graph_normalizers__ = True
            return True
        pending.extend(nested_schemas(current))
    if seen is None:
        remember_graph_without(visited, "__graph_normalizers__")
    return False


def nested_schemas(cls: type[Schema]) -> list[type[Schema]]:
    found: list[type[Schema]] = []
    for spec in cls.specs().values():
        pending: list[FieldType] = [spec.ftype]
        while pending:
            ftype = pending.pop()
            if ftype.schema is not None:
                found.append(ftype.schema)
            found.extend(schema for _, schema in ftype.variants)
            pending.extend(ftype.children())
    return found


def run_checks(
    instance: Any,
    path: str,
    *,
    use_aliases: bool = True,
    seen: set[int] | None = None,
) -> list[ErrorDetail]:
    """Run the checks of an instance and of every schema inside it."""
    checked_instances = seen if seen is not None else set()
    identity = id(instance)
    if identity in checked_instances:
        return []
    checked_instances.add(identity)
    problems = nested_instance_checks(instance, path, use_aliases, checked_instances)
    problems.extend(declared_instance_checks(instance, path, use_aliases))
    return problems


def nested_instance_checks(
    instance: Any, path: str, use_aliases: bool, seen: set[int]
) -> list[ErrorDetail]:
    """Run checks for every Schema field nested in an instance."""
    problems: list[ErrorDetail] = []
    for name, spec in type(instance).specs().items():
        field_name = spec.wire_name if use_aliases else name
        problems.extend(
            _nested_check_problems(
                spec.ftype,
                getattr(instance, name),
                field_name,
                path,
                use_aliases=use_aliases,
                seen=seen,
            )
        )
    return problems


def declared_instance_checks(
    instance: Any, path: str, use_aliases: bool
) -> list[ErrorDetail]:
    """Run the checks declared directly or inherited by one Schema class."""
    problems: list[ErrorDetail] = []
    specs = type(instance).specs()
    for kind, names, method in type(instance).__checks__:
        for target in names if kind == "field" else (None,):
            wire_name = check_wire_name(specs, target, use_aliases)
            problems.extend(call_check(instance, method, target, wire_name, path))
    return problems


def check_wire_name(
    specs: dict[str, FieldSpec], target: str | None, use_aliases: bool
) -> str | None:
    if target is None:
        return None
    return specs[target].wire_name if use_aliases else target


def _nested_check_problems(
    ftype: FieldType,
    value: Any,
    name: str,
    path: str,
    *,
    use_aliases: bool,
    seen: set[int],
) -> list[ErrorDetail]:
    if not holds_schema(ftype):
        return []  # list[int], dict[str, str]...: nothing to check, nothing walked
    where = f"{path}.{name}" if path else name
    return value_check_problems(ftype, value, where, use_aliases, seen)


def holds_schema(ftype: FieldType) -> bool:
    """Whether a value of this type can contain a Schema instance."""
    if ftype.schema is not None or ftype.variants:
        return True
    return any(holds_schema(child) for child in ftype.children())


def value_check_problems(
    ftype: FieldType, value: Any, where: str, use_aliases: bool, seen: set[int]
) -> list[ErrorDetail]:
    """Checks of the Schema instances in one field value, through lists, dicts
    and tuples at any depth; ``ftype`` can hold a Schema."""
    if getattr(type(value), "__hinoki_schema__", False):
        return run_checks(value, where, use_aliases=use_aliases, seen=seen)
    return [
        problem
        for child, at, element in typed_entries(ftype, value, where)
        for problem in value_check_problems(child, element, at, use_aliases, seen)
    ]


def call_check(
    instance: Any,
    method: str,
    target: str | None,
    wire_name: str | None,
    path: str,
) -> list[ErrorDetail]:
    """One check on one field (or on the object when ``target`` is ``None``)."""
    where = f"{path}.{wire_name}" if path and wire_name else (wire_name or path)
    try:
        if target is None:
            result = getattr(instance, method)()
        else:
            value = getattr(instance, target)
            result = getattr(instance, method)(value) if value is not UNSET else None
        ensure_check_result(result)
    except Invalid as refusal:
        return [ErrorDetail(refusal.path or where, refusal.code, refusal.message)]
    return []


def ensure_check_result(result: Any) -> None:
    if result is None:
        return
    if inspect.isawaitable(result):
        stop_awaitable(result)
    raise TypeError("Schema check hooks must return None; raise Invalid to reject")
