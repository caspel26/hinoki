//! A response whose body is produced while it is being sent.
//!
//! A [`Backend`](crate::conn::Backend) that answers with [`Delivery::Streamed`]
//! hands over the status and headers at once and the body through a bounded
//! channel: the producer waits when the client reads slowly (backpressure), and
//! learns that the client is gone when the receiving end is dropped. A producer
//! that fails midway sends [`Aborted`], which resets the stream (HTTP/2) or
//! closes the connection (HTTP/1.1) instead of ending the body as if complete.
//!
//! While it waits for the producer a [`Live`] body keeps two optional timers
//! ([`Pace`]): one that ends the response as [`Aborted`] when the producer stays
//! silent too long, and one that sends a server-sent-events comment so a quiet
//! stream is not closed by an intermediary. A file is sent by [`FileBody`],
//! which reads it in pieces without any producer.

use std::fmt;
use std::future::Future;
use std::io::Write as _;
use std::path::Path;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::Duration;

use bytes::Bytes;
use http_body_util::Full;
use hyper::body::{Body, Frame, SizeHint};
use tokio::io::{AsyncRead, ReadBuf};
use tokio::sync::mpsc;
use tokio::time::{Instant, Sleep};

use crate::conn::Answer;
use crate::encode::Encoded;
use crate::headers::Header;
use crate::metrics::{ErrorKind, Registry};
use crate::wsrun::Accepted;

/// One piece of a streamed body, or the producer's failure.
pub type Chunk = Result<Bytes, Aborted>;

/// The producer of a streamed body failed: the response is not complete.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Aborted;

impl fmt::Display for Aborted {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("the response body was aborted")
    }
}

impl std::error::Error for Aborted {}

/// Write one error line to stderr: the transport could not send an answer as
/// the backend gave it. A closed or failing stderr is ignored.
pub fn report(text: &str) {
    let line = format!("kiso: error: {text}\n");
    let _ = std::io::stderr().lock().write_all(line.as_bytes());
}

fn report_length(received: u64, declared: u64, relation: &str) {
    report(&format!(
        "the response body has {received} bytes, {relation} than the {declared} its \
         content-length declares; the connection is closed"
    ));
}

/// How long a [`Live`] body waits for its producer.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Pace {
    /// End the response as [`Aborted`] when the producer sends nothing for this
    /// long. Keep-alive frames do not count as the producer's progress.
    pub idle: Option<Duration>,
    /// Send [`sse::KEEPALIVE`](crate::sse::KEEPALIVE) whenever the producer has
    /// been silent for this long; any piece from the producer restarts the wait.
    pub keepalive: Option<Duration>,
}

/// An answer whose head is known and whose body follows in `chunks`.
#[derive(Debug)]
pub struct Streamed {
    pub status: u16,
    pub headers: Vec<Header>,
    pub chunks: mpsc::Receiver<Chunk>,
    pub pace: Pace,
}

/// An answer whose body is an open file. The head must carry the
/// `content-length` of [`FileBody::len`].
#[derive(Debug)]
pub struct FileAnswer {
    pub status: u16,
    pub headers: Vec<Header>,
    pub file: FileBody,
}

/// A complete answer with pre-validated hyper headers, bypassing header
/// re-parsing on the hot path.
#[derive(Debug)]
pub struct FastAnswer {
    pub status: u16,
    pub headers: hyper::HeaderMap,
    pub body: Bytes,
}

/// What a backend gives back for one request.
#[derive(Debug)]
pub enum Delivery {
    Whole(Answer),
    Fast(FastAnswer),
    Streamed(Streamed),
    File(FileAnswer),
    /// Switch the connection to WebSocket: a `101`, then the session.
    Upgrade(Box<Accepted>),
}

impl Delivery {
    pub fn status(&self) -> u16 {
        match self {
            Self::Whole(answer) => answer.status,
            Self::Fast(answer) => answer.status,
            Self::Streamed(streamed) => streamed.status,
            Self::File(file) => file.status,
            Self::Upgrade(_) => 101,
        }
    }
}

