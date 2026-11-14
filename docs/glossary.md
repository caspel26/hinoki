# Glossary: one word, one meaning

"Schema" means four different things in this field, so the docs use it for one of them only.

| Write | Means |
| --- | --- |
| `Schema` (code font) | The Hinoki class for typed, validated request and response data: `class NoteIn(Schema)`. Always capitalised and in code font. |
| table state, migration state | What the migration planner records about the database tables (`migrations/NNNN-name.state.json`). Never "the database schema". |
| OpenAPI document | The generated `/openapi.json` description of the API. Its `components.schemas` entries are "OpenAPI schema objects". |
| JSON Schema | The standard of that name, which the OpenAPI document uses to describe bodies. Always with its proper name. |
| `Model` | A class that maps to a table (`class Note(Model, table="notes")`). Not a `Schema`: a column reaches a client only when a `Schema` names it. |
| `Access`, `Caller` | The access policy of an action, and who is making the request. |
| action | One declared endpoint (`@http.get`, `@http.post`, ...), plain function or API-class method. |

When you write about the rule `HNK-SCH-001`, say "a response `Schema`", not "a response schema".
