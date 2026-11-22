"""Keyset (cursor) pagination over a ``QuerySet``.

The Python side only orders the query, picks the walking direction and turns
errors into 422s. The keyset ``WHERE`` is compiled in Rust, the cursor is signed,
checked and built in Rust, and a generated list gets its whole JSON page from
Rust (see ``Database._fetch_cursor_wire``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hinoki import core
from hinoki.errors import ErrorDetail, FieldError, ValidationError
from hinoki.orm.context import current_database
from hinoki.pagination import CursorPage, CursorParams

if TYPE_CHECKING:
    from hinoki.orm.queryset import QuerySet

FIRST, FORWARD, BACKWARD = 0, 1, 2  # the ``mode`` argument of the Rust fetch
# Column kinds a cursor can carry; nullable columns cannot be sort keys because
# NULL has no place in a keyset comparison.
KEY_KINDS = frozenset({"int", "str", "uuid", "datetime", "float"})
CURSOR_MESSAGES = {"invalid": "invalid cursor", "expired": "cursor expired"}

type Order = tuple[tuple[str, bool], ...]


def cursor_order(query: QuerySet[Any]) -> Order:
    """The query's ordering up to the primary key, which is added in the
    direction of the last sort key when missing so every row has one place."""
    model = query.model
    key = model.__primary_key__
    order: list[tuple[str, bool]] = []
    for column, descending in query.order:
        order.append((column, descending))
        if column == key:
            break
    else:
        order.append((key, order[-1][1] if order else False))
    for column, _ in order:
        field_type = model.__field_types__[column]
        if field_type.nullable or field_type.kind not in KEY_KINDS:
            raise FieldError(
                f"{model.__name__}.{column} cannot order a cursor page: only "
                f"non-null {', '.join(sorted(KEY_KINDS))} columns can"
            )
    return tuple(order)


def fingerprint(table: str, order: Order) -> bytes:
    """What a cursor is bound to: the table and the ordering it was issued for."""
    keys = ",".join(
        f"{column}{'-' if descending else '+'}" for column, descending in order
    )
    return f"{table}|{keys}".encode()


def invalid(code: str) -> ValidationError:
    return ValidationError([ErrorDetail("cursor", code, CURSOR_MESSAGES[code])])


@dataclass(frozen=True, slots=True)
class CursorRequest:
    """One cursor page, ready to run: the walking query and how to read it."""

    walk: QuerySet[Any]
    keys: list[str]
    codec: core._CursorCodec
    keyset: tuple[list[tuple[str, bool]], list[Any]] | None
    mode: int
    limit: int

    def compile(self, dialect: str) -> tuple[str, list[Any]]:
        return self.walk.compile(dialect=dialect, keyset=self.keyset)

    def page(self, rows: list[Any]) -> CursorPage[Any]:
        """Rows in walking order, with the extra one, as a page with its links."""
        more = len(rows) > self.limit
        rows = rows[: self.limit]
        if self.mode == BACKWARD:
            rows.reverse()
        after, before = self.links(rows, more)
        return CursorPage(rows, after, before, self.limit)

    def links(self, rows: list[Any], more: bool) -> tuple[str | None, str | None]:
        """``(next, previous)``; the same rules as ``Codec::links`` in Rust."""
        if not rows:
            return None, None
        after = self.token(False, rows[-1])
        before = self.token(True, rows[0])
        if self.mode == FIRST:
            return (after if more else None), None
        if self.mode == FORWARD:
            return (after if more else None), before
        return after, (before if more else None)

    def token(self, backward: bool, row: Any) -> str:
        try:
            return self.codec.encode(backward, [getattr(row, key) for key in self.keys])
        except ValueError as exc:
            raise FieldError(f"cannot build a cursor from this row: {exc}") from exc


def prepare(query: QuerySet[Any], params: CursorParams) -> CursorRequest:
    """Validate the token against this query's ordering and build the walk."""
    order = cursor_order(query)
    codec = params.strategy.codec(fingerprint(query.model.__table__, order))
    backward, values = False, None
    if params.token is not None:
        try:
            backward, values = codec.decode(params.token, len(order))
        except ValueError as exc:
            raise invalid("expired" if str(exc) == "expired" else "invalid") from None
    walking = tuple((column, descending != backward) for column, descending in order)
    walk = query.derive(order=walking, limit_to=params.limit + 1, offset_by=None)
    mode = FIRST if values is None else BACKWARD if backward else FORWARD
    keyset = None if values is None else (list(walking), values)
    return CursorRequest(
        walk, [column for column, _ in order], codec, keyset, mode, params.limit
    )


async def fetch_page(query: QuerySet[Any], params: CursorParams) -> CursorPage[Any]:
    """One keyset page of model instances; the query's own filters, scope and
    ``select_related`` apply, its limit and offset do not."""
    return await fetch_request(prepare(query, params))


async def fetch_request(request: CursorRequest) -> CursorPage[Any]:
    database = current_database()
    sql, args = request.compile(database.dialect)
    rows = await request.walk.fetch_compiled(database, sql, args)
    return request.page(rows)
