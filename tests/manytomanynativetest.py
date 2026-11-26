"""Generated CRUD with ``*_ids`` fields on the native lane answers exactly like
the Python lane: reads, writes, refusals, per backend."""

import asyncio
import os
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID, uuid4

import pytest

from hinoki import (
    UNSET,
    Access,
    Caller,
    Field,
    Hinoki,
    Schema,
    Unset,
    all_rows,
    core,
    crud,
    own_rows,
)
from hinoki.crypto import ClaimsJWTAuth, JWTKey
from hinoki.db import Database, DatabaseModule
from hinoki.errors import Forbidden, RegistrationError
from hinoki.events import EventBus, Handlers, Linked
from hinoki.orm import (
    ManyToMany,
    Model,
    QuerySet,
    field,
    many_to_many,
    migrator,
    use_database,
)
from hinoki.orm.migrations import plan
from hinoki.resources import CrudAccess, CrudBase, caller_id
from hinoki.runtime.crudplan import (
    action_schemas,
    link_events_listened,
    native_links,
    schemas_are_plain,
)
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.rowplan import schema_fields
from hinoki.settings import AppSettings, EventsSettings, ServerSettings

# Fixtures declare owner= resources on purpose: the owner read-scope warning is
# not what these tests check.
OWNER_READS = r"\w+. owner=.* constrains writes only"
pytestmark = pytest.mark.filterwarnings(f"ignore:{OWNER_READS}:UserWarning")
warnings.filterwarnings(
    "ignore", message=OWNER_READS, category=UserWarning, module=__name__
)

SECRET = b"n" * 32
URLS = {
    "postgres": os.environ.get("HINOKI_TEST_DATABASE_URL"),
    "mysql": os.environ.get("HINOKI_TEST_MYSQL_URL"),
    "mariadb": os.environ.get("HINOKI_TEST_MARIADB_URL"),
}
RED, BLUE, OTHERS = (UUID(int=n) for n in (1, 2, 3))
GHOST = UUID(int=99)


class Tokens(ClaimsJWTAuth):
    keys = (JWTKey(None, "HS256", SECRET),)
    issuer = "https://issuer.example"
    audience = "api"
    leeway = 5


class NTag(Model, table="m2mn_tags"):
    id: UUID = field(primary_key=True)
    owner: str = field(max_length=20)
    name: str = field(max_length=40)


