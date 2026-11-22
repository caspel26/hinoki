"""Pagination: page-number and keyset (cursor) parameters, strategies and pages."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from hinoki import core
from hinoki.errors import ErrorDetail, ValidationError
from hinoki.schema import encode

MIN_SECRET = 32  # bytes; the Rust codec enforces the same floor


@dataclass(frozen=True, slots=True)
class PageParams:
    """Validated request for one page (1-based)."""

    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


DEFAULT_PAGE_SIZE = 20
MAX_NUMBER = 1_000_000_000  # the largest page number accepted
MAX_DIGITS = 18
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
        limit = MAX_NUMBER if upper is None else upper
        digits = len(values) == 1 and raw.isascii() and raw.isdigit()
        short = (
            digits and len(raw) <= MAX_DIGITS
        )  # no giant int(), no u64 offset overflow
        if not digits or (short and int(raw) < 1):
            errors.append(ErrorDetail(name, "type", "expected a positive integer"))
        elif not short or int(raw) > limit:
            errors.append(ErrorDetail(name, "le", f"must be at most {limit}"))
        else:
            return int(raw)
        return default


MAX_TOKEN = 1024  # characters; the Rust codec rejects longer tokens as well


@dataclass(frozen=True, slots=True)
class CursorParams:
    """Validated request for one keyset page: the size and the raw token.

    The token is only decoded when the query runs, because the signature binds
    it to the ordering of that query."""

    limit: int
    token: str | None
    strategy: CursorPagination


@dataclass(frozen=True, slots=True)
class CursorPagination:
    """Strategy: ``?cursor=TOKEN&limit=N``, keyset pages with signed cursors.

    The cursor is opaque. It is signed with ``secret`` (at least 32 bytes) and
    bound to the table and ordering of the query, so a changed or foreign token
    is a 422, never a 500. ``ttl`` (seconds) makes cursors expire. Anything left
    out comes from the application's ``pagination`` settings when the action is
    included; values given here win."""

    page_size: int | None = None
    max_page_size: int | None = None
    secret: str | None = field(default=None, repr=False)
    ttl: int | None = None

    def __post_init__(self) -> None:
        if self.page_size is not None and self.max_page_size is not None:
            PageNumberPagination.check(self.page_size, self.max_page_size)
        if self.secret is not None and len(self.secret.encode()) < MIN_SECRET:
            raise ValueError(f"secret needs at least {MIN_SECRET} bytes")
        if self.ttl is not None and self.ttl < 1:
            raise ValueError("ttl must be positive")

    def resolved(
        self,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_page_size: int = DEFAULT_MAX_PAGE_SIZE,
        secret: str | None = None,
        ttl: int | None = None,
    ) -> CursorPagination:
        """This strategy with everything left out filled from the defaults given.
        Raises ``ValueError`` when the result is invalid or has no secret."""
        size = page_size if self.page_size is None else self.page_size
        upper = max_page_size if self.max_page_size is None else self.max_page_size
        key = secret if self.secret is None else self.secret
        if key is None:
            raise ValueError(
                "cursor pagination needs a secret: set CursorPagination(secret=...) "
                "or pagination.cursor_secret in the settings"
            )
        PageNumberPagination.check(size, upper)
        return CursorPagination(size, upper, key, ttl if self.ttl is None else self.ttl)

    @property
    def limits(self) -> tuple[int, int]:
        size = DEFAULT_PAGE_SIZE if self.page_size is None else self.page_size
        upper = (
            DEFAULT_MAX_PAGE_SIZE if self.max_page_size is None else self.max_page_size
        )
        return size, upper

    def parse(self, query: dict[str, list[str]]) -> CursorParams:
        errors: list[ErrorDetail] = []
        default_size, upper = self.limits
        size = PageNumberPagination.number(query, "limit", default_size, upper, errors)
        token = self.token(query, errors)
        if errors:
            raise ValidationError(errors)
        return CursorParams(size, token, self)

    @staticmethod
    def token(query: dict[str, list[str]], errors: list[ErrorDetail]) -> str | None:
        values = query.get("cursor")
        if values is None:
            return None
        if len(values) != 1 or not 0 < len(values[0]) <= MAX_TOKEN:
            errors.append(ErrorDetail("cursor", "invalid", "invalid cursor"))
            return None
        return values[0]

    def codec(self, fingerprint: bytes) -> core._CursorCodec:
        if self.secret is None:
            raise ValueError("an unresolved CursorPagination has no secret")
        return core._CursorCodec(self.secret.encode(), fingerprint, self.ttl)


@dataclass(frozen=True, slots=True)
class CursorPage[T]:
    """One keyset page: ``next``/``previous`` are cursors, or ``None`` at an end."""

    items: list[T]
    next: str | None
    previous: str | None
    page_size: int

    def map[U](self, convert: Callable[[T], U]) -> CursorPage[U]:
        items = [convert(item) for item in self.items]
        return CursorPage(items, self.next, self.previous, self.page_size)

    def dump(self) -> dict[str, object]:
        return {
            "items": [encode(item) for item in self.items],
            "next": self.next,
            "previous": self.previous,
            "page_size": self.page_size,
        }


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


Pagination = PageNumberPagination | CursorPagination
