---
status: implemented
related:
  - streaming.md
  - kiso.md
  - serving.md
  - settings.md
---

# Compress responses

[Kiso](kiso.md), Hinoki's Rust server, can compress the bodies it sends with brotli or gzip. It is off by default and it is **Kiso only**: Granian and any other ASGI server never compress what Hinoki gives them, and `hinoki serve --server granian` refuses `compression.enabled` instead of ignoring it.

```python
# notes/main.py
from hinoki import Hinoki
from hinoki.settings import AppSettings, CompressionSettings

app = Hinoki(
    settings=AppSettings(
        allowed_hosts=("localhost", "127.0.0.1"),
        compression=CompressionSettings(enabled=True),
    )
)
```

With a JSON action that answers about 6.5 KB, `hinoki serve main:app` and `curl` show what the client asked for:

```text
$ curl -s -D - -o /dev/null -H "accept-encoding: br" http://127.0.0.1:8000/notes
HTTP/1.1 200 OK
content-type: application/json
content-length: 429
vary: accept-encoding
content-encoding: br
```

`gzip` gives `content-encoding: gzip` with `content-length: 991` for the same body, and a request without `accept-encoding` gets the 6580 bytes unchanged. `curl --compressed` decodes the answer. These figures were produced by that example; they describe this body, not a general ratio.

## What is compressed

Kiso decides once per answer, after the application has decided it, so a plain action, `Stream`, `EventStream` and `File` are treated by the same rules and HTTP/1.1 and HTTP/2 behave alike.

An answer is compressed when all of these hold:

* The request sends an `accept-encoding` that allows `br` or `gzip` (quality values and `*` are honoured, `q=0` refuses a coding, and a malformed entry is ignored). With several acceptable codings the order of `compression.encodings` breaks ties. A request with no `accept-encoding` gets the body unchanged.
* The status can carry a body: `1xx`, `204`, `206`, `304` and an answer with `content-range` are left alone.
* The answer has no `content-encoding` already and its `cache-control` does not contain `no-transform`: that directive is the way for an action to opt out.
* Its `content-type` matches `compression.types`. The default is `text/*`, `application/json`, `*/*+json`, `application/xml`, `*/*+xml`, `application/javascript`, `application/x-ndjson` and `image/svg+xml`. A response without a `content-type` is not compressed.
* A body known to be smaller than `compression.min_size` (1024 bytes) is left alone. A streamed body whose size is not known is compressed.
* `text/event-stream` only with `compression.event_streams = True`, and a `File` only while `compression.files` is `True` (the default).

When Kiso considers an answer, it adds `Vary: accept-encoding`, even if the client asked for no coding, so a cache keeps the variants apart. A strong `etag` is weakened (`W/"..."`) on a compressed answer.

## How the body is sent

* A body of at most 64 KiB held in memory is compressed at once, and its `content-length` becomes the compressed size. If the result would not be smaller, the original goes out.
* A larger body, a `Stream`, an `EventStream` and a `File` are compressed as they are read, 64 KiB at a time. They have no `content-length`: HTTP/1.1 uses `transfer-encoding: chunked` and HTTP/2 uses data frames. A `File` is still read by the server, so its bytes do not enter Python.
* When the producer of a stream pauses, Kiso flushes what it has compressed so far, so a client receives each piece when the producer yields it and not at the end. An event stream's keep-alive comments are flushed too.
* A stream that fails after the head is cut, as without compression: the compressed body is never ended as if it were complete.
* `HEAD` receives the head the matching `GET` would get, with `content-encoding` and `vary` and without `content-length`.

## Limits

Compression runs on Kiso's runtime threads, without spawning blocking tasks, one piece of at most 64 KiB per step, and an encoder gives the thread back after four steps. Two settings bound the work:

* `compression.max_concurrent` (64) is the number of compressions in progress in one worker. An answer that arrives when all are taken is sent uncompressed at once; it never waits. The count of answers sent that way is kept by the server (`Compression::busy` in the Rust crate); no setting or log line reports it yet.
* `gzip_level` (1 to 9, default 5) and `brotli_quality` (0 to 11, default 4) trade CPU for size. The brotli window is fixed at 2^18 bytes.

Measured while this was built, on an Apple M4 Pro with 4 MiB of generated JSON flushed every 64 KiB, with the `flate2` (pure Rust backend) and `brotli` crates: gzip level 5 took about 0.54 ms per 64 KiB and held about 0.36 MB per encoder; brotli quality 4 with that window took about 0.43 ms per 64 KiB and held about 2.1 MB per encoder. Brotli quality 11 took about 48 ms per 64 KiB, which is why it is not the default. These numbers come from a synthetic sample on one machine, are not a throughput claim, and your content and hardware will differ; the memory figure is why the default limit is 64 and not larger.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `compression.enabled` | `false` | Turns compression on (Kiso only) |
| `compression.encodings` | `("br", "gzip")` | Codings offered, in order of preference; each at most once |
| `compression.min_size` | `1024` | Bodies known to be smaller are left alone; `0` to 16 MiB |
| `compression.types` | see above | `type/subtype`, `type/*` or `*/*+suffix` |
| `compression.event_streams` | `false` | Also compress `text/event-stream` |
| `compression.files` | `true` | Also compress a `File` |
| `compression.gzip_level` | `5` | `1` to `9` |
| `compression.brotli_quality` | `4` | `0` to `11` |
| `compression.max_concurrent` | `64` | `1` to `65536` compressions at once per worker |

A value outside its range stops the application at startup with the usual settings error. The settings are declared and validated in Python; the negotiation, the rules above and the encoding run in Rust.

## What stays in Python

Only the declaration and validation of `CompressionSettings`, the refusal under Granian, and passing the values to the server at start-up. No Python object is held by the compression path, and no compression runs on the asyncio loop.

## Not provided

* Compression on Granian or another ASGI server, in `ASGIClient` and in the test client: tests see the body the action produced.
* Other codings (`deflate`, `zstd`), and precompressed files kept next to a `File`.
* A per-route option other than `cache-control: no-transform`.
* Compression of request bodies.
* A compression ratio, throughput or latency figure beyond the measurement above.
* A test of a WSGI callable hosted on Kiso with compression on: the same policy is passed to that server, but it has not been exercised.
