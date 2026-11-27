//! The accept loop: connection limit, per-connection hyper service, graceful stop.
//!
//! Every connection runs as a task of a [`JoinSet`] owned by [`serve`], so no
//! task outlives the call: on stop the listener closes, each connection finishes
//! the request it is serving and then closes, and whatever is still running
//! after `shutdown_timeout` is aborted.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use hyper::server::conn::{http1, http2};
use hyper::service::service_fn;
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{watch, Notify, OwnedSemaphorePermit, Semaphore};
use tokio::task::JoinSet;
use tokio::time::{sleep, timeout};
use tokio_rustls::TlsAcceptor;

use crate::accesslog::{AccessLog, LogFilter};
use crate::compress::Compression;
use crate::conn::{respond, Backend, Handover, Switched, Transport};
use crate::framing::{Framing, Guarded, Signals};
use crate::metrics::Registry;
use crate::stall::StallGuard;
use crate::streams::{track, Close, Streams};
use crate::wsrun;

/// Stop signal: `true` (or a dropped sender) means stop.
pub type Stop = watch::Receiver<bool>;

/// Extra room above `max_header_bytes` for the request line, as a hard backstop
/// in hyper's own buffer.
const REQUEST_LINE_MARGIN: usize = 8 * 1024;

/// HTTP/2 limits, all explicit rather than hyper's defaults. A connection is
/// one slot however many streams it carries, and each stream in flight takes
/// backend capacity (the in-flight limit and the queue), so the stream cap
/// bounds what a single slot can occupy: 32 streams, so a few hundred
/// connections, not a few dozen, are needed to fill a 4096-deep queue.
const H2_MAX_CONCURRENT_STREAMS: u32 = 32;
const H2_STREAM_WINDOW: u32 = 65_535;
const H2_CONNECTION_WINDOW: u32 = 65_535 * 4;
const H2_MAX_FRAME_SIZE: u32 = 16_384;
const H2_MAX_SEND_BUF: usize = 256 * 1024;
const H2_KEEP_ALIVE_INTERVAL: Duration = Duration::from_secs(30);
const H2_KEEP_ALIVE_TIMEOUT: Duration = Duration::from_secs(20);
/// Rapid-reset mitigations of the h2 crate (CVE-2023-44487 and the reset-flood
/// family): streams a client resets before the server accepted them, and
/// streams the server itself resets because of a client error, are counted;
/// past the bound the connection gets GOAWAY (`ENHANCE_YOUR_CALM`).
const H2_MAX_PENDING_ACCEPT_RESET: usize = 20;
const H2_MAX_LOCAL_ERROR_RESET: usize = 128;

/// Pause after a failed `accept` (descriptor exhaustion), so the loop does not spin.
const ACCEPT_BACKOFF: Duration = Duration::from_millis(50);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServerConfig {
    /// Connections served at once; at the bound the loop stops accepting and
    /// the kernel backlog absorbs the rest.
    pub max_connections: usize,
    /// Time a client has to send a complete request head, also while idle
    /// between requests on a kept-alive connection.
    pub header_read_timeout: Duration,
    /// How long a client may stay silent between two chunks of its body. Total
    /// silence is answered 408 (the setting stays below the pipeline's request
    /// timeout); a client that drips a byte per interval is bounded only by the
    /// request timeout (504), as on the ASGI path.
    pub body_read_timeout: Duration,
    /// How long a response write may make no progress (a client that stopped
    /// reading) before the connection is closed.
    pub write_timeout: Duration,
    /// Time running connections get to finish after a stop.
    pub shutdown_timeout: Duration,
    /// The header limit the plan enforces; sizes hyper's backstop buffer.
    pub max_header_bytes: usize,
    pub max_headers: usize,
    /// Print one line per answered request to stderr, in this style.
    pub access_log: Option<AccessLog>,
    /// Which answers that log keeps.
    pub log_filter: Arc<LogFilter>,
    /// Add a `Server-Timing` header to every answer.
    pub server_timing: bool,
    /// Compress answers by this policy; `None` sends them as they are.
    pub compression: Option<Arc<Compression>>,
    /// Record every answer in these metrics; `None` records nothing.
    pub metrics: Option<Arc<Registry>>,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            max_connections: 10_000,
            header_read_timeout: Duration::from_secs(10),
            body_read_timeout: Duration::from_secs(10),
            write_timeout: Duration::from_secs(30),
            shutdown_timeout: Duration::from_secs(10),
            max_header_bytes: 32_768,
            max_headers: 100,
            access_log: None,
            log_filter: Arc::default(),
            server_timing: false,
            compression: None,
            metrics: None,
        }
    }
}

/// How `serve` ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Closed {
    /// Connections aborted because they outlived `shutdown_timeout`.
    pub aborted: usize,
}

