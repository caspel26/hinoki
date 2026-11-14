---
status: implemented
---

# Benchmarks

One small API implemented four times (Hinoki, FastAPI, Django Ninja, Django Bolt), measured on one machine on 2026-11-14. These are the numbers that run produced; they are not a ranking and not a statement about any framework in general. Hinoki is pre-alpha, and a result here is not evidence that it is ready for production.

## What was measured

The same service against the same PostgreSQL tables and the same seed (100 authors, 10 000 posts):

| Scenario | Request | Work per request |
| --- | --- | --- |
| `health` | `GET /health` | static JSON, no database |
| `list` | `GET /posts?page=1&page_size=20` | one `COUNT` and one joined `SELECT`; 20 posts with their authors and a page envelope |
| `detail` | `GET /posts/{id}` | one joined `SELECT` |
| `create` | `POST /posts` | validate, one `INSERT`, one author `SELECT` |

Each framework runs its own idiomatic stack at its own documented server: Hinoki with its ORM on Granian; FastAPI with SQLAlchemy 2 async and asyncpg on Uvicorn; Django Ninja and Django Bolt with the Django async ORM and psycopg 3 pooling (Ninja on Uvicorn, Bolt on its own Rust server). Every service ran 4 server processes with 10 database connections each.

Before any measurement, a verifier compared each service with an oracle that reads the rows straight from PostgreSQL: the same status, the same data and the same number of SQL statements per scenario (counted with `pg_stat_statements`), so that a framework cannot look faster because it loads less or slower because it issues extra queries. A service that failed verification would not have been measured; all four passed.

## Results

Intel(R) Core(TM) i5-10300H CPU @ 2.50GHz, 8 logical CPUs; servers pinned to CPUs 0-3, the load generator (`oha`) to CPUs 6-7; 64 concurrent connections; 10 s warm-up, then 3 runs of 20 s per scenario. The table gives the median across the runs and the range. No response was anything other than the expected status in any run.

### health

| Framework | Requests/s (median of runs) | Range across runs | p50 ms | p95 ms | p99 ms | Responses not `200` |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Hinoki | 41,014 | 39,489 – 41,941 | 1.5 | 2.9 | 3.8 | 0 |
| FastAPI | 17,592 | 17,488 – 18,026 | 3.5 | 5.2 | 6.5 | 0 |
| Django Ninja | 4,408 | 4,369 – 4,474 | 12.9 | 22.0 | 27.3 | 0 |
| Django Bolt | 63,500 | 62,453 – 63,652 | 0.9 | 1.4 | 1.6 | 0 |

### list

| Framework | Requests/s (median of runs) | Range across runs | p50 ms | p95 ms | p99 ms | Responses not `200` |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Hinoki | 1,484 | 1,151 – 1,544 | 43.4 | 64.0 | 83.7 | 0 |
| FastAPI | 1,325 | 1,304 – 1,327 | 31.8 | 128.7 | 204.1 | 0 |
| Django Ninja | 906 | 905 – 922 | 67.1 | 99.8 | 110.5 | 0 |
| Django Bolt | 1,563 | 1,550 – 1,601 | 39.3 | 62.1 | 73.7 | 0 |

### detail

| Framework | Requests/s (median of runs) | Range across runs | p50 ms | p95 ms | p99 ms | Responses not `200` |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Hinoki | 9,211 | 9,197 – 9,240 | 6.7 | 10.4 | 12.6 | 0 |
| FastAPI | 2,742 | 2,724 – 2,785 | 21.8 | 55.8 | 73.1 | 0 |
| Django Ninja | 1,729 | 1,707 – 1,733 | 36.0 | 54.7 | 63.4 | 0 |
| Django Bolt | 4,551 | 4,541 – 4,662 | 13.3 | 22.4 | 25.7 | 0 |

### create

| Framework | Requests/s (median of runs) | Range across runs | p50 ms | p95 ms | p99 ms | Responses not `201` |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Hinoki | 2,732 | 1,866 – 3,001 | 17.2 | 25.5 | 76.9 | 0 |
| FastAPI | 1,761 | 1,759 – 1,784 | 34.5 | 61.4 | 83.4 | 0 |
| Django Ninja | 981 | 764 – 1,120 | 46.4 | 82.8 | 849.8 | 0 |
| Django Bolt | 1,406 | 1,246 – 1,996 | 28.2 | 41.7 | 65.1 | 0 |

## How to read this

* The `health` row isolates framework overhead with no database. The other three rows are dominated by the database round trips and, for `list`, by serialising twenty posts (about 9 KB per response); differences there are smaller, and the ranges across runs overlap in places.
* `list` and `create` varied noticeably between runs for some frameworks (see the ranges). Three 20-second runs do not support a claim of significance.
* Each library schedules database work differently (for example Django's async ORM calls the database from a worker thread), so equal pool sizes are not equal behaviour.

## Limits

* One machine, one day. The CPU is a laptop part and the machine was not idle: the load average was 2.9 when the run started (other containers were running). PostgreSQL was not pinned and shared CPUs with the rest.
* The data is small and warm in PostgreSQL's cache; `detail` hits one row and `list` one page. Nothing here measures cold data, large tables, long-running connections, memory, or other payloads.
* Loopback networking only; no TLS, no proxy, no authentication, no compression.
* Each application is written once, by the Hinoki project, from each framework's documentation. Another author could tune any of them further; settings that were changed on purpose are in the code (for example the Django time zone is set to the server's so Django does not issue a `SET TimeZone` per connection).
* Hinoki's native extension was built in release mode (`maturin develop --release`). The default development build is much slower.

## Reproduce

The code, the raw `oha` output for every run, the verifier output and the machine and package versions are in `benchmarks/comparative/` (`README.md` there lists the exact commands). Versions used: Hinoki 0.0.1 (pre-alpha, this repository), Python 3.13.18, FastAPI 0.162.7, Starlette 1.10.0, Uvicorn 0.54.0, SQLAlchemy 2.1.8, asyncpg 0.32.0, Django 6.1.3, Django Ninja 1.8.0, Django Bolt 0.13.0, psycopg 3.3.9, Granian 2.8.8, Pydantic 2.14.3, postgres (PostgreSQL) 17.12, oha 1.17.0.
