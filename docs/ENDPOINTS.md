# Endpoints

Hinoki endpoints are ordinary Python functions decorated with an HTTP method. The decorator records metadata; `Hinoki.include(...)` registers the function or an `API` class.

## Declare an action

```python
from hinoki import NotFound, post


@post("/tasks", status=201, transaction=True, errors=(NotFound,))
async def create_task(data: TaskInput) -> TaskOutput:
    row = await Task.acreate(data)
    return TaskOutput.from_model(row)
```

Use `@get`, `@post`, `@put`, `@patch` or `@delete`. Supported arguments include `status`, `access`, `transaction`, `pagination`, `parsers`, `renderers`, `errors` and `replaces`. Each is checked when the route is declared or registered.

## Bind request values

Typed path parameters are taken from the path, query parameters from the query string, and a body parameter from the request body. A handler may declare `request: Request` as its first parameter when it needs headers, cookies or raw request data. The request is injected only when that parameter is explicitly annotated.

```python
from uuid import UUID

from hinoki import Request, Schema, get


class TaskView(Schema):
    id: UUID
    locale: str


@get("/tasks/{task_id}")
async def get_task(request: Request, task_id: UUID) -> TaskView:
    locale = request.headers.get("accept-language", "en")
    return TaskView(id=task_id, locale=locale)
```

Validation errors are returned before the handler runs. Hinoki does not coerce input values into different types.

## Group related paths

For plain functions that share a prefix or defaults, use `RouteGroup`. The group
collects its handlers and can be included once as a unit:

```python
from uuid import UUID, uuid4

from hinoki import Hinoki, RouteGroup, Schema


class TaskInput(Schema):
    title: str


class TaskOutput(Schema):
    id: UUID
    title: str


tasks = RouteGroup("/tasks", atomic=True)


@tasks.get("/")
async def listing() -> list[TaskOutput]:
    return []


@tasks.post("/", status=201)
async def create(data: TaskInput) -> TaskOutput:
    return TaskOutput(id=uuid4(), title=data.title)


app = Hinoki(include=[tasks])
```

When composing a group action with another decorator, put the HTTP decorator
outside it. The group records the callable when `@tasks.get` runs:

```python
@tasks.get("/audited")
@audit
async def audited() -> str:
    return "ok"
```

For a standalone function passed directly to `Hinoki.include`, the final
decorated function is dispatched, so either order works with wrappers that use
`functools.wraps`.

For class-based APIs, put a shared path on the class and keep actions together:

```python
from uuid import UUID, uuid4

from hinoki import API, Schema, get, post


class TaskInput(Schema):
    title: str


class TaskOutput(Schema):
    id: UUID
    title: str


class Tasks(API, path="/tasks"):
    @get("/")
    async def listing(self) -> list[TaskOutput]:
        return []

    @post("/", status=201)
    async def create(self, data: TaskInput) -> TaskOutput:
        return TaskOutput(id=uuid4(), title=data.title)
```

Then the application can include `Tasks` as one item. The path prefix is visible
on the class; the full paths remain beside their actions. Use `RouteGroup` when
functions are a better fit, especially when several actions share access,
transaction defaults, or declared errors. A group must contain at least one
action and becomes immutable after successful inclusion.

## Responses, errors and pagination

Return a value matching the declared response type, or return `Reply` when the handler needs to set an explicit status, headers or body. Declare expected `APIError` subclasses in `errors=(...)`; their status, stable code and message feed the OpenAPI document and strict test client.

Paginated actions declare a pagination policy. See [pagination](PAGINATION.md) for the query parameters and response envelope.

See [authentication](AUTHENTICATION.md), [parsers and renderers](CODECS.md), and [error handling](ERRORS.md).
