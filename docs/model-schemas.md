---
status: implemented
related:
  - schemas.md
  - models.md
  - endpoints.md
---

# Schemas from models

A `Schema` can take its fields and constraints from a `Model` with
`@derive(Model, ...)`. Response handlers convert rows explicitly with
`from_model`.

```python
# notes/apps/billing/schemas.py
from hinoki import Schema, derive

from .models import Invoice


@derive(Invoice, only=("reference",))
class InvoiceIn(Schema): ...


@derive(Invoice)
class InvoiceOut(Schema): ...


@derive(Invoice, exclude=("customer_id",))
class InvoiceDetail(Schema):
    customer_name: str


@derive(Invoice, only=("reference", "paid"), partial=True)
class InvoicePatch(Schema): ...
```

| Option | Effect |
| --- | --- |
| `@derive(Invoice)` | every column |
| `only=("a", "b")` | exactly these columns |
| `exclude=("a",)` | every column except these |
| `partial=True` | every selected field optional (`UNSET` when absent) |
| `partial=("a",)` | only the named fields optional |

Column types, maximum lengths, defaults and nullability carry over. Annotations
in the class body add or override fields; methods and checks are kept.
Relations are not derived: declare nested shapes explicitly. Derivation takes
every column by default, so use `only` or `exclude` when a column is private.

These fail when the class is declared, with `RegistrationError`: unknown
columns, a non-model, `only` together with `exclude`, an empty `only=()`,
`partial` names outside the selection, a class that is not a `Schema`, and a
second `@derive`. (`exclude=()` selects every column.) The older `model=` class
keyword (`class InvoiceOut(Schema, model=Invoice)`) still derives every
column; prefer `@derive(Invoice)`. Combining it with `@derive(...)` without a
model emits `DeprecationWarning`.

## Inherit from a derived schema

A subclass inherits its base's fields; `only` and `exclude` cannot remove them.
A derivation that would leave out a column the base declares fails closed:

```python
@derive(Invoice, only=("reference",))  # RegistrationError: inherited field 'id'
class ReferenceOnly(InvoiceOut): ...
```

Derive the narrower schema from `Schema` instead. A selection that keeps or
widens the inherited columns is allowed.

## Convert rows { #convert-rows }

Return a `Schema`, never a `Model`:

```python
# notes/apps/billing/api.py
from uuid import UUID

from hinoki import RouteGroup, errors
from hinoki.pagination import Page, PageNumberPagination, PageParams

from notes.security import billing_staff

from .models import Invoice
from .schemas import InvoiceDetail, InvoiceOut

invoices = RouteGroup("/invoices", access=billing_staff)


def to_detail(invoice: Invoice) -> InvoiceDetail:
    return InvoiceDetail.from_model(invoice, customer_name=invoice.customer.name)


@invoices.get("/", pagination=PageNumberPagination())
async def list_invoices(page: PageParams) -> Page[InvoiceDetail]:
    rows = await Invoice.all().select_related("customer").apaginate(page)
    return rows.map(to_detail)


@invoices.get("/{invoice_id}", errors=(errors.NotFound,))
async def get_invoice(invoice_id: UUID) -> InvoiceDetail:
    invoice = await Invoice.filter(id=invoice_id).select_related("customer").aget()
    return to_detail(invoice)
```

* `from_model(row, **extra)` reads the attributes the schema needs; `extra`
  fills fields the model lacks, such as `customer_name`. A missing attribute
  raises `AttributeError`.
* `from_models(rows, **extra)` converts a list; `extra` is shared by every row.
  For a page, `Page.map(InvoiceOut.from_model)`.
* Relations must be loaded first (`select_related`); nothing is fetched lazily.
* An invalid stored value (longer than the schema allows) is the server's
  fault: it becomes `500 internal_error`, logged with the `ValidationError`,
  never a client 422.

!!! note "Returning a model"
    A handler annotated with one concrete `Schema` still converts a single
    returned `Model` of the derived class at runtime, but type checkers reject
    it and the `HNK-API-001` rule (`Actions.forbid_model_responses`) reports
    it. Lists or pages of models, models of another class, and plain `dict`
    values are `500 internal_error` unless the action sets
    `validate_response=False`. A subclass instance is sent with the declared
    fields only, whether the action returns it alone, as `Schema | None`, in a
    `list[Schema]`, or in a `Page[Schema]` or `CursorPage[Schema]`; a list or page
    of exact `Schema` instances is sent as it is. A `Schema` nested in a field of
    another `Schema` is encoded as the instance it holds: declare the field's
    own type with the instance you mean to send. See [the response check](endpoints.md#the-response-is-checked-against-the-annotation).

## See also

- [Schemas](schemas.md)
- [Validate input](schema-validation.md)
- [Generate CRUD routes](crud.md): `read=`, `create=` and `update=` take these schemas
