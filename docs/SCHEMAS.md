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

## Differences from Pydantic

If you come from Pydantic (or an assistant suggests its API), these do not exist or behave differently:

| Pydantic | Hinoki |
| --- | --- |
| `BaseModel`, `model_config`, `ConfigDict` | `Schema`; class-level options are keyword arguments or decorators, never `model_config` |
| `model_validate`, `parse_obj` | `Schema.validate(data)` (raises a validation error listing every problem) |
| `model_dump`, `.dict()`, `.json()` | `to_dict()` (Python values), `dump()` (JSON-compatible) |
| Coercion in "lax" mode (`"1"` becomes `1`) | none: strict only |
| `Field(..., ge=, le=, pattern=)` | `Field(ge=, le=, gt=, lt=, min_length=, max_length=, pattern=)`; patterns use Rust syntax, whole-value, no look-around |
| `validator`, `field_validator` | custom validators are planned (see the roadmap), not available yet |
| Mutable instances | instances are immutable |

See also the [glossary](GLOSSARY.md) for what "schema" does and does not mean in these docs.
