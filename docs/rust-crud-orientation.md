# Native CRUD phase 1: end-to-end orientation run

This is one host-specific orientation measurement, not a performance claim or a framework ranking.
The primary table reports five alternating pairs of Hinoki's automatically selected Rust generated CRUD path (declared with the since-removed `CrudAPI` class when measured; `crud(...)` now generates the same routes) and a manually implemented equivalent Django Bolt endpoint.
Both apps use the same PostgreSQL table and deterministic 10,000-row seed. Their list, detail, create and update responses were verified against the database before timing.

Run started: 2026-11-18T03:09:11.864738+00:00. Host: Intel(R) Core(TM) i5-10300H CPU @ 2.50GHz (8 logical CPUs), Linux-7.2.11-arch1-1-x86_64-with-glibc2.44; Python 3.13.18; PostgreSQL postgres (PostgreSQL) 17.12 (Debian 17.12-1.pgdg13+1); Rust rustc 1.100.0 (0ef1bc386 2026-11-09); Oha oha 1.17.0.
Hinoki 0.0.1 uses Kiso and tokio-postgres; Django Bolt 0.13.0 uses Django 6.1.3 with psycopg 3.3.9 and its Actix server. Each starts 4 processes with a pool of 10 database connections per process.
Scenarios: list page sizes 20 and 100, detail, create and PATCH update. Each sample has 10 s warm-up, then 20 s measured with 64 concurrent connections. Server CPUs: 0-3; load generator CPUs: 6-7; response compression disabled.
Each sample's one-minute host load is checked before its server starts and must be at most 1.0. Across the samples, scenario start loads ranged from 0.81 to 1.00. SQL calls are read from `pg_stat_statements` after each timed request window; the table reports the median calls per successful HTTP request.

| Scenario | Hinoki median req/s (range, CV) | Django Bolt median req/s (range, CV) | Paired median ratio | SQL calls/request (Hinoki / Bolt) | Median p50 ms (Hinoki / Bolt) | Median p99 ms (Hinoki / Bolt) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| list_20 | 4444.4 (4340.9-4492.7, 1.3%) | 2342.2 (2289.1-2347.0, 1.0%) | 1.90x | 2.00 / 2.00 | 13.62 / 27.18 | 34.91 / 43.04 |
| list_100 | 3860.4 (3843.6-5415.4, 19.1%) | 2151.1 (1864.7-2160.4, 7.8%) | 2.06x | 2.00 / 2.00 | 14.51 / 28.58 | 35.67 / 58.00 |
| detail | 36700.3 (36062.4-36868.6, 0.9%) | 6940.5 (6798.4-6953.3, 1.1%) | 5.30x | 1.00 / 1.00 | 1.61 / 8.86 | 4.35 / 17.92 |
| create | 6221.5 (5908.0-6468.7, 3.8%) | 2561.0 (2412.8-2629.9, 3.3%) | 2.45x | 2.00 / 1.00 | 8.71 / 21.65 | 23.87 / 58.14 |
| update | 327.6 (267.9-357.5, 11.8%) | 332.4 (191.5-375.0, 23.5%) | 1.07x | 3.04 / 2.02 | 168.48 / 155.92 | 647.31 / 362.09 |

The paired ratio is a descriptive ratio of the five pairwise throughput values. The samples are not a universal ranking and do not establish production performance. Host, OS, Python, server, ORM, driver, database, warm-cache state, and workload all affect these results. CV above 15% marks that scenario as unstable for interpretation; no sample is dropped.

Raw `oha` JSON, per-sample start-load metadata and SQL stats, server logs, environment metadata, verifier output, request bodies, and the machine-readable summary are available in the [2026-11-18 benchmark archive](https://github.com/caspel26/hinoki/releases/download/bench-2026-11-18-crud-phase1/bench-2026-11-18-crud-phase1.tar.gz) (SHA-256: `c32ea9a982b5bc5d30cce8d79f10bc93890603cebb123d4f927f41436049a3b2`; [checksum file](https://github.com/caspel26/hinoki/releases/download/bench-2026-11-18-crud-phase1/bench-2026-11-18-crud-phase1.tar.gz.sha256)). The archive manifest records per-file hashes and measurement provenance.

Reproduction: build the native extension in release mode, start an isolated PostgreSQL 17 container, then run `phase1_orientation.py` as documented in `benchmarks/README.md`. The script refuses to time a sample whose starting load is above 1.0.

## Supplemental samples

Because the initial `list_100` Hinoki CV exceeded 15%, five additional alternating pairs (6-10) were collected separately. This block is not pooled into the primary table:

| Scenario | Hinoki median req/s (range, CV) | Django Bolt median req/s (range, CV) |
| --- | ---: | ---: |
| list_100, pairs 6-10 | 5088.4 (5073.4-5128.2, 0.4%) | 2080.8 (2066.3-2085.8, 0.4%) |

The supplemental block is internally stable, while the initial Hinoki list_100 series remains unstable and shows a change in throughput regime. Treat both blocks as host-specific orientation data; the stable follow-up does not erase the initial variance.

The supplemental update series stopped at Hinoki pair 7 after unsuccessful responses (status distribution: 200 = 3,554, 500 = 20). Its server log reports database connection acquisition timeouts. The failed sample and log are retained and excluded from throughput summaries; the initial update series remains volatile (Hinoki CV 11.8%, Django Bolt CV 23.5%).
