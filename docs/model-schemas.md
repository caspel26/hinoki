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
    `validate_response=False`. A subclass instance is sent as the
    declared type: see [Subclass instances](#subclass-instances) below. See
    [the response check](endpoints.md#the-response-is-checked-against-the-annotation).

## Subclass instances

A response sends the type its annotation declares. An instance of a subclass
(`Private(Public)`) shows the fields and `@computed_field` values of the
declared class, never the fields the subclass added, in the JSON, XML and
`dump()` outputs alike. An exact instance is sent unchanged.

Narrowed, wherever the annotation names a `Schema` `S`:

- the top level and a `Schema` field: `S`, `S | None`, a union of `S` with other
  types, `list[S]`, `list[S | None]`, `list[list[S]]`, `tuple[S, ...]`,
  `tuple[S, T]`, `dict[str, S]`, `Page[S]` and `CursorPage[S]` (the top level
  only for the two pages), and the `T` field of a generic `Envelope[S]`;
- the top level also takes a `TypedDict` of these and any combination of them;
  top-level `dict`, `tuple` and `TypedDict` shapes are narrowed too, with
  `validate_response=False` where the check refuses the annotation;
- the abstract forms `Sequence[S]`, `MutableSequence[S]`, `Collection[S]`,
  `Iterable[S]` (a returned `list`, `tuple`, `set` or `frozenset`), `set[S]`,
  `frozenset[S]`, `Set[S]`, `MutableSet[S]` (a set holding a subclass instance
  is written as a list, in JSON and XML alike; the response check refuses a
  set annotation, so these take `validate_response=False`), `Mapping[str, S]` and
  `MutableMapping[str, S]` (a returned `dict`), and a PEP 695 `type` alias of
  any of these (`type Items = list[S]`; a generic one with its arguments substituted, `type Box[T] = list[T]` as `Box[S]`, `type Ident[T] = T`, `type Pair[A, B] = dict[B, A]`). A recursive alias that holds a `Schema` (`type Tree[T] = list[Tree[T]] | T`) and an alias with a `TypeVarTuple` parameter cannot be narrowed: the app stops with a `RegistrationError` that names the action;
- the return annotation of a `@computed_field`: a method declared `-> Public`
  that returns a `Private` sends `Public`'s fields;
- a tuple returned where `list[S]` is declared (with `validate_response=False`:
  the response check, on by default, answers `500` for it), and a lazy projection
  (`aget_as`, `aproject`) held anywhere above;
- the `reply=` of a WebSocket route, for `send_json`.

Written whole, because no declared schema decides them:

- a position typed `Any` or `object`;
- a dataclass, and a `Page` inside another container (`dict[str, Page[S]]`);
- a declared class that overrides `dump()`: its own `dump()` decides the shape;
- a value that is not a `list`, `tuple`, `set`, `frozenset` or `dict` where a
  collection is declared (a generator or iterator returned for `Iterable[S]`, a
  mapping that is not a `dict`) is not walked, and the JSON and XML encoders
  refuse it: return the declared container;
- a `TypedDict` that refers to itself
  (`class Tree(TypedDict): item: S; kids: list[Tree]`) is narrowed at its first
  level only: a `Tree` nested inside it, at any depth, is written as it is, so a
  subclass of `S` held there shows its added fields. Declare a `Schema` for a
  recursive structure instead;
- a `list` that a lazy projection already holds, changed afterwards with
  `list.append(projection, subclass_instance)`: the JSON encoder drops the
  appended item and the XML encoder writes the list storage as it is.

A narrowed value keeps the wire names (aliases) of an exact one.
[Custom renderers](codecs.md#inherit-and-override) that take objects receive the
narrowed instance as a `Narrowed` view of the declared class.

## See also

- [Schemas](schemas.md)
- [Validate input](schema-validation.md)
- [Generate CRUD routes](crud.md): `read=`, `create=` and `update=` take these schemas
