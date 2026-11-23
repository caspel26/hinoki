"""Client filters, ordering and search on generated lists."""

import asyncio
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest

from hinoki import API, Hinoki, Schema, core, crud, http
from hinoki.db import Database, DatabaseModule
from hinoki.errors import RegistrationError, ValidationError
from hinoki.filters import Eq, FilterSet, In, Range, Search
from hinoki.orm import Model, QuerySet, field
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.pagination import CursorPagination, PageNumberPagination, PageParams
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings, ServerSettings

SECRET = "k" * 40
Response = tuple[int, bytes]


class Invoice(Model, table="filter_invoices"):
    id: int = field(primary_key=True)
    reference: str
    customer_name: str
    status: str
    paid: bool
    issued: date
    discount: int | None = None
    owner: str = ""
    amount: Decimal | None = None


class InvoiceOut(Schema):
    id: int
    reference: str
    customer_name: str
    status: str
    paid: bool
    issued: date
    discount: int | None
    owner: str
    amount: Decimal | None


class InvoiceFilters(FilterSet):
    paid: Eq[bool]
    status: In[Literal["open", "paid", "overdue"]]
    issued: Range[date]
    discount: Eq[int | None]
    search = Search("reference", "customer_name")
    ordering = ("issued", "reference")


class CursorFilters(FilterSet):
    status: In[str]
    ordering = ("id", "reference")


class AmountFilters(FilterSet):
    amount: Range[Decimal]
    ordering = ("amount",)


PAGES = PageNumberPagination(page_size=20, max_page_size=50)
CURSORS = CursorPagination(page_size=2, max_page_size=4, secret=SECRET)


@crud(
    Invoice, path="/invoices", read=InvoiceOut, pagination=PAGES, filters=InvoiceFilters
)
class Invoices(API):
    pass


@crud(Invoice, path="/mine", read=InvoiceOut, pagination=PAGES, filters=InvoiceFilters)
class Mine(API):
    def get_queryset(self, principal: Any) -> QuerySet[Invoice]:
        return Invoice.all().filter(owner="ann")


@crud(
    Invoice, path="/hooked", read=InvoiceOut, pagination=PAGES, filters=InvoiceFilters
)
class Hooked(API):
    seen: ClassVar[list[InvoiceFilters]] = []

    def filter_queryset(
        self, query: QuerySet[Invoice], params: InvoiceFilters, principal: Any
    ) -> QuerySet[Invoice]:
        Hooked.seen.append(params)
        return query.filter(paid=False)


@crud(
    Invoice,
    path="/cursor",
    read=InvoiceOut,
    pagination=CURSORS,
    filters=CursorFilters,
)
class Cursor(API):
    pass


@crud(
    Invoice, path="/amounts", read=InvoiceOut, pagination=PAGES, filters=AmountFilters
)
class Amounts(API):
    pass


ROWS = [
    # id, reference, customer, status, paid, issued, discount, owner
    (1, "INV-001", "Acme Corp", "open", False, "2024-01-10", None, "ann"),
    (2, "INV-002", "Globex", "paid", True, "2024-02-05", 5, "bob"),
    (3, "INV-003", "ACME Labs", "overdue", False, "2024-03-01", 10, "ann"),
    (4, "INV-004", "Initech", "open", False, "2024-03-15", None, "bob"),
    (5, "INV-005", "Umbrella 100%", "paid", True, "2024-04-20", 5, "ann"),
    (6, "INV-006", "Hooli", "open", True, "2024-05-02", None, "bob"),
]


async def get(port: int, target: str) -> Response:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {target} HTTP/1.1\r\nhost: test\r\n\r\n".encode())
    await writer.drain()
    block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    lines = block.decode("latin-1").split("\r\n")
    length = 0
    for line in lines[1:]:
        name, _, value = line.partition(": ")
        if name.lower() == "content-length":
            length = int(value)
    payload = await asyncio.wait_for(reader.readexactly(length), 5)
    writer.close()
    await writer.wait_closed()
    return int(lines[0].split()[1]), payload


@pytest.fixture
async def worker(tmp_path: Path) -> AsyncIterator[KisoWorker]:
    url = f"sqlite:///{tmp_path / 'filters.db'}"
    database = await Database.connect(url, max_size=1, wait=1)
    await database.execute(
        create_table_sql(Invoice.__table__, table_state(Invoice), "sqlite")
    )
    for row in ROWS:
        await database.execute(
            "INSERT INTO filter_invoices (id, reference, customer_name, status, "
            "paid, issued, discount, owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            *row,
        )
    database.close()
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[Invoices, Mine, Hooked, Cursor, Amounts],
        modules=[DatabaseModule(url)],
    )
    started = KisoWorker(app, "127.0.0.1", 0)
    await started.start()
    try:
        yield started
    finally:
        await started.stop()


