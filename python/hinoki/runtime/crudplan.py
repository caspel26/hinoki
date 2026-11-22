"""Conservative startup selection and Rust compilation for generated CRUD plans."""

from __future__ import annotations

import dataclasses
import inspect
import math
import uuid
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

import hinoki.resources as crud_module
from hinoki import context, core
from hinoki import pagination as pagination_module
from hinoki.crypto import ClaimsJWTAuth
from hinoki.db import Database, DatabaseModule
from hinoki.errors import ErrorDetail, ErrorRegistry
from hinoki.events import Created, Deleted, EventBus, Updated
from hinoki.fields import UNSET
from hinoki.orm import Model, QuerySet
from hinoki.orm import queryset as queryset_module
from hinoki.orm.queryset import condition, write_statement, written_rows
from hinoki.pagination import PageNumberPagination, PageParams
from hinoki.parsers import DEFAULT_PARSERS, JSONParser
from hinoki.renderers import is_default_json_renderer
from hinoki.resources import (
    ACTIONS,
    GENERATED,
    CrudBase,
    _CrudConfig,
    caller_id,
    create_row,
    delete_row,
    list_rows,
    retrieve_row,
    update_row,
)
from hinoki.runtime.binding import Binding
from hinoki.runtime.responses import HTTPError
from hinoki.runtime.rowplan import (
    ROW_KINDS,
    class_static,
    is_plain_model,
    plain_column,
    plain_schema_setattr,
    schema_fields,
    static_state,
    static_state_matches,
)
from hinoki.schema import Schema, SchemaMeta, uses_generated_init
from hinoki.utils import utc_now

ACTION_ROUTES = {
    "list": ("GET", "/"),
    "retrieve": ("GET", "/{item_id}"),
    "create": ("POST", "/"),
    "update": ("PATCH", "/{item_id}"),
    "delete": ("DELETE", "/{item_id}"),
}
ACTION_HANDLERS = {
    "list": list_rows,
    "retrieve": retrieve_row,
    "create": create_row,
    "update": update_row,
    "delete": delete_row,
}
OWNER_METHODS = (
    "__init__",
    "__new__",
    "__getattribute__",
    "__getattr__",
    "__setattr__",
    "get_queryset",
    "written_queryset",
    "reload_written",
    "to_schema",
    "check_written",
    "refuse",
    "store",
    "change",
    "remove",
    "apply_change",
    "apply_removal",
    "call_hook",
    "call_default_hook",
    "call_custom_hook",
)
MODEL_METHODS = (
    "__new__",
    "__getattribute__",
    "__getattr__",
    "__setattr__",
    "__post_init__",
    "from_row",
    "remember",
    "insert",
    "acreate",
    "from_schema",
    "merged_values",
    "check_field",
    "validate_values",
    "aget",
    "all",
    "concrete",
    "load_stored",
    "write_insert",
    "write_update",
    "run_hooks",
    "before_save",
    "before_create",
    "after_create",
    "after_save",
)
# Overridden hooks only matter to the actions that write through the model.
HOOK_METHODS = frozenset(MODEL_METHODS[-4:])
HOOKED_ACTIONS = frozenset({"create", "update"})
WRITE_ACTIONS = frozenset({"create", "update", "delete"})
ACTION_EVENTS: dict[str, type] = {
    "create": Created,
    "update": Updated,
    "delete": Deleted,
}
QUERYSET_METHODS = (
    "__init__",
    "__await__",
    "compile",
    "derive",
    "filter",
    "order_by",
    "limit",
    "offset",
    "aget",
    "aupdate",
    "adelete",
    "acount",
    "apaginate",
    "fetch",
    "_hydrate",
    "build",
    "load",
)
DATABASE_METHODS = ("fetch", "execute", "atomic", "active", "dialect")
DATABASE_METHODS_ORIGINAL = static_state(Database, DATABASE_METHODS)
QUERYSET_METHODS_ORIGINAL = static_state(QuerySet, QUERYSET_METHODS)
OWNER_METHODS_ORIGINAL = static_state(CrudBase, OWNER_METHODS)
MODEL_METHODS_ORIGINAL = static_state(Model, MODEL_METHODS)
IMMUTABLE_TYPES = (type(None), bool, int, float, str)


