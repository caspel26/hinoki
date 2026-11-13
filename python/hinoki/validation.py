"""Strict validation of decoded JSON values against schemas."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from hinoki.errors import ErrorDetail
from hinoki.fieldtypes import FieldType

if TYPE_CHECKING:
    from hinoki.schema import FieldSpec

INVALID = object()
"""Returned by checks that recorded an error."""


def join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


class Validator:
    """Collects every problem found in one input instead of stopping at the first."""

    def __init__(self) -> None:
        self.errors: list[ErrorDetail] = []
        self.handlers: dict[str, Callable[[FieldType, Any, str], object]] = {
            "str": self.text,
            "int": self.integer,
            "float": self.number,
            "bool": self.boolean,
            "uuid": self.uuid,
            "datetime": self.moment,
            "list": self.sequence,
            "schema": self.schema,
        }

    def fail(self, path: str, code: str, message: str) -> object:
        self.errors.append(ErrorDetail(path, code, message))
        return INVALID

    def check(self, ftype: FieldType, data: Any, path: str) -> object:
        if data is None:
            if ftype.nullable:
                return None
            return self.fail(path, "null", "null is not allowed")
        return self.handlers[ftype.kind](ftype, data, path)

    def text(self, ftype: FieldType, data: Any, path: str) -> object:
        if not isinstance(data, str):
            return self.fail(path, "type", "expected a string")
        info = ftype.info
        if info.pattern is not None and not re.fullmatch(info.pattern, data):
            return self.fail(path, "pattern", f"must match {info.pattern!r}")
        return self.length(ftype, data, path)

    def length(self, ftype: FieldType, data: Any, path: str) -> object:
        info, size = ftype.info, len(data)
        if info.min_length is not None and size < info.min_length:
            return self.fail(path, "min_length", f"at least {info.min_length} long")
        if info.max_length is not None and size > info.max_length:
            return self.fail(path, "max_length", f"at most {info.max_length} long")
        return data

    def bounds(self, ftype: FieldType, data: Any, path: str) -> object:
        info = ftype.info
        checks = (
            (info.ge, data.__ge__, "ge", "greater than or equal to"),
            (info.le, data.__le__, "le", "less than or equal to"),
            (info.gt, data.__gt__, "gt", "greater than"),
            (info.lt, data.__lt__, "lt", "less than"),
        )
        for limit, compare, code, words in checks:
            if limit is not None and not compare(limit):
                return self.fail(path, code, f"must be {words} {limit}")
        return data

    def integer(self, ftype: FieldType, data: Any, path: str) -> object:
        if type(data) is not int:
            return self.fail(path, "type", "expected an integer")
        return self.bounds(ftype, data, path)

    def number(self, ftype: FieldType, data: Any, path: str) -> object:
        if type(data) not in {int, float}:
            return self.fail(path, "type", "expected a number")
        return self.bounds(ftype, float(data), path)

    def boolean(self, _ftype: FieldType, data: Any, path: str) -> object:
        if type(data) is not bool:
            return self.fail(path, "type", "expected a boolean")
        return data

    def uuid(self, _ftype: FieldType, data: Any, path: str) -> object:
        try:
            return UUID(data) if isinstance(data, str) else INVALID
        except ValueError:
            pass
        return self.fail(path, "uuid", "expected a UUID string")

    def moment(self, _ftype: FieldType, data: Any, path: str) -> object:
        try:
            parsed = datetime.fromisoformat(data) if isinstance(data, str) else None
        except ValueError:
            parsed = None
        if parsed is None:
            return self.fail(path, "datetime", "expected an RFC 3339 datetime string")
        if parsed.tzinfo is None:
            return self.fail(path, "datetime_tz", "datetime needs a UTC offset")
        return parsed

    def sequence(self, ftype: FieldType, data: Any, path: str) -> object:
        if not isinstance(data, list):
            return self.fail(path, "type", "expected an array")
        if self.length(ftype, data, path) is INVALID:
            return INVALID
        assert ftype.item is not None  # noqa: S101 - set for every list type
        before = len(self.errors)
        items = [self.check(ftype.item, v, f"{path}[{i}]") for i, v in enumerate(data)]
        return INVALID if len(self.errors) > before else items

    def schema(self, ftype: FieldType, data: Any, path: str) -> object:
        schema = ftype.schema
        assert schema is not None  # noqa: S101 - set for every schema type
        if not isinstance(data, dict):
            return self.fail(path, "type", "expected an object")
        before = len(self.errors)
        specs: dict[str, FieldSpec] = schema.specs()
        for key in data:
            if key not in specs:
                self.fail(join(path, str(key)), "extra_forbidden", "unknown field")
        values = {
            name: self.member(spec, data, join(path, name))
            for name, spec in specs.items()
            if name in data or spec.required
        }
        if len(self.errors) > before:
            return INVALID
        return schema(**values)

    def member(self, spec: FieldSpec, data: dict[str, Any], path: str) -> object:
        if spec.name not in data:
            return self.fail(path, "required", "field is required")
        return self.check(spec.ftype, data[spec.name], path)
