---
status: implemented
related:
  - patterns.md
  - errors.md
---

# 3. Operations

The rules of the application live in plain async functions. They take the
acting member's id and plain values, never a `Request`, so an endpoint, a
command, a test or an admin hook can call them. The list, retrieve and create
of tasks are generated ([chapter 4](4-endpoints.md)); what remains for
`operations.py` is what the generator cannot know. It starts with one query
that every action reuses:

```python
# tracker/apps/tasks/operations.py
from uuid import UUID

from hinoki import events
from hinoki.events import Updated
from hinoki.orm import QuerySet
from hinoki.utils import utc_now

from .errors import DoneTasksAreKept, TaskAlreadyDone
from .models import Task


def mine(owner_id: UUID) -> QuerySet[Task]:
    return Task.filter(owner_id=owner_id)
```

Writes put the owner in the statement itself:

```python
# tracker/apps/tasks/operations.py (continued)
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
```

There is no gap between reading a row, checking its owner and writing it.
Another member's task looks exactly like a missing one (404), so nothing leaks.

Business failures are named after the rule they report. Each carries its HTTP
status (409 for `Conflict`) and a stable code:

```python
# tracker/apps/tasks/errors.py
from hinoki import errors


class TaskAlreadyDone(errors.Conflict):
    code = "task_already_done"
    message = "The task is already done."


class DoneTasksAreKept(errors.Conflict):
    code = "done_tasks_are_kept"
    message = "Completed tasks are kept as a record and cannot be deleted."
```

## Events between apps

Creating a task and completing one both leave a line in the member's activity
feed, but `tasks` does not know the feed exists. It announces: generated create
emits `Created[Task]`, and `complete_task` emits `Updated[Task]` itself because
`aupdate_where` emits no model event. The `activity` app listens:

```python
# tracker/apps/activity/events.py
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
```

`in_transaction` runs the handler inside the transaction of the change, so the
task and its activity line commit together or not at all. The handlers are
registered by the `events` entry of the `activity` app in `hinoki.toml`
([chapter 6](6-run.md)), and one command lists them:

```sh
uv run hinoki events
```

```text
Created[Task]  in_transaction  activity     async  tracker.apps.activity.events.task_created
Updated[Task]  in_transaction  activity     async  tracker.apps.activity.events.task_updated

limits: events.handler_timeout=5.0, events.max_handlers=16, events.max_depth=4
declared in hinoki.toml: activity -> tracker.apps.activity.events:handlers
```

The dependency points from `activity` to `tasks` only, which is also why
`activity` lists `requires = ["tasks", "members"]`. [Chapter 5](5-rules.md)
makes the other direction a failing check. See [events](../events.md) for
`after_commit` handlers and the limits.

## Recap

- Operations hold the rules and know nothing about HTTP.
- Ownership is part of the `WHERE` clause, not a separate check.
- Errors are classes with a status and a stable code.
- An app announces what happened; another app listens, without an import in
  the other direction.

Next: [4. Endpoints](4-endpoints.md).