/// The body hyper sends: all of it at once, as the producer makes it, a file, or
/// the compressed form of one of them.
pub enum Reply {
    Whole(Full<Bytes>),
    Live(Live),
    File(FileBody),
    Encoded(Box<Encoded>),
    /// One of the above whose bytes the metrics count.
    Counted(Box<Counted>),
}

/// A restartable wait on the runtime's clock.
struct Timer {
    period: Duration,
    sleep: Pin<Box<Sleep>>,
}

impl Timer {
    /// `None` for a zero period, which would fire on every poll.
    fn new(period: Duration) -> Option<Self> {
        (!period.is_zero()).then(|| Self {
            period,
            sleep: Box::pin(tokio::time::sleep(period)),
        })
    }

    fn restart(&mut self) {
        self.sleep.as_mut().reset(Instant::now() + self.period);
    }

    /// Whether the period has passed; registers the waker when it has not.
    fn expired(&mut self, cx: &mut Context<'_>) -> bool {
        self.sleep.as_mut().poll(cx).is_ready()
    }
}

/// The receiving end of a [`Streamed`] body.
pub struct Live {
    chunks: mpsc::Receiver<Chunk>,
    pending: Bytes,
    finished: bool,
    /// The `content-length` the head declared, which the body must match.
    declared: Option<u64>,
    received: u64,
    idle: Option<Timer>,
    keepalive: Option<Timer>,
}

impl Live {
    pub fn new(chunks: mpsc::Receiver<Chunk>) -> Self {
        Self::with_length(chunks, None)
    }

    /// A body whose head declared `content-length: declared`. One that turns
    /// out shorter or longer ends as [`Aborted`] (the connection is closed or
    /// the stream reset) instead of leaving the client waiting for bytes that
    /// never come, or reading the excess as the next response.
    pub fn with_length(chunks: mpsc::Receiver<Chunk>, declared: Option<u64>) -> Self {
        Self {
            chunks,
            pending: Bytes::new(),
            finished: false,
            declared,
            received: 0,
            idle: None,
            keepalive: None,
        }
    }

    /// Keep the timers of `pace`. Call it on the runtime that polls the body.
    #[must_use]
    pub fn paced(mut self, pace: Pace) -> Self {
        self.idle = pace.idle.and_then(Timer::new);
        self.keepalive = pace.keepalive.and_then(Timer::new);
        self
    }

    /// The next frame of at most `limit` bytes, the producer's failure, or the end.
    pub fn poll_piece(
        &mut self,
        cx: &mut Context<'_>,
        limit: usize,
    ) -> Poll<Option<Result<Bytes, Aborted>>> {
        loop {
            if !self.pending.is_empty() {
                let take = self.pending.len().min(limit);
                return Poll::Ready(Some(Ok(self.pending.split_to(take))));
            }
            if self.finished {
                return Poll::Ready(None);
            }
            match self.fill(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(Some(aborted)) => return Poll::Ready(Some(Err(aborted))),
                Poll::Ready(None) => {}
            }
        }
    }

    /// Advance the body one step: `Ready(None)` says the state changed (a piece
    /// is queued or the body ended) and the caller looks again; `Ready(Some)`
    /// is a failure to report.
    fn fill(&mut self, cx: &mut Context<'_>) -> Poll<Option<Aborted>> {
        match self.chunks.poll_recv(cx) {
            Poll::Pending => self.wait(cx),
            Poll::Ready(Some(Ok(bytes))) => {
                self.restart_timers();
                Poll::Ready(self.take(bytes))
            }
            Poll::Ready(Some(Err(aborted))) => {
                self.finished = true;
                Poll::Ready(Some(aborted))
            }
            Poll::Ready(None) => Poll::Ready(self.end()),
        }
    }

    fn restart_timers(&mut self) {
        for timer in [&mut self.idle, &mut self.keepalive].into_iter().flatten() {
            timer.restart();
        }
    }

