---
status: implemented
related:
  - model-options.md
  - queries.md
  - relations.md
  - migrations.md
---

# Define models

A `Model` maps a Python class to one table. Annotated fields are the columns;
indexes, ordering and other options are [decorators](model-options.md). A model
never opens a connection on import and never loads a relation implicitly.

```python
# notes/apps/billing/models.py
from datetime import datetime
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, OnDelete, field, foreign_key, pk
from hinoki.utils import utc_now


class Customer(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=120)
    email: str = field(max_length=254, unique=True)


@db.indexed("customer_id", "created_at")
@db.ordered_by("-created_at", "-id")
class Invoice(Model):
    id: UUID = pk(default_factory=uuid4)
    customer_id: UUID = foreign_key(
        Customer, on_delete=OnDelete.RESTRICT, related_name="invoices"
    )
    reference: str = field(max_length=40, unique=True)
    paid: bool = False
    created_at: datetime = field(default_factory=utc_now)
```

The tables are `billing_customers` and `billing_invoices`. Changing a model
never changes the database: create the tables with
`hinoki migrations plan billing` and `hinoki migrations apply`
([migrations](migrations.md)).

## Declare columns

| Declaration | Column |
| --- | --- |
| `name: str` | required, not null |
| `note: str \| None = None` | nullable, optional in the constructor |
| `paid: bool = False` | default value |
| `id: UUID = pk(default_factory=uuid4)` | the primary key |
| `email: str = field(max_length=254, unique=True)` | bounded text, unique |
| `created_at: datetime = field(default_factory=utc_now, index=True)` | factory default, single-column index |
| `customer_id: UUID = foreign_key(Customer, on_delete=...)` | a reference ([relations](relations.md)) |

`field()` takes `unique`, `index`, `max_length`, `default`, `default_factory`,
`constraints` and `validators`. The annotation gives the type and nullability.

* **One primary key.** `pk()` declares it; pass `default` or
  `default_factory` when the constructor may omit it. A nullable key
  (`int | None`) raises `RegistrationError` when the class is defined.
* **Keyword-only constructor.** `Invoice(reference="INV-1", customer_id=c.id)`.
* **Indexes.** `field(index=True)` indexes one column; it cannot be combined
  with `unique=True` or `pk()`, which are already indexed. A `foreign_key` has
  no `index=` option: index it with `@db.indexed(...)`, or a composite index
  that starts with it, as `Invoice` does ([model options](model-options.md)).
* **Composite uniqueness** is `@db.unique("a", "b")`; there is no nested
  `class Meta`.

## Column types

| Python type | Stored as | Read back as |
| --- | --- | --- |
| `str`, `int`, `float`, `bool` | the backend's text, integer, float and boolean types | the same type |
| `UUID`, `datetime` | native on PostgreSQL; text on SQLite | `UUID`, `datetime` |
| `dict[...]`, `list[...]` | JSON (JSON text on SQLite, text alias on MariaDB) | decoded by the annotation |
| `bytes` | `BYTEA` on PostgreSQL, blob on SQLite | `bytes` |
| `date`, `time` | ISO text (`time` with microseconds, no timezone) | `date`, `time` |
| `timedelta` | whole microseconds, `bigint` | `timedelta` |
| `Enum` subclass | its value: `bigint` when every value is an integer, else text | the member |
| `IPv4Address`, `IPv6Address`, `IPv4Network`, `IPv6Network` | text | the `ipaddress` object; writes accept only that class |
| `Decimal` | exact, per backend (below) | `Decimal` |

A stored value that does not read back as its annotation raises `FieldError`.
`date` and `timedelta` compare and order by value. A column type without a
SQL type (a set, tuple, `Literal`, `Path`, `Secret`, `Any`, `object`, or a
union of several types such as `int | str`) raises `RegistrationError` when the
class is defined. `Annotated[str, Field(max_length=32)]` sizes the column like
`field(max_length=32)`. SQLite's `contains` folds ASCII case only.

### `Decimal` columns

| Backend | Column | Filters and ordering |
| --- | --- | --- |
| PostgreSQL | `numeric` | `=`, `ne`, `in`, `isnull`, `gt`, `gte`, `lt`, `lte`, `order_by` |
| MySQL, MariaDB | `decimal(65,30)` | the same; read back with scale 30 (`Decimal("1.5")` returns as `Decimal("1.500…0")`, equal in value) |
| SQLite | `text` (its exact spelling) | `=`, `ne`, `in`, `isnull` only; `12.3` and `12.30` differ; range filters and `order_by` raise `FieldError` |

```python
# notes/apps/billing/models.py (continued)
from decimal import Decimal


class Price(Model):
    id: UUID = pk(default_factory=uuid4)
    amount: Decimal
```

```python
# inside an async operation
# every backend
cheap = await Price.filter(amount=Decimal("9.99"))
# not SQLite
ordered = await Price.filter(amount__gte=Decimal("5")).order_by("amount")
```

