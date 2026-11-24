"""The ``many_to_many`` field: bridge model, migrations, link writes, filters."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID, uuid4

import pytest

from hinoki import context, core, events
from hinoki.db import Database
from hinoki.errors import (
    FieldError,
    LinkLimitExceeded,
    ModelError,
    ModelValidationError,
    RegistrationError,
    RelationNotLoaded,
    UnknownLinkTarget,
)
from hinoki.orm import (
    LinkKey,
    ManyToMany,
    Model,
    OnDelete,
    field,
    many_to_many,
    migrator,
    use_database,
)
from hinoki.orm.links import LinkSide
from hinoki.orm.migrations import models_of, plan, state_of
from hinoki.settings import EventsSettings

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
URLS = {
    "postgres": URL,
    "mysql": os.environ.get("HINOKI_TEST_MYSQL_URL"),
    "mariadb": os.environ.get("HINOKI_TEST_MARIADB_URL"),
}


class Tag(Model, table="m2mf_tags"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    label: str
    active: bool = True
    posts: ClassVar["ManyToMany[Post]"]


class Post(Model, table="m2mf_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str
    tags: ClassVar[ManyToMany[Tag]] = many_to_many(
        Tag, related_name="posts", ids_name="tag_ids", max_items=3
    )
    tag_ids: ClassVar[tuple[LinkKey, ...]]


class Person(Model, table="m2mf_people"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    friends: ClassVar["ManyToMany[Person]"] = many_to_many(
        "self", related_name="friend_of"
    )
    friend_of: ClassVar["ManyToMany[Person]"]


MODELS: list[type[Model]] = [Tag, Post, *Post.__bridges__, Person, *Person.__bridges__]
TABLES = [
    "m2mf_posts_tags",
    "m2mf_people_friends",
    "m2mf_posts",
    "m2mf_tags",
    "m2mf_people",
    "hinoki_migrations",
]


async def drop_tables(database: Database) -> None:
    quote = "`" if database.dialect in {"mysql", "mariadb"} else '"'
    for table in TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {quote}{table}{quote}")


@pytest.fixture(params=["sqlite", "postgres", "mysql", "mariadb"])
async def db(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Database]:
    if request.param == "sqlite":
        database = await Database.connect(
            f"sqlite://{tmp_path / 'm2m.sqlite3'}", max_size=1, wait=0.5
        )
    else:
        url = URLS[request.param]
        if url is None:
            pytest.skip(f"the {request.param} test database URL is not set")
        database = await Database.connect(url, max_size=2, wait=0.5)
        await drop_tables(database)
    plan(MODELS, tmp_path, "initial", dialect=database.dialect)
    await migrator.apply(database, tmp_path)
    with use_database(database):
        yield database
    if request.param != "sqlite":
        await drop_tables(database)
    database.close()


async def deactivate(tag: Tag) -> None:
    await Tag.filter(id=tag.id).aupdate({"active": False}, returning=False)


async def make_post(*labels: str) -> tuple[Post, list[Tag]]:
    tags = [await Tag.acreate(label=label) for label in labels]
    return await Post.acreate(title="p"), tags


async def test_parallel_adds_never_exceed_max_items(db: Database) -> None:
    if db.dialect == "sqlite":
        pytest.skip("SQLite has a single writer connection")
    post, tags = await make_post("a", "b", "c", "d")

    async def link(tag: Tag) -> bool:
        try:
            async with db.atomic():
                await post.tags.aadd(tag)
        except LinkLimitExceeded:
            return False
        return True

    results = await asyncio.gather(*(link(tag) for tag in tags))
    assert sum(results) == 3
    assert await post.tags.acount() == 3


async def test_parallel_sets_of_one_owner_end_in_one_of_them(db: Database) -> None:
    if db.dialect == "sqlite":
        pytest.skip("SQLite has a single writer connection")
    post, tags = await make_post("a", "b", "c")

    async def replace(chosen: list[Tag]) -> None:
        async with db.atomic():
            await post.tags.aset(chosen)

    await asyncio.gather(replace(tags[:2]), replace(tags[1:]))
    linked = {t.id for t in await post.tags.alist()}
    assert linked in ({t.id for t in tags[:2]}, {t.id for t in tags[1:]})


def test_the_bridge_is_a_registered_model() -> None:
    (bridge,) = Post.__bridges__
    assert bridge.__table__ == "m2mf_posts_tags"
    assert bridge.__columns__ == ("id", "post_id", "tag_id")
    assert bridge.__unique__ == (("post_id", "tag_id"),)
    assert ("tag_id",) in bridge.__indexes__
    assert bridge.__field_types__["tag_id"].kind == "uuid"
    assert Tag.__links__["posts"].target is Post
    assert Post.__links__["tags"].max_items == 3
    assert Person.__bridges__[0].__columns__ == ("id", "from_person_id", "to_person_id")


def test_the_state_of_the_owners_alone_includes_their_bridges() -> None:
    tables = list(state_of([Tag, Post, Person])["tables"])
    assert tables.index("m2mf_posts_tags") == tables.index("m2mf_posts") + 1
    assert "PostTags" not in [m.__name__ for m in models_of(__name__)]


@pytest.mark.parametrize("dialect", ["postgres", "sqlite", "mysql", "mariadb"])
def test_the_migration_creates_the_bridge(dialect: str, tmp_path: Path) -> None:
    result = plan(MODELS, tmp_path, "initial", dialect=dialect)
    assert result.path is not None
    sql = result.path.read_text()
    quote = "`" if dialect in {"mysql", "mariadb"} else '"'
    assert f"CREATE TABLE {quote}m2mf_posts_tags{quote}" in sql
    assert f"UNIQUE ({quote}post_id{quote}, {quote}tag_id{quote})" in sql
    assert f"REFERENCES {quote}m2mf_tags{quote}" in sql
    assert "ON DELETE CASCADE" in sql
    assert not plan(MODELS, tmp_path, "again", dialect=dialect).operations


def declare(**options: Any) -> type[Model]:
    namespace = {
        "__annotations__": {"id": UUID, "x": ClassVar[ManyToMany[Tag]]},
        "id": field(primary_key=True, default_factory=uuid4),
        "x": many_to_many(Tag, **options),
    }
    return type("Holder", (Model,), namespace, table="m2mf_holders")


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"on_delete": OnDelete.SET_NULL}, "CASCADE"),
        ({"max_items": 0}, "max_items"),
        ({"max_items": 10_001}, "max_items"),
        ({"bridge": "B", "table": "t"}, "bridge="),
        ({"related_name": "label"}, "already has"),
        ({"table": "t" * 64}, "shorter"),
    ],
)
def test_a_bad_declaration_is_refused(options: dict[str, Any], message: str) -> None:
    with pytest.raises(RegistrationError, match=message):
        declare(**options)


def test_a_failed_declaration_leaves_the_target_untouched() -> None:
    before = dict(Tag.__links__)
    with pytest.raises(RegistrationError):
        declare(related_name="posts")
    assert Tag.__links__ == before


async def test_add_remove_set_are_idempotent_and_report_the_change(
    db: Database,
) -> None:
    post, (red, blue, green) = await make_post("red", "blue", "green")
    first = await post.tags.aadd(red, blue.id)
    assert set(first.added) == {red.id, blue.id}
    assert (await post.tags.aadd(red)).added == ()
    changed = await post.tags.aset([blue, green])
    assert (changed.added, changed.removed) == ((green.id,), (red.id,))
    assert (await post.tags.aremove(red, green)).removed == (green.id,)
    assert [t.id for t in await post.tags.alist()] == [blue.id]
    assert await post.tags.acount() == 1
    assert await post.tags.aset([]) == type(changed)((), (blue.id,))
    assert not await post.tags.aexists()


async def test_unknown_and_out_of_scope_targets_write_nothing(db: Database) -> None:
    post, (red, blue) = await make_post("red", "blue")
    await deactivate(blue)
    with pytest.raises(UnknownLinkTarget) as unknown:
        await post.tags.aadd(red, uuid4())
    with pytest.raises(UnknownLinkTarget) as hidden:
        await post.tags.aadd(red, blue, within=Tag.filter(active=True))
    assert unknown.value.details == hidden.value.details
    assert not await post.tags.aexists()
    assert (await post.tags.aremove(uuid4())).removed == ()


async def test_set_only_touches_links_inside_the_scope(db: Database) -> None:
    post, (red, blue, green) = await make_post("red", "blue", "green")
    await deactivate(blue)
    await post.tags.aadd(red, blue)
    visible = Tag.filter(active=True)
    await post.tags.aset([green], within=visible)
    assert {t.id for t in await post.tags.alist()} == {blue.id, green.id}
    await post.tags.aset([], within=visible)
    assert [t.id for t in await post.tags.alist()] == [blue.id]


async def test_the_limit_applies_to_the_resulting_links(db: Database) -> None:
    post, tags = await make_post("a", "b", "c", "d")
    await post.tags.aadd(*tags[:3])
    with pytest.raises(LinkLimitExceeded):
        await post.tags.aadd(tags[3])
    assert await post.tags.acount() == 3
    with pytest.raises(LinkLimitExceeded):
        await post.tags.aset(tags)
    assert await post.tags.acount() == 3


async def test_invalid_keys_and_unsaved_owners_are_refused(db: Database) -> None:
    post, _ = await make_post()
    with pytest.raises(ModelValidationError) as invalid:
        await post.tags.aadd("1; DROP TABLE m2mf_tags")
    assert invalid.value.details[0].path == "tag_ids[0]"
    assert "DROP" not in str(invalid.value.details)
    with pytest.raises(ModelValidationError):
        await post.tags.aadd(True)
    with pytest.raises(FieldError):
        await post.tags.aadd(cast(Any, post))
    with pytest.raises(ModelError, match="save Post"):
        await Post(title="new").tags.aadd(uuid4())
    assert await Tag.all().acount() == 0


async def test_deleting_either_side_removes_its_links(db: Database) -> None:
    post, (red, blue) = await make_post("red", "blue")
    await post.tags.aadd(red, blue)
    await Tag.filter(id=red.id).adelete()
    assert [t.id for t in await post.tags.alist()] == [blue.id]
    await Post.filter(id=post.id).adelete()
    assert await Tag.filter(id=blue.id).acount() == 1
    (bridge,) = Post.__bridges__
    assert await bridge.all().acount() == 0


async def test_filters_across_the_relation_do_not_multiply_rows(db: Database) -> None:
    post, (red, blue, other) = await make_post("red", "blue", "other")
    lone = await Post.acreate(title="lone")
    await post.tags.aadd(red, blue)
    ids = {p.id for p in await Post.filter(tags__label__in=["red", "blue"])}
    assert ids == {post.id}
    assert await Post.filter(tags__label__in=["red", "blue"]).acount() == 1
    assert {p.id for p in await Post.filter(tags__id=red.id)} == {post.id}
    assert {p.id for p in await Post.filter(tags__id__in=[other.id])} == set()
    assert {p.id for p in await Post.filter(tags__isnull=True)} == {lone.id}
    assert {p.id for p in await Post.filter(tags__isnull=False)} == {post.id}
    assert {p.id for p in await Post.filter(title="p", tags__label="red")} == {post.id}
    assert {p.id for p in await Tag.filter(posts__title="p")} == {red.id, blue.id}
    with pytest.raises(FieldError):
        await Post.filter(tags__label="red").adelete()


async def test_a_label_that_looks_like_sql_is_only_a_value(db: Database) -> None:
    post, (tag,) = await make_post("x'; DROP TABLE m2mf_tags; --")
    await post.tags.aadd(tag)
    found = await Post.filter(tags__label="x'; DROP TABLE m2mf_tags; --")
    assert [p.id for p in found] == [post.id]
    assert await Tag.all().acount() == 1


async def test_prefetch_loads_every_owner_with_one_extra_query(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, (red, blue) = await make_post("red", "blue")
    second = await Post.acreate(title="second")
    third = await Post.acreate(title="third")
    await first.tags.aadd(red, blue)
    await second.tags.aadd(blue)
    calls = 0
    for name in ("fetch", "fetch_models"):
        original = getattr(Database, name)

        async def counted(
            self: Database, *args: Any, _original: Any = original, **kwargs: Any
        ) -> Any:
            nonlocal calls
            calls += 1
            return await _original(self, *args, **kwargs)

        monkeypatch.setattr(Database, name, counted)
    posts = await Post.all().prefetch_related("tags")
    assert calls == 2  # the posts, then their joined tags
    by_id = {p.id: p for p in posts}
    assert {t.label for t in by_id[first.id].tags.loaded} == {"red", "blue"}
    assert by_id[second.id].tag_ids == (blue.id,)
    assert (
        next(t for t in by_id[first.id].tags.loaded if t.id == blue.id)
        is (by_id[second.id].tags.loaded[0])
    )
    assert by_id[third.id].tags.loaded == ()
    with pytest.raises(RelationNotLoaded):
        _ = (await Post.aget(id=first.id)).tags.loaded


async def test_prefetch_can_be_narrowed_to_a_scope(db: Database) -> None:
    post, (red, blue) = await make_post("red", "blue")
    await post.tags.aadd(red, blue)
    posts = await Post.all().prefetch_related(tags=Tag.filter(label="red"))
    assert [t.label for t in posts[0].tags.loaded] == ["red"]


async def test_prefetch_fallback_matches_native_and_subfilter_scope(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, (red, blue) = await make_post("red", "blue")
    second = await Post.acreate(title="selected")
    await first.tags.aadd(red, blue)
    await second.tags.aadd(red)
    scope = Tag.filter(posts__title="selected")
    native = await Post.all().prefetch_related(tags=scope)
    expected = {post.id: post.tag_ids for post in native}
    assert expected[first.id] == (red.id,)
    monkeypatch.setattr(Tag, "row_plan", classmethod(lambda _cls: None))
    fallback = await Post.all().prefetch_related(tags=scope)
    assert {post.id: post.tag_ids for post in fallback} == expected


async def test_prefetch_matches_two_query_oracle_for_scoped_targets(
    db: Database,
) -> None:
    first, (red, blue, green) = await make_post("red", "blue", "green")
    second = await Post.acreate(title="selected")
    empty = await Post.acreate(title="empty")
    await first.tags.aadd(red, blue, green)
    await second.tags.aadd(red, green)
    await deactivate(green)
    side = cast(LinkSide, Post.__links__["tags"])
    sql, params = core.compile_query(
        "select",
        side.bridge.__table__,
        columns=[side.near, side.far],
        filters=[(side.near, "in", [first.id, second.id, empty.id])],
        dialect=db.dialect,
    )
    pairs = await db.fetch(sql, *params)
    scope = Tag.filter(active=True, posts__title="selected")
    target_ids = [row[side.far] for row in pairs]
    targets = await scope.filter(id__in=target_ids).order_by("id")
    order = [str(tag.id) for tag in targets]
    expected = {
        str(post.id): tuple(
            tag_id
            for tag_id in order
            if any(
                str(row[side.near]) == str(post.id) and str(row[side.far]) == tag_id
                for row in pairs
            )
        )
        for post in (first, second, empty)
    }
    loaded = await Post.all().prefetch_related(tags=scope)
    assert {str(post.id): tuple(map(str, post.tag_ids)) for post in loaded} == expected


async def test_a_self_relation_has_two_distinct_directions(db: Database) -> None:
    ann, bob = await Person.acreate(name="ann"), await Person.acreate(name="bob")
    await ann.friends.aadd(bob)
    assert [p.name for p in await ann.friends.alist()] == ["bob"]
    assert await bob.friends.acount() == 0
    assert [p.name for p in await bob.friend_of.alist()] == ["ann"]
    assert {p.name for p in await Person.filter(friends__name="bob")} == {"ann"}
    people = await Person.all().prefetch_related("friends", "friend_of")
    by_name = {person.name: person for person in people}
    assert [p.name for p in by_name["ann"].friends.loaded] == ["bob"]
    assert [p.name for p in by_name["bob"].friend_of.loaded] == ["ann"]


async def test_the_reverse_side_writes_the_same_links(db: Database) -> None:
    post, (red, *_) = await make_post("red", "blue")
    other = await Post.acreate(title="other")
    await red.posts.aadd(post, other)
    assert await post.tags.acount() == 1
    assert (await red.posts.aremove(other)).removed == (other.id,)
    assert {p.id for p in await red.posts.alist()} == {post.id}


async def test_events_follow_the_commit(db: Database) -> None:
    seen: list[tuple[str, object, tuple[object, ...]]] = []
    handlers = events.Handlers()

    @handlers.after_commit(events.Linked[Post])
    async def linked(event: events.Linked[Post]) -> None:
        seen.append(("linked", event.key, event.targets))

    @handlers.after_commit(events.Unlinked[Post])
    async def unlinked(event: events.Unlinked[Post]) -> None:
        seen.append(("unlinked", event.key, event.targets))

    bus = events.EventBus([handlers], EventsSettings())
    post, (red, blue) = await make_post("red", "blue")
    with events.use(bus):
        await post.tags.aadd(red)
        await post.tags.aadd(red)
        with pytest.raises(UnknownLinkTarget):
            await post.tags.aadd(blue, uuid4())
        await post.tags.aset([blue])
    assert seen == [
        ("linked", post.id, (red.id,)),
        ("linked", post.id, (blue.id,)),
        ("unlinked", post.id, (red.id,)),
    ]


async def test_an_outer_rollback_discards_the_links(db: Database) -> None:
    post, (red,) = await make_post("red")

    async def failing() -> None:
        async with db.atomic():
            await post.tags.aadd(red)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await failing()
    assert await post.tags.acount() == 0


async def test_the_sync_twins_run_in_a_sync_handler_thread(db: Database) -> None:
    post, (red, blue) = await make_post("red", "blue")

    def handler() -> tuple[int, list[str], bool]:
        post.tags.add(red)
        post.tags.set([red, blue])
        post.tags.remove(red)
        return (
            post.tags.count(),
            [t.label for t in post.tags.list()],
            post.tags.exists(),
        )

    token = context.event_loop.set(asyncio.get_running_loop())
    try:
        assert await asyncio.to_thread(handler) == (1, ["blue"], True)
    finally:
        context.event_loop.reset(token)


async def other_connection(db: Database) -> Database:
    """A second connection to the same database, for a concurrent writer."""
    url = URLS[db.dialect]
    assert url is not None
    return await Database.connect(url, max_size=1, wait=0.5)


async def test_a_link_committed_after_the_snapshot_still_counts(db: Database) -> None:
    if db.dialect == "sqlite":
        pytest.skip("SQLite has a single writer connection")
    post, tags = await make_post("a", "b", "c", "d")
    await post.tags.aadd(tags[0], tags[1])
    other = await other_connection(db)
    try:
        async with db.atomic():
            assert await post.tags.acount() == 2  # a first read fixes the snapshot
            with use_database(other):
                await post.tags.aadd(tags[2])  # commits on its own connection
            with pytest.raises(LinkLimitExceeded):
                await post.tags.aadd(tags[3])
    finally:
        other.close()
    assert await post.tags.acount() == 3


async def test_a_set_removes_the_links_committed_after_its_snapshot(
    db: Database,
) -> None:
    if db.dialect == "sqlite":
        pytest.skip("SQLite has a single writer connection")
    post, tags = await make_post("a", "b", "c")
    await post.tags.aadd(tags[0])
    other = await other_connection(db)
    try:
        async with db.atomic():
            assert await post.tags.acount() == 1
            with use_database(other):
                await post.tags.aadd(tags[1])
            change = await post.tags.aset([tags[2]])
    finally:
        other.close()
    assert set(change.removed) == {tags[0].id, tags[1].id}
    assert [t.id for t in await post.tags.alist()] == [tags[2].id]


async def test_a_resolved_target_cannot_be_deleted_until_the_commit(
    db: Database,
) -> None:
    if db.dialect == "sqlite":
        pytest.skip("SQLite has a single writer connection")
    post, (tag,) = await make_post("a")
    await post.tags.aadd(tag)
    other = await other_connection(db)
    try:
        async with db.atomic():
            await post.tags.aadd(tag)  # nothing to write: the target is only read
            with use_database(other):
                deleting = asyncio.create_task(Tag.filter(id=tag.id).adelete())
            await asyncio.sleep(0.3)
            assert not deleting.done()
        await asyncio.wait_for(deleting, 5)
    finally:
        other.close()
    assert await post.tags.acount() == 0


async def test_adding_while_the_target_is_deleted_is_clean(db: Database) -> None:
    if db.dialect == "sqlite":
        pytest.skip("SQLite has a single writer connection")
    other = await other_connection(db)
    try:
        for _ in range(10):
            post, (tag,) = await make_post("a")

            async def link(post: Post = post, tag: Tag = tag) -> bool:
                try:
                    await post.tags.aadd(tag)
                except UnknownLinkTarget:
                    return False
                return True

            async def delete(tag: Tag = tag) -> None:
                with use_database(other):
                    await Tag.filter(id=tag.id).adelete()

            await asyncio.gather(link(), delete())
            assert await post.tags.acount() == 0
    finally:
        other.close()


async def test_the_reverse_side_keeps_the_limit_of_the_declaring_side(
    db: Database,
) -> None:
    post, tags = await make_post("a", "b", "c", "d")
    await post.tags.aadd(*tags[:3])
    with pytest.raises(LinkLimitExceeded):
        await tags[3].posts.aadd(post)
    spare = await Post.acreate(title="spare")
    with pytest.raises(LinkLimitExceeded):
        await tags[3].posts.aset([post, spare])
    assert await post.tags.acount() == 3
    assert await spare.tags.acount() == 0
    await tags[3].posts.aset([spare])
    assert await spare.tags.acount() == 1


async def test_parallel_reverse_adds_never_exceed_the_declared_limit(
    db: Database,
) -> None:
    if db.dialect == "sqlite":
        pytest.skip("SQLite has a single writer connection")
    post, tags = await make_post("a", "b", "c", "d")

    async def link(tag: Tag) -> bool:
        try:
            async with db.atomic():
                await tag.posts.aadd(post)
        except LinkLimitExceeded:
            return False
        return True

    results = await asyncio.gather(*(link(tag) for tag in tags))
    assert sum(results) == 3
    assert await post.tags.acount() == 3


async def test_a_refused_write_caught_inside_a_transaction_leaves_nothing(
    db: Database,
) -> None:
    post, tags = await make_post("a", "b", "c", "d")
    await post.tags.aadd(*tags[:2])
    other = await Post.acreate(title="other")
    await other.tags.aadd(*tags[:3])
    async with db.atomic():
        with pytest.raises(LinkLimitExceeded):
            await post.tags.aadd(tags[2], tags[3])
        with pytest.raises(LinkLimitExceeded):
            await tags[3].posts.aadd(other)
        with pytest.raises(UnknownLinkTarget):
            await post.tags.aset([tags[3], uuid4()])
    assert {t.id for t in await post.tags.alist()} == {t.id for t in tags[:2]}
    assert await other.tags.acount() == 3
    assert await tags[3].posts.acount() == 0


async def test_a_sync_handler_that_catches_a_refused_write_commits_nothing(
    db: Database,
) -> None:
    post, tags = await make_post("a", "b", "c", "d")
    await post.tags.aadd(*tags[:2])

    def handler() -> None:
        try:
            post.tags.add(tags[2], tags[3])
        except LinkLimitExceeded:
            return

    token = context.event_loop.set(asyncio.get_running_loop())
    try:
        async with db.atomic():
            await asyncio.to_thread(handler)
    finally:
        context.event_loop.reset(token)
    assert await post.tags.acount() == 2


async def test_the_reverse_lock_never_touches_a_row_out_of_scope(
    db: Database,
) -> None:
    if db.dialect == "sqlite":
        pytest.skip("SQLite has no row locks")
    hidden = await Post.acreate(title="hidden")
    (tag,) = [await Tag.acreate(label="a")]
    other = await other_connection(db)
    try:
        async with other.atomic():
            with use_database(other):
                await Post.filter(id=hidden.id).aupdate(
                    {"title": "hidden"}, returning=False
                )
            with pytest.raises(UnknownLinkTarget):
                await asyncio.wait_for(
                    tag.posts.aadd(hidden, within=Post.filter(title="visible")), 3
                )
    finally:
        other.close()


async def test_both_ends_lock_the_declaring_row_first(db: Database) -> None:
    if db.dialect == "sqlite":
        pytest.skip("SQLite has a single writer connection")
    post, (tag,) = await make_post("a")
    other = await other_connection(db)
    try:
        async with db.atomic():
            await Post.filter(id=post.id).aupdate({"title": "q"}, returning=False)
            with use_database(other):
                reverse = asyncio.create_task(tag.posts.aadd(post))
            await asyncio.sleep(0.3)
            await post.tags.aadd(tag)
        await asyncio.wait_for(reverse, 5)
    finally:
        other.close()
    assert await post.tags.acount() == 1
