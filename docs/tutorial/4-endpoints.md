---
status: implemented
---

# 4. Endpoints

The HTTP layer maps requests to operations and operations to responses. It contains no business rule and no SQL.

```python
from uuid import UUID

from tracker import operations
from tracker.errors import DoneTasksAreKept, TaskAlreadyDone
from tracker.models import Member, Task
from tracker.schemas import TaskIn, TaskOut
from tracker.security import member_only
from hinoki import Caller, Hinoki, RouteGroup, errors, http
from hinoki.pagination import Page, PageNumberPagination, PageParams


def out(task: Task) -> TaskOut:
    return TaskOut.from_model(task)


tasks = RouteGroup("/tasks", access=member_only, atomic=True)   # stated once


@tasks.get("/", pagination=PageNumberPagination())
async def list_tasks(page: PageParams, caller: Caller[Member]) -> Page[TaskOut]:
    """Your tasks, newest first."""
    return (await operations.list_tasks(caller.user.id, page)).map(out)


@tasks.post("/", status=201)
async def create_task(data: TaskIn, caller: Caller[Member]) -> TaskOut:
    return await operations.create_task(caller.user.id, data)


@tasks.post("/{task_id}/complete", errors=(errors.NotFound, TaskAlreadyDone))
async def complete_task(task_id: UUID, caller: Caller[Member]) -> TaskOut:
    return await operations.complete_task(caller.user.id, task_id)


@tasks.delete("/{task_id}", status=204, errors=(errors.NotFound, DoneTasksAreKept))
async def delete_task(task_id: UUID, caller: Caller[Member]) -> None:
    await operations.delete_task(caller.user.id, task_id)


app = Hinoki(include=[tasks])
```

* **Access** is a reusable policy (`member_only`) naming the provider that authenticates (a bearer token here) and the permissions required. Public is the default, so protect on purpose; a rule below enforces it.
* **`atomic=True`** wraps writes in a transaction that commits after the response is encoded and rolls back before an error becomes HTTP.
* **`errors=(...)`** declares what an action may raise. The status and code come from each error class; OpenAPI, the MCP tool description and the strict test client all use it.
* **`caller: Caller[Member]`** is who is asking, typed: `caller.user` is your `Member`.
* A handler annotated with one response `Schema` may return a `Model` directly; Hinoki projects it through that Schema. Lists and pages still use explicit mapping.
* The `request` object is optional: declare `request: Request` first only when you need headers, cookies or the raw body.

For bounded file uploads, declare `UploadedFile` or `list[UploadedFile]` parameters and Hinoki selects `MultiPartParser` for that action. The [endpoint guide](../endpoints.md#upload-files) covers optional files, limits and OpenAPI output.

Next: [5. Rules](5-rules.md).
