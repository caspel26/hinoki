# Adapter request path: prepared plan (local design, not committed)

Status: draft for owner review, 2026-11-15. No adapter code has changed.

## Observed

All numbers: Apple M4 Pro, release extension, Python 3.14.0, host not idle; samples in
`benchmarks/results/` on branch `perf/request-profile` (not yet merged).

* One Granian worker, 64 connections, `GET /health`: minimal ASGI app 131 600 req/s
  (7.6 µs/request), minimal RSGI app 133 600 req/s, Hinoki 42 100 req/s (23.7 µs). Hinoki's
  layer is about 16 µs, about two thirds of a request with no database work. ASGI vs RSGI
  differs by about 1.5%: the protocol is not the lever.
* In process the same path costs 12.7 µs of event-loop CPU (`profile_request.py`, `health`).
* For a database request (`detail`) the adapter's 13 µs is about 13% of ~90 µs of event-loop CPU
  and 5% of the whole request once the ~260 µs database wait is counted.
* Earlier measured Rust moves (routing, JSON decode, percent-decoding fused into lookup) gave
  nothing end to end because each crossing costs about as much as the work (`benchmarks/README.md`).
* cProfile shares of the event-loop thread on `health` (inflated, locates only): `_http` 11.8%,
  `build_request` 3.9%, `_call` 3.4%, `os.urandom` (request id from `uuid4().hex`) 2.8%,
  `core.header_dict` 2.8%, `_serve` 2.3%, `Request.__init__` 2.2%, `_respond` 2.1%,
  `_dispatch_request` 2.0%; asyncio 10.8% (timeout context, contextvars, loop).

## What `_http` does per request that does not depend on the request

* builds `security` policy headers, lower-cases the response header names into a set, re-encodes
  `request_id_header` and iterates the headers twice, every request;
* `uuid.uuid4().hex` (a syscall) when no trusted inbound id;
* three `ContextVar.set`/`reset` pairs and `asyncio.timeout(...)` even when the route needs none.

## Recommendation: stages, each measured before the next

1. **Stage 1, Python, no new API.** Precompute per app: the static security/CORS header tail per
   scheme, the encoded request-id header name, the "no extras" fast path; generate the request id
   from a per-process counter plus random prefix (still unique, no syscall) if the owner accepts
   the change in id format (decision needed); skip `asyncio.timeout` when the route has no timeout
   and merge the context-variable writes. Tests: existing ASGI/response-header/CORS/security tests
   plus new tests for header order and ids. Measure with `benchmarks/protectedgetbench.py`,
   `profile_request.py --scenario health` and `server_floor.py --workers 1`. Expected: part of the
   16 µs; to be measured, not assumed.
2. **Stage 2, Rust prepared plan, only for what survives stage 1.** A per-route immutable plan
   built at startup (route, parameter converters, body schema, header policy) so one native call
   does match + parse + validate + build the response head. The plan must replace several current
   crossings with one, otherwise it repeats the rejected experiments. Gate: stage-1 profile still
   shows a Python adapter share worth more than the measured crossing cost (a Python to Rust call
   is about 0.3 µs; `header_dict` plus `build_request` plus `Request.__init__` are the candidates).
3. **Stage 3, asyncio-facing pieces stay Python.** Cancellation, `asyncio.timeout`, lifespan and the
   executor are not moved: cancellation and the event loop are Python semantics; a native version
   adds risk and no measured gain. Revisit only with a measured reason.

## Not recommended

* RSGI or a custom Rust HTTP server for now: protocol gain about 1.5% with the same Granian;
  replacing the server loses Granian's lifespan/worker handling that the runtime relies on.
* Porting `_http` line by line: the crossings and Python objects stay.

## Failure and edge cases to keep (tests exist or must be added)

Client disconnect and cancellation mid-request, timeout 504 vs handler `TimeoutError`, saturated
executor 503, not-ready 503, HEAD without body, request-id trust rules, route response headers
vs policy headers, CORS preflight, HTTPS redirect, size limits, error translation order
(rollback before HTTP error).

## Verification

`protectedgetbench.py` five alternating before/after runs (15% detector), `server_floor.py
--workers 1` and `profile_request.py` health on the same host session, full pytest with
PostgreSQL, Granian runtime tests on Linux CI. Independent read-only review required
(concurrency and cancellation).

## Owner decisions needed

1. Request id format may change from `uuid4().hex` (32 hex) to another unique token of the same
   length? (Recommended: same length and charset, random per-process prefix plus counter, so
   external consumers parse it unchanged.)
2. Own branch and PR separate from `perf/request-profile`? (Recommended: yes, after that PR.)

## Stage 2 decision (2026-11-15, after stage 1 merged as f2dbdeb): not worth it

Measured on main `f2dbdeb`, macOS M4 Pro, release extension, PostgreSQL 17 container, load average
6-7 at the time (noisy host; absolute numbers only for proportions):

* `profile_request.py --scenario health` (5 blocks): 11.5 us per request in process
  (89 396 req/s with 64 in flight). cProfile split: Hinoki Python 51 pct, Python builtins 18,
  asyncio 12, other libraries 8, Rust calls 7, application 3.
* `server_floor.py --workers 1` (3 runs): minimal ASGI 8.8 us, minimal RSGI 8.6 us, Hinoki
  `/health` 20.9 us per request (47 896 req/s). Hinoki layer about 12 us.
* Candidates stage 2 could fold into one native call: `header_dict` 3.6 pct, `build_request`
  4.3, `Request.__init__` 2.4 = about 10 pct of 11.5 us = about 1.2 us (cProfile inflates
  Python costs). The Rust crossings in total are 7 pct (about 0.8 us), so replacing three of
  them by one saves about 0.5 us at most, because the Python `Request` object still has to be
  built. That is about 2 pct of the served request (20.9 us), below the 5 pct detector.
* The rest of the Python share is `_http`, `_call`, `_serve`, `_respond`, dispatch and asyncio:
  stage 3 territory (cancellation, timeout, executor), which stays Python by design.

Decision: do not build stage 2. The adapter work is closed at stage 1. Reopen only if a handler
path with a larger fixed Python cost appears in a profile.
