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

Request path, query and body values are validated before the handler runs; a Schema body uses `Schema.validate()` and reports nested field errors with its parameter prefix. Discriminated request variants belong in a wrapper Schema field (`Annotated[Cat | Dog, Field(discriminator="kind")]`); bare union body parameters are not supported. Hinoki does not coerce input values into different types. Return annotations describe OpenAPI and output serialization. When the annotation is one concrete Schema (also inside `Annotated[...]`), a directly returned Model is projected through that Schema, and an instance of a subclass is sent with only the declared Schema's fields; a row that fails the Schema's constraints is a `500 internal_error`, not a client error. Other response values, such as a returned `dict`, are sent unchanged and are not validated at runtime ([details](model-schemas.md#return-a-model-from-a-schema-annotated-handler)). Use a wrapper Schema for a discriminated response union too; bare union return annotations are rejected by OpenAPI generation.

## Upload files

Bind a file by the multipart field name. An action that declares a file parameter and no `parsers=` gets `MultiPartParser()` automatically, with its default limits, unless the application's `parsers=` include one; pass `parsers=[MultiPartParser(...)]` to change them. `UploadedFile` contains the client filename, claimed content type and in-memory `bytes`; it does not save files or validate their content.

```python
from hinoki import Hinoki, MultiPartParser, Request, UploadedFile, http


@http.post(
    "/documents",
    status=201,
    parsers=[MultiPartParser(max_parts=4, max_part_bytes=512_000)],
)
async def create_document(
    request: Request, document: UploadedFile
) -> dict[str, object]:
    title = request.POST.get("title", "")
    # Validate content and choose a generated storage name before saving.
    return {"title": title, "filename": document.filename, "size": document.size}


@http.post("/batch")  # MultiPartParser() selected automatically
def create_batch(documents: list[UploadedFile]) -> dict[str, int]:
    return {"count": len(documents)}


app = Hinoki(include=[create_document, create_batch])
```

`UploadedFile` requires exactly one part with that name. `list[UploadedFile]` requires at least one and keeps repeated parts in order. To make a single file optional, write `file: UploadedFile | None = None`. These declarations cannot be combined with a `Schema` body parameter. Use `request.POST` for adjacent text fields or `request.FILES` when names/repetitions are dynamic. Parser precedence for a file action: its own `parsers=` (which must then include `MultiPartParser`, or registration fails), then an application-wide `Hinoki(parsers=[...])` that includes a `MultiPartParser`, then the automatic `MultiPartParser()`. An application parser list without multipart does not apply to file actions, and actions without file parameters keep their parsers.

All declared files require `multipart/form-data`. A missing file is a `422` validation error; a wrong media type is `415`; malformed multipart is `400`; a part or complete body over its configured limit is `413 payload_too_large`. A zero-byte body with no `Content-Type` is treated as absent input for optional files; an explicitly multipart empty body is malformed, while a valid closing-boundary-only body has no parts. The default complete-body limit is 1 MiB, and the parser buffers bytes in memory. This API is for bounded small uploads, not streaming. Concurrent requests can multiply memory use. Keep limits conservative, never use the supplied filename as a filesystem path, and verify content instead of trusting the declared media type. See [parsers and renderers](codecs.md#upload-files) and [testing uploads](testing.md#test-file-uploads).

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

A project created by `hinoki new` does not list its routes in `main.py`. The manifest (`hinoki.toml`) names them, and the application the manifest declares binds them when it is built without `include=`:

```toml
[project]
application = "notes.main:app"
routes = "notes.routes:routes"          # the project's own routes (a list)

[[apps]]
name = "billing"
models = "notes.apps.billing.models"
migrations = "notes.apps.billing.migrations"
routes = "notes.apps.billing.routes:routes"   # mounted under prefix
prefix = "/billing"                     # optional: "/<name>" when omitted
```

The project's routes come first, then each app's, in dependency order, each app under its prefix. Three rules keep this free of surprises: only the module named by `project.application` binds the manifest (a `Hinoki()` in a test or script next to the project stays empty); a `Hinoki(include=[...])` you write yourself wins and the manifest is ignored (`include=[]` is an explicit empty list); and `app.include(...)` still adds routes by hand, for example a feature that only exists when a setting is on. A route module that cannot be imported, or whose `routes` is not a list, fails when the application is built. Projects without these keys behave as before.

When the ordinary list/retrieve/create/update/delete routes fit a model,
`CrudAPI` can generate them from a model and schemas declared on one API class.
Mount that class once in `Hinoki`; generated and handwritten decorated actions
can share it. Start with
[Generate CRUD routes](crud.md); use handwritten actions for domain workflows
or to replace an individual generated action.

Give each feature one module that owns its routes and exports them as a list, with paths relative to the feature. Choose where each feature lives in the root declaration with `Mount`, so the paths stay visible in one place:

```python
# tasks/routes.py (paths are relative: "/" and "/{task_id}")
from hinoki import Includable

routes: list[Includable] = [tasks_group, TaskReports, export_tasks]

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

For a versioned API, let each feature module export its routes and compose them
under one version mount. Each CRUD class keeps its resource path; the version
prefix is declared once in `api/v1.py`:

```python
# api/people.py
from hinoki import CrudAPI

from app.models import Person
from app.schemas import PersonOut


class People(CrudAPI, model=Person, path="/people"):
    read_schema = PersonOut


# Each feature exports a list of routes relative to that feature.
routes = [People]
```

```python
# api/teams.py
from hinoki import CrudAPI

from app.models import Team
from app.schemas import TeamOut


class Teams(CrudAPI, model=Team, path="/teams"):
    read_schema = TeamOut


routes = [Teams]
```

```python
# api/v1.py
from hinoki import Mount
from app.api import people, teams

routes = Mount("/api/v1", people.routes, teams.routes)
```

```python
# app.py
from hinoki import Hinoki
from app.api import v1

app = Hinoki(include=[v1.routes])
```

The resulting paths include `/api/v1/people`,
`/api/v1/people/{item_id}`, `/api/v1/teams`, and
`/api/v1/teams/{item_id}`. Add `api/v2.py` with its own `Mount("/api/v2", ...)`
when a new version is needed. Feature route lists and mixins can be shared
where their behavior is genuinely the same. Route collisions are still checked
after prefixes are composed.

`Includable` is the type of everything `include` accepts: an `API` class, a decorated function, a `RouteGroup`, a `Mount`, an object with an `api()` method (the admin, which is included as it is) or a list or tuple of those. Annotate a feature's list with it (`routes: list[Includable]`) and the type checker rejects anything else. `include` spreads nested lists and tuples in place, so order is the order you wrote. `Mount(prefix, *items)` prepends its prefix to every route it holds; mounts nest and prefixes join from the outside in. The prefix starts with `/` and does not end with one. Items you do not mount keep their own paths, so small apps can stay a flat list. Mounting changes only the path: access, errors and transactions are unchanged, and a route that ends up at the same method and path as another still fails at declaration time. A `RouteGroup` reached twice, an item that is not an action, class or group, or a module passed instead of its list is refused and named; a module gets a hint to export a list.

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

Return a value matching the declared response type, or return `Response` when the handler needs to set an explicit status, headers or body. Declare expected `APIError` subclasses in `errors=(...)`; their status, stable code and message feed the OpenAPI document and strict test client.

### The response is checked against the annotation

Every action's return value is checked against its return annotation, and the annotation itself is checked when the app starts. There is nothing to switch on.

```python
@http.get("/notes/{note_id}")
async def detail(note_id: int) -> NoteOut:
    return {"id": note_id}          # a dict is not a NoteOut: 500 internal_error
```

- **At startup.** An annotation that cannot be checked stops the app with a `RegistrationError` that names the action: no annotation, `Any`, `object`, `set[...]`, a plain class, or `Response` inside a collection. Annotate a concrete type, or declare the action with `validate_response=False` to skip the check for that action.
- **At runtime.** A result that does not match is a `500 internal_error`, because the fault is the server's, not the client's. With `debug=True` (development) the answer names the action, the type it returned and the annotation it broke, for example `detail returned a dict that does not match its return annotation NoteOut`. Without debug the body is generic. The log and the answer never contain the value itself. With `transaction=True` the check runs inside the transaction, so a mismatch rolls the write back.
- **What is accepted.** A `Schema` class, a `Model` class (projected through the declared `Schema` when there is one), `list[...]`, `dict[str, ...]` and `Page[...]` of these, `str`, `int`, `float`, `bool` (a `bool` is not an `int`), `bytes`, `datetime`, `date`, `UUID`, `Decimal`, `None`, plain `dict` and `list`, `Any` or `object` as an item type, and unions of these. `Annotated[...]` is unwrapped. The check never coerces.
- **`Response`.** `-> Response` and `NoteOut | Response` are accepted: a returned `Response` carries its own status, headers and body and is not checked. Actions with a bodyless status (`204`, `304`) are not checked.
- **What it does not check.** A `Schema` instance is valid by construction, so the check is about the shape of the result, not the field values. A custom renderer or a `dump()` override can still change the bytes afterwards. The check is one type test per request; no performance figure is claimed.

Use `Response` from `hinoki` or `hinoki.http` for explicit status, headers or body. It is also available from `hinoki.http.response`. The former `Reply` name and `hinoki.reply` module are deprecated aliases kept for one compatibility cycle: `from hinoki import Reply` and `import hinoki.reply` still work, refer to the same `Response` class and emit `DeprecationWarning`. Replace their imports, annotations and constructors with `Response`.

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
