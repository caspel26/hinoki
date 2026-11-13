# Schema design (Q004 step S1)

Status: **design** · 2026-11-13 · Public syntax in `framework-handoff/FRAMEWORK_SPEC.md` §5 is confirmed in spirit; the engine below is a proposal.

## Observed

The runtime (R1–R5) binds path parameters only. The blog example needs JSON request bodies (`PostCreate`), response models (`PostOut`), query parameters and field-level errors. There is no schema code yet. Pydantic is excluded by owner decision; msgspec is a *candidate engine*, not a decision.

## Recommendation

Implement `Schema` in **pure Python first**, behind a narrow interface, so the engine (msgspec, or a Rust decoder) can be swapped after measurement without changing the public syntax.

```python
class PostCreate(Schema):
    title: Annotated[str, Field(min_length=1, max_length=160)]
    body: str
    tags: list[str] = Field(default_factory=list)
    subtitle: str | None = None          # nullable, defaults to None
```

* `Schema` subclasses are frozen, slotted dataclasses created in `__init_subclass__` and annotated with `@dataclass_transform`, so constructors are checked statically (proven in `docs/TYPING.md`).
* Supported types in S1: `str`, `int`, `float`, `bool`, `UUID`, `datetime`, `list[T]`, `T | None`, nested `Schema`, `Annotated[T, Field(...)]`. Anything else fails at class creation, not at request time.
* Constraints in `Field`: `min_length`, `max_length`, `pattern`, `ge`, `le`, `gt`, `lt`, `default`, `default_factory`.
* **Strict by default:** no implicit coercion from JSON (`"1"` is not an `int`; `1` is not a `bool`; unknown keys are errors). Path/query strings are converted by the binder, which is the only coercion point.
* **PATCH:** `UNSET` sentinel (`str | None | Unset = UNSET`): absent ≠ `null`; a non-nullable field rejects `null`.
* **Errors:** `ValidationError` carries a list of `{path, code, message}`; codes are stable (`required`, `type`, `min_length`, `max_length`, `pattern`, `ge`, `le`, `extra_forbidden`, `invalid_json`). All errors are collected, not only the first. HTTP mapping: 422 with `{"error": "validation_error", "details": [...]}`; malformed JSON: 400 `invalid_json`.
* **Output:** `PostOut.from_model` converts only already-loaded data (no hidden queries); the adapter serialises `Schema` instances itself (no `default=str` fallback for unknown types once schemas exist).
* Introspection (`Schema.fields()`) returns an immutable description used by binding, OpenAPI and the future rules engine: one source of truth.

## Binding extension (same step)

Parameter in path → path; `Schema` subclass → JSON body (exactly one allowed per action, only for POST/PUT/PATCH); other primitives → query string; `Annotated[..., Query(...)]`/`Header(...)` for overrides. Ambiguity fails at startup, as today.

## Alternatives

* **msgspec now:** fast and mature but adds a C dependency and shapes error formats; deferred until a benchmark compares it with the pure-Python engine on equivalent payloads.
* **Rust decoder now:** best end state, but premature without a stable field model; the introspection interface is the seam for it.
* **Coercing mode:** rejected as default (hidden behaviour); may be an explicit `Field(coerce=True)` later.

## Tests

Valid/invalid payloads per type and constraint; nested path in errors (`tags[2]`, `author.name`); `UNSET` vs `None`; bool-vs-int strictness; unknown key; type-check fixtures for constructors and `Field`; request-level tests (400/422 bodies, 413 limit); startup rejection of unsupported annotations.

## Steps and ownership

| Step | Deliverable | Files |
| --- | --- | --- |
| S1a | `Field`, `Schema`, field model + validator, `ValidationError` | `python/hinoki/schema.py`, `python/hinoki/validation.py` |
| S1b | Body/query binding and 400/422 mapping | `python/hinoki/runtime/binding.py`, `asgi.py` |
| S1c | Typing fixtures + docs | `tests/typing/`, `docs/TYPING.md` |

Depends on the runtime review findings being resolved (binding/asgi are shared files: serial edits).

## Open

Datetime formats accepted (RFC 3339 only proposed); decimal support; whether `Schema` allows non-frozen opt-in; engine choice after measurement.
