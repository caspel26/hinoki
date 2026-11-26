# Glossary

One word, one meaning. "Schema" has four meanings in this field, so the docs keep the word for one of them.

## The four schemas

| Write | Meaning |
| --- | --- |
| `Schema` (code font) | The Hinoki class for typed, validated request and response data: `class InvoiceIn(Schema)`. Always capitalised and in code font; say "a response `Schema`". |
| table state, migration state | What the migration planner records about the tables (`migrations/NNNN-name.state.json`). Never "the database schema". |
| OpenAPI document | The generated `/openapi.json`. Its `components.schemas` entries are "OpenAPI schema objects". |
| JSON Schema | The standard the OpenAPI document uses to describe bodies, always by its name. |

## Names

| Name | Origin |
| --- | --- |
| Hinoki | 檜, the Japanese cypress of temples: fragrant, durable wood. |
| Kiso | Hinoki's Rust server, named after the Kiso Valley (Nagano, Japan), known for its hinoki forests such as Akasawa. |
| mailer | The per-worker email resource (`hinoki.mail.Mailer`): it owns the SMTP connection pool, is opened in the worker's lifespan and closed at shutdown. |
| email template | A `hinoki.mail.Template`: a subject, HTML and optional text in a small fixed language, checked against a context `Schema` and rendered in Rust. Not an admin template or an HTML page. |

## Projects and apps

| Term | Meaning |
| --- | --- |
| manifest | `hinoki.toml`: the project's application, its `[project] routes` map, middleware and rules, and its registered `[[apps]]` with their optional persistence, events and dependencies. |
| project application | The `Hinoki` object named by `project.application` (`notes.main:app`); the one a server process serves. |
| app | A package listed under `[[apps]]` (with optional `models`/`migrations`, `events`, `requires`, and reusable `routes.py`). Not a second `Hinoki` object. |
| app mount | `Mount("/billing", billing_routes)` in the project's `routes.py`, deciding whether and at which prefix an app is exposed over HTTP. |
| target | `module:attribute` naming an app, such as `notes.main:app`. Commands use `project.application` from `hinoki.toml` when it is omitted. |

## Code

| Term | Meaning |
| --- | --- |
| `Model` | A class mapped to a table. Without `@db.table`, the name is `<app>_<plural>`: `notes.apps.billing.models.Invoice` → `billing_invoices`. A column reaches a client only through a `Schema`. |
| action | One declared endpoint (`@http.get`, `@invoices.post`, ...): a function or an `API` class method. |
| route group | A `RouteGroup`: functions under one path prefix with shared `access=`, `atomic=` and `errors=`. |
| `Access` | The access policy of an action: an authentication provider and required permissions. |
| `Caller` | Who is making the request, as resolved by the provider. |
