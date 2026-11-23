"""Generated CRUD over a many-to-many relation: ``tag_ids`` on create, PATCH and
read, scoped by ``links=`` and the owner's base query."""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any, ClassVar
from uuid import UUID, uuid4

import pytest
from openapi_spec_validator import validate

from hinoki import (
    API,
    UNSET,
    Access,
    BearerAuth,
    Caller,
    Field,
    Hinoki,
    Schema,
    Unset,
    all_rows,
    crud,
    derive,
)
from hinoki.ai.mcp import describe_model
from hinoki.ai.mcpapp import expose_as_mcp
from hinoki.db import Database, DatabaseModule
from hinoki.errors import RegistrationError
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
from hinoki.runtime.crudplan import select_crud_plan
from hinoki.runtime.pipeline import Pipeline
from hinoki.testing import ASGIClient
from tests.harness import Client, Pool

JSON = "application/json"
ANN = {"authorization": "Bearer ann"}
BOB = {"authorization": "Bearer bob"}
URLS = {
    "postgres": os.environ.get("HINOKI_TEST_DATABASE_URL"),
    "mysql": os.environ.get("HINOKI_TEST_MYSQL_URL"),
    "mariadb": os.environ.get("HINOKI_TEST_MARIADB_URL"),
}


class Tag(Model, table="m2mc_tags"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner: str = field(max_length=20)
    name: str = field(max_length=40)


class Post(Model, table="m2mc_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner: str = field(max_length=20)
    title: str = field(max_length=40)
    tags: ClassVar[ManyToMany[Tag]] = many_to_many(Tag, ids_name="tag_ids", max_items=3)


MODELS: list[type[Model]] = [Tag, Post, *Post.__bridges__]
TABLES = ["m2mc_posts_tags", "m2mc_posts", "m2mc_tags", "hinoki_migrations"]
TagIds = Annotated[set[UUID], Field(max_length=3)]


class PostOut(Schema):
    id: UUID
    title: str
    tag_ids: TagIds


class PostIn(Schema):
    title: str
    tag_ids: TagIds | Unset = UNSET


class PostPatch(Schema):
    title: str | Unset = UNSET
    tag_ids: TagIds | Unset = UNSET


class Tokens(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        return Caller(id=token.strip(), user=token.strip(), permissions=frozenset())


writer = Access(auth=Tokens())


def own_tags(user: str) -> QuerySet[Tag]:
    return Tag.filter(owner=user)


@crud(
    Post,
    path="/posts",
    read=PostOut,
    create=PostIn,
    update=PostPatch,
    delete=True,
    access=writer,
    owner="owner",
    identity_key=str,
    links={"tags": own_tags},
)
class Posts(API):
    def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Post]:
        assert principal is not None
        return Post.filter(owner=principal.user)


class Everything(Schema):
    id: UUID
    title: str


Plain = crud(Post, path="/plain", read=Everything, name="PlainCrud").api()
WithIds = crud(
    Post, path="/ids", read=PostOut, links={"tags": all_rows(Tag)}, name="IdsCrud"
).api()


@pytest.fixture(params=["sqlite", "postgres", "mysql", "mariadb"])
async def database(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[tuple[str, Database]]:
    kind = request.param
    if kind == "sqlite":
        url = f"sqlite://{tmp_path / 'm2mc.sqlite3'}"
    else:
        found = URLS[kind]
        if found is None:
            pytest.skip(f"the {kind} test database URL is not set")
        url = found
    connection = await Database.connect(url, max_size=2, wait=0.5)
    await drop_tables(connection)
    plan(MODELS, tmp_path, "initial", dialect=connection.dialect)
    await migrator.apply(connection, tmp_path)
    with use_database(connection):
        yield url, connection
    await drop_tables(connection)
    connection.close()


async def drop_tables(connection: Database) -> None:
    quote = "`" if connection.dialect in {"mysql", "mariadb"} else '"'
    for table in TABLES:
        await connection.execute(f"DROP TABLE IF EXISTS {quote}{table}{quote}")


@pytest.fixture
async def client(pool: Pool, database: tuple[str, Database]) -> Client:
    app = Hinoki(openapi_url="/openapi.json", include=[Posts])
    app.install(DatabaseModule(database[0], max_size=3))
    return await pool.start(app)


async def make_tags(owner: str, *names: str) -> list[Tag]:
    return [await Tag.acreate(owner=owner, name=name) for name in names]


def payload(**values: Any) -> bytes:
    return json.dumps(values).encode()


async def send(
    client: Client, method: str, path: str, who: dict[str, str], **values: Any
) -> Any:
    body = payload(**values) if values else b""
    return await client.request(
        method, path, body, content_type=JSON if body else None, headers=who
    )


async def post_row(client: Client, **values: Any) -> dict[str, Any]:
    reply = await send(client, "POST", "/posts", ANN, **values)
    assert reply.status == 201, reply.body
    return dict(reply.body)


@pytest.mark.usefixtures("database")
async def test_create_links_the_tags_and_reads_them_back(client: Client) -> None:
    red, blue = await make_tags("ann", "red", "blue")
    created = await post_row(client, title="t", tag_ids=[str(red.id), str(blue.id)])
    both = {str(red.id), str(blue.id)}
    assert set(created["tag_ids"]) == both
    shown = await send(client, "GET", f"/posts/{created['id']}", ANN)
    assert set(shown.body["tag_ids"]) == both
    assert (await post_row(client, title="no tags"))["tag_ids"] == []


@pytest.mark.usefixtures("database")
async def test_patch_replaces_the_set_and_absent_keeps_it(client: Client) -> None:
    red, blue = await make_tags("ann", "red", "blue")
    created = await post_row(client, title="t", tag_ids=[str(red.id)])
    path = f"/posts/{created['id']}"
    renamed = await send(client, "PATCH", path, ANN, title="u")
    assert renamed.body["tag_ids"] == [str(red.id)]
    swapped = await send(client, "PATCH", path, ANN, tag_ids=[str(blue.id)])
    assert swapped.body["tag_ids"] == [str(blue.id)]
    cleared = await send(client, "PATCH", path, ANN, tag_ids=[])
    assert cleared.body["tag_ids"] == []


@pytest.mark.usefixtures("database")
async def test_other_peoples_tags_look_missing_and_nothing_is_written(
    client: Client,
) -> None:
    (mine,) = await make_tags("ann", "mine")
    (theirs,) = await make_tags("bob", "theirs")
    refusals = []
    for ghost in (theirs.id, uuid4()):
        reply = await send(
            client, "POST", "/posts", ANN, title="x", tag_ids=[str(mine.id), str(ghost)]
        )
        assert reply.status == 422
        refusals.append({k: v for k, v in reply.body.items() if k != "request_id"})
    assert refusals[0] == refusals[1]
    assert str(theirs.id) not in json.dumps(refusals[0])
    assert await Post.all().acount() == 0  # the route's transaction rolled back


@pytest.mark.usefixtures("database")
async def test_a_failed_patch_leaves_the_row_and_links_alone(client: Client) -> None:
    (mine,) = await make_tags("ann", "mine")
    created = await post_row(client, title="before", tag_ids=[str(mine.id)])
    reply = await send(
        client,
        "PATCH",
        f"/posts/{created['id']}",
        ANN,
        title="after",
        tag_ids=[str(uuid4())],
    )
    assert reply.status == 422
    row = await Post.aget(id=UUID(created["id"]))
    assert row.title == "before"
    assert await row.tags.acount() == 1


@pytest.mark.usefixtures("database")
async def test_links_to_hidden_targets_survive_a_replace_and_stay_hidden(
    client: Client,
) -> None:
    (mine,) = await make_tags("ann", "mine")
    (theirs,) = await make_tags("bob", "theirs")
    created = await post_row(client, title="t", tag_ids=[str(mine.id)])
    post = await Post.aget(id=UUID(created["id"]))
    await post.tags.aadd(theirs)
    shown = await send(client, "GET", f"/posts/{created['id']}", ANN)
    assert shown.body["tag_ids"] == [str(mine.id)]
    await send(client, "PATCH", f"/posts/{created['id']}", ANN, tag_ids=[])
    assert {t.id for t in await post.tags.alist()} == {theirs.id}


@pytest.mark.usefixtures("database")
async def test_a_post_outside_the_base_query_is_not_found_before_any_write(
    client: Client,
) -> None:
    (theirs,) = await make_tags("bob", "bobs")
    created = await post_row(client, title="t")
    reply = await send(
        client, "PATCH", f"/posts/{created['id']}", BOB, tag_ids=[str(theirs.id)]
    )
    assert reply.status == 404
    post = await Post.aget(id=UUID(created["id"]))
    assert await post.tags.acount() == 0


@pytest.mark.usefixtures("database")
async def test_the_set_is_bounded_and_the_ids_are_typed(client: Client) -> None:
    many = [str(uuid4()) for _ in range(4)]
    twice = [many[0], many[0]]
    for value in (many, ["not-a-uuid"], twice, "x'); DROP TABLE m2mc_tags;--"):
        reply = await send(client, "POST", "/posts", ANN, title="x", tag_ids=value)
        assert reply.status == 422
    assert await Tag.all().acount() == 0


@pytest.mark.usefixtures("database")
async def test_the_list_loads_links_with_a_constant_number_of_queries(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tag,) = await make_tags("ann", "t")
    for number in range(6):
        await post_row(client, title=f"p{number}", tag_ids=[str(tag.id)])
    calls = 0
    original = Database.fetch
    original_models = Database.fetch_models

    async def counted(self: Database, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await original(self, *args, **kwargs)

    async def counted_models(self: Database, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await original_models(self, *args, **kwargs)

    monkeypatch.setattr(Database, "fetch", counted)
    monkeypatch.setattr(Database, "fetch_models", counted_models)
    reply = await send(client, "GET", "/posts", ANN)
    assert reply.status == 200
    assert len(reply.body["items"]) == 6
    assert all(item["tag_ids"] == [str(tag.id)] for item in reply.body["items"])
    six = calls
    drop = [row.id for row in await Post.all().limit(4)]
    await Post.filter(id__in=drop).adelete()
    calls = 0
    reply = await send(client, "GET", "/posts", ANN)
    assert len(reply.body["items"]) == 2
    assert six == calls  # the number of queries does not grow with the rows


@pytest.mark.usefixtures("database")
async def test_deleting_the_post_removes_its_links(client: Client) -> None:
    (tag,) = await make_tags("ann", "t")
    created = await post_row(client, title="t", tag_ids=[str(tag.id)])
    gone = await send(client, "DELETE", f"/posts/{created['id']}", ANN)
    assert gone.status == 204
    assert await Post.__bridges__[0].all().acount() == 0


@pytest.mark.usefixtures("database")
async def test_the_openapi_document_describes_the_ids_field(client: Client) -> None:
    document = (await client.request("GET", "/openapi.json")).body
    validate(document)
    schema = document["components"]["schemas"]["PostIn"]
    ids = schema["properties"]["tag_ids"]
    assert ids["maxItems"] == 3
    assert ids["uniqueItems"] is True
    assert ids["items"] == {"type": "string", "format": "uuid"}
    assert "tag_ids" not in schema.get("required", [])


def test_every_ids_field_needs_a_scope_for_its_targets() -> None:
    with pytest.raises(RegistrationError, match=r"tag_ids needs links="):
        crud(Post, path="/x", read=PostOut).api()
    with pytest.raises(RegistrationError, match="not many_to_many relations"):
        crud(Post, path="/x", read=Everything, links={"nope": all_rows(Tag)}).api()


def test_all_rows_is_the_explicit_way_to_allow_every_target() -> None:
    api = crud(Post, path="/x", read=PostOut, links={"tags": all_rows(Tag)}).api()
    assert api.resource.links["tags"](None).model is Tag


def test_the_ids_field_is_a_bounded_set_of_the_target_key_type() -> None:
    class Loose(Schema):
        id: UUID
        tag_ids: set[int]

    with pytest.raises(RegistrationError, match="set of Tag keys"):
        crud(Post, path="/x", read=Loose, links={"tags": all_rows(Tag)}).api()

    class Unbounded(Schema):
        title: str
        tag_ids: set[UUID]

    with pytest.raises(RegistrationError, match="max_length"):
        crud(
            Post,
            path="/x",
            read=PostOut,
            create=Unbounded,
            access=writer,
            owner="owner",
            identity_key=str,
            links={"tags": all_rows(Tag)},
        ).api()


def test_derive_takes_the_ids_field_when_it_is_named() -> None:
    @derive(Post, only=("title", "tag_ids"))
    class Derived(Schema):
        pass

    spec = Derived.specs()["tag_ids"].ftype
    assert spec.collection == "set"
    assert spec.info.max_length == 3
    with pytest.raises(RegistrationError, match="no columns"):

        @derive(Post, only=("title", "nope"))
        class Bad(Schema):
            pass


def test_generated_crud_with_plain_ids_fields_is_a_native_candidate(
    tmp_path: Path,
) -> None:
    app = Hinoki(
        include=[WithIds, Plain],
        modules=[DatabaseModule(f"sqlite://{tmp_path / 'lane.sqlite3'}")],
    )
    pipeline = Pipeline(app)
    reasons = {}
    for binding in pipeline.bindings:
        owner = binding.route.owner
        reasons[owner.__name__ if owner else ""] = select_crud_plan(binding, pipeline)[
            1
        ]
    assert reasons["IdsCrud"] == "eligible"
    assert reasons["PlainCrud"] != "custom_or_unsupported_schema"


RACE: dict[str, Database | str] = {}


@crud(
    Post,
    path="/race",
    read=PostOut,
    update=PostPatch,
    access=writer,
    owner="owner",
    identity_key=str,
    links={"tags": own_tags},
)
class Racing(API):
    def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Post]:
        return Post.all()

    async def link_queryset(
        self, relation: str, principal: Caller[Any] | None
    ) -> QuerySet[Any]:
        # The window between the caller's read of the row and the link write:
        # another connection hands the row to Bob or deletes it, and commits.
        other = RACE.pop("other", None)
        if isinstance(other, Database):
            with use_database(other):
                if RACE.get("mode") == "delete":
                    await Post.filter(owner="ann").adelete()
                else:
                    await Post.filter(owner="ann").aupdate(
                        {"owner": "bob"}, returning=False
                    )
        assert principal is not None
        return own_tags(principal.user)


@pytest.mark.parametrize(("mode", "status"), [("handover", 403), ("delete", 404)])
async def test_a_row_taken_away_while_the_links_wait_is_refused(
    pool: Pool, database: tuple[str, Database], mode: str, status: int
) -> None:
    url, connection = database
    if connection.dialect == "sqlite":
        pytest.skip("SQLite has a single writer: nobody can write in the window")
    app = Hinoki(include=[Racing])
    app.install(DatabaseModule(url, max_size=3))
    client = await pool.start(app)
    (mine,) = await make_tags("ann", "mine")
    created = {"id": str((await Post.acreate(owner="ann", title="t")).id)}
    other = await Database.connect(url, max_size=1, wait=0.5)
    RACE["other"] = other
    RACE["mode"] = mode
    try:
        reply = await send(
            client,
            "PATCH",
            f"/race/{created['id']}",
            ANN,
            tag_ids=[str(mine.id)],
        )
    finally:
        RACE.pop("other", None)
        RACE.pop("mode", None)
        other.close()
    assert reply.status == status
    taken = await Post.filter(id=UUID(created["id"]))
    if mode == "handover":
        assert [p.owner for p in taken] == ["bob"]
        assert await taken[0].tags.acount() == 0
    else:
        assert taken == []


def test_the_dev_mcp_describes_the_many_to_many_relation() -> None:
    assert describe_model(Post)["many_to_many"] == {
        "tags": {
            "to": "Tag",
            "bridge": "PostTags",
            "ids_name": "tag_ids",
            "max_items": 3,
        }
    }


async def test_the_app_mcp_tool_for_create_accepts_the_ids_field(
    database: tuple[str, Database],
) -> None:
    app = Hinoki()
    app.include(Posts)
    app.install(DatabaseModule(database[0], max_size=3))
    app.include(
        *expose_as_mcp(app, tools=["*.create"], access=writer, allow_writes=True)
    )
    async with ASGIClient(app) as mcp_client:
        reply = await mcp_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=ANN,
        )
    (listed,) = reply.body["result"]["tools"]
    body = json.dumps(listed["inputSchema"])
    assert "tag_ids" in body and "uniqueItems" in body and "maxItems" in body
