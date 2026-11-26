---
status: implemented
related:
  - schemas.md
  - models.md
  - model-schemas.md
---

# 8. Validate input

A `Schema` validates in three steps, all before your code runs: built-in
constraints reject invalid input, normalizers make valid strings canonical, and
checks reject values that are valid but not acceptable.

## Normalize and check fields

```python
# registration.py
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
                "password_mismatch", "passwords do not match", path="confirmation"
            )
```

```python
# registration.py (continued)
user = Registration.validate(
    {
        "username": "mari_23",
        "email": "Mari@Example.test",
        "password": "correct-horse-battery",
        "confirmation": "correct-horse-battery",
    }
)
assert user.email == "mari@example.test"

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

```text
password common_password choose a less common password
confirmation password_mismatch passwords do not match
```

- **Constraints** come first: `min_length`, `max_length`, `pattern`, and
  `format=` (`email`, `url`, `slug`, `hex_color`); numbers take `gt`, `ge`,
  `lt` and `le`, and `one_of` fixes the allowed values. A failed constraint
  stops validation of that field.
- **`@field_normalize`** receives a valid string and returns a `str`; the result
  is checked against the same constraints. It also runs for `Schema(...)` and
  `from_model(...)`.
- **`@field_check`** sees the final value of one field, **`@object_check`** the
  whole schema. Both raise `Invalid(code, message)` and return `None`.

Normalizers and checks are synchronous and do no I/O. A rule that needs the
database, such as "this email is not registered yet", belongs in an operation,
where it can be race-safe.

## Ignore unknown fields

Unknown fields are rejected by default. For an external payload that adds
metadata you do not use, discard unknown keys on that `Schema`:

```python
# webhooks.py
from hinoki import Schema, SchemaConfig, SchemaExtra


class Webhook(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    event_id: str
    revision: int


event = Webhook.validate(
    {"event_id": "evt-1", "revision": 2, "provider_data": {"trace": "abc"}}
)
assert event.dump() == {"event_id": "evt-1", "revision": 2}
```

Declared fields stay strict, and ignored keys are never stored or dumped.
Constructor keywords and `from_model(..., **extra)` stay strict too, and each
nested `Schema` applies its own policy.

## Validate model writes

Data can also reach a table without an HTTP `Schema`: from a management command
or the admin. Put the same constraints on the model:

```python
# catalog/models.py
from uuid import UUID, uuid4

from hinoki import Field, db
from hinoki.orm import Model, field, pk


def trimmed(value: str) -> None:
    if value != value.strip():
        raise ValueError("surrounding whitespace is not allowed")


@db.table("catalog_products")
class Product(Model):
    id: UUID = pk(default_factory=uuid4)
    sku: str = field(
        max_length=32, constraints=Field(min_length=3, pattern=r"[A-Z0-9-]+")
    )
    name: str = field(max_length=120, validators=(trimmed,))
    price: float = field(constraints=Field(gt=0))
```

The checks run before `acreate`, `asave` and queryset `aupdate`, and their
synchronous forms. An invalid write raises `ModelValidationError` (import it
from `hinoki.errors`) with structured `details` and sends no SQL. Rows that are
read are not revalidated.
See [field constraints and validators](../models.md#model-field-constraints-and-validators).

## Recap

- Constraints, then normalizers, then checks; all before the handler.
- Unknown keys are an error unless a `Schema` opts out.
- Model constraints guard writes that bypass HTTP.

Next: [9. Projects and apps](8-projects.md).
