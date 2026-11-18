---
status: implemented
related:
  - schemas.md
  - models.md
---

# Schemas derived from models

Each example below includes its own imports. `Post` is the blog example's
model (`examples/blog/models.py`).

A `Schema` can take its fields and constraints from a `Model`. Pass the source model to the single `@derive(Model, ...)` decorator when selecting or making fields optional. A class without a decorator can still use `model=Model` to derive every column during the compatibility cycle.

```python
from blog.models import Post
from hinoki import Schema, derive


@derive(Post, only=("title", "slug", "body"))
class PostCreate(Schema): ...


@derive(Post, exclude=("author_id",))
class PostOut(Schema):
    author_name: str


@derive(Post, only=("title", "body"), partial=True)
class PostPatch(Schema): ...


@derive(Post)
class PostAll(Schema): ...
```

| Option | Effect |
| --- | --- |
| `@derive(Post, ...)` | source model and field selection |
| `only=("a", "b")` | take exactly these columns |
| `exclude=("a",)` | take every column except these |
| `partial=True` | make every selected derived field optional (`UNSET` when absent) |
| `partial=("a",)` | make only the named selected fields optional |

`only` and `exclude` cannot be combined. An empty `only=()` is invalid; `exclude=()` is equivalent to selecting all columns. Model field types, maximum lengths, defaults and nullability are used. Annotations in the class body add fields or override a derived field, and methods and checks in the body are kept. Methods can use zero-argument `super()` as usual.

Unknown columns, a non-model, an empty `only`, optional names outside the selection, a class that is not a `Schema`, or a second `@derive` on the same class fail when the class is declared. Relations are not derived as nested values; declare the API shape explicitly. Model derivation includes all columns by default, so use `only` or `exclude` when the API must omit private fields. During the compatibility cycle, `@derive(options...)` with `model=Model` remains valid and emits `DeprecationWarning`; use `@derive(Model, options...)` for new code.

### Inheriting from a Schema

A derived class inherits its base's fields as usual, and `only` or `exclude`
cannot remove an inherited field. To fail closed, a derivation whose selection
leaves out a model column that a base Schema already declares raises
`RegistrationError` when the class is declared, naming the field and the base:

```python
from blog.models import Post
from hinoki import Schema, derive


@derive(Post)
class PostAll(Schema): ...


@derive(Post, only=("title",))  # RegistrationError: inherited field 'id' from PostAll
class TitleOnly(PostAll): ...
```

Derive the narrower Schema from `Schema` (or from a base that declares only the
columns it keeps). A selection that keeps or widens the inherited columns is
allowed, and a field redeclared in the class body is kept on purpose.

## Return a Model from a Schema-annotated handler

When a handler declares one concrete response `Schema`, Hinoki converts a
directly returned `Model` with that Schema's `from_model()` projection:

```python
from uuid import UUID

from blog.models import Post
from hinoki import Schema, derive, http


@derive(Post, only=("id", "title"))
class PostSummary(Schema): ...


@http.get("/posts/{post_id}")
async def one(post_id: UUID) -> PostSummary:
    return await Post.aget(id=post_id)  # type: ignore[return-value]
```

The declared Schema decides what is sent for these results:

| Handler result | Response body |
| --- | --- |
| an instance of the declared Schema | sent as is |
| an instance of a subclass of the declared Schema | only the declared Schema's fields; the subclass's extra fields are dropped |
| one `Model` row | `from_model()` projection of that row |
| a `Model` row that fails the Schema's constraints | `500 internal_error` with the request id; the `ValidationError` is logged as the cause and not sent to the client |
| a `Model` of another class than the Schema's source model | `500 internal_error` |
| a list or `Page` of `Model` rows | `500 internal_error`; convert explicitly with `from_models()` |
| a `dict` or other plain value | `500 internal_error`; sent unchanged only with `validate_response=False` |

!!! note "Plain values are checked"
    A returned `dict` (or any other non-Schema, non-Model value) where the
    annotation names a `Schema` is a `500 internal_error`: the annotation is
    enforced at runtime (see [the response check](endpoints.md#the-response-is-checked-against-the-annotation)).
    Return a Schema instance or a Model; declare the action with
    `validate_response=False` only to send a value as it is.

A row that fails the Schema's constraints is the server's fault (for example a
stored value longer than the Schema allows), so it is never reported as a
client `422`. No relation is loaded implicitly. For a computed field such as
`author_name`, keep using `PostOut.from_model(post, author_name=...)`.

Static type checkers do not know about this runtime conversion: they see a
`Post` returned from a function annotated `-> PostSummary` as a type error.
This is a limitation of static typing, not of the handler, so the intentional
Model return needs `# type: ignore[return-value]` (or an explicit
`PostSummary.from_model(...)` call, which type-checks without one). Derived
fields are also not synthesized for static type checkers, so declare fields in
the Schema body when they need static checking.

## Add validation checks

Use `@field_check("name")` for a rule on one or more structurally valid fields, and `@object_check` for a rule over the whole object. Raise `Invalid(code, message, path=...)` to report a problem. Checks are synchronous and must not perform I/O; database-dependent rules belong in an operation.

```python
from typing import Annotated

from hinoki import Field, Invalid, Schema, field_check, object_check


class SignUp(Schema):
    username: Annotated[str, Field(min_length=3, max_length=32)]
    password: Annotated[str, Field(min_length=12)]
    confirmation: str

    @field_check("username")
    def username_characters(self, value: str) -> None:
        if not value.replace("_", "").isalnum():
            raise Invalid("username_characters", "use letters, numbers and _ only")

    @field_check("password")
    def reject_common_password(self, value: str) -> None:
        if value.lower() in {"password12345", "qwerty123456"}:
            raise Invalid("common_password", "choose a less common password")

    @object_check
    def passwords_match(self) -> None:
        if self.password != self.confirmation:
            raise Invalid("mismatch", "passwords do not match", path="confirmation")
```

Structural errors are reported before custom checks. All applicable check failures are returned together. See [schemas](schemas.md) for the validation contract and conversion methods.
