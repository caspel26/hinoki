"""The tracker through HTTP: access, ownership, business rules, middleware."""

from tracker.apps.members.security import hash_token
from trackerworld import World


async def create(world: World, title: str, **extra: object) -> dict[str, object]:
    made = await world.client.post(
        "/tasks", json={"title": title, **extra}, token="ann-token"
    )
    assert made.status == 201
    return dict(made.body)


async def test_a_request_without_a_member_token_is_refused(world: World) -> None:
    anonymous = await world.client.get("/tasks")
    unknown = await world.client.get("/tasks", token="nobody")

    assert (anonymous.status, unknown.status) == (401, 401)


async def test_me_names_the_member_and_never_the_token(world: World) -> None:
    me = await world.client.get("/members/me", token="ann-token")

    assert me.body == {"id": str(world.ann.id), "name": "Ann"}


async def test_the_table_keeps_a_digest_of_the_token_never_the_token(
    world: World,
) -> None:
    assert world.ann.token_hash == hash_token("ann-token") != "ann-token"
    assert len(world.ann.token_hash) == 64


async def test_a_member_only_sees_and_changes_their_own_tasks(world: World) -> None:
    mine = await create(world, "Mine")
    path = f"/tasks/{mine['id']}"

    ben = "ben-token"
    assert (await world.client.get(path, token=ben)).status == 404
    assert (await world.client.post(f"{path}/complete", token=ben)).status == 404
    assert (await world.client.delete(path, token=ben)).status == 404
    assert (await world.client.get("/tasks", token=ben)).body["items"] == []
    assert "owner_id" not in mine  # the owner is the caller, never a field


async def test_completing_twice_is_a_conflict_and_done_tasks_are_kept(
    world: World,
) -> None:
    task = await create(world, "Ship")
    path = f"/tasks/{task['id']}"
    ann = "ann-token"

    done = await world.client.post(f"{path}/complete", token=ann)
    again = await world.client.post(f"{path}/complete", token=ann)
    removed = await world.client.delete(path, token=ann)

    assert (done.status, done.body["done"]) == (200, True)
    assert (again.status, again.body["error"]) == (409, "task_already_done")
    assert (removed.status, removed.body["error"]) == (409, "done_tasks_are_kept")


async def test_an_open_task_can_be_deleted(world: World) -> None:
    task = await create(world, "Temporary")

    gone = await world.client.delete(f"/tasks/{task['id']}", token="ann-token")

    assert gone.status == 204


async def test_invalid_input_is_refused(world: World) -> None:
    empty = await world.client.post("/tasks", json={"title": ""}, token="ann-token")
    wild = await world.client.post(
        "/tasks", json={"title": "x", "priority": 9}, token="ann-token"
    )

    assert (empty.status, wild.status) == (422, 422)


async def test_the_project_middleware_marks_every_matched_route(
    world: World,
) -> None:
    reply = await world.client.get("/health")

    assert reply.headers["x-tracker-version"] == "1"
