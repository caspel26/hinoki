# Schemas derived from models, and Hinoki's own validation (Q023)

Status: **slice 1 implemented** (2026-11-13): derived schemas. Slices 2 and 3 (declarative constraints in Rust, validators) are queued below.

## Derived schemas

The equivalent of django-ninja's `ModelSchema`, without a second class name: a `Schema` takes the columns of a model with the `model=` class keyword.

```python
class PostOut(Schema, model=Post, exclude=["body_html"]):
    author_name: str                      # declared by hand: added or overriding

class PostCreate(Schema, model=Post, fields=["title", "slug", "body"])

class PostPatch(Schema, model=Post, fields=["title", "body"], optional=True)
```

* **Which columns:** `fields=[...]` (only these) or `exclude=[...]` (all but these), never both; default all columns. Names are checked against the model when the class is created.
* **Types and limits come from the model:** the column's annotation, `max_length` as `Field(max_length=...)`, nullability (`X | None`). A plain model default becomes the schema default; a column with a `default_factory` (ids, timestamps) stays required in the schema, so exclude it from input schemas (a schema can never carry the primary key into `create` anyway).
* **`optional=True`** (or `optional=["a", "b"]`) makes the fields `T | Unset = UNSET`, the PATCH shape: what the client did not send is left out of `to_dict()`.
* **Declared fields win:** an annotation in the class body replaces the derived one with the same name, and adds the others (computed values passed to `from_model(row, author_name=...)`).
* **Relations are not derived.** Foreign-key columns appear as their id (`author_id`); nested objects are declared explicitly, so what a client sees stays a visible decision (and `HNK-SCH-001` keeps working).
* **Typing:** the type checker sees only the fields declared in the body, not the derived ones. Declare a field by hand when you need static access to it. (A stub generator is a possible later step.)

## Our own validation (slices 2 and 3, implemented)

**Declarative, run in Rust** (so they cost nothing extra and appear in OpenAPI):

* `Field(one_of=["red", "green"])` on `str`, `int` and `float` fields: the value must equal one of the choices (strict types: `True` never matches `1`). Error code `one_of`; OpenAPI `enum`.
* `Field(format="email" | "url" | "slug" | "hex_color")`: a ready-made whole-value pattern (linear-time Rust syntax). It is `Field(pattern=...)` underneath, so the error code is `pattern`; combining `format=` and `pattern=` is an error.
* Lists already take `min_length`/`max_length` as item counts.

**Hooks in Python, for what cannot be declared:**

```python
class SignUp(Schema):
    password: Annotated[str, Field(min_length=8)]
    again: str

    @field_check("password")
    def not_common(self, value: str) -> None:
        if value in COMMON:
            raise Invalid("common", "too common")

    @object_check
    def same_password(self) -> None:
        if self.password != self.again:
            raise Invalid("mismatch", "the two passwords differ", path="again")
```

* Checks run **after** the structural validation succeeded, so `value` has its declared type and `self` is a fully built instance; a malformed body never reaches them.
* Every check runs (all problems come back together) and nested schemas run their own, with paths like `home.zip` and `others[1].zip`. A check raises `Invalid(code, message, path=...)`; it cannot change a value.
* They are inherited by subclasses; a schema graph without any check pays nothing.
* They are synchronous and must not do I/O: a rule that needs the database ("this email is taken") belongs in an operation that raises a declared `Conflict`.
* Same error shape as built-in validation (`path`, `code`, `message`), so clients, `explain_error` and OpenAPI treat them alike.

Not yet: cross-field comparisons declared without code (`Field(equals="password")`), checks that normalise values (trim, lower-case), and running declarative checks that need Python in the Rust engine.
