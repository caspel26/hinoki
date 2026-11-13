# Schemas derived from models

A `Schema` can take its fields and constraints from a `Model`. You still choose which fields cross the API boundary.

```python
class PostCreate(Schema, model=Post, fields=["title", "slug", "body"]):
    pass


class PostOut(Schema, model=Post, exclude=["author_id", "internal_note"]):
    author_name: str


class PostPatch(Schema, model=Post, fields=["title", "body"], optional=True):
    pass
```

`fields=` selects columns explicitly; `exclude=` starts from the model and omits named columns. They cannot be combined. `optional=True` makes every derived field optional for partial input; a list makes only those named fields optional. Model field types, maximum lengths, defaults and nullability are used. Subclass annotations can add fields or override a derived field.

Unknown columns and invalid selections fail when the class is created. Relations are not derived as nested values; declare the API shape explicitly.

## Add validation checks

Use `@field_check("name")` for a rule on one or more structurally valid fields, and `@object_check` for a rule over the whole object. Raise `Invalid(code, message, path=...)` to report a problem. Checks are synchronous and must not perform I/O; database-dependent rules belong in an operation.

```python
class SignUp(Schema):
    password: str
    confirmation: str

    @object_check
    def passwords_match(self) -> None:
        if self.password != self.confirmation:
            raise Invalid("mismatch", "passwords do not match", path="confirmation")
```

Structural errors are reported before custom checks. All applicable check failures are returned together. See [schemas](SCHEMAS.md) for strict validation and the comparison with Pydantic.
