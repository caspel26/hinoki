"""Registration-time selection for the private generated-list row-wire path."""

from __future__ import annotations

import dataclasses
import inspect
from dataclasses import dataclass
from typing import Any, TypeGuard, cast

from hinoki import core
from hinoki.crud import GENERATED, CrudAPI, _CrudConfig
from hinoki.orm import Model
from hinoki.orm.queryset import (
    ROW_WIRE_METHODS,
    ROW_WIRE_METHODS_ORIGINAL,
    Loading,
    QuerySet,
)
from hinoki.renderers import is_default_json_renderer
from hinoki.runtime.binding import Binding
from hinoki.schema import Schema, uses_generated_init

ROW_KINDS = frozenset({"str", "int", "float", "bool", "uuid", "datetime"})
OWNER_GUARD_NAMES = (
    "__init__",
    "__new__",
    "__getattribute__",
    "__getattr__",
    "__setattr__",
    "get_queryset",
    "to_schema",
    "invoke_hook",
    "invoke_default_hook",
    "invoke_custom_hook",
)
OWNER_INSTANCE_NAMES = (
    "get_queryset",
    "to_schema",
    "invoke_hook",
    "invoke_default_hook",
    "invoke_custom_hook",
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
    owner: type[CrudAPI]
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

    def valid_query(self, query: object) -> TypeGuard[QuerySet[Any]]:
        return valid_base_query(self, query)

    def valid_window(
        self,
        query: object,
        *,
        order: tuple[tuple[str, bool], ...],
        limit: int,
        offset: int,
    ) -> TypeGuard[QuerySet[Any]]:
        return valid_query_state(self, query, order=order, limit=limit, offset=offset)

    def valid_for(self, api: CrudAPI) -> bool:
        """A changed class/resource/schema falls back before any database work."""
        return (
            type(api) is self.owner
            and selection_behavior_unchanged(self, api)
            and api.resource is self.resource
            and api.model is self.model
            and api.read_schema is self.schema
        )


def with_row_wire(binding: Binding) -> Binding:
    """Attach a route plan after this app resolved and validated its renderer."""
    selection = select_row_wire(binding)
    if selection is None:
        return binding
    return dataclasses.replace(binding, row_wire=selection)


def static_state(owner: type, names: tuple[str, ...]) -> tuple[object, ...]:
    return tuple(inspect.getattr_static(owner, name, None) for name in names)


def static_state_matches(
    owner: type, names: tuple[str, ...], expected: tuple[object, ...]
) -> bool:
    return all(
        inspect.getattr_static(owner, name, None) is value
        for name, value in zip(names, expected, strict=True)
    )


def has_shadowed_owner_state(api: CrudAPI) -> bool:
    try:
        attributes = object.__getattribute__(api, "__dict__")
    except AttributeError:
        return True
    return any(name in attributes for name in OWNER_INSTANCE_NAMES)


def selection_behavior_unchanged(selection: RowWireSelection, api: CrudAPI) -> bool:
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
    if type(model) is not type or type(schema) is not type:
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
) -> tuple[type[CrudAPI], _CrudConfig] | None:
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
    owner: type[CrudAPI],
    resource: _CrudConfig,
    model: type[Model],
    schema: type[Schema],
    fields: list[tuple[str, str, str, bool]],
) -> RowWireSelection:
    plan = core._RowPlan(list(model.__columns__), fields)
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
    )


def valid_base_query(
    selection: RowWireSelection, query: object
) -> TypeGuard[QuerySet[Any]]:
    """Require the exact unmodified queryset whose hydration the wire path skips."""
    if type(query) is not QuerySet:
        return False
    attributes = object.__getattribute__(query, "__dict__")
    if set(attributes) != {
        "model",
        "filters",
        "order",
        "limit_to",
        "offset_by",
        "loading",
    }:
        return False
    expected = default_order(selection.model)
    return valid_query_state(selection, query, order=expected, limit=None, offset=None)


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
    if type(query) is not QuerySet:
        return False
    attributes = object.__getattribute__(query, "__dict__")
    return (
        set(attributes)
        == {
            "model",
            "filters",
            "order",
            "limit_to",
            "offset_by",
            "loading",
        }
        and static_state_matches(
            QuerySet, QUERYSET_GUARD_NAMES, selection.queryset_state
        )
        and attributes["model"] is selection.model
        and type(attributes["filters"]) is tuple
        and not attributes["filters"]
        and attributes["order"] == order
        and attributes["limit_to"] == limit
        and attributes["offset_by"] == offset
        and valid_loading(attributes["loading"])
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
            inspect.getattr_static(model, name, None) is expected
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
            inspect.getattr_static(schema, name, None) is expected
            for name, expected in selection.schema_field_descriptors
        )
    )


def is_generated_list(binding: Binding, owner: type | None) -> bool:
    """Only generated CrudAPI list rows use this private specialization."""
    handler = binding.route.handler
    return bool(
        type(owner) is type
        and owner is not None
        and issubclass(owner, CrudAPI)
        and handler.__name__ == "list"
        and getattr(handler, GENERATED, False)
        and binding.route.method == "GET"
        and binding.route.access is None
        and not binding.route.transaction
    )


def is_plain_binding(binding: Binding) -> bool:
    return (
        binding.takes_request
        and not binding.needs_body
        and len(binding.params) == 1
        and binding.params[0].source == "page"
        and binding.params[0].strategy is not None
    )


def is_plain_resource(resource: object, owner: type[CrudAPI]) -> TypeGuard[_CrudConfig]:
    return bool(
        type(owner) is type
        and type(resource) is _CrudConfig
        and owner_is_plain(owner)
        and resource.read_access is None
        and not resource.owner
        and not resource.assign
    )


def owner_is_plain(owner: type[CrudAPI]) -> bool:
    expected = (
        ("__init__", object.__init__),
        ("__new__", object.__new__),
        ("__getattribute__", object.__getattribute__),
        ("__getattr__", None),
        ("__setattr__", object.__setattr__),
        ("get_queryset", CrudAPI.get_queryset),
        ("to_schema", CrudAPI.to_schema),
        ("invoke_hook", CrudAPI.invoke_hook),
        ("invoke_default_hook", CrudAPI.invoke_default_hook),
        ("invoke_custom_hook", CrudAPI.invoke_custom_hook),
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
    return descriptor is None or all(
        inspect.getattr_static(type(descriptor), slot, None) is None
        for slot in ("__get__", "__set__", "__delete__")
    )


def is_plain_schema(schema: type[Schema]) -> bool:
    return bool(
        type(schema) is type
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
    model: type[Model], schema: type[Schema]
) -> list[tuple[str, str, str, bool]] | None:
    fields: list[tuple[str, str, str, bool]] = []
    columns = set(model.__columns__)
    model_types = model.__field_types__
    schema_fields = {item.name: item for item in dataclasses.fields(cast(Any, schema))}
    for name, spec in schema.specs().items():
        field_type = spec.ftype
        if (
            name not in columns
            or field_type.kind not in ROW_KINDS
            or field_type.kind != model_types[name].kind
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
