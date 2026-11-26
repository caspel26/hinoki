---
status: implemented
related:
  - function-endpoints.md
  - crud.md
  - routes.md
---

# Class-based APIs

A class-based API is a subclass of `API` whose methods carry the same `@http.*` decorators as functions. A class adds a path prefix, inheritance and, with `@crud(...)`, generated routes with hooks. For ordinary endpoints prefer [functions and a `RouteGroup`](function-endpoints.md), which also states access, transactions and errors once.

## When to use it

| Use a class for | Example |
| --- | --- |
| Generated CRUD plus hooks or extra actions | `@crud(Invoice, ...)` with `get_queryset` and a `pay` action |
| Actions that several resources inherit or override | A base `API` with a `counts` action, reused under two prefixes |
| A resource you ship for other projects to include | The admin |

A class that only puts three functions under a prefix gains nothing over a `RouteGroup`, and each of its actions must repeat `access=`, `atomic=` and `errors=`.

## A plain class

```python
# notes/apps/billing/reports.py
from hinoki import API, Schema, http

from notes.security import billing_staff

from .models import Invoice


class InvoiceCounts(Schema):
    open: int
    paid: int


class Reports(API, path="/reports"):
    @http.get("/counts", access=billing_staff)
    async def counts(self) -> InvoiceCounts:
        return InvoiceCounts(
            open=await Invoice.filter(paid=False).acount(),
            paid=await Invoice.filter(paid=True).acount(),
        )
```

`path=` is the class prefix; each action's path is relative to it, so this action is served at `/billing/reports/counts` once `Reports` is in `routes.py`.

## Inherit and override

A subclass inherits every routed method. Members are resolved by name, most derived first:

```python
# notes/apps/billing/reports.py (continued)
class ArchiveReports(Reports, path="/archive"):  # /archive/counts, own prefix
    pass


class PublicReports(Reports):  # inherits path="/reports"
    def counts(self) -> None:  # redefined without a decorator: no route
        return None
```

| In the subclass | Result |
| --- | --- |
| Nothing | The route is inherited, under the subclass's `path=` or the inherited one |
| The same name with a decorator | Replaces the inherited route |
| The same name without a decorator | Drops the inherited route |

Include only the classes you serve. A base and a subclass with the same prefix are two copies of every inherited route, and the application refuses the second.

## Generated CRUD with hooks

`@crud(...)` generates list and retrieve, plus the writes you give a `Schema` for, on a class whose only base is `API`. Hooks change what the generated actions do; any other decorated method is a handwritten action:

```python
# notes/apps/billing/api.py
from typing import Any
from uuid import UUID

from hinoki import API, Caller, crud, errors, http
from hinoki.orm import QuerySet

from notes.security import billing_staff

from .models import Invoice
from .schemas import InvoiceIn, InvoiceOut


class InvoiceAlreadyPaid(errors.Conflict):
    code = "invoice_already_paid"
    message = "The invoice is already paid."


@crud(
    Invoice, path="/invoices", read=InvoiceOut, create=InvoiceIn, access=billing_staff
)
class Invoices(API):
    def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Invoice]:
        return Invoice.all().order_by("reference")

    @http.post(
        "/{item_id}/pay",
        access=billing_staff,
        atomic=True,
        errors=(errors.NotFound, InvoiceAlreadyPaid),
    )
    async def pay(self, item_id: UUID) -> InvoiceOut:
        changed = await Invoice.aupdate_where(
            {"id": item_id, "paid": False}, {"paid": True}
        )
        if not changed:
            await Invoice.aget(id=item_id)  # 404 when it does not exist
            raise InvoiceAlreadyPaid()
        return InvoiceOut.from_model(changed[0])
```

```python
# notes/apps/billing/routes.py
from hinoki import Includable

from .api import Invoices

routes: list[Includable] = [Invoices]
```

| Route | Source | Access |
| --- | --- | --- |
| `GET /billing/invoices` | generated `list` (paginated) | `billing_staff` |
| `GET /billing/invoices/{item_id}` | generated `retrieve` | `billing_staff` |
| `POST /billing/invoices` | generated `create` (`create=InvoiceIn`, atomic) | `billing_staff` |
| `POST /billing/invoices/{item_id}/pay` | handwritten `pay` | its own `access=` |

The `access=` of `crud(...)` covers generated actions only; a handwritten method states its own. `pay` changes the row only if it is still unpaid, in one statement, so two concurrent calls cannot both succeed. Hooks, `CrudAccess`, `owner=` and every keyword are in [Generate CRUD routes](crud.md).

