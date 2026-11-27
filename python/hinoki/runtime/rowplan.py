"""Registration-time selection for the private generated-list row-wire path."""

from __future__ import annotations

import dataclasses
import inspect
import types
from dataclasses import dataclass
from typing import Any, TypeGuard, cast

from hinoki import core
from hinoki.orm import Model
from hinoki.orm.queryset import (
    ROW_WIRE_METHODS,
    ROW_WIRE_METHODS_ORIGINAL,
    Loading,
    QuerySet,
)
from hinoki.renderers import is_default_json_renderer
from hinoki.resources import GENERATED, CrudBase, _CrudConfig
from hinoki.runtime.binding import Binding
from hinoki.schema import Schema, SchemaMeta, uses_generated_init

CONDITION_PARTS = 3
ROW_KINDS = frozenset(
    {"str", "int", "float", "bool", "uuid", "datetime", "bytes", "decimal", "date"}
)
OWNER_GUARD_NAMES = (
    "__init__",
    "__new__",
    "__getattribute__",
    "__getattr__",
    "__setattr__",
    "get_queryset",
    "to_schema",
    "call_hook",
    "call_default_hook",
    "call_custom_hook",
)
OWNER_INSTANCE_NAMES = (
    "get_queryset",
    "to_schema",
    "call_hook",
    "call_default_hook",
    "call_custom_hook",
    "resource",
    "model",
    "read_schema",
    "pagination",
)
MODEL_GUARD_NAMES = (
    "__new__",
    "__getattribute__",
    "__getattr__",
    "__setattr__",
    "__post_init__",
    "all",
    "from_row",
    "remember",
)
SCHEMA_GUARD_NAMES = (
    "__new__",
    "__getattribute__",
    "__getattr__",
    "__setattr__",
    "__post_init__",
    "_validate_instance",
    "native_schema",
    "specs",
    "dump",
)
QUERYSET_GUARD_NAMES = (
    *ROW_WIRE_METHODS,
    "__getattribute__",
    "__getattr__",
    "__setattr__",
)


@dataclass(frozen=True, slots=True)
class RowWireSelection:
    """One app's immutable plan plus identities for a cheap fail-closed guard."""

    plan: core._RowPlan
    owner: type[CrudBase]
    owner_state: tuple[object, ...]
    resource: _CrudConfig
    model: type[Model]
    model_field_types: dict[str, Any]
    model_field_items: tuple[tuple[str, Any], ...]
    model_state: tuple[object, ...]
    schema: type[Schema]
    columns: tuple[str, ...]
    json_columns: tuple[str, ...]
    schema_specs: dict[str, Any]
    schema_items: tuple[tuple[str, Any], ...]
    model_init: object
    column_descriptors: tuple[tuple[str, object | None], ...]
    schema_init: object
    schema_state: tuple[object, ...]
    schema_checks: object
    schema_check_items: tuple[Any, ...]
    schema_normalizers: object
    schema_normalizer_items: tuple[Any, ...]
    schema_native: object
    schema_config: object
    schema_field_descriptors: tuple[tuple[str, object | None], ...]
    queryset_state: tuple[object, ...]
    base_filters: tuple[object, ...]
    base_implicit_filters: int
    frozen: list[object] = dataclasses.field(
        default_factory=lambda: [None, False], compare=False
    )

    def valid_query(self, query: object) -> TypeGuard[QuerySet[Any]]:
        return valid_base_query(self, query)

    def valid_window(
        self,
        query: object,
        *,
        order: tuple[tuple[str, bool], ...],
        limit: int,
        offset: int | None,
    ) -> TypeGuard[QuerySet[Any]]:
        return valid_query_state(self, query, order=order, limit=limit, offset=offset)

    def valid_for(self, api: CrudBase) -> bool:
        """A changed class/resource/schema falls back before any database work."""
        return (
            type(api) is self.owner
            and selection_behavior_unchanged(self, api)
            and api.resource is self.resource
            and api.model is self.model
            and api.resource.read is self.schema
        )

    def armed_for(self, api: CrudBase) -> bool:
        """The per-request guard: the class-level checks of ``valid_for`` run
        once per freeze generation, so a request pays only for the instance."""
        if type(api) is not self.owner or has_shadowed_owner_state(api):
            return False
        generation = FREEZE_GENERATION[0]
        frozen = self.frozen
        if frozen[0] != generation:
            frozen[1] = self.valid_for(api)
            frozen[0] = generation
        return (
            frozen[1] is True
            and api.resource is self.resource
            and api.model is self.model
        )


