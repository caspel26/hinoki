"""The generated list's FilterSet: Eq, In, Range, .null, Search and ordering."""

from trackerworld import World


async def seed(world: World) -> None:
    token = "ann-token"
    for title, priority in (("Write docs", 1), ("Fix bug", 5), ("Write tests", 3)):
        made = await world.client.post(
            "/tasks", json={"title": title, "priority": priority}, token=token
        )
        assert made.status == 201
    first = await world.client.get("/tasks", query="search=bug", token=token)
    await world.client.post(
        f"/tasks/{first.body['items'][0]['id']}/complete", token=token
    )


async def titles(world: World, query: str) -> list[str]:
    reply = await world.client.get("/tasks", query=query, token="ann-token")
    assert reply.status == 200
    return [task["title"] for task in reply.body["items"]]


async def test_eq_in_and_null_narrow_the_list(world: World) -> None:
    await seed(world)

    assert await titles(world, "done=true") == ["Fix bug"]
    assert sorted(await titles(world, "priority=1,3")) == ["Write docs", "Write tests"]
    assert await titles(world, "completed_at.null=false") == ["Fix bug"]
    assert len(await titles(world, "completed_at.null=true")) == 2


async def test_range_search_and_ordering(world: World) -> None:
    await seed(world)

    assert await titles(world, "created_at.from=2999-01-01T00:00:00Z") == []
    assert await titles(world, "search=write&ordering=title") == [
        "Write docs",
        "Write tests",
    ]
    assert await titles(world, "ordering=-priority") == [
        "Fix bug",
        "Write tests",
        "Write docs",
    ]


async def test_filters_only_narrow_the_callers_own_tasks(world: World) -> None:
    await seed(world)

    other = await world.client.get("/tasks", query="search=write", token="ben-token")

    assert other.body["items"] == []


async def test_a_filter_the_list_does_not_declare_is_refused(world: World) -> None:
    reply = await world.client.get(
        "/tasks", query="ordering=owner_id", token="ann-token"
    )

    assert reply.status == 422