class NPost(Model, table="m2mn_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner: str
    title: str
    tags: ClassVar[ManyToMany[NTag]] = many_to_many(
        NTag, ids_name="tag_ids", max_items=3
    )


class NLabel(Model, table="m2mn_labels"):
    id: str = field(primary_key=True, max_length=20)


class NNote(Model, table="m2mn_notes"):
    id: int = field(primary_key=True)
    title: str
    labels: ClassVar[ManyToMany[NLabel]] = many_to_many(
        NLabel, ids_name="label_ids", max_items=3
    )


MODELS: list[type[Model]] = [NTag, NPost, NLabel, NNote, *NPost.__bridges__]
MODELS += list(NNote.__bridges__)
TABLES = [
    "m2mn_posts_tags",
    "m2mn_notes_labels",
    "m2mn_posts",
    "m2mn_notes",
    "m2mn_tags",
    "m2mn_labels",
    "hinoki_migrations",
]
TagIds = Annotated[set[UUID], Field(max_length=3)]
LabelIds = Annotated[set[str], Field(max_length=3)]


class PostOut(Schema):
    id: UUID
    title: str
    tag_ids: TagIds


class PostIn(Schema):
    title: str
    tag_ids: TagIds


class PostPatch(Schema):
    title: str | Unset = UNSET
    tag_ids: TagIds | Unset = UNSET


class NoteOut(Schema):
    id: int
    title: str
    label_ids: LabelIds


class NoteIn(Schema):
    id: int
    title: str
    label_ids: LabelIds


class NotePatch(Schema):
    title: str | Unset = UNSET
    label_ids: LabelIds | Unset = UNSET


signed = Access(Tokens(), permissions=["posts"])
Posts = crud(
    NPost,
    path="/posts",
    read=PostOut,
    create=PostIn,
    update=PostPatch,
    delete=True,
    access=CrudAccess(default=signed, list=signed, retrieve=signed),
    owner="owner",
    identity_key=caller_id,
    links={"tags": own_rows(NTag, "owner")},
).api()
Notes = crud(
    NNote,
    path="/notes",
    read=NoteOut,
    create=NoteIn,
    update=NotePatch,
    delete=True,
    public_writes=True,
    links={"labels": all_rows(NLabel)},
).api()


def token(subject: str) -> str:
    now = int(time.time())
    claims = {
        "sub": subject,
        "scopes": ["posts"],
        "iss": Tokens.issuer,
        "aud": Tokens.audience,
        "iat": now,
        "exp": now + 600,
    }
    return core.jwt_encode(core.encode_json(claims), (None, "HS256", SECRET, False))


async def exchange(
    port: int, method: str, target: str, body: Any = None, who: str | None = None
) -> tuple[int, Any]:
    raw = core.encode_json(body) if body is not None else b""
    auth = f"authorization: Bearer {token(who)}\r\n" if who else ""
    head = (
        f"{method} {target} HTTP/1.1\r\nhost: test\r\n"
        "x-request-id: 00000000000000000000000000000001\r\n"
        f"content-type: application/json\r\ncontent-length: {len(raw)}\r\n{auth}\r\n"
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode("latin-1") + raw)
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
    return int(lines[0].split()[1]), core.decode_json(payload) if payload else None


def masked(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<masked>"
            if key == "id" and not isinstance(item, int)
            else masked(item)
            for key, item in value.items()
            if key != "request_id"
        } | (
            {"items": sorted(map(repr, masked(value["items"])))}
            if "items" in value
            else {}
        )
    if isinstance(value, list):
        return [masked(item) for item in value]
    return value


@pytest.fixture(params=["sqlite", "postgres", "mysql", "mariadb"])
async def served(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[KisoWorker, Database]]:
    kind = request.param
    url = f"sqlite://{tmp_path / 'native.sqlite3'}" if kind == "sqlite" else URLS[kind]
    if url is None:
        pytest.skip(f"the {kind} test database URL is not set")
    connection = await Database.connect(url, max_size=2, wait=0.5)
    await drop_tables(connection)
    plan(MODELS, tmp_path, "initial", dialect=connection.dialect)
    await migrator.apply(connection, tmp_path)
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[Posts, Notes],
        modules=[DatabaseModule(url, max_size=3)],
    )
    started = KisoWorker(app, "127.0.0.1", 0)
    await started.start()
    monkeypatch.setattr(started, "_check_crud_snapshots", lambda: None)
    with use_database(connection):
        yield started, connection
    await started.stop()
    await drop_tables(connection)
    connection.close()


async def drop_tables(connection: Database) -> None:
    quote = "`" if connection.dialect in {"mysql", "mariadb"} else '"'
    for table in TABLES:
        await connection.execute(f"DROP TABLE IF EXISTS {quote}{table}{quote}")


async def reseed(connection: Database) -> None:
    quote = "`" if connection.dialect in {"mysql", "mariadb"} else '"'
    for table in TABLES[:-1]:
        await connection.execute(f"DELETE FROM {quote}{table}{quote}")  # noqa: S608 - fixed test tables
    await NTag.acreate(id=RED, owner="ann", name="red")
    await NTag.acreate(id=BLUE, owner="ann", name="blue")
    await NTag.acreate(id=OTHERS, owner="bob", name="bob's")
    for label in ("red", "green", "blue", "x" * 20):
        await NLabel.acreate(id=label)


def selected(started: KisoWorker, path: str) -> set[str]:
    return {
        selection.action
        for selection in started.pipeline.crud_plan_selections
        if selection is not None and selection.binding.route.path.startswith(path)
    }


Scenario = Callable[[int], Awaitable[list[Any]]]


