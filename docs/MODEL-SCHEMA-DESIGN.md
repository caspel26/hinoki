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

## Queued slices

* **Slice 2, declarative constraints, run in Rust:** `Field(one_of=[...])`, `min_items`/`max_items` on lists, `format="email"|"url"|"slug"|...` (compiled to Rust patterns), cross-field comparisons.
* **Slice 3, validators:** `@field_check("password")` and `@object_check` hooks for what cannot be declared, with the same error shape and codes as built-in validation, run after the structural check; never able to change types.