# The row-wire class guards are frozen per generation; a worker startup
# starts a new one, so hooks patched before (or while) the app starts are seen.
FREEZE_GENERATION = [0]


def refreeze_row_wire() -> None:
    """Re-run every row-wire class guard on its next request."""
    FREEZE_GENERATION[0] += 1


def with_row_wire(binding: Binding) -> Binding:
    """Attach a route plan after this app resolved and validated its renderer."""
    selection = select_row_wire(binding)
    if selection is None:
        return binding
    return dataclasses.replace(binding, row_wire=selection)


def static_state(owner: type, names: tuple[str, ...]) -> tuple[object, ...]:
    return tuple(inspect.getattr_static(owner, name, None) for name in names)


# Attributes of ``type`` itself that win over a class's own namespace in
# ``inspect.getattr_static`` (data descriptors such as ``__doc__``).
TYPE_DATA_DESCRIPTORS = frozenset(
    name
    for name, value in vars(type).items()
    if hasattr(type(value), "__get__")
    and (hasattr(type(value), "__set__") or hasattr(type(value), "__delete__"))
)


def class_static(owner: type, name: str) -> object | None:
    """``inspect.getattr_static(owner, name, None)`` for a class, fast.

    With ``type`` as the exact metaclass, every class in the MRO has a real
    namespace and only ``type``'s data descriptors take precedence over it,
    so a plain MRO walk returns the same object. Anything else uses
    ``getattr_static`` itself.
    """
    if type(owner) is not type or name in TYPE_DATA_DESCRIPTORS:
        return cast(object | None, inspect.getattr_static(owner, name, None))
    for klass in owner.__mro__:
        namespace = klass.__dict__
        if name in namespace:
            return cast(object, namespace[name])
    return cast(object | None, vars(type).get(name))


def static_state_matches(
    owner: type, names: tuple[str, ...], expected: tuple[object, ...]
) -> bool:
    return all(
        class_static(owner, name) is value
        for name, value in zip(names, expected, strict=True)
    )


def has_shadowed_owner_state(api: CrudBase) -> bool:
    try:
        attributes = object.__getattribute__(api, "__dict__")
    except AttributeError:
        return True
    return any(name in attributes for name in OWNER_INSTANCE_NAMES)


def selection_behavior_unchanged(selection: RowWireSelection, api: CrudBase) -> bool:
    return (
        static_state_matches(selection.owner, OWNER_GUARD_NAMES, selection.owner_state)
        and not has_shadowed_owner_state(api)
        and model_unchanged(selection)
        and schema_unchanged(selection)
    )


def select_row_wire(binding: Binding) -> RowWireSelection | None:
    candidate = eligible_route(binding)
    queryset_original = (
        *ROW_WIRE_METHODS_ORIGINAL,
        object.__getattribute__,
        None,
        object.__setattr__,
    )
    if candidate is None or not static_state_matches(
        QuerySet, QUERYSET_GUARD_NAMES, queryset_original
    ):
        return None
    owner, resource = candidate
    model = resource.model
    schema = resource.read
    if type(model) is not type or type(schema) is not SchemaMeta:
        return None
    if not is_plain_model(model) or not is_plain_schema(schema):
        return None
    fields = schema_fields(model, schema)
    return (
        build_selection(owner, resource, model, schema, fields)
        if fields is not None
        else None
    )


def eligible_route(
    binding: Binding,
) -> tuple[type[CrudBase], _CrudConfig] | None:
    route = binding.route
    owner = route.owner
    if (
        owner is None
        or not is_generated_list(binding, owner)
        or len(route.renderers) != 1
        or not is_default_json_renderer(route.renderers[0])
        or not is_plain_binding(binding)
    ):
        return None
    resource = vars(owner).get("resource")
    if not is_plain_resource(resource, owner):
        return None

    return owner, resource


