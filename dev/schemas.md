# Schemas

A `Schema` describes data that crosses the API boundary and validates it
strictly. Models describe tables; a column reaches a client only when a
`Schema` names it.

```python
# notes/apps/billing/schemas.py
from datetime import datetime
from typing import Annotated
from uuid import UUID

from hinoki import UNSET, Field, Schema, Unset


class InvoiceIn(Schema):
    reference: Annotated[str, Field(min_length=1, max_length=40)]


class InvoicePatch(Schema):
    reference: Annotated[str, Field(min_length=1, max_length=40)] | Unset = UNSET
    paid: bool | Unset = UNSET


class InvoiceOut(Schema):
    id: UUID
    reference: str
    paid: bool
    created_at: datetime
```

Validation, construction and JSON serialization run in Rust.

## Validate input

```python
data = InvoiceIn.validate({"reference": "INV-1"})
```

* **No coercion.** `"1"` is not `1`. Text sources are the explicit exception:
  `Schema.validate_text()`, `Form[...]`, `Query[...]` and `XML[...]` first
  convert text to the `int`, `float` and `bool` the fields declare.
* **Unknown keys are refused** unless the class [ignores them](https://hinoki.caspel.dev/dev/schemas.md#class-configuration).
* **Every problem is reported** with its path, up to 50, in
  `errors.ValidationError.details`; a request answers 422.

Constraints, normalization and custom checks are in
[validate input](https://hinoki.caspel.dev/dev/schema-validation.md). Supported field types are in
[scalar field types](https://hinoki.caspel.dev/dev/schema-types.md) and
[collections and unions](https://hinoki.caspel.dev/dev/schema-collections.md).

## Partial updates

Declare a field `X | Unset = UNSET`. A field the client did not send stays
`UNSET` and is left out of `dump()` and `to_dict()`, so
`await invoice.aupdate_from(patch)` writes only what was sent.

## Convert to and from rows

```python
# inside an async operation
invoice = await Invoice.acreate(data, customer_id=customer.id)
await invoice.aupdate_from(patch)
out = InvoiceOut.from_model(invoice)
outs = InvoiceOut.from_models(rows)
```

* Server keywords win over client values, and a `Schema` never carries the
  primary key.
* `from_model(row, **extra)` reads the expected attributes; relations must
  already be loaded. `from_models(rows)` converts a synchronous iterable to a
  list. See [schemas from models](https://hinoki.caspel.dev/dev/model-schemas.md).
* `to_dict()` returns Python values (UUID and datetime stay objects);
  `dump()` returns the JSON-compatible form. Both skip `UNSET`.

## Construct, change and project

`Schema(...)` takes Python values (`UUID`, aware `datetime`, nested `Schema`
instances) and validates them immediately. `Schema.validate(data)` is the
wire path: JSON-compatible input, aliases resolved, errors with wire names.
`from_model()` validates too, but accepts the naive datetimes some drivers
return.

An instance is frozen. `replace(**changes)` returns a validated copy; an
unknown name raises `TypeError`:

```python
card = InvoiceOut.validate(payload)
paid = card.replace(paid=True)
```

`dump()` projects to JSON-compatible data:

```python
out.dump(exclude_unset=True)
out.dump(include={"id", "reference"})
out.dump(by_alias=False)
```

* `include` and `exclude` take Python field names, top level only; exclusion
  wins.
* `exclude_unset=True` omits fields filled by defaults that the caller did not
  supply; explicit `""`, `None`, `0` or `False` are kept. A custom `__init__`
  makes it raise `TypeError`.
* With no options `dump()` honours custom `dump()` overrides; recursive options
  refuse a custom-dumped nested value. An override using the new options must
  accept the same keyword-only signature.
* A nested `Schema` is written as the type its field declares: a subclass
  instance held in a field `inner: Public` (or `Public | None`, `list[Public]`,
  `dict[str, Public]`, `tuple[Public, ...]`) shows `Public`'s fields and
  computed values, never the fields its subclass added. An exact instance is
  written unchanged. A field typed `Any` or `object` has no declared schema and
  writes the value whole, and a declared class that overrides `dump()` decides
  its own shape. A `@computed_field` is narrowed by its return annotation. `dump_for_storage` (the cache) narrows the same way. The OpenAPI document already describes the declared type. [Which positions are narrowed](https://hinoki.caspel.dev/dev/model-schemas.md#subclass-instances).
* Filtering a dump is not authorization and does not change the OpenAPI
  document: declare a `Schema` with only the fields the caller may see.
* `dump` and `replace` are reserved method names; a field with either name
  raises `RegistrationError`.

## Wire aliases

```python
# notes/apps/billing/schemas.py (continued)
class CustomerOut(Schema):
    display_name: Annotated[str, Field(alias="displayName")]


person = CustomerOut.validate({"displayName": "Aki"})
assert person.dump() == {"displayName": "Aki"}
assert person.to_dict() == {"display_name": "Aki"}
```

The alias is the only accepted input key; sending both names is an error.
`dump()`, validation errors and the OpenAPI document use wire names; a check's
default path is translated, an explicit `Invalid(path=...)` is already a wire
path. `name: str = Field(alias="...")` works too. Aliases are unique and cannot
contain `.`, `[` or `]`. Models take no aliases.

## Document a field

```python
class TagOut(Schema):
    name: Annotated[
        str, Field(max_length=40, description="Tag label.", examples=("priority",))
    ]
    legacy: Annotated[str | None, Field(deprecated=True)] = None
```

`description`, `examples` and `deprecated` appear in the OpenAPI document next
to the property and change nothing about validation.

## Many-to-many ids fields

`derive(Post, only=("title", "tag_ids"))` adds the ids field of a `many_to_many` relation when `only=` names it (`tag_ids` here comes from `ids_name="tag_ids"`; the default is `tags_ids`: a `set` of the target's keys with `Field(max_length=max_items)`); `derive(Post)` and `exclude=` take columns only, so a derived `Schema` never carries the field unless it is asked for. The field holds keys only: Hinoki never loads related rows while a `Schema` is serialized, so a read needs `prefetch_related` first (generated CRUD does it, and `from_model` reads the loaded keys). See [many-to-many fields](https://hinoki.caspel.dev/dev/crud.md#many-to-many-fields).

## Computed fields

```python
from hinoki import computed_field


class CustomerCard(Schema):
    first: str
    last: str

    @computed_field(alias="displayName", description="First and last name")
    def full_name(self) -> str:
        return f"{self.first} {self.last}"
```

`CustomerCard(first="Ada", last="Lovelace").dump()` includes
`"displayName": "Ada Lovelace"`. Computed values are output only: they follow
the declared fields in `dump()` and responses (both lanes), are `readOnly` in
OpenAPI, are left out of `to_dict()`, and a client that sends one gets an
unknown-field error. The method takes only `self` and needs a return
annotation; a name clash with a field is a `RegistrationError`.
`computed_fields()` returns them; subclasses inherit them.

## Class configuration

```python
from hinoki import SchemaConfig, SchemaExtra


class VendorEvent(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    event_id: str
    revision: int
```

`SchemaExtra.IGNORE` discards unknown keys in decoded input and request bodies;
declared fields stay strict, and ignored values are never stored or dumped.
Constructor keywords and `from_model(..., **extra)` keep their strict
signatures; each nested `Schema` uses its own configuration. The OpenAPI
document marks the schema `additionalProperties: true`.

## Generic schemas

```python
from hinoki import http

from notes.security import reader  # an `Access` policy


class Envelope[T](Schema):
    data: T
    count: int = 0


@http.get("/me", access=reader)
async def me() -> Envelope[CustomerOut]:
    return Envelope[CustomerOut](data=CustomerOut(display_name="Ada"))
```

`Envelope[CustomerOut]` is a cached `Schema` subclass with its own OpenAPI
component (`Envelope_CustomerOut_`). The bare `Envelope` cannot validate or be
a field; self-referencing generics and generic subclasses of generic schemas
raise `RegistrationError`.

## Scope and non-goals

`Schema` is designed for typed data at the HTTP/JSON and ORM boundary. The
following are intentionally out of scope:

* **Arbitrary Python types.** Every field type has a defined wire
  representation and OpenAPI schema object. Application-only objects stay
  outside the wire `Schema`.
* **Implicit lossy coercion.** JSON input is validated strictly (`"1"` is not
  `1`). Only explicit text sources (`validate_text()`, `Form[...]`,
  `Query[...]`, `XML[...]`) convert text to typed fields; reshape legacy input
  with [`@before_validate`](https://hinoki.caspel.dev/dev/schema-validation.md#reshape-input-with-before_validate).
* **Mutable instances.** Instances are frozen; call `replace(...)` for a
  validated copy.
* **Stateful or I/O-performing validation and serialization.** Checks,
  normalizers, `@before_validate` and `@computed_field` are synchronous and
  perform no I/O. Rules that depend on stored data belong in an operation.
* **A validator plugin system.** Extend validation with `@before_validate`,
  `@field_normalize`, `@field_check` and `@object_check` on the `Schema`.

## See also

- [Validate input](https://hinoki.caspel.dev/dev/schema-validation.md)
- [Scalar field types](https://hinoki.caspel.dev/dev/schema-types.md)
- [Collections and unions](https://hinoki.caspel.dev/dev/schema-collections.md)
- [Schemas from models](https://hinoki.caspel.dev/dev/model-schemas.md)
- [Glossary](https://hinoki.caspel.dev/dev/glossary.md): what "schema" means in these docs