@dataclass(frozen=True, slots=True)
class CrudPlanSelection:
    """A native plan and the identities and values needed for a cold recheck."""

    plan: core._CrudPlan
    action: str
    owner: type[CrudBase]
    resource: _CrudConfig
    model: type[Model]
    schemas: tuple[type[Schema], ...]
    binding: Binding
    app: Any
    database_module: DatabaseModule
    owner_state: tuple[object, ...]
    model_state: tuple[object, ...]
    queryset_state: tuple[object, ...]
    database_state: tuple[object, ...]
    schema_states: tuple[tuple[object, ...], ...]
    value_state: tuple[object, ...]
    route_snapshot: tuple[object, ...]
    modules_snapshot: tuple[object, ...]
    database_module_snapshot: tuple[object, ...]
    error_handlers_snapshot: tuple[object, ...]

    def snapshot_valid(self) -> bool:
        return (
            static_state_matches(self.owner, OWNER_METHODS, self.owner_state)
            and static_state_matches(self.model, MODEL_METHODS, self.model_state)
            and static_state_matches(QuerySet, QUERYSET_METHODS, self.queryset_state)
            and static_state_matches(Database, DATABASE_METHODS, self.database_state)
            and static_state_matches(
                DatabaseModule,
                DATABASE_MODULE_METHODS,
                DATABASE_MODULE_METHODS_ORIGINAL,
            )
            and static_state_matches(
                JSONParser, JSON_PARSER_METHODS, JSON_PARSER_METHODS_ORIGINAL
            )
            and static_state_matches(
                PageNumberPagination,
                PAGINATION_METHODS,
                PAGINATION_METHODS_ORIGINAL,
            )
            and static_state_matches(
                PageParams, PAGE_PARAMS_METHODS, PAGE_PARAMS_METHODS_ORIGINAL
            )
            and pagination_defaults() == PAGINATION_DEFAULTS
            and (
                queryset_module.condition,
                queryset_module.written_rows,
                queryset_module.write_statement,
            )
            == QUERYSET_FUNCTIONS_ORIGINAL
            and crud_handler_is_original(self.action)
            and public_route_is_plain(
                self.binding, self.owner, self.resource, self.action
            )
            and all(
                static_state_matches(schema, SCHEMA_METHODS, state)
                for schema, state in zip(self.schemas, self.schema_states, strict=True)
            )
            and value_state(self.owner, self.resource, self.model, self.schemas)
            == self.value_state
            and route_snapshot(self.binding) == self.route_snapshot
            and tuple(self.app.modules) == self.modules_snapshot
            and database_module_snapshot(self.database_module)
            == self.database_module_snapshot
            and error_handlers_snapshot(self.app) == self.error_handlers_snapshot
        )


@dataclass(frozen=True, slots=True)
class CrudCandidate:
    action: str
    owner: type[CrudBase]
    resource: _CrudConfig


