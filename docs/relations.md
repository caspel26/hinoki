---
status: implemented
related:
  - models.md
  - queries.md
  - migrations.md
---

# Relations

A relation is an ordinary foreign-key column. Related rows load only when a
query asks for them: attribute access never performs I/O.

## Declare a foreign key

```python
# notes/apps/billing/models.py
from typing import ClassVar
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, OnDelete, Related, field, foreign_key, pk


class Customer(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=120)
    invoices: ClassVar["Related[list[Invoice]]"]


@db.indexed("customer_id")
class Invoice(Model):
    id: UUID = pk(default_factory=uuid4)
    customer_id: UUID = foreign_key(
        Customer, on_delete=OnDelete.RESTRICT, related_name="invoices"
    )
    reference: str = field(max_length=40, unique=True)
    customer: ClassVar[Related[Customer]]
```

`customer_id` is stored on `billing_invoices`. `invoice.customer` is the
forward relation (named after the column without `_id`); `customer.invoices` is
the reverse relation, created only when `related_name` is set. The
`ClassVar[Related[...]]` lines are for type checkers. The column type must match
the referenced primary key.

`on_delete` is required and takes an `OnDelete` member:

| `on_delete` | When the referenced row is deleted |
| --- | --- |
| `OnDelete.RESTRICT` | the delete is refused while references exist |
| `OnDelete.CASCADE` | referencing rows are deleted too |
| `OnDelete.SET_NULL` | the column becomes `NULL`; the field must be `\| None` |
| `OnDelete.NO_ACTION` | like `RESTRICT`, checked at the end of the statement |

Text such as `on_delete="cascade"`, and `SET_NULL` on a non-nullable field, are
refused with `RegistrationError` at import:

```python
# notes/apps/billing/models.py (continued)
@db.indexed("invoice_id")
class Payment(Model):
    id: UUID = pk(default_factory=uuid4)
    invoice_id: UUID | None = foreign_key(
        Invoice, on_delete=OnDelete.SET_NULL, default=None
    )
```

The rule becomes the migration's `REFERENCES … ON DELETE` clause and the
database enforces it.

## Load related rows

```python
# inside an async operation
invoices = await Invoice.all().select_related("customer")
names = [invoice.customer.name for invoice in invoices]

customers = await Customer.all().prefetch_related("invoices")
counts = {c.id: len(c.invoices) for c in customers}
```

| Method | Use for | SQL |
| --- | --- | --- |
| `select_related("customer")` | a forward foreign key (one row) | one `LEFT JOIN` in the same statement |
| `prefetch_related("invoices")` | a reverse or collection relation | one separate, bounded query |

Reading a relation that was not loaded raises `RelationNotLoaded`. `acount()`
and `apaginate()` drop a `select_related` join from their `COUNT` (a to-one
join cannot change the count) but keep a join a filter needs, such as
`customer__name=...`.

## One-to-one

A one-to-one relation is a foreign key with `unique=True`:

```python
# notes/apps/billing/models.py (continued)
class BillingProfile(Model):
    id: UUID = pk(default_factory=uuid4)
    customer_id: UUID = foreign_key(
        Customer, on_delete=OnDelete.CASCADE, unique=True, related_name="profile"
    )
    vat_number: str = field(max_length=20)
    customer: ClassVar[Related[Customer]]
```

Add `profile: ClassVar["Related[BillingProfile | None]"]` to `Customer` for
type checkers. `profile.customer` has one required target; `customer.profile` is
`BillingProfile | None`. Load them with `select_related("customer")` and
`prefetch_related("profile")`. If the database has drifted and holds several
rows for one customer, `prefetch_related` raises `MultipleObjectsReturned`.

The migration creates the foreign key and the unique constraint together. It
fails on existing duplicates; clean them up first. Turning an existing foreign
key into `unique=True` rebuilds the table on SQLite and needs a hand-written
migration on PostgreSQL, MySQL and MariaDB.

## Many-to-many

There is no implicit join table or `through=`: declare the association model.

```python
# notes/apps/billing/models.py (continued)
from hinoki.orm import unique


class Tag(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=40, unique=True)
    invoice_links: ClassVar["Related[list[InvoiceTag]]"]


@unique("invoice_id", "tag_id")
@db.indexed("tag_id")
class InvoiceTag(Model):
    id: UUID = pk(default_factory=uuid4)
    invoice_id: UUID = foreign_key(
        Invoice, on_delete=OnDelete.CASCADE, related_name="tag_links"
    )
    tag_id: UUID = foreign_key(
        Tag, on_delete=OnDelete.CASCADE, related_name="invoice_links"
    )
```

The composite unique constraint stops duplicate pairs and indexes `invoice_id`;
`@db.indexed("tag_id")` indexes the other side. Link and read:

```python
# inside an async operation
tag = await Tag.acreate(name="priority")
await InvoiceTag.acreate(invoice_id=invoice.id, tag_id=tag.id)

links = await InvoiceTag.filter(invoice_id=invoice.id).select_related("tag")
tag_names = [link.tag.name for link in links]
```

`invoice.tag_links` holds `InvoiceTag` rows (load it with
`prefetch_related("tag_links")`), not `Tag` objects. The association model can
carry its own columns, such as when the tag was added.

## Foreign keys in abstract models and mixins

A foreign key in an [abstract model or mixin](models.md#share-fields-abstract-models-and-mixins)
is registered once per table model that inherits it. Give the reverse relation
a per-model name with the `{model}` placeholder (the class name in lower case):

```python
# notes/apps/billing/models.py (continued)
class CustomerOwned(Model, abstract=True):
    customer_id: UUID = foreign_key(
        Customer, on_delete=OnDelete.CASCADE, related_name="{model}_set"
    )


class CreditNote(CustomerOwned):
    id: UUID = pk(default_factory=uuid4)


class Estimate(CustomerOwned):
    id: UUID = pk(default_factory=uuid4)
```

`Customer` gets `creditnote_set` and `estimate_set`. A fixed `related_name`
makes the second inheriting model fail with an error naming both. Without
`related_name` there is no reverse relation. A foreign key cannot reference an
abstract model. `{model}` is the only placeholder.

## See also

- [Query and write rows](queries.md)
- [Migrations](migrations.md): foreign-key changes and backend notes
