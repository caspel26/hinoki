"""The admin form field of a many-to-many relation: a bounded multi-select that
follows the target admin's scope and permissions."""

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest

from hinoki import Access, Caller, Hinoki
from hinoki.admin import THEMES, Admin, ModelAdmin
from hinoki.db import Database, DatabaseModule
from hinoki.errors import ModelValidationError
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
from hinoki.settings import AppSettings
from tests.harness import Client, Pool
from tests.providers import TokenAuth

FORM = "application/x-www-form-urlencoded"
STAFF = {"authorization": "Bearer editor-token"}
# The test client runs a served lifespan: an admin under "*" needs debug on.
SETTINGS = AppSettings(debug=True)
SECRET = "s" * 40
URLS = {
    "postgres": os.environ.get("HINOKI_TEST_DATABASE_URL"),
    "mysql": os.environ.get("HINOKI_TEST_MYSQL_URL"),
    "mariadb": os.environ.get("HINOKI_TEST_MARIADB_URL"),
}
REFUSED = "Some choices are not available."


class Tag(Model, table="m2ma_tags"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=40)
    hidden: bool = False


class Post(Model, table="m2ma_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=40)
    tags: ClassVar[ManyToMany[Tag]] = many_to_many(Tag, ids_name="tag_ids", max_items=3)


MODELS: list[type[Model]] = [Tag, Post, *Post.__bridges__]
TABLES = ["m2ma_posts_tags", "m2ma_posts", "m2ma_tags", "hinoki_migrations"]


class TagAdmin(ModelAdmin[Tag]):
    search = ("name",)

    def queryset(self, principal: Caller[object]) -> QuerySet[Tag]:
        return Tag.filter(hidden=False)


def build(url: str, *, tags: bool = True, theme: str = "studio") -> Hinoki:
    admin = Admin(access=Access(auth=TokenAuth()), secret=SECRET, theme=theme)
    admin.register(Post, slug="posts")
    if tags:
        admin.register(Tag, TagAdmin, slug="tags")
    app = Hinoki(settings=SETTINGS)
    app.include(admin.api())
    app.install(DatabaseModule(url, max_size=3))
    return app


@pytest.fixture(params=["sqlite", "postgres", "mysql", "mariadb"])
async def database(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[tuple[str, Database]]:
    kind = request.param
    if kind == "sqlite":
        url = f"sqlite://{tmp_path / 'm2ma.sqlite3'}"
    else:
        found = URLS[kind]
        if found is None:
            pytest.skip(f"the {kind} test database URL is not set")
        url = found
    connection = await Database.connect(url, max_size=2, wait=0.5)
    quote = "`" if connection.dialect in {"mysql", "mariadb"} else '"'
    for table in TABLES:
        await connection.execute(f"DROP TABLE IF EXISTS {quote}{table}{quote}")
    plan(MODELS, tmp_path, "initial", dialect=connection.dialect)
    await migrator.apply(connection, tmp_path)
    with use_database(connection):
        yield url, connection
    for table in TABLES:
        await connection.execute(f"DROP TABLE IF EXISTS {quote}{table}{quote}")
    connection.close()


async def open_session(client: Client, path: str) -> tuple[str, dict[str, str]]:
    page = await client.request("GET", path, headers=STAFF)
    assert page.status == 200, page.body
    cookie = page.headers["set-cookie"].split(";")[0]
    found = re.search(r'name="csrf" value="([0-9a-f]{32}\.[0-9a-f]{32})"', page.body)
    assert found
    return found.group(1), {**STAFF, "cookie": cookie}


async def send(
    client: Client, path: str, fields: list[tuple[str, str]]
) -> tuple[int, str]:
    token, headers = await open_session(client, path)
    body = urlencode([("csrf", token), *fields]).encode()
    reply = await client.request("POST", path, body, content_type=FORM, headers=headers)
    return reply.status, str(reply.body)


@pytest.fixture
async def client(pool: Pool, database: tuple[str, Database]) -> Client:
    return await pool.start(build(database[0]))


async def tags_named(*names: str, hidden: bool = False) -> list[Tag]:
    return [await Tag.acreate(name=name, hidden=hidden) for name in names]


def option_values(html: str) -> list[str]:
    select = re.search(r'<select multiple[^>]*name="tag_ids"[^>]*>(.*?)</select>', html)
    assert select, html[:800]
    return re.findall(r'<option value="([^"]+)"', select.group(1))


@pytest.mark.usefixtures("database")
async def test_the_form_lists_only_choices_the_target_admin_shows(
    client: Client,
) -> None:
    red, blue = await tags_named("red", "blue")
    (secret,) = await tags_named("secret", hidden=True)
    post = await Post.acreate(title="p")
    await post.tags.aadd(red)
    page = await client.request("GET", f"/admin/posts/{post.id}", headers=STAFF)
    values = option_values(page.body)
    assert set(values) == {str(red.id), str(blue.id)}
    assert str(secret.id) not in page.body
    assert re.search(rf'value="{red.id}" selected', page.body)
    assert not re.search(rf'value="{blue.id}" selected', page.body)


@pytest.mark.usefixtures("database")
async def test_create_and_edit_replace_the_links_inside_the_scope_only(
    client: Client,
) -> None:
    red, blue = await tags_named("red", "blue")
    (secret,) = await tags_named("secret", hidden=True)
    status, _ = await send(
        client,
        "/admin/posts/add",
        [("title", "new"), ("tag_ids", str(red.id)), ("tag_ids", str(blue.id))],
    )
    assert status == 303
    post = await Post.aget(title="new")
    assert {t.id for t in await post.tags.alist()} == {red.id, blue.id}
    await post.tags.aadd(secret)
    status, _ = await send(
        client, f"/admin/posts/{post.id}", [("title", "new"), ("tag_ids", str(red.id))]
    )
    assert status == 303
    assert {t.id for t in await post.tags.alist()} == {red.id, secret.id}
    await send(client, f"/admin/posts/{post.id}", [("title", "new")])
    assert {t.id for t in await post.tags.alist()} == {secret.id}


@pytest.mark.usefixtures("database")
async def test_a_hidden_or_unknown_choice_is_refused_alike_and_nothing_is_saved(
    client: Client,
) -> None:
    (red,) = await tags_named("red")
    (secret,) = await tags_named("secret", hidden=True)
    replies = []
    for ghost in (str(secret.id), str(uuid4())):
        status, html = await send(
            client,
            "/admin/posts/add",
            [("title", "bad"), ("tag_ids", str(red.id)), ("tag_ids", ghost)],
        )
        assert status in {422, 200}
        assert REFUSED in html
        assert ghost not in html
        replies.append((status, re.sub(r'name="csrf[^>]*>', "", html)))
    assert replies[0][0] == replies[1][0]
    assert len(replies[0][1]) == len(replies[1][1])
    status, html = await send(
        client,
        "/admin/posts/add",
        [("title", "bad"), ("tag_ids", "x'); DROP TABLE m2ma_tags;--")],
    )
    assert status in {422, 200}
    assert REFUSED in html
    assert await Post.all().acount() == 0
    assert await Tag.all().acount() == 2


@pytest.mark.usefixtures("database")
async def test_more_choices_than_the_limit_are_refused(client: Client) -> None:
    found = await tags_named("a", "b", "c", "d")
    status, html = await send(
        client,
        "/admin/posts/add",
        [("title", "many"), *[("tag_ids", str(t.id)) for t in found]],
    )
    assert status in {422, 200} and REFUSED in html
    assert await Post.all().acount() == 0


@pytest.mark.usefixtures("database")
async def test_the_choices_and_the_search_are_bounded(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hinoki.admin.linkfields.OPTION_LIMIT", 2)
    await tags_named("alpha one", "alpha two", "beta", "gamma")
    post = await Post.acreate(title="p")
    page = await client.request("GET", f"/admin/posts/{post.id}", headers=STAFF)
    assert len(option_values(page.body)) == 2
    assert "link_q_tag_ids" in page.body
    found = await client.request(
        "GET", f"/admin/posts/{post.id}", query="link_q_tag_ids=alpha", headers=STAFF
    )
    assert len(option_values(found.body)) == 2
    narrow = await client.request(
        "GET", f"/admin/posts/{post.id}", query="link_q_tag_ids=beta", headers=STAFF
    )
    assert len(option_values(narrow.body)) == 1
    long_search = "x" * 5000
    huge = await client.request(
        "GET",
        f"/admin/posts/{post.id}",
        query=f"link_q_tag_ids={long_search}",
        headers=STAFF,
    )
    assert huge.status == 200 and option_values(huge.body) == []


async def test_without_a_target_admin_the_field_is_read_only_without_a_count(
    pool: Pool, database: tuple[str, Database]
) -> None:
    client = await pool.start(build(database[0], tags=False))
    (red,) = await tags_named("red")
    post = await Post.acreate(title="p")
    await post.tags.aadd(red)
    page = await client.request("GET", f"/admin/posts/{post.id}", headers=STAFF)
    assert "You may not view Tag rows." in page.body
    assert "1 linked" not in page.body
    assert re.search(r'<select multiple[^>]*name="tag_ids"[^>]*disabled', page.body)
    assert str(red.id) not in page.body
    other = await tags_named("other")
    status, _ = await send(
        client,
        f"/admin/posts/{post.id}",
        [("title", "p"), ("tag_ids", str(other[0].id))],
    )
    assert status == 303
    assert {t.id for t in await post.tags.alist()} == {red.id}


@pytest.mark.parametrize("theme", sorted(THEMES))
async def test_every_theme_renders_the_multi_select(
    theme: str, pool: Pool, database: tuple[str, Database]
) -> None:
    client = await pool.start(build(database[0], theme=theme))
    (red,) = await tags_named("red")
    page = await client.request("GET", "/admin/posts/add", headers=STAFF)
    assert option_values(page.body) == [str(red.id)]


class ReadonlyPostAdmin(ModelAdmin[Post]):
    readonly = ("tag_ids",)
    exclude = ()


def test_the_ids_name_is_valid_in_readonly_exclude_and_fieldsets() -> None:
    ReadonlyPostAdmin().validate_config("posts")

    class Fieldsets(ModelAdmin[Post]):
        exclude = ("tag_ids",)
        fieldsets: ClassVar[dict[str, tuple[str, ...]]] = {"Main": ("title", "tag_ids")}

    Fieldsets().validate_config("posts")


class KeySearchTagAdmin(ModelAdmin[Tag]):
    exclude = ("name",)


@pytest.mark.usefixtures("database")
async def test_a_search_on_an_admin_without_search_fields_matches_the_key(
    pool: Pool, database: tuple[str, Database]
) -> None:
    admin = Admin(access=Access(auth=TokenAuth()), secret=SECRET)
    admin.register(Post, slug="posts")
    admin.register(Tag, KeySearchTagAdmin, slug="tags")
    app = Hinoki(settings=SETTINGS)
    app.include(admin.api())
    app.install(DatabaseModule(database[0], max_size=3))
    client = await pool.start(app)
    red, blue = await tags_named("red", "blue")
    post = await Post.acreate(title="p")
    found = await client.request(
        "GET",
        f"/admin/posts/{post.id}",
        query=f"link_q_tag_ids={red.id}",
        headers=STAFF,
    )
    assert option_values(found.body) == [str(red.id)]
    assert str(blue.id) not in found.body


@pytest.mark.parametrize("failure", ["no_scope", "invalid", "gone"])
@pytest.mark.usefixtures("database")
async def test_a_link_write_that_cannot_proceed_is_refused_without_saving(
    client: Client, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    (red,) = await tags_named("red")
    post = await Post.acreate(title="before")
    if failure == "no_scope":
        monkeypatch.setattr("hinoki.admin.linkfields.scope_of", lambda *_: None)
    else:
        error = (
            ModelValidationError([]) if failure == "invalid" else Post.NotFound("gone")
        )

        async def refuse(*_a: object, **_k: object) -> None:
            raise error

        monkeypatch.setattr("hinoki.orm.links.LinkSet.write", refuse)
    status, html = await send(
        client,
        f"/admin/posts/{post.id}",
        [("title", "after"), ("tag_ids", str(red.id))],
    )
    assert status != 303
    assert (REFUSED in html) == (failure != "gone")
    assert (await Post.aget(id=post.id)).title == "before"
    assert await post.tags.acount() == 0
