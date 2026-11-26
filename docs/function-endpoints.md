---
status: implemented
related:
  - class-endpoints.md
  - routes.md
  - endpoints.md
  - testing.md
---

# Function-based APIs

A function-based API is a set of plain `async def` functions, each decorated with an HTTP method, grouped by a `RouteGroup` that states their shared prefix, access policy, transactions and errors once. It is the default pattern in Hinoki: use it unless a [class](class-endpoints.md) gives you something concrete.

## When to use it

| Use functions and a `RouteGroup` when | Use a class instead when |
| --- | --- |
| The endpoints are ordinary reads, writes or workflows | You want generated CRUD routes with hooks (`@crud`) |
| Several actions share `access=`, `atomic=` or `errors=` | Several resources inherit or override the same actions |
| You want handlers a test can call as plain functions | You ship a resource for other projects to include |

## A complete example

The Billing app of the Notes project serves invoices. `hinoki app create billing` created the app directory, its `routes.py` and its block in `hinoki.toml`; the files below are what you write.

=== "api.py"

    ```python
    # notes/apps/billing/api.py
    from uuid import UUID

    from hinoki import RouteGroup, errors
    from hinoki.pagination import Page, PageNumberPagination, PageParams

    from notes.security import billing_staff

    from .models import Invoice
    from .schemas import InvoiceIn, InvoiceOut

    invoices = RouteGroup("/invoices", access=billing_staff, atomic=True)


    @invoices.get("/", pagination=PageNumberPagination())
    async def list_invoices(page: PageParams) -> Page[InvoiceOut]:
        """Invoices, newest first."""
        return (await Invoice.all().apaginate(page)).map(InvoiceOut.from_model)


    @invoices.get("/{invoice_id}", errors=(errors.NotFound,))
    async def get_invoice(invoice_id: UUID) -> InvoiceOut:
        return InvoiceOut.from_model(await Invoice.aget(id=invoice_id))


    @invoices.post("/", status=201)
    async def create_invoice(data: InvoiceIn) -> InvoiceOut:
        return InvoiceOut.from_model(await Invoice.acreate(data))
    ```

=== "routes.py"

    ```python
    # notes/apps/billing/routes.py
    from hinoki import Includable

    from .api import invoices

    routes: list[Includable] = [invoices]
    ```

=== "hinoki.toml"

    ```toml
    # hinoki.toml (the block hinoki app create billing appended)
    [[apps]]
    name = "billing"
    models = "notes.apps.billing.models"
    migrations = "notes.apps.billing.migrations"
    requires = []
    ```

=== "models.py"

    ```python
    # notes/apps/billing/models.py
    from datetime import datetime
    from uuid import UUID, uuid4

    from hinoki import db
    from hinoki.orm import Model, field, pk
    from hinoki.utils import utc_now


    @db.ordered_by("-created_at")
    class Invoice(Model):
        id: UUID = pk(default_factory=uuid4)
        reference: str = field(max_length=40, unique=True)
        paid: bool = False
        created_at: datetime = field(default_factory=utc_now, index=True)
    ```

=== "schemas.py"

    ```python
    # notes/apps/billing/schemas.py
    from typing import Annotated

    from hinoki import Field, Schema, derive

    from .models import Invoice


    class InvoiceIn(Schema):
        reference: Annotated[str, Field(min_length=1, max_length=40)]


    @derive(Invoice)
    class InvoiceOut(Schema): ...
    ```

