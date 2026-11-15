---
status: implemented
related:
  - authentication.md
  - errors.md
  - transactions.md
---

# Endpoints

Hinoki endpoints are ordinary Python functions decorated with an HTTP method. The decorator records metadata; `Hinoki.include(...)` registers the function or an `API` class. The registered declarations also supply the generated [OpenAPI document](openapi.md).

## Declare an action

```python
from hinoki import Hinoki, Schema, http


class Greeting(Schema):
    name: str


@http.post("/greetings", status=201)
async def greet(data: Greeting) -> Greeting:
    return data


app = Hinoki(include=[greet])
```

Use `@http.get`, `@http.post`, `@http.put`, `@http.patch`, `@http.delete` or `@http.query` (`from hinoki import http`). Hinoki also answers `HEAD` through each `GET` route: it runs the GET response path and returns the same status and representation headers without a body. The GET handler therefore runs for HEAD requests too. A `405` response's `Allow` header includes `HEAD` whenever `GET` is available. Supported decorator arguments include `status`, `access`, `transaction`, `pagination`, `parsers`, `renderers`, `errors` and `replaces`. Each is checked when the route is declared or registered.

## Read with a body: `@http.query`

`QUERY` is the HTTP method for a read whose input does not fit a query string: it is safe and idempotent like `GET`, and carries a body like `POST`. Declare it the same way as the other methods:

```python
from hinoki import Schema, http


class TaskSearch(Schema):
    text: str
    limit: int = 20


@http.query("/tasks/search")
async def search_tasks(data: TaskSearch) -> list[TaskOutput]:
    rows = await Task.filter(title__contains=data.text).limit(data.limit)
    return TaskOutput.from_models(rows)
```

```sh
curl -X QUERY http://127.0.0.1:8000/tasks/search \
  -H 'content-type: application/json' -d '{"text": "docs"}'
```

* The body is bound, parsed and validated exactly as for `POST`: the same parsers, size limit and `400`/`413`/`415`/`422` answers.
* A `QUERY` action must not change state. Hinoki does not wrap it in a transaction: `RouteGroup(atomic=True)` applies to `POST`, `PUT`, `PATCH` and `DELETE` only. Pass `transaction=True` on the action if the read needs one.
* `RouteGroup.query(...)` and `@http.query` inside an `API` class work like the other methods. A `405` answer lists `QUERY` in `Allow` where it is available. `HEAD` is answered through `GET` routes only.
* Hinoki sends no `Accept-Query` or cache headers by itself; add them to the response where they matter.
* The method is recent. Granian passes it through to Hinoki over ASGI and WSGI (both are tested), but a proxy, gateway, firewall or HTTP client in front of your service may reject a method it does not know, and browsers send a CORS preflight for it. Check the path your traffic takes, and keep a `GET` or `POST` route beside it when clients cannot send `QUERY`.
* In the OpenAPI document a `QUERY` operation appears under the path's `x-query` field; see [OpenAPI](openapi.md#query-operations).

## Bind request values

Typed path parameters are taken from the path, query parameters from the query string, and a body parameter from the request body. A handler may declare `request: Request` as its first parameter when it needs headers, cookies or raw request data. The request is injected only when that parameter is explicitly annotated.

```python
from uuid import UUID

from hinoki import Request, Schema
from hinoki import http


class TaskView(Schema):
    id: UUID
    locale: str


@http.get("/tasks/{task_id}")
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

from hinoki import API, Schema
from hinoki import http


class TaskInput(Schema):
    title: str


class TaskOutput(Schema):
    id: UUID
    title: str


class Tasks(API, path="/tasks"):
    @http.get("/")
    async def listing(self) -> list[TaskOutput]:
        return []

    @http.post("/", status=201)
    async def create(self, data: TaskInput) -> TaskOutput:
        return TaskOutput(id=uuid4(), title=data.title)
```

Then the application can include `Tasks` as one item. The path prefix is visible
on the class; the full paths remain beside their actions. Use `RouteGroup` when
functions are a better fit, especially when several actions share access,
transaction defaults, or declared errors. A group must contain at least one
action and becomes immutable after successful inclusion.

## Organising a large application

When the ordinary list/retrieve/create/update/delete routes fit a model,
`CrudAPI` can generate them from a model and schemas declared on one API class.
Mount that class once in `Hinoki`; generated and handwritten decorated actions
can share it. Start with
[Generate CRUD routes](crud.md); use handwritten actions for domain workflows
or to replace an individual generated action.

Give each feature one module that owns its routes and exports them as a list, with paths relative to the feature. Choose where each feature lives in the root declaration with `Mount`, so the paths stay visible in one place:

```python
# tasks/routes.py (paths are relative: "/" and "/{task_id}")
routes = [tasks_group, TaskReports, export_tasks]

# main.py
from hinoki import Hinoki, Mount
from billing.routes import routes as billing
from tasks.routes import routes as tasks

app = Hinoki(
    include=[
        health,
        Mount("/tasks", tasks),
        Mount("/billing", billing),
    ]
)
```

`include` spreads nested lists and tuples in place, so order is the order you wrote. `Mount(prefix, *items)` prepends its prefix to every route it holds; mounts nest and prefixes join from the outside in. The prefix starts with `/` and does not end with one. Items you do not mount keep their own paths, so small apps can stay a flat list. Mounting changes only the path: access, errors and transactions are unchanged, and a route that ends up at the same method and path as another still fails at declaration time. A `RouteGroup` reached twice, an item that is not an action, class or group, or a module passed instead of its list is refused and named; a module gets a hint to export a list.

If a feature declares its own prefix (a `RouteGroup("/tasks")` or `API, path="/tasks"`), mounting adds to it. Pick one place for each prefix: feature-relative paths with `Mount` at the root is the layout that keeps the whole map readable in `main.py`.

To see the resulting map without starting the server, run `hinoki routes blog.main:create_app --path /posts` (the blog example):

```text
GET     /posts                    public                   blog.api.Posts.listing
POST    /posts                    TokenAuth[posts:write]   hinoki.crud.create
DELETE  /posts/{item_id}          TokenAuth[posts:write]   hinoki.crud.delete
GET     /posts/{item_id}          public                   blog.api.Posts.one
POST    /posts/{item_id}/publish  TokenAuth[posts:write]   blog.api.Posts.publish
```

Use `--path` or `--method` to narrow it, and `--format json` for scripts. The list is sorted by path.

## Responses, errors and pagination

Return a value matching the declared response type, or return `Reply` when the handler needs to set an explicit status, headers or body. Declare expected `APIError` subclasses in `errors=(...)`; their status, stable code and message feed the OpenAPI document and strict test client.

Paginated actions declare a pagination policy. See [pagination](pagination.md) for the query parameters and response envelope.

See [authentication](authentication.md), [parsers and renderers](codecs.md), and [error handling](errors.md).

## Migrating from the top-level decorators

Earlier revisions exported `get`, `post`, `put`, `patch` and `delete` from `hinoki`. They now live in the `hinoki.http` namespace:

```python
# before
from hinoki import get

@get("/tasks")
async def listing() -> list[TaskOut]: ...

# now
from hinoki import http

@http.get("/tasks")
async def listing() -> list[TaskOut]: ...
```

`from hinoki import get` raises `ImportError`. If a module also needs the standard library's `http` (for `HTTPStatus`), import it explicitly: `from http import HTTPStatus`.
