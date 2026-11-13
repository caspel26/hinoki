# Relations, migrations and CRUD generation — design (Q005)

Status: **R1 design** (2026-11-13). Implementation follows in the steps below; each step ends with the blog green and tests that fail without it.

## Observed

* `Model` (`python/hinoki/orm.py`) is single-table. The blog stores `Post.author_id` as a plain `UUID` and creates its tables from hand-written DDL (`examples/blog/tables.py`).
* The query compiler (`crates/orm/src/query.rs`) knows one table per statement; values are `$n` placeholders, identifiers are validated and quoted.
* The owner's rule for the framework: no lazy I/O hidden behind attribute access, SQL inspectable, migrations never applied implicitly.

## Decisions

### D1 Foreign keys are explicit columns

```python
class Post(Model, table="blog_posts"):
    author_id: UUID = foreign_key(User, on_delete="restrict", related_name="posts")
```

* The column keeps its real name and type (`author_id: UUID`); the relation name is the column without `_id` (`author`).
* `on_delete` is required: `restrict`, `cascade`, `set_null` (column must be `| None`) or `no_action`. There is no default, so a deletion rule is always a decision.
* The referenced model must have a primary key; the column type must equal the key type (checked at class creation, `RegistrationError` otherwise).
* `related_name` is the reverse name; if omitted the reverse relation is not exposed.

### D2 Relations are loaded explicitly, never lazily

* Accessing `post.author` or `user.posts` on an instance where it was not loaded raises `RelationNotLoaded` (with the exact `select_related(...)` call to use). No attribute access ever touches the database.
* `Post.all().select_related("author")` — **one query**, a `LEFT`/`INNER JOIN` compiled in Rust (inner when the column is not nullable). Forward single-valued relations only.
* `User.all().prefetch_related("posts")` — **two queries**: the parents, then the children with `author_id IN (...)`, grouped in Python. Works for reverse relations and, as an alternative, for forward ones. Prefetch never loads more than the parents' keys and issues one extra query per relation name regardless of row count (proven by a statement-counting test).
* Filtering across a relation uses the same double-underscore syntax: `Post.filter(author__name="Ada")` compiles to a JOIN (one hop in this slice; deeper paths are rejected with a clear error).

### D3 Many-to-many is a model

M2M is an explicit join model with two `foreign_key` columns and a composite uniqueness, not hidden machinery. A convenience `through=` on `prefetch_related` may follow once real use shows the shape; it is **not** part of this slice (recorded in Open).

### D4 Migrations are files, planned from models, applied by a command

* `python -m hinoki migrations plan --models blog.models` compares the models with the **recorded state** and writes `migrations/NNNN-name.sql` (forward SQL, human readable) and `migrations/NNNN-name.state.json` (the schema after the migration). Names use no underscores.
* Operations in this slice: create table, add column, add foreign key, create index/unique. Operations that lose data (drop table/column, narrowing a type) are **never generated silently**: the plan prints them as `DESTRUCTIVE` and writes them only with `--allow-destructive`.
* `migrations apply` runs unapplied files in order, each in one transaction, and records `(name, checksum, applied_at)` in `hinoki_migrations`. A changed checksum of an applied file is an error. `migrations status` and `migrations sql NAME` print without touching anything.
* **Nothing applies migrations at startup.** The lifespan checks nothing about schema; the example documents running `apply` as a deploy step. `transaction=True` request code never runs DDL.
* Types map from annotations: `UUID`→`uuid`, `str`→`text` (`max_length` → `varchar(n)`), `int`→`bigint`, `float`→`double precision`, `bool`→`boolean`, `datetime`→`timestamptz`, `bytes`→`bytea`, `dict/list`→`jsonb`; `| None` drops `NOT NULL`. Unsupported annotations fail the plan.
* DDL identifiers are validated with the same rule as the compiler; DDL contains no user values.

### D5 Generated CRUD is ordinary routes

`crud(Post, create=PostIn, read=PostOut, ...)` returns a normal `API` class. Every route is a real declaration (visible in OpenAPI, overridable):

* `replaces="list"` (etc.) on a handwritten route removes the generated one of that action at include time; two routes for one action without `replaces` are a `RegistrationError`.
* Policy hooks are functions the generated code calls: `owner="author_id"` (the principal is assigned on create and required to match on update/delete, else 403/404 per the existing 401/403 rules) and `assign={"author_id": principal.id}` style field assignment. Clients can never send an assigned field (it is not in the input schema).
* Default access is the framework default (public) unless `access=` is given — generated CRUD does not silently invent security.

### D6 Sync model methods

Sync handlers run in the executor thread; the blocking call path is `run_sync(coroutine)` back onto the event loop that owns the pool. `Model.create/get/update/delete` and `QuerySet.all/first/...` call it. Using them on the event loop thread raises (it would deadlock); the error names the `a...` method to use.

### D7 `acreate`/`aupdate` typing

Accepted by the owner: **generated `.pyi` stubs** for models (`python -m hinoki stubs --models ...`), checked in CI by comparing the generated text with the committed file. Until then `**values` stays runtime-checked.

## Failure states

* Unknown relation name in `select_related`/`prefetch_related`/filter → `FieldError` listing the valid names.
* Orphan/ambiguous data (a child pointing at a missing parent cannot exist with the FK; `select_related` on a nullable key yields `None`).
* A migration whose SQL fails rolls back entirely and is not recorded.

## Steps

| Step | Deliverable | Files |
| --- | --- | --- |
| R1 | This design | `docs/RELATIONS-DESIGN.md` |
| R2 | Joins in the Rust compiler; `foreign_key`, `select_related`, `prefetch_related`, cross-relation filter; blog uses them | `crates/orm/src/query.rs`, `crates/bindings/src/query.rs`, `python/hinoki/orm.py`, `python/hinoki/relations.py` |
| R3 | Migrations: state, planner, apply, status, CLI; blog uses migrations instead of `tables.py` | `python/hinoki/migrations.py`, `python/hinoki/__main__.py` |
| R4 | Generated CRUD with `replaces` and ownership/assignment hooks | `python/hinoki/crud.py` |
| R5 | Sync model methods; model stubs generator | `python/hinoki/orm.py`, `python/hinoki/stubs.py` |
| R6 | Independent reviews, fixes, docs, handoff | — |

## Open

Deep relation paths (`a__b__c`), M2M `through=` sugar, composite keys, `ON UPDATE`, deferrable constraints, multi-schema, renames (planned as drop+add and therefore flagged), backends other than PostgreSQL.
