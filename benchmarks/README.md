# Benchmarks

Measured results only; nothing here is a performance claim about Hinoki as a whole.
The raw result files of the micro-benchmarks below were removed from the repository; only the data rendered on the documentation site is retained (see git history for the rest).

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

## `jsondecodebench.py` and `jsonencodebench.py` — native JSON codec against the standard library

Question: how long does one `hinoki.core.decode_json` / `encode_json` call take compared with `json.loads` / `json.dumps` (compact separators, `ensure_ascii=False`, same bytes) across payload sizes? Decoding: seven cases (lists of objects at about 60 B, 145 B, 740 B, 20 KB and 1 MB; lists of numbers at about 20 KB and 1 MB). Encoding: object-, number-, string- and nesting-heavy payloads at about 740 B, 20 KB and 1 MB, a UUID/datetime payload and two small objects. Seven samples per case; the report gives the median, the coefficient of variation, the min/max and the ratio to the standard library. Build the extension in release mode first; a debug build is meaningless.

```sh
uv run --locked --with maturin maturin develop --release
uv run --no-sync python benchmarks/jsondecodebench.py
uv run --no-sync python benchmarks/jsonencodebench.py --output result.json
```

Numbers depend on the machine and are not retained here. Results are isolated function calls, not request-path results.

## `websocketbench.py` — WebSocket echo and plain GET through Kiso

Question: what does an echo round trip of a 64-byte text message over a WebSocket cost, and does a build that has WebSocket support still serve plain HTTP GETs as fast as one without? Server and client share one process and event loop; 16 clients, 500 rounds each, 7 samples after a warm-up.

```sh
uv run --locked python benchmarks/websocketbench.py --mode ws
uv run --locked python benchmarks/websocketbench.py --mode http
```

Run `--mode http` on both builds to compare them; numbers depend on the machine and the build profile and are not retained here.

## `requestbench.py` — JSON and query parsing, Python vs Rust


Question: what does decoding a JSON body, encoding a response and parsing a query string cost through `hinoki.core` compared with `json.loads` / `json.dumps` / `urllib.parse.parse_qs`? One payload (about 1.2 KB: nested object, 20-item list, strings, numbers, booleans, null), 7 samples × 20 000 calls.

```sh
uv run --locked python benchmarks/requestbench.py
uv run --locked python benchmarks/requestbench.py --output /tmp/request-path-micro.json
```

The optional JSON output retains all seven per-operation samples and the
interpreter, platform and payload metadata. Query parsing supplies the same
1000-pair bound as the runtime.

Release-profile baseline captured on 2026-11-15 after the comparative Q063
merge: across seven samples, medians were 8.59 µs Python / 7.26 µs Rust for
JSON decode (1.18x), 10.26 / 10.06 µs for JSON encode (1.02x), and 6.56 / 1.23
µs for query parsing (5.35x). These are isolated function calls, not complete
request-path results. The machine-readable samples are in
`request-codecs-before-rust.json`.

### Rate limiter

`ratelimitbench.py` sends a GET through the in-process ASGI adapter to three
routes with the same trivial handler: no limit, `RateLimit` on `MemoryLimits`
and `RateLimit` on `DatabaseLimits` over a SQLite file (7 samples × 5,000
requests, the limit far above the request count). It excludes a server, a
network and a deployment database. The result of five runs, with the
before/after runs of `protectedgetbench.py` and `nativelanebench.py` on the same
release-profile build (no change within run-to-run noise), is in
`ratelimit-path.json`. The limiter is new, so there is no earlier baseline for
its own cost; the SQLite file runs showed a CV of up to 17% within a run on a
machine that was not idle.

### ASGI request-path baseline

`protectedgetbench.py` sends an async, bearer-protected GET through the full
in-process ASGI adapter. Its ASGI scope includes `raw_path`, as a real server
provides. It excludes a listening server, network, database, and application
work beyond the auth callback and trivial handler. Five blocks of seven
samples × 20,000 requests on a release-profile extension produced a median of
block medians of 57.125 µs/request (CV across the five blocks: 0.8%). This is a
local regression baseline, not a public serving-throughput claim. See
`request-path-before-rust.json`.

