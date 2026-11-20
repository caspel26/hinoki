"""Typed, immutable settings with environment loading (no Pydantic).

    class BlogSettings(AppSettings):
        jwt_secret: Secret
        database: DatabaseSettings

    settings = BlogSettings.load(prefix="BLOG_", dotenv=".env")
    app = Hinoki(settings=settings)

See ``docs/SETTINGS-DESIGN.md``. Errors name keys and reasons, never values.
"""

from __future__ import annotations

import dataclasses
import functools
import hmac
import ipaddress
import json
import logging
import logging.config
import math
import os
import re
import sys
import tomllib
import types
import typing
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal, Self, dataclass_transform
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hinoki.context import request_id
from hinoki.runtime.config import MAX_REQUEST_INPUT_BYTES, RuntimeConfig
from hinoki.target import load_target

TRUE = frozenset({"1", "true", "yes", "on"})
FALSE = frozenset({"0", "false", "no", "off"})
LEVELS = ("debug", "info", "warning", "error", "critical")
SEPARATOR = "__"
MAX_BODY_LIMIT = 1 << 30  # 1 GiB: a larger "limit" is no limit
MAX_SYNC_WORKERS = 1024
MAX_PORT = 65536
MAX_WORKERS = 256
MAX_CONNECTIONS = 1_000_000
MAX_READ_TIMEOUT = 3600
RUST_ONLY = (
    "max_connections",
    "header_read_timeout",
    "body_read_timeout",
    "write_timeout",
    "native_dispatch",
)
# What Granian cannot honour: the limits above and how Kiso formats its log.
KISO_ONLY = (
    *RUST_ONLY,
    "access_log_format",
    "access_log_skip",
    "access_log_slow_ms",
    "recent_requests",
    "server_timing",
)
QUOTED = 2  # shortest quoted value: the two quotes
MAX_CORS_AGE = 86400
HSTS_MAX_SECONDS = 63_072_000
HSTS_PRELOAD_MIN_SECONDS = 31_536_000
ANY = "*"
HOST_PATTERN = re.compile(
    r"\.?(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*",
    re.ASCII | re.IGNORECASE,
)
ORIGIN = re.compile(
    r"[a-z][a-z0-9+.-]*://(?:[a-z0-9.-]+|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?",
    re.ASCII | re.IGNORECASE,
)
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9a-z-]+", re.ASCII | re.IGNORECASE)
REQUEST_ID_HEADER_CONFLICTS = frozenset(
    {
        "allow",
        "cache-control",
        "connection",
        "content-encoding",
        "content-length",
        "content-range",
        "content-security-policy",
        "content-type",
        "date",
        "host",
        "location",
        "referrer-policy",
        "retry-after",
        "server",
        "set-cookie",
        "strict-transport-security",
        "transfer-encoding",
        "vary",
        "www-authenticate",
        "x-content-type-options",
        "x-frame-options",
        "x-request-id",
    }
)


class SettingsError(ValueError):
    """The configuration is invalid; ``problems`` lists every one (no values)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("invalid settings:\n" + "\n".join(f"  {p}" for p in problems))
        self.problems = problems


class Secret:
    """A string that never prints itself: ``repr`` and ``str`` show ``***``."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and hmac.compare_digest(
            self._value.encode(), other._value.encode()
        )

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass_transform(frozen_default=True, kw_only_default=True)
@dataclasses.dataclass(frozen=True, kw_only=True)
class Settings:
    """Base class: subclasses are keyword-only frozen dataclasses."""

    __env_prefix__: ClassVar[str] = ""
    __dotenv__: ClassVar[str | None] = None
    __toml__: ClassVar[str | None] = None

    def __init_subclass__(
        cls,
        prefix: str | None = None,
        dotenv: str | None = None,
        toml: str | None = None,
        **options: Any,
    ) -> None:
        """``class BlogSettings(AppSettings, prefix="BLOG_", dotenv=".env")``
        declares where the class reads its variables, so ``BlogSettings.load()``,
        ``hinoki settings`` and ``serve`` need no repeated arguments.
        A subclass inherits what its parent declared."""
        super().__init_subclass__(**options)
        if prefix is not None:
            cls.__env_prefix__ = prefix
        if dotenv is not None:
            cls.__dotenv__ = dotenv
        if toml is not None:
            cls.__toml__ = toml
        dataclasses.dataclass(frozen=True, kw_only=True)(cls)

    @classmethod
    def fields_of(cls) -> dict[str, Any]:
        """Field name to resolved type."""
        return resolved_hints(typing.cast(Any, cls))

    @classmethod
    def load(
        cls,
        *,
        prefix: str | None = None,
        env: Mapping[str, str] | None = None,
        dotenv: str | os.PathLike[str] | None = None,
        toml: str | os.PathLike[str] | None = None,
        overrides: Mapping[str, Any] | None = None,
        strict: bool = True,
    ) -> Self:
        """Build the settings. Precedence: ``overrides``, ``env`` (default: the
        process environment), dotenv, TOML file, and field defaults.

        ``strict`` (default) makes a variable that starts with ``prefix`` but
        names no field an error, which catches typos; pass ``strict=False`` when
        another program shares the prefix. With an empty prefix nothing can be
        told apart from the rest of the environment, so no typo check happens.

        ``prefix``, ``dotenv`` and ``toml`` default to what the class declared."""
        prefix = cls.__env_prefix__ if prefix is None else prefix
        dotenv = cls.__dotenv__ if dotenv is None else dotenv
        toml = cls.__toml__ if toml is None else toml
        source, _, problems = gather(cls, prefix, env, dotenv, toml)
        found = build(cls, prefix, source, dict(overrides or {}), problems)
        known = known_keys(cls, prefix)
        problems += [
            f"{key}: unknown setting"
            for key in sorted(source)
            if strict and prefix and key.startswith(prefix) and key not in known
        ]
        if problems or found is None:
            raise SettingsError(problems or ["could not build the settings"])
        return typing.cast(Self, found)

    @classmethod
    def explain(
        cls,
        *,
        prefix: str | None = None,
        env: Mapping[str, str] | None = None,
        dotenv: str | os.PathLike[str] | None = None,
        toml: str | os.PathLike[str] | None = None,
        overrides: Mapping[str, Any] | None = None,
        strict: bool = True,
    ) -> list[SettingRow]:
        """Load like ``load`` and say, for every value, where it came from:
        ``override``, ``environment``, ``.env``, ``toml``, ``file`` (a ``*_FILE``
        variable) or ``default``. Secrets are redacted."""
        prefix = cls.__env_prefix__ if prefix is None else prefix
        dotenv = cls.__dotenv__ if dotenv is None else dotenv
        toml = cls.__toml__ if toml is None else toml
        found = cls.load(
            prefix=prefix,
            env=env,
            dotenv=dotenv,
            toml=toml,
            overrides=overrides,
            strict=strict,
        )
        _, origins, _ = gather(cls, prefix, env, dotenv, toml)
        return rows_of(found, prefix, origins, dict(overrides or {}))

    @classmethod
    def example(cls, prefix: str | None = None) -> str:
        """A ``.env.example``: every variable with its default, required ones and
        secrets left blank, and a comment with the type."""
        return example_of(cls, cls.__env_prefix__ if prefix is None else prefix)

    def describe(self) -> dict[str, Any]:
        """The effective values as plain data, secrets redacted."""
        return typing.cast(dict[str, Any], describe(self))

    def __post_init__(self) -> None:
        for name, hint in self.fields_of().items():
            value = getattr(self, name, None)
            if hint is Secret and isinstance(value, str):  # built directly in code
                object.__setattr__(self, name, Secret(value))
        self.validate()

    def validate(self) -> None:
        """Override to check relations between fields; raise ``ValueError``.
        The message is shown in ``SettingsError``: never put a field's value in it."""


