"""Business operations: plain async functions, no Request, no HTTP types.

Each takes the acting member's id and plain values, so an action, a CLI, a test
or an admin hook can call it. Row permission lives in the *statement*: the
owner is part of every UPDATE/DELETE, so there is no read-check-write gap and
another member's task looks exactly like a missing one.
"""

from uuid import UUID

from examples.tracker.errors import DoneTasksAreKept, TaskAlreadyDone
from examples.tracker.models import Task, now
from examples.tracker.schemas import TaskIn
from hinoki.orm import QuerySet
from hinoki.pagination import Page, PageParams


def mine(owner_id: UUID) -> QuerySet[Task]:
    return Task.filter(owner_id=owner_id)


async def list_tasks(owner_id: UUID, page: PageParams) -> Page[Task]:
    return await mine(owner_id).order_by("-created_at", "-id").apaginate(page)


async def create_task(owner_id: UUID, data: TaskIn) -> Task:
    return await Task.acreate(data, owner_id=owner_id)  # the server's value wins


async def complete_task(owner_id: UUID, task_id: UUID) -> Task:
    """Mark an own open task done in one statement."""
    changed = await Task.aupdate_where(
        {"id": task_id, "owner_id": owner_id, "done": False},
        {"done": True, "completed_at": now()},
    )
    if changed:
        return changed[0]
    await mine(owner_id).filter(id=task_id).aget()  # missing or not yours: 404
    raise TaskAlreadyDone()


async def delete_task(owner_id: UUID, task_id: UUID) -> None:
    """Delete an own open task; completed ones are kept."""
    removed = await Task.adelete(id=task_id, owner_id=owner_id, done=False)
    if removed:
        return
    await mine(owner_id).filter(id=task_id).aget()  # missing or not yours: 404
    raise DoneTasksAreKept()
