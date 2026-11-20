//! A response whose body is produced while it is being sent.
//!
//! A [`Backend`](crate::conn::Backend) that answers with [`Delivery::Streamed`]
//! hands over the status and headers at once and the body through a bounded
//! channel: the producer waits when the client reads slowly (backpressure), and
//! learns that the client is gone when the receiving end is dropped. A producer
//! that fails midway sends [`Aborted`], which resets the stream (HTTP/2) or
//! closes the connection (HTTP/1.1) instead of ending the body as if complete.

use std::fmt;
use std::io::Write as _;
use std::pin::Pin;
use std::task::{Context, Poll};

use bytes::Bytes;
use http_body_util::Full;
use hyper::body::{Body, Frame, SizeHint};
use tokio::sync::mpsc;

use crate::conn::Answer;
use crate::headers::Header;

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

/// An answer whose head is known and whose body follows in `chunks`.
#[derive(Debug)]
pub struct Streamed {
    pub status: u16,
    pub headers: Vec<Header>,
    pub chunks: mpsc::Receiver<Chunk>,
}

/// What a backend gives back for one request.
#[derive(Debug)]
pub enum Delivery {
    Whole(Answer),
    Streamed(Streamed),
}

impl Delivery {
    pub fn status(&self) -> u16 {
        match self {
            Self::Whole(answer) => answer.status,
            Self::Streamed(streamed) => streamed.status,
        }
    }
}

/// The body hyper sends: all of it at once, or as the producer makes it.
pub enum Reply {
    Whole(Full<Bytes>),
    Live(Live),
}

/// The receiving end of a [`Streamed`] body.
pub struct Live {
    chunks: mpsc::Receiver<Chunk>,
    pending: Bytes,
    finished: bool,
    /// The `content-length` the head declared, which the body must match.
    declared: Option<u64>,
    received: u64,
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
        }
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
            match self.chunks.poll_recv(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(Some(Ok(bytes))) => {
                    if let Some(aborted) = self.take(bytes) {
                        return Poll::Ready(Some(Err(aborted)));
                    }
                }
                Poll::Ready(Some(Err(aborted))) => {
                    self.finished = true;
                    return Poll::Ready(Some(Err(aborted)));
                }
                Poll::Ready(None) => return Poll::Ready(self.end()),
            }
        }
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
    fn end(&mut self) -> Option<Result<Bytes, Aborted>> {
        self.finished = true;
        match self.declared {
            Some(declared) if self.received < declared => {
                report_length(self.received, declared, "fewer");
                Some(Err(Aborted))
            }
            _ => None,
        }
    }

    /// Whether the body ended and nothing is left to send.
    pub fn is_done(&self) -> bool {
        self.finished && self.pending.is_empty()
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
        }
    }

    fn is_end_stream(&self) -> bool {
        match self {
            Self::Whole(full) => full.is_end_stream(),
            Self::Live(live) => live.is_done(),
        }
    }

    fn size_hint(&self) -> SizeHint {
        match self {
            Self::Whole(full) => full.size_hint(),
            Self::Live(_) => SizeHint::default(),
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
}