@functools.cache
def resolved_hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


@dataclasses.dataclass(frozen=True, slots=True)
class SettingRow:
    """One effective value: the variable name, the value as shown (secrets are
    ``***``), and where it came from."""

    key: str
    value: str
    origin: str


FILE_SUFFIX = "_FILE"
MAX_SECRET_FILE = 64 * 1024
MIN_SECRET = 32  # bytes: the shortest server secret accepted
MAX_SESSION_HOURS = 24 * 30
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def gather(
    cls: type[Settings],
    prefix: str,
    env: Mapping[str, str] | None,
    dotenv: str | os.PathLike[str] | None,
    toml: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """The merged variables, where each came from, and any problems reading
    ``*_FILE`` secrets. The environment beats the dotenv file."""
    source: dict[str, Any] = {}
    origins: dict[str, str] = {}
    problems: list[str] = []
    if toml is not None:
        toml_source = read_toml_source(cls, prefix, toml, problems)
        source.update(toml_source)
        origins.update(dict.fromkeys(toml_source, "toml"))
    if dotenv is not None and Path(dotenv).is_file():
        for key, value in parse_dotenv(
            Path(dotenv).read_text(encoding="utf-8")
        ).items():
            source[key], origins[key] = value, ".env"
    for key, value in (os.environ if env is None else env).items():
        source[key], origins[key] = value, "environment"
    known = known_keys(cls, prefix)
    for key in sorted(k for k in source if k.endswith(FILE_SUFFIX)):
        target = key.removesuffix(FILE_SUFFIX)
        if target not in known:
            continue  # left for the unknown-setting check
        path = source.pop(key)
        origins.pop(key, None)
        if target in source:
            problems.append(f"{target}: set both directly and through {key}")
            continue
        text = read_secret_file(key, path, problems)
        if text is not None:
            source[target], origins[target] = text, "file"
    return source, origins, problems


def read_toml_source(
    cls: type[Settings],
    prefix: str,
    path: str | os.PathLike[str],
    problems: list[str],
) -> dict[str, Any]:
    """Read and flatten a TOML file without exposing parser details or values."""
    toml_path = Path(path)
    try:
        document = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        problems.append(f"{toml_path}: TOML file cannot be read or parsed")
        return {}
    return flatten_toml(cls, prefix, document, problems)


def flatten_toml(
    cls: type[Settings],
    prefix: str,
    table: Mapping[str, Any],
    problems: list[str],
    path: str = "",
) -> dict[str, Any]:
    """Flatten TOML tables to the keys used by environment variables."""
    flattened: dict[str, Any] = {}
    hints = cls.fields_of()
    for name, value in table.items():
        key = f"{path}{name.upper()}"
        field_name = next(
            (field for field in hints if field.casefold() == name.casefold()), None
        )
        hint = hints.get(field_name) if field_name is not None else None
        flattened.update(_flatten_toml_value(prefix, key, hint, value, problems))
    return flattened


def _flatten_toml_value(  # noqa: PLR0911 - direct result per TOML value kind
    prefix: str, key: str, hint: Any, value: Any, problems: list[str]
) -> dict[str, Any]:
    full = prefix + key
    if is_secret_hint(hint):
        problems.append(f"{full}: secrets must come from environment or *_FILE")
        return {}
    if isinstance(hint, type) and issubclass(hint, Settings):
        if isinstance(value, Mapping):
            return flatten_toml(hint, prefix, value, problems, key + SEPARATOR)
        problems.append(f"{full}: a settings group must be a TOML table")
        return {}
    if isinstance(value, dict):
        if hint is not None:
            problems.append(f"{full}: TOML value has an incompatible type")
            return {}
        return {full: value}  # strict unknown-key checking reports its path
    if hint is not None and not toml_value_matches(hint, value):
        problems.append(f"{full}: TOML value has an incompatible type")
        return {}
    return {full: value}


def is_secret_hint(hint: Any) -> bool:
    if hint is Secret:
        return True
    origin = typing.get_origin(hint)
    return origin in (typing.Union, types.UnionType) and Secret in typing.get_args(hint)


def toml_value_matches(hint: Any, value: Any) -> bool:
    """Check TOML's native type before applying the shared settings converter."""
    origin = typing.get_origin(hint)
    arguments = typing.get_args(hint)
    if origin in (typing.Union, types.UnionType):
        return _matches_union(arguments, value)
    if origin is Literal:
        return _matches_literal(arguments, value)
    if origin in (list, tuple):
        return _matches_sequence(arguments, value)
    return _matches_scalar(hint, value)


def _matches_union(arguments: tuple[Any, ...], value: Any) -> bool:
    return any(
        value is None if option is type(None) else toml_value_matches(option, value)
        for option in arguments
    )


def _matches_literal(arguments: tuple[Any, ...], value: Any) -> bool:
    return any(type(value) is type(choice) and value == choice for choice in arguments)


def _matches_sequence(arguments: tuple[Any, ...], value: Any) -> bool:
    return isinstance(value, list) and all(
        toml_value_matches(arguments[0], item) for item in value
    )


def _matches_scalar(hint: Any, value: Any) -> bool:  # noqa: PLR0911 - type checks
    if hint is bool:
        return isinstance(value, bool)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if hint is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if isinstance(hint, type) and issubclass(hint, StrEnum):
        return isinstance(value, str)
    if hint is str:
        return isinstance(value, str)
    return False


def read_secret_file(key: str, path: str, problems: list[str]) -> str | None:
    """The content of a secret file without its trailing newline. Only a regular
    file up to 64 KiB; the problem names the variable, never the content."""
    target = Path(path)
    try:
        if not target.is_file() or target.stat().st_size > MAX_SECRET_FILE:
            problems.append(f"{key}: must name a regular file of at most 64 KiB")
            return None
        return target.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeDecodeError):
        problems.append(f"{key}: the file cannot be read as text")
        return None