def build_selection(
    owner: type[CrudBase],
    resource: _CrudConfig,
    model: type[Model],
    schema: type[Schema],
    fields: list[tuple[str, str, str, bool]],
) -> RowWireSelection:
    plan = core._RowPlan(list(model.__columns__), fields)
    base = model.all()
    # Compile the validator now: the first compile later (the native CRUD lane
    # does it at startup) must not look like a patched ``__native__``.
    schema.native_schema()
    return RowWireSelection(
        plan=plan,
        owner=owner,
        owner_state=static_state(owner, OWNER_GUARD_NAMES),
        resource=resource,
        model=model,
        model_field_types=model.__field_types__,
        model_field_items=tuple(model.__field_types__.items()),
        model_state=static_state(model, MODEL_GUARD_NAMES),
        schema=schema,
        columns=model.__columns__,
        json_columns=model.__json_columns__,
        schema_specs=schema.specs(),
        schema_items=tuple(schema.specs().items()),
        model_init=model.__dict__["__init__"],
        column_descriptors=tuple(
            (name, inspect.getattr_static(model, name, None))
            for name in model.__columns__
        ),
        schema_init=schema.__dict__["__init__"],
        schema_state=static_state(schema, (*SCHEMA_GUARD_NAMES, "__init__")),
        schema_checks=schema.__checks__,
        schema_check_items=tuple(schema.__checks__),
        schema_normalizers=schema.__normalizers__,
        schema_normalizer_items=tuple(schema.__normalizers__),
        schema_native=schema.__dict__.get("__native__"),
        schema_config=schema.__schema_config__,
        schema_field_descriptors=tuple(
            (item.name, inspect.getattr_static(schema, item.name, None))
            for item in dataclasses.fields(cast(Any, schema))
        ),
        queryset_state=(
            *ROW_WIRE_METHODS_ORIGINAL,
            object.__getattribute__,
            None,
            object.__setattr__,
        ),
        base_filters=base.filters,
        base_implicit_filters=base.implicit_filter_count,
    )


_QUERYSET_SLOTS = (
    "model",
    "filters",
    "order",
    "limit_to",
    "offset_by",
    "loading",
    "implicit_filter_count",
)


def _queryset_state(query: object) -> dict[str, Any] | None:
    """Read the exact slots of a stock ``QuerySet`` without triggering descriptors."""
    if type(query) is not QuerySet:
        return None
    try:
        return {name: object.__getattribute__(query, name) for name in _QUERYSET_SLOTS}
    except AttributeError:
        return None


def _matches_base_filters(selection: RowWireSelection, filters: object) -> bool:
    if type(filters) is not tuple:
        return False
    scope = selection.resource.row_scope
    if scope is None:
        return filters == selection.base_filters
    base = selection.base_filters
    if len(filters) != len(base) + 1 or filters[: len(base)] != base:
        return False
    added = filters[-1]
    return (
        plain_filter(added)
        and added[0] == scope.column
        and added[1] == "eq"
        and len(added[2]) == 1
    )


def valid_base_query(
    selection: RowWireSelection, query: object
) -> TypeGuard[QuerySet[Any]]:
    """Require the exact unmodified queryset whose hydration the wire path skips."""
    attributes = _queryset_state(query)
    if attributes is None:
        return False
    expected = default_order(selection.model)
    return (
        _matches_base_filters(selection, attributes["filters"])
        and attributes["implicit_filter_count"] == selection.base_implicit_filters
    ) and valid_query_state(selection, query, order=expected, limit=None, offset=None)


def default_order(model: type[Model]) -> tuple[tuple[str, bool], ...]:
    return tuple(
        (name.removeprefix("-"), name.startswith("-")) for name in model.__ordering__
    )


def valid_query_state(
    selection: RowWireSelection,
    query: object,
    *,
    order: tuple[tuple[str, bool], ...],
    limit: int | None,
    offset: int | None,
) -> TypeGuard[QuerySet[Any]]:
    attributes = _queryset_state(query)
    if attributes is None:
        return False
    return (
        static_state_matches(QuerySet, QUERYSET_GUARD_NAMES, selection.queryset_state)
        and attributes["model"] is selection.model
        and type(attributes["filters"]) is tuple
        and type(attributes["implicit_filter_count"]) is int
        and all(plain_filter(item) for item in attributes["filters"])
        and attributes["order"] == order
        and attributes["limit_to"] == limit
        and attributes["offset_by"] == offset
        and valid_loading(attributes["loading"])
    )


