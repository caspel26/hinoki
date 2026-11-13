# Schema validation and serialisation in Rust — design (Q014)

Status: **N1 design, N2 implemented** (Rust validator + oracle + differential tests + benchmark, 2.0×); N3 implemented (`encode_json` takes `Schema` instances, `UUID`, `datetime`, `dump()` objects; `Schema.dump()` is native; JSONRenderer skips the Python walk; 2.1×); N4–N6 open (2026-11-13). Owner request: all schema and serialisation work happens in the Rust core; use `Schema` everywhere and dataclasses only where genuinely needed; renaming `Schema` is allowed (other frameworks use the word).

## Observed

* Class declaration, annotation parsing and the closed type model are Python (`schema.py`, `fieldtypes.py`, `fields.py`): kinds `str`, `int`, `float`, `bool`, `uuid`, `datetime`, `list`, `schema`; constraints `min_length`, `max_length`, `pattern`, `ge`, `le`, `gt`, `lt`; `Unset`, nullability, recursive schemas via forward references.
* Validation is pure Python (`validation.py`, ~180 lines): strict, no coercion, path-aware errors capped at 50 (`too_many_errors`), unknown keys rejected (`extra_forbidden`, key echo truncated to 64), canonical UUIDs only, RFC 3339 datetimes with an offset, pattern checked with `re.fullmatch` after the length check, depth failures reported as `depth`. It builds the instances itself (`ftype.schema(**values)`).
* Serialisation: `Schema.dump()`, `to_dict()`, and `encode()` in `schema.py` produce JSON-compatible Python data; the bytes come from `native.encode_json` (Rust, already). JSON decoding is Rust (`native.decode_json`).
* Measured earlier: the Rust JSON decode is only ~1.2× faster than `json.loads` because creating Python objects dominates, so the gain from moving validation must come from **not crossing back into Python per field** and from fusing steps, not from the parsing itself.

## Decisions

### D1 The declaration stays Python; the engine is Rust

Classes are still declared as Python classes (frozen dataclasses; this is what lets mypy, IDEs and `Model`/CRUD/OpenAPI see the fields). At first use, `Schema` compiles its closed type model into a **Rust `CompiledSchema`** (a table of schema definitions with field lists, constraint values and compiled regexes), cached on the class (`__native__`). All of `validate`, instance construction, `dump`, `to_dict` and response encoding run in Rust over that table. Recursive and mutually recursive schemas are table entries that refer to each other by index.

### D2 Validation returns finished objects

`CompiledSchema.validate(data)` walks the decoded JSON (Python dict/list/str/int/float/bool/None) once and either returns the instance (built by calling the class constructor from Rust, nested ones too) or a list of `(path, code, message)` errors; Python wraps them in `ValidationError` exactly as today. Same semantics, same codes, same messages, same caps (50 errors, 64-char key echo, depth 128 → `depth`). Datetimes are still parsed by Python's `datetime.fromisoformat` after Rust has checked the RFC 3339 shape (identical results, no second implementation to drift); UUIDs are checked in Rust (canonical form) and built with `uuid.UUID`.

### D3 Patterns are Rust regexes

`pattern=` is compiled with the Rust `regex` crate and matched against the **whole** string. This is a deliberate, documented change: no look-around or back-references (the crate rejects them at compile time, which becomes a `RegistrationError`), and in exchange matching is linear time, which removes a class of regular-expression denial-of-service. Existing patterns that use only the common subset behave identically; the differential tests cover this.

### D4 Serialisation is native end to end

`native.dump_schema(instance)` (used by `Schema.dump()`) and `to_dict` build their dicts in Rust from the compiled field list; `encode_json` learns `Schema` instances, `UUID` and `datetime` directly so a handler's `PostOut` becomes JSON bytes without a Python walk. `UNSET` fields are omitted as today. Output text is unchanged (compact UTF-8), which the existing tests pin.

### D5 Dataclasses and naming

No new dataclasses are introduced for data that is a schema; `Schema` subclasses are the only data-carrier type. Internal configuration objects (`FieldSpec`, `FieldType`) stay as small frozen dataclasses because the compiler consumes them. **Naming:** the owner permits a rename. Recommendation: keep `Schema` working as an alias and introduce **`Contract`** as the canonical name in a final, mechanical step (docs, README, examples, typing fixtures), once the engine change is verified, so the rename is not mixed with the engine risk.

### D6 The Python engine becomes the test oracle

`validation.py` moves to `tests/oracle/validation.py` and is no longer imported by the package. Differential tests generate many inputs (valid, near-valid, hostile: wrong types, nulls, huge/small numbers, bad UUID/datetime strings, unicode, deep nesting, many errors, extra keys) for a set of schemas (flat, nested, lists of schemas, recursive, constraints, nullable/`Unset`) and require **identical results**: same instance field values or the same ordered error list. Any intended difference (patterns) is listed explicitly.

## Failure, empty and private states

An uncompilable schema (unsupported regex, inconsistent constraints) fails at class creation or first compile with a `RegistrationError` naming the field, never per request. A schema with forward references compiles on first use, as today. Errors never echo more than 64 characters of a key and never the offending value (unchanged). A Rust panic must not cross the boundary: no `unwrap` on input-derived values (reviewed, clippy `unwrap_used` denied in the module).

## Tests

Unit tests in Rust for each kind and constraint; the Python differential suite above; the existing schema, binding, OpenAPI, CRUD and blog suites must pass unchanged; typing fixtures (mypy strict) unchanged; a benchmark (`benchmarks/schemabench.py`) of validate and dump for a typical nested post against the Python engine and against the earlier numbers, 7 samples, recorded in `benchmarks/README.md` whether or not it is faster.

## Steps

| Step | Deliverable | Files |
| --- | --- | --- |
| N1 | This design | `docs/SCHEMA-RUST-DESIGN.md` |
| N2 (done) | Rust `CompiledSchema` (compile table, validate, construct instances) and Python glue; Python engine moved to tests as oracle; differential tests | `crates/bindings/src/schema.rs`, `python/hinoki/schema.py`, `tests/oracle/`, `tests/schemadifftest.py` |
| N3 (done) | Native `dump` / `to_dict` and `encode_json` support for `Schema`, `UUID`, `datetime` | `crates/bindings/src/request.rs`, `schema.rs` |
| N4 | Benchmark, docs, handoff | `benchmarks/schemabench.py` |
| N5 | Independent review (untrusted input), fixes | — |
| N6 | `Contract` as the canonical name, `Schema` alias, docs/examples updated | repo-wide, mechanical |

## Open (owner)

Accepting the pattern-syntax change (D3); confirming `Contract` as the name (D5) or choosing another before N6.
