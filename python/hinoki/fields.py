"""Field declarations: constraints, defaults and the ``UNSET`` sentinel."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, overload


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
    """Constraints and default of one schema field."""

    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    ge: float | None = None
    le: float | None = None
    gt: float | None = None
    lt: float | None = None
    one_of: tuple[Any, ...] | None = None
    default_factory: Callable[[], Any] | None = None

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
        )
        return {n: getattr(self, n) for n in names if getattr(self, n) is not None}


@overload
def Field[T](*, default_factory: Callable[[], T]) -> T: ...
@overload
def Field(
    *,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    ge: float | None = None,
    le: float | None = None,
    gt: float | None = None,
    lt: float | None = None,
    one_of: Sequence[Any] | None = None,
    format: str | None = None,
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
    return FieldInfo(**options)


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
