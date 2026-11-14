# 4. Endpoints

The HTTP layer maps requests to operations and operations to responses. It contains no business rule and no SQL.

```python
tasks = RouteGroup("/tasks", access=member_only, atomic=True)   # stated once


@tasks.get("/", pagination=PageNumberPagination())
async def list_tasks(page: PageParams, caller: Caller[Member]) -> Page[TaskOut]:
    """Your tasks, newest first."""
    return (await operations.list_tasks(caller.user.id, page)).map(out)


@tasks.post("/", status=201)
async def create_task(data: TaskIn, caller: Caller[Member]) -> TaskOut:
    return out(await operations.create_task(caller.user.id, data))


@tasks.post("/{task_id}/complete", errors=(NotFound, TaskAlreadyDone))
async def complete_task(task_id: UUID, caller: Caller[Member]) -> TaskOut:
    return out(await operations.complete_task(caller.user.id, task_id))


app = Hinoki(include=[tasks])
```

* **Access** is a reusable policy (`member_only`) naming the provider that authenticates (a bearer token here) and the permissions required. Public is the default, so protect on purpose; a rule below enforces it.
* **`atomic=True`** wraps writes in a transaction that commits after the response is encoded and rolls back before an error becomes HTTP.
* **`errors=(...)`** declares what an action may raise. The status and code come from each error class; OpenAPI, the MCP tool description and the strict test client all use it.
* **`caller: Caller[Member]`** is who is asking, typed: `caller.user` is your `Member`.
* The `request` object is optional: declare `request: Request` first only when you need headers, cookies or the raw body.

Next: [5. Rules](5-rules.md).