* A filter value is a `Decimal`, an `int` or a numeric string; a `float`, a
  `bool`, `NaN`, an infinity or `contains` raises `FieldError`.
* `decimal(65,30)` bounds MySQL and MariaDB to 35 digits before the point;
  PostgreSQL `numeric` is unbounded.
* A table that already stores a `Decimal` as text on PostgreSQL or MySQL needs a
  reviewed migration that changes the column type.
* A generated CRUD list whose `@db.ordered_by` names a `Decimal` column takes the
  Python path on SQLite, where that ordering is refused.

## Table and display names

Hinoki derives the table name `<app>_<models>` and display names from the class.

| Model | Module | Table | Labels |
| --- | --- | --- | --- |
| `Invoice` | `notes.apps.billing.models` | `billing_invoices` | Invoice / Invoices |
| `LineItem` | `notes.apps.billing.models` | `billing_line_items` | Line item / Line items |
| `Note` | `notes.models` or `notes.models.note` | `notes_notes` | Note / Notes |
| `APIKey` | `notes.models` | `notes_api_keys` | API key / API keys |

The app is the package that holds the `models` module; a module without a
`models` segment uses its own last name. The plural is a small English rule:
consonant + `y` → `ies`; `s`, `x`, `z`, `ch`, `sh` → `es`; otherwise `s`. It
does not know irregular nouns (`Person` → `persons`).

| Override | Form |
| --- | --- |
| app part of the name | `class Invoice(Model, app="accounts")` |
| whole table name (never prefixed) | `@db.table("invoices")` |
| display names | `class Person(Model, label="Person", plural="People")` |

Pin the table name with `@db.table(...)` when a model already has migration
history and you rename the class or move its module: a changed derived name is
planned as a drop and a create, which the destructive-migration guard stops; it
is never a rename. The optional `HNK-DB-001` rule
(`Models.require_explicit_table`) requires every matching model to pin it.

Names must be portable ASCII of at most 63 characters. A derived name that is
rejected (`sqlite_…`, `hinoki_migrations`, non-ASCII such as `class Café`) does
not stop the class from being defined: `@db.table("cafes")` can still name it.
Without one, the error is raised when the model is first used (query, write,
migration plan, project or admin registration, or a foreign key to it).

## Model field constraints and validators

`field()` can also check values before every ORM write. Use
`constraints=Field(...)` for built-in checks and `validators=(...)` for
synchronous application rules:

```python
# notes/apps/billing/models.py (continued)
from hinoki import Field


def no_padding(value: str) -> None:
    if value != value.strip():
        raise ValueError("surrounding whitespace is not allowed")


class Product(Model):
    id: UUID = pk(default_factory=uuid4)
    sku: str = field(
        max_length=32, constraints=Field(min_length=3, pattern=r"[A-Z0-9-]+")
    )
    price: float = field(constraints=Field(gt=0))
    name: str = field(max_length=120, validators=(no_padding,))
```

* **When.** Immediately before inserts and ORM updates, filtered bulk updates
  included; never on construction, assignment or reads. Invalid declarations
  fail when the class is created.
* **Checks.** String and list lengths, patterns, membership, numeric bounds
  on `int`, `float` and `Decimal` (a `NaN` or infinite `Decimal` is refused),
  and `multiple_of` on `int` and `Decimal`. `strip`, `lower` and `upper`
  normalize a `str` before the other checks, and the normalized text is what
  is written (an instance being saved gets it too). A JSON column
  (`dict[...]`, `list[dict[...]]`) takes no built-in checks; validators still
  run on it.
* **Failure.** `ModelValidationError`, before any SQL, with `details` holding
  each field's `path`, `code` and `message`. In a request it becomes the
  standard 422 response; the admin shows the messages.
* **Validators** are synchronous, raise `ValueError` to refuse, perform no I/O
  and do not change the value. Rules about other rows, permissions or current
  state belong in an operation.

These checks protect CLI, admin and direct ORM writes; a `Schema` still
validates HTTP input first ([validate input](schema-validation.md)). Database
constraints remain authoritative.

## Share fields: abstract models and mixins

An abstract model has no table: it is not migrated, cannot be queried and only
exists to be subclassed.

```python
# notes/apps/billing/models.py (continued)
class Keyed(Model, abstract=True):
    id: UUID = pk(default_factory=uuid4)


@db.timestamps()
@db.ordered_by("-updated_at")
class Tracked(Model, abstract=True):
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now, index=True)


class Article(Keyed, Tracked):
    title: str = field(max_length=120)
```

`Article` has `created_at`, `updated_at`, `id` and `title`, is ordered by
`-updated_at`, indexes `updated_at` and refreshes it on every update.
`Tracked.all()` raises `ModelError`.

* `abstract=True` is not inherited; an abstract model needs no primary key, a
  table model needs exactly one, its own or inherited.
