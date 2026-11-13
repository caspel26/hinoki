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
from typing import TYPE_CHECKING, Any, get_args, get_origin

from hinoki.errors import RegistrationError
from hinoki.fieldtypes import FieldType
from hinoki.runtime.binding import Binding, Param
from hinoki.schema import Schema

if TYPE_CHECKING:
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
}
LENGTH_KEYWORDS = {
    "string": ("minLength", "maxLength"),
    "array": ("minItems", "maxItems"),
}
VALIDATION_ERROR = "ValidationError"
DICT_ARGS = 2
SIMPLE_DEFAULTS = (str, int, float, bool)


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

    def build(self) -> dict[str, Any]:
        paths: dict[str, dict[str, Any]] = {}
        for binding in self.app.bindings:
            route = binding.route
            path = paths.setdefault(route.path, {})
            path[route.method.lower()] = self.operation(binding)
        document: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {"title": self.title, "version": self.version},
            "paths": paths,
        }
        if self.components:
            document["components"] = {"schemas": dict(sorted(self.components.items()))}
        return document

    def operation(self, binding: Binding) -> dict[str, Any]:
        route = binding.route
        operation: dict[str, Any] = {"operationId": self.operation_id(binding)}
        summary, description = split_docstring(route.handler)
        if summary:
            operation["summary"] = summary
        if description:
            operation["description"] = description
        parameters = [self.parameter(p) for p in binding.params if p.source != "body"]
        if parameters:
            operation["parameters"] = parameters
        body = next((p for p in binding.params if p.source == "body"), None)
        if body is not None and body.schema is not None:
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {"schema": self.component(body.schema)}
                },
            }
        operation["responses"] = self.responses(binding)
        if route.access is not None:
            operation["x-required-scopes"] = sorted(route.access.scopes)
        return operation

    def operation_id(self, binding: Binding) -> str:
        base = re.sub(r"\W+", "_", binding.route.name.replace("<locals>.", "")).strip(
            "_"
        )
        candidate, number = base, 2
        while candidate in self.operation_ids:
            candidate, number = f"{base}_{number}", number + 1
        self.operation_ids.add(candidate)
        return candidate

    def parameter(self, param: Param) -> dict[str, Any]:
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
        success: dict[str, Any] = {"description": "Successful response"}
        schema = (
            None if annotation in {None, type(None)} else self.annotation(annotation)
        )
        if schema is not None:
            success["content"] = {"application/json": {"schema": schema}}
        responses: dict[str, Any] = {str(route.status): success}
        if binding.params:
            self.components.setdefault(VALIDATION_ERROR, validation_error_schema())
            responses["422"] = {
                "description": "Validation error",
                "content": {"application/json": {"schema": ref(VALIDATION_ERROR)}},
            }
        return responses

    def annotation(self, annotation: object) -> dict[str, Any]:
        """JSON schema of a handler return annotation."""
        origin, args = get_origin(annotation), get_args(annotation)
        if isinstance(annotation, type) and issubclass(annotation, Schema):
            return self.component(annotation)
        if origin is list and args:
            return {"type": "array", "items": self.annotation(args[0])}
        if origin is dict and len(args) == DICT_ARGS:
            return {"type": "object", "additionalProperties": self.annotation(args[1])}
        return dict(SCALAR_TYPES.get(annotation, {}))  # type: ignore[call-overload]

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
                base[keyword] = value
        return {"anyOf": [base, {"type": "null"}]} if ftype.nullable else base


def constraint_keyword(name: str, schema: dict[str, Any]) -> str | None:
    fixed = CONSTRAINT_KEYWORDS[name]
    if fixed is not None:
        return fixed
    low, high = LENGTH_KEYWORDS[schema["type"]]
    return low if name == "min_length" else high


def split_docstring(handler: object) -> tuple[str, str]:
    doc = inspect.getdoc(handler) or ""
    summary, _, rest = doc.partition("\n")
    return summary.strip(), rest.strip()


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