A cProfile map of the same scenario attributed much of its Python time to
authentication hook calls and `inspect.iscoroutinefunction`, followed by
request construction, timeout scheduling and response handling. Profiling
inflates Python call costs and is used only to locate hotspots. Rust currently
owns route matching, query/header/cookie and body parsing, JSON conversion,
schema work, crypto and ORM execution; the ASGI lifecycle and application
callbacks stay Python. A Rust adapter port needs a measured net benefit after
PyO3 crossings and must preserve asyncio cancellation and ASGI behavior.

An experiment fused percent-decoding of route parameters into Rust route
lookup. Existing ASGI tests for encoded slashes, UTF-8 and invalid UTF-8 passed.
On the protected-GET microbenchmark, the prototype's five-block median was
56.402 µs versus 57.125 µs for the baseline (−1.27%); between-block CV was
2.2% versus 0.8%, and within-block CV was 2.6–3.7%. This is below the 15%
regression detector and inside observed variation, so the prototype was
discarded and no extra Rust routing work was merged. The experiment's samples
are retained in
`request-path-rust-decode-prototype.json`.

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

## `compressionbench.py` — Kiso response compression over loopback

Question: what do the compressed bodies weigh, and what does one request cost with compression off, with gzip and with brotli? The Rust server runs in the process; a blocking client in a thread sends sequential requests on one keep-alive connection; modes alternate every round. Two scenarios: a JSON body of 12,980 bytes (compressed eagerly) and a stream of sixteen distinct pieces of about 70 KB (compressed as it is read).

```sh
uv run --locked python benchmarks/compressionbench.py --rounds 5 --requests 300
```

Recorded 2026-11-21, Apple M4 Pro, Python 3.13, debug-profile extension, five rounds of 300 requests (CV across rounds 0.3–3.2 %). Body bytes identity / gzip / brotli: 12,980 / 1,911 / 733 and, with chunk framing, 1,161,929 / 150,933 / 40,374. Median per request, off / gzip / brotli: 0.145 / 0.175 / 0.194 ms (small) and 0.426 / 4.36 / 3.33 ms (stream). The client shares the host and the body is synthetic: this is a cost for that body here, not a rate and not a comparison with another server. The whole-response path with compression off is checked with `protectedgetbench.py` below; compression is not on that path.

## `schemabench.py` — schema validation, Rust vs the pure-Python oracle

Question: how much does validating a typical nested body cost with the Rust `CompiledSchema` compared with the previous pure-Python engine (kept in `tests/oracle/validation.py`)? One post with a UUID, a datetime, a pattern-constrained slug, 800 characters of body and ten nested tags (each with length and range constraints); 7 samples × 5 000 calls, schema compiled once outside the timing.

```sh
PYTHONPATH=. uv run --locked python benchmarks/schemabench.py
```

Observation (2026-11-13, one 8-core Linux laptop, Python 3.14, debug-profile extension): Python ≈ 34.8 µs, Rust ≈ 17.1 µs per call, **2.0× faster** (CV 3–4 %). Instance construction (calling the dataclass constructors) and datetime/UUID object creation still happen through Python, which bounds the gain. Serialising the same post to JSON bytes (`encode_json` on the instance against `json.dumps(encode(instance))`): ≈ 31.6 µs against ≈ 14.8 µs, **2.1× faster**. Not end to end: no HTTP, binding or I/O.

Control run for the Schema alias change (2026-11-16, Linux, Python 3.13.18): five invocations of the existing non-aliased workload gave Rust validation medians of 19.16 µs before and 19.58 µs after; Rust encoding was 14.46 µs before and 14.62 µs after. These changes (about 2.2% and 1.1%) are below the 15% investigation threshold. This did not measure aliased fields and makes no performance claim about them. Per-invocation results and variation are in `schema-aliases-control-2026-11-16.json`.

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