async def ids(started: KisoWorker, target: str) -> list[int]:
    status, body = await get(started.address[1], target)
    assert status == 200, body
    return [item["id"] for item in core.decode_json(body)["items"]]


async def refused(started: KisoWorker, target: str) -> list[dict[str, str]]:
    status, body = await get(started.address[1], target)
    assert status == 422, body
    found: list[dict[str, str]] = core.decode_json(body)["details"]
    return found


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("paid=false", [1, 3, 4]),
        ("paid=true", [2, 5, 6]),
        ("status=open", [1, 4, 6]),
        ("status=open,overdue", [1, 3, 4, 6]),
        ("issued.from=2024-03-01", [3, 4, 5, 6]),
        ("issued.to=2024-02-05", [1, 2]),
        ("issued.from=2024-02-05&issued.to=2024-03-15", [2, 3, 4]),
        ("discount=5", [2, 5]),
        ("discount.null=true", [1, 4, 6]),
        ("discount.null=false", [2, 3, 5]),
        ("paid=false&status=open", [1, 4]),
        ("paid=", [1, 2, 3, 4, 5, 6]),
    ],
)
async def test_every_operator_selects_the_expected_rows(
    worker: KisoWorker, query: str, expected: list[int]
) -> None:
    assert sorted(await ids(worker, f"/invoices?{query}")) == expected


async def test_search_matches_any_column_and_treats_wildcards_literally(
    worker: KisoWorker,
) -> None:
    assert sorted(await ids(worker, "/invoices?search=acme")) == [1, 3]
    assert await ids(worker, "/invoices?search=inv-002") == [2]
    assert await ids(worker, "/invoices?search=100%25") == [5]
    assert await ids(worker, "/invoices?search=%25") == [5]
    assert await ids(worker, "/invoices?search=_") == []


async def test_ordering_follows_the_client_and_allows_a_descending_prefix(
    worker: KisoWorker,
) -> None:
    assert await ids(worker, "/invoices?ordering=issued") == [1, 2, 3, 4, 5, 6]
    assert await ids(worker, "/invoices?ordering=-issued") == [6, 5, 4, 3, 2, 1]
    assert await ids(worker, "/invoices?ordering=-reference") == [6, 5, 4, 3, 2, 1]
    assert await ids(worker, "/invoices?paid=true&ordering=-issued,reference") == [
        6,
        5,
        2,
    ]


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("paid=maybe", "type"),
        ("status=closed", "choice"),
        ("status=open,closed", "choice"),
        ("issued.from=yesterday", "type"),
        ("issued.from=2024-13-40", "type"),
        ("discount=many", "type"),
        ("discount.null=sometimes", "type"),
        ("paid=true&paid=false", "duplicate"),
        ("customer_name=x", "unknown"),
        ("owner=bob", "unknown"),
        ("paid.from=true", "operator"),
        ("paid.null=true", "operator"),
        ("status.to=open", "operator"),
        ("issued=2024-01-01", "operator"),
        ("ordering=owner", "choice"),
        ("ordering=customer_name", "choice"),
        ("ordering=--issued", "choice"),
    ],
)
async def test_values_and_names_outside_the_contract_are_422(
    worker: KisoWorker, query: str, code: str
) -> None:
    problems = await refused(worker, f"/invoices?{query}")
    assert code in {problem["code"] for problem in problems}


async def test_every_problem_is_reported_and_values_are_not_echoed(
    worker: KisoWorker,
) -> None:
    problems = await refused(worker, "/invoices?paid=zzzsecret&status=qqqsecret")
    assert len(problems) == 2
    assert "secret" not in repr(problems)


@pytest.mark.parametrize(
    ("query", "path"),
    [
        ("amount.from=10", "amount"),
        ("amount.to=10", "amount"),
        ("ordering=amount", "ordering"),
        ("ordering=-amount", "ordering"),
    ],
)
async def test_decimal_range_and_ordering_are_a_named_422_on_sqlite(
    worker: KisoWorker, query: str, path: str
) -> None:
    problems = await refused(worker, f"/amounts?{query}")
    assert problems == [
        {"path": path, "code": "unsupported", "message": problems[0]["message"]}
    ]
    assert "10" not in problems[0]["message"]
    assert await ids(worker, "/amounts") == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize(
    "target",
    [
        "/invoices?ordering=id;DROP%20TABLE%20filter_invoices",
        "/invoices?ordering=-issued,(select%201)",
        "/invoices?issued.from=2024-01-01'%20OR%20'1'='1",
        "/invoices?status=open'%20OR%201=1%20--",
        "/invoices?search=x'%20OR%20'1'='1",
        "/invoices?%22paid%22=true",
        "/invoices?paid;DROP=true",
        "/invoices?id=1",
    ],
)
async def test_injection_attempts_never_reach_sql(
    worker: KisoWorker, target: str
) -> None:
    status, _ = await get(worker.address[1], target)
    assert status in {200, 422}
    assert await ids(worker, "/invoices") == [1, 2, 3, 4, 5, 6]


