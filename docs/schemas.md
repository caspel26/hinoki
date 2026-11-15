---
status: implemented
related:
  - model-schemas.md
  - errors.md
  - endpoints.md
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
* **From rows.** `NoteOut.from_model(row, author_name=row.author.name)` converts one row; `NoteOut.from_models(rows)` eagerly converts a synchronous iterable to a list. Relations must already be loaded (nothing fetches lazily). A derived schema keeps its selected fields. Conversion reads the expected attributes rather than checking the concrete model class; missing attributes raise `AttributeError`. Values passed as `**extra` are shared across rows.
* **Into a row.** `await Note.acreate(data, author_id=me)` and `await row.aupdate_from(patch)`: keyword values set by the server win over the client's, and a schema can never carry the primary key.

`Schema.validate(data)` reports structural problems together. Values are not coerced. Declare field constraints with `Field(...)`; use `@field_check` and `@object_check` for synchronous checks that depend on validated schema values.

## Built-in and custom validation

`Field(...)` provides built-in structural constraints. `format=` accepts `email`, `url`, `slug` or `hex_color` and is shorthand for a regular-expression pattern. Numeric bounds apply to numbers; length limits apply to strings and collections; `one_of` restricts a value to the listed choices.

```python
from typing import Annotated

from hinoki import Field, Invalid, Schema, field_check, object_check


class Registration(Schema):
    username: Annotated[str, Field(min_length=3, max_length=32, pattern=r"[a-z0-9_]+")]
    email: Annotated[str, Field(format="email")]
    password: Annotated[str, Field(min_length=12)]
    confirmation: str

    @field_check("password")
    def reject_common_password(self, value: str) -> None:
        if value.lower() in {"password12345", "qwerty123456"}:
            raise Invalid("common_password", "choose a less common password")

    @object_check
    def passwords_match(self) -> None:
        if self.password != self.confirmation:
            raise Invalid(
                "password_mismatch",
                "passwords do not match",
                path="confirmation",
            )
```

Structural checks run first and reject wrong types, missing required values and unknown keys. If they pass, field checks receive validated values and object checks can compare fields. Raise `Invalid(code, message)` to reject a value; checks do not transform it. All applicable custom-check failures are returned together in `errors.ValidationError.details`. They must be synchronous and side-effect free: do not query the database or call external services. Put checks that depend on current data in an operation.

These decorators validate a `Schema` instance passed through `Schema.validate`; they are not run on a `Model`. Model fields have separate checks that run before ORM writes, described in [Model field constraints and validators](models.md#model-field-constraints-and-validators).

See also the [glossary](glossary.md) for what "schema" does and does not mean in these docs.
