//! Reading a request body: the size limits, the idle timeout and the answer to
//! "did the whole body arrive".
//!
//! hyper sends `100 Continue` when the body is first polled, so nothing here
//! polls before the caller asks: a request refused before its body is read never
//! gets a `100`. The reader is generic over [`Body`] only so the rules can be
//! tested without a socket.

use std::error::Error;
use std::fmt;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::body::{Body, Incoming as RequestBody};
use std::io::ErrorKind;
use tokio::time::timeout;

/// Why a body could not be read in full.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BodyError {
    /// The declared or the received size is above the limit.
    TooLarge,
    /// The client went away or the body was cut short (hyper ends the
    /// connection).
    Disconnected,
    /// The body framing is broken (an invalid chunk size, for example).
    Malformed,
    /// No data for longer than the idle timeout.
    Timeout,
}

impl fmt::Display for BodyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            BodyError::TooLarge => "too_large",
            BodyError::Disconnected => "disconnected",
            BodyError::Malformed => "malformed",
            BodyError::Timeout => "timeout",
        })
    }
}

impl Error for BodyError {}

/// Bytes reserved before the first one arrives; the buffer grows as data does,
/// so a large declared length costs nothing until it is sent.
const FIRST_RESERVE: usize = 64 * 1024;

/// A body error caused by malformed framing (hyper reports it as invalid data).
fn is_malformed(error: &(dyn Error + 'static)) -> bool {
    let mut current = Some(error);
    while let Some(e) = current {
        let invalid = e
            .downcast_ref::<std::io::Error>()
            .is_some_and(|io| io.kind() == ErrorKind::InvalidData);
        if invalid {
            return true;
        }
        current = e.source();
    }
    false
}

/// A request body that has not been read yet. Dropping it unread is how a
/// request is refused without reading: the connection layer then closes the
/// connection.
#[derive(Debug)]
pub struct BodyReader<B = RequestBody> {
    inner: B,
    idle: Duration,
    finished: Arc<AtomicBool>,
}

impl<B> BodyReader<B>
where
    B: Body<Data = Bytes> + Unpin,
    B::Error: Error + 'static,
{
    /// `idle` is how long the client may stay silent between two chunks.
    pub fn new(inner: B, idle: Duration) -> Self {
        Self {
            inner,
            idle,
            finished: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Set once the whole body was read; the connection layer closes the
    /// connection after any answer given without it.
    pub fn finished(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.finished)
    }

    /// Read the whole body, refusing more than `limit` bytes. A length that is
    /// declared above the limit is refused before anything is read.
    pub async fn read_all(mut self, limit: usize) -> Result<Vec<u8>, BodyError> {
        let declared = self.inner.size_hint().exact();
        if declared.is_some_and(|n| n > limit as u64) {
            return Err(BodyError::TooLarge);
        }
        let capacity = declared.map_or(0, |n| {
            usize::try_from(n)
                .unwrap_or(0)
                .min(limit)
                .min(FIRST_RESERVE)
        });
        let mut out = Vec::with_capacity(capacity);
        loop {
            let frame = timeout(self.idle, self.inner.frame())
                .await
                .map_err(|_| BodyError::Timeout)?;
            match frame {
                None => break,
                Some(Err(e)) if is_malformed(&e) => return Err(BodyError::Malformed),
                Some(Err(_)) => return Err(BodyError::Disconnected),
                Some(Ok(frame)) => {
                    let Ok(data) = frame.into_data() else {
                        continue;
                    };
                    if data.len() > limit - out.len() {
                        return Err(BodyError::TooLarge);
                    }
                    out.extend_from_slice(&data);
                }
            }
        }
        self.finished.store(true, Ordering::Release);
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::io;
    use std::pin::Pin;
    use std::task::{Context, Poll};

    use hyper::body::{Frame, SizeHint};

    use super::*;

    enum Step {
        Data(&'static [u8]),
        Fail,
        Garbage,
        Stall,
    }

    /// A body that plays a script.
    struct Script {
        steps: VecDeque<Step>,
        exact: Option<u64>,
    }

    impl Body for Script {
        type Data = Bytes;
        type Error = io::Error;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, io::Error>>> {
            if matches!(self.steps.front(), Some(Step::Stall)) {
                return Poll::Pending;
            }
            match self.steps.pop_front() {
                None => Poll::Ready(None),
                Some(Step::Data(bytes)) => {
                    Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(bytes)))))
                }
                Some(Step::Garbage) => Poll::Ready(Some(Err(io::ErrorKind::InvalidData.into()))),
                Some(Step::Fail) => Poll::Ready(Some(Err(io::ErrorKind::UnexpectedEof.into()))),
                Some(Step::Stall) => unreachable!(),
            }
        }

        fn size_hint(&self) -> SizeHint {
            self.exact.map_or_else(SizeHint::new, SizeHint::with_exact)
        }
    }

    fn reader(steps: Vec<Step>, exact: Option<u64>) -> BodyReader<Script> {
        let script = Script {
            steps: steps.into(),
            exact,
        };
        BodyReader::new(script, Duration::from_millis(50))
    }

    #[tokio::test]
    async fn reads_every_chunk_and_marks_the_body_finished() {
        let body = reader(vec![Step::Data(b"ab"), Step::Data(b"cd")], None);
        let finished = body.finished();
        assert_eq!(body.read_all(10).await.unwrap(), b"abcd");
        assert!(finished.load(Ordering::Acquire));
    }

    #[tokio::test]
    async fn a_declared_length_above_the_limit_is_refused_without_reading() {
        let body = reader(vec![Step::Stall], Some(11));
        assert_eq!(body.read_all(10).await.unwrap_err(), BodyError::TooLarge);
    }

    #[tokio::test]
    async fn the_limit_itself_is_allowed_and_one_more_byte_is_not() {
        let exact = reader(vec![Step::Data(b"0123456789")], None);
        assert_eq!(exact.read_all(10).await.unwrap().len(), 10);
        let over = reader(vec![Step::Data(b"01234"), Step::Data(b"567890")], None);
        assert_eq!(over.read_all(10).await.unwrap_err(), BodyError::TooLarge);
    }

    #[tokio::test]
    async fn a_stalled_client_times_out_and_the_body_stays_unfinished() {
        let body = reader(vec![Step::Data(b"a"), Step::Stall], None);
        let finished = body.finished();
        assert_eq!(body.read_all(10).await.unwrap_err(), BodyError::Timeout);
        assert!(!finished.load(Ordering::Acquire));
    }

    #[tokio::test]
    async fn a_broken_body_is_a_disconnect() {
        let body = reader(vec![Step::Data(b"a"), Step::Fail], None);
        assert_eq!(
            body.read_all(10).await.unwrap_err(),
            BodyError::Disconnected
        );
    }

    #[tokio::test]
    async fn broken_framing_is_told_apart_from_a_disconnect() {
        let body = reader(vec![Step::Garbage], None);
        assert_eq!(body.read_all(10).await.unwrap_err(), BodyError::Malformed);
    }

    #[tokio::test]
    async fn a_large_declared_length_reserves_nothing_before_data_arrives() {
        let limit = 1 << 30;
        let body = reader(vec![Step::Data(b"abc")], Some(limit as u64));
        let out = body.read_all(limit).await.unwrap();
        assert_eq!(out, b"abc");
        assert!(out.capacity() <= FIRST_RESERVE);
    }

    #[tokio::test]
    async fn an_empty_body_reads_as_empty() {
        let body: BodyReader<http_body_util::Empty<Bytes>> =
            BodyReader::new(http_body_util::Empty::new(), Duration::from_secs(1));
        assert!(body.read_all(10).await.unwrap().is_empty());
    }
}
