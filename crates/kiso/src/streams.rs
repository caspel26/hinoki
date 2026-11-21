//! What one HTTP/2 connection is doing, and when to stop serving it.
//!
//! hyper's h2 driver has no idle, send-stall or reset-flood limits beyond the
//! counting of resets of streams nobody accepted yet. [`Streams`] fills the gaps
//! from the service side: every request holds an [`Entry`] from the moment its
//! head is complete until its response body is dropped, and [`Streams::watch`]
//! tells the connection task to close:
//!
//! * [`Close::Graceful`], when no stream was open for `idle` (a connection that
//!   only answers pings): GOAWAY, then drain, bounded by the shutdown timeout. A
//!   stream opened in the GOAWAY race is accepted and then cut when that bound
//!   ends. A client that trickles the head of a stream while others run is not
//!   bounded here (h2 exposes no per-head timer); it is bounded when it is the
//!   only activity.
//! * [`Close::Graceful`], when one response made no progress for `stall` (the
//!   peer keeps that stream's window at zero while answering pings, or stopped
//!   reading). Bodies are handed to hyper in [`CHUNK`]-sized frames (the last
//!   byte alone, see `poll_frame`), and hyper keeps the body (and so the
//!   entry) until every byte it sent fits the stream window, so the time since
//!   a stream's last poll measures its own progress; a new stream never resets
//!   it. hyper offers no way to reset one stream from
//!   outside (the stalled one is not polled), so the whole connection gets a
//!   GOAWAY and its other streams get the shutdown timeout to finish, after which
//!   the stalled one is cut with the connection.
//! * [`Close::Hard`], on a reset flood: more than [`RESET_LIMIT`] streams of this
//!   connection ended without their response being completed within a fixed
//!   [`RESET_WINDOW`] are client resets of requests the backend already received,
//!   which hyper does not count. The count is per connection, not per client: a
//!   client can pass up to twice the limit across a window boundary, and a new
//!   connection starts from zero. hyper cannot send `GOAWAY(ENHANCE_YOUR_CALM)`
//!   from here, so the connection is dropped.

use std::collections::HashMap;
use std::convert::Infallible;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::body::{Body, Frame, SizeHint};
use hyper::Response;
use tokio::sync::Notify;
use tokio::time::timeout;

use crate::stream::{Aborted, Produced, Reply};

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
    Graceful,
    /// Drop the connection.
    Hard,
}

pub struct Streams {
    born: Instant,
    open: AtomicUsize,
    next_id: AtomicU64,
    /// The responses being sent, with the milliseconds after `born` of the last
    /// DATA frame of each handed to hyper.
    sending: Mutex<HashMap<u64, u64>>,
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
            next_id: AtomicU64::new(0),
            sending: Mutex::new(HashMap::new()),
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
            id: self.next_id.fetch_add(1, Ordering::Relaxed),
            sending: false,
        }
    }

    fn sending(&self) -> std::sync::MutexGuard<'_, HashMap<u64, u64>> {
        self.sending.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn touch(&self, id: u64) {
        let millis = u64::try_from(self.born.elapsed().as_millis()).unwrap_or(u64::MAX);
        self.sending().insert(id, millis);
    }

    /// How long the response that has been quiet the longest has been so, or
    /// `None` when nothing is being sent.
    fn quiet(&self) -> Option<Duration> {
        let oldest = self.sending().values().min().copied()?;
        Some(
            self.born
                .elapsed()
                .saturating_sub(Duration::from_millis(oldest)),
        )
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
            } else {
                self.quiet().map(|quiet| stall.saturating_sub(quiet))
            };
            let Some(limit) = limit else {
                self.changed.notified().await;
                continue;
            };
            let timed_out = timeout(limit, self.changed.notified()).await.is_err();
            if timed_out && (self.open.load(Ordering::Acquire) == 0 || self.quiet() >= Some(stall))
            {
                return Close::Graceful;
            }
        }
    }
}

