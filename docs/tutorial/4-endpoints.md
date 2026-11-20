---
status: implemented
related:
  - endpoints.md
  - authentication.md
  - transactions.md
---

# 4. Endpoints

The HTTP layer maps requests to operations and results to responses. It holds
no business rule and no SQL.

First, who may call. A bearer token resolves to a `Member`, and the policy
names the permission that every task action requires:

```python
# tracker/security.py
from hinoki import Access, BearerAuth, Caller, Permission

from .models import Member


class TrackerPermission(Permission):
    USE = "tasks:use"


class MemberTokens(BearerAuth[Member]):
    async def resolve_token(self, token: str) -> Caller[Member] | None:
        member = await Member.aget_or_none(token=token)
        if member is None:
            return None
        return Caller(
            id=str(member.id), user=member, permissions={TrackerPermission.USE}
        )


member_only = Access(auth=MemberTokens(), permissions=[TrackerPermission.USE])
```

Then the actions. A `RouteGroup` states the path prefix, the policy and the
transaction once:

```python
# tracker/api.py
from uuid import UUID

from hinoki import Caller, RouteGroup, errors
from hinoki.pagination import Page, PageNumberPagination, PageParams

from . import operations
from .errors import DoneTasksAreKept, TaskAlreadyDone
from .models import Member
from .schemas import TaskIn, TaskOut
from .security import member_only

tasks = RouteGroup("/tasks", access=member_only, atomic=True)


@tasks.get("/", pagination=PageNumberPagination(page_size=10, max_page_size=50))
async def list_tasks(page: PageParams, caller: Caller[Member]) -> Page[TaskOut]:
    """Your tasks, newest first."""
    return (await operations.list_tasks(caller.user.id, page)).map(TaskOut.from_model)


@tasks.post("/", status=201)
async def create_task(data: TaskIn, caller: Caller[Member]) -> TaskOut:
    return TaskOut.from_model(await operations.create_task(caller.user.id, data))
```

```python
# tracker/api.py (continued)
@tasks.post("/{task_id}/complete", errors=(errors.NotFound, TaskAlreadyDone))
async def complete_task(task_id: UUID, caller: Caller[Member]) -> TaskOut:
    return TaskOut.from_model(await operations.complete_task(caller.user.id, task_id))


@tasks.delete("/{task_id}", status=204, errors=(errors.NotFound, DoneTasksAreKept))
async def delete_task(task_id: UUID, caller: Caller[Member]) -> None:
    await operations.delete_task(caller.user.id, task_id)
```

- **`access=`** names a reusable policy. Actions are public unless they name
  one, so protect on purpose; a rule in the next chapter enforces it.
- **`atomic=True`** runs each write in a transaction. It commits after the
  response is encoded and rolls back before an error becomes HTTP.
- **`errors=(...)`** declares what an action may raise. The OpenAPI document,
  the MCP tool description and the strict test client all read it.
- **`caller: Caller[Member]`** is the authenticated caller, typed:
  `caller.user` is your `Member`.
- **`TaskOut.from_model(...)`** converts a row into the response. A handler
  never returns a model; lists and pages use `.map(...)`.

Declare `request: Request` as the first parameter only when you need headers,
cookies or the raw body. File uploads are covered in
[upload files](../endpoints.md#upload-files).

## Register the routes

In a project, `routes.py` lists the groups and `hinoki.toml` mounts them
([chapter 8](8-projects.md)). Tracker's factory includes `tasks` directly,
because `examples/` hosts three applications ([chapter 6](6-run.md)).

## Recap

- A `RouteGroup` states access, transactions and the prefix once.
- Handlers call operations and convert rows with `from_model`.
- Every error an action raises is declared.

Next: [5. Rules](5-rules.md). In the guides: [function-based APIs](../function-endpoints.md) and [class-based APIs](../class-endpoints.md).
