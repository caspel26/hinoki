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

## Wire aliases

Use `Field(alias=...)` when the public JSON name should differ from the Python attribute. The alias is the only accepted input key; sending both names is an error. `dump()` writes aliases, including in nested schemas and lists, while `to_dict()` keeps Python attribute names. Validation errors and OpenAPI properties use the wire names. A custom check's default field path is translated to its alias; an explicit `Invalid(path=...)` is already a wire path.

```python
from typing import Annotated

from hinoki import Field, Schema


class PersonOut(Schema):
    first_name: Annotated[str, Field(alias="firstName")]


person = PersonOut.validate({"firstName": "Aki"})
assert person.first_name == "Aki"
assert person.dump() == {"firstName": "Aki"}
assert person.to_dict() == {"first_name": "Aki"}
```

An assigned declaration such as `display_name: str = Field(alias="displayName")` is also supported, including alongside `default_factory`. Aliases must be unique within a schema and cannot contain `.`, `[` or `]`, which are reserved by error paths. Model columns do not accept wire aliases; put API naming on the `Schema`.

## Controlled serialization

Use `serialize()` when application code needs a JSON-compatible projection of a Schema. `include` and `exclude` accept Python field names and apply to the top-level fields only; exclusion wins if a field appears in both. There are no dotted paths or nested selection maps. Nested Schemas and supported lists are converted recursively, with `by_alias` applied at every Schema level.

```python
from typing import Annotated

from hinoki import Field, Schema


class PersonOut(Schema):
    first_name: Annotated[str, Field(alias="firstName")]
    bio: str = ""


person = PersonOut.validate({"firstName": "Aki"})
assert person.serialize() == {"firstName": "Aki", "bio": ""}
assert person.serialize(exclude_unset=True) == {"firstName": "Aki"}
assert person.serialize(include={"first_name"}, by_alias=False) == {
    "first_name": "Aki"
}
assert person.serialize(include=set()) == {}
```

`exclude_unset=True` omits values supplied by dataclass defaults or default factories when the caller did not provide that field. It does not compare values with defaults: explicitly passing `bio=""`, `None`, `0`, `False` or an empty collection keeps that value. `UNSET` is omitted regardless of this option. `from_model()` tracks attributes it actually reads and values passed via `**extra`; missing optional attributes that use Schema defaults remain unset. A custom `__post_init__` changing a field does not make it supplied. A custom `__init__` has no reliable input-presence information, so `exclude_unset=True` raises `TypeError` for its instances.

The default `serialize()` output matches `dump()` for Schemas using the built-in dumper. If a Schema overrides `dump()`, controlled serialization refuses it, including when such a Schema appears in a selected nested field. This preserves custom projection/privacy behavior instead of bypassing it. Use the custom `dump()` directly or define a public projection Schema. `dump()` and automatic HTTP responses keep their existing behavior. Filtering a manually returned dictionary does not change the OpenAPI contract and is not an authorization mechanism; declare a response Schema containing only fields the caller may see. `to_dict()` remains the Python-value conversion and its existing `exclude=` behavior is unchanged.

`serialize` is reserved as a `Schema` method name. Declaring a field with that Python attribute name raises `RegistrationError` instead of treating the inherited method as a default value; rename the Python field to avoid the collision. A wire alias alone cannot resolve a Python attribute-name conflict.

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
