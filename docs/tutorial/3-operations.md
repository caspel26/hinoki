# 3. Operations

The rules of the application live in plain async functions that take the acting member's id and plain values. No `Request`, no HTTP types: an action, a CLI, a test or an admin hook can all call them.

```python
async def list_tasks(owner_id: UUID, page: PageParams) -> Page[Task]:
    return await mine(owner_id).apaginate(page)   # Task is @ordered_by newest first


async def create_task(owner_id: UUID, data: TaskIn) -> Task:
    return await Task.acreate(data, owner_id=owner_id)   # the server's value wins


async def complete_task(owner_id: UUID, task_id: UUID) -> Task:
    """Mark an own open task done in one statement."""
    changed = await Task.aupdate_where(
        {"id": task_id, "owner_id": owner_id, "done": False},
        {"done": True, "completed_at": now()},
    )
    if changed:
        return changed[0]
    await mine(owner_id).filter(id=task_id).aget()   # missing or not yours: 404
    raise TaskAlreadyDone()
```

The important line is the `WHERE`: the owner is part of the statement. There is no "read the row, check the owner, then update" gap, and another member's task looks exactly like a missing one (404), so nothing leaks.

The business failures are named after the rule they report:

```python
class TaskAlreadyDone(Conflict):
    code = "task_already_done"
    message = "The task is already done."
```

Each error carries its HTTP status (409 here) and a stable code.

Next: [4. Endpoints](4-endpoints.md).
