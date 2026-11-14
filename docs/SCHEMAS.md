---
status: implemented
related:
  - MODEL-SCHEMAS.md
  - ERRORS.md
  - ENDPOINTS.md
---

# Schemas

A `Schema` describes data that crosses the API boundary and validates it strictly. Models describe tables; a column reaches a client only when a `Schema` names it.

```python
class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: str = ""

class NoteOut(Schema):
    id: UUID
    title: str
    body: str
```

* **Strict, no coercion.** `"1"` is not `1`; unknown keys are refused; every problem is reported with its path (capped at 50). Validation, instance construction and JSON serialisation run in Rust.
* **Partial updates.** Declare `title: str | Unset = UNSET`: a field the client did not send stays `UNSET` and is left out of `dump()` and `to_dict()`, so `row.aupdate_from(patch)` writes only what was sent.
* **Get data out.** `schema.to_dict()` gives Python values (UUID and datetime stay objects), `schema.dump()` the JSON-compatible form; both skip `UNSET`.
* **From a row.** `NoteOut.from_model(row, author_name=row.author.name)`; relations must already be loaded (nothing fetches lazily).
* **Into a row.** `await Note.acreate(data, author_id=me)` and `await row.aupdate_from(patch)`: keyword values set by the server win over the client's, and a schema can never carry the primary key.

`Schema.validate(data)` reports structural problems together. Values are not coerced. Declare field constraints with `Field(...)`; use `@field_check` and `@object_check` for synchronous checks that depend on validated schema values.

See also the [glossary](GLOSSARY.md) for what "schema" does and does not mean in these docs.
