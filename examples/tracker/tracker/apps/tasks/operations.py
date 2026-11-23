"""Business operations: plain async functions, no Request, no HTTP types.

Each takes the acting member's id and plain values, so an action, a CLI or a
test can call it. Row permission lives in the *statement*: the owner is part of
every UPDATE/DELETE, so there is no read-check-write gap and another member's
task looks exactly like a missing one.
"""

from uuid import UUID

from hinoki import events
from hinoki.events import Updated
from hinoki.orm import QuerySet
from hinoki.utils import utc_now

from .errors import DoneTasksAreKept, TaskAlreadyDone
from .models import Task


def mine(owner_id: UUID) -> QuerySet[Task]:
    return Task.filter(owner_id=owner_id)


async def complete_task(owner_id: UUID, task_id: UUID) -> Task:
    """Mark an own open task done in one statement."""
    changed = await Task.aupdate_where(
        {"id": task_id, "owner_id": owner_id, "done": False},
        {"done": True, "completed_at": utc_now()},
    )
    if changed:
        # aupdate_where emits no model event, so announce the change ourselves.
        await events.emit(Updated(Task, changed[0], ("done", "completed_at")))
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
