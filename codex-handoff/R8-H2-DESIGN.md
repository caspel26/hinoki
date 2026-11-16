# R8: HTTP/2 on the Rust server (design)

Local design (gitignored by `docs/*-DESIGN.md`). Written before any code.

## 1. Decision

- New setting `server.http2: bool = False`. Off by default: HTTP/1.1 behaviour
  must not change for anyone who does not opt in.
- HTTP/2 is **TLS only (ALPN `h2`)**. No h2c (neither prior knowledge nor
  `Upgrade: h2c`: R9 refuses Upgrade). `http2=True` without TLS files is a
  settings error, on both servers.
- When on, ALPN offers `["h2", "http/1.1"]`; the negotiated protocol picks the
  connection driver in `accept.rs::run_connection`. When off, ALPN stays
  `["http/1.1"]` as today.
- Granian: `granian_tls` uses `HTTPModes.auto` instead of `http1` when `http2`
  is on (still `ssl_protocol_min=tls12`).

## 2. Rust changes (`crates/server`)

- `hyper` gains `http2`; `hyper-util` gains `server-auto` is NOT needed: dispatch
  is manual on ALPN, using `hyper::server::conn::http2::Builder` with
  `hyper_util::rt::TokioExecutor` and `TokioTimer`.
- `tls.rs::acceptor(cert, key, http2)` sets `alpn_protocols` accordingly.
- `accept.rs`: `serve_stream` is split: `serve_h1` (today's code, unchanged) and
  `serve_h2`. `run_connection` reads `ServerConnection::alpn_protocol()` of the
  accepted `TlsStream` and calls one of them. A TLS connection with no ALPN
  result is HTTP/1.1.
- `serve_h2` does NOT wrap the stream in `Guarded`/`Framing` (HTTP/1 only:
  smuggling and head-size guards of the h1 text parser). Write stall protection
  is still needed: wrap the stream in a write-timeout-only adapter. If
  `Guarded` cannot be reused without `Framing`, add a small `StallGuard` that
  shares the write-stall timer code (extract, do not copy).
- Builder settings, all explicit (not hyper defaults):
  `max_concurrent_streams(100)`, `max_header_list_size(max_header_bytes)`,
  `initial_stream_window_size(65_535)`, `initial_connection_window_size(65_535 * 4)`,
  `adaptive_window(false)`, `max_frame_size(16_384)`,
  `max_send_buf_size(256 KiB)`, `keep_alive_interval(30 s)` +
  `keep_alive_timeout(20 s)`, `max_pending_accept_reset_streams(20)`,
  `max_local_error_reset_streams(...)` (the h2 mitigation for rapid reset,
  CVE-2023-44487 and the CONTINUATION/reset-flood family). Verify the h2 crate
  version in `Cargo.lock` contains the mitigations (h2 >= 0.4.x with
  `max_pending_accept_reset_streams`); record the version in the PR.
- Idle connections and `header_read_timeout`: hyper h2 has no header timeout;
  the keep-alive ping bounds idle/dead peers. A connection that opens no stream
  is closed by `header_read_timeout` (wrap the h2 handshake and first-stream
  wait: if no stream arrives within it, graceful close).
- Shutdown: on stop call `graceful_shutdown()` (GOAWAY), then await; the
  existing `drain` abort after `shutdown_timeout` still bounds it.
- `conn.rs`: `Transport.signals` (close/linger) are HTTP/1 notions. For h2,
  `respond` is called with a `Transport` whose signals are never read; the
  `connection: close` header MUST NOT be sent on h2 (forbidden connection-specific
  header, hyper would drop or reject it): skip that block when the request
  version is `HTTP_2`.
  `refusal_of`: for h2 there is no Host rule; `:authority` (parts.uri.authority())
  becomes `host`, if both `host` header and `:authority` exist and differ,
  `:authority` wins (RFC 9113 8.3.1). `Incoming` gains `http_version: &'static str`.
- Request bodies and trailers go through `BodyReader` as today (it already works
  on `Incoming` body frames). No 100-continue concern in h2.
- Response: `Full<Bytes>` as today; `HEAD` keeps `content-length`.

## 3. Python/bindings

- `rustserver.py:95` scope `http_version` is `"1.1"` constant: take it from the
  request (`"2"` for h2) through the existing request tuple/`Incoming` field.
- `ServerSettings.http2: bool`, validation: requires both TLS files
  ("http2 needs TLS: set tls_cert_file and tls_key_file"). `conflicts()` untouched
  (works on both servers).
- `RustServer.start` receives the flag and passes it to `tls::acceptor`.

## 4. Docs

`docs/serving.md` (replace "HTTP/1.1 only"), settings page / `AppSettings` docs,
README line about HTTP/1.1, ROADMAP row, reference page for `ServerSettings`.
State plainly: TLS only, no h2c, no server push, no WebSocket over h2.

## 5. Tests (must be real, not mocks)

Rust (`crates/server/tests` or unit): with the fixture cert,
1. ALPN negotiates `h2` when on, `http/1.1` when off or when the client offers
   only http/1.1.
2. A request/response over h2 (use `h2` crate client or `hyper` client with
   rustls in dev-dependencies): status, headers, body, HEAD, POST body.
3. `:authority` becomes `host`; no `connection: close` on h2.
4. Rapid reset: a client that opens and RST_STREAMs streams in a tight loop is
   disconnected (GOAWAY/close) within a bound, and the server keeps serving
   a new connection afterwards.
5. `max_concurrent_streams`: stream 101 is refused (REFUSED_STREAM) while 100 stay open.
6. Oversized header list is refused; large body flows under flow control
   (e.g. 5 MiB upload and download).
7. Graceful stop: in-flight stream completes, GOAWAY sent, new connection refused.
8. h1 over TLS with `http2` on still works (ALPN fallback).
Python: settings validation (http2 without TLS), scope `http_version == "2"`
through the real server, `granian_tls` mode switch, supervisor scheme unchanged.
Real-client check by hand: `curl --http2 -k https://...` and `curl --http1.1`,
record the output.

## 6. Review focus (Opus, independent)

Stream limits, rapid reset, flow control/window exhaustion, slot accounting
(one connection = one slot regardless of streams), shutdown, h2 header smuggling
(`:authority`/host mismatch, uppercase names, `te`, connection-specific headers),
trusted-proxy interaction with `x-forwarded-proto` on h2, write stall timer.

## 7. Out of scope

h2c, server push, WebSocket/CONNECT over h2, per-stream priorities, HTTP/3.
