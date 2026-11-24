"""Keyset (cursor) pagination: generated lists, manual routes and bad cursors."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hinoki import API, Hinoki, Schema, context, core, crud, http
from hinoki.db import Database, DatabaseModule
from hinoki.errors import FieldError, RegistrationError, ValidationError
from hinoki.orm import Model, QuerySet, field, ordered_by, use_database
from hinoki.orm.keyset import cursor_order
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.pagination import CursorPage, CursorPagination, CursorParams, PageParams
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings, PaginationSettings, Secret, ServerSettings

SECRET = "k" * 40
STRATEGY = CursorPagination(page_size=3, max_page_size=4, secret=SECRET)
Response = tuple[int, bytes]


@ordered_by("-rank", "name")
class Item(Model, table="cursor_items"):
    id: int = field(primary_key=True)
    rank: int
    name: str
    owner: str = ""


class ItemOut(Schema):
    id: int
    rank: int
    name: str
    owner: str


@crud(Item, path="/items", read=ItemOut, pagination=STRATEGY)
class Items(API):
    pass


@crud(Item, path="/mine", read=ItemOut, pagination=STRATEGY)
class Mine(API):
    def get_queryset(self, principal: Any) -> QuerySet[Item]:
        return Item.all().filter(owner="ann")


@crud(
    Item,
    path="/short",
    read=ItemOut,
    pagination=CursorPagination(page_size=2, max_page_size=4, secret=SECRET, ttl=1),
)
class Short(API):
    pass


class Manual(API):
    @http.get("/manual", pagination=STRATEGY)
    async def manual(self, page: CursorParams) -> CursorPage[ItemOut]:
        found = await Item.all().order_by("id").apaginate_cursor(page)
        return found.map(
            lambda row: ItemOut(
                id=row.id, rank=row.rank, name=row.name, owner=row.owner
            )
        )


RANKS = [3, 1, 3, 2, 2, 3, 1, 2, 1, 1]
NAMES = ["d", "a", "c", "b", "e", "a", "b", "c", "d", "e"]
OWNERS = ["ann", "bob"] * 5
ROWS: list[dict[str, Any]] = [
    {"id": i + 1, "rank": RANKS[i], "name": NAMES[i], "owner": OWNERS[i]}
    for i in range(10)
]
EXPECTED = sorted(ROWS, key=lambda r: (-r["rank"], r["name"], r["id"]))


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
    url = f"sqlite:///{tmp_path / 'cursor.db'}"
    database = await Database.connect(url, max_size=1, wait=1)
    await database.execute(
        create_table_sql(Item.__table__, table_state(Item), "sqlite")
    )
    for row in ROWS:
        await database.execute(
            "INSERT INTO cursor_items (id, rank, name, owner) VALUES (?, ?, ?, ?)",
            row["id"],
            row["rank"],
            row["name"],
            row["owner"],
        )
    database.close()
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[Items, Mine, Short, Manual],
        modules=[DatabaseModule(url)],
    )
    started = KisoWorker(app, "127.0.0.1", 0)
    await started.start()
    try:
        yield started
    finally:
        await started.stop()


async def page(started: KisoWorker, target: str) -> dict[str, Any]:
    status, body = await get(started.address[1], target)
    assert status == 200, body
    document: dict[str, Any] = core.decode_json(body)
    return document


async def walk(started: KisoWorker, path: str, link: str, start: str) -> list[dict]:  # type: ignore[type-arg]
    pages = []
    target: str | None = start
    while target is not None:
        found = await page(started, target)
        pages.append(found)
        token = found[link]
        target = None if token is None else f"{path}?cursor={token}&limit=3"
    return pages


def ids(found: dict[str, Any]) -> list[int]:
    return [item["id"] for item in found["items"]]


async def test_only_a_plain_public_list_takes_the_rust_wire_path(
    worker: KisoWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    original = Database._fetch_cursor_wire

    async def counted(self: Database, *args: Any) -> Any:
        calls.append(1)
        return await original(self, *args)

    monkeypatch.setattr(Database, "_fetch_cursor_wire", counted)
    await page(worker, "/items?limit=2")
    assert calls == [1]
    await page(worker, "/mine?limit=2")
    assert calls == [1]


async def test_forward_walk_visits_every_row_once_in_order(
    worker: KisoWorker,
) -> None:
    pages = await walk(worker, "/items", "next", "/items")
    assert [len(p["items"]) for p in pages] == [3, 3, 3, 1]
    assert [i for p in pages for i in ids(p)] == [r["id"] for r in EXPECTED]
    assert pages[0]["previous"] is None
    assert pages[-1]["next"] is None
    assert all(p["page_size"] == 3 for p in pages)


async def test_previous_walks_back_to_the_same_pages(worker: KisoWorker) -> None:
    forward = await walk(worker, "/items", "next", "/items")
    last = forward[-1]
    backward = []
    found = last
    while found["previous"] is not None:
        found = await page(worker, f"/items?cursor={found['previous']}&limit=3")
        backward.append(found)
    assert [ids(p) for p in backward] == [ids(p) for p in reversed(forward[:-1])]
    assert backward[-1]["previous"] is None
    assert backward[0]["next"] is not None


async def test_the_wire_page_matches_the_python_lane(worker: KisoWorker) -> None:
    status, wire = await get(worker.address[1], "/items?limit=4")
    other, plain = await get(worker.address[1], "/short?limit=4")
    assert status == other == 200
    assert core.decode_json(wire)["items"] == core.decode_json(plain)["items"]
    scoped = await page(worker, "/mine?limit=4")
    assert {item["owner"] for item in scoped["items"]} == {"ann"}


async def test_a_cursor_never_widens_the_scope_of_the_query(
    worker: KisoWorker,
) -> None:
    first = await page(worker, "/items?limit=2")
    token = first["next"]
    mine = await page(worker, f"/mine?cursor={token}&limit=4")
    assert {item["owner"] for item in mine["items"]} == {"ann"}
    after = [r["id"] for r in EXPECTED][2:]
    assert ids(mine) == [i for i in after if i % 2 == 1][:4]


@pytest.mark.parametrize("limit", ["0", "5", "x", "-1"])
async def test_a_bad_limit_is_a_422(worker: KisoWorker, limit: str) -> None:
    status, _ = await get(worker.address[1], f"/items?limit={limit}")
    assert status == 422


async def test_tampered_foreign_or_malformed_cursors_are_422_not_500(
    worker: KisoWorker,
) -> None:
    token = (await page(worker, "/items?limit=2"))["next"]
    flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
    manual = (await page(worker, "/manual?limit=2"))["next"]
    for bad in (flipped, token[:-4], "!!", "A" * 2000, manual, "%00"):
        status, body = await get(worker.address[1], f"/items?cursor={bad}")
        assert status == 422, (bad, body)
        detail = core.decode_json(body)
        assert "cursor" in str(detail)


async def test_a_cursor_for_another_ordering_is_refused(worker: KisoWorker) -> None:
    token = (await page(worker, "/manual?limit=2"))["next"]
    status, _ = await get(worker.address[1], f"/items?cursor={token}")
    assert status == 422


async def test_an_expired_cursor_is_a_422(worker: KisoWorker) -> None:
    token = (await page(worker, "/short"))["next"]
    assert (await get(worker.address[1], f"/short?cursor={token}"))[0] == 200
    await asyncio.sleep(2.2)
    status, body = await get(worker.address[1], f"/short?cursor={token}")
    assert status == 422
    assert b"expired" in body


async def test_a_manual_route_pages_with_the_same_contract(
    worker: KisoWorker,
) -> None:
    pages = await walk(worker, "/manual", "next", "/manual?limit=3")
    assert [i for p in pages for i in ids(p)] == list(range(1, 11))


def test_openapi_documents_the_cursor_contract() -> None:
    document = Hinoki(include=[Items]).openapi()
    operation = document["paths"]["/items"]["get"]
    names = {p["name"]: p for p in operation["parameters"]}
    assert set(names) == {"cursor", "limit"}
    assert names["limit"]["schema"]["maximum"] == 4
    schema = document["components"]["schemas"]["CursorPage_ItemOut"]
    assert set(schema["properties"]) == {"items", "next", "previous", "page_size"}


def test_a_cursor_strategy_without_a_secret_fails_at_registration() -> None:
    class Open(API):
        @http.get("/open", pagination=CursorPagination())
        async def listing(self, page: CursorParams) -> CursorPage[ItemOut]:
            raise NotImplementedError

    with pytest.raises(RegistrationError, match="secret"):
        Hinoki(include=[Open])


def test_the_secret_comes_from_the_pagination_settings() -> None:
    class Open(API):
        @http.get("/open", pagination=CursorPagination())
        async def listing(self, page: CursorParams) -> CursorPage[ItemOut]:
            raise NotImplementedError

    settings = AppSettings(
        pagination=PaginationSettings(cursor_secret=Secret(SECRET), page_size=7)
    )
    app = Hinoki(settings=settings, include=[Open])
    assert app.routes[0].pagination == CursorPagination(7, 100, SECRET, None)


def test_the_page_params_type_must_match_the_strategy() -> None:
    class Wrong(API):
        @http.get("/wrong", pagination=STRATEGY)
        async def listing(self, page: PageParams) -> CursorPage[ItemOut]:
            raise NotImplementedError

    with pytest.raises(RegistrationError, match="PageNumberPagination"):
        Hinoki(include=[Wrong])


def test_short_secrets_are_refused() -> None:
    with pytest.raises(ValueError, match="32"):
        CursorPagination(secret="short")  # noqa: S106
    with pytest.raises(ValueError, match="32"):
        PaginationSettings(cursor_secret=Secret("short")).validate()


def test_a_cursor_lifetime_must_be_positive() -> None:
    with pytest.raises(ValueError, match="ttl"):
        CursorPagination(ttl=0)
    with pytest.raises(ValueError, match="cursor_ttl_seconds"):
        PaginationSettings(cursor_ttl_seconds=0).validate()


def test_an_unresolved_cursor_strategy_cannot_make_a_codec() -> None:
    with pytest.raises(ValueError, match="no secret"):
        CursorPagination(page_size=2).codec(b"fingerprint")


class Event(Model, table="cursor_events"):
    id: uuid.UUID = field(primary_key=True)
    at: datetime
    note: str | None = None


async def test_timestamp_and_uuid_keys_walk_without_gaps(tmp_path: Path) -> None:
    database = await Database.connect(
        f"sqlite:///{tmp_path / 'events.db'}", max_size=1, wait=1
    )
    await database.execute(
        create_table_sql(Event.__table__, table_state(Event), "sqlite")
    )
    start = datetime(2024, 1, 1, tzinfo=UTC)
    ids = sorted(uuid.uuid4() for _ in range(7))
    with use_database(database):
        for index, key in enumerate(ids):
            # Pairs share a timestamp, so the uuid tie-break decides.
            await Event(id=key, at=start + timedelta(hours=index // 2)).asave()
        seen: list[uuid.UUID] = []
        token = None
        while True:
            page = (
                await Event.all()
                .order_by("at")
                .apaginate_cursor(CursorParams(3, token, STRATEGY))
            )
            seen += [event.id for event in page.items]
            token = page.next
            if token is None:
                break
    assert seen == ids
    database.close()


def test_a_nullable_column_cannot_order_a_cursor() -> None:
    with pytest.raises(FieldError, match="note"):
        cursor_order(Event.all().order_by("note"))


@ordered_by("note")
class Note(Model, table="cursor_notes"):
    id: int = field(primary_key=True)
    note: str | None = None


class NoteOut(Schema):
    id: int


def test_a_crud_ordered_by_a_nullable_column_fails_at_registration() -> None:
    with pytest.raises(RegistrationError, match=r"Note.*note"):

        @crud(Note, path="/notes", read=NoteOut, pagination=STRATEGY)
        class Notes(API):
            pass


class Doc(Model, table="cursor_docs"):
    id: int = field(primary_key=True)
    title: str


async def docs(tmp_path: Path, titles: list[str]) -> Database:
    database = await Database.connect(
        f"sqlite:///{tmp_path / 'docs.db'}", max_size=1, wait=1
    )
    await database.execute(create_table_sql(Doc.__table__, table_state(Doc), "sqlite"))
    for number, title in enumerate(titles, 1):
        await database.execute(
            "INSERT INTO cursor_docs (id, title) VALUES (?, ?)", number, title
        )
    return database


async def read(limit: int, token: str | None) -> CursorPage[Doc]:
    params = CursorParams(limit, token, STRATEGY)
    return await Doc.all().order_by("title").apaginate_cursor(params)


async def test_long_text_keys_page_and_every_issued_token_decodes(
    tmp_path: Path,
) -> None:
    titles = [f"{letter}{'x' * 999}" for letter in "abcde"]
    database = await docs(tmp_path, titles)
    with use_database(database):
        seen: list[str] = []
        max_length = 0
        token = None
        while True:
            found = await read(2, token)
            seen += [doc.title for doc in found.items]
            for link in (found.next, found.previous):
                assert link is None or len(link) <= 4096
                if link is not None:
                    max_length = max(max_length, len(link))
                    assert STRATEGY.parse({"cursor": [link]}).token == link
            token = found.next
            if token is None:
                break
    assert seen == titles
    assert max_length > 1024
    database.close()


async def test_an_unencodable_edge_only_matters_when_its_link_is_needed(
    tmp_path: Path,
) -> None:
    titles = ["a", "b", "c" * 2000]
    database = await docs(tmp_path, titles)
    with use_database(database):
        everything = await read(5, None)
        assert [doc.title for doc in everything.items] == titles
        assert everything.next is None and everything.previous is None
        first = await read(2, None)
        assert first.next is not None and first.previous is None
        with pytest.raises(ValidationError) as caught:
            await read(1, first.next)
    assert caught.value.details[0].path == "cursor"
    assert caught.value.details[0].code == "unencodable"
    database.close()


async def test_a_manual_cursor_walks_back_with_the_previous_link(
    tmp_path: Path,
) -> None:
    database = await docs(tmp_path, [f"t{n}" for n in range(1, 6)])
    with use_database(database):
        first = await read(2, None)
        second = await read(2, first.next)
        third = await read(2, second.next)
        back = await read(2, third.previous)
        start = await read(2, back.previous)
    database.close()

    assert [d.title for d in back.items] == ["t3", "t4"]
    assert back.next is not None and back.previous is not None
    assert [d.title for d in start.items] == ["t1", "t2"]
    assert start.previous is None and start.next is not None


async def test_a_cursor_page_over_no_rows_has_no_links(tmp_path: Path) -> None:
    database = await docs(tmp_path, [])
    with use_database(database):
        empty = await read(2, None)
    database.close()

    assert (empty.items, empty.next, empty.previous) == ([], None, None)


async def test_a_sync_handler_can_page_by_cursor(tmp_path: Path) -> None:
    database = await docs(tmp_path, ["a", "b", "c"])
    token = context.event_loop.set(asyncio.get_running_loop())
    try:
        with use_database(database):
            found = await asyncio.to_thread(
                lambda: (
                    Doc.all()
                    .order_by("title")
                    .paginate_cursor(CursorParams(2, None, STRATEGY))
                )
            )
    finally:
        context.event_loop.reset(token)
    database.close()

    assert [d.title for d in found.items] == ["a", "b"]
    assert found.next is not None
