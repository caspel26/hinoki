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
from hinoki import Hinoki, Schema, http
from hinoki.settings import AppSettings, MetricsSettings


class Note(Schema):
    id: int


@http.get("/notes/{note_id}")
async def note(note_id: int) -> Note:
    return Note(id=note_id)


app = Hinoki(
    settings=AppSettings(
        allowed_hosts=("localhost", "127.0.0.1"),
        metrics=MetricsSettings(listen="127.0.0.1:9100"),
    ),
    include=[note],
)
```

Run `hinoki serve notes.main:app --server kiso --port 8765`, request `/notes/1`, `/notes/2` and `/nope`, then scrape `http://127.0.0.1:9100/metrics`. The lines for those requests (comments and the other families left out) are:

```text
hinoki_http_requests_total{route="/notes/{note_id}",method="GET",status="2xx",worker="0"} 2
hinoki_http_requests_total{route="",method="GET",status="4xx",worker="0"} 1
hinoki_http_requests_in_flight{worker="0"} 0
hinoki_http_response_bytes_total{route="/notes/{note_id}",worker="0"} 16
hinoki_http_response_bytes_total{route="",worker="0"} 69
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
| `hinoki_events_total` | counter | `event`, `phase`, `outcome` | [Event handler](events.md#metrics) runs. `outcome` is `ok`, `error` or `timeout` |
| `hinoki_event_handler_duration_seconds` | histogram | `event`, `phase` | Time a handler ran, whatever its outcome |
| `hinoki_email_messages_total` | counter | `backend`, `outcome` | [Email](email.md) sends. `outcome` is `sent` or the error kind (`invalid_address`, `invalid_header`, `invalid_message`, `limit_exceeded`, `closed`, `busy`, `connect`, `timeout`, `tls`, `auth`, `rejected`, `temporary`, `protocol`) |
| `hinoki_email_send_duration_seconds` | histogram | `backend` | Time of a `send`, whatever its outcome |
| `hinoki_email_retries_total` | counter | `backend` | Delivery attempts made again after a transient failure |
| `hinoki_build_info` | gauge | `version`, `server` | Always `1` |
| `hinoki_worker_start_time_seconds` | gauge | none | Start of the worker, in Unix seconds |
| `hinoki_metrics_snapshot_errors_total` | counter | none | Snapshots of other workers that were unreadable, too large or not valid UTF-8 |

* `route` is the route's pattern (`/notes/{note_id}`), never the path of the request, so the number of series is bounded by the number of routes. An answer no route produced (an unknown path, a refused host, the documentation pages) has `route=""`.
* `method` is `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `QUERY` or `OTHER`.
* The duration is measured when the answer's head is ready, as the access log does. The time a stream takes to send its body is not in it, and a request is *in flight* only until the backend has answered, not until a stream ends.
* Bytes are counted as the pieces are handed to the connection: a stream's pieces one by one, a `File` as it is read, a compressed body at its compressed size. A `HEAD` counts none. The exposition itself is never compressed and never counted.

The duration buckets are `0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10` seconds. Set `metrics.buckets` to change them.

## Event handlers

The two event families exist only when the application registers [event handlers](events.md). Their series are created at startup, one per registered (event, phase) pair (`Created[Note]`, `after_commit`), in a fixed table in Rust; Python records each run with one call that adds to a counter and a histogram bucket, so nothing grows with traffic and no label comes from a payload. The histogram uses the same buckets as the request duration. A series with no run yet is not printed. Handler runs of events emitted inside a request, a startup hook or a command are all counted by the worker that ran them, under the same `worker` label; the table is built from the handlers of the Hinoki application served by the ASGI path of Kiso; Kiso's WSGI mode records none.

Example from a real run (three emits of an event with an `ok`, a failing and a timed-out handler, `events.handler_timeout=0.05`):

```text
hinoki_events_total{event="NoteSaved",phase="after_commit",outcome="ok",worker="0"} 3
hinoki_events_total{event="NoteSaved",phase="after_commit",outcome="error",worker="0"} 3
hinoki_events_total{event="NoteSaved",phase="after_commit",outcome="timeout",worker="0"} 3
hinoki_event_handler_duration_seconds_bucket{event="NoteSaved",phase="after_commit",le="0.05",worker="0"} 6
hinoki_event_handler_duration_seconds_bucket{event="NoteSaved",phase="after_commit",le="0.1",worker="0"} 9
hinoki_event_handler_duration_seconds_sum{event="NoteSaved",phase="after_commit",worker="0"} 0.153349
hinoki_event_handler_duration_seconds_count{event="NoteSaved",phase="after_commit",worker="0"} 9
```

Measured cost of recording on one Apple-silicon laptop (an `after_commit` handler that returns at once, 200,000 emits per sample, 15 interleaved samples per variant, medians over three runs): about 3.3 µs per emit with metrics off and 3.4 to 3.5 µs with them on, that is roughly 50 to 110 ns (about 2 %) more; the extension was a debug build. A handler that does real work makes the share smaller.

## Email

The three email families exist only when the application has an [email module](email.md) (an `email: settings.EmailSettings` field). The `backend` label is `smtp`, `console` or `memory` and every `outcome` series is created at startup, so no recipient, subject, host or error text becomes a label; a series with no send yet is not printed, except the retries counter. The send duration uses the same buckets as the request duration. Example from a real run of one send with the `memory` backend:

```text
hinoki_email_messages_total{backend="memory",outcome="sent",worker="0"} 1
hinoki_email_send_duration_seconds_count{backend="memory",worker="0"} 1
hinoki_email_retries_total{backend="memory",worker="0"} 0
```

Without metrics, or under Granian, `Mailer.stats()` still counts sent and failed messages, retries and connections per worker.

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

With one worker nothing is written to disk when `server.sighup` is `"stop"`: the worker binds `listen` itself and answers from memory. Otherwise (the default) the supervisor owns the listener and the directory even for one worker, because a [reload on `SIGHUP`](kiso.md#reload-on-sighup) runs the old and the new worker side by side. A replacement worker takes the next free `worker` number (a reload of `worker="0"` continues as `worker="1"`), starts its counters at zero, and the snapshot of the worker it replaces is removed when that one has stopped. Under `hinoki dev` the supervisor owns the listener, so it stays open while the worker reloads, and the counters restart with the worker.

## What it does not cover

A worker's snapshot is limited to 4 MiB, which is roughly 2000 active route and method pairs with the default buckets and roughly 800 with 32 buckets (approximate figures). A worker whose snapshot would be larger keeps its previous file, logs the problem once to stderr, and its series leave the other workers' scrapes once that file is older than five intervals; its own scrape still shows live numbers.

Pool and executor gauges, process metrics (`process_*`) and tracing are not exported. Metrics are not available under Granian or another ASGI server; wrap the ASGI application there as described in [serving](serving.md#metrics-and-tracing).