def plain_filter(item: object) -> bool:
    """A condition on one of the model's own columns: ``(column, op, values)``."""
    return (
        type(item) is tuple
        and len(item) == CONDITION_PARTS
        and type(item[0]) is str
        and "." not in item[0]
        and type(item[1]) is str
        and type(item[2]) is list
    )


def valid_loading(value: object) -> bool:
    return (
        type(value) is Loading
        and not value.joined
        and not value.selected
        and not value.prefetched
    )


def model_unchanged(selection: RowWireSelection) -> bool:
    model = selection.model
    return (
        model.__dict__.get("__init__") is selection.model_init
        and model.__columns__ is selection.columns
        and model.__json_columns__ is selection.json_columns
        and model.__field_types__ is selection.model_field_types
        and tuple(model.__field_types__.items()) == selection.model_field_items
        and static_state_matches(model, MODEL_GUARD_NAMES, selection.model_state)
        and all(
            class_static(model, name) is expected
            for name, expected in selection.column_descriptors
        )
    )


def schema_unchanged(selection: RowWireSelection) -> bool:
    schema = selection.schema
    return (
        schema.__dict__.get("__init__") is selection.schema_init
        and static_state_matches(
            schema,
            (*SCHEMA_GUARD_NAMES, "__init__"),
            selection.schema_state,
        )
        and schema.__dict__.get("__specs__") is selection.schema_specs
        and tuple(schema.specs().items()) == selection.schema_items
        and schema.__checks__ is selection.schema_checks
        and type(schema.__checks__) is tuple
        and tuple(schema.__checks__) == selection.schema_check_items
        and schema.__normalizers__ is selection.schema_normalizers
        and type(schema.__normalizers__) is tuple
        and tuple(schema.__normalizers__) == selection.schema_normalizer_items
        and schema.__dict__.get("__native__") is selection.schema_native
        and schema.__schema_config__ is selection.schema_config
        and all(
            class_static(schema, name) is expected
            for name, expected in selection.schema_field_descriptors
        )
    )


def is_generated_list(binding: Binding, owner: type | None) -> bool:
    """Only generated CRUD list rows use this private specialization."""
    handler = binding.route.handler
    return bool(
        type(owner) is type
        and owner is not None
        and issubclass(owner, CrudBase)
        and handler.__name__ == "list"
        and getattr(handler, GENERATED, False)
        and binding.route.method == "GET"
        and not binding.route.atomic
    )


def is_plain_binding(binding: Binding) -> bool:
    sources = tuple(param.source for param in binding.params)
    return (
        binding.takes_request
        and not binding.needs_body
        and sources
        in {
            ("page",),
            ("page", "filters"),
            ("page", "principal"),
            ("page", "filters", "principal"),
        }
        and binding.params[0].strategy is not None
    )


def is_plain_resource(
    resource: object, owner: type[CrudBase]
) -> TypeGuard[_CrudConfig]:
    return bool(
        type(owner) is type
        and type(resource) is _CrudConfig
        and owner_is_plain(owner)
        and not resource.link_fields(resource.read)
    )


def owner_is_plain(owner: type[CrudBase]) -> bool:
    expected = (
        ("__init__", object.__init__),
        ("__new__", object.__new__),
        ("__getattribute__", object.__getattribute__),
        ("__getattr__", None),
        ("__setattr__", object.__setattr__),
        ("get_queryset", CrudBase.get_queryset),
        ("to_schema", CrudBase.to_schema),
        ("call_hook", CrudBase.call_hook),
        ("call_default_hook", CrudBase.call_default_hook),
        ("call_custom_hook", CrudBase.call_custom_hook),
    )
    return all(
        inspect.getattr_static(owner, name, None) is value for name, value in expected
    )


def is_plain_model(model: type[Model]) -> bool:
    return (
        type(model) is type
        and generated_model_init(model)
        and inspect.getattr_static(model, "__new__")
        is inspect.getattr_static(object, "__new__")
        and not hasattr(model, "__post_init__")
        and not model.__json_columns__
        and inspect.getattr_static(model, "__getattribute__")
        is inspect.getattr_static(Model, "__getattribute__")
        and inspect.getattr_static(model, "__getattr__", None)
        is inspect.getattr_static(Model, "__getattr__", None)
        and inspect.getattr_static(model, "__setattr__")
        is inspect.getattr_static(Model, "__setattr__")
        and inspect.getattr_static(model, "from_row")
        is inspect.getattr_static(Model, "from_row")
        and inspect.getattr_static(model, "remember")
        is inspect.getattr_static(Model, "remember")
        and inspect.getattr_static(model, "all") is inspect.getattr_static(Model, "all")
        and all(plain_column(model, name) for name in model.__columns__)
    )


