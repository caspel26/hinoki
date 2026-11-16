//! What one HTTP/2 connection is doing, and when to stop serving it.
//!
//! hyper's h2 driver has no idle, send-stall or reset-flood limits beyond the
//! counting of resets of streams nobody accepted yet. [`Streams`] fills the gaps
//! from the service side: every request holds an [`Entry`] from the moment its
//! head is complete until its response body is dropped, and [`Streams::watch`]
//! tells the connection task to close:
//!
//! * [`Close::Idle`]: no stream was open for `idle` (a connection that only
//!   answers pings). Graceful: GOAWAY, then drain. A client that trickles the
//!   head of a stream while others run is not bounded here (h2 exposes no
//!   per-head timer); it is bounded when it is the only activity.
//! * [`Close::Hard`], on a flow-control stall: a response body is being sent and
//!   no DATA moved for `stall` (the peer keeps its window at zero while still
//!   answering pings). Bodies are handed to hyper in [`CHUNK`]-sized frames, and
//!   hyper asks for the next one only when the previous was accepted by the
//!   stream window, so the time between two polls measures send progress. It is
//!   a connection-wide measure: it fires when no stream makes progress.
//! * [`Close::Hard`], on a reset flood: more than [`RESET_LIMIT`] streams ended
//!   without their response being completed ([`RESET_WINDOW`] seconds window)
//!   are client resets of requests the backend already received, which hyper
//!   does not count. hyper cannot send `GOAWAY(ENHANCE_YOUR_CALM)` from here, so
//!   the connection is dropped.

use std::convert::Infallible;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::body::{Body, Frame, SizeHint};
use hyper::Response;
use tokio::sync::Notify;
use tokio::time::timeout;

/// Size of the DATA frames a response is cut into.
const CHUNK: usize = 16 * 1024;
/// Incomplete streams tolerated per [`RESET_WINDOW`]: ten a second, far above
/// what cancelling clients (navigations, deadlines) do, far below a flood.
pub const RESET_LIMIT: u32 = 100;
pub const RESET_WINDOW: Duration = Duration::from_secs(10);

/// Why a connection should stop.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Close {
    /// Send GOAWAY and let the connection finish.
    Idle,
    /// Drop the connection.
    Hard,
}

pub struct Streams {
    born: Instant,
    open: AtomicUsize,
    sending: AtomicUsize,
    /// Milliseconds after `born` of the last DATA frame handed to hyper.
    progress: AtomicU64,
    flooded: AtomicBool,
    window: Mutex<(Instant, u32)>,
    changed: Notify,
}

impl Streams {
    pub fn new() -> Arc<Self> {
        let now = Instant::now();
        Arc::new(Self {
            born: now,
            open: AtomicUsize::new(0),
            sending: AtomicUsize::new(0),
            progress: AtomicU64::new(0),
            flooded: AtomicBool::new(false),
            window: Mutex::new((now, 0)),
            changed: Notify::new(),
        })
    }

    /// A request whose head is complete.
    pub fn enter(self: &Arc<Self>) -> Entry {
        self.open.fetch_add(1, Ordering::AcqRel);
        self.changed.notify_one();
        Entry {
            streams: Arc::clone(self),
            done: false,
            sending: false,
        }
    }

    fn touch(&self) {
        let millis = u64::try_from(self.born.elapsed().as_millis()).unwrap_or(u64::MAX);
        self.progress.store(millis, Ordering::Release);
    }

    fn quiet(&self) -> Duration {
        let last = Duration::from_millis(self.progress.load(Ordering::Acquire));
        self.born.elapsed().saturating_sub(last)
    }

    fn note_reset(&self) {
        let mut window = self.window.lock().unwrap_or_else(|e| e.into_inner());
        if window.0.elapsed() > RESET_WINDOW {
            *window = (Instant::now(), 0);
        }
        window.1 += 1;
        if window.1 > RESET_LIMIT {
            self.flooded.store(true, Ordering::Release);
        }
    }

    /// Resolves when the connection should stop.
    pub async fn watch(&self, idle: Duration, stall: Duration) -> Close {
        loop {
            if self.flooded.load(Ordering::Acquire) {
                return Close::Hard;
            }
            let limit = if self.open.load(Ordering::Acquire) == 0 {
                Some(idle)
            } else if self.sending.load(Ordering::Acquire) > 0 {
                Some(stall.saturating_sub(self.quiet()))
            } else {
                None
            };
            let Some(limit) = limit else {
                self.changed.notified().await;
                continue;
            };
            if timeout(limit, self.changed.notified()).await.is_err() {
                if self.open.load(Ordering::Acquire) == 0 {
                    return Close::Idle;
                }
                if self.sending.load(Ordering::Acquire) > 0 && self.quiet() >= stall {
                    return Close::Hard;
                }
            }
        }
    }
}

/// One request of the connection, from its head until its response is dropped.
pub struct Entry {
    streams: Arc<Streams>,
    done: bool,
    sending: bool,
}

impl Drop for Entry {
    fn drop(&mut self) {
        if self.sending {
            self.streams.sending.fetch_sub(1, Ordering::AcqRel);
        }
        if !self.done {
            self.streams.note_reset();
        }
        self.streams.open.fetch_sub(1, Ordering::AcqRel);
        self.streams.changed.notify_one();
    }
}

/// A response body that reaches hyper in [`CHUNK`]-sized frames.
pub struct Chunked {
    rest: Bytes,
    entry: Entry,
}

impl Body for Chunked {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        let this = self.get_mut();
        if this.rest.is_empty() {
            this.entry.done = true;
            return Poll::Ready(None);
        }
        let chunk = this.rest.split_to(this.rest.len().min(CHUNK));
        this.entry.streams.touch();
        this.entry.done = this.rest.is_empty();
        Poll::Ready(Some(Ok(Frame::data(chunk))))
    }

    fn is_end_stream(&self) -> bool {
        self.rest.is_empty()
    }

    fn size_hint(&self) -> SizeHint {
        SizeHint::with_exact(self.rest.len() as u64)
    }
}

/// Wait for the answer of a request and hand it to hyper chunked, keeping the
/// entry alive until hyper drops the body. Dropped before it completes (the
/// client reset the stream), the entry counts as a reset.
pub async fn track(
    answered: impl Future<Output = Result<Response<Full<Bytes>>, Infallible>>,
    mut entry: Entry,
) -> Result<Response<Chunked>, Infallible> {
    let Ok(response) = answered.await;
    let (parts, body) = response.into_parts();
    let Ok(collected) = body.collect().await;
    let rest = collected.to_bytes();
    entry.done = rest.is_empty();
    if !rest.is_empty() {
        entry.sending = true;
        entry.streams.sending.fetch_add(1, Ordering::AcqRel);
        entry.streams.touch();
        entry.streams.changed.notify_one();
    }
    Ok(Response::from_parts(parts, Chunked { rest, entry }))
}