async def test_client_filters_never_widen_the_tenant_scope(
    worker: KisoWorker,
) -> None:
    assert await ids(worker, "/mine") == [1, 3, 5]
    assert await ids(worker, "/mine?status=paid") == [5]
    assert await ids(worker, "/mine?paid=true") == [5]
    assert await ids(worker, "/mine?search=globex") == []
    assert await ids(worker, "/mine?status=open,paid,overdue&ordering=-issued") == [
        5,
        3,
        1,
    ]
    assert await refused(worker, "/mine?owner=bob")


async def test_the_hook_runs_after_the_client_filters_and_gets_typed_params(
    worker: KisoWorker,
) -> None:
    Hooked.seen.clear()
    assert await ids(worker, "/hooked?status=open") == [1, 4]
    (params,) = Hooked.seen
    assert isinstance(params, InvoiceFilters)
    assert params.status == ["open"]
    assert params.paid is None


async def test_cursor_walk_keeps_filters_and_order(worker: KisoWorker) -> None:
    status, body = await get(
        worker.address[1], "/cursor?status=open,paid&ordering=-id&limit=2"
    )
    assert status == 200, body
    first = core.decode_json(body)
    assert [item["id"] for item in first["items"]] == [6, 5]
    follow = f"/cursor?status=open,paid&ordering=-id&limit=2&cursor={first['next']}"
    status, body = await get(worker.address[1], follow)
    assert status == 200, body
    assert [item["id"] for item in core.decode_json(body)["items"]] == [4, 2]


@pytest.mark.parametrize(
    "changed", ["status=open", "status=open,paid&ordering=id", "ordering=-id"]
)
async def test_a_cursor_is_bound_to_its_filters_and_ordering(
    worker: KisoWorker, changed: str
) -> None:
    _, body = await get(
        worker.address[1], "/cursor?status=open,paid&ordering=-id&limit=2"
    )
    token = core.decode_json(body)["next"]
    status, answer = await get(
        worker.address[1], f"/cursor?{changed}&limit=2&cursor={token}"
    )
    assert status == 422, answer


def test_the_list_documents_typed_parameters() -> None:
    for version in ("3.1", "3.2"):
        document = Hinoki(openapi_version=version, include=[Invoices, Cursor]).openapi()
        cursored = document["paths"]["/cursor"]["get"]["parameters"]
        by_name = {param["name"]: param for param in cursored}
        assert by_name["status"]["schema"]["items"]["maxLength"] == 256
        assert {"cursor", "limit"} <= set(by_name)
        operation = document["paths"]["/invoices"]["get"]
        found = {param["name"]: param for param in operation["parameters"]}
        assert found["paid"]["schema"] == {"type": "boolean"}
        assert found["status"]["schema"]["items"]["enum"] == ["open", "paid", "overdue"]
        assert found["status"]["explode"] is False
        assert found["issued.from"]["schema"]["format"] == "date"
        assert found["issued.to"]["schema"]["format"] == "date"
        assert found["discount.null"]["schema"] == {"type": "boolean"}
        assert found["search"]["schema"]["maxLength"] == 128
        assert found["ordering"]["schema"]["items"]["enum"] == [
            "issued",
            "reference",
            "-issued",
            "-reference",
        ]
        assert {"page", "page_size"} <= set(found)
        assert "422" in operation["responses"]


def test_filters_are_checked_when_the_routes_are_declared() -> None:
    class Unknown(FilterSet):
        nothing: Eq[int]

    class Hidden(FilterSet):
        ordering = ("customer_name", "missing")

    cases: list[dict[str, Any]] = [
        {"filters": Unknown},
        {"filters": Hidden},
        {"filters": InvoiceFilters, "omit": ("list",)},
        {"filters": object},
    ]
    for changes in cases:
        with pytest.raises(RegistrationError):
            crud(Invoice, path="/x", read=InvoiceOut, **changes).api()