* **Options** from abstract bases are inherited: all `@db.unique` and
  `@db.indexed`, the order and timestamp column of the first base that sets
  one, a soft-delete option, and status tracks. A decorator on the subclass
  *replaces* that kind of option. A
  `field(index=True)` index belongs to its column and is dropped when a subclass
  redeclares the column without it. Constraint names are not inherited: an
  abstract model cannot use `@db.unique(..., name=...)`.
* **Plain mixins** (non-model classes) contribute annotated fields and methods;
  `ClassVar` annotations are not fields. Type checkers do not see mixin fields
  as constructor arguments: prefer an abstract model.
* **Conflicting types.** Two bases giving one field different types are refused
  unless the class redeclares the field.
* **Subclassing a table model** copies its columns into a separate, unrelated
  table; no join, no shared rows, no inherited options.
* **Column order** follows dataclass rules: model bases (last listed first),
  then mixins, then the class's own fields.

A foreign key in an abstract model needs a per-subclass reverse name; see
[relations](relations.md#foreign-keys-in-abstract-models-and-mixins).

## Lifecycle utilities

`UUIDModel`, `TimestampedModel` and `SoftDeleteModel` are composable abstract
bases. Their columns remain ordinary typed fields and are included in
`table_state()` and migration plans. `UUIDModel` supplies a UUIDv4 primary key
(`id: UUID = uuid_pk()`); `TimestampedModel` supplies `created_at` and
`updated_at`; `SoftDeleteModel` supplies an indexed nullable `deleted_at`
column.

For configurable UUID primary keys, use `uuid_pk()`: it defaults to UUIDv4,
accepts `version=7` (time-ordered RFC 9562 UUIDv7 generated in Rust, also
available as `hinoki.orm.uuid7` / `hinoki.utils.uuid7`), or any custom
`default_factory`. For opaque URL-safe identifiers or share codes, use
`urlsafe_token(nbytes=32, unique=True)` — values are generated by Rust's CSPRNG
(`hinoki.core.make_token`) and validated against `^[A-Za-z0-9_-]+$`. For
authentication secrets (password reset tokens, API keys, sessions), store a
hash instead of a raw token column.

```python
from datetime import datetime
from uuid import UUID

from hinoki import db
from hinoki.orm import (
    Model,
    SoftDeleteModel,
    TimestampedModel,
    UUIDModel,
    field,
    urlsafe_token,
    uuid_pk,
)
from hinoki.utils import utc_now


class Document(UUIDModel, TimestampedModel, SoftDeleteModel):
    title: str
    share_token: str = urlsafe_token()


@db.track_status("status", changed_at="status_changed_at")
class Order(Model):
    id: UUID = uuid_pk(version=7)
    status: str = "draft"
    status_changed_at: datetime = field(default_factory=utc_now)
```

`@db.soft_delete(column="archived_at", clock=...)` and
`@db.track_status(field="state", changed_at="state_changed_at", clock=...)`
can use custom declared columns. Decorators validate columns and types when the
model is registered; tracked status decorators can be stacked. The supplied
clock is used for managed timestamps, otherwise the model's
`@db.timestamps(clock=...)` clock is used.

On a soft-delete model, `all()`, `filter()` and model lookups see live rows
only. `with_deleted()` includes every row and `only_deleted()` selects deleted
rows. Instance `asoft_delete()` / `soft_delete()` and `arestore()` / `restore()`
run normal save hooks. `is_deleted` reports the instance state. These methods
are idempotent; unsaved instances are refused. `previous(name)` returns the
stored value (or `UNSET` on a new instance), and `changed_fields` lists fields
that differ from the last snapshot.

Status tracking updates its timestamp only when an instance save actually
changes the tracked value; an explicit timestamp value is preserved. A bulk
`QuerySet.aupdate()` is a single SQL statement and cannot compare old values, so
it updates the timestamp whenever the tracked field is included in `values`,
even when the supplied status equals the current value.

Soft deletion is an `UPDATE`; database `ON DELETE CASCADE` and `RESTRICT`
actions apply only to physical `ahard_delete()` calls. Ordinary `unique=True`
and `@db.unique(...)` constraints continue to include soft-deleted rows. A
partial unique index for live rows needs a custom SQL migration.

## Relate models through a link table

`many_to_many(...)` is a class attribute, not a column: the model keeps its own columns and Hinoki generates (or adopts) a bridge model that holds the links.

```python
# notes/apps/billing/models.py (continued)
class Label(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=40)


class Memo(Model):
    id: UUID = pk(default_factory=uuid4)
    labels: ClassVar[ManyToMany[Label]] = many_to_many(Label, related_name="memos")
```

`Memo.__bridges__` lists the generated bridge model, `MemoLabels` on the table `<memo table>_labels`; `@db.table` on `Memo` names the bridge after the new table (set it before the first migration: later table changes are not planned as renames, see [migrations](migrations.md#bridge-tables-of-many-to-many-relations)). Declaration, link writes, scopes and limits are in [relations](relations.md#many-to-many).

## See also

- [Model options](model-options.md)
- [Query and write rows](queries.md)
- [Relations](relations.md)
- [Migrations](migrations.md)
