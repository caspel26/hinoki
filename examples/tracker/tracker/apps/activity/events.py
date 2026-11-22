"""Handlers for task events: one line of activity per change, in the same
transaction as the change (``in_transaction``), so both commit or neither does.
"""

from hinoki import events
from tracker.apps.tasks.models import Task

from .models import Activity

handlers = events.Handlers("activity")


async def record(task: Task, kind: str) -> None:
    await Activity.acreate(member_id=task.owner_id, task_id=task.id, kind=kind)


@handlers.in_transaction(events.Created[Task])
async def task_created(event: events.Created[Task]) -> None:
    await record(event.instance, "created")


@handlers.in_transaction(events.Updated[Task])
async def task_updated(event: events.Updated[Task]) -> None:
    await record(event.instance, "updated")
