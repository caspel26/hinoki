//! A response whose body is produced while it is being sent.
//!
//! A [`Backend`](crate::conn::Backend) that answers with [`Delivery::Streamed`]
//! hands over the status and headers at once and the body through a bounded
//! channel: the producer waits when the client reads slowly (backpressure), and
//! learns that the client is gone when the receiving end is dropped. A producer
//! that fails midway sends [`Aborted`], which resets the stream (HTTP/2) or
//! closes the connection (HTTP/1.1) instead of ending the body as if complete.

use std::fmt;
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
}

impl Live {
    pub fn new(chunks: mpsc::Receiver<Chunk>) -> Self {
        Self {
            chunks,
            pending: Bytes::new(),
            finished: false,
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
                Poll::Ready(Some(Ok(bytes))) => self.pending = bytes,
                Poll::Ready(Some(Err(aborted))) => {
                    self.finished = true;
                    return Poll::Ready(Some(Err(aborted)));
                }
                Poll::Ready(None) => {
                    self.finished = true;
                    return Poll::Ready(None);
                }
            }
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
}