def rows_of(
    value: Settings,
    prefix: str,
    origins: Mapping[str, str],
    overrides: dict[str, Any],
    path: str = "",
) -> list[SettingRow]:
    rows: list[SettingRow] = []
    for item in dataclasses.fields(value):
        if item.name.startswith("_"):
            continue
        key = f"{path}{item.name.upper()}"
        current = getattr(value, item.name)
        if isinstance(current, Settings):
            nested = overrides.get(item.name)
            rows += rows_of(
                current,
                prefix,
                origins,
                dict(nested) if isinstance(nested, Mapping) else {},
                key + SEPARATOR,
            )
            continue
        if item.name in overrides:
            origin = "override"
        else:
            origin = origins.get(prefix + key, "default")
        rows.append(SettingRow(prefix + key, str(describe(current)), origin))
    return rows


TYPE_NAMES = {str: "text", int: "integer", float: "number", bool: "true/false"}


def type_name(hint: Any) -> str:  # noqa: PLR0911 - one answer per kind of hint
    if hint is Secret:
        return "secret"
    origin = typing.get_origin(hint)
    if origin is Literal:
        return "one of " + "|".join(typing.get_args(hint))
    if origin in (typing.Union, types.UnionType):
        inner = [a for a in typing.get_args(hint) if a is not type(None)]
        return f"{type_name(inner[0])}, optional"
    if origin in (list, tuple):
        return "comma-separated " + type_name(typing.get_args(hint)[0])
    return TYPE_NAMES.get(hint, getattr(hint, "__name__", str(hint)))


def example_of(cls: type[Settings], prefix: str, path: str = "") -> str:
    lines: list[str] = []
    for name, hint in cls.fields_of().items():
        if name.startswith("_") or typing.get_origin(hint) is typing.ClassVar:
            continue
        key = f"{path}{name.upper()}"
        if isinstance(hint, type) and issubclass(hint, Settings):
            lines.append(example_of(hint, prefix, key + SEPARATOR))
            continue
        lines.append(example_line(cls, name, hint, prefix + key))
    return "\n".join(lines)


def example_line(cls: type[Settings], name: str, hint: Any, variable: str) -> str:
    """One field of a ``.env.example``: a type comment, then the variable."""
    field = {f.name: f for f in dataclasses.fields(cls)}[name]
    required = has_default(cls, name) is False
    default = "" if required or hint is Secret else describe_default(field)
    note = type_name(hint) + (", required" if required else "")
    # A blank line would set the empty value; leave an optional one commented
    # so a copied example keeps the default.
    comment = "# " if default == "" and not required else ""
    return f"# {note}\n{comment}{variable}={default}"


def describe_default(field: dataclasses.Field[Any]) -> str:  # noqa: PLR0911
    if field.default is not dataclasses.MISSING:
        value = field.default
    elif field.default_factory is not dataclasses.MISSING:
        value = field.default_factory()
        if isinstance(value, Settings):
            return ""
    else:
        return ""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ",".join(str(v) for v in value)
    return str(value)


