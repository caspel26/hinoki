---
status: implemented
---

# Request path with handwritten handlers: baseline before native handler inputs

This is one host-specific baseline, not a performance claim or a framework ranking.
It measures a fixture app whose handlers do no work, on Hinoki's Rust server and on Granian, so each number is the request path around the handler: routing, input binding, validation, the handler call and the response. It was taken on commit `63a2248`, before the native typed-input, body, DELETE, transaction, access and rate-limit work, and is the comparison point for later runs of the same harness.

Run started: 2026-11-18T11:02:42Z. Host: Intel(R) Core(TM) i5-10300H CPU @ 2.50GHz (8 logical CPUs), Linux-7.2.11-arch1-1-x86_64-with-glibc2.44; Python 3.13.18; Rust rustc 1.100.0; Oha 1.17.0; Granian 2.8.8.
Each stack starts 4 workers with a pool of 10 database connections per worker. Scenarios run five alternating pairs, each sample with 10 s warm-up and 20 s measured at 64 connections. Server CPUs: 0-3; load generator CPUs: 6-7; response compression disabled.
The one-minute host load was checked before every sample and had to be at most 1.0; starting loads ranged from 0.79 to 1.00. A response check ran first: both stacks returned the same status and body for every scenario.

| Scenario | Hinoki Rust server median req/s (range, CV) | Granian median req/s (range, CV) | Paired median ratio |
| --- | ---: | ---: | ---: |
| health | 89845.4 (75803.0-92285.3, 7.8%) | 47453.9 (42091.6-47722.6, 5.2%) | 1.93x |
| echo_str | 83434.6 (72745.0-84722.2, 6.0%) | 43111.6 (42172.0-43804.3, 1.5%) | 1.91x |
| echo_typed | 41010.0 (34942.7-42028.1, 7.4%) | 38499.1 (32898.4-39413.1, 7.2%) | 1.06x |
| post_small | 26140.4 (25561.4-26914.2, 2.1%) | 24459.2 (24310.1-24552.0, 0.4%) | 1.07x |
| post_10k | 24021.4 (23798.7-24271.3, 0.7%) | 21477.3 (21303.8-21549.3, 0.4%) | 1.12x |
| post_100k | 11817.4 (11520.0-11880.3, 1.2%) | 12113.1 (11732.2-12184.1, 1.5%) | 0.98x |
| patch | 26244.1 (25508.7-26817.1, 2.1%) | 24096.4 (23906.3-24278.3, 0.6%) | 1.08x |
| delete | 51929.0 (51031.0-53015.9, 1.4%) | 47243.1 (46186.4-47456.3, 1.3%) | 1.11x |
| post_tx | 15096.1 (4847.2-15364.5, 35.2%) | 13467.3 (13242.3-13525.2, 0.9%) | 1.12x |

The paired ratio is the median of the five pairwise throughput ratios. CV above 15% marks a series as unstable for interpretation; no sample is dropped. The Hinoki `post_tx` series is unstable (one sample at 4847 req/s against about 15000 for the others) and supports no conclusion; it needs a repeat.

Which lane the Rust server used is in `lanes.json`: only `GET /health` and `GET /echo-str/{item_id}` were on the native handler lane. Every other route ran on the Python pipeline, which is why those scenarios sit close to Granian.

Raw `oha` JSON, per-sample start-load metadata, server logs, verifier output and the machine-readable summary are retained in this directory (`samples.jsonl`, `summary.json`, `environment.json`, `verification.json`, `lanes.json`).

Reproduction: build the native extension in release mode, start a disposable PostgreSQL container, set `P2BENCH_DATABASE_URL`, then run `phase2_baseline.py --out <dir>` from `benchmarks/comparative/`. The script waits until the one-minute load is at most 1.0 before each sample.
