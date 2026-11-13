# Typing proof (Q002)

Status: **partially proven** · 2026-11-13 · checker: mypy `--strict` 1.x (pyright/IDEs not tested).

How it is verified: `tests/typing/ok_*.py` must produce no errors; `tests/typing/bad_*.py` and the probe mark every line that must be rejected with `# type: ignore[code]`, and `warn_unused_ignores = true` makes the run fail if any such line stops being flagged. Gate: `uv run --locked mypy`.

## Proven (against the real code in `python/hinoki`)

| Contract | Result |
| --- | --- |
| `@get/@post/...` preserve the decorated signature (`ParamSpec`/`TypeVar`) on functions and methods | Calls are checked: wrong argument type, missing argument and wrong return assignment are errors |
| Decorator options are typed | `transaction="yes"`, `access="admin"` are errors; `Access` is a frozen typed object |
| `HinokiAPI.include` / `API` subclasses | Type-check; unknown objects are rejected at runtime with `RegistrationError` |
| Decorators stack with user decorators written with `functools.wraps` + `ParamSpec` | Runtime test + `mypy` pass; untyped user decorators are flagged by `--strict` (`untyped-decorator`), which is the intended pressure |
| `Schema` subclasses | Constructors are checked like dataclasses (`tests/typing/okschema.py`, `badschema.py`): missing/extra/mistyped arguments, immutability and unknown attributes are errors; `Field(default_factory=list)` types as `list[str]`; `Schema.validate` returns the subclass |

## Probed, not implemented (`tests/typing/ormprobe.py`)

The probe uses a local prototype `Model`, not the Hinoki ORM.

| Signature style | Static result |
| --- | --- |
| `Post(title=...)` via `@dataclass_transform` | **Fully checked** (names, types, required fields, unknown kwargs, attributes) |
| `await Post.aget(id=...) -> Post` with `Self` | Checked |
| `Post.acreate(**fields: Any)` | **Not checkable**: any keyword is accepted |

**Consequence (proposal):** the spec's `Post.acreate(title=..., author_id=...)` cannot be type-checked by a plain checker. Options, to be decided with the Q004 slice: (a) generate `.pyi` stubs with per-model `acreate`/`aupdate`/filter signatures; (b) make the checked path `await Post(title=...).asave()` and keep `acreate` as sugar; (c) ship a mypy plugin (heavier, per-checker). Recommendation: (a) generated stubs, with (b) as the always-checked fallback. This is an API-shape decision and is **not frozen**.

**Status (Q005):** the owner accepted the recommendation. Path (b) is implemented and statically proven: `await Post(title=...).asave()` inserts, and an instance loaded from the database updates only the columns that changed (`tests/typing/modelcheck.py`, `tests/syncmodelstest.py`). Path (a) is **not implemented yet**: `mypy.stubgen` on a models module drops field defaults (every defaulted field would look required), so the generator has to be custom and is still open. Until then `acreate`/`aupdate` keep `**values: Any`.

## Not yet covered

`Caller[T]` binding, `Page[T].map`, query-set filters, pyright and IDE behaviour (the `.pyi` approach must be re-checked in pyright).