def generated_model_init(model: type[Model]) -> bool:
    init = vars(model).get("__init__")
    code = getattr(init, "__code__", None)
    return code is not None and code.co_filename == "<string>"


def plain_column(owner: type, name: str) -> bool:
    descriptor = inspect.getattr_static(owner, name, None)
    # A field's own slot is the schema's storage, not a customization.
    return (
        descriptor is None
        or type(descriptor) is types.MemberDescriptorType
        or all(
            inspect.getattr_static(type(descriptor), slot, None) is None
            for slot in ("__get__", "__set__", "__delete__")
        )
    )


def is_plain_schema(schema: type[Schema]) -> bool:
    return bool(
        type(schema) is SchemaMeta
        and uses_generated_init(schema)
        and schema.dump is Schema.dump
        and inspect.getattr_static(schema, "_validate_instance")
        is inspect.getattr_static(Schema, "_validate_instance")
        and inspect.getattr_static(schema, "native_schema")
        is inspect.getattr_static(Schema, "native_schema")
        and inspect.getattr_static(schema, "specs")
        is inspect.getattr_static(Schema, "specs")
        and type(schema.__checks__) is tuple
        and not schema.__checks__
        and type(schema.__normalizers__) is tuple
        and not schema.__normalizers__
        and not schema.__computed_specs__
        and inspect.getattr_static(schema, "__post_init__", None) is None
        and inspect.getattr_static(schema, "__new__")
        is inspect.getattr_static(object, "__new__")
        and inspect.getattr_static(schema, "__getattribute__")
        is inspect.getattr_static(object, "__getattribute__")
        and inspect.getattr_static(schema, "__getattr__", None) is None
        and plain_schema_setattr(schema)
        and all(
            plain_column(schema, item.name)
            for item in dataclasses.fields(cast(Any, schema))
        )
    )


def plain_schema_setattr(schema: type[Schema]) -> bool:
    setter = inspect.getattr_static(schema, "__setattr__")
    if setter is inspect.getattr_static(object, "__setattr__"):
        return True
    code = getattr(setter, "__code__", None)
    params = getattr(schema, "__dataclass_params__", None)
    return bool(params and params.frozen and code and code.co_filename == "<string>")


def schema_fields(
    model: type[Model],
    schema: type[Schema],
    links: frozenset[str] = frozenset(),
) -> list[tuple[str, str, str, bool]] | None:
    """The fields of ``schema`` the row plan writes. ``links`` names the ids
    fields of many-to-many relations, which the plan fills from the bridge
    instead of a column."""
    fields: list[tuple[str, str, str, bool]] = []
    columns = set(model.__columns__)
    model_types = model.__field_types__
    schema_fields = {item.name: item for item in dataclasses.fields(cast(Any, schema))}
    for name, spec in schema.specs().items():
        field_type = spec.ftype
        if name in links:
            item = field_type.item
            if (
                field_type.nullable
                or field_type.allows_unset
                or item is None
                or set(field_type.info.constraints()) != {"max_length"}
                or item.info.constraints()
                or item.enum_class is not None
                or item.variants
            ):
                return None
            fields.append((name, spec.wire_name, "ids", False))
            continue
        if (
            name not in columns
            or field_type.kind not in ROW_KINDS
            or field_type.kind != model_types[name].kind
            or field_type.enum_class is not model_types[name].enum_class
            or field_type.nullable != model_types[name].nullable
            or field_type.info.constraints()
            or field_type.allows_unset
            or field_type.item is not None
            or field_type.schema is not None
            or field_type.discriminator is not None
            or field_type.variants
        ):
            return None
        schema_field = schema_fields[name]
        if (
            schema_field.default is not dataclasses.MISSING
            or schema_field.default_factory is not dataclasses.MISSING
        ):
            return None
        fields.append((name, spec.wire_name, field_type.kind, field_type.nullable))
    return fields
