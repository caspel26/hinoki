# Rows in Rust — design (local-only, draft for owner review)

Status: DRAFT 2026-11-15. Nothing implemented. Written before R5b because the R4 profile puts the largest measured Python cost here and the work does not depend on the server track.

## 1. Observed code and data

Row path today for `await Model.all()` / `.filter()` (`QuerySet.fetch`, `python/hinoki/orm/queryset.py:249`):

1. `Database.fetch` (Rust, `crates/bindings/src/db.rs:220`) → `rows_to_py` builds one `dict` per row, one Python object per cell (`from_value`). JSON cells are made by `json.loads(v.to_string())` (Rust value → text → Python parse); timestamps by `datetime.fromisoformat(t.to_rfc3339())`.
2. `QuerySet.build` (`queryset.py:196`) copies the row into a second dict of the model's own columns, calls `Model.from_row` (`orm/model.py:836`): JSON-column decode again if the value is a `str` (Python `json.loads`), `cls(**values)` (the dataclass constructor, kw-only), then `remember()` (`model.py:858`), which builds a snapshot dict with a `type(value) in _IMMUTABLE` test per column and `copy.deepcopy` for the rest.
3. Joined relations (`select_related`): for each, a third dict from prefixed columns, `from_model`, `store(...)`.
4. Prefetch (`load`) is Python on top of `fetch_in` (which goes through the same path).
5. Response: `Schema.from_model` is already Rust (`ModelPlan`, Q021); `dump_instance` is Rust.

Recorded numbers (handoff, R4, host load 4.5–8.8, indicative): `list_20` py_cpu 679 us (Rust 27 %, `dump_instance` alone 17 %; Hinoki Python 38 %: `remember`, `QuerySet.build`, `from_row`); `list_100` py_cpu 1287 us. `detail_protected` py_cpu 80 us (one row). These are the only numbers; no gain is claimed here.

Other callers of `build`/`from_row`: `admin/views.py:398`, `Model.refresh`/`insert`/`aupdate` (RETURNING row), `fetch_in`.

## 2. Recommendation

One Rust object per model class, prepared once (like `ModelPlan`): **`RowPlan`**, and one new fetch entry point that returns finished instances.

- `RowPlan(cls, columns, json_columns, relations, snapshot_immutable)` built lazily on first query, cached on the class (`__row_plan__`), same idiom as `model_plan`.
- `Database.fetch_models(sql, params, plan)`: the Rust future returns the raw `Vec<Vec<(String, Value)>>`; the conversion step (under the GIL, already how `bridge!` works) builds each instance directly: `object.__new__(cls)`, fill `__dict__` from the planned columns by position (no row dict, no name lookups: the compiled column order is known), decode JSON natively (`serde_json::Value` → Python without text round trip), build the snapshot dict in the same pass (immutable exact types shared, others `copy.deepcopy`, same rule as today), and attach joined instances by the prefixed-column positions.
- Fallback to the constructor (`cls(**values)`) when the class has its own `__init__`/`__post_init__` (same `uses_generated_init` check as `ModelPlan`); behaviour stays identical, only slower.
- `QuerySet.fetch` calls `fetch_models`; `build`/`from_row`/`remember` stay as the Python oracle and for the non-bulk callers (RETURNING row, `refresh`, admin) until measured.
- `from_value`'s remaining slow cells are fixed in the same step because they sit on this path: `Timestamp` via `PyDateTime` constructor from components instead of `fromisoformat(rfc3339)`; `Json` natively.

No public API change. Settings, docs and `QuerySet` signatures are untouched; `docs/models.md` needs no change unless a documented behaviour changes (none intended).

## 3. Alternatives

| Option | For | Against |
| --- | --- | --- |
| A. `RowPlan` + `fetch_models` (recommended) | Removes both dict layers, `from_row`, `remember` and Python JSON decode from the bulk path; same idiom as `ModelPlan`. | A second place that knows how a model instance is built; needs a differential test against the Python path. |
| B. Keep Python, trim (`remember` lazily, fewer dict copies) | Tiny diff. | Lazy snapshot changes `asave` semantics (what counts as changed); leaves the cell conversion and constructor cost. |
| C. Return rows as tuples, build in Python | Smaller Rust. | Still pays the constructor and snapshot in Python; keeps most of the 38 %. |
| D. Skip instances: Rust renders rows straight to JSON for list routes | Largest possible gain. | Bypasses `Schema`, handlers, hooks and `access` on rows; a different feature, not "rows in Rust". Out of scope. |

## 4. Failure, empty and private states

- Zero rows: empty list, no plan work. Unknown column in the result: impossible by construction (positions come from the compiled query); a mismatch between plan and result width is a `DbError` (internal), not a silent partial instance.
- A JSON cell that is not valid JSON: same `FieldError("<Model>.<col>: database value is not valid JSON")` as today (SQLite/MySQL deliver text; PostgreSQL `jsonb` is already structured).
- Snapshot semantics unchanged: `asave` writes only changed columns; mutable values are deep-copied. Test: mutate a list/dict column in place, `asave` writes it.
- Private data: no new exposure; instances are the same objects the handler gets today. Tenant/owner filters are in the SQL, untouched.
- GIL: conversion runs inside the existing `Python::attach` of `bridge!`; no new thread or await point. Large result sets hold the GIL for the conversion (as `rows_to_py` does today): bound by the existing limit/pagination; note in the PR.

## 5. Tests (written first)

1. Differential: for every model in the test fixtures (JSON, UUID, timestamp, nullable, FK, decimal, bytes columns), `fetch_models` result equals the Python path: `__dict__`, snapshot, equality, `type(...)`, on PostgreSQL, SQLite (and MySQL/MariaDB where the live URL is set).
2. `select_related` single and nested, NULL join (no related row), prefetch forward/reverse.
3. Class with its own `__init__`/`__post_init__` goes through the constructor.
4. `asave`: unchanged → no UPDATE; changed scalar; in-place mutated JSON; primary-key change refused.
5. Invalid JSON text error; zero rows; wide row.
6. Existing suite unchanged (full pytest on a disposable PostgreSQL, ruff pinned, mypy, complexipy, `cargo fmt/clippy/test`, `core.pyi` updated).

## 6. Performance gate (AGENTS.md policy)

`benchmarks/comparative/profile_request.py --prod --blocks 5`, 5 alternating pairs vs the current `main` release build, on `list_20`, `list_100`, `detail_protected`, `create`, `health`. Pass: `list_*` py_cpu lower with disjoint ranges; no other route worse than the 15 % threshold. Record load average and CV; a CV above 15 % means rerun alone. If `list_20` does not improve with disjoint ranges, report it and stop; do not move thresholds.

## 7. Build steps and ownership

1. Differential test harness + oracle helper (tests only, fails on current code only where a `fetch_models` import is missing).
2. `crates/bindings/src/rows.rs`: `RowPlan`, native timestamp/JSON conversion; `core.pyi`. Own file; `db.rs` gets only the `fetch_models` method and the shared `from_value` change.
3. `queryset.py` `fetch` switches to it; `model.py` adds `__row_plan__` cache and keeps `from_row`/`remember`.
4. Gate run (section 6), then an independent read-only review (data path: snapshot/JSON/refcount/GIL) because it touches the data layer.
5. Docs: `benchmarks/README.md` note only if the gate passes; no performance claim otherwise.

Dependencies: none on R5b/R5c/R6. Commit scopes: test; perf(orm) Rust + Python; docs.

## 8. Open owner decisions

None blocking. Recommended defaults applied: option A; admin and RETURNING callers stay on the Python path in this item (follow-up if the gate shows they matter).