When a resource needs no code, `crud(...)` is a value, not a class:

```python
# notes/apps/billing/routes.py
from hinoki import Includable, crud

from notes.security import billing_staff

from .models import Invoice
from .schemas import InvoiceIn, InvoiceOut

routes: list[Includable] = [
    crud(
        Invoice,
        path="/invoices",
        read=InvoiceOut,
        create=InvoiceIn,
        access=billing_staff,
    ),
]
```

To let clients filter, order and search the generated list, pass a `FilterSet` as `filters=InvoiceFilters` to `@crud(...)` or to the value form. The class then may define `filter_queryset(self, query, params: InvoiceFilters, principal)`, which runs after `get_queryset` and the client's filters, so it can only narrow further; see [filter, order and search the list](crud.md#filter-order-and-search-the-list). The Tracker example's [`Tasks` class](https://github.com/caspel26/hinoki/blob/main/examples/tracker/tracker/apps/tasks/api.py) combines `filters=`, `owner=`, a `get_queryset` scope, a `remove` hook and a handwritten `complete` action.

## One instance per request

Hinoki creates the class with no arguments for every request and calls the method on that instance. Keep `self` empty:

- Take request data as parameters: `request: Request`, `caller: Caller[Staff]`, path and body values.
- Reach worker resources through a [module](modules.md), never through attributes set on `self` or on the class.
- Do not define an `__init__` that needs arguments. Hinoki refuses it when the class is included.

A class that keeps the stock constructor and sets no `response_headers` is eligible for the [native handler lane](kiso.md#the-native-handler-lane) like a function. `hinoki routes --lanes` shows the choice per route.

## Best practices

1. **Use a class for `@crud`, inheritance or a shipped resource**, functions otherwise.
2. **One base, `API`.** A `@crud` class accepts no other base. Share configuration with a value (`staff_crud = functools.partial(crud, access=billing_staff)`) instead of a mixin.
3. **State `access=`, `atomic=` and `errors=` on every handwritten action.** The class carries a path only; on a protected `@crud` class, omitting `access=` on a handwritten action raises `RegistrationError` (pass `access=None` to make it public on purpose).
4. **Scope writes in the statement** (`aupdate_where` with the conditions), not check-then-write.
5. **Hooks build queries.** `get_queryset` does no I/O; load relations explicitly in `to_schema` or `written_queryset`.
6. **Let `crud` generate or omit, not compete.** To write any generated action yourself, pass it in `omit=` (for example `omit=("list",)` or `omit=("update",)`), or leave `create=` / `update=` / `delete=True` out.

## Test it

```python
# tests/invoicestest.py
from notes.apps.billing.api import Invoices
from notes.security import billing_staff


def test_invoice_routes() -> None:
    routes = {(r.method, r.path): r for r in Invoices.routes()}
    pay = routes["POST", "/invoices/{item_id}/pay"]
    assert (pay.access, pay.atomic) == (billing_staff, True)
    assert ("GET", "/invoices/{item_id}") in routes
```

`Invoices.routes()` lists the class routes with its own prefix, without an application. Request tests are the same as for functions: build `Hinoki(include=[Mount("/billing", routes)])` and use `ASGIClient` ([function-based APIs](function-endpoints.md#test-it), [testing](testing.md)).

## Pitfalls

| Symptom | Cause | Fix |
| --- | --- | --- |
| `RegistrationError: … is created without arguments per request` | An `__init__` with required arguments | Remove it; take values as parameters |
| `RegistrationError` on a `@staticmethod` or `@classmethod` route | Routes are instance methods | Drop the decorator and keep `self` |
| `RegistrationError: Invoices: a crud(...) class has exactly one base, API` | A mixin or another base | Share a `functools.partial(crud, ...)` or `Access` values |
| `RegistrationError` naming `create` (or `list`, …) | A method named like an action `crud` generates | Rename it, or stop generating that action (`omit=`, no `create=`) |
| `RegistrationError: … is declared by two classes (/a and /b)` | Two different classes (often two `crud(...)` values for one model in one module) give an action the same qualified name | Rename one class, or pass `crud(..., name="ArchiveCrud")` |
| `RegistrationError` naming a handwritten action and `access=` | A handwritten action on a protected `@crud` class omitted `access=` | Add `access=...` to its decorator, or `access=None` to make it public on purpose |
| A route vanished in a subclass | The method was redefined without a decorator | Re-declare it with its decorator |
