"""OpenAPI 3.1 document generated from the registered routes and schemas.

Everything comes from declarations that already exist (paths, handler
signatures, ``Schema`` fields, return annotations, docstrings); nothing is
written twice. Not covered yet: auth security schemes, examples, deprecation,
custom error documentation and response validation against the annotation.
"""

from __future__ import annotations

import inspect
import re
import typing
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, get_args, get_origin

from hinoki.errors import RegistrationError
from hinoki.fieldtypes import FieldType
from hinoki.httperrors import APIError
from hinoki.pagination import Page, PageNumberPagination
from hinoki.runtime.binding import Binding, Param
from hinoki.schema import Schema

if TYPE_CHECKING:
    from hinoki.access import Access
    from hinoki.app import HinokiAPI

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
}
LENGTH_KEYWORDS = {
    "string": ("minLength", "maxLength"),
    "array": ("minItems", "maxItems"),
}
VALIDATION_ERROR = "ValidationError"
ERROR = "Error"
DICT_ARGS = 2
SIMPLE_DEFAULTS = (str, int, float, bool)
INPUTS = {"path", "query", "page"}
BODYLESS_STATUSES = {HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED}


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


class OpenAPIBuilder:
    """Builds one document; create a new builder per document."""

    def __init__(self, app: HinokiAPI, title: str, version: str) -> None:
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
            path[route.method.lower()] = self.operation(binding)
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
        body = next((p for p in binding.params if p.source == "body"), None)
        if body is not None and body.schema is not None:
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
        annotation = typing.get_type_hints(route.handler).get("return")
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
            "properties": {n: self.field(s.ftype) for n, s in specs.items()},
            "additionalProperties": False,
        }
        required = [n for n, s in specs.items() if s.required]
        if required:
            result["required"] = required
        if schema.__doc__:
            result["description"] = inspect.cleandoc(schema.__doc__).split("\n\n")[0]
        return result

    def field(self, ftype: FieldType) -> dict[str, Any]:
        if ftype.kind == "list" and ftype.item is not None:
            base: dict[str, Any] = {"type": "array", "items": self.field(ftype.item)}
        elif ftype.kind == "schema" and ftype.schema is not None:
            base = self.component(ftype.schema)
        else:
            base = dict(KIND_SCHEMAS[ftype.kind])
        for name, value in ftype.info.constraints().items():
            keyword = constraint_keyword(name, base)
            if keyword is not None:
                base[keyword] = list(value) if isinstance(value, tuple) else value
            elif name == "pattern":
                # JSON Schema patterns are unanchored; the runtime matches fully.
                base["pattern"] = f"^(?:{value})$"
        return {"anyOf": [base, {"type": "null"}]} if ftype.nullable else base


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
                "maximum": strategy.max_page_size,
                "default": strategy.page_size,
            },
        },
    ]


def split_docstring(handler: object) -> tuple[str, str]:
    doc = inspect.getdoc(handler) or ""
    summary, _, rest = doc.partition("\n")
    return summary.strip(), rest.strip()


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
