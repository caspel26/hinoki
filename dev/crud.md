# Generate CRUD routes

`crud(...)` generates model-backed routes: list and retrieve always, create, update and delete when you ask for them. Use it as a value in `routes.py` for plain CRUD, or apply it to an `API` class to add hooks and handwritten actions ([class-based APIs](https://hinoki.caspel.dev/dev/class-endpoints.md)).

```python
# notes/apps/billing/routes.py
from hinoki import Includable, crud

from notes.security import billing_staff

from .models import Invoice
from .schemas import InvoiceIn, InvoiceOut, InvoicePatch

routes: list[Includable] = [
    crud(
        Invoice,
        path="/invoices",
        read=InvoiceOut,
        create=InvoiceIn,
        update=InvoicePatch,
        delete=True,
        access=billing_staff,
    ),
]
```

```python
# notes/apps/billing/schemas.py (continued)
@derive(Invoice, only=("reference", "paid"), partial=True)
class InvoicePatch(Schema): ...
```

## Generated actions

| Keyword | Action | Method and path | Success |
| --- | --- | --- | --- |
| always | `list` | `GET /invoices` | `Page[InvoiceOut]` |
| always | `retrieve` | `GET /invoices/{item_id}` | `InvoiceOut` |
| `create=InvoiceIn` | `create` | `POST /invoices` | `201`, `InvoiceOut` |
| `update=InvoicePatch` | `update` | `PATCH /invoices/{item_id}` | `200`, `InvoiceOut` |
| `delete=True` | `delete` | `DELETE /invoices/{item_id}` | `204`, no body |

Writes run in a transaction. `pagination=` selects the pagination policy of the list route: `PageNumberPagination()` (the default) or `CursorPagination()` for keyset [cursor pagination](https://hinoki.caspel.dev/dev/pagination.md#cursor-keyset-pagination). `omit=("list",)` or `omit=("retrieve",)` drops a read so that you can write it yourself. The generated actions are ordinary routes, visible to OpenAPI, `hinoki routes` and the route registry; nothing is registered at import time.

### Access per action

`access=` takes one `Access`, which protects **every** generated action including reads, or `CrudAccess` to set actions apart. An action without its own entry uses `default`; `None` means public:

```python
# notes/apps/billing/routes.py (variant)
from hinoki import CrudAccess

from notes.security import billing_admin, billing_staff

access = CrudAccess(
    default=billing_staff, list=None, retrieve=None, delete=billing_admin
)
```

Here list and retrieve are public, create and update need `billing_staff`, delete needs `billing_admin`. Unknown action names raise `TypeError`. Generated writes always need a policy: a write left without one raises `RegistrationError` (when a `@crud` class is declared, or when a `crud(...)` value is included) unless you pass `public_writes=True` on purpose. The `access=` of `crud(...)` covers generated actions only; on a protected `@crud` class (any resource where at least one generated action has an `Access` policy), every handwritten action must declare `access=...` explicitly (or `access=None` to make it public on purpose), and omitting `access=` raises `RegistrationError` when the class is declared.

Rules see a generated action under the module that declares the `crud(...)` value: the value in `notes/apps/billing/routes.py` produces `notes.apps.billing.routes.InvoiceCrud.list`, `.retrieve`, `.create`, `.update` and `.delete`, and the class form uses its class (`notes.apps.billing.api.Invoices.list`). So the scaffold's `match="notes.*"` rules check them, and a public list goes in `except_` like any other action:

```python
Actions.require_access(
    match="notes.*",
    except_=["notes.api.health", "notes.apps.billing.routes.InvoiceCrud.list"],
)
```

The same names select [MCP tools](https://hinoki.caspel.dev/dev/app-mcp.md#choose-the-tools): `tools=["notes.apps.billing.*"]` also publishes the generated reads, and the writes with `allow_writes=True`.

How the name is chosen:

* A shared helper that wraps `crud(...)` and is called at the top level of a feature module names the actions after that feature module, not after the helper's module.
* `name="ArchiveCrud"` sets the class name; `name="notes.apps.archive.ArchiveCrud"` sets the module too. Pass it when the value is built deeper (a factory that calls another factory, or a function run from `main.py`), or when one module declares two `crud(...)` values for the same model. `name=` is for the function form only.
* Two different classes whose actions would share a name raise `RegistrationError` when included, naming both paths, so one `except_` entry, waiver or MCP pattern never covers both by accident.
* One value mounted twice (`Mount("/a", notes)` and `Mount("/b", notes)`) keeps one name, as any `API` class or function does: an exception for it covers both mounts.

## Filter, order and search the list

Declare what a client may ask for in a `FilterSet` and pass it as `filters=`. It works for `crud(...)` as a value and as `@crud(...)` on an `API` class.

```python
# notes/apps/billing/filters.py
from datetime import date
from typing import Literal

from hinoki.filters import Eq, FilterSet, In, Range, Search


class InvoiceFilters(FilterSet):
    paid: Eq[bool]
    status: In[Literal["open", "paid", "overdue"]]
    issued: Range[date]
    search = Search("reference", "customer_name")
    ordering = ("issued", "reference")
```

```python
# notes/apps/billing/routes.py
from hinoki import crud

from notes.apps.billing.filters import InvoiceFilters

invoices = crud(
    Invoice,
    path="/invoices",
    read=InvoiceOut,
    access=billing_staff,
    filters=InvoiceFilters,
)
```

| Declaration | Request | Meaning |
| --- | --- | --- |
| `paid: Eq[bool]` | `?paid=false` | equals the value |
| `status: In[...]` | `?status=open,overdue` | equals any of at most 50 comma-separated values |
| `issued: Range[date]` | `?issued.from=2024-01-01&issued.to=2024-03-31` | inclusive bounds, either one optional |
| `discount: Eq[int \| None]` | `?discount.null=true` | no value (`true`) or a value (`false`); only for a nullable column and a `T \| None` argument |
| `search = Search("reference", "customer_name")` | `?search=acme` | the text is contained in any of the columns, ignoring case; `%` and `_` are literal |
| `ordering = ("issued", "reference")` | `?ordering=-issued,reference` | sort keys from the list, `-` for descending |

Each attribute name is the column it filters. Types are `bool`, `int`, `float`, `str`, `UUID`, `date`, `datetime` (an RFC 3339 instant, read as UTC), `Decimal` and `Literal[...]` of strings; an `Eq`, `In` or `Range` is also what the typed OpenAPI parameter documents. A bool, UUID or `Literal` has no `Range`. An empty value (`?paid=`) is ignored.

A name that is not declared, an operator that the field does not have (`?paid.from=1`), a value of the wrong type or outside the `Literal`, a repeated parameter, an ordering key outside `ordering`, or a value that is too long is a `422` listing every problem with its `path` and a `code` (`type`, `choice`, `unknown`, `operator`, `duplicate`, `too_long`, `too_many`); the message never repeats the client's value. Values are parsed and coerced in Rust, bound as parameters and never become SQL text, and column names come only from the declaration, checked against the model when the routes are declared.

SQLite keeps a `Decimal` column as text, which compares by spelling and orders alphabetically, not numerically. A `Range[Decimal]` filter (`?amount.from=...`/`?amount.to=...`) or an `ordering` entry naming a `Decimal` column is a `422` there, with `code` `unsupported` and `path` naming the field (or `ordering`); only equality (`Eq`, `In`, `.null`) is checked against a `Decimal` column on SQLite. PostgreSQL and MySQL compare and order a `Decimal` numerically and accept the full `Range`.

A filter's produced names (its own name, `.from`/`.to` for a `Range`, `.null` when nullable, plus `search` and `ordering` when declared) must not collide with the pagination parameters (`page`/`page_size`, or `cursor`/`limit`): a `FilterSet` that does is refused when the routes are declared, for `crud(...)` and for a manual `@http.get` action alike.

The list applies, in this order: `get_queryset(principal)` (tenant and visibility), the client's filters combined with AND on top of it, the `filter_queryset` hook, the client's ordering and then pagination. A client filter can only narrow what `get_queryset` returned. The client's `ordering` replaces the order that `get_queryset` or the model set, always ends with the primary key so pages are stable, and without it that order stays.

`filter_queryset(self, query, params, principal)` is an optional hook of the class form; `params` is the `InvoiceFilters` instance (`params.paid`, `params.status` as a list, `params.issued` as `Bounds(from_, to)`, `params.search_term`, `params.is_null("discount")`, each `None` when the client did not send it):

```python
# notes/apps/billing/api.py
from typing import Any

from hinoki import API, Caller, crud
from hinoki.orm import QuerySet


@crud(
    Invoice,
    path="/invoices",
    read=InvoiceOut,
    access=billing_staff,
    filters=InvoiceFilters,
)
class Invoices(API):
    def filter_queryset(
        self,
        query: QuerySet[Invoice],
        params: InvoiceFilters,
        principal: Caller[Any] | None,
    ) -> QuerySet[Invoice]:
        if params.paid is None:
            return query.filter(archived=False)
        return query
```

The Tracker example's [`TaskFilters`](https://github.com/caspel26/hinoki/blob/main/examples/tracker/tracker/apps/tasks/filters.py) and its `Tasks` class in [`api.py`](https://github.com/caspel26/hinoki/blob/main/examples/tracker/tracker/apps/tasks/api.py) show a filtered, owner-scoped list.

The filters belong to the `list` action: `omit=("list",)` with `filters=` is refused. With `CursorPagination` the ordering columns must be non-null `int`, `str`, `UUID`, `datetime` or `float` columns, and a cursor is bound to the filters and ordering of the request that issued it ([details](https://hinoki.caspel.dev/dev/pagination.md#cursor-keyset-pagination)). On Kiso, a filtered list with `PageNumberPagination` and no custom `get_queryset` or `filter_queryset` hook runs on the native generated-CRUD list lane (with `CursorPagination` or custom hooks staying on the Python lane, where rows are still encoded in Rust); the [admin](https://hinoki.caspel.dev/dev/admin.md) lists have their own filters and do not use this one. A `FilterSet` also works on its own: `InvoiceFilters.parse({"paid": ["true"]})` returns the typed instance or raises `ValidationError`.

## Hooks in the class form

Apply `@crud(...)` to a class whose only base is `API`. These methods change what the generated actions do; every other decorated method is a handwritten action:

| Hook | Called for | Purpose |
| --- | --- | --- |
| `get_queryset(principal)` | every action | The base query: tenant or visibility filters. Builds a query, does no I/O |
| `filter_queryset(query, params, principal)` | list | Narrows the list after the client's [filters](https://hinoki.caspel.dev/dev/crud.md#filter-order-and-search-the-list); `params` is the `FilterSet` instance |
| `to_schema(item)` | every answer | A response shape that needs loaded relations |
| `written_queryset(principal)` | after a write | Loads response relations of the written row |
| `store(values, principal)` | create | Creates the row |
| `change(item, scoped_queryset, values, principal)` | update | Updates the row |
| `remove(item, scoped_queryset, principal)` | delete | Deletes the row |

```python
# notes/apps/billing/api.py
from typing import Any

from hinoki import API, Caller, crud

from notes.security import billing_staff

from .models import Invoice
from .schemas import InvoiceIn, InvoiceOut


def normalised(reference: str) -> str:
    return reference.strip().upper()


@crud(
    Invoice, path="/invoices", read=InvoiceOut, create=InvoiceIn, access=billing_staff
)
class Invoices(API):
    async def store(self, values: dict[str, Any], principal: Caller[Any]) -> Invoice:
        values["reference"] = normalised(str(values["reference"]))
        return await Invoice.acreate(**values)
```

Each hook may be `def` or `async def`. Sync hooks run on Hinoki's bounded executor and use the sync ORM methods (`Invoice.create(**values)`); async hooks run on the event loop and use the async ones. A sync hook must not return an un-awaited coroutine.

When reads are public, `principal` is `None`; when the action has an access policy, it is the authenticated `Caller`. When every generated action that calls a hook has an access policy, you can annotate the parameter with a non-optional `Caller[User]` (no `| None` guard needed). At class declaration Hinoki checks that no public generated action can call a hook expecting a non-optional `Caller[...]`, and that `Caller[User]` matches the user type of a parameterized `BaseAuth[User]` provider; any mismatch raises `RegistrationError`. For mixed public/protected resources, annotate `Caller[User] | None` (or `Caller[Any] | None`).

```python
# notes/apps/billing/api.py (protected CRUD with typed Caller)
from hinoki import API, Caller, crud
from hinoki.orm import QuerySet

from notes.security import StaffUser, billing_staff

from .models import Invoice
from .schemas import InvoiceOut


@crud(Invoice, path="/invoices", read=InvoiceOut, access=billing_staff)
class Invoices(API):
    def get_queryset(self, caller: Caller[StaffUser]) -> QuerySet[Invoice]:
        return Invoice.filter(account_id=caller.user.account_id)
```

Apply tenant or visibility filters in `get_queryset`; `owner=` is an independent write safeguard, not read authorization. Relations are never loaded implicitly: load them in `to_schema`, and in `written_queryset` for the answer to a write, independently of filters that might hide the changed row from later list requests. Writes are constrained by the query of `get_queryset`, the primary key and the owner column, inside the mutation statement. `change` and `remove` receive that scoped query; call `super()` to keep the default mutation.

Hooks run inside the transaction and before the commit, so do not send mail or publish external events from them. They are trusted application code: do not bypass the scoped query with an unscoped write or raw SQL. Hinoki checks the persisted item after create and update for model identity and server-assigned fields, but cannot sandbox arbitrary side effects.

### Generate or hand-write

Each action is generated **or** handwritten, never both:

| To hand-write | Do this |
| --- | --- |
| `create`, `update`, `delete` | Leave `create=`, `update=` or `delete=True` out, and declare your own method |
| `list`, `retrieve` | Pass `omit=("list",)` or `omit=("retrieve",)` and declare a method with another name |

A method named like an action that `crud(...)` still generates (`list`, `retrieve`, `create`, `update`, `delete`) raises `RegistrationError` when the class is declared. A handwritten route at the same method and path as a generated one fails when the class is included.

## Reuse

CRUD configuration is plain Python values. Share one `Access` or `CrudAccess`, or bind your defaults once:

```python
# notes/apps/billing/resources.py
from functools import partial

from hinoki import crud

from notes.security import billing_staff

staff_crud = partial(crud, access=billing_staff)
```

`staff_crud(Invoice, path="/invoices", read=InvoiceOut)` works as a value and as a class decorator. A `@crud` class accepts no base other than `API`, so mixins do not apply.

## Many-to-many fields

A model with `many_to_many(...)` ([relations](https://hinoki.caspel.dev/dev/relations.md#many-to-many)) exposes an ids field named `<field>_ids` by default: `tags` gives `tags_ids`. `ids_name=` overrides the default. The examples below declare `tags: ClassVar[ManyToMany[Tag]] = many_to_many(Tag, ids_name="tag_ids")`, so their schemas say `tag_ids`; without `ids_name` they would say `tags_ids`. The field is a bounded set of the target's keys. Declare it in the schemas and give the targets a scope with `links=`; `all_rows(Tag)` is the explicit way to allow every row:

```python
class PostOut(Schema):
    id: UUID
    title: str
    tag_ids: Annotated[set[UUID], Field(max_length=20)]


class PostPatch(Schema):
    title: str | Unset = UNSET
    tag_ids: Annotated[set[UUID], Field(max_length=20)] | Unset = UNSET


@crud(
    Post,
    path="/posts",
    read=PostOut,
    update=PostPatch,
    access=writer,
    links={"tags": lambda user: Tag.filter(owner=user)},
)
class Posts(API): ...
```

* `links=` is required for every relation whose ids field appears in a schema; the callable receives `Caller.user` (`None` on public routes) and returns the targets a caller may link and see. The class form can override `link_queryset(relation, principal)` instead.
* A write replaces the whole set; an omitted field keeps it and `[]` clears it. Only the links inside the scope are replaced, so links to targets the caller cannot see are neither shown nor removed.
* An id that does not exist and an id outside the scope give the same `422` (type `unknown_reference`) without echoing the id; more ids than the field's `max_length` is a `422` too. The owner row is checked against `get_queryset` and the owner policy first, and the row and its links are written in one transaction.
* A read schema includes the ids through a prefetch bounded by the field's limit: one joined target-and-bridge query per owner chunk and relation. Each statement reads that chunk under the database's transaction isolation, and shared targets appear once per link in its rows. Rust builds eligible models; Python orchestrates the prefetch and handles custom row builders. See [many-to-many prefetch](https://hinoki.caspel.dev/dev/relations.md#many-to-many).
* The `max_length` of every ids field in a create or update schema is required.
* `own_rows(Tag, "owner")` scopes the targets to those whose text `owner` column equals the caller's id (`Caller.id`); `all_rows(Tag)` allows every row. The owner value is checked again inside the write's transaction, after the owner row is locked.

### Native lane

A route with an ids field runs on the [native lane](https://hinoki.caspel.dev/dev/kiso.md#generated-crud) when every relation it exposes is scoped with `all_rows(...)` or `own_rows(...)` (the latter needs `ClaimsJWTAuth`, and `identity_key=caller_id` when the resource also sets `owner=`), the target's key is an `int`, `UUID` or `str`, the read field is a non-nullable `set` whose only constraint is `max_length` (at least the relation's `max_items`), with plain items (no item constraints, enums or literals), the create field is required with a `max_length` and the update field is `... | Unset = UNSET`. Rust then reads the ids of a page with one `SELECT` on the bridge (a row with more links than `max_items` is a `500`: the stored data broke its bound), and on create and update it parses and bounds the set, locks the owner row, checks the targets inside the scope and replaces the links in the same transaction as the row; the answers (`403`, `404`, `422` `unknown_reference`, `max_length`) match the Python lane. A key Rust cannot read (a string longer than the key's `max_length`, for example) is answered by the Python lane. A scope callable, a `link_queryset` override, a model or lifecycle hook, or a `Linked`/`Unlinked` handler keeps the route on the Python lane, which accepts the same schemas and also an optional create field and any other rule the schema states.

## Security and validation

Set `owner="author_id"` to assign that column from the authenticated caller on create and constrain update and delete. `identity_key` extracts the owner value from `Caller.user`; its default reads `.id`. `assign` maps additional server-owned columns to functions of the caller's user object. Use `from_user("publication_id")` (from `hinoki`) to declare an attribute (or dotted attribute path such as `from_user("org.id")`) read from `Caller.user`; any callable such as `operator.attrgetter` also works:

```python
from hinoki import crud, from_user

posts = crud(
    Post,
    path="/posts",
    read=PostOut,
    create=PostCreate,
    access=editor,
    owner="author_id",
    assign={"publication_id": from_user("publication_id")},
)
```

Client schemas cannot accept server-assigned fields. Generated writes require an access policy even when `owner` is configured.

With `ClaimsJWTAuth` there is no user object, so use `identity_key=caller_id` (from `hinoki.resources`): the owner value is then the caller's `Caller.id` (the token `sub`). A foreign update or delete answers `403` and a missing row `404`. This is the only owner declaration that can run natively; see [authentication](https://hinoki.caspel.dev/dev/authentication.md#claims-only-tokens-on-the-native-lane).

At declaration Hinoki rejects abstract models, unknown schema fields, unsafe nullable fields, missing required create values, update schemas that can overwrite omitted fields, primary-key updates, unknown assigned columns and incompatible derived schemas. `only=` and `exclude=` schemas remain subject to the same checks.

A stored row that the `read=` schema refuses (for example a `max_length` that the data exceeds) is a server fault: the action answers `500`, the response does not repeat the stored values, and the server log names the model, the read schema, the field and the code. Client input that fails a create or update schema answers `422`. A create that breaks a unique constraint answers `409 unique_conflict` unless an error handler you register says otherwise.

## Model hooks and Kiso

Generated create and PATCH update write through the model, so its [lifecycle hooks](https://hinoki.caspel.dev/dev/model-hooks.md) run: create calls `acreate`, and update saves the item with the scoped query's conditions in its UPDATE, so a row that left the caller's scope since it was read is refused (`403`/`404`). A model that defines any of `before_save`, `before_create`, `after_create` or `after_save` is not eligible for the native create and update routes; they use the Python lane, while its list, retrieve and delete routes stay native. Deleting does not run hooks. A handler registered for the model's `Created`, `Updated` or `Deleted` [event](https://hinoki.caspel.dev/dev/events.md#model-events) likewise moves only the matching action (`create`, `update`, `delete`) to the Python lane, where the event is emitted; update then saves the instance with the scoped UPDATE, as it does for hooks.

On Kiso, eligible generated routes execute in Rust when their schemas and model are supported and the resource has no access policy (other than an unmodified `ClaimsJWTAuth`), owner or assignment rule (other than `identity_key=caller_id`) or custom hook. Ineligible and stale plans use the Python lane. See [Kiso: generated CRUD](https://hinoki.caspel.dev/dev/kiso.md#generated-crud); no switch is required.

## Inspect the generated routes

```sh
hinoki routes --path /billing/invoices
```

```text
GET     /billing/invoices            StaffTokens[billing:manage] notes.apps.billing.routes.InvoiceCrud.list
POST    /billing/invoices            StaffTokens[billing:manage] notes.apps.billing.routes.InvoiceCrud.create
DELETE  /billing/invoices/{item_id}  StaffTokens[billing:manage] notes.apps.billing.routes.InvoiceCrud.delete
GET     /billing/invoices/{item_id}  StaffTokens[billing:manage] notes.apps.billing.routes.InvoiceCrud.retrieve
PATCH   /billing/invoices/{item_id}  StaffTokens[billing:manage] notes.apps.billing.routes.InvoiceCrud.update
```

Generated routes appear in the OpenAPI document with their schemas, access policies, status codes and pagination. See the [CRUD API reference](https://hinoki.caspel.dev/dev/reference/crud.md). Prefer a handwritten action when a workflow has its own state transitions, errors or side effects beyond one model mutation.