/// Resolves once a stop was requested or the stop sender is gone.
async fn stopped(stop: &mut Stop) {
    let _ = stop.wait_for(|requested| *requested).await;
}

/// Wait for a free connection slot, then for a client.
async fn next_client(
    listener: &TcpListener,
    slots: &Arc<Semaphore>,
) -> Option<(
    OwnedSemaphorePermit,
    std::io::Result<(TcpStream, SocketAddr)>,
)> {
    let slot = Arc::clone(slots).acquire_owned().await.ok()?;
    Some((slot, listener.accept().await))
}

/// Run the TLS handshake, if the listener has TLS, then serve the connection.
/// The handshake has `header_read_timeout`, like a request head, and a
/// connection that fails it is closed without an answer.
async fn run_connection<B: Backend>(
    stream: TcpStream,
    peer: SocketAddr,
    backend: Arc<B>,
    config: ServerConfig,
    tls: Option<TlsAcceptor>,
    mut stop: Stop,
) {
    let _ = stream.set_nodelay(true);
    let Some(acceptor) = tls else {
        return serve_h1(stream, false, peer, backend, config, stop).await;
    };
    // A client still in its handshake does not hold a stop back.
    let handshake = timeout(config.header_read_timeout, acceptor.accept(stream));
    let secured = tokio::select! {
        done = handshake => done,
        () = stopped(&mut stop) => return,
    };
    let Ok(Ok(secured)) = secured else { return };
    // No ALPN result (a client that offered none) is HTTP/1.1.
    if secured.get_ref().1.alpn_protocol() == Some(b"h2".as_slice()) {
        serve_h2(secured, peer, backend, config, stop).await;
    } else {
        serve_h1(secured, true, peer, backend, config, stop).await;
    }
}

async fn serve_h1<S, B>(
    stream: S,
    secure: bool,
    peer: SocketAddr,
    backend: Arc<B>,
    config: ServerConfig,
    mut stop: Stop,
) where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    B: Backend,
{
    let signals = Arc::new(Signals::default());
    let handover = Handover::default();
    let transport = Arc::new(Transport {
        secure,
        max_headers: config.max_headers,
        access_log: config.access_log,
        log_filter: Arc::clone(&config.log_filter),
        server_timing: config.server_timing,
        compression: config.compression.clone(),
        metrics: config.metrics.clone(),
        body_read_timeout: config.body_read_timeout,
        signals: Arc::clone(&signals),
        handover: Arc::clone(&handover),
    });
    let first = Arc::new(Notify::new());
    let first_seen = Arc::clone(&first);
    let saw_first = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let service = service_fn(move |request| {
        if !saw_first.load(std::sync::atomic::Ordering::Relaxed) {
            saw_first.store(true, std::sync::atomic::Ordering::Relaxed);
            first_seen.notify_one();
        }
        respond(Arc::clone(&backend), peer, Arc::clone(&transport), request)
    });
    let head_limit = config.max_header_bytes.saturating_add(REQUEST_LINE_MARGIN);
    let guarded = Guarded::new(
        stream,
        Framing::new(head_limit, Arc::clone(&signals)),
        signals,
        config.write_timeout,
    );
    let connection = http1::Builder::new()
        .timer(TokioTimer::new())
        .header_read_timeout(config.header_read_timeout)
        // A client that shuts down its write side after a request still gets
        // the answer (`nc`, some health checkers).
        .half_close(true)
        .max_buf_size(head_limit)
        // The guard refuses the same bound, so both agree on what a head is.
        .max_header_size(head_limit)
        .max_headers(config.max_headers)
        .serve_connection(TokioIo::new(guarded), service)
        .with_upgrades();
    tokio::pin!(connection);
    tokio::select! {
        _ = connection.as_mut() => return switched(&handover, &config, stop).await,
        () = stopped(&mut stop) => {}
    }
    // `first` holds a permit once any request has reached the service. Without
    // one, the connection was accepted before the stop but not yet read: answer
    // its first request (with `Connection: close`) instead of resetting it.
    // Bounded by the header timeout, like any client that sends nothing.
    let arrived = timeout(config.header_read_timeout, async {
        tokio::select! {
            _ = connection.as_mut() => false,
            () = first.notified() => true,
        }
    })
    .await;
    if arrived != Ok(true) {
        return switched(&handover, &config, stop).await;
    }
    connection.as_mut().graceful_shutdown();
    let _ = connection.await;
    switched(&handover, &config, stop).await;
}

/// Run the WebSocket session an upgrade left in `handover`, if one did. A stop
/// that came first closes it right away with 1001.
async fn switched(handover: &Handover, config: &ServerConfig, stop: Stop) {
    let taken = handover
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .take();
    let Some(Switched { on, session }) = taken else {
        return;
    };
    let Ok(Ok(upgraded)) = timeout(config.write_timeout, on).await else {
        return;
    };
    wsrun::run(TokioIo::new(upgraded), session, stop).await;
}

