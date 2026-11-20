---
status: implemented
related:
  - class-endpoints.md
  - models.md
  - authentication.md
  - pagination.md
  - kiso.md
---

# Generate CRUD routes

`crud(...)` generates model-backed routes: list and retrieve always, create, update and delete when you ask for them. Use it as a value in `routes.py` for plain CRUD, or apply it to an `API` class to add hooks and handwritten actions ([class-based APIs](class-endpoints.md)).

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

Writes run in a transaction. `pagination=` selects the page-number policy of the list route (default `PageNumberPagination()`). `omit=("list",)` or `omit=("retrieve",)` drops a read so that you can write it yourself. The generated actions are ordinary routes, visible to OpenAPI, `hinoki routes` and the route registry; nothing is registered at import time.

### Access per action

`access=` takes one `Access`, which protects **every** generated action including reads, or `CrudAccess` to set actions apart. An action without its own entry uses `default`; `None` means public:

```python
# notes/apps/billing/routes.py (variant)
from hinoki import CrudAccess

access = CrudAccess(
    default=billing_staff, list=None, retrieve=None, delete=billing_admin
)
```

Here list and retrieve are public, create and update need `billing_staff`, delete needs `billing_admin`. Unknown action names raise `TypeError`. Generated writes always need a policy: a write left without one fails at declaration unless you pass `public_writes=True` on purpose. The `access=` of `crud(...)` covers generated actions only; a handwritten action in the class form states its own.

## Hooks in the class form

Apply `@crud(...)` to a class whose only base is `API`. These methods change what the generated actions do; every other decorated method is a handwritten action:

| Hook | Called for | Purpose |
| --- | --- | --- |
| `get_queryset(principal)` | every action | The base query: tenant or visibility filters. Builds a query, does no I/O |
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

When reads are public, `principal` is `None`; when the action has an access policy, it is the authenticated `Caller`. Apply tenant or visibility filters in `get_queryset`; `owner=` is an independent write safeguard, not read authorization. Relations are never loaded implicitly: load them in `to_schema`, and in `written_queryset` for the answer to a write, independently of filters that might hide the changed row from later list requests. Writes are constrained by the query of `get_queryset`, the primary key and the owner column, inside the mutation statement. `change` and `remove` receive that scoped query; call `super()` to keep the default mutation.

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

## Security and validation

Set `owner="author_id"` to assign that column from the authenticated caller on create and constrain update and delete. `identity_key` extracts the owner value from `Caller.user`; its default reads `.id`. `assign` maps additional server-owned columns to functions of the caller's user object. Client schemas cannot accept server-assigned fields. Generated writes require an access policy even when `owner` is configured.

With `ClaimsJWTAuth` there is no user object, so use `identity_key=caller_id` (from `hinoki.resources`): the owner value is then the caller's `Caller.id` (the token `sub`). A foreign update or delete answers `403` and a missing row `404`. This is the only owner declaration that can run natively; see [authentication](authentication.md#claims-only-tokens-on-the-native-lane).

At declaration Hinoki rejects abstract models, unknown schema fields, unsafe nullable fields, missing required create values, update schemas that can overwrite omitted fields, primary-key updates, unknown assigned columns and incompatible derived schemas. `only=` and `exclude=` schemas remain subject to the same checks.

A stored row that the `read=` schema refuses (for example a `max_length` that the data exceeds) is a server fault: the action answers `500`, the response does not repeat the stored values, and the server log names the model, the read schema, the field and the code. Client input that fails a create or update schema answers `422`. A create that breaks a unique constraint answers `409 unique_conflict` unless an error handler you register says otherwise.

## Model hooks and Kiso

Generated create and PATCH update write through the model, so its [lifecycle hooks](model-hooks.md) run: create calls `acreate`, and update saves the item with the scoped query's conditions in its UPDATE, so a row that left the caller's scope since it was read is refused (`403`/`404`). A model that defines any of `before_save`, `before_create`, `after_create` or `after_save` is not eligible for the native create and update routes; they use the Python lane, while its list, retrieve and delete routes stay native. Deleting does not run hooks.

On Kiso, eligible generated routes execute in Rust when their schemas and model are supported and the resource has no access policy (other than an unmodified `ClaimsJWTAuth`), owner or assignment rule (other than `identity_key=caller_id`) or custom hook. Ineligible and stale plans use the Python lane. See [Kiso: generated CRUD](kiso.md#generated-crud); no switch is required.

## Inspect the generated routes

```sh
hinoki routes --path /billing/invoices
```

```text
GET     /billing/invoices            StaffTokens[billing:manage] hinoki.resources.list
POST    /billing/invoices            StaffTokens[billing:manage] hinoki.resources.create
DELETE  /billing/invoices/{item_id}  StaffTokens[billing:manage] hinoki.resources.delete
GET     /billing/invoices/{item_id}  StaffTokens[billing:manage] hinoki.resources.retrieve
PATCH   /billing/invoices/{item_id}  StaffTokens[billing:manage] hinoki.resources.update
```

Generated routes appear in the OpenAPI document with their schemas, access policies, status codes and pagination. See the [CRUD API reference](reference/crud.md). Prefer a handwritten action when a workflow has its own state transitions, errors or side effects beyond one model mutation.
