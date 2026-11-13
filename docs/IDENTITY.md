# Hinoki's identity

These choices are adopted and applied. Hinoki's identity comes from verifiable behaviour and a consistent vocabulary, not from inventing a second API grammar.

## Product promise

**A Python API framework that checks its own architecture.** `hinoki check` reports deterministic rule violations with a location, stable ID and a concrete fix. Rust is the implementation choice for selected request, schema, crypto and ORM paths; it is not a performance claim.

The endpoint declaration is ordinary Python: `@get` / `@post`, reusable `Access`, and `access=`, `transaction=` and `errors=` options. Group defaults use `Endpoints`. The same declarations feed runtime routing, OpenAPI, MCP descriptions and the strict test client.

## Vocabulary and schemas

Use the Hinoki class name `Schema` in code font. A model-derived schema is still a `Schema`:

```python
class PostOut(Schema, model=Post, exclude=["password_hash"]):
    ...
```

There is no separate `ModelSchema` type or alias. The term is only a comparison for the use case. Do not write Pydantic method names for Hinoki: use `validate`, `dump`, `to_dict` and `from_model` as documented. The conditional `Shape` renaming test remains a future reconsideration trigger: run 10–15 assistant coding tasks, and reconsider only if more than 20% use Pydantic APIs despite the docs. Until then, `Schema` is the decision.

Use `Model` for persisted rows, “table state” or “migration state” for database structure, “OpenAPI document” and “JSON Schema” for their respective documents. See [the glossary](GLOSSARY.md).

## Application and command line

Small apps can be declared as data:

```python
app = Hinoki(
    settings=AppSettings,
    include=[health, Tasks],
    modules=[DatabaseModule],
)
```

Factories remain appropriate when setup depends on runtime inputs or conditionally installs features. `hinoki dev`, `hinoki serve`, `hinoki check`, `hinoki settings`, `hinoki migrations` and `hinoki new` are the documented command forms.

## Errors and status

Diagnostics say what failed and where, then provide an actionable `fix:` line when a deterministic correction exists. Settings and migration CLI errors follow the same pattern. Keep the project's pre-alpha status explicit; do not present planned work or unmeasured performance as available.

## Visual identity

The docs and project pages use the Hinoki violet and tree-ring motif. The admin has three structurally distinct themes—Studio, Console and Paper—with light and dark modes for each. The logo and banner are the project's own assets.
