# Comparative benchmark: Django Bolt's seven routes on every stack

A bounded benchmark of one small API served by Hinoki, Django Bolt, Litestar, FastAPI and Django Ninja, with a separate run of the same API on the JavaScript servers of Bolt's own comparison (Elysia on Bun, Hono on Bun, Hono on Node.js). The routes, data shapes and settings follow Django Bolt's published comparison ([bolt.farhana.li/benchmarks](https://bolt.farhana.li/benchmarks/), [python-api-frameworks-benchmark](https://github.com/FarhanAliRaza/python-api-frameworks-benchmark)), so the results can be read next to Bolt's. The code is written for this repository (the reference repository has no licence, so nothing is copied); only the routes, shapes, sizes and settings follow it. It measures what these implementations did on one machine on one day, nothing more. Hinoki is pre-alpha; this is not evidence of production readiness for any of them.

## The service

PostgreSQL tables created by `parity_schema.sql` in the column layout Django gives the models of Bolt's comparison app (no framework creates or migrates them). `parity_seed.py` loads the same deterministic rows (`parity_data.py`) before every stack: 10 benchmark users, 50 authors, 100 tags, 500 articles with 2 to 5 tags each, 2 000 comments and one `auth_user`. Timestamps are whole milliseconds, so JavaScript `Date` keeps them exactly.

| Scenario | Request | What the service does | SQL statements |
| --- | --- | --- | --- |
| `json-1k` | `GET /json-1k` | a fixed list of person records, 1 171 bytes in compact JSON | 0 |
| `json-10k` | `GET /json-10k` | the same, 10 864 bytes | 0 |
| `db` | `GET /db` | 10 rows of `benchmark_users` | 1 |
| `articles` | `GET /articles?page=1&page_size=20` | newest articles, each with its author and tags, in a page envelope | 3 (count, articles with author, tags) |
| `article` | `GET /articles/1` | one article with author, tags and comments (newest first); `404` when missing | 3 |
| `auth-me` | `GET /auth/me` | the user named by the HS256 JWT in the `access_token` cookie; `401` without a valid token | 1 |
| `auth-articles` | `GET /auth/articles?page=1&page_size=20` | `/articles` behind the same cookie | 4 |

Out-of-range `page`/`page_size` are clamped to `1` and `1..100` on every stack, as Bolt's handler does; a value that is not an integer is `422`. The runner mints one token per sample (`parity_data.mint_token`, PyJWT, 24 h expiry, `sub` is the user id) and sends it as a cookie header; Bolt's comparison logs in through each framework's own route instead.

## Implementations

Every handler is async on every stack.

| | Code | ORM / driver | Encoder | JWT | Server (`documented` mode) |
| --- | --- | --- | --- | --- | --- |
| Hinoki | `parity_hinoki.py` | Hinoki ORM (Rust, tokio-postgres) | Hinoki | Hinoki's `JWTAuth` on the cookie, in an `Access` policy | Granian; Kiso in the `kiso` mode |
| Django Bolt | `parityapp/api.py`, `parity_dj_settings.py` | Django ORM, psycopg 3 pool | msgspec | Bolt's `JWTAuthentication` | `runbolt`, 8 executor and ORM threads |
| Litestar | `parity_litestar.py` | SQLAlchemy 2 async, asyncpg (`parity_sqla.py`) | msgspec | Litestar's `JWTCookieAuth` | Uvicorn (uvloop, httptools) |
| FastAPI | `parity_fastapi.py` | SQLAlchemy 2 async, asyncpg (`parity_sqla.py`) | Pydantic | PyJWT in a dependency | Uvicorn (uvloop, httptools) |
| Django Ninja | `parity_ninja.py`, `parity_ninja_settings.py` | Django ORM on a bounded executor, psycopg 3 pool | Pydantic, compact JSON renderer | django-ninja-jwt on `APIKeyCookie` | Uvicorn (uvloop, httptools) |

SQLAlchemy's engines use `pool_pre_ping=True`, as Bolt's comparison does; asyncpg's ping (`;`) is not counted as a statement. Django Ninja runs the sync ORM through `sync_to_async(thread_sensitive=False)` on an executor of as many threads as pool connections and releases the connection after each call; the user lookup of django-ninja-jwt runs there too, because its own path keeps the connection on the request's thread until the request ends, and with 100 connections and 8 pooled database connections every connection is then held by a request waiting for another one (the service stalls until the pool times out). Ninja's renderer writes compact JSON (no spaces after `,` and `:`) so that `json-1k` and `json-10k` have the bytes every other stack sends.

### Other runtimes (JavaScript)

The `javascript` server mode serves the same routes from `js/` on the three servers of Django Bolt's [comparison with JavaScript servers](https://bolt.farhana.li/benchmarks/#against-javascript-servers), in its order, with exactly pinned npm packages (`js/package-lock.json`, installed with `npm ci`):

| | Code | Runtime | Framework | Driver | More than one process |
| --- | --- | --- | --- | --- | --- |
| Elysia on Bun | `js/elysia_app.js` | Bun 1.4.5 | Elysia 1.4.32 | `Bun.SQL` (built in), prepared statements | processes on one port (`reusePort`) and an idle launcher |
| Hono on Bun | `js/hono_bun.js` | Bun 1.4.5 | Hono 4.13.21 on `Bun.serve` | `Bun.SQL` (built in), prepared statements | processes on one port (`reusePort`) and an idle launcher |
| Hono on Node.js | `js/hono_node.js` | Node.js 26 | Hono 4.13.21, `@hono/node-server` 2.1.11 | node-postgres (`pg` 8.23.1), named prepared statements | `node:cluster`, a primary that hands out connections |

The two Hono stacks share one application (`js/hono_routes.js`) and differ only in runtime, server adapter and driver; the two Bun stacks share the process model and client (`js/bun_runtime.js`). With one worker (the default) each serves in a single process with no cluster primary or launcher. The SQL texts and response shapes are in `js/common.js`. Each framework reads the cookie itself (Hono's `getCookie`, Elysia's `cookie`), and the JWT is checked with `node:crypto` (HS256 signature compared in constant time, `exp` required, numeric `sub`); `test_run.py` checks that it accepts the runner's token and rejects a forged, an expired and a malformed one, and that Node.js encodes the two `json-*` values to the bytes of `parity_data.py` (the values reach the applications through `payloads.json`, written by `run.py` and passed as `BENCH_PAYLOADS`). Elysia and Hono run without validation schemas (no Elysia `t` models). `run.py` passes the database URL, pool, worker count and port as `BENCH_DATABASE_URL`, `BENCH_POOL`, `BENCH_WORKERS` and `BENCH_PORT`, sets `NODE_ENV=production` and strips every other inherited `BENCH_*` variable. The Node.js, Bun and package versions are recorded under `javascript` in `environment.json`.

What Bolt publishes about its JavaScript comparison, and what is chosen here because it does not:

| | Bolt's comparison | This harness |
| --- | --- | --- |
| Servers | Elysia on Bun, Hono on Bun, Hono on Node.js | the same |
| Routes | 26 B, 1 KB and 10 KB JSON only | the seven routes of the Python comparison, database and JWT included; no 26 B route |
| Processes | 1, and 8 (Node `node:cluster`, Bun 8 processes with `reusePort`) | `--workers 1`, and `--workers 8` with the same process models |
| Load | 100 connections, 100 000 requests | the Python comparison's `-c 100 -d 10s` (`--requests 100000` reproduces the request count) |
| Versions, code, driver, JWT | not published (Bolt 0.10, July 2026) | the latest releases when written, pinned above; `Bun.SQL` on Bun (its built-in client) and `pg` on Node.js; the shared `node:crypto` check |

Known differences, disclosed rather than hidden: a different language, runtime and driver (the comparison is of whole stacks, not of Hinoki's Rust core with a JavaScript engine), and with several processes the Node.js cluster primary passes connections to the workers round-robin while Bun's processes share the port through `SO_REUSEPORT`. Bun 1.3.14 broke its PostgreSQL connections under 64 concurrent requests (`ERR_POSTGRES_INVALID_MESSAGE`, after which the database routes stopped answering); Bun 1.4.5 does not, so use the pinned version. The report shows these stacks in a separate **Other runtimes** group after the Python stacks.

## Procedure

`run.py` does, for each stack in turn: reset the database to the seed, start the server pinned to CPUs 0, 1, 4 and 5, run `parity_verify.py`, then for every route warm up with 1 000 requests at 10 connections and measure with `bombardier -c 100 -d 10s` (load generator pinned to CPUs 2, 3, 6 and 7), keeping each raw JSON result. The whole sequence runs twice per stack (`--restarts 2`, a fresh server start each time) with 20 s of rest between stacks (`--cooldown 20`). The two CPU sets are the hyperthread pairs of different physical cores, so the load generator never takes execution resources from the server (on another machine set `BENCH_SERVER_CPUS` and `BENCH_LOAD_CPUS` to disjoint physical cores). Before measuring, the runner requires the port to be free, waits for a `200` from `/health`, and stops the server with SIGTERM (SIGKILL after a grace period) and waits for the port to be released, so an old server is never measured. PostgreSQL runs in a disposable container (`postgres:16-alpine`, `pg_stat_statements` enabled).

| Setting | Django Bolt's comparison | This harness |
| --- | --- | --- |
| Processes | 1 per framework (8 in the JavaScript comparison's second view) | `--workers 1`; `--workers 8` as a variant |
| Pool | 8 connections | `--pool 8` per process |
| Hinoki Kiso runtime threads | Not applicable (Bolt does not use Kiso) | `--runtime-threads 1` per worker (Kiso mode only) |
| Load | `bombardier -c 100 -d 10s` | the same (`--duration 10`; `--requests N` fixes the request count instead) |
| Warm-up | 1 000 requests at `-c 10` | the same |
| Samples | 1 per route and start, 2 starts | `--runs 1`, `--restarts 2` |
| Rest between frameworks | 20 s | `--cooldown 20` |
| Statistic | `sorted(samples)[len // 2]` | median of all samples with range and CV; the exact tables also show that statistic as "Upper median" |
| Python | 3.14 | the `uv` environment's interpreter, recorded in `environment.json` |
| PostgreSQL | 16 (Alpine) | 16 (Alpine) |
| Isolation | Docker, `--memory=750m`, no CPU pinning | no container or memory limit; `taskset` CPU pinning |
| Login | each framework's login route sets the cookie | the runner mints the token and sends the cookie |

There are four independent runs. `documented` serves each framework on the server of Bolt's comparison: Hinoki on Granian, Litestar, FastAPI and Django Ninja on Uvicorn, Django Bolt on its own Actix server. `same-granian` uses one Granian configuration for the four ASGI applications; it excludes Bolt because Bolt does not expose ASGI. `kiso` serves only Hinoki, on its own Rust server (`hinoki serve --server kiso`); Kiso is not an ASGI server, so the other stacks have no counterpart in it. `javascript` serves Elysia on Bun, Hono on Bun and Hono on Node.js. These slices answer different questions and must not be combined into a single ranking.

**Nothing is measured unless verification passes.** `parity_verify.py` compares each service with an oracle that reads the rows straight from PostgreSQL: the same status and data for every route (datetimes compared after parsing, so `Z` and `+00:00` or trailing zeros do not count as differences), the exact `json-*` bytes, `404`, `401` for a missing, bad or expired token, clamping, and the number of SQL statements per route, counted with `pg_stat_statements` after every worker has been warmed. That is what prevents one framework from looking faster because it loads less, or slower because it issues an N+1. The database routes are not byte-identical across stacks: each encoder writes datetimes its own way, so for example the `articles` page is 50 856 bytes on Ninja and the three JavaScript stacks, 51 036 on Bolt, Litestar and FastAPI and 51 336 on Hinoki in the smoke checks.

## Reproduce

```sh
docker run -d --name hinoki-bench-pg -e POSTGRES_PASSWORD=bench-only -e POSTGRES_DB=bench \
  -p 127.0.0.1:54402:5432 postgres:16-alpine -c shared_preload_libraries=pg_stat_statements \
  -c pg_stat_statements.track=all -c max_connections=300
docker exec hinoki-bench-pg psql -U postgres -d bench -c "CREATE EXTENSION pg_stat_statements"
go install github.com/codesenberg/bombardier@latest   # puts bombardier in ~/go/bin
cd benchmarks/comparative
uv sync --locked
VIRTUAL_ENV=$PWD/.venv uvx maturin develop --release --uv --manifest-path ../../crates/bindings/Cargo.toml
(cd js && npm ci)   # Node.js 26 and Bun on PATH, see below
export BENCH_DATABASE_URL=postgres://postgres:bench-only@127.0.0.1:54402/bench BENCH_POSTGRES_CONTAINER=hinoki-bench-pg
export BENCH_SERVER_CPUS=0,1,4,5 BENCH_LOAD_CPUS=2,3,6,7   # the defaults; check `lscpu -e` for the core pairs
for mode in documented same-granian kiso javascript; do
  uv run --locked python run.py --server-mode $mode --max-load 2 --out results/YYYY-MM-DD/$mode
done
uv run --locked python report.py results/YYYY-MM-DD/documented results/YYYY-MM-DD/same-granian \
  results/YYYY-MM-DD/kiso results/YYYY-MM-DD/javascript > ../../docs/benchmarks.md
```

For the multi-process variant add `--workers 8` and write to a separate directory (for example `results/YYYY-MM-DD/w8/documented`). Pass the directories of both runs to `report.py`: it reads each run's `settings.workers` and puts 8 workers and 1 worker on one page behind two tabs, never added together. With one worker count the page has no tabs.

The JavaScript run needs Node.js 26 and npm, and Bun 1.4.5. To install Bun into your home directory with a pinned version and a verified checksum:

```sh
curl -fLO https://github.com/oven-sh/bun/releases/download/bun-v1.4.5/bun-linux-x64.zip
curl -fLO https://github.com/oven-sh/bun/releases/download/bun-v1.4.5/SHASUMS256.txt
grep ' bun-linux-x64.zip$' SHASUMS256.txt | sha256sum -c -
unzip -q bun-linux-x64.zip -d ~/.local/opt/bun-v1.4.5
ln -s ~/.local/opt/bun-v1.4.5/bun-linux-x64/bun ~/.local/bin/bun
```

`run.py` finds `node` and `bun` on `PATH`, or at the paths in `BENCH_NODE` and `BENCH_BUN`, and stops with a message if a runtime is missing. Use `--framework NAME` to run a single stack and `--smoke` with any `--server-mode` to verify behaviour and statement counts without measuring. Use `--runs`, `--restarts`, `--duration`, `--requests`, `--workers`, `--pool` and `--runtime-threads` only when recording the chosen values in the report metadata and rationale. `--runtime-threads` sets Hinoki's `server.runtime_threads` for Kiso; it does not change Granian or the other stacks. The `environment.json` records the checkout's Git commit and whether it was dirty at run start, before the output directory is created. Remove the disposable container when finished: `docker rm -f hinoki-bench-pg`.

`--max-load N` holds every measured sample until the one-minute load average is at most `N` (it polls every 10 s and stops the run after 30 minutes without getting there). The load at the start of each sample is appended to `loadavg.txt` in the results directory, and the limit is recorded as `max_load` in `environment.json`. Without the option the load is only recorded.

If a route's sample CV exceeds 15%, inspect host variance and repeat that route with `--scenario articles --run-offset 1` (or the affected route), keeping both the original and additional raw samples. The report includes every sample found in its directory.

Exact package versions, the source commit and dirty status, the CPU model, the load average when the run started and every setting are written to `environment.json`; each raw `bombardier` result (`{stack}-{scenario}-r{start}-{n}.json`) is kept next to the report, with the server log, the verifier output and the worker CPU times of every start.

`results/2026-11-18/crud-phase1/` is the separate generated-CRUD orientation run documented in `benchmarks/README.md`. `results/2026-11-23/` contains an earlier posts-service run (`health`, `json-1k`, `json-10k`, `list`, `detail`, `create`, pool 10, PostgreSQL 17); `report.py` no longer reads those scenarios, and this is not the current public page. `schema.sql`, `seed.py`, `hinoki_app.py`, `ninja_app.py`, `dj_settings.py` and `benchapp/` belong to that earlier service and remain for the experiments in `benchmarks/README.md`.

The retained comparative data serves these outputs and experiments:

* `results/2026-11-24/` supplies the compact Hinoki-only report card in `theme/home.html`.
* `results/2026-11-25/` is the current full comparative report in `docs/benchmarks.md`; its `w8/` results are nested inside this date directory.
* `results/2026-11-23/` is the earlier posts-service run described above and is kept as historical data.

The separate CRUD orientation data remains at `results/2026-11-18/crud-phase1/`. Its reproduction instructions are in `benchmarks/README.md` and `docs/rust-crud-orientation.md`.

* `results/2026-11-23/`: historical earlier-service data, no longer read by `report.py`; Linux 7.0.0, AMD EPYC-Milan (Hetzner Cloud CCX33,
  8 dedicated vCPUs, 4 cores with 2 threads each, 32 GB, Helsinki),
  `bombardier` v1.2.6, PostgreSQL 17 in a container on the same machine. The
  server ran on CPUs 0 to 3 and the load generator on CPUs 4 to 7 (the
  thread pairs of two physical cores each), PostgreSQL was not pinned.
  `--max-load 2` before every sample. `documented`, `same-granian`, `kiso`
  and `javascript` modes, two starts of five samples per scenario for every
  stack. Hinoki was built from commit `478e1452`; later changes to Hinoki are
  not in these numbers. Fastify and Hono on `json-1k` are Unstable (CV above
  15%).

The public page is generated by `report.py` from the raw JSON, with no number written by hand. Reading order: one sentence with what was measured, where and when; tabs for the worker counts found in the directories (8 workers first, then 1 worker); in each, a Summary table (one row per test: JSON 1 KB, 10 rows from PostgreSQL and the JWT articles route; Hinoki on Kiso in requests per second, then one cell per competitor framework with the ratio of medians and the word faster or slower, Tie within 10% or Not settled, and a dash where a framework was not measured; the conditions are stated once above the table), then the Python frameworks (Hinoki on Kiso first as the reference, then Hinoki on Granian, Django Bolt, Litestar, FastAPI and Django Ninja), the same-Granian view and the other runtimes (Bun and Node.js) as separate views; each view has one tab per route with a table (requests per second with a bar from zero, the ratio against the reference, p50, p99, lowest to highest sample and CV), the exact numbers (median, upper median, CV, samples) and the samples of any unstable result. The method and limits come last in collapsible sections. Without JavaScript every tab is shown. A result whose requests-per-second CV exceeds 15% is labelled Unstable with the reason; its median and every sample stay visible but it is never highlighted or used in a comparison. A gap under 10% between two medians is reported as a tie. `tests/browser/benchmarks.py` checks the built page in real browsers; see its docstring.
## Limits

* One machine, one day, a loopback connection; the load generator, PostgreSQL and the servers share its memory and its other workloads (see the load average in the report). No container memory limit is applied, unlike Bolt's 750 MB.
* Two samples of 10 s per route by default (one per start). The report shows the median and range; it does not claim statistical significance.
* Every route reads the same few rows, warm in PostgreSQL's cache; it says nothing about cold data, large tables or writes.
* Connection-pool sizes are equal per process, but each library schedules work differently (Django's ORM runs queries through threads, for example); equal settings are not equal behaviour.
* The same-server control covers only the four ASGI applications. Django Bolt's server remains part of the documented slice and cannot be treated as an ASGI control.
* The JavaScript stacks differ in language, runtime, driver and process model (see Other runtimes above). Their results describe those stacks on this machine; they say nothing about Hinoki's Rust core in isolation.
