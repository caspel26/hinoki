"""Typed, immutable settings with environment loading (no Pydantic).

    class BlogSettings(FrameworkSettings):
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
import json
import logging
import math
import os
import re
import sys
import tomllib
import types
import typing
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar, Literal, Self, dataclass_transform

from hinoki.runtime.config import RuntimeConfig

TRUE = frozenset({"1", "true", "yes", "on"})
FALSE = frozenset({"0", "false", "no", "off"})
LEVELS = ("debug", "info", "warning", "error", "critical")
SEPARATOR = "__"
MAX_BODY_LIMIT = 1 << 30  # 1 GiB: a larger "limit" is no limit
MAX_SYNC_WORKERS = 1024
MAX_PORT = 65536
MAX_WORKERS = 256
QUOTED = 2  # shortest quoted value: the two quotes


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
        """``class BlogSettings(FrameworkSettings, prefix="BLOG_", dotenv=".env")``
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


def flatten_toml(  # noqa: PLR0912
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
        full = prefix + key
        if is_secret_hint(hint):
            problems.append(f"{full}: secrets must come from environment or *_FILE")
            continue
        if isinstance(hint, type) and issubclass(hint, Settings):
            if isinstance(value, Mapping):
                flattened.update(
                    flatten_toml(hint, prefix, value, problems, key + SEPARATOR)
                )
            else:
                problems.append(f"{full}: a settings group must be a TOML table")
            continue
        if isinstance(value, dict):
            if hint is not None:
                problems.append(f"{full}: TOML value has an incompatible type")
            else:
                flattened[full] = value  # strict unknown-key checking reports its path
            continue
        if hint is not None and not toml_value_matches(hint, value):
            problems.append(f"{full}: TOML value has an incompatible type")
            continue
        flattened[full] = value
    return flattened


def is_secret_hint(hint: Any) -> bool:
    if hint is Secret:
        return True
    origin = typing.get_origin(hint)
    return origin in (typing.Union, types.UnionType) and Secret in typing.get_args(hint)


def toml_value_matches(hint: Any, value: Any) -> bool:  # noqa: PLR0911 - each type has a direct check
    """Check TOML's native type before applying the shared settings converter."""
    origin = typing.get_origin(hint)
    arguments = typing.get_args(hint)
    if origin in (typing.Union, types.UnionType):
        return any(
            value is None if option is type(None) else toml_value_matches(option, value)
            for option in arguments
        )
    if origin is Literal:
        return any(
            type(value) is type(choice) and value == choice for choice in arguments
        )
    if origin in (list, tuple):
        return isinstance(value, list) and all(
            toml_value_matches(arguments[0], item) for item in value
        )
    if hint is bool:
        return isinstance(value, bool)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if hint is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
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
    defaults = {f.name: f for f in dataclasses.fields(cls)}
    for name, hint in cls.fields_of().items():
        if name.startswith("_") or typing.get_origin(hint) is typing.ClassVar:
            continue
        key = f"{path}{name.upper()}"
        if isinstance(hint, type) and issubclass(hint, Settings):
            lines.append(example_of(hint, prefix, key + SEPARATOR))
            continue
        field = defaults[name]
        required = has_default(cls, name) is False
        default = "" if required or hint is Secret else describe_default(field)
        note = type_name(hint) + (", required" if required else "")
        lines.append(f"# {note}\n{prefix}{key}={default}")
    return "\n".join(lines)


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
        key = f"{path}{name.upper()}"
        if isinstance(hint, type) and issubclass(hint, Settings):
            nested = overrides.get(name, {})
            if not isinstance(nested, Mapping):
                problems.append(
                    f"{prefix}{key}: overrides for a group must be a mapping"
                )
                nested = {}
            value = build(hint, prefix, source, dict(nested), problems, key + SEPARATOR)
            value = MISSING if value is None else value
            failed |= value is MISSING
        else:
            value = scalar(cls, name, hint, prefix + key, source, overrides, problems)
            failed |= value is MISSING
        if value is not MISSING and value is not DEFAULT:
            values[name] = value
    if failed:
        return None
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


def convert(hint: Any, raw: Any) -> Any:
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
    raise ValueError(f"unsupported type {getattr(hint, '__name__', hint)}")


class ExecutionSettings(Settings):
    sync_workers: int = 8
    sync_queue: int = 64
    request_timeout: float = 30.0
    max_body_bytes: int = 1024 * 1024

    def validate(self) -> None:
        if self.sync_workers < 1 or self.sync_queue < 0:
            raise ValueError("sync_workers must be >= 1 and sync_queue >= 0")
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if not 1 <= self.max_body_bytes <= MAX_BODY_LIMIT:
            raise ValueError(f"max_body_bytes must be 1 to {MAX_BODY_LIMIT}")
        if self.sync_workers > MAX_SYNC_WORKERS:
            raise ValueError(f"sync_workers must be at most {MAX_SYNC_WORKERS}")


class LifecycleSettings(Settings):
    startup_timeout: float = 30.0
    shutdown_timeout: float = 10.0

    def validate(self) -> None:
        if min(self.startup_timeout, self.shutdown_timeout) <= 0:
            raise ValueError("timeouts must be positive")


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, exception."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


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

    HANDLER: ClassVar[str] = "hinoki-settings"

    def apply(self) -> None:
        """Configure the ``hinoki`` logger tree (not the root logger); safe to
        call again, it replaces its own handler."""
        logger = logging.getLogger("hinoki")
        for handler in list(logger.handlers):
            if getattr(handler, "name", "") == self.HANDLER:
                logger.removeHandler(handler)
        handler = StderrHandler()
        handler.name = self.HANDLER
        handler.setFormatter(
            JsonFormatter()
            if self.format == "json"
            else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(self.level.upper())


class DocsSettings(Settings):
    title: str = "Hinoki API"
    version: str = "0.1.0"
    openapi_url: str | None = None
    docs_url: str | None = None
    redoc_url: str | None = None


class DatabaseSettings(Settings):
    """For your own settings class: ``database: DatabaseSettings``."""

    url: Secret
    max_size: int = 10

    def validate(self) -> None:
        if self.max_size < 1:
            raise ValueError("max_size must be >= 1")


class ServerSettings(Settings):
    """How ``hinoki serve`` and ``dev`` run the application (Granian).
    ``serve`` applies these; they do nothing when another runner starts the app."""

    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1
    runtime_threads: int = 1
    backlog: int = 1024
    access_log: bool = False

    def validate(self) -> None:
        if not 0 < self.port < MAX_PORT:
            raise ValueError("port must be 1 to 65535")
        if not 0 < self.workers <= MAX_WORKERS or self.runtime_threads < 1:
            raise ValueError(f"workers 1 to {MAX_WORKERS}, runtime_threads >= 1")
        if self.backlog < 1:
            raise ValueError("backlog must be positive")


class FrameworkSettings(Settings):
    """The built-in groups every application has; subclass to add your own."""

    execution: ExecutionSettings = dataclasses.field(default_factory=ExecutionSettings)
    lifecycle: LifecycleSettings = dataclasses.field(default_factory=LifecycleSettings)
    logging: LoggingSettings = dataclasses.field(default_factory=LoggingSettings)
    docs: DocsSettings = dataclasses.field(default_factory=DocsSettings)
    server: ServerSettings = dataclasses.field(default_factory=ServerSettings)

    def runtime_config(self) -> RuntimeConfig:
        return RuntimeConfig(
            sync_workers=self.execution.sync_workers,
            sync_queue=self.execution.sync_queue,
            request_timeout=self.execution.request_timeout,
            startup_timeout=self.lifecycle.startup_timeout,
            shutdown_timeout=self.lifecycle.shutdown_timeout,
            max_body_bytes=self.execution.max_body_bytes,
        )
