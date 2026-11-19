"""OpenAPI 3.1 document generated from the registered routes and schemas.

Everything comes from declarations that already exist (paths, handler
signatures, ``Schema`` fields, return annotations, docstrings); nothing is
written twice. Not exposed yet: examples, per-action deprecation and tags,
custom operation IDs, and response validation against the annotation.
"""

from __future__ import annotations

import inspect
import re
from decimal import Decimal
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, get_args, get_origin

from hinoki.errors import MAX_RETRY_AFTER, APIError, RegistrationError, TooManyRequests
from hinoki.fieldtypes import FieldType, is_union
from hinoki.handlerhints import hints_of
from hinoki.pagination import Page, PageNumberPagination
from hinoki.runtime.binding import Binding, Param
from hinoki.schema import Schema, SchemaExtra

if TYPE_CHECKING:
    from hinoki.access import Access
    from hinoki.app import Hinoki

SCALAR_TYPES: dict[type, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}
KIND_SCHEMAS: dict[str, dict[str, Any]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "uuid": {"type": "string", "format": "uuid"},
    "datetime": {"type": "string", "format": "date-time"},
    "date": {"type": "string", "format": "date"},
    "time": {"type": "string", "format": "time"},
    "timedelta": {"type": "string", "format": "duration"},
    "decimal": {
        "type": "string",
        "format": "decimal",
        "pattern": "^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$",
    },
    "bytes": {"type": "string", "format": "byte", "contentEncoding": "base64"},
    "ipv4": {"type": "string", "format": "ipv4"},
    "ipv6": {"type": "string", "format": "ipv6"},
    "ipv4network": {"type": "string", "format": "ipv4network"},
    "ipv6network": {"type": "string", "format": "ipv6network"},
    "path": {"type": "string", "format": "path"},
    "secret": {"type": "string", "format": "password", "writeOnly": True},
}
# Not keywords of the document: digit counts are enforced, not described, and
# the length and the pattern of a ``bytes`` field count decoded bytes, not base64
# characters; normalisation happens before the constraints, which describe its
# result.
UNDESCRIBED = {
    "decimal": frozenset({"max_digits", "decimal_places", "multiple_of"}),
    "bytes": frozenset({"min_length", "max_length", "pattern"}),
    "str": frozenset({"strip", "lower", "upper"}),
}
CONSTRAINT_KEYWORDS = {
    "min_length": None,  # depends on the kind (string vs array)
    "max_length": None,
    "pattern": "pattern",
    "ge": "minimum",
    "le": "maximum",
    "gt": "exclusiveMinimum",
    "lt": "exclusiveMaximum",
    "one_of": "enum",
    "multiple_of": "multipleOf",
}
LENGTH_KEYWORDS = {
    "string": ("minLength", "maxLength"),
    "array": ("minItems", "maxItems"),
    "object": ("minProperties", "maxProperties"),
}
VALIDATION_ERROR = "ValidationError"
ERROR = "Error"
DICT_ARGS = 2
SIMPLE_DEFAULTS = (str, int, float, bool)
INPUTS = {"path", "query", "page", "queryschema"}
BODYLESS_STATUSES = {HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED}


def operation_key(method: str) -> str:
    """The path item field holding a method's operation. OpenAPI 3.1 has no
    ``query`` operation (3.2 adds it), so QUERY uses the ``x-query`` extension
    field with the same operation object."""
    return "x-query" if method == "QUERY" else method.lower()


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


