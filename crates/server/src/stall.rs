//! The write-stall timer, and a stream that has only that guard.
//!
//! A client that stopped reading must not hold a connection forever: any write
//! progress clears the timer, and a write that stays pending past the limit
//! fails with `TimedOut`. [`WriteStall`] is the timer, shared by the HTTP/1
//! [`crate::framing::Guarded`] stream and by [`StallGuard`], which HTTP/2
//! connections use (the framing checks are text-head rules of HTTP/1).

use std::future::Future;
use std::io;
use std::pin::Pin;
use std::task::{ready, Context, Poll};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::time::{sleep, Sleep};

/// Fails a write or flush that makes no progress for `limit`.
pub struct WriteStall {
    limit: Duration,
    timer: Option<Pin<Box<Sleep>>>,
}

impl WriteStall {
    pub fn new(limit: Duration) -> Self {
        Self { limit, timer: None }
    }

    /// Apply the timer to the outcome of a write or flush: any progress clears
    /// it, and a write that stays pending past the limit fails.
    pub fn guard<R>(
        &mut self,
        cx: &mut Context<'_>,
        polled: Poll<io::Result<R>>,
    ) -> Poll<io::Result<R>> {
        if polled.is_ready() {
            self.timer = None;
            return polled;
        }
        let limit = self.limit;
        let timer = self.timer.get_or_insert_with(|| Box::pin(sleep(limit)));
        if timer.as_mut().poll(cx).is_ready() {
            return Poll::Ready(Err(io::ErrorKind::TimedOut.into()));
        }
        Poll::Pending
    }
}

/// A stream with the write-stall timer and nothing else.
pub struct StallGuard<T> {
    inner: T,
    stall: WriteStall,
}

impl<T> StallGuard<T> {
    pub fn new(inner: T, write_timeout: Duration) -> Self {
        Self {
            inner,
            stall: WriteStall::new(write_timeout),
        }
    }
}

impl<T: AsyncRead + Unpin> AsyncRead for StallGuard<T> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_read(cx, buf)
    }
}

impl<T: AsyncWrite + Unpin> AsyncWrite for StallGuard<T> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        let polled = Pin::new(&mut this.inner).poll_write(cx, data);
        this.stall.guard(cx, polled)
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let polled = Pin::new(&mut this.inner).poll_flush(cx);
        this.stall.guard(cx, polled)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let polled = Pin::new(&mut this.inner).poll_shutdown(cx);
        ready!(this.stall.guard(cx, polled))?;
        Poll::Ready(Ok(()))
    }

    fn poll_write_vectored(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bufs: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        let polled = Pin::new(&mut this.inner).poll_write_vectored(cx, bufs);
        this.stall.guard(cx, polled)
    }

    fn is_write_vectored(&self) -> bool {
        self.inner.is_write_vectored()
    }
}
