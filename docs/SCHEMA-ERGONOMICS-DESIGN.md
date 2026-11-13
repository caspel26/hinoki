# Schema ergonomics and examples — design (Q009)

Status: **E1 design, E2 implemented** (`Schema.to_dict`, `Schema.from_model`); E3–E5 open (2026-11-13). Owner request: common backend code should be short — create a row by passing the schema (or its dump) directly, convert models to response schemas without hand-written helpers — and the examples and README should show it.

## Observed

* `Schema.dump()` returns the **JSON-compatible** form (UUID and datetime become strings, `UNSET` is omitted); there is no way to get the Python values back out for a model constructor.
* Creating a row from a request body is manual: `Post.acreate(title=data.title, slug=data.slug, body=data.body, author_id=...)`. Reading a row into a response needs a helper such as the blog's `to_out(post)` that repeats every field.
* `Model.acreate(**values)` and `aupdate(**values)` accept any keyword (`docs/TYPING.md`: not checkable); `Model(...)` and `await model.asave()` are checked.
* The blog example works but is long, and the README snippets show the long forms.

## Decisions

### D1 Schemas expose their Python values

```python
data.to_dict()                       # {"title": ..., "slug": ..., "body": ...}  (Python values, UNSET omitted)
data.to_dict(exclude={"body"})       # drop fields
data.dump()                          # unchanged: JSON-compatible, for responses
```

`to_dict` converts nested schemas (and lists/dicts of them) recursively and keeps `None`; fields left `UNSET` are always omitted, which is exactly what a partial update needs. It never touches the database.

### D2 Models accept a schema where they accept values

```python
post = await Post.acreate(data, author_id=principal.identity.id)   # schema + server-side values
post = Post.from_schema(data, author_id=...)                      # unsaved, fully type-checked constructor call is not possible; see D5
await post.update_from(patch)                                      # sets the sent fields, saves only what changed
```

`acreate(data, /, **values)`: the schema's `to_dict()` is merged with `values`, and **explicit keyword values win**, so a server-assigned column (owner, state) can never come from the client even if a schema field of that name exists. Unknown names still fail (`check_field`). `update_from(schema)` assigns the non-`UNSET` fields and uses the existing change tracking of `asave`. Sync variants (`create`, `update_from` through `save`) follow the existing sync-method rules.

### D3 Response schemas build themselves from rows

```python
PostOut.from_model(post, author_name=post.author.name)
found.map(PostOut.from_model)                       # a Page of rows -> a Page of schemas
```

`Schema.from_model(row, **extra)` fills every schema field from the same-named attribute of `row`, `extra` overriding or supplying computed values, and converts nested models to nested schemas. A missing attribute is an error naming the field (never a silent default); an unloaded relation still raises `RelationNotLoaded` (no lazy I/O). Output is not re-validated (it is trusted server data), like constructing the schema directly today.

### D4 What is deliberately not added

No automatic model-to-schema derivation (`Schema.from_orm`-style magic) and no Pydantic-style `model_dump` names: explicit field lists in schemas stay the guard against leaking columns (the exposure rule `HNK-SCH-001` depends on that). Schemas are not generated from models.

### D5 Typing

The schema path is checkable where it matters: `to_dict() -> dict[str, Any]` and `from_model(...) -> Self` are fully typed; `acreate(data: Schema | None = None, /, **values: Any)` keeps the existing `**values: Any` looseness for keywords (the open point of `docs/TYPING.md`, generated stubs, is unchanged). `from_schema` returns `Self` and is checked by mypy only to the extent `Model.__init__` is, which is why it is thin sugar over `Model(**dict)`; the always-checked constructor path is unchanged and documented.

### D6 Examples and README

* `examples/blog` uses `PostOut.from_model`, `Post.acreate(data, author_id=...)` and shorter handlers; the README snippets are regenerated from the real code.
* A new minimal example, `examples/notes` (one file, about 40 lines: model, schemas, four actions, function handlers), is the README's quick start, so a reader sees a whole working API before the blog.
* Documentation states the order of precedence in `acreate` and the no-lazy-load rule in `from_model`.

## Failure, empty and private states

`to_dict()` on a schema with only `UNSET` fields returns `{}`; `update_from({})` is a no-op that does not hit the database. `acreate` with a schema naming a column the model lacks raises `FieldError` (as keyword values do). `from_model` with a missing attribute raises `AttributeError` naming schema, field and model. Nothing here widens what a client can send: fields not in the schema never reach `to_dict`.

## Tests

`to_dict` (nesting, `UNSET`, `exclude`, `None`, lists of schemas); `acreate(data, owner=...)` with precedence and with a client-supplied owner field in the schema; `update_from` changing only sent fields (statement-counting test), no-op on empty patches; `from_model` with extras, nested models, missing attribute, unloaded relation; `Page.map(Schema.from_model)`; typing fixtures (mypy strict) for the new signatures; the blog and notes examples under the existing suites and a PostgreSQL run of both.

## Steps

| Step | Deliverable | Files |
| --- | --- | --- |
| E1 | This design | `docs/SCHEMA-ERGONOMICS-DESIGN.md` |
| E2 (done) | `Schema.to_dict`, `Schema.from_model` | `python/hinoki/schema.py` |
| E3 | `Model.acreate(data, ...)`, `from_schema`, `update_from`, sync variants | `python/hinoki/orm.py` |
| E4 | Blog and README rewritten with the short forms; `examples/notes` | `examples/`, `README.md` |
| E5 | Review of precedence/mass-assignment behaviour, fixes, docs, handoff | — |

## Open (owner)

Method names (`to_dict`, `from_model`, `update_from`) are proposals; Pydantic-style aliases (`model_dump`) are intentionally not added. Whether `from_model` should be allowed to infer relations by name (`author.name` → `author_name`) is not proposed.