Raw samples and environment metadata are retained in `results/orm-compile-postgres-before-stage0.json`. These are compiler-only values; they do not predict database latency or SQLite performance.

## `comparative/` — Django Bolt's seven routes on every stack

Hinoki (on Granian and on Kiso), Django Bolt, Litestar, FastAPI, Django Ninja, and the JavaScript servers of Bolt's own comparison (Elysia on Bun, Hono on Bun, Hono on Node.js) serving the seven routes of Django Bolt's published comparison against the same PostgreSQL data, with Bolt's method (one process, a pool of 8, `bombardier -c 100 -d 10s`, two starts) and a verifier for behaviour and statement counts. See `comparative/README.md` for the method and limits and the [benchmarks page](../docs/benchmarks.md) for the published run. `schema.sql`, `seed.py`, `hinoki_app.py`, `ninja_app.py` and `benchapp/` are the earlier posts service, kept for the experiments below.

## `comparative/phase1_orientation.py` — generated CRUD, end to end

This dedicated run measures the generated CRUD routes (declared with the
since-removed `CrudAPI` class when measured; `crud(...)` generates the same
routes) on Hinoki's Rust
server against equivalent Django Bolt endpoints. It covers list pages of 20 and
100 rows, detail, create and PATCH update on the same 10,000-row PostgreSQL
table. The script checks that all native plans arm, verifies both apps against
the seeded rows, alternates five paired samples per scenario, and retains each
raw Oha result, response verification, per-sample host load and SQL call counts.
It records no result when the one-minute host load at sample start is above
1.0. Treat the result as orientation data for this host and workload only.

For reproduction and the retained data, see [Native CRUD orientation](../docs/rust-crud-orientation.md)
and `comparative/results/2026-11-18/crud-phase1/`.

Run on Linux with `taskset`, Oha and Docker available. Start a fresh PostgreSQL
17 container with a dynamically assigned loopback port, then set
`PHASE1_DATABASE_URL` to the published port and `PHASE1_POSTGRES_CONTAINER` to
the task's container name. Put the virtual environment, `TMPDIR`, uv cache and
Cargo target under `/home/caspel/.cache` by setting `UV_PROJECT_ENVIRONMENT`,
`TMPDIR`, `UV_CACHE_DIR` and `CARGO_TARGET_DIR`. From this directory:

```sh
uv sync --locked
uv run --locked --with maturin maturin develop --release \
  --manifest-path ../../crates/bindings/Cargo.toml
uv run --locked --no-sync python phase1_orientation.py \
  --out results/2026-11-18/crud-phase1
```

The script verifies that the Rust server arms list/retrieve/create/update
plans, compares both apps with the seeded database, and waits for one-minute
host load at or below 1.0 before each timed sample. If the host does not become
quiet within 20 minutes, the run stops without timing that sample. Remove only
the disposable container created for the run.

Run on Linux with `taskset`, Oha and Docker available. Create a fresh PostgreSQL
17 container using an automatically assigned loopback port, then set
`PHASE1_DATABASE_URL` to that port and `PHASE1_POSTGRES_CONTAINER` to the
container name. Keep the build target, virtual environment, `TMPDIR` and uv
cache under `/home/caspel/.cache`; set `CARGO_TARGET_DIR`,
`UV_PROJECT_ENVIRONMENT`, `TMPDIR`, `UV_CACHE_DIR` and `PHASE1_DATABASE_URL`
before running:

```sh
cd benchmarks/comparative
uv sync --locked
uv run --locked --with maturin maturin develop --release \
  --manifest-path ../../crates/bindings/Cargo.toml
uv run --locked --no-sync python phase1_orientation.py \
  --out results/2026-11-18/crud-phase1
```

The script verifies that the Rust server arms list/retrieve/create/update
plans, compares both apps with the seeded database, and waits for one-minute
host load at or below 1.0 before each timed sample. If the host does not become
quiet within 20 minutes, the run stops without timing that sample. Keep the
container for the run and remove only that task's container afterward.

### Request-size limit check

