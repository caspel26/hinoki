# Comparative benchmark: one service, several frameworks

A bounded benchmark of the same small API implemented on Hinoki, FastAPI, Django Ninja and Django Bolt, with an optional separate run of the same API on JavaScript runtimes (Fastify and Hono on Node.js, `Bun.serve` on Bun). It measures what these implementations did on one machine on one day, nothing more. Hinoki is pre-alpha; this is not evidence of production readiness for any of them.

## The service

PostgreSQL, two tables created once by `schema.sql` (no framework creates or migrates them): `authors(id, name)` and `posts(id, author_id, title, body, published, created_at)` with an index on `(published, created_at DESC, id DESC)`. `seed.py` loads the same deterministic rows every time: 100 authors, 10 000 posts, 80 % published. Primary keys are UUIDs everywhere.

| Scenario | Request | What the service does |
| --- | --- | --- |
| `health` | `GET /health` | static JSON, no database (framework overhead) |
| `list` | `GET /posts?page=1&page_size=20` | published posts, newest first, each with its author; page envelope with total and page count (one `COUNT`, one joined `SELECT`) |
| `detail` | `GET /posts/{id}` | one published post with its author (one joined `SELECT`) |
| `create` | `POST /posts` | validate the body, insert, return the post with its author (one `INSERT`, one author `SELECT`) |
| `json-1k` | `GET /json-1k` | fixed JSON body of 939 bytes, no database (serialization and response size) |
| `json-10k` | `GET /json-10k` | fixed JSON body of 10 238 bytes, no database |

The two `json-*` bodies are generated deterministically by `payloads.py` and returned byte for byte by every application; `verify.py` compares the exact bytes, the JSON content type and that no SQL statement ran. The JavaScript applications build the same bodies with a port of the same generator in `js/common.js` (no fixture file to regenerate or keep in sync); `verify.py` rejects any byte difference before a run, and `test_run.py` compares the Node.js output with `payloads.BODIES` when Node.js is installed.

Validation: `title` 1–120 characters, `body` non-empty, `page >= 1`, `1 <= page_size <= 100`; invalid input is `422`, an unpublished or missing post is `404`.

## Implementations

Each uses its own idiomatic stack, at its own recommended server:

| | Code | ORM / driver | Server |
| --- | --- | --- | --- |
| Hinoki | `hinoki_app.py` | Hinoki ORM (Rust, tokio-postgres) | Granian, ASGI (Kiso, Hinoki's Rust server, in the `kiso` run) |
| FastAPI | `fastapi_app.py` | SQLAlchemy 2 async, asyncpg | Uvicorn (uvloop) |
| Django Ninja | `ninja_app.py` | Django async ORM, psycopg 3 with pool | Uvicorn (uvloop), Django ASGI |
| Django Bolt | `benchapp/api.py` | Django async ORM, psycopg 3 with pool | its own Actix server (`runbolt`) |

Every service runs 4 server processes with a pool of 10 connections per process. Responses are not cached; no framework-specific response compression or tuning beyond what is in these files. Hinoki's native extension is built with `maturin develop --release`.

### Other runtimes (JavaScript)

The `javascript` server mode serves the same API from `js/` with exactly pinned npm packages (`js/package-lock.json`, installed with `npm ci`):

| | Code | Runtime | Framework | Driver | Processes |
| --- | --- | --- | --- | --- | --- |
| Fastify | `js/fastify_app.js` | Node.js 26 | Fastify 5.12.8 | node-postgres (`pg` 8.23.1), named prepared statements | `node:cluster`: 4 workers and a primary that hands out connections |
| Hono | `js/hono_app.js` | Node.js 26 | Hono 4.13.21, `@hono/node-server` 2.1.11 | node-postgres (`pg` 8.23.1), named prepared statements | `node:cluster`: 4 workers and a primary that hands out connections |
| Bun.serve | `js/bun_app.js` | Bun 1.4.5 | none (`Bun.serve` routes) | `Bun.SQL` (built in), prepared statements | 4 processes on one port (`reusePort`) and an idle launcher |

Parity with the Python stacks: the same database, schema, seed and SQL statements (the statement texts are in `js/common.js`), the same per-scenario statement counts checked by `verify.py`, the same validation rules and statuses (`422`, `404`), the same bodies and content types, 4 serving processes with a pool of `POOL` (10) connections each, the same CPU pinning (`taskset` applies to the launcher and every child), no response caching and `NODE_ENV=production`. `run.py` passes the database URL, pool, worker count and port as `BENCH_DATABASE_URL`, `BENCH_POOL`, `BENCH_WORKERS` and `BENCH_PORT`, and strips every other inherited `BENCH_*` variable from each server's environment. The Node.js and Bun versions and the package versions are recorded under `javascript` in `environment.json`.

Known differences, disclosed rather than hidden:

* A different language, runtime and driver. The comparison is of whole stacks, not of Hinoki's Rust core with a JavaScript engine.
* The Node.js cluster primary accepts connections and passes them to the workers round-robin. Granian also has a main process, but its role differs. Bun's processes share the port through `SO_REUSEPORT`, and the kernel distributes connections.
* Fastify validates with its JSON Schema compiler (Ajv). Hono and Bun validate by hand with the same rules. Fastify's and Ajv's own `400` responses are mapped to `422`, as are malformed JSON bodies on every stack.
* JavaScript `Date` has millisecond precision. The seed uses whole minutes, so the bodies are identical; a row with microseconds would lose them.

The report shows these stacks in the overview as a separate **Other runtimes** group, after the Python stacks. The result is retained in its own directory so that Python-only results stay comparable with earlier runs.

`create` uses per-statement autocommit in all four Python implementations. Hinoki leaves
the endpoint `atomic` option off, SQLAlchemy uses PostgreSQL `AUTOCOMMIT`,
and Django keeps its default autocommit. The JavaScript stacks issue both
statements without a transaction, in autocommit mode. Each request still issues one `INSERT`
and one author `SELECT`; the response verifier checks both the SQL count and
the stored row before measurements.

## Procedure

`run.py` does, for each stack in turn: reset the database to the seed, start the server pinned to CPUs 0, 1, 4 and 5, run `verify.py`, warm up every scenario, then measure each scenario five times with `bombardier` (100 connections, 100 000 requests per sample, load generator pinned to CPUs 2, 3, 6 and 7) and keep each raw JSON result. The two CPU sets are the hyperthread pairs of different physical cores, so the load generator never takes execution resources from the server (on another machine set `BENCH_SERVER_CPUS` and `BENCH_LOAD_CPUS` to disjoint physical cores). The whole sequence runs twice per stack (`--restarts 2`, a fresh server start each time); the report pools every sample of every start (median, range and CV over all of them, no sample discarded) and also lists each start's own median. The method follows Django Bolt's published benchmark method. Before measuring, the runner requires the port to be free, waits for a `200` from the server, and stops it with SIGTERM (SIGKILL after a grace period) and waits for the port to be released, so an old server is never measured. PostgreSQL runs in a disposable container (`postgres:17`, `pg_stat_statements` enabled). Created rows are removed after the `create` scenario.

There are three independent runs, plus an optional fourth. The documented-stack run uses Granian for
Hinoki, Uvicorn for FastAPI and Django Ninja, and Django Bolt's own server. The
`same-granian` control uses the same Granian configuration for the three ASGI
applications; it excludes Bolt because Bolt does not expose ASGI. The `kiso` run
serves only Hinoki, on its own Rust server (`hinoki serve --server kiso`, 4
workers, the same pool and CPU pinning); Kiso is not an ASGI server, so the other
stacks have no counterpart in it. The optional `javascript` run serves only
Fastify, Hono and Bun.serve with the same verification, settings and CPU
pinning. These slices answer different questions and
must not be combined into a single ranking.

**Nothing is measured unless verification passes.** `verify.py` compares each service with an oracle that reads the rows straight from PostgreSQL: the same status and the same data for the same requests (datetimes compared after parsing), and the same number of SQL statements per scenario, counted with `pg_stat_statements` after every worker has been warmed (PostgreSQL's own foreign-key trigger statements excluded). That is what prevents one framework from looking faster because it loads less, or slower because it issues an N+1.

Transaction control statements are not counted. The refreshed `create` handlers use per-statement autocommit across all four implementations; the earlier 2026-11-14 result predates this alignment and is exploratory only.

## Reproduce

```sh
docker run -d --name hinoki-bench-pg -e POSTGRES_PASSWORD=bench-only -e POSTGRES_DB=bench \
  -p 127.0.0.1:54402:5432 postgres:17 -c shared_preload_libraries=pg_stat_statements \
  -c pg_stat_statements.track=all -c max_connections=300
docker exec hinoki-bench-pg psql -U postgres -d bench -c "CREATE EXTENSION pg_stat_statements"
go install github.com/codesenberg/bombardier@latest   # puts bombardier in ~/go/bin
cd benchmarks/comparative
uv sync --locked
VIRTUAL_ENV=$PWD/.venv uvx maturin develop --release --uv --manifest-path ../../crates/bindings/Cargo.toml
BENCH_DATABASE_URL=postgres://postgres:bench-only@127.0.0.1:54402/bench BENCH_POSTGRES_CONTAINER=hinoki-bench-pg \
  uv run --locked python run.py --server-mode documented --out results/YYYY-MM-DD/documented
BENCH_DATABASE_URL=postgres://postgres:bench-only@127.0.0.1:54402/bench BENCH_POSTGRES_CONTAINER=hinoki-bench-pg \
  uv run --locked python run.py --server-mode same-granian --out results/YYYY-MM-DD/same-granian
BENCH_DATABASE_URL=postgres://postgres:bench-only@127.0.0.1:54402/bench BENCH_POSTGRES_CONTAINER=hinoki-bench-pg \
  uv run --locked python run.py --server-mode kiso --out results/YYYY-MM-DD/kiso
uv run --locked python report.py results/YYYY-MM-DD/documented results/YYYY-MM-DD/same-granian \
  results/YYYY-MM-DD/kiso > ../../docs/benchmarks.md
```

The optional JavaScript run needs Node.js 26 and npm, and Bun 1.4.5. To install Bun into your home directory with a pinned version and a verified checksum:

```sh
curl -fLO https://github.com/oven-sh/bun/releases/download/bun-v1.4.5/bun-linux-x64.zip
curl -fLO https://github.com/oven-sh/bun/releases/download/bun-v1.4.5/SHASUMS256.txt
grep ' bun-linux-x64.zip$' SHASUMS256.txt | sha256sum -c -
unzip -q bun-linux-x64.zip -d ~/.local/opt/bun-v1.4.5
ln -s ~/.local/opt/bun-v1.4.5/bun-linux-x64/bun ~/.local/bin/bun
```

`run.py` finds `node` and `bun` on `PATH`, or at the paths in `BENCH_NODE` and `BENCH_BUN`. It stops with a message if a runtime is missing. Then:

```sh
(cd js && npm ci)
BENCH_DATABASE_URL=postgres://postgres:bench-only@127.0.0.1:54402/bench BENCH_POSTGRES_CONTAINER=hinoki-bench-pg \
  uv run --locked python run.py --server-mode javascript --out results/YYYY-MM-DD/javascript
uv run --locked python report.py results/YYYY-MM-DD/documented results/YYYY-MM-DD/same-granian \
  results/YYYY-MM-DD/kiso results/YYYY-MM-DD/javascript > ../../docs/benchmarks.md
```

Use `--framework fastify`, `hono` or `bun` to run a single stack.

Use `--smoke` with any `--server-mode` to verify behavior and query counts
without measuring. Use `--runs`, `--restarts`, `--requests` and `--warmup` only when recording
the chosen values in the report metadata and rationale. Remove the disposable
container when finished: `docker rm -f hinoki-bench-pg`.

`--max-load N` holds every measured sample until the one-minute load average is
at most `N` (it polls every 10 s and stops the run after 30 minutes without
getting there). The load at the start of each sample is appended to
`loadavg.txt` in the results directory, and the limit is recorded as `max_load`
in `environment.json`. Without the option the load is only recorded.

If a workload's sample CV exceeds 15%, inspect host variance and repeat that
scenario with `--scenario create --run-offset 5` (or the affected scenario),
keeping both the original and additional raw samples. The report includes every
sample found in its directory; additional captures get separate environment
metadata files rather than replacing the first environment record.

Exact package versions, the CPU model, the load average when the run started and every setting are written to `environment.json`; each raw `bombardier` result (`{stack}-{scenario}-r{start}-{n}.json`) is kept next to the report, with the server log and worker CPU times of every start.

Results retained from before this method were measured with `oha` (fixed duration, 64 connections, one start, files named `{stack}-{scenario}-{n}.json`). The report labels them as such and warns when both formats appear; the two are not comparable and must not be mixed.

The result sets rendered on the site are retained; earlier and interim runs were removed (see git history):

* `results/2026-11-21/`: Linux 7.2.11, i5-10300H, CPU pinning, `bombardier`
  v1.2.6, run from a text console with the desktop closed. One-minute load
  average at most 1.00 before every sample. `documented`, `same-granian` and
  `kiso` modes, two starts of five samples per scenario for every stack. The
  `create` scenario of `hinoki-granian` and `hinoki-kiso` was repeated with
  `--run-offset 5` because the first ten samples had a CV above 15%; the
  repeat is pooled with them and no sample was discarded. Both still
  exceeded 15% (their samples split into a fast group near 5.9 k req/s and a slow group
  between 2.4 k and 4.8 k, independent of order and load), so the report
  marks those two results Unstable. Server logs are not retained.

The public page is generated by `report.py` from the raw JSON of the latest
Linux results, with no number written by hand. Reading order: one sentence
with what was measured, where and when and how Hinoki on Kiso compares with
Hinoki on Granian; a matrix of every stack (Hinoki on Granian, Hinoki on Kiso,
FastAPI, Django Ninja, Django Bolt and, when a `javascript` run is passed, the
Other runtimes group) against every request type, with the
highest median marked; one tab per request type with bars on one axis, the
exact table (median, lowest to highest sample, p50 and p99 latency, CV) and the
samples of any unstable result; then the `documented` and `same-granian` views
for the reader who wants the server taken into account, and the method and
limits in collapsible sections. The stack legend filters the overview and the
open tab; without JavaScript every tab is shown. A result whose
requests-per-second CV exceeds 15% is labelled Unstable with the reason; its
median and every sample stay visible but it is never highlighted or used in a
comparison. A gap under 10% between two medians is reported as a tie.

`tests/browser/benchmarks.py` checks the built page in real browsers
(layout at four viewports, tabs by click, touch and keyboard, the legend filter);
see its docstring.

## Limits

* One machine, one day, a single loopback connection; the load generator, PostgreSQL and the servers share its memory and its other workloads (the machine was not idle: see the load average in the report).
* Five samples of 100 000 requests per scenario and start, two starts, by default. The report shows the median and range; it does not claim statistical significance.
* A request path that does one fixed row (`detail`) or one fixed page (`list`) is warm in PostgreSQL's cache; it says nothing about cold data or large tables.
* Connection-pool sizes are equal per process, but each library schedules work differently (Django's async ORM runs queries through a thread, for example); equal settings are not equal behaviour.
* `create` includes a database write per request; rows are removed afterwards.
* The same-server control covers only the three ASGI applications. Django Bolt's server remains part of the documented-stack slice and cannot be treated as an ASGI control.
* The JavaScript stacks differ in language, runtime, driver and process model (see Other runtimes above). Their results describe those stacks on this machine; they say nothing about Hinoki's Rust core in isolation.