@pytest.mark.parametrize(
    ("field_name", "pagination"),
    [
        ("page", PAGES),
        ("page_size", PAGES),
        ("cursor", CURSORS),
        ("limit", CURSORS),
    ],
)
def test_a_filter_name_cannot_shadow_a_pagination_parameter(
    field_name: str, pagination: PageNumberPagination | CursorPagination
) -> None:
    colliding = declared(__annotations__={field_name: Eq[int]})
    with pytest.raises(RegistrationError):
        crud(
            Invoice,
            path="/x",
            read=InvoiceOut,
            filters=colliding,
            pagination=pagination,
        ).api()


def test_a_manual_route_also_rejects_a_filter_name_shadowing_pagination() -> None:
    class PageField(FilterSet):
        page: Eq[int]

    class Manual(API):
        @http.get("/manual", pagination=PAGES)
        async def manual(
            self, page_params: PageParams, filters: PageField
        ) -> dict[str, Any]:
            return {"page": filters.page}

    with pytest.raises(RegistrationError):
        Hinoki(include=[Manual])


def test_a_filter_set_parses_and_validates_on_its_own() -> None:
    parsed = InvoiceFilters.parse({"paid": ["true"], "issued.from": ["2024-01-01"]})
    assert parsed.paid is True
    assert parsed.issued is not None
    assert parsed.issued.from_ == date(2024, 1, 1)
    with pytest.raises(ValidationError):
        InvoiceFilters.parse({"paid": ["x"]})
    with pytest.raises(ValidationError):
        InvoiceFilters.parse({"unlisted": ["1"]})


def declared(**members: Any) -> type[FilterSet]:
    return type("Declared", (FilterSet,), members)


@pytest.mark.parametrize(
    "members",
    [
        {},
        {"__annotations__": {"x": "Missing[int]"}},
        {"__annotations__": {"x": Eq[bytes]}},
        {"__annotations__": {"x": list[int]}},
        {"__annotations__": {"x": Eq[int | str]}},
        {"__annotations__": {"x": Eq[Literal[1, 2]]}},
        {"__annotations__": {"x": Range[bool]}},
        {"__annotations__": {"x": Range[Literal["a"]]}},
        {"__annotations__": {"scope": Eq[int]}},
        {"search": "reference", "ordering": ("issued",)},
        {"search": Search(), "ordering": ("issued",)},
        {"ordering": "issued"},
        {"ordering": ("issued", "issued")},
    ],
)
def test_malformed_filter_sets_fail_at_declaration(members: dict[str, Any]) -> None:
    with pytest.raises(RegistrationError):
        declared(**members)


@pytest.mark.parametrize(
    ("members", "cursor"),
    [
        ({"__annotations__": {"reference": Eq[int]}}, False),
        ({"__annotations__": {"reference": Eq[str | None]}}, False),
        ({"ordering": ("missing",)}, False),
        ({"ordering": ("discount",)}, True),
        ({"search": Search("paid")}, False),
    ],
)
def test_filter_sets_must_fit_the_model(members: dict[str, Any], cursor: bool) -> None:
    with pytest.raises(RegistrationError):
        declared(**members).check(Invoice, cursor=cursor)


def test_a_null_filter_reports_what_the_client_sent() -> None:
    nulls = InvoiceFilters.parse({"discount.null": ["true"]})
    assert nulls.is_null("discount") is True
    assert InvoiceFilters.parse({}).is_null("discount") is None


def test_a_filter_set_belongs_to_one_get_list_and_owns_the_query_string() -> None:
    class Posting(API):
        @http.post("/x")
        async def post(self, filters: InvoiceFilters) -> dict[str, str]:
            return {}

    class Twice(API):
        @http.get("/y")
        async def twice(
            self, one: InvoiceFilters, other: InvoiceFilters
        ) -> dict[str, str]:
            return {}

    class Mixed(API):
        @http.get("/z")
        async def mixed(self, filters: InvoiceFilters, extra: int) -> dict[str, str]:
            return {}

    for api in (Posting, Twice, Mixed):
        with pytest.raises(RegistrationError):
            Hinoki(include=[api])


async def test_a_manual_list_can_take_the_filters_too() -> None:
    class Manual(API):
        @http.get("/manual", pagination=PAGES)
        async def manual(
            self, page: PageParams, filters: InvoiceFilters
        ) -> dict[str, Any]:
            return {"paid": filters.paid}

    document = Hinoki(include=[Manual]).openapi()
    names = {p["name"] for p in document["paths"]["/manual"]["get"]["parameters"]}
    assert {"paid", "issued.from", "page"} <= names