`billing_staff` is an `Access` policy declared once in `notes/security.py` ([the billing policies](authentication.md#billing-policies)). `notes/main.py` stays as `hinoki new` wrote it: the application it declares binds every `routes` list named in `hinoki.toml` ([Register routes](routes.md)).

The manifest prefix and the group prefix join, and a `"/"` path means the group prefix itself:

```sh
hinoki routes --path /billing
```

```text
GET     /billing/invoices               StaffTokens[billing:manage] notes.apps.billing.api.list_invoices
POST    /billing/invoices               StaffTokens[billing:manage] notes.apps.billing.api.create_invoice
GET     /billing/invoices/{invoice_id}  StaffTokens[billing:manage] notes.apps.billing.api.get_invoice
```

## Best practices

1. **One `RouteGroup` per resource.** State `access=`, `atomic=True` and the errors every action shares on the group. `atomic=True` on a group covers `POST`, `PUT`, `PATCH` and `DELETE`; reads stay outside a transaction.
2. **Keep handlers thin.** Validate with a `Schema`, call the model or a function in `operations.py`, and return `XOut.from_model(...)`. A handler never returns a model.
3. **Write paths relative to the app.** The app's prefix lives only in `hinoki.toml`. Never repeat `/billing` in code.
4. **Paginate every list**: `pagination=`, a `PageParams` parameter and `Page[...]`.
5. **Declare what you raise.** Every `APIError` an action can raise belongs in its `errors=` (or the group's). A missing row from `Model.aget` is answered as `errors.NotFound` (`404 not_found`).
6. **List the group in `routes.py`.** An action is served only when its group is in the list that `hinoki.toml` names.

## Group defaults and overrides

An action inherits the group's options and may override them. `InvoiceSummary` is a `Schema` with one field, `open: int`:

```python
# notes/apps/billing/api.py (continued)
@invoices.get("/summary", access=None)  # public on purpose
async def summary() -> InvoiceSummary:
    return InvoiceSummary(open=await Invoice.filter(paid=False).acount())
```

| Option | On the group | On the action |
| --- | --- | --- |
| `access=` | Default for every action | Replaces it; `access=None` makes the action public |
| `atomic=` | Applies to `POST`, `PUT`, `PATCH`, `DELETE` | `atomic=True`/`False` decides for this action, any method |
| `errors=` | Added to every action | Merged with the group's |

A public action must also be listed in `except_` of `Actions.require_access` in `notes/rules.py`, for example `except_=["notes.api.health", "notes.apps.billing.api.summary"]`, or the application refuses to start (`HNK-ACC-001`). The rule exists so that public endpoints are a written decision. `Actions.require_permissions` (`HNK-ACC-002`) only checks actions that have a policy, so it needs no entry.

## Compose with other decorators

Put the HTTP decorator outermost. The group records the function it receives when `@invoices.get(...)` runs, so a decorator above it would not be served. Both sync and async decorators written with `functools.wraps` preserve their signature and run when placed underneath `@invoices.<method>(...)`:

```python
# notes/apps/billing/api.py (continued)
@invoices.get("/export")
@audited  # your sync or async decorator, written with functools.wraps
async def export_invoices() -> InvoiceExport: ...
```

A wrapped function runs in an asyncio task. The [native handler lane](kiso.md#the-native-handler-lane) needs a plain `async def`.

## Test it

A test builds its own application from the same `routes` list, so it needs neither the manifest nor a server. Mount the list at the manifest prefix to check the final paths, and pass the project's settings when the test sends requests: `atomic=True` needs the database they declare.

```python
# tests/billingroutestest.py
from hinoki import Hinoki, Mount
from hinoki.testing import ASGIClient

from notes.apps.billing.routes import routes
from notes.security import billing_staff
from notes.settings import ProjectSettings


def test_writes_are_staff_only_and_atomic() -> None:
    app = Hinoki(include=[Mount("/billing", routes)])
    found = app.resolve("POST", "/billing/invoices")
    assert found is not None
    route, _ = found
    assert (route.access, route.atomic) == (billing_staff, True)


async def test_anonymous_callers_are_refused() -> None:
    app = Hinoki(settings=ProjectSettings, include=[Mount("/billing", routes)])
    async with ASGIClient(app) as client:
        reply = await client.get("/billing/invoices")
    assert reply.status == 401
```

The handlers are plain functions, so a unit test can also `await get_invoice(invoice_id)` inside `use_database(...)`. Tests that reach the database need a disposable one; see [testing](testing.md).

## Pitfalls

| Symptom | Cause | Fix |
| --- | --- | --- |
| The route is missing from `hinoki routes` | The group is not in `routes.py`, or the app has no `routes` key in `hinoki.toml` | Add it to `routes: list[Includable]` |
| Paths read `/billing/billing/...` | The prefix is in code and in `hinoki.toml` | Keep the app prefix in `hinoki.toml` only |
| `ValueError` on `RouteGroup("/invoices/")` | A prefix starts with `/` and does not end with one | `RouteGroup("/invoices")` |
| `TypeError: unknown action options` | An unknown or misspelled keyword on a group action | Use the option names listed in [Requests and responses](endpoints.md#declare-an-action) |
| `RuntimeError: a RouteGroup cannot change after it is included` | An action is added after the group is included | Declare every action at import time |
| `RegistrationError: a RouteGroup must declare at least one action` | An empty group in `routes.py` | Remove it or add an action |
| `RegistrationError: a RouteGroup can be included only once` | The group is listed twice in one application | List it once; mount the list, not each group |
| `ValueError: … already declares an HTTP route` | Two HTTP decorators on one function | One decorator per function |
| `RegistrationError: 1 rule violation(s)` naming `HNK-ACC-001` | A public action not listed in `notes/rules.py` | Add it to `except_`, or give it a policy |
