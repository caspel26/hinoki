"""Field declarations: constraints, defaults and the ``UNSET`` sentinel."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, get_origin, overload


class Unset:
    """Type of ``UNSET``: a field that was absent from the input (PATCH)."""

    _instance: Unset | None = None

    def __new__(cls) -> Unset:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = Unset()


@dataclass(frozen=True, slots=True)
class FieldInfo:
    """Constraints, alias and default of one schema field."""

    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    ge: float | Decimal | None = None
    le: float | Decimal | None = None
    gt: float | Decimal | None = None
    lt: float | Decimal | None = None
    one_of: tuple[Any, ...] | None = None
    max_digits: int | None = None
    decimal_places: int | None = None
    multiple_of: int | Decimal | None = None
    strip: bool | None = None
    lower: bool | None = None
    upper: bool | None = None
    alias: str | None = None
    discriminator: str | None = None
    default_factory: Callable[[], Any] | None = None
    description: str | None = None
    examples: tuple[Any, ...] | None = None
    deprecated: bool | None = None

    def notes(self) -> dict[str, object]:
        """The documentation-only options that were set; they never validate."""
        names = ("description", "examples", "deprecated")
        return {n: getattr(self, n) for n in names if getattr(self, n) is not None}

    def constraints(self) -> dict[str, object]:
        """Only the constraints that were actually set."""
        names = (
            "min_length",
            "max_length",
            "pattern",
            "ge",
            "le",
            "gt",
            "lt",
            "one_of",
            "max_digits",
            "decimal_places",
            "multiple_of",
            "strip",
            "lower",
            "upper",
        )
        return {n: getattr(self, n) for n in names if getattr(self, n) is not None}


# A default (``name: T = Field(...)``) takes only what does not validate: the
# wire name, a discriminator and the documentation notes. Constraints belong in
# ``Annotated[T, Field(...)]``, where the third overload types them.
@overload
def Field[T](
    *,
    default_factory: Callable[[], T],
    alias: str | None = None,
    description: str | None = None,
    examples: Sequence[Any] | None = None,
    deprecated: bool | None = None,
) -> T: ...
@overload
def Field(
    *,
    alias: str | None = None,
    discriminator: str | None = None,
    description: str | None = None,
    examples: Sequence[Any] | None = None,
    deprecated: bool | None = None,
) -> Any: ...
@overload
def Field(
    *,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    ge: float | Decimal | None = None,
    le: float | Decimal | None = None,
    gt: float | Decimal | None = None,
    lt: float | Decimal | None = None,
    one_of: Sequence[Any] | None = None,
    max_digits: int | None = None,
    decimal_places: int | None = None,
    multiple_of: int | Decimal | None = None,
    strip: bool | None = None,
    lower: bool | None = None,
    upper: bool | None = None,
    format: str | None = None,
    alias: str | None = None,
    discriminator: str | None = None,
    description: str | None = None,
    examples: Sequence[Any] | None = None,
    deprecated: bool | None = None,
) -> FieldInfo: ...
def Field(**options: Any) -> Any:
    """Declare constraints (inside ``Annotated``) or a default factory.

    ``tags: list[str] = Field(default_factory=list)`` gives a per-instance
    default; ``title: Annotated[str, Field(min_length=1)]`` adds constraints.
    """
    if "format" in options:
        options = resolve_format(options)
    if options.get("one_of") is not None:
        # a tuple: hashable, so ``Annotated`` can cache it
        options["one_of"] = tuple(options["one_of"])
    if options.get("examples") is not None:
        options["examples"] = tuple(options["examples"])
    return FieldInfo(**options)


def carries_field(hint: object) -> bool:
    """Whether ``hint`` is ``Annotated[...]`` with a ``Field(...)`` among its
    metadata."""
    return get_origin(hint) is Annotated and any(
        isinstance(mark, FieldInfo) for mark in getattr(hint, "__metadata__", ())
    )


# Ready-made patterns for common formats; they run in the Rust engine like any pattern
# (whole value, linear time). ``Field(format="email")`` is ``Field(pattern=...)``.
FORMATS = {
    "email": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}",
    "url": r"https?://[^\s/?#]+([/?#]\S*)?",
    "slug": r"[a-z0-9]+(-[a-z0-9]+)*",
    "hex_color": r"#[0-9a-fA-F]{6}",
}


def resolve_format(options: dict[str, Any]) -> dict[str, Any]:
    """Replace ``format=`` by the pattern it stands for."""
    name = options.pop("format")
    if name not in FORMATS:
        raise ValueError(f"unknown format {name!r}; known: {sorted(FORMATS)}")
    if options.get("pattern") is not None:
        raise ValueError("use format= or pattern=, not both")
    return {**options, "pattern": FORMATS[name]}


# Ready-made annotations: ``email: Email`` is ``Annotated[str, Field(format="email")]``.
Email = Annotated[str, Field(format="email")]
Url = Annotated[str, Field(format="url")]
Slug = Annotated[str, Field(format="slug")]


MASK = "**********"
"""What a ``Secret`` shows instead of its value."""


class Secret:
    """Text that never shows itself: ``repr``, ``str``, ``dump()`` and JSON
    responses give ``**********``; ``reveal()`` returns the text.

    The one secret type of Hinoki: a ``Schema`` field, a ``Settings`` field, a
    ``JWTKey`` and ``SessionSettings(csrf_secret=...)`` all take it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("Secret takes a str")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({MASK!r})"

    def __str__(self) -> str:
        return MASK

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and hmac.compare_digest(
            self._value.encode(), other._value.encode()
        )

    def __hash__(self) -> int:
        return hash((Secret, self._value))

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)


def secret_bytes(value: bytes | str | Secret) -> bytes:
    """The bytes of key material given as bytes, text (UTF-8) or a ``Secret``."""
    if isinstance(value, Secret):
        return value.reveal().encode()
    return value.encode() if isinstance(value, str) else bytes(value)
