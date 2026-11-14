# Benchmarks

Measured results only; nothing here is a performance claim about Hinoki as a whole.

## `routerbench.py` — path lookup, Python → Rust

Question: what does one `Router.find` call cost from Python, compared with a deliberately simple pure-Python regex router (linear scan)? Nine routes, six fixed request lines (static, one/two parameters, miss), 5 samples × 200 000 lookups after warm-up, median and coefficient of variation recorded in `results.json` together with Python, platform and package version.

```sh
uv run python benchmarks/routerbench.py --samples 5 --iterations 200000
```

First observations (2026-11-13, one Linux laptop, Python 3.13, **debug-profile** extension as built by `uv sync`):

| Router | Median per lookup | CV |
| --- | --- | --- |
| `hinoki.core.Router` | ≈ 0.3 µs | ≈ 1–5 % |
| Python regex baseline (9 routes) | ≈ 0.6 µs | ≈ 3 % |

A one-off release-profile build (`uvx maturin develop --release --uv`) gave a statistically indistinguishable result (≈ 0.33 µs vs ≈ 0.6 µs), so the cost is dominated by the Python↔Rust call, argument conversion and the result tuple, not by the matching itself. Consequences:

* Routing alone is **not** where the Rust core pays off at this table size; the advantage must come from work that stays in Rust across many steps (request decoding, validation, ORM), which is exactly why the design keeps crossings few.
* The baseline is a toy. It says nothing about FastAPI, Starlette or any other framework, and no such comparison is made.
* These numbers are not end-to-end: they exclude HTTP, ASGI, parsing, validation and I/O.

Not yet measured (RUNTIME-DESIGN V10): ASGI vs RSGI, extra Tokio threads per worker, full request path under load.

## `requestbench.py` — JSON and query parsing, Python vs Rust

Question: what does decoding a JSON body, encoding a response and parsing a query string cost through `hinoki.core` compared with `json.loads` / `json.dumps` / `urllib.parse.parse_qs`? One payload (about 1.2 KB: nested object, 20-item list, strings, numbers, booleans, null), 7 samples × 20 000 calls.

```sh
uv run --locked python benchmarks/requestbench.py
```

Observations (2026-11-13, one 8-core Linux laptop, Python 3.14.8, debug-profile extension; a release-profile build gave the same numbers within noise):

| Operation | Python (CPython C accelerators for JSON) | Rust | Ratio |
| --- | --- | --- | --- |
| decode JSON | ≈ 8.3 µs | ≈ 7.0 µs | 1.2× faster |
| encode JSON | ≈ 9.8 µs | ≈ 5.7 µs | 1.7× faster |
| parse query | ≈ 6.5 µs | ≈ 1.2 µs | 5.4× faster |

Notes and limits:

* A first decoder built on `serde_json::Value` was **slower** than `json.loads` (0.67×), so it was replaced by a direct parser that builds Python objects without an intermediate tree. The result is only a modest win because creating Python objects, not parsing, dominates. Do not expect more than tens of percent on decode.
* The Rust decoder is stricter: it rejects a BOM, lone surrogates and nesting deeper than 128 levels (the standard library accepts the first two). Integers of any size stay exact.
* Not end to end: no HTTP, ASGI, validation or I/O. Query-string CV was 2–16 % between runs on this machine; the gap is far larger than that. No claim is made about any other framework.

## `requestobjectbench.py` — building the `Request` per call

Question: is a native (`#[pyclass]`) `Request` worth its complexity? Measured: header decoding (Rust), query parsing (Rust), cookie parsing (Rust) and the Python `Request` object for a typical scope with five headers, a cookie and a two-parameter query, 7 samples × 20 000 calls (2026-11-13, same machine as above).

* First version: ≈ 7.8 µs per request. The adapter decoded the headers up to three times (authentication, negotiation, request); decoding them once brought it to ≈ 5.9 µs (CV ≈ 2 %) with no native class.
* Decision (REQUEST-DESIGN S6): **not built.** The remaining cost is a few microseconds of attribute assignment and small Python objects per request; without an end-to-end measurement (V10) there is no evidence it matters, and a native class would add a second place to keep in sync with the documented attributes. Revisit with end-to-end numbers.

* 2026-11-14 follow-up: the adapter's header map already has lower-case names, so `Request` now copies it instead of lower-casing every name again, and a binding records once whether it reads a body. Five alternating runs of this script on the commit before and after, same session, debug-profile extension, laptop not idle (load average 1.3–1.8): 6.15–6.36 µs before, 5.33–5.57 µs after (CV 1.8–3.0 % within a run).

## `protectedgetbench.py` — one protected GET through the ASGI adapter, in process

