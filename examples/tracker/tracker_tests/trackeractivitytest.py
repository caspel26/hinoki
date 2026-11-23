"""Events: the activity app records task changes; the tasks app never imports it."""

from trackerworld import World


async def feed(world: World, token: str) -> list[dict[str, object]]:
    reply = await world.client.get("/activity", token=token)
    assert reply.status == 200
    return list(reply.body["items"])


async def test_creating_a_task_records_activity_in_the_same_transaction(
    world: World,
) -> None:
    made = await world.client.post("/tasks", json={"title": "A"}, token="ann-token")

    lines = await feed(world, "ann-token")

    assert [(line["kind"], line["task_id"]) for line in lines] == [
        ("created", made.body["id"])
    ]


async def test_activity_is_private_to_its_member(world: World) -> None:
    await world.client.post("/tasks", json={"title": "A"}, token="ann-token")

    assert await feed(world, "ben-token") == []


async def test_completing_a_task_records_its_change(world: World) -> None:
    made = await world.client.post("/tasks", json={"title": "A"}, token="ann-token")
    await world.client.post(f"/tasks/{made.body['id']}/complete", token="ann-token")

    kinds = [str(line["kind"]) for line in await feed(world, "ann-token")]

    assert sorted(kinds) == ["created", "updated"]