class OpenAPIBuilder:
    """Builds one document; create a new builder per document."""

    def __init__(self, app: Hinoki, title: str, version: str) -> None:
        self.app = app
        self.title = title
        self.version = version
        self.components: dict[str, dict[str, Any]] = {}
        self.owners: dict[str, type[Schema]] = {}
        self.operation_ids: set[str] = set()
        self.security_schemes: dict[str, dict[str, Any]] = {}

    def build(self) -> dict[str, Any]:
        paths: dict[str, dict[str, Any]] = {}
        for binding in self.app.bindings:
            route = binding.route
            if getattr(route.owner, "hide_from_docs", False):
                continue  # HTML pages (the admin) are not part of the API contract
            path = paths.setdefault(route.path, {})
            path[operation_key(route.method)] = self.operation(binding)
        document: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {"title": self.title, "version": self.version},
            "paths": paths,
        }
        components: dict[str, Any] = {}
        if self.components:
            components["schemas"] = dict(sorted(self.components.items()))
        if self.security_schemes:
            components["securitySchemes"] = self.security_schemes
        if components:
            document["components"] = components
        return document

    def operation(self, binding: Binding) -> dict[str, Any]:
        route = binding.route
        operation: dict[str, Any] = {"operationId": self.operation_id(binding)}
        summary, description = split_docstring(route.handler)
        if summary:
            operation["summary"] = summary
        if description:
            operation["description"] = description
        parameters = [
            entry
            for p in binding.params
            if p.source in INPUTS
            for entry in self.parameter(p)
        ]
        if parameters:
            operation["parameters"] = parameters
        file_params = [p for p in binding.params if p.source == "file"]
        body = next((p for p in binding.params if p.source == "body"), None)
        if file_params:
            properties = {
                param.name: self.file_schema(param.file_mode == "many")
                for param in file_params
            }
            required_files = [
                param.name for param in file_params if param.file_mode != "optional"
            ]
            schema: dict[str, Any] = {"type": "object", "properties": properties}
            if required_files:
                schema["required"] = required_files
            operation["requestBody"] = {
                "required": bool(required_files),
                "content": {"multipart/form-data": {"schema": schema}},
            }
        elif body is not None and body.schema is not None:
            operation["requestBody"] = {
                "required": True,
                "content": {
                    parser.media_type: {"schema": self.component(body.schema)}
                    for parser in route.parsers
                },
            }
        operation["responses"] = self.responses(binding)
        if route.access is not None:
            self.secure(operation, route.access)
        self.declare_errors(operation["responses"], route.errors)
        return operation

    @staticmethod
    def file_schema(many: bool) -> dict[str, Any]:
        """OpenAPI's binary file shape, optionally repeated as an array."""
        binary: dict[str, Any] = {"type": "string", "format": "binary"}
        return {"type": "array", "items": binary} if many else binary

    def declare_errors(
        self, responses: dict[str, Any], errors: tuple[type[APIError], ...]
    ) -> None:
        """One response per declared status: the shared ``Error`` body with the
        possible ``error`` codes listed. Errors sharing a status are merged."""
        if not errors:
            return
        self.components.setdefault(ERROR, error_schema())
        by_status: dict[str, list[type[APIError]]] = {}
        for error in errors:
            by_status.setdefault(str(int(error.status)), []).append(error)
        for status, group in by_status.items():
            codes = sorted({e.code for e in group})
            messages = " / ".join(dict.fromkeys(e.message for e in group))
            known = responses.get(status)
            description = f"{known['description']}; {messages}" if known else messages
            responses[status] = {
                "description": description,
                **retry_after_header(group),
                "content": {
                    "application/json": {
                        "schema": {
                            "allOf": [
                                ref(ERROR),
                                {
                                    "type": "object",
                                    "properties": {"error": {"enum": codes}},
                                },
                            ]
                        }
                    }
                },
            }

    def secure(self, operation: dict[str, Any], access: Access) -> None:
        """Document the policy: security requirement plus its 401/403 answers."""
        name = type(access.auth).__name__
        scheme = access.auth.openapi_scheme()
        if scheme is not None:
            self.security_schemes.setdefault(name, scheme)
            operation["security"] = [{name: sorted(access.permissions)}]
        responses = operation["responses"]
        responses["401"] = {"description": "Missing or invalid credentials"}
        if access.permissions:
            responses["403"] = {"description": "Missing required scope"}

    def operation_id(self, binding: Binding) -> str:
        base = re.sub(r"\W+", "_", binding.route.name.replace("<locals>.", "")).strip(
            "_"
        )
        candidate, number = base, 2
        while candidate in self.operation_ids:
            candidate, number = f"{base}_{number}", number + 1
        self.operation_ids.add(candidate)
        return candidate

    def parameter(self, param: Param) -> list[dict[str, Any]]:
        if param.source == "page" and param.strategy is not None:
            return page_parameters(param.strategy)
        if param.source == "queryschema" and param.schema is not None:
            return [
                {
                    "name": spec.wire_name,
                    "in": "query",
                    "required": spec.required,
                    "schema": self.field(spec.ftype),
                }
                for spec in param.schema.fields().values()
            ]
        return [self.single_parameter(param)]

    def single_parameter(self, param: Param) -> dict[str, Any]:
        required = param.source == "path" or param.default is inspect.Parameter.empty
        schema = dict(KIND_SCHEMAS[param.kind])
        if not required and isinstance(param.default, SIMPLE_DEFAULTS):
            schema["default"] = param.default
        return {
            "name": param.name,
            "in": param.source,
            "required": required,
            "schema": schema,
        }

    def responses(self, binding: Binding) -> dict[str, Any]:
        route = binding.route
        annotation = hints_of(route.handler).get("return")
        empty = annotation in {None, type(None)} or route.status in BODYLESS_STATUSES
        status = 204 if empty and route.status == HTTPStatus.OK else route.status
        success: dict[str, Any] = {"description": "Successful response"}
        if not empty:
            schema = self.annotation(annotation)
            success["content"] = {
                renderer.media_type: {"schema": schema} for renderer in route.renderers
            }
        responses: dict[str, Any] = {str(status): success}
        if any(p.source != "principal" for p in binding.params):
            self.components.setdefault(VALIDATION_ERROR, validation_error_schema())
            responses["422"] = {
                "description": "Validation error",
                "content": {"application/json": {"schema": ref(VALIDATION_ERROR)}},
            }
        return responses

    def annotation(self, annotation: object) -> dict[str, Any]:
        """JSON schema of a handler return annotation."""
        if isinstance(annotation, type) and issubclass(annotation, Schema):
            return self.component(annotation)
        if is_union(annotation):
            raise RegistrationError(
                "bare union return annotations are unsupported; use a wrapper Schema"
            )
        generic = self.generic(get_origin(annotation), get_args(annotation))
        if generic is not None:
            return generic
        return dict(SCALAR_TYPES.get(annotation, {}))  # type: ignore[call-overload]

    def generic(self, origin: object, args: tuple[Any, ...]) -> dict[str, Any] | None:
        if origin is list and args:
            return {"type": "array", "items": self.annotation(args[0])}
        if origin is Page and args:
            return self.page_schema(args[0])
        if origin is dict and len(args) == DICT_ARGS:
            return {"type": "object", "additionalProperties": self.annotation(args[1])}
        return None

    def page_schema(self, item: object) -> dict[str, Any]:
        items = self.annotation(item)
        name = "Page_" + str(items.get("$ref", "item")).rsplit("/", 1)[-1]
        if name not in self.components:
            integer: dict[str, Any] = {"type": "integer"}
            self.components[name] = {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": items},
                    "page": integer,
                    "page_size": integer,
                    "total": integer,
                    "pages": integer,
                },
                "required": ["items", "page", "page_size", "total", "pages"],
            }
        return ref(name)

    def component(self, schema: type[Schema]) -> dict[str, str]:
        name = schema.__name__
        known = self.owners.get(name)
        if known is not None and known is not schema:
            raise RegistrationError(f"two different schemas are both named {name!r}")
        if known is None:
            self.owners[name] = schema
            self.components[name] = {}  # placeholder: breaks recursion
            self.components[name] = self.object_schema(schema)
        return ref(name)

    def object_schema(self, schema: type[Schema]) -> dict[str, Any]:
        specs = schema.fields()
        result: dict[str, Any] = {
            "type": "object",
            "properties": {s.wire_name: self.field(s.ftype) for s in specs.values()},
            "additionalProperties": schema.__schema_config__.extra
            is SchemaExtra.IGNORE,
        }
        for computed in schema.computed_fields().values():
            result["properties"][computed.wire_name] = self.computed(schema, computed)
        required = [s.wire_name for s in specs.values() if s.required]
        if required:
            result["required"] = required
        if schema.__schema_config__.extra is SchemaExtra.IGNORE:
            alias_pairs = [
                {"not": {"required": [spec.wire_name, spec.name]}}
                for spec in specs.values()
                if spec.alias is not None and spec.wire_name != spec.name
            ]
            if alias_pairs:
                result["allOf"] = alias_pairs
        if schema.__doc__:
            result["description"] = inspect.cleandoc(schema.__doc__).split("\n\n")[0]
        return result

    def computed(self, schema: type[Schema], spec: Any) -> dict[str, Any]:
        """A computed field: output only, so it is read-only and never required."""
        result = dict(self.field(spec.field_type(schema)))
        result["readOnly"] = True
        if spec.description is not None:
            result["description"] = spec.description
        if spec.examples:
            result["examples"] = list(spec.examples)
        return result

    def shape(self, ftype: FieldType) -> dict[str, Any]:
        """The JSON Schema of a field type before its constraints are added."""
        if ftype.kind == "anyof":
            return {"anyOf": [self.field(child) for child in ftype.items]}
        if ftype.item is not None or ftype.kind == "tuple":
            return self.container(ftype, ftype.item)
        if ftype.kind in {"schema", "union"}:
            return self.reference(ftype)
        return dict(KIND_SCHEMAS.get(ftype.kind, {}))

    def reference(self, ftype: FieldType) -> dict[str, Any]:
        if ftype.kind == "schema" and ftype.schema is not None:
            return self.component(ftype.schema)
        return self.union(ftype)

    def container(self, ftype: FieldType, item: FieldType | None) -> dict[str, Any]:
        """A list, set or tuple as an array; a dict as an object whose property
        names follow the key type and whose values follow the value type."""
        if item is None:
            return {
                "type": "array",
                "prefixItems": [self.field(child) for child in ftype.items],
                "items": False,
            }
        if ftype.kind == "dict" and ftype.key is not None:
            return {
                "type": "object",
                "additionalProperties": self.field(item),
                "propertyNames": self.field(ftype.key),
            }
        array: dict[str, Any] = {"type": "array", "items": self.field(item)}
        if ftype.collection in {"set", "frozenset"}:
            array["uniqueItems"] = True
        return array

    def union(self, ftype: FieldType) -> dict[str, Any]:
        variants = [(tag, schema) for tag, schema in ftype.variants]
        return {
            "oneOf": [self.component(schema) for _, schema in variants],
            "discriminator": {
                "propertyName": ftype.discriminator_wire_name,
                "mapping": {
                    tag: f"#/components/schemas/{schema.__name__}"
                    for tag, schema in variants
                },
            },
        }

    def field(self, ftype: FieldType) -> dict[str, Any]:
        described = self.constrained(ftype)
        if ftype.kind != "any" and ftype.nullable:
            described = {"anyOf": [described, {"type": "null"}]}
        notes = {k: document_value(v) for k, v in ftype.info.notes().items()}
        return {**described, **notes}

    def constrained(self, ftype: FieldType) -> dict[str, Any]:
        base = self.shape(ftype)
        if ftype.kind == "any":
            return base
        skipped = UNDESCRIBED.get(ftype.kind, frozenset())
        for name, value in ftype.info.constraints().items():
            if name in skipped:
                continue
            keyword = constraint_keyword(name, base)
            if keyword is not None:
                base[keyword] = document_value(value)
            elif name == "pattern":
                # JSON Schema patterns are unanchored; the runtime matches fully.
                base["pattern"] = f"^(?:{value})$"
        return base


