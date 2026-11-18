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
from typing import Annotated
from uuid import UUID

from hinoki import Field, Schema


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: str = ""

class NoteOut(Schema):
    id: UUID
    title: str
    body: str
```

* **Strict, no coercion.** `"1"` is not `1`; unknown keys are refused by default; every problem is reported with its path (capped at 50). Validation, instance construction and JSON serialisation run in Rust.
* **Partial updates.** Declare `title: str | Unset = UNSET`: a field the client did not send stays `UNSET` and is left out of `dump()` and `to_dict()`, so `row.aupdate_from(patch)` writes only what was sent.
* **Get data out.** `schema.to_dict()` gives Python values (UUID and datetime stay objects), `schema.dump()` the JSON-compatible form; both skip `UNSET`.
* **From rows.** `NoteOut.from_model(row, author_name=row.author.name)` converts one row; `NoteOut.from_models(rows)` eagerly converts a synchronous iterable to a list. Relations must already be loaded (nothing fetches lazily). A derived schema keeps its selected fields. Conversion reads the expected attributes rather than checking the concrete model class; missing attributes raise `AttributeError`. Values passed as `**extra` are shared across rows.
* **Into a row.** `await Note.acreate(data, author_id=me)` and `await row.aupdate_from(patch)`: keyword values set by the server win over the client's, and a schema can never carry the primary key.

`Schema.validate(data)` reports structural problems together. Values are not coerced. Declare field constraints with `Field(...)`; use `@field_normalize` for narrow, typed string canonicalization and `@field_check`/`@object_check` for synchronous rejection checks.

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

## Class configuration

`SchemaConfig` is an immutable typed configuration passed in the class declaration. By default, unknown string keys in decoded input are rejected. `SchemaExtra.IGNORE` discards them while keeping declared fields strict:

```python
from hinoki import Schema, SchemaConfig, SchemaExtra


class VendorEvent(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    event_id: str
    revision: int


event = VendorEvent.validate(
    {"event_id": "evt-1", "revision": 2, "vendor_context": {"trace": "abc"}}
)
assert event.dump() == {"event_id": "evt-1", "revision": 2}
```

Ignored values are not stored as attributes or emitted by `dump()`. A wrong type, failed constraint or missing required field among the declared fields still raises `ValidationError` before either a sync or async handler runs. This option applies to decoded `Schema.validate(...)` input and request bodies. Generated constructor keywords and explicit `from_model(..., **extra)` overrides keep their normal strict signatures. Each nested `Schema` uses its own configuration.

An alias remains the only accepted wire name. If `given_name` has alias `givenName`, sending only `given_name` is ignored as an extra and the required `givenName` field remains missing; sending both names is still a duplicate-field error. OpenAPI marks an IGNORE schema with `additionalProperties: true`, while its response serialization still includes only declared fields.

`Schema(...)` validates the final Python values immediately and raises `ValidationError` for invalid values. Pass Python objects such as `UUID`, timezone-aware `datetime` and nested `Schema` instances. `Schema.validate(decoded_data)` is the wire-input path: it accepts JSON-compatible values, resolves aliases and returns field errors using wire names. Request body binding uses that path before calling the handler. `from_model()` also validates the constructed Schema; naive datetimes returned by database drivers remain unchanged and are allowed on that model-conversion path. When a handler declares one concrete Schema return type, a direct Model result is passed through `from_model()` automatically (an invalid row is a `500`, not a `422`) and a subclass instance is sent with the declared fields only; this does not validate other handler return values such as a `dict`. See [Schemas derived from models](model-schemas.md#return-a-model-from-a-schema-annotated-handler).

## Discriminated unions

Use a discriminated union when one field can contain one of several known Schema shapes. Declare the union on a `Schema` field with `Annotated[Cat | Dog, Field(discriminator="kind")]`. Every variant needs the same required string field, the same wire alias for that field, and exactly one `Field(one_of=(...))` tag. Hinoki selects the variant by that tag in Rust; it does not try each schema in order.

```python
from typing import Annotated

from hinoki import Field, Schema, http


class Cat(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("cat",))]
    name: str


class Dog(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("dog",))]
    walks: int


Pet = Annotated[Cat | Dog, Field(discriminator="kind")]


class PetRequest(Schema):
    pet: Pet


@http.post("/pets")
async def create_pet(data: PetRequest) -> PetRequest:
    return data