async def posts_scenario(port: int) -> list[Any]:
    seen: list[Any] = []

    async def call(
        method: str, target: str, body: Any = None, who: str | None = "ann"
    ) -> Any:
        status, reply = await exchange(port, method, target, body, who)
        seen.append((status, masked(reply)))
        return reply

    first = await call(
        "POST", "/posts", {"title": "a", "tag_ids": [str(RED), str(BLUE)]}
    )
    await call("POST", "/posts", {"title": "dup", "tag_ids": [str(RED), str(RED)]})
    await call("POST", "/posts", {"title": "none"})
    await call("POST", "/posts", {"title": "b", "tag_ids": [str(OTHERS)]})
    await call("POST", "/posts", {"title": "b", "tag_ids": [str(RED), str(GHOST)]})
    await call(
        "POST", "/posts", {"title": "b", "tag_ids": [str(RED)] * 2 + [str(BLUE)]}
    )
    await call(
        "POST",
        "/posts",
        {"title": "b", "tag_ids": [str(UUID(int=n)) for n in (1, 2, 3, 4)]},
    )
    await call("POST", "/posts", {"title": "b", "tag_ids": ["nope"]})
    await call("POST", "/posts", {"title": "b", "tag_ids": None})
    await call("POST", "/posts", {"title": "b", "tag_ids": "x"})
    await call("POST", "/posts", {"title": "b", "tag_ids": [1]})
    path = f"/posts/{first['id']}"
    await call("GET", path)
    await call("GET", path, who="bob")
    await call("GET", "/posts?page_size=100")
    await call("GET", "/posts?page=2&page_size=100", who="bob")
    await call("GET", path, who=None)
    await call("PATCH", path, {"tag_ids": [str(BLUE)]})
    await call("PATCH", path, {"title": "renamed"})
    await call("PATCH", path, {})
    await call("PATCH", path, {"tag_ids": [str(OTHERS)]})
    await call("PATCH", path, {"title": "never", "tag_ids": [str(GHOST)]})
    await call("PATCH", path, {"title": "stolen"}, who="bob")
    await call("PATCH", path, {"tag_ids": [str(RED)]}, who="bob")
    await call("PATCH", f"/posts/{GHOST}", {"tag_ids": []})
    await call("PATCH", path, {"tag_ids": []})
    await call("GET", path)
    await call("DELETE", path, who="bob")
    await call("DELETE", path)
    await call("DELETE", path)
    await call("GET", "/posts?page_size=100")
    seen.append(await NPost.all().acount())
    seen.append(await NPost.__bridges__[0].all().acount())
    return seen


async def notes_scenario(port: int) -> list[Any]:
    seen: list[Any] = []

    async def call(method: str, target: str, body: Any = None) -> None:
        status, reply = await exchange(port, method, target, body)
        seen.append((status, masked(reply)))

    await call("POST", "/notes", {"id": 1, "title": "a", "label_ids": ["red", "green"]})
    await call("POST", "/notes", {"id": 2, "title": "b", "label_ids": []})
    await call("POST", "/notes", {"id": 3, "title": "c", "label_ids": ["red", "red"]})
    await call("POST", "/notes", {"id": 4, "title": "d", "label_ids": ["nope"]})
    await call("POST", "/notes", {"id": 4, "title": "d", "label_ids": ["x" * 21]})
    await call("POST", "/notes", {"id": 4, "title": "d", "label_ids": [1]})
    await call("POST", "/notes", {"id": 4, "title": "d", "label_ids": list("abcd")})
    await call("POST", "/notes", {"id": 1, "title": "d", "label_ids": ["red"]})
    await call("POST", "/notes", {"id": 5, "title": "e", "label_ids": ["x" * 20]})
    await call("GET", "/notes/1")
    await call("GET", "/notes/2")
    await call("GET", "/notes?page_size=100")
    await call("PATCH", "/notes/1", {"label_ids": ["blue"]})
    await call("PATCH", "/notes/1", {"label_ids": ["blue", "nope"]})
    await call("PATCH", "/notes/2", {"title": "bb", "label_ids": ["green", "blue"]})
    await call("PATCH", "/notes/99", {"label_ids": []})
    await call("PATCH", "/notes/1", {"label_ids": []})
    await call("DELETE", "/notes/2")
    await call("GET", "/notes?page_size=100")
    seen.append(await NNote.all().acount())
    seen.append(await NNote.__bridges__[0].all().acount())
    return seen