For the query/header byte limits, five alternating baseline/candidate runs of the existing protected-GET microbenchmark were collected on 2026-11-15 (Python 3.13.18, debug-profile extension). The baseline is `f334e0c`; each run executes 7 samples × 20,000 in-process requests after warm-up. Baseline medians were 48.386, 47.976, 47.945, 47.629 and 46.950 µs; candidate medians were 48.990, 51.299, 49.774, 50.148 and 49.264 µs. The group medians are 47.945 µs and 49.774 µs (+3.8%). Within-run CV was 2.6–2.9% for baseline and 2.7–4.3% for the candidate. This is below the 15% regression detector threshold and is the measured cost of checking the default bounds. This in-process result has no server, network or database and does not predict end-to-end throughput. Raw data: `protected-get-request-limits.json`.

### 2026-11-15 — application-wide response security headers

Five alternating runs of `protectedgetbench.py` compared the main baseline (`a79cbad`) with the response security policy implementation (`3d1eb7a`). Each run reports the median of seven samples × 20,000 requests after one warm-up. Median of run medians: 51.408 µs baseline and 53.804 µs candidate (+4.66%); paired changes ranged from +2.08% to +7.27%. The within-group coefficients of variation were 2.29–4.00% baseline and 2.64–3.37% candidate. This is below the 15% detector threshold.

The added work builds and appends three application-wide security headers to each response. This measures only the in-process ASGI path; no server, network or database is involved, and host load was not isolated. It is a regression check, not an end-to-end or throughput claim. Raw samples and environment metadata are in `security-response-policy.json`.

## `comparative/profile_request.py` — where one request's time goes

Question: for a request that reaches PostgreSQL, how much CPU does Hinoki spend in Python, how much in the native extension's own threads, and how much is waiting for the database? One process, no HTTP server and no network between client and application: the ASGI adapter is called directly, as a server would, against `hinoki_app.py` plus one bearer-protected route and a 100-row page. A pass through `psycopg` of the same SQL gives the driver-level floor. Each figure is the median of five blocks; `--profile` adds a cProfile split of the event-loop thread (cProfile inflates Python call costs, so the shares locate hot spots and are not microseconds).

```sh
# PostgreSQL as in comparative/README.md, seeded with seed.py
cd benchmarks/comparative
PROFILE_DATABASE_URL=postgres://postgres:bench-only@127.0.0.1:54402/bench \
  uv run python profile_request.py --profile --output ../results/request-profile.json
```

Measured 2026-11-15 on one Apple M4 Pro (macOS 26.2, Python 3.14.0, release-profile extension), PostgreSQL 17 inside the Docker Desktop virtual machine, host not idle (one-minute load average 2 to 11, including unrelated system and container activity). Samples and metadata are in `results/request-profile-*-macos-m4pro.json`.

CPU per request with 64 requests in flight (µs; the event-loop thread, then all other threads), and the requests per second one process then serves. Each cell reads *before → after the row-snapshot change → after the native row changes* (the other-thread column: before → after the native changes):

| Scenario | Event-loop CPU | Other-thread CPU | Requests/s (one process) |
| --- | ---: | ---: | ---: |
| `health` (no database) | 13 → 13 → 13 | 0 → 0 | 78 664 → 78 524 → 77 902 |
| `detail` | 106 → 92 → 88 | 67 → 62 | 7 345 → 8 167 → 8 781 |
| `detail`, bearer-protected | 110 → 97 → 92 | 66 → 61 | 7 211 → 7 908 → 8 366 |
| `list`, 20 rows | 517 → 357 → 285 | 178 → 154 | 1 572 → 2 083 → 2 632 |
| `list`, 100 rows | 1 938 → 1 203 → 866 | 416 → 290 | 433 → 631 → 915 |
| `create` | 159 → 134 → 130 | 135 → 127 | 4 620 → 5 008 → 5 193 |

Two changes produced the arrows:

* **Row snapshots.** `Model.remember` deep-copied every column of every loaded row (the snapshot `asave` uses for dirty tracking), also in read-only requests; UUIDs and datetimes went through `copy`'s generic reconstruction. Values of immutable types are now kept as they are and mutable ones are still copied. Three alternating baseline/candidate pairs gave event-loop CPU of 105–106 / 92–94 µs (`detail`), 511–520 / 357–364 µs (`list`, 20 rows) and 1 923–1 956 / 1 187–1 226 µs (`list`, 100 rows); the ranges do not overlap. This is a Python change.
* **Native row values.** `Schema.dump()` ran `import uuid`, `import datetime` and a failing `hasattr("dump")` for every scalar of every row; scalars of exact built-in types now return straight away, and the exact `UUID` and `datetime` classes are looked up once. A UUID read from the database was built by calling `uuid.UUID(text)` and printed through `str(UUID)`; it is now built without running `UUID.__init__` (after checking once at first use that the result equals `UUID(text)`, otherwise the old call is used) and printed from its integer value. A change that skipped the dict copy in the queryset row builder gave nothing measurable (849–855 µs against 844–849 µs, `list`, 100 rows) and was not kept.

Isolated per-row costs (`rowcostbench.py`, a nested `PostOut` with two UUIDs and a datetime; two alternating before/after pairs): `Schema.dump()` 7.07 and 7.08 µs → 3.20 and 3.05 µs, `encode_json` 4.01 and 3.94 µs → 2.41 and 2.28 µs; `Post.from_row` (0.55 µs) and Python's own `str(UUID)` (0.25 µs) are unchanged, as expected. Samples: `results/row-cost-before-native-macos-m4pro.json` and `results/row-cost-after-native-macos-m4pro.json`.

Readings, and their limits:

