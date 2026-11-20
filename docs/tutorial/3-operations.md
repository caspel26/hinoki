---
status: implemented
related:
  - patterns.md
  - errors.md
---

# 3. Operations

The rules of the application live in plain async functions. They take the
acting member's id and plain values, never a `Request`, so an endpoint, a
command, a test or an admin hook can call them.

```python
# tracker/operations.py
from uuid import UUID

from hinoki.orm import QuerySet
from hinoki.pagination import Page, PageParams
from hinoki.utils import utc_now

from .errors import DoneTasksAreKept, TaskAlreadyDone
from .models import Task
from .schemas import TaskIn


def mine(owner_id: UUID) -> QuerySet[Task]:
    return Task.filter(owner_id=owner_id)


async def list_tasks(owner_id: UUID, page: PageParams) -> Page[Task]:
    return await mine(owner_id).apaginate(page)  # Task is @ordered_by newest first


async def create_task(owner_id: UUID, data: TaskIn) -> Task:
    return await Task.acreate(data, owner_id=owner_id)  # the server's value wins
```

Writes put the owner in the statement itself:

```python
# tracker/operations.py (continued)
async def complete_task(owner_id: UUID, task_id: UUID) -> Task:
    """Mark an own open task done in one statement."""
    changed = await Task.aupdate_where(
        {"id": task_id, "owner_id": owner_id, "done": False},
        {"done": True, "completed_at": utc_now()},
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
```

There is no gap between reading a row, checking its owner and writing it.
Another member's task looks exactly like a missing one (404), so nothing leaks.

Business failures are named after the rule they report. Each carries its HTTP
status (409 for `Conflict`) and a stable code:

```python
# tracker/errors.py
from hinoki import errors


class TaskAlreadyDone(errors.Conflict):
    code = "task_already_done"
    message = "The task is already done."


class DoneTasksAreKept(errors.Conflict):
    code = "done_tasks_are_kept"
    message = "Completed tasks are kept as a record and cannot be deleted."
```

## Recap

- Operations hold the rules and know nothing about HTTP.
- Ownership is part of the `WHERE` clause, not a separate check.
- Errors are classes with a status and a stable code.

Next: [4. Endpoints](4-endpoints.md).
