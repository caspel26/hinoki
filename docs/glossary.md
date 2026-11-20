---
status: implemented
related:
  - concepts.md
  - schemas.md
---

# Glossary

One word, one meaning. "Schema" has four meanings in this field, so the docs keep the word for one of them.

## The four schemas

| Write | Meaning |
| --- | --- |
| `Schema` (code font) | The Hinoki class for typed, validated request and response data: `class InvoiceIn(Schema)`. Always capitalised and in code font; say "a response `Schema`". |
| table state, migration state | What the migration planner records about the tables (`migrations/NNNN-name.state.json`). Never "the database schema". |
| OpenAPI document | The generated `/openapi.json`. Its `components.schemas` entries are "OpenAPI schema objects". |
| JSON Schema | The standard the OpenAPI document uses to describe bodies, always by its name. |

## Projects and apps

| Term | Meaning |
| --- | --- |
| manifest | `hinoki.toml`: the project's application, its apps, their prefixes and dependencies. |
| project application | The `Hinoki` object named by `project.application` (`notes.main:app`); the one a server process serves. |
| app | A package listed under `[[apps]]`: models, migrations and a `routes.py`. Not a second `Hinoki` object. |
| app prefix | The path an app's routes are mounted under, set only in `hinoki.toml` (`/billing`). |
| target | `module:attribute` naming an app or a no-argument factory, given to commands such as `hinoki dev notes.main:app`. |

## Code

| Term | Meaning |
| --- | --- |
| `Model` | A class mapped to a table. Without `@db.table`, the name is `<app>_<plural>`: `notes.apps.billing.models.Invoice` → `billing_invoices`. A column reaches a client only through a `Schema`. |
| action | One declared endpoint (`@http.get`, `@invoices.post`, ...): a function or an `API` class method. |
| route group | A `RouteGroup`: functions under one path prefix with shared `access=`, `atomic=` and `errors=`. |
| `Access` | The access policy of an action: an authentication provider and required permissions. |
| `Caller` | Who is making the request, as resolved by the provider. |
