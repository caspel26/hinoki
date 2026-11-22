---
status: implemented
related:
  - kiso.md
  - serving.md
  - settings.md
  - compression.md
---

# Export Prometheus metrics

[Kiso](kiso.md), Hinoki's Rust server, can count the requests it answers and serve the counts in the Prometheus text exposition format. It is off by default and it is **Kiso only**: `hinoki serve --server granian` refuses `metrics.listen` instead of ignoring it. The counters live in Rust and cost a few atomic additions per request; the exposition is served by a dedicated listener, never by the application's own port.

```python
# notes/main.py
from hinoki import Hinoki, http
from hinoki.settings import AppSettings, MetricsSettings

app = Hinoki(
    settings=AppSettings(
        allowed_hosts=("localhost", "127.0.0.1"),
        metrics=MetricsSettings(listen="127.0.0.1:9100"),
    )
)


@http.get("/notes/{note_id}")
async def note(note_id: int) -> dict[str, int]:
    return {"id": note_id}


app.include(note)
```

Run `hinoki serve notes.main:app --server kiso --port 8765`, request `/notes/1`, `/notes/2` and `/nope`, then scrape `http://127.0.0.1:9100/metrics`. The lines for those requests (comments and the other families left out) are:

```text
hinoki_http_requests_total{route="/notes/{note_id}",method="GET",status="2xx",worker="0"} 2
hinoki_http_requests_total{route="",method="GET",status="4xx",worker="0"} 1
hinoki_http_response_bytes_total{route="/notes/{note_id}",worker="0"} 16
hinoki_http_requests_in_flight{worker="0"} 0
```

The application's own port answers `/metrics` like any other unknown path (`404`).

## What is exported

Every sample carries a `worker` label: the number of the process that counted it.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `hinoki_http_requests_total` | counter | `route`, `method`, `status` | Requests answered. `status` is the class: `1xx` to `5xx` |
| `hinoki_http_request_duration_seconds` | histogram | `route`, `method` | Time from the request head to the answer's head |
| `hinoki_http_requests_in_flight` | gauge | none | Requests the backend has not answered yet |
| `hinoki_http_response_bytes_total` | counter | `route` | Body bytes handed to the connection, after compression |
| `hinoki_http_errors_total` | counter | `route`, `kind` | `invalid_response`: the backend's answer could not be sent as given and became a `500`. `stream_aborted`: a stream failed after its head |
| `hinoki_build_info` | gauge | `version`, `server` | Always `1` |
| `hinoki_worker_start_time_seconds` | gauge | none | Start of the worker, in Unix seconds |
| `hinoki_metrics_snapshot_errors_total` | counter | none | Snapshots of other workers that were unreadable, too large or not valid UTF-8 |

* `route` is the route's pattern (`/notes/{note_id}`), never the path of the request, so the number of series is bounded by the number of routes. An answer no route produced (an unknown path, a refused host, the documentation pages) has `route=""`.
* `method` is `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `QUERY` or `OTHER`.
* The duration is measured when the answer's head is ready, as the access log does. The time a stream takes to send its body is not in it, and a request is *in flight* only until the backend has answered, not until a stream ends.
* Bytes are counted as the pieces are handed to the connection: a stream's pieces one by one, a `File` as it is read, a compressed body at its compressed size. A `HEAD` counts none. The exposition itself is never compressed and never counted.

The duration buckets are `0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10` seconds. Set `metrics.buckets` to change them.

## Settings

| Field | Default | Meaning |
| --- | --- | --- |
| `listen` | unset | `host:port` of the metrics listener (an IPv6 host in brackets). Unset keeps metrics off |
| `path` | `/metrics` | The only path the listener serves |
| `allow` | empty | Addresses or CIDRs of the peers that may scrape (the TCP peer, not `X-Forwarded-For`); empty allows every peer |
| `token` | unset | A `Secret` bearer token a scrape must send as `Authorization: Bearer ...` |
| `buckets` | see above | 1 to 32 positive, finite, strictly increasing upper bounds in seconds |
| `snapshot_interval` | `1.0` | `0.1` to `60` seconds; how often each worker writes its snapshot when there are several |

A `listen` address off the loopback needs `allow` or `token`, so the metrics are never open to everyone by accident. The listener answers `GET` and `HEAD` only (`405` otherwise), `404` for another path, `403` for a peer outside `allow` and `401` with `www-authenticate: Bearer` for a missing or wrong token (compared in constant time). It takes at most 64 connections, waits 5 seconds for a request head and reads at most 8 KiB of headers. [`Access`](authentication.md) policies do not apply to it: it does not pass through application code.

## Several workers

`hinoki serve --workers N` opens the metrics listener once, like the main one, and every worker accepts scrapes on it. Counters are not added up: each worker writes a snapshot of its own numbers to a private directory (created by the supervisor, readable only by its user, removed when it exits) every `snapshot_interval`, and the worker that receives a scrape answers with its live numbers and the fresh snapshots of the others. Each process keeps its own series, told apart by `worker`, so Prometheus can `sum without (worker)` them and handles a restarted worker's counter reset as usual. A peer's numbers are at most one interval old (and a snapshot older than five intervals, from a worker that is gone, is ignored); the answering worker's own are live. A snapshot that is unreadable, too large or not valid UTF-8 is skipped and counted in `hinoki_metrics_snapshot_errors_total`.

With one worker nothing is written to disk: the worker binds `listen` itself and answers from memory. Under `hinoki dev` the supervisor owns the listener, so it stays open while the worker reloads, and the counters restart with the worker.

## What it does not cover

A worker's snapshot is limited to 4 MiB, which is roughly 2000 active route and method pairs with the default buckets and roughly 800 with 32 buckets (approximate figures). A worker whose snapshot would be larger keeps its previous file, logs the problem once to stderr, and its series leave the other workers' scrapes once that file is older than five intervals; its own scrape still shows live numbers.

Pool and executor gauges, process metrics (`process_*`) and tracing are not exported. Metrics are not available under Granian or another ASGI server; wrap the ASGI application there as described in [serving](serving.md#metrics-and-tracing).