/// One HTTP/2 connection (TLS only). The HTTP/1 text guards do not apply: h2
/// frames the messages itself. The write-stall timer still does. hyper's h2 has
/// no idle, send-stall or reset-flood limits of its own, so [`Streams`] watches
/// them: idle for `header_read_timeout`, or a response with no progress for
/// `write_timeout`, closes gracefully (GOAWAY); a flood of resets drops the
/// connection; the keep-alive ping bounds a peer that is gone.
async fn serve_h2<S, B>(
    stream: S,
    peer: SocketAddr,
    backend: Arc<B>,
    config: ServerConfig,
    mut stop: Stop,
) where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    B: Backend,
{
    let transport = Arc::new(Transport {
        secure: true,
        max_headers: config.max_headers,
        access_log: config.access_log,
        log_filter: Arc::clone(&config.log_filter),
        server_timing: config.server_timing,
        compression: config.compression.clone(),
        metrics: config.metrics.clone(),
        body_read_timeout: config.body_read_timeout,
        signals: Arc::new(Signals::default()),
        handover: Handover::default(),
    });
    let streams = Streams::new();
    let counted = Arc::clone(&streams);
    let service = service_fn(move |request| {
        let entry = counted.enter();
        track(
            respond(Arc::clone(&backend), peer, Arc::clone(&transport), request),
            entry,
        )
    });
    let guarded = StallGuard::new(stream, config.write_timeout);
    let connection = http2::Builder::new(TokioExecutor::new())
        .timer(TokioTimer::new())
        .max_concurrent_streams(H2_MAX_CONCURRENT_STREAMS)
        .max_header_list_size(u32::try_from(config.max_header_bytes).unwrap_or(u32::MAX))
        .initial_stream_window_size(H2_STREAM_WINDOW)
        .initial_connection_window_size(H2_CONNECTION_WINDOW)
        .adaptive_window(false)
        .max_frame_size(H2_MAX_FRAME_SIZE)
        .max_send_buf_size(H2_MAX_SEND_BUF)
        .keep_alive_interval(H2_KEEP_ALIVE_INTERVAL)
        .keep_alive_timeout(H2_KEEP_ALIVE_TIMEOUT)
        .max_pending_accept_reset_streams(H2_MAX_PENDING_ACCEPT_RESET)
        .max_local_error_reset_streams(H2_MAX_LOCAL_ERROR_RESET)
        .serve_connection(TokioIo::new(guarded), service);
    tokio::pin!(connection);
    let close = tokio::select! {
        _ = connection.as_mut() => return,
        () = stopped(&mut stop) => None,
        close = streams.watch(config.header_read_timeout, config.write_timeout) => Some(close),
    };
    if close == Some(Close::Hard) {
        return;
    }
    connection.as_mut().graceful_shutdown();
    if close.is_some() {
        // Closed for idleness or a stall: a half-sent head or a stalled stream must
        // not keep it open.
        let _ = timeout(config.shutdown_timeout, connection).await;
    } else {
        let _ = connection.await;
    }
}

/// Let running connections finish, then abort what is left.
async fn drain(mut connections: JoinSet<()>, limit: Duration) -> Closed {
    let finished = timeout(limit, async {
        while connections.join_next().await.is_some() {}
    })
    .await;
    if finished.is_ok() {
        return Closed { aborted: 0 };
    }
    let aborted = connections.len();
    connections.abort_all();
    while connections.join_next().await.is_some() {}
    Closed { aborted }
}

/// Accept connections on `listener` and serve them with `backend` (over TLS
/// when `tls` is set) until `stop` fires; returns once the connections have finished or been aborted.
pub async fn serve<B: Backend>(
    listener: TcpListener,
    backend: Arc<B>,
    config: ServerConfig,
    tls: Option<TlsAcceptor>,
    mut stop: Stop,
) -> Closed {
    let slots = Arc::new(Semaphore::new(config.max_connections));
    let mut connections = JoinSet::new();
    loop {
        tokio::select! {
            biased;
            () = stopped(&mut stop) => break,
            Some(_) = connections.join_next() => {}
            Some((slot, accepted)) = next_client(&listener, &slots) => match accepted {
                Ok((stream, peer)) => {
                    let task = run_connection(
                        stream, peer, Arc::clone(&backend), config.clone(), tls.clone(), stop.clone(),
                    );
                    connections.spawn(async move {
                        task.await;
                        drop(slot);
                    });
                }
                Err(_) => sleep(ACCEPT_BACKOFF).await,
            },
        }
    }
    drop(listener);
    drain(connections, config.shutdown_timeout).await
}
