"""Field declarations: constraints, defaults and the ``UNSET`` sentinel."""

from __future__ import annotations

from collections.abc import Callable
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
    default_factory: Callable[[], Any] | None = None

    def constraints(self) -> dict[str, object]:
        """Only the constraints that were actually set."""
        names = ("min_length", "max_length", "pattern", "ge", "le", "gt", "lt")
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
) -> FieldInfo: ...
def Field(**options: Any) -> Any:
    """Declare constraints (inside ``Annotated``) or a default factory.

    ``tags: list[str] = Field(default_factory=list)`` gives a per-instance
    default; ``title: Annotated[str, Field(min_length=1)]`` adds constraints.
    """
    return FieldInfo(**options)
