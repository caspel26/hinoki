# 7. Validate input

`Schema` validation has two stages. `Field(...)` declares built-in type and value constraints. Custom synchronous checks add rules that depend on one field or compare several fields. Both run before a validated schema reaches an operation.

```python
from typing import Annotated

from hinoki import Field, Invalid, Schema, errors, field_check, object_check


class Registration(Schema):
    username: Annotated[
        str,
        Field(min_length=3, max_length=32, pattern=r"[a-z0-9_]+"),
    ]
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


valid = Registration.validate(
    {
        "username": "mari_23",
        "email": "mari@example.test",
        "password": "correct-horse-battery",
        "confirmation": "correct-horse-battery",
    }
)
assert valid.username == "mari_23"
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

The invalid call reports a `common_password` error on `password` and a `password_mismatch` error on `confirmation`. `min_length`, `max_length` and `pattern` are built-in field constraints. `format="email"` is a ready-made pattern; `url`, `slug` and `hex_color` are also available. Numeric fields can use `gt`, `ge`, `lt` and `le`, and `one_of` restricts a value to a fixed set. An invalid built-in constraint prevents custom checks from running.

`@field_check("password")` receives the already validated value. `@object_check` receives the complete schema and can compare fields. Raise `Invalid(code, message)` to report a failure; the method does not transform the value. Checks are synchronous, have no I/O, and all applicable failures are collected. Structural failures such as a missing field or wrong type are reported first. A check that needs current database state, such as whether an email is already registered, belongs in an operation where it can be made race-safe.

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