def document_value(value: object) -> object:
    """A constraint value as JSON: a tuple as a list, a ``Decimal`` as a number."""
    if isinstance(value, tuple):
        return list(value)
    return float(value) if isinstance(value, Decimal) else value


def constraint_keyword(name: str, schema: dict[str, Any]) -> str | None:
    fixed = CONSTRAINT_KEYWORDS[name]
    if name == "pattern":
        return None  # anchored form is added by the caller
    if fixed is not None:
        return fixed
    low, high = LENGTH_KEYWORDS[schema["type"]]
    return low if name == "min_length" else high


def page_parameters(strategy: PageNumberPagination) -> list[dict[str, Any]]:
    return [
        {
            "name": "page",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1, "default": 1},
        },
        {
            "name": "page_size",
            "in": "query",
            "required": False,
            "schema": {
                "type": "integer",
                "minimum": 1,
                "maximum": strategy.sizes()[1],
                "default": strategy.sizes()[0],
            },
        },
    ]


def split_docstring(handler: object) -> tuple[str, str]:
    doc = inspect.getdoc(handler) or ""
    summary, _, rest = doc.partition("\n")
    return summary.strip(), rest.strip()


def retry_after_header(group: list[type[APIError]]) -> dict[str, Any]:
    """``Retry-After`` as a required whole number of seconds on a rate-limit 429."""
    if not any(issubclass(error, TooManyRequests) for error in group):
        return {}
    return {
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before trying again.",
                "required": True,
                "schema": {"type": "integer", "minimum": 1, "maximum": MAX_RETRY_AFTER},
            }
        }
    }


def error_schema() -> dict[str, Any]:
    """The body every ``APIError`` answers with."""
    return {
        "type": "object",
        "properties": {
            "error": {"type": "string", "description": "Stable error code"},
            "message": {"type": "string"},
            "request_id": {"type": "string"},
        },
        "required": ["error", "request_id"],
    }


def validation_error_schema() -> dict[str, Any]:
    detail = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "code": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["path", "code", "message"],
    }
    return {
        "type": "object",
        "properties": {
            "error": {"type": "string"},
            "details": {"type": "array", "items": detail},
        },
        "required": ["error", "details"],
    }
