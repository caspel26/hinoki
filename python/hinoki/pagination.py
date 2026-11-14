"""Page-number pagination: parameters, strategy and the page container."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from hinoki.errors import ErrorDetail, ValidationError
from hinoki.schema import encode


@dataclass(frozen=True, slots=True)
class PageParams:
    """Validated request for one page (1-based)."""

    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class PageNumberPagination:
    """Strategy: ``?page=N&page_size=M`` with a hard upper bound on the size.

    A size left out (``None``) comes from the application's ``pagination``
    settings when the action is included, and from 20 / 100 without settings.
    A size given here always wins.
    """

    page_size: int | None = None
    max_page_size: int | None = None

    def __post_init__(self) -> None:
        if self.page_size is not None and self.max_page_size is not None:
            self.check(self.page_size, self.max_page_size)

    @staticmethod
    def check(page_size: int, max_page_size: int) -> None:
        if not 1 <= page_size <= max_page_size:
            raise ValueError("need 1 <= page_size <= max_page_size")

    def sizes(self) -> tuple[int, int]:
        """``(page_size, max_page_size)`` with the framework defaults filled in."""
        return self.resolved().limits

    @property
    def limits(self) -> tuple[int, int]:
        size = DEFAULT_PAGE_SIZE if self.page_size is None else self.page_size
        upper = (
            DEFAULT_MAX_PAGE_SIZE if self.max_page_size is None else self.max_page_size
        )
        return size, upper

    def resolved(
        self,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_page_size: int = DEFAULT_MAX_PAGE_SIZE,
    ) -> PageNumberPagination:
        """This strategy with every size left out filled from the defaults given;
        sizes set on it are kept. Raises ``ValueError`` when the result is invalid."""
        size = page_size if self.page_size is None else self.page_size
        upper = max_page_size if self.max_page_size is None else self.max_page_size
        self.check(size, upper)
        return PageNumberPagination(size, upper)

    def parse(self, query: dict[str, list[str]]) -> PageParams:
        """Read and validate the query; every problem is reported at once."""
        errors: list[ErrorDetail] = []
        default_size, upper = self.sizes()
        page = self.number(query, "page", 1, None, errors)
        size = self.number(query, "page_size", default_size, upper, errors)
        if errors:
            raise ValidationError(errors)
        return PageParams(page, size)

    @staticmethod
    def number(
        query: dict[str, list[str]],
        name: str,
        default: int,
        upper: int | None,
        errors: list[ErrorDetail],
    ) -> int:
        values = query.get(name)
        if values is None:
            return default
        raw = values[0]
        if len(values) > 1 or not raw.isascii() or not raw.isdigit() or int(raw) < 1:
            errors.append(ErrorDetail(name, "type", "expected a positive integer"))
            return default
        if upper is not None and int(raw) > upper:
            errors.append(ErrorDetail(name, "le", f"must be at most {upper}"))
        return int(raw)


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results plus the totals needed to navigate."""

    items: list[T]
    page: int
    page_size: int
    total: int

    @property
    def pages(self) -> int:
        """Number of pages; 0 for an empty collection."""
        return -(-self.total // self.page_size)

    @classmethod
    def of(cls, rows: Sequence[T], params: PageParams) -> Page[T]:
        """Slice an in-memory sequence. Pages past the end are empty."""
        window = rows[params.offset : params.offset + params.page_size]
        return cls(list(window), params.page, params.page_size, len(rows))

    def map[U](self, convert: Callable[[T], U]) -> Page[U]:
        items = [convert(item) for item in self.items]
        return Page(items, self.page, self.page_size, self.total)

    def dump(self) -> dict[str, object]:
        return {
            "items": [encode(item) for item in self.items],
            "page": self.page,
            "page_size": self.page_size,
            "total": self.total,
            "pages": self.pages,
        }
