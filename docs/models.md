---
status: implemented
related:
  - relations.md
  - migrations.md
  - model-options.md
---

# Models and queries

`Model` maps a Python class to one database table. SQLite, PostgreSQL, MySQL and MariaDB are supported. Its annotated fields define the columns; queries are explicit and do not load relations implicitly. See [configure a database](databases.md) for setup and backend-specific limits.

## Define a model

```python
from datetime import datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, OnDelete, field, foreign_key, indexed, ordered_by, unique
from hinoki.utils import utc_now


class Member(Model, table="members"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    email: str = field(unique=True, max_length=254)


@indexed("owner_id", "created_at")
@ordered_by("-created_at", "-id")
class Task(Model, table="tasks", label="Task", plural="Tasks"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID = foreign_key(Member, on_delete=OnDelete.RESTRICT, related_name="tasks")
    title: str = field(max_length=120)
    done: bool = False
    created_at: datetime = field(default_factory=utc_now)
```

`table=` is required. A model has exactly one primary key. Fields are keyword-only when constructing a model. Single-column uniqueness is `field(unique=True)`; use `@unique("a", "b")` for a composite constraint. There is no nested `class Meta`.

Supported field metadata includes primary key, uniqueness, maximum string length, defaults and foreign-key behavior. Database types are inferred from annotations; unsupported types fail during model or migration validation.

### Model field constraints and validators

`field()` metadata describes the database column and its migration: `max_length` becomes a bounded string column and a derived `Schema` constraint, `unique` becomes a database constraint, and `default` / `default_factory` provide values when constructing an instance. A `foreign_key(...)` declares the referenced table and its `OnDelete` behavior. The Python annotation supplies the column type and nullability.

Use `constraints=Field(...)` for built-in value checks and `validators=(...)` for synchronous, application-specific checks. Existing `max_length=` remains a shorthand:

```python
from uuid import UUID, uuid4

from hinoki import Field
from hinoki.orm import Model, field


def normalized(value: str) -> None:
    if value != value.strip():
        raise ValueError("surrounding whitespace is not allowed")


class Product(Model, table="products"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    sku: str = field(
        max_length=32,
        constraints=Field(min_length=3, pattern=r"[A-Z0-9-]+"),
    )
    price: float = field(constraints=Field(gt=0))
    name: str = field(max_length=120, validators=(normalized,))
```

Built-in checks support string/list lengths, string patterns and membership, and numeric bounds and membership. Before applying checks, runtime type validation follows annotations for fields that declare constraints or validators, including nested list and dict values. Invalid declarations fail when the model class is created. Validators run immediately before inserts and ORM updates, including filtered bulk updates; they do not run on construction, assignment or reads. A refusal raises `ModelValidationError` with `details` containing each field's `path`, stable `code` and `message`, before SQL is sent. In an HTTP request it produces the standard 422 validation response; the admin displays the field messages. Custom validators are synchronous; raise `ValueError` to refuse a value. Do not perform database I/O or mutate values inside a validator. Rules involving other rows, permissions or current state belong in an operation.