Question: does a change to the per-request path (routing, bearer authentication, query parsing, request building, binding, a trivial handler) cost or save anything? No server and no network, so it says nothing about throughput. 7 samples × 20 000 requests after a warm-up, one JSON line of output.

```sh
uv run --locked python benchmarks/protectedgetbench.py
```

Recorded 2026-11-14 for the change that parses the query string once per protected request (it was parsed for authentication and again for the request) and the two `Request` items above: five alternating runs before and after, same session, Python 3.13, debug-profile extension, laptop not idle (load average 1.3–1.8).

| Commit | Medians of the five runs (µs per request) | Median | CV across runs |
| --- | --- | --- | --- |
| before (`b4640f9`) | 48.80, 49.30, 49.48, 50.30, 48.55 | 49.30 | 1.4 % |
| after | 46.88, 47.26, 47.18, 48.01, 47.44 | 47.26 | 0.9 % |

The ranges do not overlap, and the difference is about 2 µs (4 %) of an in-process request that does no I/O. It is a regression check, not a throughput claim: the end-to-end effect has not been measured (that needs the quiet-machine comparative run).

## `schemabench.py` — schema validation, Rust vs the pure-Python oracle

Question: how much does validating a typical nested body cost with the Rust `CompiledSchema` compared with the previous pure-Python engine (kept in `tests/oracle/validation.py`)? One post with a UUID, a datetime, a pattern-constrained slug, 800 characters of body and ten nested tags (each with length and range constraints); 7 samples × 5 000 calls, schema compiled once outside the timing.

```sh
PYTHONPATH=. uv run --locked python benchmarks/schemabench.py
```

Observation (2026-11-13, one 8-core Linux laptop, Python 3.14, debug-profile extension): Python ≈ 34.8 µs, Rust ≈ 17.1 µs per call, **2.0× faster** (CV 3–4 %). Instance construction (calling the dataclass constructors) and datetime/UUID object creation still happen through Python, which bounds the gain. Serialising the same post to JSON bytes (`encode_json` on the instance against `json.dumps(encode(instance))`): ≈ 31.6 µs against ≈ 14.8 µs, **2.1× faster**. Not end to end: no HTTP, binding or I/O.

## `frommodelbench.py` — `Schema.from_model`, Rust plan vs the former Python loop

Question: does moving `from_model` (read each field from the row, convert nested models, build the instance) into Rust pay off? A model row with eight columns, 7 samples × 20 000 calls (2026-11-13, same machine, **release-profile extension**: `uvx maturin develop --release --uv`).

* First version: the plan was re-extracted from Python tuples on every call, and the Rust path was **slower** (≈ 4.7 µs against ≈ 2.7 µs for the Python loop; 0.58×). It was not shipped in that form.
* Final version: the plan is prepared once per class (`core.ModelPlan`), and when every field is present the instance is built directly, skipping the dataclass constructor: ≈ 1.8 µs against ≈ 2.65 µs, **1.46× faster** (CV 2–4 %). Not end to end.
* A debug-profile build makes the Rust path look slower than it is; measure releases.

## `ormcompilebench.py` — PostgreSQL ORM SQL compilation

Question: what does the current Python-to-Rust compiler path cost for representative select, insert, update and delete statements, without a database or network? The query shapes are constructed once; each sample repeatedly calls the compiler in a release-profile extension. This isolates compilation overhead from driver, pool and server latency.

```sh
uv run --locked maturin develop --release
uv run --locked python benchmarks/ormcompilebench.py \
  --samples 5 --iterations 50000 \
  --output benchmarks/results/orm-compile-postgres-before-stage0.json
```

Pre-Stage-0 baseline (2026-11-14, Python 3.13.18, Arch Linux x86_64, Rust extension release profile; five samples × 50,000 calls):

| Statement | Median per compilation | CV |
| --- | ---: | ---: |
| Select with filters, ordering and pagination | 5.210 µs | 1.5% |
| Insert with six assignments and `RETURNING` | 2.403 µs | 0.6% |
| Update with a filter and `RETURNING` | 1.972 µs | 1.9% |
| Delete with a filter | 4.784 µs | 2.0% |

Raw samples and environment metadata are retained in [`results/orm-compile-postgres-before-stage0.json`](results/orm-compile-postgres-before-stage0.json). These are compiler-only values; they do not predict database latency or SQLite performance.

## `comparative/` — one service on four frameworks

Hinoki, FastAPI, Django Ninja and Django Bolt implementing the same small API against the same PostgreSQL data, with a verifier for behaviour and statement counts. See `comparative/README.md` for the method and limits and the [benchmarks page](../docs/benchmarks.md) for the first measured run (2026-11-14).