    /// The producer has nothing ready: give up on it, or send a keep-alive.
    fn wait(&mut self, cx: &mut Context<'_>) -> Poll<Option<Aborted>> {
        if let Some(period) = self.idle_expired(cx) {
            self.finished = true;
            self.chunks.close();
            report(&format!(
                "the response producer sent nothing for {period:?}; the connection is closed"
            ));
            return Poll::Ready(Some(Aborted));
        }
        if self.keepalive_due(cx) {
            self.pending = Bytes::from_static(crate::sse::KEEPALIVE);
            return Poll::Ready(None);
        }
        Poll::Pending
    }

    /// The idle period, once the producer has been silent for all of it.
    fn idle_expired(&mut self, cx: &mut Context<'_>) -> Option<Duration> {
        let idle = self.idle.as_mut()?;
        idle.expired(cx).then_some(idle.period)
    }

    /// Whether a keep-alive is due; the interval starts again when it is.
    fn keepalive_due(&mut self, cx: &mut Context<'_>) -> bool {
        let Some(keepalive) = self.keepalive.as_mut() else {
            return false;
        };
        let due = keepalive.expired(cx);
        if due {
            keepalive.restart();
        }
        due
    }

    /// Queue a piece the producer sent, unless it goes past the declared length.
    fn take(&mut self, bytes: Bytes) -> Option<Aborted> {
        self.received += bytes.len() as u64;
        match self.declared {
            Some(declared) if self.received > declared => {
                self.finished = true;
                report_length(self.received, declared, "more");
                Some(Aborted)
            }
            _ => {
                self.pending = bytes;
                None
            }
        }
    }

    /// The producer is done: the end, or an error if the body fell short.
    fn end(&mut self) -> Option<Aborted> {
        self.finished = true;
        match self.declared {
            Some(declared) if self.received < declared => {
                report_length(self.received, declared, "fewer");
                Some(Aborted)
            }
            _ => None,
        }
    }

    /// Whether the body ended and nothing is left to send.
    pub fn is_done(&self) -> bool {
        self.finished && self.pending.is_empty()
    }
}

/// The most a [`FileBody`] reads at once.
pub const FILE_PIECE: usize = 64 * 1024;

/// A regular file sent as a body, read in pieces of at most [`FILE_PIECE`]
/// bytes without any producer. It sends exactly the length it saw when it was
/// opened: a file that grows meanwhile is cut there, one that shrinks ends as
/// [`Aborted`]. Dropping it closes the file.
#[derive(Debug)]
pub struct FileBody {
    file: tokio::fs::File,
    length: u64,
    remaining: u64,
    failed: bool,
    scratch: Vec<u8>,
}

impl FileBody {
    /// Open `path`, which must be a regular file (a directory, device or other
    /// special file is `InvalidInput`).
    pub async fn open(path: &Path) -> std::io::Result<Self> {
        let file = tokio::fs::File::open(path).await?;
        let metadata = file.metadata().await?;
        if !metadata.is_file() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "not a regular file",
            ));
        }
        Ok(Self {
            file,
            length: metadata.len(),
            remaining: metadata.len(),
            failed: false,
            scratch: Vec::new(),
        })
    }

    /// The size of the file when it was opened: the `content-length` to declare.
    pub fn len(&self) -> u64 {
        self.length
    }

    pub fn is_empty(&self) -> bool {
        self.length == 0
    }

    /// The next piece of at most `limit` bytes, a failure, or the end.
    pub fn poll_piece(
        &mut self,
        cx: &mut Context<'_>,
        limit: usize,
    ) -> Poll<Option<Result<Bytes, Aborted>>> {
        if self.is_done() {
            return Poll::Ready(None);
        }
        let want = u64::try_from(limit.min(FILE_PIECE))
            .map_or(self.remaining, |cap| cap.min(self.remaining))
            .max(1) as usize;
        self.scratch.resize(want, 0);
        let mut buffer = ReadBuf::new(&mut self.scratch);
        match Pin::new(&mut self.file).poll_read(cx, &mut buffer) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(Err(error)) => {
                report(&format!("the file cannot be read ({error})"));
                self.failed = true;
                Poll::Ready(Some(Err(Aborted)))
            }
            Poll::Ready(Ok(())) => {
                let read = buffer.filled().len();
                Poll::Ready(Some(self.piece(read)))
            }
        }
    }

    /// The `read` bytes just read into the scratch buffer, or the failure of a
    /// file that ended before its length.
    fn piece(&mut self, read: usize) -> Result<Bytes, Aborted> {
        if read == 0 {
            report(&format!(
                "the file has {} bytes, fewer than the {} its content-length declares; the \
                 connection is closed",
                self.length - self.remaining,
                self.length
            ));
            self.failed = true;
            return Err(Aborted);
        }
        self.remaining -= read as u64;
        Ok(Bytes::copy_from_slice(&self.scratch[..read]))
    }

    /// The bytes not yet read.
    pub fn remaining(&self) -> u64 {
        self.remaining
    }

    /// Whether everything was sent, or the body failed.
    pub fn is_done(&self) -> bool {
        self.remaining == 0 || self.failed
    }
}

