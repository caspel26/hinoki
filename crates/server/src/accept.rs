//! The accept loop: connection limit, per-connection hyper service, graceful stop.
//!
//! Every connection runs as a task of a [`JoinSet`] owned by [`serve`], so no
//! task outlives the call: on stop the listener closes, each connection finishes
//! the request it is serving and then closes, and whatever is still running
//! after `shutdown_timeout` is aborted.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::{TokioIo, TokioTimer};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{watch, OwnedSemaphorePermit, Semaphore};
use tokio::task::JoinSet;
use tokio::time::{sleep, timeout};

use crate::conn::{respond, Backend};

/// Stop signal: `true` (or a dropped sender) means stop.
pub type Stop = watch::Receiver<bool>;

/// Extra room above `max_header_bytes` for the request line, as a hard backstop
/// in hyper's own buffer.
const REQUEST_LINE_MARGIN: usize = 8 * 1024;

/// Pause after a failed `accept` (descriptor exhaustion), so the loop does not spin.
const ACCEPT_BACKOFF: Duration = Duration::from_millis(50);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ServerConfig {
    /// Connections served at once; at the bound the loop stops accepting and
    /// the kernel backlog absorbs the rest.
    pub max_connections: usize,
    /// Time a client has to send a complete request head, also while idle
    /// between requests on a kept-alive connection.
    pub header_read_timeout: Duration,
    /// Time running connections get to finish after a stop.
    pub shutdown_timeout: Duration,
    /// The header limit the plan enforces; sizes hyper's backstop buffer.
    pub max_header_bytes: usize,
    pub max_headers: usize,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            max_connections: 10_000,
            header_read_timeout: Duration::from_secs(10),
            shutdown_timeout: Duration::from_secs(10),
            max_header_bytes: 32_768,
            max_headers: 100,
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

async fn run_connection<B: Backend>(
    stream: TcpStream,
    peer: SocketAddr,
    backend: Arc<B>,
    config: ServerConfig,
    mut stop: Stop,
) {
    let _ = stream.set_nodelay(true);
    let service = service_fn(move |request| respond(Arc::clone(&backend), peer, request));
    let connection = http1::Builder::new()
        .timer(TokioTimer::new())
        .header_read_timeout(config.header_read_timeout)
        // A client that shuts down its write side after a request still gets
        // the answer (`nc`, some health checkers).
        .half_close(true)
        .max_buf_size(config.max_header_bytes.saturating_add(REQUEST_LINE_MARGIN))
        .max_headers(config.max_headers)
        .serve_connection(TokioIo::new(stream), service);
    tokio::pin!(connection);
    tokio::select! {
        _ = connection.as_mut() => return,
        () = stopped(&mut stop) => {}
    }
    connection.as_mut().graceful_shutdown();
    let _ = connection.await;
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

/// Accept connections on `listener` and serve them with `backend` until `stop`
/// fires; returns once the connections have finished or been aborted.
pub async fn serve<B: Backend>(
    listener: TcpListener,
    backend: Arc<B>,
    config: ServerConfig,
    mut stop: Stop,
) -> Closed {
    let slots = Arc::new(Semaphore::new(config.max_connections));
    let mut connections = JoinSet::new();
    loop {
        tokio::select! {
            () = stopped(&mut stop) => break,
            Some(_) = connections.join_next() => {}
            Some((slot, accepted)) = next_client(&listener, &slots) => match accepted {
                Ok((stream, peer)) => {
                    let task = run_connection(
                        stream, peer, Arc::clone(&backend), config, stop.clone(),
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
