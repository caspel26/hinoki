"""The blog's API: public reads, editor writes, and the publication as the tenant."""

from blogworld import World

PAYLOAD = {"title": "Hello", "slug": "hello", "body": "text"}


async def test_public_reads_show_published_posts_only(world: World) -> None:
    listing = await world.client.get("/posts")
    assert [p["slug"] for p in listing.body["items"]] == ["live"]
    assert (await world.client.get("/health")).body == {"status": "ok"}


async def test_creating_assigns_the_author_and_the_publication(world: World) -> None:
    reply = await world.client.post("/posts", json=PAYLOAD, token="ada-token")
    assert reply.status == 201
    assert (
        reply.body["author_id"] == str(world.ada_id)
        and reply.body["published"] is False
    )
    drafts = await world.client.get("/posts/drafts", token="ada-token")
    assert {p["slug"] for p in drafts.body["items"]} == {"draft", "hello"}


async def test_writes_need_an_editor(world: World) -> None:
    assert (await world.client.post("/posts", json=PAYLOAD)).status == 401
    assert (
        await world.client.post("/posts", json=PAYLOAD, token="rae-token")
    ).status == 403


async def test_another_publication_cannot_see_or_touch_a_draft(world: World) -> None:
    mine = await world.client.get("/posts/drafts", token="bob-token")
    assert [p["slug"] for p in mine.body["items"]] == ["cedar-draft"]
    own = await world.client.get("/posts/drafts", token="ada-token")
    draft = next(p for p in own.body["items"] if p["slug"] == "draft")
    reply = await world.client.post(f"/posts/{draft['id']}/publish", token="bob-token")
    assert reply.status == 404
    assert (
        await world.client.delete(f"/posts/{draft['id']}", token="bob-token")
    ).status == 404


async def test_publishing_twice_is_a_conflict(world: World) -> None:
    own = await world.client.get("/posts/drafts", token="ada-token")
    draft = own.body["items"][0]
    first = await world.client.post(f"/posts/{draft['id']}/publish", token="ada-token")
    assert first.status == 200 and first.body["published"] is True
    again = await world.client.post(f"/posts/{draft['id']}/publish", token="ada-token")
    assert again.status == 409 and again.body["error"] == "post_already_published"
    assert (await world.client.get(f"/posts/{draft['id']}")).status == 200


async def test_the_export_streams_the_publications_posts_as_csv(world: World) -> None:
    reply = await world.client.get("/posts/export.csv", token="ada-token")
    assert reply.status == 200
    assert reply.headers["content-type"].startswith("text/csv")
    lines = reply.raw.decode().splitlines()
    assert lines[0] == "slug,title,published"
    assert sorted(lines[1:]) == ["draft,Draft,False", "live,Live,True"]
    assert (await world.client.get("/posts/export.csv")).status == 401
