---
status: implemented
related:
  - patterns.md
  - errors.md
---

# 4. Implement a business rule

Generated CRUD ([chapter 3](4-endpoints.md)) already handles listing, retrieving,
creating and deleting rows. When an action encodes a domain rule — such as
marking an open task done or refusing to delete a completed one — put that rule
in `operations.py` instead of writing SQL or state checks directly inside the
HTTP handler:

- **Reuse across entry points**: plain `async def` functions take `owner_id` and
  plain values, never a `Request`, so an endpoint, a management command, a test
  or an admin hook can call the same rule.
- **Layer isolation**: `api.py` stays responsible only for HTTP shapes, status
  codes, `Access` policies and `atomic=True` transaction boundaries, while
  [chapter 6](5-rules.md) enforces that `operations.py` never imports HTTP types.
- **Single-statement ownership**: writes include the owner in the `WHERE` clause
  so there is no race between reading a row, checking ownership and updating it.

It starts with one scoped query that every action reuses:

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

## Recap

- Business rules such as `complete_task(...)` belong in `operations.py`,
  separate from HTTP handlers in `api.py`.
- Ownership is part of the `WHERE` clause, not a separate check.
- Errors are classes with a status and a stable code.

Next: [5. Events between apps](events.md).