/// A body that is produced piece by piece: the two kinds of [`Reply`] that are
/// not whole, for a transport that frames them itself.
pub enum Produced {
    Live(Live),
    File(FileBody),
    Encoded(Box<Encoded>),
    Counted(Box<Counted>),
}

/// A body that is produced piece by piece and counted by the metrics: the
/// bytes handed on are added to its route, and a producer that fails is an error.
pub struct Counted {
    inner: Produced,
    registry: Arc<Registry>,
    slot: usize,
}

impl Counted {
    /// `reply` with its bytes counted for the route in `slot`. A whole body is
    /// counted at once; a body still being produced, as it is produced.
    pub fn wrap(reply: Reply, registry: &Arc<Registry>, slot: usize) -> Reply {
        let inner = match reply {
            Reply::Whole(full) => {
                let size = full.size_hint().exact().unwrap_or(0);
                registry.add_bytes(slot, usize::try_from(size).unwrap_or(usize::MAX));
                return Reply::Whole(full);
            }
            Reply::Live(live) => Produced::Live(live),
            Reply::File(file) => Produced::File(file),
            Reply::Encoded(encoded) => Produced::Encoded(encoded),
            Reply::Counted(counted) => return Reply::Counted(counted),
        };
        Reply::Counted(Box::new(Self {
            inner,
            registry: Arc::clone(registry),
            slot,
        }))
    }

    pub fn poll_piece(
        &mut self,
        cx: &mut Context<'_>,
        limit: usize,
    ) -> Poll<Option<Result<Bytes, Aborted>>> {
        let polled = self.inner.poll_piece(cx, limit);
        match &polled {
            Poll::Ready(Some(Ok(chunk))) => self.registry.add_bytes(self.slot, chunk.len()),
            Poll::Ready(Some(Err(_))) => self.registry.error(self.slot, ErrorKind::StreamAborted),
            _ => {}
        }
        polled
    }

    pub fn is_done(&self) -> bool {
        self.inner.is_done()
    }

    fn size_hint(&self) -> SizeHint {
        match &self.inner {
            Produced::File(file) => SizeHint::with_exact(file.remaining()),
            _ => SizeHint::default(),
        }
    }
}

impl Produced {
    pub fn poll_piece(
        &mut self,
        cx: &mut Context<'_>,
        limit: usize,
    ) -> Poll<Option<Result<Bytes, Aborted>>> {
        match self {
            Self::Live(live) => live.poll_piece(cx, limit),
            Self::File(file) => file.poll_piece(cx, limit),
            Self::Encoded(encoded) => encoded.poll_piece(cx, limit),
            Self::Counted(counted) => counted.poll_piece(cx, limit),
        }
    }

    pub fn is_done(&self) -> bool {
        match self {
            Self::Live(live) => live.is_done(),
            Self::File(file) => file.is_done(),
            Self::Encoded(encoded) => encoded.is_done(),
            Self::Counted(counted) => counted.is_done(),
        }
    }
}

impl Body for Reply {
    type Data = Bytes;
    type Error = Aborted;

