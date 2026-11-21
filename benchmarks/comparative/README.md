# Comparative benchmark: one service, four frameworks

A bounded benchmark of the same small API implemented on Hinoki, FastAPI, Django Ninja and Django Bolt. It measures what these four implementations did on one machine on one day, nothing more. Hinoki is pre-alpha; this is not evidence of production readiness for any of them.

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

The two `json-*` bodies are generated deterministically by `payloads.py` and returned byte for byte by all four applications; `verify.py` compares the exact bytes, the JSON content type and that no SQL statement ran.

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

`create` uses per-statement autocommit in all four implementations. Hinoki leaves
the endpoint `atomic` option off, SQLAlchemy uses PostgreSQL `AUTOCOMMIT`,
and Django keeps its default autocommit. Each request still issues one `INSERT`
and one author `SELECT`; the response verifier checks both the SQL count and
the stored row before measurements.

## Procedure

`run.py` does, for each stack in turn: reset the database to the seed, start the server pinned to CPUs 0–3, run `verify.py`, warm up every scenario, then measure each scenario five times with `bombardier` (100 connections, 100 000 requests per sample, load generator pinned to CPUs 6–7) and keep each raw JSON result. The whole sequence runs twice per stack (`--restarts 2`, a fresh server start each time); the report pools every sample of every start (median, range and CV over all of them, no sample discarded) and also lists each start's own median. The method follows Django Bolt's published benchmark method. Before measuring, the runner requires the port to be free, waits for a `200` from the server, and stops it with SIGTERM (SIGKILL after a grace period) and waits for the port to be released, so an old server is never measured. PostgreSQL runs in a disposable container (`postgres:17`, `pg_stat_statements` enabled). Created rows are removed after the `create` scenario.

There are three independent runs. The documented-stack run uses Granian for
Hinoki, Uvicorn for FastAPI and Django Ninja, and Django Bolt's own server. The
`same-granian` control uses the same Granian configuration for the three ASGI
applications; it excludes Bolt because Bolt does not expose ASGI. The `kiso` run
serves only Hinoki, on its own Rust server (`hinoki serve --server kiso`, 4
workers, the same pool and CPU pinning); Kiso is not an ASGI server, so the other
stacks have no counterpart in it. These slices answer different questions and
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

* `results/2026-11-17/`: Linux 7.2.11, i5-10300H, CPU pinning, built from the
  `main` of that day (generated lists and the other Rust request-path work
  merged since the earlier Linux run). Health, list and detail had five samples
  per stack; create had ten per stack (a second block of five with a separate
  environment record). Create CV stayed above 20% for every stack, so create
  results there are inconclusive for close comparisons. The one-minute load
  average was 0.90 when each slice started; the second slice followed the first
  immediately, so its five- and fifteen-minute averages were still high.

The public page is generated by `report.py` from the raw JSON of the 2026-11-17
Linux results. It shows median-and-range plots with a separate results table for
each scenario. Each scenario uses one requests-per-second axis across both
server views. A scenario whose requests-per-second CV exceeds 15% for any stack
is withheld from comparison; its raw files stay available. A gap under 10% between two medians is reported as a tie.

## Limits

* One machine, one day, a single loopback connection; the load generator, PostgreSQL and the servers share its memory and its other workloads (the machine was not idle: see the load average in the report).
* Five samples of 100 000 requests per scenario and start, two starts, by default. The report shows the median and range; it does not claim statistical significance.
* A request path that does one fixed row (`detail`) or one fixed page (`list`) is warm in PostgreSQL's cache; it says nothing about cold data or large tables.
* Connection-pool sizes are equal per process, but each library schedules work differently (Django's async ORM runs queries through a thread, for example); equal settings are not equal behaviour.
* `create` includes a database write per request; rows are removed afterwards.
* The same-server control covers only the three ASGI applications. Django Bolt's server remains part of the documented-stack slice and cannot be treated as an ASGI control.
* The retained macOS results (not on the public page) were not taken on an idle host: the one-minute load average was about 3 to 5 when it started, with background system services running. `quiet-condition.txt` in its results directory lists the busiest processes at that moment. macOS has no hard CPU affinity, so servers and the load generator were scheduled by the operating system.
* On macOS, Hinoki's Granian workers were unevenly loaded (one of the four used noticeably less CPU time than the others; see the `*-workers.txt` files), and Django Bolt used only one of its four processes. The two machines and operating systems differ, and on macOS PostgreSQL ran inside a virtual machine, so no number from one run may be compared with the other.
