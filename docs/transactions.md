---
status: implemented
related:
  - endpoints.md
  - rules.md
  - modules.md
---

# Run writes in transactions

An action opts into a database transaction with `atomic=True`. The transaction
opens for the request and commits only after the handler returns and the
response body is encoded and rendered. Set it once on a `RouteGroup` and every
write method of the group is atomic:

```python
# notes/apps/billing/api.py
from uuid import UUID

from hinoki import RouteGroup

from notes.security import billing_staff

from .models import AuditEvent, Invoice  # AuditEvent: any model of yours
from .schemas import InvoiceIn, InvoiceOut

invoices = RouteGroup("/invoices", access=billing_staff, atomic=True)


@invoices.post("/", status=201)  # atomic: a write method in an atomic group
async def create_invoice(data: InvoiceIn) -> InvoiceOut:
    invoice = await Invoice.acreate(data)
    await AuditEvent.acreate(subject=invoice.reference, action="created")
    return InvoiceOut.from_model(invoice)


@invoices.post("/{invoice_id}/remind", status=204, atomic=False)  # writes nothing
async def remind(invoice_id: UUID) -> None: ...
```

On a single action, write `@http.post("/invoices", atomic=True)`. Hinoki checks
the returned value against the response annotation before encoding it (see
[the response check](endpoints.md#the-response-is-checked-against-the-annotation)).

## What rolls back

If the handler raises, response encoding/rendering fails, the request times out, or the task is cancelled, the transaction rolls back before the error response is translated. A value that does not match the response annotation is rejected as a `500`, and the transaction rolled back, unless the action opts out with `validate_response=False`. Returning a streamed response (`Stream`, `EventStream`, or `File`) from an `atomic=True` action annotated `-> http.Response` is rejected as a `500` inside the transaction and rolls back as well, whereas returning a plain non-stream `http.Response` with a `4xx` status (such as `409`) is a normal return and commits; raise an `APIError` when a rejected operation should roll back.

Session persistence (`Session.commit()`) runs after the handler and its `atomic=True` SQL transaction have committed, so a session store write failure happens in a separate boundary after the SQL commit. Do not send email, write local files, or perform another irreversible side effect inside the transaction; arrange it after the transaction commits, or use an outbox owned by the application.

`RouteGroup(..., atomic=True)` makes `POST`, `PUT`, `PATCH` and `DELETE` atomic
by default; `GET` actions are not. An action opts out with `atomic=False`.

A project created with `hinoki new` enforces `Transactions.require_atomic()`
(`HNK-TXN-001`), so a write that opts out stops `hinoki check` and startup until
you record the decision. List the action in `except_` in `notes/rules.py`:

```python
# notes/rules.py, listed in `rules` of hinoki.toml
from hinoki import Transactions
from hinoki.rules import RuleSet

project_rules = RuleSet(
    # ... the other rules
    Transactions.require_atomic(except_=["notes.apps.billing.api.remind"]),
)
```

A temporary exception goes in `hinoki-rules.toml` instead ([waivers](rules.md#accept-an-exception-with-a-waiver)).

## Atomic blocks and functions in application code

`db.atomic` (`from hinoki import db`) opens the same kind of scope in your own code, on the database that is current for the task: the one a request makes active, or the one set with `hinoki.orm.use_database(...)`. Use it as a block:

```python
# notes/apps/billing/operations.py
from hinoki import db

from .models import AuditEvent, Invoice


async def mark_paid(invoice: Invoice) -> None:
    async with db.atomic():
        invoice.paid = True
        await invoice.asave()
        await AuditEvent.acreate(subject=invoice.reference, action="paid")
```

or as a decorator on an `async def`, with or without parentheses:

```python
# notes/apps/billing/operations.py
@db.atomic
async def settle_all() -> int:
    return await Invoice.filter(paid=False).aupdate({"paid": True}, returning=False)


@db.atomic()
async def reopen(invoice_id: UUID) -> None:
    await Invoice.aupdate(id=invoice_id, paid=False)
```

Each call of a decorated function opens its own scope: returning commits, an exception rolls back and propagates. The wrapper keeps the function's name, docstring, signature and annotations (`functools.wraps`).

In a sync handler, hook or helper, use the same API without `await`: `with db.atomic():` as a block, or `@db.atomic` on a plain `def`. The sync model methods (`create`, `update`, `bulk_create`, ...) inside it run in the same transaction:

```python
# notes/apps/billing/operations.py
@db.atomic
def settle_all_sync() -> int:
    return Invoice.update_where({"paid": False}, {"paid": True}, returning=False)


def mark_paid_sync(invoice: Invoice) -> None:
    with db.atomic():
        invoice.paid = True
        invoice.save()
```

The sync forms only work on a worker thread served by Hinoki, as the sync model methods do; on the event loop they raise `RuntimeError` (use `async with` there). A nested sync scope is a savepoint, like the async one. Raw `Database.execute` and `fetch` remain async-only (see [sync and async](sync-async.md)).

`db.atomic()` is `current_database().atomic()` resolved when the block is entered or the function is called. When you hold a `Database` yourself (a script, a module lifespan, `ctx.get(Database)`), call `database.atomic()` directly; it behaves the same. `Database.migration()` is the separate, exclusive scope used for migrations.

### Nesting and savepoints

A scope opened while another scope of the same task is already open becomes a savepoint, whether it comes from `atomic=True`, `async with`, or a decorated function. An exception inside the inner scope rolls back only that savepoint; the outer transaction continues and commits when it ends. A decorated function called from an `atomic=True` action therefore joins the request's transaction as a savepoint rather than committing on its own.

```python
# notes/apps/billing/operations.py
async def create_with_discount(data: InvoiceIn, coupon_id: UUID) -> Invoice:
    async with db.atomic():
        invoice = await Invoice.acreate(data)
        try:
            async with db.atomic():  # SAVEPOINT
                await Coupon.aupdate(id=coupon_id, used=True)
                raise CouponExpired()
        except CouponExpired:
            pass  # only the coupon update is undone
    return invoice  # the invoice committed when the outer block ended
```

A child task cannot share its parent's active transaction: opening a nested scope from another task raises `DatabaseError`.

### Decorated functions and routes

A function decorated with `@db.atomic` can also be a route handler (put the HTTP decorator outermost). Binding, OpenAPI and the native server see the original signature, and the handler runs inside its own atomic scope. That scope covers the handler call only: it commits when the handler returns, before the response is encoded. `atomic=True` on the action is the declaration that also covers response encoding, and it is what `HNK-TXN-001` (`Transactions.require_atomic`) reads: the rule does not inspect decorators, so an action that is only decorated with `@db.atomic` is still reported. Prefer `atomic=True` on actions and keep `db.atomic` for application code that the action calls.

On the native server lane, a handler is still Python code: Rust calls the Python function, so a `@db.atomic` function behaves exactly as on the Python pipeline, including the rollback when it raises.

## After-commit callbacks

`Database.after_commit(callback)` (for example `current_database().after_commit(...)`) registers a sync or async callable without arguments to run after the outermost transaction commits, which is where email, queue publishing and cache invalidation belong. Callbacks run in registration order; a rollback of the transaction, or of the savepoint scope that registered them, drops them, and an exception in one is logged without undoing the commit. Calling it outside a transaction raises `DatabaseError`. Model hooks register through `self.after_commit(callback)`; see [lifecycle hooks](model-hooks.md).

```python
# notes/apps/billing/operations.py
from hinoki.orm import current_database


async def issue(data: InvoiceIn) -> Invoice:
    async with db.atomic():
        invoice = await Invoice.acreate(data)
        current_database().after_commit(lambda: publish("invoice.issued", invoice.id))
    return invoice
```

A send that fails after the commit is logged and does not undo the commit; the callback is not retried. For an email, `current_database().after_commit(lambda: mail.send(message))` sends only once the data is durable and never on rollback; see [send email](email.md#send-after-a-commit).

## Errors and edge cases

- `after_commit` outside a transaction, or a nested scope opened from a child task, raises `DatabaseError`.
- The sync forms raise `RuntimeError` on the event loop.
- An action that is only decorated with `@db.atomic` is still reported by `HNK-TXN-001`; declare `atomic=True`.

## See also

- [Rules](rules.md): `Transactions.require_atomic` makes atomic writes a checked promise.
- [Lifecycle hooks](model-hooks.md): `self.after_commit` in model hooks.
- [Sync and async](sync-async.md)