def describe(value: Any) -> Any:
    if isinstance(value, Secret):
        return "***"
    if isinstance(value, Settings):
        return {
            f.name: describe(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, list | tuple):
        return [describe(v) for v in value]
    return value


def parse_dotenv(text: str) -> dict[str, str]:
    """``KEY=value`` lines; ``#`` comments, blank lines, optional ``export``,
    matching single or double quotes removed. No expansion, no multi-line values."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").strip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= QUOTED and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if key.strip():
            values[key.strip()] = value
    return values


def known_keys(cls: type[Settings], prefix: str, path: str = "") -> set[str]:
    keys: set[str] = set()
    for name, hint in cls.fields_of().items():
        if name.startswith("_") or typing.get_origin(hint) is typing.ClassVar:
            continue
        key = f"{path}{name.upper()}"
        if isinstance(hint, type) and issubclass(hint, Settings):
            keys |= known_keys(hint, prefix, key + SEPARATOR)
        else:
            keys.add(prefix + key)
    return keys


MISSING = object()


def build(
    cls: type[Settings],
    prefix: str,
    source: Mapping[str, Any],
    overrides: dict[str, Any],
    problems: list[str],
    path: str = "",
) -> Any:
    """An instance of ``cls`` or ``None`` (with every problem appended)."""
    values: dict[str, Any] = {}
    failed = False
    for name, hint in cls.fields_of().items():
        if name.startswith("_") or typing.get_origin(hint) is typing.ClassVar:
            continue
        value = _build_field(cls, name, hint, prefix, source, overrides, problems, path)
        failed |= value is MISSING
        if value is not MISSING and value is not DEFAULT:
            values[name] = value
    if failed:
        return None
    return _construct_settings(cls, prefix, path, values, problems)


def _build_field(  # noqa: PLR0913, PLR0917 - settings build context is shared through recursion
    cls: type[Settings],
    name: str,
    hint: Any,
    prefix: str,
    source: Mapping[str, Any],
    overrides: dict[str, Any],
    problems: list[str],
    path: str,
) -> Any:
    key = f"{path}{name.upper()}"
    if not (isinstance(hint, type) and issubclass(hint, Settings)):
        return scalar(cls, name, hint, prefix + key, source, overrides, problems)
    nested = overrides.get(name, {})
    if not isinstance(nested, Mapping):
        problems.append(f"{prefix}{key}: overrides for a group must be a mapping")
        nested = {}
    value = build(hint, prefix, source, dict(nested), problems, key + SEPARATOR)
    return MISSING if value is None else value


def _construct_settings(
    cls: type[Settings],
    prefix: str,
    path: str,
    values: dict[str, Any],
    problems: list[str],
) -> Any:
    try:
        return cls(**values)
    except (ValueError, TypeError) as reason:  # never carries a configured value
        problems.append(f"{prefix}{path.removesuffix(SEPARATOR)}: {reason}")
    return None


DEFAULT = object()


def scalar(
    cls: type[Settings],
    name: str,
    hint: Any,
    full: str,
    source: Mapping[str, Any],
    overrides: dict[str, Any],
    problems: list[str],
) -> Any:
    """The converted value (which may be ``None``), ``DEFAULT`` when the field
    default applies, or ``MISSING`` after recording a problem."""
    if name in overrides:
        raw = overrides[name]
    elif full in source:
        raw = source[full]
    elif has_default(cls, name):
        return DEFAULT
    else:
        problems.append(f"{full}: required, not set")
        return MISSING
    try:
        return convert(hint, raw)
    except ValueError as reason:
        problems.append(f"{full}: {reason}")
        return MISSING


def has_default(cls: type[Settings], name: str) -> bool:
    field = next(f for f in dataclasses.fields(cls) if f.name == name)
    return (
        field.default is not dataclasses.MISSING
        or field.default_factory is not dataclasses.MISSING
    )


def convert_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in TRUE | FALSE:
        return text in TRUE
    raise ValueError("must be true or false")


INTEGER = re.compile(r"[+-]?[0-9]{1,18}", re.ASCII)
DECIMAL = re.compile(
    r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]{1,4})?", re.ASCII
)


def convert_int(raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError("must be an integer")
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if not INTEGER.fullmatch(text):  # no underscores, no other scripts' digits
        raise ValueError("must be an integer")
    return int(text)


def convert_float(raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError("must be a number")
    text = str(raw).strip()
    if not isinstance(raw, int | float) and not DECIMAL.fullmatch(text):
        raise ValueError("must be a number")
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("must be a finite number")
    return number


SCALARS: dict[Any, Callable[[Any], Any]] = {
    bool: convert_bool,
    int: convert_int,
    float: convert_float,
    str: lambda raw: raw if isinstance(raw, str) else str(raw),
    Secret: lambda raw: raw if isinstance(raw, Secret) else Secret(str(raw)),
}


def convert_optional(hint: Any, raw: Any) -> Any:
    arguments = typing.get_args(hint)
    options = [a for a in arguments if a is not type(None)]
    if len(options) != 1 or len(arguments) != len(options) + 1:
        raise ValueError("unsupported union type")
    if raw is None or raw == "":
        return None
    return convert(options[0], raw)


def convert_list(hint: Any, raw: Any) -> list[Any] | tuple[Any, ...]:
    """``list[T]`` or the immutable ``tuple[T, ...]`` (preferred in settings)."""
    item = typing.get_args(hint)[0]
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list | tuple):
        raise ValueError("must be a list")
    converted = [convert(item, part) for part in raw]
    return tuple(converted) if typing.get_origin(hint) is tuple else converted


def convert(hint: Any, raw: Any) -> Any:  # noqa: PLR0911 - each setting type has a direct conversion
    """``raw`` (environment text or an ``overrides`` value) as ``hint``."""
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        return convert_optional(hint, raw)
    if origin is Literal:
        choices = typing.get_args(hint)
        if raw not in choices:
            raise ValueError(f"must be one of {list(choices)}")
        return raw
    if origin in (list, tuple):
        return convert_list(hint, raw)
    if hint in SCALARS:
        return SCALARS[hint](raw)
    if isinstance(hint, type) and issubclass(hint, StrEnum):
        if isinstance(raw, hint):
            return raw
        try:
            return hint(raw)
        except (TypeError, ValueError):
            choices_text = ", ".join(member.value for member in hint)
            raise ValueError(f"must be one of: {choices_text}") from None
    raise ValueError(f"unsupported type {getattr(hint, '__name__', hint)}")


class ExecutionSettings(Settings):
    sync_workers: int = 8
    sync_queue: int = 64
    request_timeout: float = 30.0
    max_body_bytes: int = 1024 * 1024
    max_query_bytes: int = 8192
    max_header_bytes: int = 32768

    def validate(self) -> None:
        if self.sync_workers < 1 or self.sync_queue < 0:
            raise ValueError("sync_workers must be >= 1 and sync_queue >= 0")
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if not 1 <= self.max_body_bytes <= MAX_BODY_LIMIT:
            raise ValueError(f"max_body_bytes must be 1 to {MAX_BODY_LIMIT}")
        if not 1 <= self.max_query_bytes <= MAX_REQUEST_INPUT_BYTES:
            raise ValueError(f"max_query_bytes must be 1 to {MAX_REQUEST_INPUT_BYTES}")
        if not 1 <= self.max_header_bytes <= MAX_REQUEST_INPUT_BYTES:
            raise ValueError(f"max_header_bytes must be 1 to {MAX_REQUEST_INPUT_BYTES}")
        if self.sync_workers > MAX_SYNC_WORKERS:
            raise ValueError(f"sync_workers must be at most {MAX_SYNC_WORKERS}")


class LifecycleSettings(Settings):
    startup_timeout: float = 30.0
    shutdown_timeout: float = 10.0

    def validate(self) -> None:
        for value in (self.startup_timeout, self.shutdown_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("timeouts must be positive and finite")


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, exception."""

    def __init__(self, include_request_id: bool = False) -> None:
        super().__init__()
        self.include_request_id = include_request_id

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if self.include_request_id:
            rid = getattr(record, "hinoki_request_id", None)
            if rid is not None:
                entry["request_id"] = rid
        return json.dumps(entry, default=str)


class _RequestIdFilter(logging.Filter):
    """Attach the active request ID to Hinoki-owned log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.hinoki_request_id = request_id.get()
        return True


class _TextFormatter(logging.Formatter):
    """Append the request ID only when the record was emitted in a request."""

    def __init__(self, fmt: str, include_request_id: bool = False) -> None:
        super().__init__(fmt)
        self.include_request_id = include_request_id

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        rid = getattr(record, "hinoki_request_id", None)
        if self.include_request_id and rid is not None:
            return f"{rendered} request_id={rid}"
        return rendered


class StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever ``sys.stderr`` is at the moment of each record."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:  # the base class assigns it; ignore
        pass


class LoggingSettings(Settings):
    level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    format: Literal["text", "json"] = "text"
    include_request_id: bool = False
    config: str | None = None

    HANDLER: ClassVar[str] = "hinoki-settings"

    def validate(self) -> None:
        if self.config is not None and ":" not in self.config:
            raise ValueError("config must be 'module:attribute'")

    def custom_config(self) -> dict[str, Any] | None:
        """The ``logging.config.dictConfig`` mapping ``config`` names (called first
        when it is a function), with ``disable_existing_loggers`` off unless the
        mapping says otherwise."""
        if self.config is None:
            return None
        found = load_target(self.config)
        if callable(found):
            found = found()
        if not isinstance(found, Mapping):
            raise TypeError(f"{self.config} must be a logging dictConfig mapping")
        return {"disable_existing_loggers": False, **found}

    def apply(self) -> None:
        """Configure the ``hinoki`` logger tree (not the root logger), then apply
        ``config`` if set; safe to call again, it replaces its own handler."""
        logger = logging.getLogger("hinoki")
        for handler in list(logger.handlers):
            if getattr(handler, "name", "") == self.HANDLER:
                logger.removeHandler(handler)
        handler = StderrHandler()
        handler.name = self.HANDLER
        if self.include_request_id:
            handler.addFilter(_RequestIdFilter())
        handler.setFormatter(
            JsonFormatter(include_request_id=self.include_request_id)
            if self.format == "json"
            else _TextFormatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                include_request_id=self.include_request_id,
            )
        )
        logger.addHandler(handler)
        logger.setLevel(self.level.upper())
        custom = self.custom_config()
        if custom is not None:
            logging.config.dictConfig(custom)


class DocsSettings(Settings):
    title: str = "Hinoki API"
    version: str = "0.1.0"
    openapi_url: str | None = None
    docs_url: str | None = None
    redoc_url: str | None = None


class DatabaseEngine(StrEnum):
    """The backend that ``DatabaseSettings.engine`` selects."""

    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MARIADB = "mariadb"


LOCAL_DATABASE_URL = "sqlite://./.hinoki/local.sqlite3"
DATABASE_FIELDS = ("name", "user", "password", "host", "port", "options")
MAX_DATABASE_PORT = 65535
DATABASE_SENSITIVE_OPTION_PARTS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "credential",
)
DATABASE_IDENTITY_OPTIONS = frozenset(
    {"user", "username", "host", "hostaddr", "port", "dbname", "database", "socket"}
)


class DatabaseSettings(Settings):
    """Set either ``url`` or ``engine`` with its connection fields; with
    neither, the database is local SQLite at ``.hinoki/local.sqlite3``.

    ``name`` is the database name, or the file path for SQLite. ``options`` are
    ``key=value`` pairs passed in the URL query, such as ``sslmode=verify-full``.
    With ``engine``, ``url`` holds the URL built from the fields.
    """

    url: Secret = dataclasses.field(default_factory=lambda: Secret(LOCAL_DATABASE_URL))
    engine: DatabaseEngine | None = None
    name: str = ""
    user: str = ""
    password: Secret | None = None
    host: str = ""
    port: int | None = None
    options: tuple[str, ...] = ()
    max_size: int = 10

    def validate(self) -> None:
        if self.max_size < 1:
            raise ValueError("max_size must be >= 1")
        if isinstance(self.password, str):  # built directly in code
            object.__setattr__(self, "password", Secret(self.password))
        fields = [name for name in DATABASE_FIELDS if self.field_is_set(name)]
        if self.engine is None:
            if fields:
                raise ValueError(f"{', '.join(fields)} need engine")
            return
        self.check_engine()
        if self.engine is DatabaseEngine.SQLITE:
            self.check_sqlite_fields(fields)
        else:
            self.check_server_fields()
        object.__setattr__(self, "url", BuiltDatabaseURL(self.built_url(self.engine)))

    def check_engine(self) -> None:
        if not isinstance(self.engine, DatabaseEngine):
            raise TypeError("engine must be a settings.DatabaseEngine member")
        explicit = not isinstance(self.url, BuiltDatabaseURL)
        if explicit and self.url.reveal() != LOCAL_DATABASE_URL:
            raise ValueError("set either url or engine with its fields, not both")
        if not self.name:
            raise ValueError("engine needs name (the database, or the SQLite file)")

    def check_sqlite_fields(self, fields: list[str]) -> None:
        server_only = [name for name in fields if name != "name"]
        if server_only:
            raise ValueError(f"sqlite takes only name; remove {', '.join(server_only)}")
        if self.name == ":memory:" or "?" in self.name or "#" in self.name:
            raise ValueError("sqlite name must be a file path")

    def check_server_fields(self) -> None:
        if self.port is not None and not 1 <= self.port <= MAX_DATABASE_PORT:
            raise ValueError("port must be between 1 and 65535")
        socket = self.host.startswith("/") and self.engine is DatabaseEngine.POSTGRESQL
        if not socket and any(char in self.host for char in "/?#@ \t"):
            raise ValueError("host must be a host name or address")
        self.check_options()

    def check_options(self) -> None:
        for option in self.options:
            key, separator, _ = option.partition("=")
            normalized = key.strip().lower().replace("-", "_")
            sensitive = any(
                part in normalized for part in DATABASE_SENSITIVE_OPTION_PARTS
            )
            if (
                not separator
                or not key
                or key != key.strip()
                or sensitive
                or normalized in DATABASE_IDENTITY_OPTIONS
            ):
                raise ValueError(
                    "options must be key=value pairs and cannot contain credentials "
                    "or override connection fields"
                )

    def field_is_set(self, name: str) -> bool:
        value = getattr(self, name)
        if isinstance(value, Secret):
            return value.reveal() != ""
        return value is not None and value not in ("", ())

    def built_url(self, engine: DatabaseEngine) -> str:
        if engine is DatabaseEngine.SQLITE:
            return f"sqlite://{self.name}"
        userinfo = quote(self.user, safe="")
        if self.field_is_set("password"):
            userinfo += ":" + quote(
                typing.cast(Secret, self.password).reveal(), safe=""
            )
        return (
            f"{engine.value}://{userinfo + '@' if userinfo else ''}{self.url_host()}"
            f"{f':{self.port}' if self.port is not None else ''}"
            f"/{quote(self.name, safe='')}{self.url_query()}"
        )

    def url_host(self) -> str:
        host = self.host or "localhost"
        if host.startswith("/"):  # a PostgreSQL Unix-socket directory
            return quote(host, safe="")
        if ":" in host and not host.startswith("["):
            return f"[{host}]"
        return host

    def url_query(self) -> str:
        query = "&".join(
            "{}={}".format(*(quote(part, safe="/") for part in item.split("=", 1)))
            for item in self.options
        )
        return f"?{query}" if query else ""


class BuiltDatabaseURL(Secret):
    """The URL ``DatabaseSettings`` built from its fields; rebuilt on revalidation."""

    __slots__ = ()


class ServerSettings(Settings):
    """How ``hinoki serve`` and ``dev`` run the application. ``serve`` applies
    these; they do nothing when another runner starts the app.

    ``server`` picks Kiso, Hinoki's Rust server (the default), or Granian.
    ``access_log``
    prints one line per request on either server; unset, ``hinoki dev`` logs and
    ``hinoki serve`` does not, and ``True`` or ``False`` decides for both.
    ``access_log_format`` (Rust server only: ``pretty``, ``plain``, ``json`` or
    ``logger``) chooses how a line looks; unset, it is ``pretty`` on a colour
    terminal and ``plain`` otherwise, and setting it turns the log on unless
    ``access_log`` is ``False``. ``json`` writes one object per line with an RFC
    3339 UTC ``time``; ``logger`` sends each to the ``hinoki.access`` logger.
    ``access_log_skip`` (Rust server only) lists paths the log leaves out, such
    as health checks: exact, or a prefix when the entry ends with ``*``.
    ``access_log_slow_ms`` keeps only answers at least that slow. Both always
    keep failures (status 400 and up). ``recent_requests`` (Rust server only) is a
    file that also receives each record as JSON, for ``hinoki mcp``'s
    ``recent_requests`` tool. ``server_timing`` (Rust server only) adds a
    ``Server-Timing`` header with the time taken and the serving lane: unset
    means in ``hinoki dev`` only.
    ``max_connections``, ``header_read_timeout``,
    ``body_read_timeout``, ``write_timeout``, ``native_dispatch`` and
    ``access_log_format``, ``access_log_skip`` and ``access_log_slow_ms`` belong to
    the Rust server (``None`` keeps its default). ``hinoki serve`` refuses a
    setting the chosen server cannot honour. ``tls_cert_file`` and
    ``tls_key_file`` (PEM files, set together) serve HTTPS on either server;
    ``http2`` adds HTTP/2 to it (ALPN ``h2``, TLS only, off by default).
    ``native_dispatch`` (unset means on)
    lets the Rust server run the ``async def`` handlers it can prove safe
    without an asyncio task; ``False`` keeps every handler in one.
    """

    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1
    runtime_threads: int = 1
    backlog: int = 1024
    access_log: bool | None = None
    access_log_format: Literal["pretty", "plain", "json", "logger"] | None = None
    access_log_skip: tuple[str, ...] | None = None
    access_log_slow_ms: float | None = None
    recent_requests: str | None = None
    server_timing: bool | None = None
    trusted_proxies: tuple[str, ...] = ()
    request_id_header: str = "x-request-id"
    trust_request_id: bool = False
    server: Literal["granian", "kiso"] = "kiso"
    max_connections: int | None = None
    header_read_timeout: float | None = None
    body_read_timeout: float | None = None
    write_timeout: float | None = None
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    http2: bool = False
    native_dispatch: bool | None = None

    def validate(self) -> None:
        if not 0 < self.port < MAX_PORT:
            raise ValueError("port must be 1 to 65535")
        if not 0 < self.workers <= MAX_WORKERS or self.runtime_threads < 1:
            raise ValueError(f"workers 1 to {MAX_WORKERS}, runtime_threads >= 1")
        if self.backlog < 1:
            raise ValueError("backlog must be positive")
        for proxy in self.trusted_proxies:
            if "%" in proxy:
                raise ValueError(
                    "trusted_proxies entries cannot carry an IPv6 zone id "
                    f"({proxy!r}); list the address without '%zone'"
                )
            try:
                ipaddress.ip_network(proxy, strict=False)
            except ValueError as exc:
                raise ValueError(
                    "trusted_proxies entries must be IP addresses or CIDRs"
                ) from exc
        self.validate_rust_limits()
        self.validate_access_log()
        self.validate_tls()
        if TOKEN.fullmatch(self.request_id_header) is None:
            raise ValueError("request_id_header must be an HTTP token")
        request_id_header = self.request_id_header.lower()
        if (
            request_id_header in REQUEST_ID_HEADER_CONFLICTS
            and request_id_header != "x-request-id"
        ) or request_id_header.startswith("access-control-"):
            raise ValueError("request_id_header conflicts with a framework header")

    def validate_rust_limits(self) -> None:
        if self.max_connections is not None and not (
            0 < self.max_connections <= MAX_CONNECTIONS
        ):
            raise ValueError(f"max_connections must be 1 to {MAX_CONNECTIONS}")
        for name in ("header_read_timeout", "body_read_timeout", "write_timeout"):
            value = getattr(self, name)
            if value is not None and not 0 < value <= MAX_READ_TIMEOUT:
                raise ValueError(
                    f"{name} must be above 0 and at most {MAX_READ_TIMEOUT}"
                )

    def validate_access_log(self) -> None:
        slow = self.access_log_slow_ms
        if slow is not None and not 0 <= slow <= MAX_READ_TIMEOUT * 1000:
            raise ValueError("access_log_slow_ms must be a number of milliseconds >= 0")
        for rule in self.access_log_skip or ():
            if not rule.startswith("/"):
                raise ValueError(f"access_log_skip entries start with '/': {rule!r}")

    def validate_tls(self) -> None:
        if (self.tls_cert_file is None) != (self.tls_key_file is None):
            raise ValueError("tls_cert_file and tls_key_file must be set together")
        if self.http2 and self.tls_cert_file is None:
            raise ValueError("http2 needs TLS: set tls_cert_file and tls_key_file")
        for name in ("tls_cert_file", "tls_key_file"):
            path = getattr(self, name)
            if path is not None and not Path(path).is_file():
                raise ValueError(f"{name} is not a file: {path}")

    def conflicts(self, server: str | None = None) -> list[str]:
        """The settings that ``server`` (default: this group's) cannot honour."""
        if (server or self.server) == "kiso":
            return []
        return [name for name in KISO_ONLY if getattr(self, name) is not None]

    def logs_access(self, dev: bool) -> bool:
        """Whether requests are logged: ``access_log`` if set, else when a format
        is chosen or in ``dev``."""
        if self.access_log is not None:
            return self.access_log
        return dev or self.access_log_format is not None


class FrameOptions(StrEnum):
    """Values accepted by the ``X-Frame-Options`` response policy."""

    DENY = "deny"
    SAMEORIGIN = "sameorigin"


class ReferrerPolicy(StrEnum):
    """Values accepted by the ``Referrer-Policy`` response policy."""

    NO_REFERRER = "no-referrer"
    NO_REFERRER_WHEN_DOWNGRADE = "no-referrer-when-downgrade"
    SAME_ORIGIN = "same-origin"
    ORIGIN = "origin"
    STRICT_ORIGIN = "strict-origin"
    ORIGIN_WHEN_CROSS_ORIGIN = "origin-when-cross-origin"
    STRICT_ORIGIN_WHEN_CROSS_ORIGIN = "strict-origin-when-cross-origin"
    UNSAFE_URL = "unsafe-url"


class SecuritySettings(Settings):
    """Browser response protections and an optional HTTPS redirect."""

    redirect_https: bool = False
    hsts_seconds: int = 0
    hsts_include_subdomains: bool = False
    hsts_preload: bool = False
    frame_options: FrameOptions = FrameOptions.SAMEORIGIN
    referrer_policy: ReferrerPolicy = ReferrerPolicy.SAME_ORIGIN

    def validate(self) -> None:
        if not isinstance(self.frame_options, FrameOptions):
            raise ValueError("frame_options must be a FrameOptions value")
        if not isinstance(self.referrer_policy, ReferrerPolicy):
            raise ValueError("referrer_policy must be a ReferrerPolicy value")
        if not 0 <= self.hsts_seconds <= HSTS_MAX_SECONDS:
            raise ValueError("hsts_seconds must be between 0 and 63072000")
        if self.hsts_seconds == 0 and (
            self.hsts_include_subdomains or self.hsts_preload
        ):
            raise ValueError("HSTS subdomain and preload options require hsts_seconds")
        if self.hsts_preload and (
            not self.hsts_include_subdomains
            or self.hsts_seconds < HSTS_PRELOAD_MIN_SECONDS
        ):
            raise ValueError(
                "hsts_preload requires include_subdomains and at least 31536000 seconds"
            )


class AdminSettings(Settings):
    """The admin site: the secret that keys its CSRF tokens and the rules of
    its own users' sessions and passwords."""

    secret: Secret | None = None
    session_hours: int = 12  # a session ends this long after sign-in, whatever else
    idle_minutes: int = 120  # ... or this long after its last request
    min_password_length: int = 12

    def validate(self) -> None:
        if self.secret is not None and len(self.secret.reveal().encode()) < MIN_SECRET:
            raise ValueError(f"secret needs at least {MIN_SECRET} bytes")
        if not 1 <= self.session_hours <= MAX_SESSION_HOURS:
            raise ValueError(f"session_hours must be between 1 and {MAX_SESSION_HOURS}")
        if not 1 <= self.idle_minutes <= self.session_hours * 60:
            raise ValueError("idle_minutes must be between 1 and session_hours * 60")
        if not MIN_PASSWORD_LENGTH <= self.min_password_length <= MAX_PASSWORD_LENGTH:
            raise ValueError(
                f"min_password_length must be between {MIN_PASSWORD_LENGTH} "
                f"and {MAX_PASSWORD_LENGTH}"
            )


class PaginationSettings(Settings):
    """Defaults for ``PageNumberPagination`` that an action does not set itself."""

    page_size: int = 20
    max_page_size: int = 100

    def validate(self) -> None:
        if not 1 <= self.page_size <= self.max_page_size:
            raise ValueError("need 1 <= page_size <= max_page_size")


def is_host_pattern(entry: str) -> bool:
    """``*``, a host name, ``.domain`` (the domain and its subdomains) or an IP
    address; never a scheme, port or path."""
    if entry == ANY:
        return True
    if ":" in entry:  # only an IPv6 address may contain colons
        try:
            ipaddress.IPv6Address(entry)
        except ValueError:
            return False
        return entry.isascii()  # a zone ID may be any text; entries are ASCII
    return HOST_PATTERN.fullmatch(entry) is not None


class CorsSettings(Settings):
    """Which browser origins may read responses. Off while ``origins`` is empty."""

    origins: tuple[str, ...] = ()
    methods: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "QUERY")
    headers: tuple[str, ...] = ("authorization", "content-type")
    expose_headers: tuple[str, ...] = ()
    credentials: bool = False
    max_age: int = 600

    def validate(self) -> None:
        if ANY in self.origins and len(self.origins) > 1:
            raise ValueError("origins: '*' must be the only entry")
        if ANY in self.origins and self.credentials:
            raise ValueError("origins: '*' cannot be combined with credentials")
        if any(o != ANY and ORIGIN.fullmatch(o) is None for o in self.origins):
            raise ValueError("origins must look like scheme://host[:port], or be '*'")
        names = (*self.methods, *self.headers, *self.expose_headers)
        if any(TOKEN.fullmatch(name) is None for name in names):
            raise ValueError("methods and headers must be HTTP token names")
        if not 0 <= self.max_age <= MAX_CORS_AGE:
            raise ValueError(f"max_age must be 0 to {MAX_CORS_AGE} seconds")


class AppSettings(Settings):
    """The built-in groups every application has; subclass to add your own."""

    execution: ExecutionSettings = dataclasses.field(default_factory=ExecutionSettings)
    lifecycle: LifecycleSettings = dataclasses.field(default_factory=LifecycleSettings)
    logging: LoggingSettings = dataclasses.field(default_factory=LoggingSettings)
    docs: DocsSettings = dataclasses.field(default_factory=DocsSettings)
    server: ServerSettings = dataclasses.field(default_factory=ServerSettings)
    pagination: PaginationSettings = dataclasses.field(
        default_factory=PaginationSettings
    )
    cors: CorsSettings = dataclasses.field(default_factory=CorsSettings)
    security: SecuritySettings = dataclasses.field(default_factory=SecuritySettings)
    admin: AdminSettings = dataclasses.field(default_factory=AdminSettings)
    timezone: str = "UTC"
    debug: bool = False
    allowed_hosts: tuple[str, ...] = (ANY,)

    def validate(self) -> None:
        if not self.allowed_hosts:
            raise ValueError("allowed_hosts must name at least one host, or '*'")
        if ANY in self.allowed_hosts and len(self.allowed_hosts) > 1:
            raise ValueError("allowed_hosts: '*' must be the only entry")
        if self.security.redirect_https and ANY in self.allowed_hosts:
            raise ValueError("security.redirect_https requires explicit allowed_hosts")
        if not all(is_host_pattern(entry) for entry in self.allowed_hosts):
            raise ValueError(
                "allowed_hosts entries are host names, '.domain' or '*' "
                "(no scheme, port or path; an international name in its ASCII "
                "IDNA form)"
            )
        try:
            ZoneInfo(self.timezone)
        except (TypeError, ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone name") from exc

    def runtime_config(self) -> RuntimeConfig:
        return RuntimeConfig(
            sync_workers=self.execution.sync_workers,
            sync_queue=self.execution.sync_queue,
            request_timeout=self.execution.request_timeout,
            startup_timeout=self.lifecycle.startup_timeout,
            shutdown_timeout=self.lifecycle.shutdown_timeout,
            max_body_bytes=self.execution.max_body_bytes,
            max_query_bytes=self.execution.max_query_bytes,
            max_header_bytes=self.execution.max_header_bytes,
        )