These checks complement, rather than replace, a `Schema` at the HTTP boundary. Schema validation can report request errors before calling an operation; Model validation also protects CLI, admin and direct ORM writes. Database constraints remain authoritative, and their enforcement varies by backend; see [Schema validation](schemas.md#built-in-and-custom-validation).

Hinoki stores PostgreSQL UUID, timestamp, JSON and byte values in their native column types. SQLite stores UUIDs and timestamps as text, JSON as JSON text and bytes as blobs. SQLite has one writer at a time; its transactions use a bounded connection pool and a busy timeout. SQLite string `contains` matching follows SQLite's case-folding behavior (ASCII case-insensitive).

## Query rows

Class methods and querysets offer sync and async variants for common operations. Async variants are preferred in async handlers:

```python
task = await Task.aget(id=task_id)
tasks = await Task.all().filter(owner_id=member_id, done=False).order_by("title")
page = await Task.all().filter(done=False).apaginate(PageParams(page=1, page_size=20))
```

Filters accept validated field names and supported comparison/membership operators. Values are bound parameters; SQL identifiers are compiled from model metadata. Query methods do not issue SQL until an evaluation method such as awaiting the query, `aget`, `acount`, `aupdate` or `adelete` is called.

`@ordered_by` supplies ordering only when a query does not specify `order_by`. An explicit order replaces it. Counts and bulk updates do not inherit default ordering.

## Reusable queries

A reusable query is a typed class method that returns a `QuerySet`. Hinoki has no hidden default manager: `Model.all()` always means every row.

```python
class Task(Model, table="tasks"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=200)
    done: bool = False
    deleted_at: datetime | None = None

    @classmethod
    def active(cls) -> QuerySet[Self]:
        return cls.filter(deleted_at=None)


open_tasks = await Task.active().filter(done=False).order_by("title")
everything = await Task.all()           # the explicit way around the policy
```

Pass the method where the framework asks for a base query, so every operation built on it sees the same rows:

```python
from hinoki import CrudAPI


class Tasks(CrudAPI, model=Task, path="/tasks"):
    read_schema = TaskOut

    def get_queryset(self, principal):
        return Task.active()


class TaskAdmin(ModelAdmin[Task]):
    def queryset(self, principal):
        return Task.active()
```

Choose the place by what the rule is:

| Rule | Where | Why |
| --- | --- | --- |
| Row order | `@ordered_by(...)` ([model options](model-options.md)) | declared once, replaced by any explicit `order_by` |
| A policy the whole app shares, such as hiding soft-deleted rows | a named class method such as `Task.active()`, used in `CrudAPI.get_queryset()` or the admin's `queryset()` | visible at each call site and in review; nothing is hidden implicitly |
| Rows that depend on the caller | `CrudAPI.get_queryset(principal)` or `ModelAdmin.queryset(principal)` | the caller is available there; model-level code never sees it |

Authorization must not depend on a default that a call to `all()` or a raw query can skip: keep ownership and permission checks in `access=`, `owner=` and `ModelAdmin.can()`, and treat a reusable query as convenience, not as the security boundary.

To generate write routes as well, provide `create=` or `update=` schemas and/or
set `deletable = True`; protect generated writes with `access = ...`. See
[Generate CRUD routes](crud.md) for the full route and ownership contract.

## Create and update

```python
task = await Task.acreate(TaskInput(title="Write docs"), owner_id=member_id)
task.done = True
await task.asave()  # updates changed columns
```

Server-supplied keywords take precedence over values from a `Schema`. A loaded model remembers its stored values; `asave()` updates changed columns and refuses primary-key changes. For a conditional write, use `aupdate_where` so ownership and state checks are part of the SQL condition. `QuerySet.aupdate(...)` and `adelete()` act on every row the filters match; they ignore ordering and refuse a queryset with `limit` or `offset`, so select the rows first and then filter on their keys (`filter(id__in=[...])`).

The model constructor is statically typed. Some keyword-based model methods still have broader type-checker signatures; see [typing](typing.md).

## Share fields: abstract models and mixins

Fields, options and methods that several models need can live in a base class. An **abstract model** has no table of its own: it is not migrated, cannot be queried or saved, and exists to be subclassed.

```python
from datetime import datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, field, indexed, ordered_by, timestamps
from hinoki.utils import utc_now


class Keyed(Model, abstract=True):
    id: UUID = field(primary_key=True, default_factory=uuid4)


@timestamps()
@indexed("updated_at")
@ordered_by("-updated_at")
class Tracked(Model, abstract=True):
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


class Article(Keyed, Tracked, table="articles"):
    title: str = field(max_length=120)
```

`Article` has the columns `created_at`, `updated_at`, `id` and `title`, is ordered by `-updated_at`, has the index and refreshes `updated_at` on every update. `Tracked.all()` raises `ModelError`: query `Article`.

* `abstract=True` is not inherited. A subclass is a table model (and needs `table=`) unless it says `abstract=True` again. An abstract model needs no primary key; a table model needs exactly one, its own or an inherited one.
* **Options** declared on abstract bases are inherited: every base's `@unique` constraints and `@indexed` indexes, and the order and timestamp column of the first base (in the order the bases resolve) that sets one. Applying a decorator on the subclass *replaces* what was inherited for that kind of option; it does not add to it. To keep an inherited index while adding one, declare both on the subclass. Options you do not touch stay inherited. A replacement made in an abstract base holds for everything below it: a model reads its options from the abstract bases it lists, and each of those already carries what it inherited and replaced.
* **Plain mixins** work too: a class that is not a model contributes the fields it annotates, `field(...)` and `foreign_key(...)` included, and its methods. An annotation marked `ClassVar` is not a field. A type checker does not see a plain mixin's fields as constructor arguments, so prefer an abstract model when you construct instances with those fields.
* **One name, two types.** If two bases give the same field name different types and the class does not declare that field itself, the class is refused with an error naming both bases. The same type from two bases is accepted, and a class (or an abstract subclass) that redeclares a field decides its type. A type checker still reports a redeclaration with an incompatible type, as it does for any class.
* **Subclassing a table model** is allowed: the subclass needs its own `table=` and gets a copy of the parent's columns in a separate table, which `hinoki migrations plan` shows. The two tables are unrelated: there is no join between them and a query on the parent does not return the subclass's rows. A table model does not pass its options on; only abstract bases do.
* **Column order** follows Python's dataclass rules: the fields of model bases come first, starting from the last base listed, then the fields of plain mixins, then the class's own. Hinoki does not depend on the order; a new table is created with it.

A foreign key in an abstract model or a mixin needs a reverse name that differs per subclass; see [relations](relations.md#foreign-keys-in-abstract-models-and-mixins).

## Relations

Declare a foreign-key column with `foreign_key(OtherModel, ...)`. It creates the `other_id` column and a named relation (`row.other`), but reading the relation does not fetch data. Load it explicitly with `select_related` or `prefetch_related`. For many-to-many data, declare an association model with two foreign keys and a composite `@unique` constraint; see [relations and loading](relations.md).

## Schema options

Use [model option decorators](model-options.md) for indexes, ordering and composite uniqueness. Use [schemas](schemas.md) to define request and response data; a model does not decide which columns cross the HTTP boundary.

## Migrations

Model changes do not alter a database. Generate and review migration files, then apply them deliberately with the [migration commands](migrations.md).
