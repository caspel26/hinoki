"""The layered tracker: operations tested directly, then through HTTP, then rules."""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from examples.tracker import operations
from examples.tracker.app import create_app
from examples.tracker.errors import DoneTasksAreKept, TaskAlreadyDone
from examples.tracker.models import Member, Task
from examples.tracker.schemas import TaskIn
from hinoki.__main__ import main
from hinoki.db import Database
from hinoki.orm import migrator, use_database
from hinoki.testing import ASGIClient
from tests.harness import Client, Pool

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(URL is None, reason="no database")
MIGRATIONS = Path(__file__).parent.parent / "examples" / "tracker" / "migrations"
DROP = (
    "DROP TABLE IF EXISTS tracker_tasks",
    "DROP TABLE IF EXISTS tracker_members",
    "DROP TABLE IF EXISTS hinoki_migrations",
)
JSON = "application/json"
# ruff: noqa: S106 - fixture tokens, not secrets


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
async def members() -> AsyncIterator[tuple[Member, Member]]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2)
    for statement in DROP:
        await database.execute(statement)
    await migrator.apply(database, MIGRATIONS)
    with use_database(database):
        yield (
            await Member.acreate(name="Ann", token="ann-token"),
            await Member.acreate(name="Ben", token="ben-token"),
        )
    for statement in DROP:
        await database.execute(statement)
    database.close()


@needs_database
async def test_operations_run_without_any_http(members: tuple[Member, Member]) -> None:
    ann, ben = members
    task = await operations.create_task(ann.id, TaskIn(title="write docs"))
    assert task.owner_id == ann.id and not task.done
    assert (await operations.complete_task(ann.id, task.id)).done
    with pytest.raises(TaskAlreadyDone):
        await operations.complete_task(ann.id, task.id)
    with pytest.raises(DoneTasksAreKept):
        await operations.delete_task(ann.id, task.id)
    # another member's task is indistinguishable from a missing one
    for other in (ben.id, uuid4()):
        with pytest.raises(Task.NotFound):
            await operations.complete_task(other, task.id)
        with pytest.raises(Task.NotFound):
            await operations.delete_task(other, task.id)


@needs_database
async def test_the_http_layer_maps_operations_to_responses(
    members: tuple[Member, Member], pool: Pool
) -> None:
    assert URL is not None
    client: Client = await pool.start(create_app(URL))
    made = await client.request(
        "POST",
        "/tasks",
        json.dumps({"title": "ship it", "owner_id": str(uuid4())}).encode(),
        content_type=JSON,
        headers=bearer("ann-token"),
    )
    assert made.status == 422  # unknown fields are refused: the owner is never input
    made = await client.request(
        "POST",
        "/tasks",
        b'{"title": "ship it"}',
        content_type=JSON,
        headers=bearer("ann-token"),
    )
    assert made.status == 201
    task_id = made.body["id"]
    path = f"/tasks/{task_id}/complete"
    assert (
        await client.request("POST", path, headers=bearer("ben-token"))
    ).status == 404
    assert (
        await client.request("POST", path, headers=bearer("ann-token"))
    ).status == 200
    again = await client.request("POST", path, headers=bearer("ann-token"))
    assert (again.status, again.body["error"]) == (409, "task_already_done")
    listing = await client.request("GET", "/tasks", headers=bearer("ben-token"))
    assert listing.status == 200 and listing.body["items"] == []
    assert (await client.request("GET", "/tasks")).status == 401
    assert (await client.request("GET", "/health")).status == 200


def test_the_tracker_keeps_its_layering_promises(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRACKER_DATABASE_URL", "postgres://unused")
    argv = [
        "check",
        "--app",
        "examples.tracker.app:create_app",
        "--rules",
        "examples.tracker.rules:tracker_rules",
        "--package",
        "examples",
    ]
    assert main(argv) == 0
    assert "0 violated" in capsys.readouterr().out


@needs_database
async def test_every_business_error_of_the_tracker_is_declared(
    members: tuple[Member, Member],
) -> None:
    assert URL is not None
    async with ASGIClient(create_app(URL), strict_errors=True) as client:
        made = await client.post("/tasks", json={"title": "x"}, token="ann-token")
        path = f"/tasks/{made.body['id']}"
        assert (await client.post(f"{path}/complete", token="ben-token")).status == 404
        assert (await client.post(f"{path}/complete", token="ann-token")).status == 200
        again = await client.post(f"{path}/complete", token="ann-token")
        assert again.status == 409
        assert (await client.delete(path, token="ann-token")).status == 409
        assert (await client.delete(path, token="ben-token")).status == 404