/// One request of the connection, from its head until its response is dropped.
pub struct Entry {
    streams: Arc<Streams>,
    done: bool,
    id: u64,
    sending: bool,
}

impl Drop for Entry {
    fn drop(&mut self) {
        if self.sending {
            self.streams.sending().remove(&self.id);
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
    /// A body still being produced; `rest` is empty while this is set.
    live: Option<Produced>,
    entry: Entry,
}

impl Chunked {
    /// A frame of a body the backend is still producing. A producer that
    /// failed is not a client reset, so the entry is complete either way.
    fn poll_live(
        live: &mut Produced,
        entry: &mut Entry,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Aborted>>> {
        match live.poll_piece(cx, CHUNK) {
            // hyper asked for a frame, so the peer has window; the wait is on
            // the producer and must not count as a stalled client.
            Poll::Pending => {
                entry.streams.sending().remove(&entry.id);
                Poll::Pending
            }
            Poll::Ready(Some(Ok(chunk))) => {
                entry.streams.touch(entry.id);
                Poll::Ready(Some(Ok(Frame::data(chunk))))
            }
            Poll::Ready(Some(Err(aborted))) => {
                entry.done = true;
                Poll::Ready(Some(Err(aborted)))
            }
            Poll::Ready(None) => {
                entry.done = true;
                Poll::Ready(None)
            }
        }
    }
}

impl Body for Chunked {
    type Data = Bytes;
    type Error = Aborted;

    fn poll_frame(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Aborted>>> {
        let this = self.get_mut();
        if let Some(live) = this.live.as_mut() {
            return Self::poll_live(live, &mut this.entry, cx);
        }
        if this.rest.is_empty() {
            this.entry.done = true;
            return Poll::Ready(None);
        }
        // The last byte is a frame of its own. hyper sends the final byte only
        // once every byte before it has window (h2 counts buffered data against
        // the stream's capacity) and drops the body, and so the entry, after
        // that send. Without the split the last chunk is handed over with
        // end-of-stream as soon as one byte of window exists, the entry goes
        // away, and up to `CHUNK` bytes stay buffered on a stalled stream where
        // nothing watches them.
        let len = this.rest.len();
        let take = if len > CHUNK { CHUNK } else { (len - 1).max(1) };
        let chunk = this.rest.split_to(take);
        this.entry.streams.touch(this.entry.id);
        this.entry.done = this.rest.is_empty();
        Poll::Ready(Some(Ok(Frame::data(chunk))))
    }

    fn is_end_stream(&self) -> bool {
        match &self.live {
            Some(live) => live.is_done(),
            None => self.rest.is_empty(),
        }
    }

    fn size_hint(&self) -> SizeHint {
        match &self.live {
            Some(_) => SizeHint::default(),
            None => SizeHint::with_exact(self.rest.len() as u64),
        }
    }
}

/// Wait for the answer of a request and hand it to hyper chunked, keeping the
/// entry alive until hyper drops the body. Dropped before it completes (the
/// client reset the stream), the entry counts as a reset.
pub async fn track(
    answered: impl Future<Output = Result<Response<Reply>, Infallible>>,
    mut entry: Entry,
) -> Result<Response<Chunked>, Infallible> {
    let Ok(response) = answered.await;
    let (parts, body) = response.into_parts();
    let (rest, live) = match body {
        Reply::Whole(full) => {
            let Ok(collected) = full.collect().await;
            (collected.to_bytes(), None)
        }
        Reply::Live(live) => (Bytes::new(), Some(Produced::Live(live))),
        Reply::File(file) => (Bytes::new(), Some(Produced::File(file))),
    };
    entry.done = rest.is_empty() && live.is_none();
    if !rest.is_empty() || live.is_some() {
        entry.sending = true;
        // A live body is timed from its first chunk, not while it is produced.
        if !rest.is_empty() {
            entry.streams.touch(entry.id);
        }
        entry.streams.changed.notify_one();
    }
    Ok(Response::from_parts(parts, Chunked { rest, live, entry }))
}