* A request that does no database work costs about 13 µs of event-loop CPU inside Hinoki.
* The database round trip dominates a single request: the wait is about 260 µs for `detail` (a bare `SELECT 1` through the virtual machine's port forward is about 240 µs) and about 1.5 to 1.8 ms for `list` (two statements, one of them a `COUNT`).
* Remaining per-row costs in a list are the native serializer (`dump_instance`, about 17 to 26% of event-loop CPU), `Model.remember`, `from_row` and the queryset row builder; they grow with page size.
* Event-loop CPU is not the only limit of a served worker. Granian's HTTP handling, the load generator, PostgreSQL and the Docker virtual machine share the same cores.
* One machine, one day, in process: not a throughput claim, not a comparison with any other framework, and not a prediction for Linux.

## `comparative/server_floor.py` — server cost next to Hinoki's own

Question: how much of a served request is the server and its protocol, and how much is Hinoki? The same `oha` run (64 connections, three runs of 15 s) hits Granian with the same number of worker processes serving a minimal ASGI application, a minimal RSGI application and `hinoki_app`'s `/health`.

```sh
cd benchmarks/comparative
uv run python server_floor.py --workers 1 --output ../results/server-floor.json
```

Measured 2026-11-15 on the same Apple M4 Pro (load average 6 to 12 at the start of the runs):

| Granian workers | Minimal ASGI app | Minimal RSGI app | Hinoki `/health` (ASGI) |
| ---: | ---: | ---: | ---: |
| 1 | 131 600 req/s | 133 600 req/s | 42 100 req/s |
| 4 | 124 300 req/s (runs 109 000 to 127 200) | 119 800 req/s (runs 105 300 to 122 900) | 91 600 req/s (runs 78 100 to 101 600) |

* With one worker the server is the limit. A request costs about 7.6 µs of worker time for the minimal ASGI application and about 23.7 µs for Hinoki's `/health`, so Hinoki's layer is about 16 µs of it, consistent with the 13 µs measured in process. On a path with no database work that layer is about two thirds of the request.
* ASGI against RSGI differs by about 1.5% with one worker and not measurably with four: the protocol is not where the time goes here.
* With four workers the minimal applications already serve about 120 000 requests per second and their runs vary by more than 10%; the load generator and the other processes share the machine, so these runs do not separate the server from the client. Use the one-worker row for per-request costs.
* For a request that reaches the database, Hinoki's 13 µs is a small part of the roughly 90 µs of event-loop CPU of `detail` and of the 260 µs it waits for PostgreSQL.

## Native-await completion (`nativeawaitbench.py`, `comparative/profile_request.py`)

Question: does resolving database awaitables on the event-loop thread (a per-loop completion queue and one wake fd, no GIL on Tokio threads) beat `pyo3_async_runtimes::future_into_py` plus the conversion on a Tokio worker? Compared against `main` `f672172`, both release builds, five alternating runs each, raw data in `results/native-await.json`. Measured 2026-11-16 on one Apple laptop, load average 6 to 7 for the retained runs (a first set under load up to 18 gave the same picture).

`profile_request.py --prod` (in-process ASGI, PostgreSQL 17 container, 64 requests in flight; median of five runs, range in brackets):

| Scenario | req/s before | req/s after | loop CPU per request before → after |
| --- | ---: | ---: | ---: |
| `health` (no database, control) | 95 627 | 97 001 (+1.4%) | 10.4 → 10.3 µs |
| `detail` | 8 946 | 16 658 (+86%) | 84.9 → 58.1 µs |
| `create` | 5 049 | 8 265 (+64%) | 135.8 → 90.0 µs |
| `list_20` | 2 633 | 3 705 (+41%) | 285.2 → 266.4 µs |
| `list_100` | 918 | 992 (+8%) | 858.4 → 997.3 µs (+16%) |

`nativeawaitbench.py` (SQLite file, 64 concurrent fetches): one row 36 500 → 230 000 calls/s, 100 rows 24 200 → 50 500 calls/s.

* The `list_100` event-loop CPU rises because row conversion now runs on the loop thread instead of a Tokio worker; throughput still improved here, but a loop-bound worker with very large pages could see the conversion cost on its own thread.
Under Granian with real sockets (`comparative/run.py --framework hinoki`, 4 workers, 64 connections, `oha`, PostgreSQL 17, 2 rounds of 3 runs of 10 s per build, alternating; load 2 to 25, so noisy), median req/s before → after, no non-2xx responses:

| Scenario | Before | After |
| --- | ---: | ---: |
| `detail` | 20 921 | 27 424 (+31%) |
| `create` | 11 884 | 14 263 (+20%) |
| `list` (20 rows), alone, 4 workers | 6 267 | 6 904 (+10%) |
| `list` (20 rows), alone, 1 worker | 2 626 | 3 020 (+15%) |

* `list` was first measured right after `create` in the same run, on a table the inserts had grown; the faster build inserted more rows, so it paid for a bigger table and looked 14% slower. That was an ordering artifact. The `list` rows above run it alone on a freshly seeded table, three rounds per build with the build order alternated (load 8 to 18, noisy).
* The in-process gain for `list` is larger than the Granian one; row conversion now runs on the loop thread, which raises event-loop CPU per request on large pages.
* Not measured: PostgreSQL with `runtime_threads` 2, Linux, a quiet host, the Rust server (not built yet).

## Generated CRUD rows-to-JSON gate (`rowwirebench.py`)

This local A/B gate compares a real generated CRUD list route (`CrudAPI.list` when measured; now `crud(...)`) with the same
binding's private Rust RowPlan enabled or disabled. Granian, the model, schema,
seed, database, pool, workers and SQL remain the same. It is a regression check
for this implementation slice; it is not the Rust-server comparison, the
cross-framework benchmark or a public performance claim. Output belongs in the
ignored `.cache/` directory.

Use a disposable PostgreSQL database. The runner verifies GET/HEAD response
parity and the count/page SQL sequence before timing. Its in-process trace also
checks that the Rust path avoids row/model/schema materialization and the
Python path performs it. It pins Granian to CPUs 0–3 and Oha to CPUs 6–7 on
Linux when those CPUs are available. For every sample it seeds the database,
starts and warms the server, then waits with the idle server and database in
place for a stable 10-second window with one-minute load at or below 1.0, up to
five minutes. Oha remains stopped during this gate. It records the wait, load
values and active processes, reads load once more immediately before timing, and
refuses to start Oha if that reading is missing or above 1.0. It records load
again after the sample. If the host does not become quiet, the campaign stops
with a partial report and keeps that attempt's raw files isolated. Use a fresh
empty output directory for each attempt and keep the machine otherwise idle.
Timed runs also require a clean committed worktree; `--verify-only` can run
against local edits.

```sh
docker run -d --name hinoki-rowwire-ab \
  -e POSTGRES_PASSWORD=rowwire-only -e POSTGRES_DB=rowwire \
  -p 127.0.0.1:55517:5432 postgres:17-alpine
export ROWWIRE_DATABASE_URL=postgres://postgres:rowwire-only@127.0.0.1:55517/rowwire
uv sync --locked
uvx maturin develop --release --uv
uv run --locked --no-sync python benchmarks/rowwirebench.py \
  --verify-only --seed-rows 10000 --out .cache/rowwire-ab-verify
uv run --locked --no-sync python benchmarks/rowwirebench.py \
  --runs 5 --duration 20 --warmup 10 --workers 1 --pool-size 10 \
  --connections 64 --page-sizes 20 100 --seed-rows 10000 --port 8891 \
  --server-cpus 0-3 --load-cpus 6-7 --out .cache/rowwire-ab
docker rm -f hinoki-rowwire-ab
```

Raw Oha JSON, load averages, server logs, environment metadata and paired
summaries are retained in the selected output directory. Interpret only the
measured local run; do not copy these results into the comparative report.

## Offset against cursor pages (`cursorbench.py`)

Question: what does one list page cost with `apaginate` (offset plus COUNT) against `apaginate_cursor` (keyset, no COUNT) as the page gets deeper? In process, through the ORM, on a disposable SQLite file of 1 000 000 rows ordered `rank DESC, id`, page size 20, one warm-up call and 7 timed samples per cell (median and range). The script checks that both strategies return the same rows before timing.

```sh
uv run python benchmarks/cursorbench.py            # sort order indexed
uv run python benchmarks/cursorbench.py --no-index # sort order not indexed
```

Observed (2026-11-22, one macOS laptop under a load average of 6 to 13, **debug-profile** extension as built by `uv sync`; medians in ms):

| Depth (offset) | Offset, indexed | Cursor, indexed | Offset, no index | Cursor, no index |
| --- | --- | --- | --- | --- |
| 0 % (0) | 1.73 | 0.066 | 24.4 | 21.7 |
| 1 % (10 000) | 1.82 | 0.107 | 41.8 | 31.7 |
| 10 % (100 000) | 2.68 | 0.114 | 250.4 | 30.2 |
| 50 % (500 000) | 7.00 | 0.099 | 1394.4 | 24.2 |
| 90 % (900 000) | 11.30 | 0.108 | 1842.9 | 16.8 |

Consequences and limits:

* With an index on the sort order the cursor page stays at about 0.1 ms at every depth while the offset page grows with it; without an index both sort the table, and the cursor still avoids the growing offset scan.
* Each run's own range is in the script output; the unindexed 50 % and 90 % offset maxima were above 2 s, so those cells are noisy.
* SQLite only, one connection, no HTTP; numbers on PostgreSQL or MySQL, a release build and a quiet host were not measured. They are not a statement about any other framework.

## Native handler lane (`nativelanebench.py`)

This script runs the Rust server in process with `native_dispatch` on or off
and drives it from a separate process with 32 keep-alive connections on
loopback. Modes alternate every round. It reports, per scenario and mode, the
median requests per second, the coefficient of variation, and the median p50
and p99 latency.

```bash
uv run --locked python benchmarks/nativelanebench.py --rounds 5 --seconds 3
```

Scenarios: `one`, `list_20` and `list_100` are served by the lane; `mixed`
(a read plus `asyncio.sleep`) and `write` (an `atomic` POST) are refused by the
proof and stay on asyncio in both modes, so they are controls. A difference
between modes on a control is host noise.

To isolate the per-call cost of `driving()` and `current_owner()` added with the
Lane B driver, run the same script on two builds, before and after that change,
and compare `write`, `mixed` and `list_100`. Use an idle, dedicated machine and
investigate a coefficient of variation above 15% before reading the medians.
No results are recorded here.

## `comparative/phase2_baseline.py` — the request path around handwritten handlers

Measures a fixture app whose handlers do no work, on Hinoki's Rust server and on
Granian, so each number is the request path around the handler. Three suites:

* `handlers` (default, `phase2_hinoki.py`): GET with path and typed query
  parameters, JSON POST at 64 B, 10 kB and 100 kB, PATCH, DELETE and a POST with
  `transaction=True`. Needs a disposable PostgreSQL URL in `P2BENCH_DATABASE_URL`.
* `sources` (`PHASE2_SUITE=sources`, `phase2_sources_hinoki.py`): `Form[...]`,
  `XML[...]` and `Query[...]` inputs, the built-in HTML and XML renderers and a
  `Request` parameter. No database.
* `sync` (`PHASE2_SUITE=sync`, `phase2_sync_hinoki.py`): the `handlers` scenarios
  (without the transaction one) served by `def` handlers, so the difference to
  `handlers` is the cost of the bounded sync executor. No database.

Each run first checks that both stacks return the same status and body for every
scenario, records which lane the Rust server uses per route (`lanes.json`), then
alternates five pairs per scenario and waits for the one-minute load to be at most
1.0 before each sample. The first baseline was recorded on 2026-11-18; its raw data is no longer
retained in the repository (see git history).

```sh
cd benchmarks/comparative
uv sync --locked
uv run --locked --with maturin maturin develop --release \
  --manifest-path ../../crates/bindings/Cargo.toml
P2BENCH_DATABASE_URL=postgresql://... \
  uv run --locked --no-sync python phase2_baseline.py --out results/<date>/request-path
PHASE2_SUITE=sources \
  uv run --locked --no-sync python phase2_baseline.py --out results/<date>/text-sources
PHASE2_SUITE=sync \
  uv run --locked --no-sync python phase2_baseline.py --out results/<date>/sync-handlers
```

## `ci_bench.py`, `ci_compare.py` and `.github/workflows/benchmark.yml` — CI regression check

Automated paired regression check run on every `pull_request` (including forks,
without `pull_request_target`) and `push` to `main` via
`.github/workflows/benchmark.yml`.

Five representative workloads run on a disposable SQLite file over `KisoWorker`
loopback keep-alive connections (load generated by a separate subprocess):

* `health`: `GET /health` baseline route.
* `crud_scoped_list`: `GET /tasks?page=1&page_size=20` with `ClaimsJWTAuth` and `RowScope.by_claim`.
* `projected_list`: `GET /projected/tasks?page=1&page_size=20` with `select_related("owner").apaginate_as(...)`.
* `atomic_write`: `POST /tasks/atomic` (`atomic=True`) creating one row.
* `handwritten_get`: `GET /manual/tasks/{task_id}` with `aget()` and `Schema` serialization.

`ci_compare.py` runs a warm-up followed by 5 alternating paired rounds
(`AB, BA, AB, BA, AB`) in separate base and candidate Python environments,
marks HTTP errors or empty runs `invalid`, high-variance runs `noisy`, and
sustained throughput drops (`> 10%`) or p95 rises (`> 15%`) as `regression`.
While calibrating on GitHub-hosted runners, the workflow runs with
`--informational`, publishes the comparison table to `$GITHUB_STEP_SUMMARY`, and
retains the raw JSON and Markdown reports as workflow artifacts.

```sh
uv run --locked --no-sync python benchmarks/ci_bench.py --mode profile
uv run --locked --no-sync python benchmarks/ci_compare.py \
  --base-python .venv/bin/python \
  --cand-python .venv/bin/python \
  --rounds 5 --seconds 2.0 --informational
```
