"""Valid and invalid request bodies generated from an OpenAPI document.

``request_cases(app.openapi())`` gives, for each operation with a JSON body, one body
that satisfies the schema and a list of bodies that each break exactly one rule, so a
test can assert ``2xx`` for the first and ``422`` for the rest. Nothing here reads
application code: it follows only what the document declares.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from hinoki.openapi import operation_of

MAX_DEPTH = 3
JSON = "application/json"
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace", "query")
FORMATS = {
    "date-time": "2026-01-02T03:04:05Z",
    "date": "2026-01-02",
    "time": "03:04:05",
    "duration": "PT1S",
    "uuid": "123e4567-e89b-42d3-a456-426614174000",
    "email": "user@example.com",
    "uri": "https://example.com/",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "decimal": "1",
    "byte": "AAAA",
}
WRONG_TYPE = {
    "string": 123,
    "integer": "x",
    "number": "x",
    "boolean": "x",
    "array": "x",
    "object": "x",
}


class CannotSample(Exception):
    """The document declares something a sample cannot be built for."""


@dataclass(frozen=True)
class RequestCase:
    method: str
    path: str
    body: Any
    invalid: list[tuple[str, Any]]


def request_cases(document: dict[str, Any]) -> list[RequestCase]:
    """One case per operation that declares a JSON request body."""
    cases: list[RequestCase] = []
    for path, item in document.get("paths", {}).items():
        for method in METHODS:
            operation = operation_of(item, method)
            if operation is None:
                continue
            schema = (
                operation.get("requestBody", {})
                .get("content", {})
                .get(JSON, {})
                .get("schema")
            )
            if isinstance(schema, dict):
                cases.append(
                    RequestCase(
                        method.upper(),
                        path,
                        sample_value(schema, document),
                        invalid_values(schema, document),
                    )
                )
    return cases


def resolve(schema: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    target: Any = document
    for part in ref.removeprefix("#/").split("/"):
        target = target.get(part) if isinstance(target, dict) else None
    if not isinstance(target, dict):
        raise CannotSample(f"unresolved reference {ref}")
    return target


def type_of(schema: dict[str, Any]) -> str | None:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind is None and "properties" in schema:
        return "object"
    return kind if isinstance(kind, str) else None


def sample_value(
    schema: dict[str, Any], document: dict[str, Any], depth: int = 0
) -> Any:
    """A value that satisfies ``schema`` (deterministic)."""
    schema = resolve(schema, document)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("oneOf", "anyOf", "allOf"):
        if key in schema:
            return sample_value(first_member(schema[key]), document, depth)
    builder = BUILDERS.get(type_of(schema) or "")
    if builder is None:
        raise CannotSample(f"no sample for schema {schema!r}")
    return builder(schema, document, depth)


def first_member(members: list[dict[str, Any]]) -> dict[str, Any]:
    return next((m for m in members if m.get("type") != "null"), members[0])


def sample_string(
    schema: dict[str, Any], _document: dict[str, Any], _depth: int
) -> str:
    fmt = schema.get("format")
    if fmt in FORMATS:
        return FORMATS[fmt]
    if "pattern" in schema:
        raise CannotSample(f"cannot build a string for pattern {schema['pattern']!r}")
    length = max(schema.get("minLength", 0), 1)
    return str("a" * min(length, schema.get("maxLength", length)))


def bound(schema: dict[str, Any], inclusive: str, exclusive: str) -> tuple[Any, bool]:
    if exclusive in schema:
        return schema[exclusive], True
    return schema.get(inclusive), False


def first_candidate(low: Any, low_open: bool, high: Any, whole: bool) -> float:
    if low is None:
        return 1
    if low_open and (whole or high is None):
        return float(low + 1)
    if low_open:
        return float((low + high) / 2)
    return float(low)


def fits(value: float, limit: Any, is_open: bool, upper: bool) -> bool:
    if limit is None:
        return True
    if value == limit:
        return not is_open
    return bool(value < limit if upper else value > limit)


def sample_number(
    schema: dict[str, Any], _document: dict[str, Any], _depth: int
) -> int | float:
    whole = type_of(schema) == "integer"
    low, low_open = bound(schema, "minimum", "exclusiveMinimum")
    high, high_open = bound(schema, "maximum", "exclusiveMaximum")
    if low is None and high is not None:
        value = float(high - 1 if high_open or whole else high)
    else:
        value = first_candidate(low, low_open, high, whole)
    value = apply_multiple(value, schema.get("multipleOf"))
    if not (fits(value, low, low_open, False) and fits(value, high, high_open, True)):
        raise CannotSample("numeric bounds leave no value to sample")
    return int(value) if whole else float(value)


def apply_multiple(value: float, step: float | None) -> float:
    return value if not step else math.ceil(value / step) * step


def sample_array(
    schema: dict[str, Any], document: dict[str, Any], depth: int
) -> list[Any]:
    if "prefixItems" in schema:
        return [sample_value(s, document, depth + 1) for s in schema["prefixItems"]]
    count = max(schema.get("minItems", 0), 1)
    count = min(count, schema.get("maxItems", count))
    if count > 1 and schema.get("uniqueItems"):
        raise CannotSample("several unique items cannot be sampled")
    items = schema.get("items")
    if count == 0 or not isinstance(items, dict):
        return []
    return [sample_value(items, document, depth + 1) for _ in range(count)]


def sample_object(
    schema: dict[str, Any], document: dict[str, Any], depth: int
) -> dict[str, Any]:
    required = set(schema.get("required", []))
    result: dict[str, Any] = {}
    for name, sub in schema.get("properties", {}).items():
        if depth >= MAX_DEPTH and name not in required:
            continue
        if depth > MAX_DEPTH * 2:
            raise CannotSample("recursive schema")
        result[name] = sample_value(sub, document, depth + 1)
    extra = schema.get("additionalProperties")
    if not result and isinstance(extra, dict) and schema.get("maxProperties") != 0:
        result["key"] = sample_value(extra, document, depth + 1)
    return result


Builder = Callable[[dict[str, Any], dict[str, Any], int], Any]
BUILDERS: dict[str, Builder] = {
    "string": sample_string,
    "integer": sample_number,
    "number": sample_number,
    "boolean": lambda *_: True,
    "null": lambda *_: None,
    "array": sample_array,
    "object": sample_object,
}


def invalid_values(
    schema: dict[str, Any], document: dict[str, Any]
) -> list[tuple[str, Any]]:
    """Bodies that each break exactly one rule, labelled by what they break."""
    base = sample_value(schema, document)
    return list(mutations(schema, base, document, ""))


def mutations(
    schema: dict[str, Any], value: Any, document: dict[str, Any], path: str
) -> Iterator[tuple[str, Any]]:
    schema = resolve(schema, document)
    name = path or "body"
    kind = type_of(schema)
    if (
        kind in WRONG_TYPE
        and "anyOf" not in schema
        and "oneOf" not in schema
        and schema.get("format") != "decimal"  # a Decimal also accepts a number
    ):
        yield f"{name} has the wrong type", WRONG_TYPE[kind]
    yield from constraint_mutations(schema, value, name)
    if kind == "object" and isinstance(value, dict):
        yield from object_mutations(schema, value, document, path)
    elif kind == "array" and isinstance(value, list) and value:
        items = schema.get("items")
        if isinstance(items, dict):
            for label, bad in mutations(items, value[0], document, f"{path}[]"):
                yield label, [bad, *value[1:]]


def object_mutations(
    schema: dict[str, Any], value: dict[str, Any], document: dict[str, Any], path: str
) -> Iterator[tuple[str, Any]]:
    prefix = f"{path}." if path else ""
    for name in schema.get("required", []):
        yield f"missing {prefix}{name}", {k: v for k, v in value.items() if k != name}
    for name, sub in schema.get("properties", {}).items():
        if name in value:
            for label, bad in mutations(sub, value[name], document, f"{prefix}{name}"):
                yield label, {**value, name: bad}
    if schema.get("additionalProperties") is False:
        where = f" in {path}" if path else ""
        yield f"unexpected property{where}", {**value, "unexpected_field": 1}


def constraint_mutations(
    schema: dict[str, Any], value: Any, name: str
) -> Iterator[tuple[str, Any]]:
    kind = type_of(schema)
    if kind == "string" and "format" not in schema:
        if schema.get("minLength", 0) >= 1:
            yield f"{name} is too short", "a" * (schema["minLength"] - 1)
        if "maxLength" in schema:
            yield f"{name} is too long", "a" * (schema["maxLength"] + 1)
    if kind in {"integer", "number"}:
        yield from number_mutations(schema, name)
    if kind == "string" and "enum" in schema:
        yield f"{name} is not an allowed value", "__not_allowed__"
    if kind == "array" and isinstance(value, list) and value:
        yield from array_mutations(schema, value, name)


def number_mutations(schema: dict[str, Any], name: str) -> Iterator[tuple[str, Any]]:
    low, low_open = bound(schema, "minimum", "exclusiveMinimum")
    high, high_open = bound(schema, "maximum", "exclusiveMaximum")
    if low is not None:
        yield f"{name} is below the minimum", low if low_open else low - 1
    if high is not None:
        yield f"{name} is above the maximum", high if high_open else high + 1


def array_mutations(
    schema: dict[str, Any], value: list[Any], name: str
) -> Iterator[tuple[str, Any]]:
    if schema.get("minItems", 0) >= 1:
        yield f"{name} has too few items", []
    if "maxItems" in schema and "prefixItems" not in schema:
        yield f"{name} has too many items", value * (schema["maxItems"] + 1)