@pytest.mark.parametrize(
    ("path", "scenario"), [("/posts", posts_scenario), ("/notes", notes_scenario)]
)
async def test_the_native_lane_answers_like_the_python_lane(
    served: tuple[KisoWorker, Database],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    scenario: Scenario,
) -> None:
    started, connection = served
    assert selected(started, path) == {"list", "retrieve", "create", "update", "delete"}

    fallbacks: list[str] = []

    def watching(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            fallbacks.append(name)
            return original(*args, **kwargs)

        return wrapper

    for name in ("link_queryset", "to_schema", "get_queryset"):
        monkeypatch.setattr(CrudBase, name, watching(name, getattr(CrudBase, name)))
    port = started.address[1]
    await reseed(connection)
    native = await scenario(port)
    # Only the requests Rust hands over (a key it cannot read) reach Python.
    assert len(fallbacks) <= 6, fallbacks
    monkeypatch.undo()
    monkeypatch.setattr(started, "_check_crud_snapshots", lambda: None)
    started.core._disarm_crud_plans()
    await reseed(connection)
    python = await scenario(port)
    assert native == python
    statuses = {item[0] for item in native if isinstance(item, tuple)}
    assert {200, 201, 204, 403 if path == "/posts" else 404, 422} <= statuses


def test_a_link_event_handler_keeps_the_route_on_the_python_lane() -> None:
    handlers = Handlers()

    @handlers.after_commit(Linked[NPost])
    async def linked(event: Linked[NPost]) -> None:
        return None

    bus = EventBus([handlers], EventsSettings())
    assert link_events_listened(bus, NPost, Posts.resource)
    assert not link_events_listened(
        EventBus([], EventsSettings()), NPost, Posts.resource
    )


def test_own_rows_needs_a_text_column_and_access() -> None:
    with pytest.raises(RegistrationError, match="text column"):
        crud(
            NPost,
            path="/p",
            read=PostOut,
            access=signed,
            links={"tags": own_rows(NTag, "id")},
        ).api()
    with pytest.raises(RegistrationError, match="access"):
        crud(
            NPost, path="/p", read=PostOut, links={"tags": own_rows(NTag, "owner")}
        ).api()


def test_scopes_and_schemas_rust_cannot_apply_stay_on_the_python_lane() -> None:
    custom = crud(
        NPost,
        path="/custom",
        read=PostOut,
        create=PostIn,
        update=PostPatch,
        access=signed,
        owner="owner",
        identity_key=caller_id,
        links={"tags": lambda user: NTag.filter(owner=user)},
    ).api()
    assert native_links(custom.resource) is None
    assert not schemas_are_plain(
        action_schemas(custom.resource), NPost, custom.resource
    )

    class NullablePostOut(Schema):
        id: UUID
        tag_ids: TagIds | None

    assert schema_fields(NPost, NullablePostOut, frozenset({"tag_ids"})) is None
    assert schema_fields(NPost, PostOut, frozenset({"tag_ids"})) is not None


async def test_link_scopes_need_a_caller_to_be_owned() -> None:
    resource = Posts.resource
    assert resource.link_fields(None) == []
    assert len(resource.link_fields(PostOut)) == 1
    with pytest.raises(Forbidden):
        Posts().link_queryset("tags", None)
    scoped = Posts().link_queryset("tags", Caller(id="ann", user="ann"))
    assert isinstance(scoped, QuerySet)
    assert scoped.filters == NTag.filter(owner="ann").filters


class MinPostOut(Schema):
    id: UUID
    tag_ids: Annotated[set[UUID], Field(min_length=1, max_length=3)]


class NarrowPostOut(Schema):
    id: UUID
    tag_ids: Annotated[set[UUID], Field(max_length=2)]


class ItemRulePostOut(Schema):
    id: int
    label_ids: Annotated[set[Annotated[str, Field(upper=True)]], Field(max_length=3)]


class Shade(StrEnum):
    RED = "red"


class EnumNoteOut(Schema):
    id: int
    label_ids: Annotated[set[Shade], Field(max_length=3)]


class LiteralNoteOut(Schema):
    id: int
    label_ids: Annotated[set[Literal["red", "blue"]], Field(max_length=3)]


@pytest.mark.parametrize(
    ("model", "schema", "name"),
    [
        (NPost, MinPostOut, "tag_ids"),
        (NPost, NarrowPostOut, "tag_ids"),
        (NNote, ItemRulePostOut, "label_ids"),
        (NNote, EnumNoteOut, "label_ids"),
        (NNote, LiteralNoteOut, "label_ids"),
    ],
)
def test_ids_fields_with_rules_rust_does_not_apply_stay_on_the_python_lane(
    model: type[Model], schema: type[Schema], name: str
) -> None:
    if schema is not NarrowPostOut:
        assert schema_fields(model, schema, frozenset({name})) is None
    if model is NPost:
        built = crud(
            NPost,
            path="/rules",
            read=schema,
            access=signed,
            owner="owner",
            identity_key=caller_id,
            links={"tags": own_rows(NTag, "owner")},
        ).api()
    else:
        built = crud(
            NNote,
            path="/rules",
            read=schema,
            access=signed,
            links={"labels": all_rows(NLabel)},
        ).api()
    assert not schemas_are_plain(action_schemas(built.resource), model, built.resource)


async def test_stored_links_over_the_limit_are_a_server_error_on_both_lanes(
    served: tuple[KisoWorker, Database],
) -> None:
    started, connection = served
    await reseed(connection)
    await NTag.acreate(id=UUID(int=4), owner="ann", name="extra")
    post = await NPost.acreate(owner="ann", title="full")
    bridge = NPost.__bridges__[0]
    near, far = (c for c in bridge.__columns__ if c.endswith("_id"))
    await bridge.abulk_create(
        [{near: post.id, far: UUID(int=n)} for n in (1, 2, 4)]
        + [{near: post.id, far: UUID(int=3)}],
        returning=False,
    )
    await NTag.filter(id=OTHERS).aupdate({"owner": "ann"}, returning=False)
    port = started.address[1]
    assert selected(started, "/posts") >= {"list", "retrieve"}
    native = [
        (await exchange(port, "GET", target, who="ann"))[0]
        for target in (f"/posts/{post.id}", "/posts")
    ]
    started.core._disarm_crud_plans()
    python = [
        (await exchange(port, "GET", target, who="ann"))[0]
        for target in (f"/posts/{post.id}", "/posts")
    ]
    assert native == python == [500, 500]


@pytest.mark.parametrize(("mode", "status"), [("handover", 403), ("delete", 404)])
async def test_a_row_taken_away_while_the_links_wait_is_refused_on_both_lanes(
    served: tuple[KisoWorker, Database], mode: str, status: int
) -> None:
    started, connection = served
    if connection.dialect == "sqlite":
        pytest.skip("SQLite has a single writer: nobody can write in the window")
    url = URLS[connection.dialect]
    port = started.address[1]
    seen: list[int] = []
    for native in (True, False):
        if not native:
            started.core._disarm_crud_plans()
        await reseed(connection)
        post = await NPost.acreate(owner="ann", title="t")
        assert url is not None
        other = await Database.connect(url, max_size=1, wait=0.5)
        try:
            async with other.atomic():
                with use_database(other):
                    await NPost.filter(id=post.id).aupdate(
                        {"title": "held"}, returning=False
                    )
                    patch = asyncio.create_task(
                        exchange(
                            port,
                            "PATCH",
                            f"/posts/{post.id}",
                            {"tag_ids": [str(RED)]},
                            "ann",
                        )
                    )
                    await asyncio.sleep(0.4)
                    if mode == "delete":
                        await NPost.filter(id=post.id).adelete()
                    else:
                        await NPost.filter(id=post.id).aupdate(
                            {"owner": "bob"}, returning=False
                        )
            seen.append((await asyncio.wait_for(patch, 10))[0])
        finally:
            other.close()
    assert seen == [status, status]
