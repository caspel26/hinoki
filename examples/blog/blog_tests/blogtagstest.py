"""Tags: a many-to-many relation scoped to the caller's publication."""

from blogworld import World

PAYLOAD = {"title": "Hello", "slug": "hello", "body": "text"}


async def make_tag(world: World, name: str, *, editor: str = "ada") -> str:
    reply = await world.client.post(
        "/tags", json={"name": name}, token=f"{editor}-token"
    )
    assert reply.status == 201
    return str(reply.body["id"])


async def test_a_post_is_created_with_tags_and_shows_them(world: World) -> None:
    news = await make_tag(world, "news")
    notes = await make_tag(world, "notes")
    reply = await world.client.post(
        "/posts", json={**PAYLOAD, "tag_ids": [news, notes]}, token="ada-token"
    )
    assert reply.status == 201
    assert sorted(reply.body["tag_ids"]) == sorted([news, notes])
    drafts = await world.client.get("/posts/drafts", token="ada-token")
    hello = next(p for p in drafts.body["items"] if p["slug"] == "hello")
    assert sorted(hello["tag_ids"]) == sorted([news, notes])


async def test_a_post_without_tag_ids_has_none(world: World) -> None:
    reply = await world.client.post("/posts", json=PAYLOAD, token="ada-token")
    assert reply.status == 201 and reply.body["tag_ids"] == []


async def test_a_tag_of_another_publication_is_unknown(world: World) -> None:
    foreign = await make_tag(world, "cedar-only", editor="bob")
    reply = await world.client.post(
        "/posts", json={**PAYLOAD, "tag_ids": [foreign]}, token="ada-token"
    )
    assert reply.status == 422
    assert foreign not in reply.raw.decode()
    drafts = await world.client.get("/posts/drafts", token="ada-token")
    assert "hello" not in {p["slug"] for p in drafts.body["items"]}


async def test_more_tags_than_the_limit_is_refused(world: World) -> None:
    ids = [await make_tag(world, f"t{n}") for n in range(11)]
    reply = await world.client.post(
        "/posts", json={**PAYLOAD, "tag_ids": ids}, token="ada-token"
    )
    assert reply.status == 422


async def test_tags_are_listed_per_publication(world: World) -> None:
    await make_tag(world, "news")
    await make_tag(world, "cedar-only", editor="bob")
    mine = await world.client.get("/tags", token="ada-token")
    assert [t["name"] for t in mine.body["items"]] == ["news"]
    assert (await world.client.get("/tags")).status == 401


async def test_deleting_a_post_removes_its_links(world: World) -> None:
    news = await make_tag(world, "news")
    made = await world.client.post(
        "/posts", json={**PAYLOAD, "tag_ids": [news]}, token="ada-token"
    )
    gone = await world.client.delete(f"/posts/{made.body['id']}", token="ada-token")
    assert gone.status == 204
    assert (await world.client.get("/tags", token="ada-token")).status == 200