    fn poll_frame(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Aborted>>> {
        match self.get_mut() {
            Self::Whole(full) => Pin::new(full)
                .poll_frame(cx)
                .map(|frame| frame.map(|result| result.map_err(|never| match never {}))),
            Self::Live(live) => live
                .poll_piece(cx, usize::MAX)
                .map(|piece| piece.map(|result| result.map(Frame::data))),
            Self::File(file) => file
                .poll_piece(cx, usize::MAX)
                .map(|piece| piece.map(|result| result.map(Frame::data))),
            Self::Encoded(encoded) => encoded
                .poll_piece(cx, usize::MAX)
                .map(|piece| piece.map(|result| result.map(Frame::data))),
            Self::Counted(counted) => counted
                .poll_piece(cx, usize::MAX)
                .map(|piece| piece.map(|result| result.map(Frame::data))),
        }
    }

    fn is_end_stream(&self) -> bool {
        match self {
            Self::Whole(full) => full.is_end_stream(),
            Self::Live(live) => live.is_done(),
            Self::File(file) => file.is_done(),
            Self::Encoded(encoded) => encoded.is_done(),
            Self::Counted(counted) => counted.is_done(),
        }
    }

    fn size_hint(&self) -> SizeHint {
        match self {
            Self::Whole(full) => full.size_hint(),
            Self::Live(_) | Self::Encoded(_) => SizeHint::default(),
            Self::File(file) => SizeHint::with_exact(file.remaining()),
            Self::Counted(counted) => counted.size_hint(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::future::poll_fn;

    async fn next(live: &mut Live, limit: usize) -> Option<Result<Bytes, Aborted>> {
        poll_fn(|cx| live.poll_piece(cx, limit)).await
    }

    #[tokio::test]
    async fn pieces_arrive_in_order_and_are_cut_to_the_limit() {
        let (sender, receiver) = mpsc::channel(2);
        let mut live = Live::new(receiver);
        sender.send(Ok(Bytes::from_static(b"abcde"))).await.unwrap();
        sender.send(Ok(Bytes::from_static(b"f"))).await.unwrap();
        drop(sender);
        let mut seen = Vec::new();
        while let Some(piece) = next(&mut live, 2).await {
            seen.push(piece.unwrap());
        }
        assert_eq!(seen, ["ab", "cd", "e", "f"]);
        assert!(live.is_done());
    }

    #[tokio::test]
    async fn a_failed_producer_is_an_error_not_an_end() {
        let (sender, receiver) = mpsc::channel(2);
        let mut live = Live::new(receiver);
        sender.send(Ok(Bytes::from_static(b"x"))).await.unwrap();
        sender.send(Err(Aborted)).await.unwrap();
        assert_eq!(next(&mut live, 8).await, Some(Ok(Bytes::from_static(b"x"))));
        assert_eq!(next(&mut live, 8).await, Some(Err(Aborted)));
        assert_eq!(next(&mut live, 8).await, None);
    }

    #[tokio::test]
    async fn dropping_the_body_tells_the_producer() {
        let (sender, receiver) = mpsc::channel::<Chunk>(1);
        drop(Live::new(receiver));
        assert!(sender.send(Ok(Bytes::new())).await.is_err());
    }

    async fn declared(length: u64, pieces: &[&'static [u8]]) -> Vec<Result<Bytes, Aborted>> {
        let (sender, receiver) = mpsc::channel(pieces.len().max(1));
        for piece in pieces {
            sender.send(Ok(Bytes::from_static(piece))).await.unwrap();
        }
        drop(sender);
        let mut live = Live::with_length(receiver, Some(length));
        let mut seen = Vec::new();
        while let Some(piece) = next(&mut live, 8).await {
            seen.push(piece);
        }
        seen
    }

    #[tokio::test]
    async fn a_body_held_to_its_declared_length() {
        let exact = declared(4, &[b"ab", b"cd"]).await;
        assert_eq!(
            exact,
            [Ok(Bytes::from_static(b"ab")), Ok(Bytes::from_static(b"cd"))]
        );
        let short = declared(5, &[b"ab"]).await;
        assert_eq!(short, [Ok(Bytes::from_static(b"ab")), Err(Aborted)]);
        let long = declared(3, &[b"ab", b"cd"]).await;
        assert_eq!(long, [Ok(Bytes::from_static(b"ab")), Err(Aborted)]);
    }

    fn paced(idle: Option<u64>, keepalive: Option<u64>) -> (mpsc::Sender<Chunk>, Live) {
        let (sender, receiver) = mpsc::channel(4);
        let pace = Pace {
            idle: idle.map(Duration::from_millis),
            keepalive: keepalive.map(Duration::from_millis),
        };
        (sender, Live::new(receiver).paced(pace))
    }

    const BEAT: Result<Bytes, Aborted> = Ok(Bytes::from_static(b": \n\n"));

    #[tokio::test]
    async fn a_silent_producer_is_aborted_after_the_idle_timeout() {
        let (sender, mut live) = paced(Some(80), None);
        sender.send(Ok(Bytes::from_static(b"a"))).await.unwrap();
        assert_eq!(next(&mut live, 8).await, Some(Ok(Bytes::from_static(b"a"))));
        let started = std::time::Instant::now();
        assert_eq!(next(&mut live, 8).await, Some(Err(Aborted)));
        assert!(started.elapsed() >= Duration::from_millis(70));
        assert!(sender.is_closed(), "the producer must see the end");
        assert_eq!(next(&mut live, 8).await, None);
    }

    #[tokio::test]
    async fn a_piece_restarts_the_idle_timeout() {
        let (sender, mut live) = paced(Some(250), None);
        let producer = tokio::spawn(async move {
            for _ in 0..4 {
                tokio::time::sleep(Duration::from_millis(120)).await;
                sender.send(Ok(Bytes::from_static(b"p"))).await.unwrap();
            }
            sender
        });
        let started = std::time::Instant::now();
        for _ in 0..4 {
            assert_eq!(next(&mut live, 8).await, Some(Ok(Bytes::from_static(b"p"))));
        }
        assert!(started.elapsed() >= Duration::from_millis(450));
        let _held = producer.await.unwrap();
        assert_eq!(next(&mut live, 8).await, Some(Err(Aborted)));
    }

    #[tokio::test]
    async fn a_silent_producer_gets_a_keep_alive_per_interval_and_a_piece_restarts_it() {
        let (sender, mut live) = paced(None, Some(60));
        let started = std::time::Instant::now();
        for _ in 0..3 {
            assert_eq!(next(&mut live, 8).await, Some(BEAT));
        }
        assert!(started.elapsed() >= Duration::from_millis(150));
        sender.send(Ok(Bytes::from_static(b"x"))).await.unwrap();
        assert_eq!(next(&mut live, 8).await, Some(Ok(Bytes::from_static(b"x"))));
        let soon = tokio::time::timeout(Duration::from_millis(25), next(&mut live, 8)).await;
        assert!(soon.is_err(), "the piece did not restart the interval");
        assert_eq!(next(&mut live, 8).await, Some(BEAT));
    }

    #[tokio::test]
    async fn keep_alives_do_not_count_as_the_producers_progress() {
        let (_sender, mut live) = paced(Some(250), Some(60));
        let started = std::time::Instant::now();
        let mut beats = 0;
        while next(&mut live, 8).await == Some(BEAT) {
            beats += 1;
        }
        let elapsed = started.elapsed();
        assert!((2..=5).contains(&beats), "{beats} keep-alives");
        assert!(elapsed >= Duration::from_millis(230), "{elapsed:?}");
        assert!(elapsed < Duration::from_millis(1500), "{elapsed:?}");
    }

    #[tokio::test]
    async fn a_keep_alive_is_cut_to_the_frame_limit_like_any_piece() {
        let (_sender, mut live) = paced(None, Some(20));
        assert_eq!(
            next(&mut live, 3).await,
            Some(Ok(Bytes::from_static(b": \n")))
        );
        assert_eq!(
            next(&mut live, 3).await,
            Some(Ok(Bytes::from_static(b"\n")))
        );
    }
}