```

`PetRequest.validate({"pet": {"type": "cat", "name": "Mugi"}})` returns a `PetRequest` whose `pet` is a `Cat`; manual construction takes an already validated variant instance, such as `PetRequest(pet=Dog(kind="dog", walks=2))`. A dict is not coerced into a variant. `list[Pet]`, nested Schema fields, nullable unions and `Unset` use the same discriminator rules. Unknown tags produce a field error at the tag's wire path without echoing the submitted value. The handler is not called for an invalid body, whether it is synchronous or asynchronous.

Each variant's tag field is required and cannot have a default. Tags must be unique strings, variants must be distinct Schema classes, and variant classes cannot override `dump()` because the override could remove or change the tag on the wire. Plain unions without `Field(discriminator=...)`, `Literal`, non-Schema variants, and unions directly in handler body or return annotations are not supported; wrap the union in a Schema for route input and output. The runtime check of return values is separate and applies to every action.

## Projection options on `dump()`

Use `dump()` when application code needs a JSON-compatible projection of a Schema. `include` and `exclude` accept Python field names and apply to the top-level fields only; exclusion wins if a field appears in both. There are no dotted paths or nested selection maps. Nested Schemas and supported lists are converted recursively, with `by_alias` applied at every Schema level.

```python
from typing import Annotated

from hinoki import Field, Schema


class PersonOut(Schema):
    first_name: Annotated[str, Field(alias="firstName")]
    bio: str = ""


person = PersonOut.validate({"firstName": "Aki"})
assert person.dump() == {"firstName": "Aki", "bio": ""}
assert person.dump(exclude_unset=True) == {"firstName": "Aki"}
assert person.dump(include={"first_name"}, by_alias=False) == {
    "first_name": "Aki"
}
assert person.dump(include=set()) == {}
```

`exclude_unset=True` omits values supplied by dataclass defaults or default factories when the caller did not provide that field. It does not compare values with defaults: explicitly passing `bio=""`, `None`, `0`, `False` or an empty collection keeps that value. `UNSET` is omitted regardless of this option. `from_model()` tracks attributes it actually reads and values passed via `**extra`; missing optional attributes that use Schema defaults remain unset. A custom `__post_init__` changing a field does not make it supplied. A custom `__init__` has no reliable input-presence information, so `exclude_unset=True` raises `TypeError` for its instances.

With no options, `dump()` preserves its current output and honors custom `dump()` overrides, including nested ones. Root-only `include` and `exclude` can select a custom-dumped nested value; recursive controls (`exclude_unset=True` or `by_alias=False`) refuse that value because Hinoki cannot safely inspect the override. A zero-argument override continues to work at runtime for automatic HTTP output, but widening the base signature means mypy flags that override; typed overrides that use the new options must accept the same keyword-only signature. Filtering a manually returned dictionary does not change the OpenAPI contract and is not an authorization mechanism; declare a response Schema containing only fields the caller may see. `to_dict()` remains the Python-value conversion and its existing `exclude=` behavior is unchanged.

`dump` is reserved as a `Schema` method name. Declaring a field with that Python attribute name raises `RegistrationError` instead of treating the inherited method as a default value; rename the Python field to avoid the collision. A wire alias alone cannot resolve a Python attribute-name conflict.

## Built-in and custom validation

`Field(...)` provides built-in structural constraints. `format=` accepts `email`, `url`, `slug` or `hex_color` and is shorthand for a regular-expression pattern. Numeric bounds apply to numbers; length limits apply to strings and collections; `one_of` restricts a value to the listed choices.

```python
from typing import Annotated

from hinoki import (
    Field,
    Invalid,
    Schema,
    field_check,
    field_normalize,
    object_check,
)


class Registration(Schema):
    username: Annotated[str, Field(min_length=3, max_length=32, pattern=r"[a-z0-9_]+")]
    email: Annotated[str, Field(format="email")]
    password: Annotated[str, Field(min_length=12)]
    confirmation: str

    @field_normalize("email")
    def lowercase_email(self, value: str) -> str:
        return value.lower()

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

Structural validation runs first and rejects wrong types, missing required values, unknown keys and failed constraints. `@field_normalize("email")` then receives a valid string and returns its canonical form; the result is checked against the same `Field(...)` constraints before it is applied. The same hook runs for `Schema.validate(...)`, direct `Schema(...)` construction and `from_model(...)`. It cannot rescue an input that failed the initial validation. Normalizers must be typed synchronous methods returning `str`; they cannot perform I/O or reject input.

After normalization, field checks receive final values and object checks can compare final fields. Raise `Invalid(code, message)` to reject a value; these checks return `None` and do not transform it. Non-`None` returns and async check methods are programming errors. Applicable custom-check failures are returned together in `errors.ValidationError.details`. Checks must be side-effect free; do not query the database or call external services. Put checks that depend on current data in an operation.

These decorators validate a `Schema` instance passed through `Schema.validate`; they are not run on a `Model`. Model fields have separate checks that run before ORM writes, described in [Model field constraints and validators](models.md#model-field-constraints-and-validators).

See also the [glossary](glossary.md) for what "schema" does and does not mean in these docs.
