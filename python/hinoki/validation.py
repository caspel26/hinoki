"""Strict validation of decoded JSON values against schemas."""

from __future__ import annotations

import math
import operator
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

MAX_ERRORS = 50
MAX_KEY_ECHO = 64
RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})?"
)
BOUNDS = (
    ("ge", "greater than or equal to", operator.ge),
    ("le", "less than or equal to", operator.le),
    ("gt", "greater than", operator.gt),
    ("lt", "less than", operator.lt),
)


class TooManyErrors(Exception):
    """Validation stopped early: more than ``MAX_ERRORS`` problems."""


def join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


class Validator:
    """Collects problems found in one input, up to ``MAX_ERRORS``.

    Past the cap it raises ``TooManyErrors`` so a hostile payload cannot make
    the server build an unbounded list of details.
    """

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
        if len(self.errors) >= MAX_ERRORS:
            raise TooManyErrors
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
        if self.length(ftype, data, path) is INVALID:
            return INVALID
        pattern = ftype.info.pattern
        # Length is checked first so it bounds the cost of the regex.
        if pattern is not None and not re.fullmatch(pattern, data):
            return self.fail(path, "pattern", f"must match {pattern!r}")
        return data

    def length(self, ftype: FieldType, data: Any, path: str) -> object:
        info, size = ftype.info, len(data)
        if info.min_length is not None and size < info.min_length:
            return self.fail(path, "min_length", f"at least {info.min_length} long")
        if info.max_length is not None and size > info.max_length:
            return self.fail(path, "max_length", f"at most {info.max_length} long")
        return data

    def bounds(self, ftype: FieldType, data: Any, path: str) -> object:
        for name, words, compare in BOUNDS:
            limit = getattr(ftype.info, name)
            if limit is not None and not compare(data, limit):
                return self.fail(path, name, f"must be {words} {limit}")
        return data

    def integer(self, ftype: FieldType, data: Any, path: str) -> object:
        if type(data) is not int:
            return self.fail(path, "type", "expected an integer")
        return self.bounds(ftype, data, path)

    def number(self, ftype: FieldType, data: Any, path: str) -> object:
        if type(data) not in {int, float}:
            return self.fail(path, "type", "expected a number")
        try:
            value = float(data)
        except OverflowError:
            return self.fail(path, "finite", "number is out of range")
        if not math.isfinite(value):
            return self.fail(path, "finite", "number must be finite")
        return self.bounds(ftype, value, path)

    def boolean(self, _ftype: FieldType, data: Any, path: str) -> object:
        if type(data) is not bool:
            return self.fail(path, "type", "expected a boolean")
        return data

    def uuid(self, _ftype: FieldType, data: Any, path: str) -> object:
        try:
            value = UUID(data) if isinstance(data, str) else None
        except ValueError:
            value = None
        if value is None or str(value) != data.lower():
            return self.fail(path, "uuid", "expected a canonical UUID string")
        return value

    def moment(self, _ftype: FieldType, data: Any, path: str) -> object:
        if not isinstance(data, str) or not RFC3339.fullmatch(data):
            return self.fail(path, "datetime", "expected an RFC 3339 datetime string")
        try:
            parsed = datetime.fromisoformat(data)
        except ValueError:
            return self.fail(path, "datetime", "expected an RFC 3339 datetime string")
        if parsed.tzinfo is None:
            return self.fail(path, "datetime_tz", "datetime needs a UTC offset")
        return parsed

    def sequence(self, ftype: FieldType, data: Any, path: str) -> object:
        if not isinstance(data, list):
            return self.fail(path, "type", "expected an array")
        if self.length(ftype, data, path) is INVALID:
            return INVALID
        if ftype.item is None:
            raise TypeError("list type without an item type")
        before = len(self.errors)
        items = [self.check(ftype.item, v, f"{path}[{i}]") for i, v in enumerate(data)]
        return INVALID if len(self.errors) > before else items

    def schema(self, ftype: FieldType, data: Any, path: str) -> object:
        if ftype.schema is None:
            raise TypeError("schema type without a schema")
        if not isinstance(data, dict):
            return self.fail(path, "type", "expected an object")
        before = len(self.errors)
        specs: dict[str, FieldSpec] = ftype.schema.specs()
        for key in data:
            if key not in specs:
                shown = str(key)[:MAX_KEY_ECHO]
                self.fail(join(path, shown), "extra_forbidden", "unknown field")
        values = {
            name: self.member(spec, data, join(path, name))
            for name, spec in specs.items()
            if name in data or spec.required
        }
        if len(self.errors) > before:
            return INVALID
        return ftype.schema(**values)

    def member(self, spec: FieldSpec, data: dict[str, Any], path: str) -> object:
        if spec.name not in data:
            return self.fail(path, "required", "field is required")
        return self.check(spec.ftype, data[spec.name], path)
