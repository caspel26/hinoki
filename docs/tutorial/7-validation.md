# 7. Validate input

`Schema` validation combines built-in type and value constraints with optional synchronous normalization and rejection checks. Constraints reject invalid input first; normalizers canonicalize valid strings; checks can then reject values based on one field or compare several fields. All of this happens before a validated schema reaches an operation.

```python
from typing import Annotated

from hinoki import (
    Field,
    Invalid,
    Schema,
    errors,
    field_check,
    field_normalize,
    object_check,
)


class Registration(Schema):
    username: Annotated[
        str,
        Field(min_length=3, max_length=32, pattern=r"[a-z0-9_]+"),
    ]
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


valid = Registration.validate(
    {
        "username": "mari_23",
        "email": "mari@example.test",
        "password": "correct-horse-battery",
        "confirmation": "correct-horse-battery",
    }
)
assert valid.username == "mari_23"
assert valid.email == "mari@example.test"
try:
    Registration.validate(
        {
            "username": "mari_23",
            "email": "mari@example.test",
            "password": "password12345",
            "confirmation": "different-password",
        }
    )
except errors.ValidationError as error:
    for detail in error.details:
        print(detail.path, detail.code, detail.message)
```

The invalid call reports a `common_password` error on `password` and a `password_mismatch` error on `confirmation`. `min_length`, `max_length` and `pattern` are built-in field constraints. `format="email"` is a ready-made pattern; `url`, `slug` and `hex_color` are also available. Numeric fields can use `gt`, `ge`, `lt` and `le`, and `one_of` restricts a value to a fixed set. An invalid built-in constraint prevents normalizers and custom checks from running.

`@field_normalize("email")` receives a structurally valid string and must return a `str`. Its result is checked against the same constraints before it is applied. Normalization also runs when you manually construct a `Schema(...)` or call `from_model(...)`; it does not run twice when an existing nested Schema is passed to a new parent. Inputs that fail the original constraints cannot be rescued by normalization.

`@field_check("password")` receives the final value. `@object_check` receives the complete schema and can compare final fields. Raise `Invalid(code, message)` to report a failure; checks must return `None` and do not transform the value. Normalizers and checks are synchronous, have no I/O, and run the same way before both sync and async handlers. A check that needs current database state, such as whether an email is already registered, belongs in an operation where it can be made race-safe.

## Ignore unknown fields from external payloads

Unknown fields are rejected by default. For an external payload that adds unrelated metadata, opt in to discarding unknown string keys on that Schema:

```python
from hinoki import Schema, SchemaConfig, SchemaExtra


class Webhook(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    event_id: str
    revision: int


event = Webhook.validate(
    {"event_id": "evt-1", "revision": 2, "provider_data": {"trace": "abc"}}
)
assert event.dump() == {"event_id": "evt-1", "revision": 2}
```

Known fields still require the declared types and constraints, and required fields remain required. Ignored keys are discarded; they are never attached to the object or returned by `dump()`. The policy affects `Schema.validate(...)` and request bodies before sync or async handlers. Constructor keywords and explicit `from_model(..., **extra)` overrides remain strict. Nested Schemas each use their own policy. Aliases also remain canonical: a Python field name does not substitute for its alias, and supplying both spellings is still an error.

## Validate values at Model write boundaries

Use the same built-in constraints on Model fields when data can be written outside an HTTP Schema, such as from a management command or the admin. Custom callbacks are synchronous and raise `ValueError` to refuse a value:

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
    name: str = field(max_length=120, validators=(normalized,))
    price: float = field(constraints=Field(gt=0))
```

The checks run before `await Product.acreate(...)`, `await product.asave()` and `await Product.filter(...).aupdate(...)`, as well as their synchronous counterparts. Construction and assignment stay ordinary dataclass operations; existing rows are not revalidated when read. Invalid writes raise `ModelValidationError` with structured `details`, and no SQL is issued for that write. Validators must not perform I/O or change values. Keep request parsing in a Schema, and put rules that depend on other rows or permissions in an operation. See [Model field constraints and validators](../models.md#model-field-constraints-and-validators).

More: [Schema constraints and checks](../schemas.md#built-in-and-custom-validation), [schemas derived from models](../model-schemas.md).
