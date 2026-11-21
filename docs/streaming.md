---
status: implemented
related:
  - endpoints.md
  - kiso.md
  - serving.md
  - middleware.md
  - testing.md
---

# Stream a response

An action can answer with a body that is sent while it is produced: `Stream` for
bytes or text, `EventStream` for server-sent events and `File` for a file on
disk. The handler returns the object; the status, the headers and the source of
the body are fixed at that point, so access, validation, rate limits, errors and
the request timeout all happen before the first byte.

```python
# notes/exports.py
from collections.abc import AsyncIterator

from hinoki import http
from hinoki.http import Stream


@http.get("/export.csv")
async def export() -> Stream:
    async def rows() -> AsyncIterator[str]:
        yield "id,title\n"
        for note_id in range(1, 4):
            yield f"{note_id},note {note_id}\n"

    return Stream(
        rows(),
        content_type="text/csv; charset=utf-8",
        headers={"content-disposition": 'attachment; filename="notes.csv"'},
    )
```

The return annotation must name the class: `-> Stream`, `-> EventStream` or
`-> File`, alone or in a union (`-> Stream | NoteOut`). That is how Hinoki knows
at startup which actions stream. An action that returns one of them without
saying so answers `500`, and the stream is closed.

## `Stream`

`Stream(body, *, status=200, headers=(), content_type=None, length=None)`.

* `body` is an async iterable or a plain iterable of `bytes` or `str` (a `str`
  is sent as UTF-8). A plain iterable, including a sync generator, is advanced
  on the bounded sync executor, so a handler declared with `def` can stream.
* The default `content-type` is `application/octet-stream`.
* Without `length` the body is sent with chunked framing on HTTP/1.1. With
  `length` the answer carries that `content-length`, and a body that does not
  match it is cut off instead of ending as complete, on Kiso and under an ASGI
  server alike: a body that is too long is cut at the length, and one that is too
  short is aborted.
* A piece that is neither `bytes` nor `str` ends the stream like an exception.
* An async generator cannot be the handler itself: write
  `return Stream(rows())` instead. Hinoki refuses the declaration when the
  application is created.

## `EventStream` and `Event`

```python
# notes/events.py
from collections.abc import AsyncIterator

from hinoki import Request, http
from hinoki.http import Event, EventStream


@http.get("/notes/events")
async def note_events(request: Request) -> EventStream:
    after = request.headers.get("last-event-id")

    async def events() -> AsyncIterator[Event]:
        yield Event({"after": after}, event="hello", id="1")
        yield Event("done")

    return EventStream(events(), retry=3000)
```

`EventStream(events, *, status=200, headers=(), keepalive=..., retry=None)`
takes an iterable of `Event` or `str` (a `str` is the `data` of one event). The
answer has `content-type: text/event-stream; charset=utf-8`,
`cache-control: no-store` and `x-accel-buffering: no`. `retry` is the
reconnection delay in milliseconds, sent before the first event.

`Event(data=None, *, event=None, id=None, retry=None, comment=None)`:

* `data` that is not a `str` (a `Schema`, a `dict`, a list or a scalar) is sent
  as JSON; a line break in a `str` becomes one `data:` line per line.
* `event` and `id` that contain CR, LF or NUL raise `ValueError` when the
  `Event` is created, so a value from a client cannot add a field.
* `retry` is a non-negative number of milliseconds.

While the source is quiet, a comment (`: `) is sent every `keepalive` seconds so
proxies do not close the connection; the default is `execution.sse_keepalive`
(15 seconds) and `keepalive=None` turns it off. A client that reconnects sends
`Last-Event-ID`; read it from `request.headers` as above.

## `File`

```python
# notes/downloads.py
from pathlib import Path

from hinoki import http
from hinoki.http import File

REPORTS = Path("/srv/notes/reports")


@http.get("/reports/q3.pdf")
async def report() -> File:
    return File("q3.pdf", root=REPORTS, filename="q3-report.pdf")
```

`File(path, *, root=None, filename=None, content_type=None, status=200,
headers=())`.

* A relative `path` is taken from `root`. With `root`, the file must be inside
  it once symbolic links are resolved; a missing file, a directory or a path
  that leaves `root` answers `404`. A symbolic link inside `root` that points
  outside it is refused the same way.
* `filename` adds `content-disposition: attachment`, with an ASCII fallback and
  the exact UTF-8 name when they differ. A name with `/` or a control character
  raises `ValueError`.
* The `content-type` is guessed from the name unless you give it, and the answer
  has the file size as `content-length` and `accept-ranges: none`. When the
  server does not send the file itself, no more than that size is read, so a
  file that grows meanwhile is cut at the size the head declared. Range
  requests are not supported.
* Resolve and check the path from trusted data; a `root` is what keeps a
  client-supplied name inside a directory.

## Registration rules

Hinoki refuses these when the application is created (`RegistrationError`
naming the action):

| Declaration | Why |
| --- | --- |
| `atomic=True` on an action that streams | The transaction would stay open for the whole body |
| Status `204` or `304` with a stream | These answers have no body |
| An `async def` or `def` generator as the handler | Return `Stream(...)` or `EventStream(...)` built from it |