SCHEMA_METHODS = (
    "__init__",
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
SCHEMA_METHODS_ORIGINAL = static_state(Schema, SCHEMA_METHODS)
CRUD_HANDLER_NAMES = (
    "list_rows",
    "retrieve_row",
    "create_row",
    "update_row",
    "delete_row",
)
CRUD_HANDLER_ORIGINALS = tuple(
    getattr(crud_module, name) for name in CRUD_HANDLER_NAMES
)
CRUD_RUNTIME_NAMES = (
    "asyncio",
    "inspect",
    "python_list_rows",
    "wire_list_rows",
    "schema_items",
    "current_database",
    "Page",
    "Response",
)
CRUD_RUNTIME_ORIGINALS = tuple(
    crud_module.list_rows.__globals__[name] for name in CRUD_RUNTIME_NAMES
)
# Names lane C resolves through ``hinoki.resources``'s module globals at request time.
CRUD_HELPER_NAMES = (
    "python_list_rows",
    "schema_items",
    "wire_list_rows",
    "current_database",
    "Page",
)
CRUD_HELPER_ORIGINALS = tuple(getattr(crud_module, name) for name in CRUD_HELPER_NAMES)
CONFIG_METHODS = ("values", "owner_of", "assigned")
CONFIG_METHODS_ORIGINAL = static_state(_CrudConfig, CONFIG_METHODS)
ERROR_REGISTRY_METHODS = ("handle", "add", "find", "translate")
ERROR_REGISTRY_METHODS_ORIGINAL = static_state(ErrorRegistry, ERROR_REGISTRY_METHODS)
QUERYSET_FUNCTIONS_ORIGINAL = (condition, written_rows, write_statement)
DATABASE_MODULE_METHODS = ("lifespan", "from_settings")
DATABASE_MODULE_METHODS_ORIGINAL = static_state(DatabaseModule, DATABASE_MODULE_METHODS)
JSON_PARSER_METHODS = ("__init__", "parse")
JSON_PARSER_METHODS_ORIGINAL = static_state(JSONParser, JSON_PARSER_METHODS)
PAGINATION_METHODS = ("__post_init__", "check", "sizes", "resolved", "parse", "number")
PAGINATION_METHODS_ORIGINAL = static_state(PageNumberPagination, PAGINATION_METHODS)
PAGE_PARAMS_METHODS = ("offset",)
PAGE_PARAMS_METHODS_ORIGINAL = static_state(PageParams, PAGE_PARAMS_METHODS)
PAGINATION_DEFAULTS = (
    pagination_module.DEFAULT_PAGE_SIZE,
    pagination_module.DEFAULT_MAX_PAGE_SIZE,
    pagination_module.MAX_NUMBER,
    pagination_module.MAX_DIGITS,
)


def select_crud_plan(
    binding: Binding, pipeline: Any
) -> tuple[CrudPlanSelection | None, str]:
    """Build a dark native plan, or return a stable reason for lane C."""
    if binding.route.middleware or binding.route.deprecated:
        return None, "middleware" if binding.route.middleware else "deprecated"
    candidate, reason = crud_candidate(binding)
    if candidate is None:
        return None, reason
    action, owner, resource = candidate.action, candidate.owner, candidate.resource
    model = resource.model
    checks = (
        (
            public_route_is_plain(binding, owner, resource, action),
            "custom_route_or_access_policy",
        ),
        (plain_crud_model(model, resource, action), "custom_or_unsupported_model"),
        (
            not action_has_event_handlers(pipeline.app.events, model, action),
            "model_event_handlers",
        ),
    )
    reason = next((label for passed, label in checks if not passed), "")
    schemas = action_schemas(resource)
    if not reason and not schemas_are_plain(schemas, model, resource):
        reason = "custom_or_unsupported_schema"
    if reason:
        return None, reason
    return finish_selection(binding, pipeline, candidate)


def crud_candidate(
    binding: Binding,
) -> tuple[CrudCandidate | None, str]:
    route = binding.route
    action = getattr(route.handler, "__name__", "")
    owner = route.owner
    is_owner = type(owner) is type and issubclass(owner, CrudBase)
    resource = vars(owner).get("resource") if is_owner else None
    generated_action = action in ACTIONS and getattr(route.handler, GENERATED, False)
    matches_template = (
        route_matches_action(route, action, cast(type[CrudBase], owner))
        if is_owner and type(resource) is _CrudConfig and generated_action
        else False
    )
    checks = (
        (generated_action, "not_generated_crud_action"),
        (is_owner, "custom_owner_type"),
        (type(resource) is _CrudConfig, "legacy_or_custom_resource"),
        (matches_template, "generated_route_shape_changed"),
    )
    reason = next((label for passed, label in checks if not passed), "")
    candidate = (
        CrudCandidate(action, cast(type[CrudBase], owner), cast(_CrudConfig, resource))
        if not reason
        else None
    )
    return candidate, reason or "eligible"


def finish_selection(
    binding: Binding,
    pipeline: Any,
    candidate: CrudCandidate,
) -> tuple[CrudPlanSelection | None, str]:
    resource = candidate.resource
    model = resource.model
    database_module = database_module_of(pipeline.app)
    checks = (
        (database_module is not None, "database_module_missing_or_custom"),
        (
            database_module is not None and dialect_of(database_module) is not None,
            "database_dialect_unsupported",
        ),
        (
            database_module is not None
            and not orders_decimal_as_text(model, dialect_of(database_module)),
            "decimal_order_unsupported_on_sqlite",
        ),
        (
            database_module is not None
            and database_is_plain(pipeline.app, database_module, model),
            "database_methods_or_not_found_handler_customized",
        ),
    )
    reason = next((label for passed, label in checks if not passed), "")
    selection = (
        compile_selection(
            candidate,
            binding,
            pipeline.app,
            database_module,
        )
        if not reason and database_module is not None
        else None
    )
    if not reason and selection is None:
        reason = "native_plan_build_failed"
    return (selection, "eligible") if not reason else (None, reason)


def route_matches_action(route: Any, action: str, owner: type[CrudBase]) -> bool:
    expected = ACTION_ROUTES.get(action)
    template = ACTION_HANDLERS.get(action)
    if expected is None or template is None:
        return False
    method, suffix = expected
    path = owner.prefix + ("" if suffix == "/" else suffix)
    return (
        inspect.isfunction(route.handler)
        and route.method == method
        and path_is_mounted(route.path, path)
        and route.handler.__code__ is template.__code__
        and route.handler.__globals__ is template.__globals__
        and crud_handler_is_original(action)
    )


def path_is_mounted(actual: str, declared: str) -> bool:
    """``actual`` is ``declared`` itself or ``declared`` under a ``Mount`` prefix.

    ``declared`` is the owner's path without a trailing slash (empty for the root).
    """
    if actual == (declared or "/"):
        return True
    if not actual.endswith(declared):
        return False
    mount = actual[: len(actual) - len(declared)]
    return mount.startswith("/") and not mount.endswith("/")


def crud_handler_is_original(action: str) -> bool:
    index = ACTIONS.index(action)
    return (
        getattr(crud_module, CRUD_HANDLER_NAMES[index]) is CRUD_HANDLER_ORIGINALS[index]
        and crud_helpers_are_original()
        and all(
            crud_module.list_rows.__globals__.get(name) is original
            for name, original in zip(
                CRUD_RUNTIME_NAMES, CRUD_RUNTIME_ORIGINALS, strict=True
            )
        )
    )


def pagination_defaults() -> tuple[int, int, int, int]:
    return (
        pagination_module.DEFAULT_PAGE_SIZE,
        pagination_module.DEFAULT_MAX_PAGE_SIZE,
        pagination_module.MAX_NUMBER,
        pagination_module.MAX_DIGITS,
    )


def crud_helpers_are_original() -> bool:
    return all(
        getattr(crud_module, name) is original
        for name, original in zip(CRUD_HELPER_NAMES, CRUD_HELPER_ORIGINALS, strict=True)
    ) and static_state_matches(_CrudConfig, CONFIG_METHODS, CONFIG_METHODS_ORIGINAL)


def public_route_is_plain(
    binding: Binding, owner: type[CrudBase], resource: _CrudConfig, action: str
) -> bool:
    route = binding.route
    return (
        owner_is_plain(owner)
        and owner_scope_is_plain(resource)
        and not resource.assign
        and access_is_plain(route, resource, action)
        and route.atomic == (action in {"create", "update", "delete"})
        and route_parsing_is_plain(route)
        and route_rendering_is_plain(route)
        and route_status_is_plain(route, action)
        and action_binding_is_plain(binding, action)
    )


def owner_scope_is_plain(resource: _CrudConfig) -> bool:
    """No owner, or one Rust can apply: the column is a text column that the
    client schemas cannot write, and its value is the caller's id."""
    if not resource.owner:
        return True
    model = resource.model
    field_type = model.__field_types__.get(resource.owner)
    return bool(
        resource.identity_key is caller_id
        and all(resource.access_for(a) is not None for a in WRITE_ACTIONS)
        and field_type is not None
        and field_type.kind == "str"
        and resource.owner != model.__primary_key__
        and resource.owner not in write_columns(model, resource, "update")
    )


CLAIMS_AUTH_METHODS = (
    "authenticate",
    "resolve",
    "resolve_token",
    "resolve_claims",
    "now",
    "headers",
)
CLAIMS_AUTH_METHODS_ORIGINAL = static_state(ClaimsJWTAuth, CLAIMS_AUTH_METHODS)


def claims_access_is_plain(declared: Any) -> bool:
    """The access policy's provider is an unmodified claims-only provider Rust
    can verify without running application code."""
    auth = declared.auth
    return bool(
        type(auth).__mro__.count(ClaimsJWTAuth) == 1
        and not vars(auth)
        and auth.header == "authorization"
        and static_state_matches(
            type(auth), CLAIMS_AUTH_METHODS, CLAIMS_AUTH_METHODS_ORIGINAL
        )
        and static_state_matches(
            ClaimsJWTAuth, CLAIMS_AUTH_METHODS, CLAIMS_AUTH_METHODS_ORIGINAL
        )
    )


def access_is_plain(route: Any, resource: _CrudConfig, action: str) -> bool:
    """The route's access is the resource's own and is either absent or a
    claims-only provider Rust can verify without running application code."""
    declared = resource.access_for(action)
    if route.access is not declared:
        return False
    return declared is None or claims_access_is_plain(declared)


def auth_snapshot(access: Any) -> tuple[object, ...]:
    """Everything Rust copies from a claims-only provider, for the cold recheck."""
    if access is None:
        return ()
    auth = access.auth
    return (
        auth.header,
        tuple(key.spec() for key in auth.keys),
        auth.issuer,
        auth.audience,
        auth.leeway,
        auth.require_exp,
        auth.scope_claim,
        tuple(sorted(access.permissions)),
    )


def native_claims_auth(access: Any) -> core._ClaimsAuth | None:
    if access is None:
        return None
    auth = access.auth
    return core._ClaimsAuth(
        [key.spec() for key in auth.keys],
        auth.issuer,
        auth.audience,
        auth.leeway,
        auth.require_exp,
        auth.scope_claim,
        sorted(access.permissions),
    )


def route_parsing_is_plain(route: Any) -> bool:
    return (
        route.parsers is DEFAULT_PARSERS
        and len(route.parsers) == 1
        and type(route.parsers[0]) is JSONParser
        and static_state_matches(
            JSONParser, JSON_PARSER_METHODS, JSON_PARSER_METHODS_ORIGINAL
        )
    )


def route_rendering_is_plain(route: Any) -> bool:
    return len(route.renderers) == 1 and is_default_json_renderer(route.renderers[0])


def route_status_is_plain(route: Any, action: str) -> bool:
    expected = {
        "list": 200,
        "retrieve": 200,
        "create": 201,
        "update": 200,
        "delete": 204,
    }
    return route.status == expected[action] and not route.errors


def action_binding_is_plain(binding: Binding, action: str) -> bool:
    if action == "list":
        strategy = next(
            (param.strategy for param in binding.params if param.source == "page"),
            None,
        )
        return type(strategy) is PageNumberPagination and expected_binding_shape(
            binding, action
        )
    return expected_binding_shape(binding, action)


def expected_binding_shape(binding: Binding, action: str) -> bool:
    expected = {
        "list": {"page": "page"},
        "retrieve": {"item_id": "path"},
        "create": {"data": "body"},
        "update": {"item_id": "path", "data": "body"},
        "delete": {"item_id": "path"},
    }[action]
    if binding.route.access is not None:
        expected = {**expected, "principal": "principal"}
    values = {parameter.name: parameter.source for parameter in binding.params}
    return (
        values == expected
        and binding.takes_request
        and binding.needs_body is (action in {"create", "update"})
    )


def owner_is_plain(owner: type[CrudBase]) -> bool:
    expected = (
        ("__init__", object.__init__),
        ("__new__", object.__new__),
        ("__getattribute__", object.__getattribute__),
        ("__getattr__", None),
        ("__setattr__", object.__setattr__),
        ("get_queryset", CrudBase.get_queryset),
        *tuple(
            (name, original)
            for name, original in zip(
                OWNER_METHODS[6:], OWNER_METHODS_ORIGINAL[6:], strict=True
            )
        ),
    )
    return all(class_static(owner, name) is value for name, value in expected)


def action_has_event_handlers(bus: EventBus, model: type[Model], action: str) -> bool:
    """Whether a handler (any phase) receives the model event the generated
    action emits. The native plan writes without the model, so it would skip
    the event: such an action stays on the Python lane."""
    kind = ACTION_EVENTS.get(action)
    return kind is not None and bus.registered(kind, model)


def plain_crud_model(model: type[Model], resource: _CrudConfig, action: str) -> bool:
    fields = model_fields(model)
    written = write_columns(model, resource, action)
    return bool(
        plain_model_methods(model, action in HOOKED_ACTIONS)
        and fields is not None
        and model.__field_types__[model.__primary_key__].kind in {"int", "uuid", "str"}
        and not set(written) & set(model.__field_validation_fields__)
        and model.__clock__ is utc_now
        and (resource.create is None or model_defaults_are_plain(model, resource))
    )


def plain_model_methods(model: type[Model], hooked: bool) -> bool:
    if type(model) is not type or model.__json_columns__:
        return False
    return (
        is_plain_model(model)
        and all(
            class_static(model, name) is original
            for name, original in zip(
                MODEL_METHODS, MODEL_METHODS_ORIGINAL, strict=True
            )
            if hooked or name not in HOOK_METHODS
        )
        and all(plain_column(model, column) for column in model.__columns__)
    )


def model_fields(model: type[Model]) -> list[tuple[str, str, bool]] | None:
    fields = []
    if len(model.__field_types__) != len(model.__columns__):
        return None
    for name in model.__columns__:
        field_type = model.__field_types__.get(name)
        if field_type is None or field_type.kind not in ROW_KINDS:
            return None
        fields.append((name, field_type.kind, field_type.nullable))
    return fields


def model_defaults_are_plain(model: type[Model], resource: _CrudConfig) -> bool:
    return model_default_specs(model, resource) is not None


def model_default_specs(
    model: type[Model], _resource: _CrudConfig
) -> list[tuple[str, str, str]] | None:
    try:
        fields = {item.name: item for item in dataclasses.fields(cast(Any, model))}
    except TypeError:
        return None
    found: list[tuple[str, str, str]] = []
    for name, item in fields.items():
        encoded = model_field_default(item)
        if encoded is None or not default_fits_column(model, name, encoded[0]):
            return None
        found.append((name, *encoded))
    return found


DEFAULT_COLUMN_KINDS = {
    "str": {"str"},
    "int": {"int"},
    "float": {"float"},
    "bool": {"bool"},
    "uuid4": {"uuid"},
    "utc_now": {"datetime"},
}


def default_fits_column(model: type[Model], name: str, default_kind: str) -> bool:
    """A default is native only when its kind is the column's own kind."""
    field_type = model.__field_types__.get(name)
    if field_type is None:
        return False
    if default_kind == "none":
        return field_type.nullable
    if default_kind == "required":
        return True
    return field_type.kind in DEFAULT_COLUMN_KINDS.get(default_kind, set())


def model_field_default(field: dataclasses.Field[Any]) -> tuple[str, str] | None:
    if field.default is not dataclasses.MISSING:
        encoded = encode_default(field.default)
    elif field.default_factory is dataclasses.MISSING:
        encoded = "required", ""
    elif field.default_factory is uuid.uuid4:
        encoded = "uuid4", ""
    elif field.default_factory is utc_now:
        encoded = "utc_now", ""
    else:
        encoded = None
    return encoded


def encode_default(value: object) -> tuple[str, str] | None:
    encoded: tuple[str, str] | None = None
    if value is None:
        encoded = "none", ""
    elif type(value) is bool:
        encoded = "bool", str(value).lower()
    elif type(value) is int and -(2**63) <= value < 2**63:
        encoded = "int", str(value)
    elif type(value) is float and math.isfinite(value):
        encoded = "float", repr(value)
    elif type(value) is str:
        encoded = "str", value
    return encoded


def schema_default_specs(
    schema: type[Schema] | None,
) -> list[tuple[str, str, str]] | None:
    if schema is None:
        return []
    found: list[tuple[str, str, str]] = []
    for item in dataclasses.fields(cast(Any, schema)):
        if item.default is dataclasses.MISSING or item.default is UNSET:
            continue
        encoded = encode_default(item.default)
        if encoded is None:
            return None
        found.append((item.name, *encoded))
    return found


def schema_defaults_fit(model: type[Model], *specs: list[tuple[str, str, str]]) -> bool:
    """Every schema default is stored as is only in a column of its own kind."""
    return all(
        default_fits_column(model, name, kind)
        for found in specs
        for name, kind, _value in found
    )


def action_schemas(resource: _CrudConfig) -> tuple[type[Schema], ...]:
    return tuple(
        schema
        for schema in (resource.read, resource.create, resource.update)
        if schema is not None
    )


def schemas_are_plain(
    schemas: tuple[type[Schema], ...],
    model: type[Model],
    resource: _CrudConfig,
) -> bool:
    roles = [(resource.read, "read")]
    if resource.create is not None:
        roles.append((resource.create, "create"))
    if resource.update is not None:
        roles.append((resource.update, "update"))
    return bool(schemas) and all(
        plain_crud_schema(schema, model, role) for schema, role in roles
    )


def plain_crud_schema(schema: type[Schema], model: type[Model], role: str) -> bool:
    compatible = (
        type(schema) is SchemaMeta
        and uses_generated_init(schema)
        and all(
            class_static(schema, name) is original
            for name, original in zip(
                (
                    name
                    for name in SCHEMA_METHODS
                    if name not in {"__init__", "__setattr__"}
                ),
                (
                    original
                    for name, original in zip(
                        SCHEMA_METHODS, SCHEMA_METHODS_ORIGINAL, strict=True
                    )
                    if name not in {"__init__", "__setattr__"}
                ),
                strict=True,
            )
        )
        and plain_schema_setattr(schema)
        and not schema.__checks__
        and not schema.__before__
        and not schema.__normalizers__
        and getattr(schema, "__post_init__", None) is None
        and (role != "read" or schema_fields(model, schema) is not None)
    )
    if not compatible:
        return False
    fields = {item.name: item for item in dataclasses.fields(cast(Any, schema))}
    for name, spec in schema.specs().items():
        kind = spec.ftype.kind
        valid = name in model.__columns__ and (
            kind in ROW_KINDS
            and kind == model.__field_types__[name].kind
            and spec.ftype.item is None
            and spec.ftype.schema is None
            and spec.ftype.discriminator is None
            and not spec.ftype.variants
            and (role == "update") == spec.ftype.allows_unset
        )
        if not valid or not schema_default_is_plain(fields[name], role):
            return False
    return (
        (role != "read" or read_schema_matches_rows(schema, model))
        and static_state_matches(Schema, SCHEMA_METHODS, SCHEMA_METHODS_ORIGINAL)
        and schema.native_schema() is not None
    )


def read_schema_matches_rows(schema: type[Schema], model: type[Model]) -> bool:
    """Stored rows are emitted as they are: no constraint may reject them and a
    non-null field must not be fed a nullable column."""
    fields = schema_fields(model, schema)
    return fields is not None and all(
        nullable == model.__field_types__[name].nullable
        for name, _wire, _kind, nullable in fields
    )


def schema_default_is_plain(field: dataclasses.Field[Any], role: str) -> bool:
    if role == "update":
        return field.default is UNSET and field.default_factory is dataclasses.MISSING
    if field.default is UNSET:
        return True
    if field.default is not dataclasses.MISSING:
        return type(field.default) in IMMUTABLE_TYPES
    return field.default_factory is dataclasses.MISSING


def write_columns(
    model: type[Model], resource: _CrudConfig, action: str
) -> tuple[str, ...]:
    if action == "create":
        return tuple(model.__columns__)
    if action == "update":
        names = list(resource.update.specs()) if resource.update is not None else []
        timestamp = model.__updated_at__
        if timestamp is not None and timestamp not in names:
            names.append(timestamp)
        return tuple(names)
    if action == "delete":
        return (model.__primary_key__,)
    return ()


def database_module_of(app: Any) -> DatabaseModule | None:
    found = [module for module in app.modules if type(module) is DatabaseModule]
    return found[0] if len(found) == 1 else None


def route_snapshot(binding: Binding) -> tuple[object, ...]:
    route = binding.route
    return (
        route.method,
        route.path,
        route.handler,
        route.status,
        route.access,
        route.atomic,
        route.parsers,
        route.renderers,
        route.errors,
        route.owner,
        auth_snapshot(route.access),
        tuple(
            (
                item.name,
                item.source,
                item.kind,
                item.default,
                item.convert,
                item.strategy,
            )
            for item in binding.params
        ),
        binding.needs_body,
        binding.takes_request,
    )


def database_module_snapshot(module: DatabaseModule) -> tuple[object, ...]:
    return module.url, module.max_size, module.wait


def error_handlers_snapshot(app: Any) -> tuple[object, ...]:
    errors = app.errors
    handlers = errors.handlers
    return (
        type(errors),
        static_state(type(errors), ERROR_REGISTRY_METHODS),
        id(handlers),
        tuple(handlers.items()),
        tuple(errors.defaults.items()),
    )


def orders_decimal_as_text(model: type[Model], dialect: str | None) -> bool:
    """SQLite stores a Decimal as text; the Python path refuses that ordering."""
    return dialect == "sqlite" and any(
        model.__field_types__[name.removeprefix("-")].kind == "decimal"
        for name in model.__ordering__
    )


def dialect_of(module: DatabaseModule) -> str | None:
    try:
        scheme = urlsplit(module.url).scheme
    except ValueError:
        return None
    normalized = {"postgresql": "postgres", "postgres": "postgres"}.get(scheme, scheme)
    return (
        normalized if normalized in {"postgres", "sqlite", "mysql", "mariadb"} else None
    )


def database_is_plain(app: Any, module: DatabaseModule, model: type[Model]) -> bool:
    if type(module) is not DatabaseModule or type(app.errors.handlers) is not dict:
        return False
    if not error_registry_is_plain(app.errors):
        return False
    if any(error in app.errors.handlers for error in model.NotFound.__mro__):
        return False
    return (
        all(
            class_static(Database, name) is original
            for name, original in zip(
                DATABASE_METHODS, DATABASE_METHODS_ORIGINAL, strict=True
            )
        )
        and all(
            class_static(DatabaseModule, name) is original
            for name, original in zip(
                DATABASE_MODULE_METHODS, DATABASE_MODULE_METHODS_ORIGINAL, strict=True
            )
        )
        and static_state_matches(QuerySet, QUERYSET_METHODS, QUERYSET_METHODS_ORIGINAL)
        and (
            queryset_module.condition,
            queryset_module.written_rows,
            queryset_module.write_statement,
        )
        == QUERYSET_FUNCTIONS_ORIGINAL
    )


def error_registry_is_plain(errors: Any) -> bool:
    return (
        type(errors) is ErrorRegistry
        and static_state_matches(
            ErrorRegistry, ERROR_REGISTRY_METHODS, ERROR_REGISTRY_METHODS_ORIGINAL
        )
        and errors.defaults == ErrorRegistry().defaults
    )


def compile_selection(
    candidate: CrudCandidate,
    binding: Binding,
    app: Any,
    database_module: DatabaseModule,
) -> CrudPlanSelection | None:
    action, resource = candidate.action, candidate.resource
    model = resource.model
    page_size, max_page_size = pagination_limits(binding, action)
    if page_size is None:
        return None
    schema_args = schema_plan_args(resource)
    create_defaults = schema_default_specs(resource.create)
    update_defaults = schema_default_specs(resource.update)
    read_fields = schema_fields(model, resource.read)
    if (
        schema_args is None
        or create_defaults is None
        or update_defaults is None
        or read_fields is None
        or not schema_defaults_fit(model, create_defaults, update_defaults)
    ):
        return None
    try:
        plan = core._CrudPlan(
            action,
            model.__name__,
            model.__table__,
            list(model.__columns__),
            model.__primary_key__,
            [parse_ordering(item) for item in model.__ordering__],
            page_size,
            max_page_size,
            model_fields(model) or [],
            model_default_specs(model, resource) or [],
            create_defaults,
            update_defaults,
            list(model.__columns__) if resource.create is not None else [],
            list(write_columns(model, resource, "update")),
            model.__updated_at__,
            schema_args[0],
            read_fields,
            create_schema=schema_args[1],
            update_schema=schema_args[2],
            validation_error=validation_error_template(),
            auth=native_claims_auth(binding.route.access),
            owner=resource.owner or None,
        )
    except (TypeError, ValueError):
        return None
    return make_selection(plan, candidate, binding, app, database_module)


def pagination_limits(binding: Binding, action: str) -> tuple[int | None, int]:
    if action != "list":
        return 1, 1
    strategy = next(
        (item.strategy for item in binding.params if item.source == "page"), None
    )
    if type(strategy) is not PageNumberPagination:
        return None, 0
    return strategy.sizes()


def schema_plan_args(
    resource: _CrudConfig,
) -> (
    tuple[core.CompiledSchema, core.CompiledSchema | None, core.CompiledSchema | None]
    | None
):
    try:
        return (
            resource.read.native_schema(),
            resource.create.native_schema() if resource.create is not None else None,
            resource.update.native_schema() if resource.update is not None else None,
        )
    except (TypeError, ValueError):
        return None


def parse_ordering(value: str) -> tuple[str, bool]:
    return value.removeprefix("-"), value.startswith("-")


def validation_error_template() -> bytes:
    sentinel = "0123456789abcdeffedcba9876543210"
    token = context.request_id.set(sentinel)
    try:
        detail = ErrorDetail("data.value", "type", "invalid value")
        return HTTPError(
            422, "validation_error", details=[dataclasses.asdict(detail)]
        ).response.body
    finally:
        context.request_id.reset(token)


def make_selection(
    plan: core._CrudPlan,
    candidate: CrudCandidate,
    binding: Binding,
    app: Any,
    database_module: DatabaseModule,
) -> CrudPlanSelection:
    action, owner, resource = candidate.action, candidate.owner, candidate.resource
    model = resource.model
    schemas = action_schemas(resource)
    return CrudPlanSelection(
        plan=plan,
        action=action,
        owner=owner,
        resource=resource,
        model=model,
        schemas=schemas,
        binding=binding,
        app=app,
        database_module=database_module,
        owner_state=static_state(owner, OWNER_METHODS),
        model_state=static_state(model, MODEL_METHODS),
        queryset_state=static_state(QuerySet, QUERYSET_METHODS),
        database_state=static_state(Database, DATABASE_METHODS),
        schema_states=tuple(static_state(schema, SCHEMA_METHODS) for schema in schemas),
        value_state=value_state(owner, resource, model, schemas),
        route_snapshot=route_snapshot(binding),
        modules_snapshot=tuple(app.modules),
        database_module_snapshot=database_module_snapshot(database_module),
        error_handlers_snapshot=error_handlers_snapshot(app),
    )


def value_state(
    owner: type[CrudBase],
    resource: _CrudConfig,
    model: type[Model],
    schemas: tuple[type[Schema], ...],
) -> tuple[object, ...]:
    return (
        owner.__dict__.get("resource"),
        tuple(getattr(owner, "response_headers", ())),
        resource.model,
        resource.read,
        resource.create,
        resource.update,
        resource.deletable,
        resource.access,
        resource.read_access,
        resource.public_writes,
        tuple((resource.actions or {}).items()),
        resource.omit,
        resource.owner,
        resource.identity_key,
        tuple(resource.assign.items()),
        (resource.pagination.page_size, resource.pagination.max_page_size),
        model.__columns__,
        tuple(model.__columns__),
        model.__table__,
        tuple(model.__field_types__.items()),
        tuple(model.__field_validation_fields__),
        tuple(model.__json_columns__),
        tuple(
            (item.name, item.default, item.default_factory)
            for item in dataclasses.fields(cast(Any, model))
        ),
        tuple(model.__ordering__),
        model.__primary_key__,
        model.__updated_at__,
        model.__clock__,
        tuple(
            (
                schema,
                schema.__dict__.get("__specs__"),
                tuple(schema.specs().items()),
                schema.__checks__,
                schema.__normalizers__,
                schema.__schema_config__,
                tuple(
                    (item.name, item.default, item.default_factory)
                    for item in dataclasses.fields(cast(Any, schema))
                ),
            )
            for schema in schemas
        ),
    )
