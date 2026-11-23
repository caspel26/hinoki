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

A refused delete raises `hinoki.db.DatabaseError` (`database request failed`),
not `UniqueViolation`, so it is not translated to `409`: check for references
first, or register an error handler for it.

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

Declare the relation on a model; Hinoki generates and migrates the bridge table
(`<owner table>_<name>`, one column per side, a composite unique constraint).

```python
class Tag(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=40, unique=True)


class Post(Model):
    id: UUID = pk(default_factory=uuid4)
    title: str = field(max_length=80)
    tags: ClassVar[ManyToMany[Tag]] = many_to_many(
        Tag, related_name="posts", ids_name="tag_ids"
    )
```

```python
# inside an async operation
await post.tags.aadd(red, blue)  # idempotent
await post.tags.aset([red], within=Tag.filter(owner=user))
tags = await post.tags.alist()
posts = await Post.filter(tags__name="red").prefetch_related("tags")
print(posts[0].tags.loaded, posts[0].tag_ids)  # only after prefetch_related
```

`aadd`, `aremove` and `aset` take model instances or keys, run in one
transaction that locks the owner row, and refuse more than `max_items` links
(`LinkLimitExceeded`). With `within=` a key outside that query is treated as
missing (`UnknownLinkTarget`, a `422`), and `aset` replaces only the links
inside the scope. `ids_name` (default `<name>_ids`) names the ids field that
[generated CRUD](crud.md#many-to-many-fields) and `derive` use (`derive` takes it only when `only=` names it). `within` is keyword-only on every read and write of the relation, and `prefetch_related(tags=queryset)` loads only targets in that queryset.
The `max_items` limit applies from both ends. A write through the reverse
relation (`tag.posts`) locks the declaring rows it touches that the caller may
see (the `within=` scope applies, so a row outside it is neither locked nor
revealed), in key order, and
refuses (`LinkLimitExceeded`) a write that would leave one of them past its own
`max_items`, so the limit cannot be raced past from either side; the reverse
relation itself holds the library-wide cap. Both ends lock in one order, the
declaring rows first and then the owner row, so two writers on opposite ends
never wait for each other. A refused write (`LinkLimitExceeded`,
`UnknownLinkTarget`) undoes its own statements in a savepoint, so a handler
that catches the error and commits leaves no link behind. Reading links stored
beyond `max_items` (the data broke its bound) raises `StoredLinksOverLimit`, a
server fault (`500`), not a validation error. Every write also locks the owner row
and re-checks it against the owner scope of generated CRUD and the admin inside
that lock, so a row that left the caller's scope reads as missing. The targets a
write resolves are held (`FOR KEY SHARE` on PostgreSQL, `LOCK IN SHARE MODE` on
MySQL and MariaDB) until the commit, so a target deleted at the same moment
either makes the write fail with `UnknownLinkTarget` or removes the new link
with it; on MySQL and MariaDB the reads that decide a write use locking reads,
because a transaction there reads one snapshot. SQLite has a single writer.
`prefetch_related` orders all targets of an owner by the target's default
ordering (else its key) as one list, however many queries it took.
`bridge="PostTag"` adopts a model you wrote instead of generating one, and an
association model with extra columns stays available as in earlier releases
(declare two foreign keys and a composite unique constraint).

### An association model with its own columns

```python
# notes/apps/billing/models.py (continued)
class Tag(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=40, unique=True)
    invoice_links: ClassVar["Related[list[InvoiceTag]]"]


@db.unique("invoice_id", "tag_id")
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