`WSGIAdapter` refuses to start an application that has such an action
([serving](serving.md#wsgi)); the application's [MCP endpoint](app-mcp.md)
refuses to expose it; the [OpenAPI document](openapi.md) describes `Stream` and
`File` as `application/octet-stream` (`string`, `binary`) and `EventStream` as
`text/event-stream` (`string`); the arms of a union that are not streamed are
documented next to them.

## Errors and what the client sees

| Situation | Result |
| --- | --- |
| Access, rate limit, validation, `404` or an exception before the handler returns | The normal JSON error; nothing was sent yet |
| `execution.request_timeout` expires before the handler returns | `504 timeout` |
| `execution.max_streams` streams already open on the worker | `503` with `Retry-After: 1`; the handler does not run |
| The source raises after the head | The error is logged by `hinoki.runtime` with the request id and the response is cut: the client never sees it end as complete |
| A `Stream` is silent for `execution.stream_idle_timeout` | The response is cut and the source is closed |
| The client goes away | The source is closed (`aclose()` of an async iterator, `close()` of a plain one). A plain iterator whose `next()` is blocked on a worker thread cannot be interrupted: the stream keeps its `max_streams` place until that call returns, and the source is closed then |
| Server shutdown | Open event streams end normally; other streams are cut when the shutdown timeout ends |

`stream_idle_timeout` defaults to `request_timeout`; it applies to `Stream`
only, because an event stream is covered by the keep-alive. The status of the
head is the one the access log records, even if the body is cut later.

`max_streams` counts the streams in a worker (default `64`) from the time the
handler is about to run until the body ends. A `File` on Kiso is sent by the
server alone, so it holds its place only until the head is decided.

A `HEAD` request to a `GET` action that streams gets the headers only, and the
source is closed without being read.

## Middleware

`call_next()` returns the `Stream`, `EventStream` or `File` itself, with an
empty `body`; a middleware never sees the pieces. `response.with_header(...)`
keeps the class and the source. A middleware that returns a different
`Response` instead of the stream does not serve it: Hinoki closes its source.

An action that streams is always served by Python and never takes
[the native lane](kiso.md#what-is-native-and-what-still-runs-in-python):
`hinoki routes --lanes` shows `lane_reason` `streamed response`.

## What runs where

Rust does what is a computation or a timer on its own; Python does what has to
call the application's code or live in the event loop.

* **Kiso.** The application's iterator runs in Python and every piece it
  produces crosses to Rust as a Python `bytes`. From there, framing, the bounded
  queue (a slow client slows the iterator down), the idle timeout, the
  keep-alive comments, the declared-length check and the disconnect check run in
  the server. SSE framing is done by `core.sse_frame`. A `File` is opened and
  sent by the server: the bytes of the file never enter Python.
* **Granian (ASGI).** Each piece is an ASGI `http.response.body` message built
  in Python, so this path has no native part for the body. The idle timeout, the
  keep-alive and the disconnect watcher run in Python, and a client that leaves
  is noticed through `receive()`. A `File` is sent with the ASGI
  `http.response.pathsend` extension when the server offers it, and read in
  chunks on the sync executor otherwise.
* **Both paths, in Rust.** The count behind `execution.max_streams`
  (`core.StreamLimit`: an atomic counter whose places give themselves back when
  released or dropped), the file lookup of a `File` (`core.locate_file`: real
  path, `root` check, regular file and size, on a blocking thread and not in the
  sync executor; at most 32 lookups run at once in the process and one more
  answers `503` with `Retry-After: 1` instead of queueing), the declared-length arithmetic of the ASGI pump
  (`core.Tally`), the `content-disposition` value (`core.content_disposition`)
  and the SSE frames. A WSGI stream's idle timeout is the connection's own timer,
  as on Kiso.

Neither path is called native: the application's iterator is Python on both.

What stays in Python, and why:

* **The source.** The iterator is user code, and a plain one has to run on a
  worker thread; Rust cannot call it from a runtime thread without moving a
  Python object there.
* **Taking the place and the `503`.** The place is taken in the pipeline right
  before the handler runs, because the answer to a full worker is a normal
  Hinoki error (`Retry-After: 1`, the application's error format, middleware)
  that is built in Python. The count itself is Rust.
* **The ASGI timers and the watcher.** The idle timeout and the keep-alive
  wrap an `await` on the application's iterator and the watcher is a task on
  `receive()`; both are asyncio objects of the server's loop. A Rust state
  machine would only compute a deadline that Python must still schedule, so
  they stay as they are.
* **Registration rules, the annotation check, middleware and `HEAD`.** They
  inspect Python annotations and `Response` objects.
* **The `content-type` of a `File`.** It is guessed from the name with the
  standard library's `mimetypes`; Hinoki does not carry a second table.

### Measured behavior

These were checked with a running server (see `tests/kisostreamingtest.py` and
`tests/runtime/granianstreamtest.py`):

* Pieces reach the client before the body ends, on Kiso and on Granian.
* On Kiso, a client that does not read stops the producer: after a pause the
  number of produced pieces stops growing, bounded by the queue of 8 pieces and
  the socket buffers.
* On Kiso, a client that leaves a stream that keeps writing closes the source at
  once. A client that leaves a stream that is **silent** is noticed at the next
  write: the next keep-alive of an event stream, the idle timeout of a `Stream`,
  or the next piece. A silent `Stream` with a long idle timeout keeps its
  iterator, its `max_streams` place and its source open until then.
* On Granian, a client that leaves an endless stream closes the source, and an
  event stream carries keep-alive comments.
* Closing the server with an open event stream ends the stream normally on Kiso.
  This was not measured on Granian.

Compression, `Range` requests and WebSockets are not provided.

## Test a stream

`ASGIClient.stream()` gives the head and the pieces as they are sent, and leaving
the block simulates a disconnect:

```python
async with ASGIClient(app) as client:
    async with client.stream("GET", "/notes/events") as reply:
        first = await anext(reply.chunks())
```

`client.get()` still collects the whole body; on a stream that never ends it
stops with `BodyTooLarge` after `max_bytes` (10 MiB by default). See
[testing](testing.md#test-streams).

## See also

* [Endpoints](endpoints.md)
* [Kiso server](kiso.md)
* [Middleware](middleware.md)
* [Settings](settings.md)
