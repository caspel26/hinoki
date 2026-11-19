"""Strict validation of decoded JSON values against schemas."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as dt
import ipaddress
import math
import operator
import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from hinoki.errors import ErrorDetail
from hinoki.fields import SecretStr
from hinoki.fieldtypes import FieldType, decimal_limits
from hinoki.schemaconfig import SchemaExtra

if TYPE_CHECKING:
    from hinoki.schema import FieldSpec

INVALID = object()
"""Returned by checks that recorded an error."""

MAX_ERRORS = 50
NETWORK_KINDS: dict[str, tuple[Callable[[str], object], str]] = {
    "ipv4": (ipaddress.IPv4Address, "an IPv4 address"),
    "ipv6": (ipaddress.IPv6Address, "an IPv6 address"),
    "ipv4network": (ipaddress.IPv4Network, "an IPv4 network with no host bits"),
    "ipv6network": (ipaddress.IPv6Network, "an IPv6 network with no host bits"),
}
MAX_KEY_ECHO = 64
RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})?"
)
# ``[0-9]`` and ``re.ASCII``: ``\\d`` would also match other Unicode digits.
DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", re.ASCII)
TIME = re.compile(
    r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?"
    r"(Z|([+-])([0-9]{2}):([0-9]{2}))?",
    re.ASCII,
)
DURATION = re.compile(
    r"(-?)P(?:([0-9]+)D)?(T(?:([0-9]+)H)?(?:([0-9]+)M)?"
    r"(?:([0-9]+)(?:\.([0-9]{1,6}))?S)?)?",
    re.ASCII,
)
DECIMAL = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", re.ASCII)
BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}", re.ASCII)
MAX_CLOCK = (23, 59, 59)
MAX_OFFSET = (23, 59)
MICROS = 6
BOUNDS = (
    ("ge", "greater than or equal to", operator.ge),
    ("le", "less than or equal to", operator.le),
    ("gt", "greater than", operator.gt),
    ("lt", "less than", operator.lt),
)


def wire_bounded(ftype: FieldType) -> FieldType:
    """``ftype`` with its wire-input default bound as its ``max_length``."""
    if ftype.info.max_length is not None or ftype.input_max_length is None:
        return ftype
    info = dataclasses.replace(ftype.info, max_length=ftype.input_max_length)
    return dataclasses.replace(ftype, info=info)


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
            "date": self.day,
            "time": self.clock,
            "timedelta": self.duration,
            "decimal": self.decimal,
            "bytes": self.binary,
            "ipv4": self.internet,
            "ipv6": self.internet,
            "ipv4network": self.internet,
            "ipv6network": self.internet,
            "path": self.filesystem_path,
            "secret": self.secret,
            "list": self.sequence,
            "tuple": self.fixed,
            "anyof": self.anyof,
            "any": lambda _ftype, data, _path: data,
            "dict": self.mapping,
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
        value = self.handlers[ftype.kind](ftype, data, path)
        if value is INVALID:
            return INVALID
        return self.settle(ftype, value, path)

    def settle(self, ftype: FieldType, value: Any, path: str) -> object:
        choices = ftype.info.one_of
        if choices is not None and value not in choices:
            shown = ", ".join(repr(c) for c in choices[:10])
            return self.fail(path, "one_of", f"must be one of {shown}")
        if ftype.enum_class is not None:
            return ftype.enum_class(value)
        return value

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
        unit = "character" if isinstance(data, str) else "item"
        if info.min_length is not None and size < info.min_length:
            bound = info.min_length
            plural = "" if bound == 1 else "s"
            return self.fail(path, "min_length", f"at least {bound} {unit}{plural}")
        if info.max_length is not None and size > info.max_length:
            bound = info.max_length
            plural = "" if bound == 1 else "s"
            return self.fail(path, "max_length", f"at most {bound} {unit}{plural}")
        return data

    def bounds(self, ftype: FieldType, data: Any, path: str) -> object:
        for name, words, compare in BOUNDS:
            limit = getattr(ftype.info, name)
            if limit is not None and not compare(data, limit):
                return self.fail(path, name, f"must be {words} {limit}")
        step = ftype.info.multiple_of
        if step is not None and Fraction(data) % Fraction(step) != 0:
            return self.fail(path, "multiple_of", f"must be a multiple of {step}")
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

    def day(self, _ftype: FieldType, data: Any, path: str) -> object:
        message = "expected an ISO 8601 date string"
        found = DATE.fullmatch(data) if isinstance(data, str) else None
        if found is None:
            return self.fail(path, "date", message)
        try:
            return dt.date(*(int(part) for part in found.groups()))
        except ValueError:
            return self.fail(path, "date", message)

    def clock(self, _ftype: FieldType, data: Any, path: str) -> object:
        found = TIME.fullmatch(data) if isinstance(data, str) else None
        if found is None:
            return self.fail(path, "time", "expected an ISO 8601 time string")
        hour, minute, second, fraction, zone, sign, zone_h, zone_m = found.groups()
        clock = (int(hour), int(minute), int(second))
        offset = (int(zone_h), int(zone_m)) if sign else (0, 0)
        if any(v > top for v, top in zip(clock, MAX_CLOCK, strict=True)) or any(
            v > top for v, top in zip(offset, MAX_OFFSET, strict=True)
        ):
            return self.fail(path, "time", "expected an ISO 8601 time string")
        if zone is None:
            return self.fail(path, "time_tz", "time needs a UTC offset")
        minutes = offset[0] * 60 + offset[1]
        zone_info = dt.timezone(
            dt.timedelta(minutes=-minutes if sign == "-" else minutes)
        )
        micro = int(fraction.ljust(MICROS, "0")) if fraction else 0
        return dt.time(*clock, micro, tzinfo=zone_info)

    def duration(self, _ftype: FieldType, data: Any, path: str) -> object:
        found = DURATION.fullmatch(data) if isinstance(data, str) else None
        if found is None:
            return self.fail(path, "duration", "expected an ISO 8601 duration string")
        sign, days, clock, hours, minutes, seconds, fraction = found.groups()
        if (days is None and clock is None) or clock == "T":
            return self.fail(path, "duration", "expected an ISO 8601 duration string")
        parts = [int(part) if part else 0 for part in (days, hours, minutes, seconds)]
        total = ((parts[0] * 24 + parts[1]) * 60 + parts[2]) * 60 + parts[3]
        micros = total * 10**MICROS + (
            int(fraction.ljust(MICROS, "0")) if fraction else 0
        )
        try:
            return dt.timedelta(microseconds=-micros if sign else micros)
        except OverflowError:
            return self.fail(path, "duration_range", "duration is out of range")

    def decimal(self, ftype: FieldType, data: Any, path: str) -> object:
        text = str(data) if type(data) is int else data
        if not isinstance(text, str) or DECIMAL.fullmatch(text) is None:
            return self.fail(path, "decimal", "expected a decimal string or an integer")
        value = Decimal(text)
        _, digits_tuple, exponent = value.as_tuple()
        places = -int(exponent)
        digits = max(len(digits_tuple), places)
        most, decimal_places, whole = decimal_limits(ftype.info)
        for count, limit, code, unit in (
            (digits, most, "max_digits", "digit"),
            (places, decimal_places, "decimal_places", "decimal place"),
            (digits - places, whole, "max_whole_digits", "digit"),
        ):
            if limit is not None and count > limit:
                plural = "" if limit == 1 else "s"
                after = (
                    " before the decimal point" if code == "max_whole_digits" else ""
                )
                return self.fail(path, code, f"at most {limit} {unit}{plural}{after}")
        return self.bounds(ftype, value, path)

    def internet(self, ftype: FieldType, data: Any, path: str) -> object:
        """An address or network as the standard library parses it, minus the
        spellings Hinoki does not take: zone ids, netmask forms, anything but
        a decimal prefix length."""
        parse, label = NETWORK_KINDS[ftype.kind]
        text = data if isinstance(data, str) else ""
        _, slash, prefix = text.partition("/")
        form_ok = "%" not in text and (
            not slash or (prefix.isascii() and prefix.isdigit())
        )
        if not isinstance(data, str) or not form_ok:
            return self.fail(path, ftype.kind, f"expected {label}")
        try:
            return parse(text)
        except ValueError:
            return self.fail(path, ftype.kind, f"expected {label}")

    def filesystem_path(self, ftype: FieldType, data: Any, path: str) -> object:
        """A non-empty string without NUL, as a ``Path``."""
        if not isinstance(data, str) or not data or "\0" in data:
            return self.fail(path, "path", "expected a file system path")
        if self.length(ftype, data, path) is INVALID:
            return INVALID
        return Path(data)

    def secret(self, ftype: FieldType, data: Any, path: str) -> object:
        text = self.text(ftype, data, path)
        return INVALID if text is INVALID else SecretStr(str(text))

    def binary(self, ftype: FieldType, data: Any, path: str) -> object:
        found = BASE64.fullmatch(data) if isinstance(data, str) else None
        raw = None
        if found is not None and len(data) % 4 == 0:
            try:
                raw = base64.b64decode(data, validate=True)
            except binascii.Error:
                raw = None
        if raw is None or base64.b64encode(raw).decode("ascii") != data:
            return self.fail(path, "bytes", "expected a base64 string")
        info, size = wire_bounded(ftype).info, len(raw)
        if info.min_length is not None and size < info.min_length:
            plural = "" if info.min_length == 1 else "s"
            return self.fail(
                path, "min_length", f"at least {info.min_length} byte{plural}"
            )
        if info.max_length is not None and size > info.max_length:
            plural = "" if info.max_length == 1 else "s"
            return self.fail(
                path, "max_length", f"at most {info.max_length} byte{plural}"
            )
        return raw

    def sequence(self, ftype: FieldType, data: Any, path: str) -> object:
        if not isinstance(data, list):
            return self.fail(path, "type", "expected an array")
        if self.length(ftype, data, path) is INVALID:
            return INVALID
        if ftype.item is None:
            raise TypeError("list type without an item type")
        before = len(self.errors)
        items = [self.check(ftype.item, v, f"{path}[{i}]") for i, v in enumerate(data)]
        if len(self.errors) > before:
            return INVALID
        collected = {"set": set, "frozenset": frozenset, "tuple": tuple}
        return collected.get(ftype.collection or "", list)(items)

    def anyof(self, ftype: FieldType, data: Any, path: str) -> object:
        before = len(self.errors)
        for variant in ftype.items:
            try:
                value = self.check(variant, data, path)
            except TooManyErrors:
                value = INVALID
            del self.errors[before:]
            if value is not INVALID:
                return value
        return self.fail(path, "union", "does not match any allowed type")

    def fixed(self, ftype: FieldType, data: Any, path: str) -> object:
        if not isinstance(data, list):
            return self.fail(path, "type", "expected an array")
        if self.length(ftype, data, path) is INVALID:
            return INVALID
        before = len(self.errors)
        items = [
            self.check(item, v, f"{path}[{i}]")
            for i, (item, v) in enumerate(zip(ftype.items, data, strict=True))
        ]
        return INVALID if len(self.errors) > before else tuple(items)

    def mapping(self, ftype: FieldType, data: Any, path: str) -> object:
        if not isinstance(data, dict):
            return self.fail(path, "type", "expected an object")
        if ftype.item is None or ftype.key is None:
            raise TypeError("dict type without a key or value type")
        before = len(self.errors)
        for key in data:
            if not isinstance(key, str):
                self.fail(path, "type", "object keys must be strings")
        if len(self.errors) > before:
            return INVALID
        if self.length(wire_bounded(ftype), data, path) is INVALID:
            return INVALID
        values = {}
        for key, item in data.items():
            where = join(path, key[:MAX_KEY_ECHO])
            if self.key(wire_bounded(ftype.key), key, where) is not INVALID:
                values[key] = self.check(ftype.item, item, where)
        return INVALID if len(self.errors) > before else values

    def key(self, ftype: FieldType, key: str, path: str) -> object:
        info = ftype.info
        if info.min_length is not None and len(key) < info.min_length:
            bound = info.min_length
            plural = "" if bound == 1 else "s"
            return self.fail(
                path,
                "key_min_length",
                f"key must have at least {bound} character{plural}",
            )
        if info.max_length is not None and len(key) > info.max_length:
            bound = info.max_length
            plural = "" if bound == 1 else "s"
            return self.fail(
                path,
                "key_max_length",
                f"key must have at most {bound} character{plural}",
            )
        if info.pattern is not None and not re.fullmatch(info.pattern, key):
            return self.fail(path, "key_pattern", f"key must match {info.pattern!r}")
        return key

    def schema(self, ftype: FieldType, data: Any, path: str) -> object:
        if ftype.schema is None:
            raise TypeError("schema type without a schema")
        if not isinstance(data, dict):
            return self.fail(path, "type", "expected an object")
        before = len(self.errors)
        specs: dict[str, FieldSpec] = ftype.schema.specs()
        for key in data:
            if not isinstance(key, str):
                self.fail(path, "type", "object keys must be strings")
        if len(self.errors) > before:
            return INVALID
        for key in data:
            if (
                isinstance(key, str)
                and key not in specs
                and ftype.schema.__schema_config__.extra is SchemaExtra.FORBID
            ):
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
