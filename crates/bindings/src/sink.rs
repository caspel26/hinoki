//! The producer's end of a streamed response.
//!
//! `ResponseSink` is what Python holds while a response is open. The body
//! pieces travel through a bounded channel to the connection task, which
//! frames them, applies backpressure and watches the idle and keep-alive
//! timers in Rust. The sink only copies bytes in and reports readiness; it
//! never touches the socket. Python still decides what the pieces are, so the
//! producer itself is not native.
//!
//! The channel has `STREAM_DEPTH + 1` slots. The last one is kept for
//! [`ResponseSink::abort`], so a producer that fills the channel can still end
//! the response as truncated instead of as complete.

use std::future::Future;
use std::pin::pin;
use std::sync::Mutex;
use std::task::Poll;

use bytes::Bytes;
use kiso::sse::{frame, Event};
use kiso::{Aborted, Chunk};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use tokio::sync::{mpsc, watch};

use crate::completion;

/// Pieces that may wait for the connection to send them.
pub(crate) const STREAM_DEPTH: usize = 8;

/// Slots in the channel: the pieces plus the one `abort` keeps.
pub(crate) const CHANNEL_SLOTS: usize = STREAM_DEPTH + 1;

#[pyclass(name = "ResponseSink", module = "hinoki.core", frozen)]
pub struct ResponseSink {
    tx: Mutex<Option<mpsc::Sender<Chunk>>>,
    done: watch::Sender<bool>,
}

impl ResponseSink {
    pub(crate) fn new(tx: mpsc::Sender<Chunk>) -> Self {
        Self {
            tx: Mutex::new(Some(tx)),
            done: watch::Sender::new(false),
        }
    }

    fn sender(&self) -> Option<mpsc::Sender<Chunk>> {
        self.tx.lock().ok().and_then(|slot| slot.clone())
    }

    /// Close the channel, after `last` when there is one. Idempotent.
    fn end(&self, last: Option<Chunk>) {
        let taken = self.tx.lock().ok().and_then(|mut slot| slot.take());
        if let Some(tx) = taken {
            if let Some(chunk) = last {
                let _ = tx.try_send(chunk);
            }
        }
        self.done.send_replace(true);
    }
}

impl Drop for ResponseSink {
    // A sink dropped while open is a producer that went away: the body is cut
    // short, never presented as complete.
    fn drop(&mut self) {
        self.end(Some(Err(Aborted)));
    }
}

/// `fut`'s output, or `None` once the response has been closed from this side.
async fn until_done<F: Future>(fut: F, mut done: watch::Receiver<bool>) -> Option<F::Output> {
    let mut fut = pin!(fut);
    let mut closed = pin!(done.wait_for(|finished| *finished));
    std::future::poll_fn(|cx| {
        if let Poll::Ready(output) = fut.as_mut().poll(cx) {
            return Poll::Ready(Some(output));
        }
        match closed.as_mut().poll(cx) {
            Poll::Ready(_) => Poll::Ready(None),
            Poll::Pending => Poll::Pending,
        }
    })
    .await
}

#[pymethods]
impl ResponseSink {
    /// Queue `data` when the channel has room (one slot is kept for `abort`).
    /// `False` means: full, or the response is closed (client gone, finished,
    /// aborted). Never blocks.
    fn try_send(&self, data: &[u8]) -> bool {
        let Ok(slot) = self.tx.lock() else {
            return false;
        };
        let Some(tx) = slot.as_ref() else {
            return false;
        };
        if tx.capacity() < 2 {
            return false;
        }
        data.is_empty() || tx.try_send(Ok(Bytes::copy_from_slice(data))).is_ok()
    }

    /// Resolves to `True` when `try_send` can take a piece, `False` when the
    /// response is closed.
    fn ready<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let sender = self.sender();
        let done = self.done.subscribe();
        let wait = async move {
            let Some(tx) = sender else {
                return Ok::<_, ()>(false);
            };
            let room = until_done(tx.reserve_many(2), done).await;
            Ok(matches!(room, Some(Ok(_))))
        };
        flag(py, wait)
    }

    /// Resolves to `True` when the client went away, `False` when the response
    /// was closed by `finish` or `abort` first. It does not resolve while the
    /// client is connected and the response open.
    fn closed<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let sender = self.sender();
        let done = self.done.subscribe();
        let wait = async move {
            let Some(tx) = sender else {
                return Ok::<_, ()>(false);
            };
            Ok(until_done(tx.closed(), done).await.is_some())
        };
        flag(py, wait)
    }

    /// End the body as complete. Idempotent.
    fn finish(&self) {
        self.end(None);
    }

    /// End the body as truncated: HTTP/1.1 closes the connection without the
    /// terminating chunk, HTTP/2 resets the stream. Idempotent.
    fn abort(&self) {
        self.end(Some(Err(Aborted)));
    }
}

/// An awaitable resolving to the future's boolean.
fn flag<'py, F>(py: Python<'py>, wait: F) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = Result<bool, ()>> + Send + 'static,
{
    completion::submit_with(
        py,
        wait,
        |py, value: bool| Ok(value.into_pyobject(py)?.to_owned().into_any().unbind()),
        (
            |_: Python<'_>, (): ()| PyRuntimeError::new_err("the response sink failed"),
            |_: Python<'_>| PyRuntimeError::new_err("the response sink task failed"),
        ),
    )
}

/// One server-sent event as wire bytes. Raises `ValueError` for a field that
/// would break out of its line or an event with no field.
#[pyfunction]
#[pyo3(signature = (data=None, event=None, id=None, retry=None, comment=None))]
pub(crate) fn sse_frame<'py>(
    py: Python<'py>,
    data: Option<&str>,
    event: Option<&str>,
    id: Option<&str>,
    retry: Option<u64>,
    comment: Option<&str>,
) -> PyResult<Bound<'py, PyBytes>> {
    let bytes = frame(&Event {
        event,
        id,
        retry,
        data,
        comment,
    })
    .map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(PyBytes::new(py, &bytes))
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use tokio::time::timeout;

    use super::*;

    fn sink() -> (ResponseSink, mpsc::Receiver<Chunk>) {
        let (tx, rx) = mpsc::channel(CHANNEL_SLOTS);
        (ResponseSink::new(tx), rx)
    }

    fn fill(sink: &ResponseSink) -> usize {
        (0..CHANNEL_SLOTS)
            .take_while(|_| sink.try_send(b"x"))
            .count()
    }

    #[test]
    fn a_full_channel_refuses_pieces_and_keeps_a_slot_for_the_abort() {
        let (sink, mut rx) = sink();
        assert_eq!(fill(&sink), STREAM_DEPTH);
        assert!(!sink.try_send(b"more"));
        sink.abort();
        for _ in 0..STREAM_DEPTH {
            assert_eq!(rx.try_recv(), Ok(Ok(Bytes::from_static(b"x"))));
        }
        assert_eq!(rx.try_recv(), Ok(Err(Aborted)));
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn finish_ends_the_body_cleanly_and_later_calls_do_nothing() {
        let (sink, mut rx) = sink();
        assert!(sink.try_send(b"a"));
        sink.finish();
        sink.abort();
        assert!(!sink.try_send(b"b"));
        assert_eq!(rx.try_recv(), Ok(Ok(Bytes::from_static(b"a"))));
        assert_eq!(rx.try_recv(), Err(mpsc::error::TryRecvError::Disconnected));
    }

    #[test]
    fn a_dropped_sink_cuts_the_body_short() {
        let (sink, mut rx) = sink();
        drop(sink);
        assert_eq!(rx.try_recv(), Ok(Err(Aborted)));
    }

    #[test]
    fn an_empty_piece_is_accepted_and_not_queued() {
        let (sink, mut rx) = sink();
        assert!(sink.try_send(b""));
        assert!(rx.try_recv().is_err());
    }

    fn block_on<F: Future>(fut: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap()
            .block_on(fut)
    }

    #[test]
    fn the_client_going_away_is_told_apart_from_the_response_ending() {
        block_on(async {
            let (sink, rx) = sink();
            let tx = sink.sender().unwrap();
            let watch = sink.done.subscribe();
            let open = timeout(
                Duration::from_millis(50),
                until_done(tx.closed(), watch.clone()),
            )
            .await;
            assert!(open.is_err(), "an open response does not resolve");
            drop(rx);
            assert_eq!(until_done(tx.closed(), watch).await, Some(()));

            let (closing, _rx) = self::sink();
            let tx = closing.sender().unwrap();
            let watch = closing.done.subscribe();
            closing.finish();
            assert_eq!(until_done(tx.closed(), watch).await, None);
        });
    }

    #[test]
    fn ready_waits_for_room_and_stops_when_the_response_ends() {
        block_on(async {
            let (sink, mut rx) = sink();
            let tx = sink.sender().unwrap();
            assert_eq!(fill(&sink), STREAM_DEPTH);
            let full = timeout(
                Duration::from_millis(50),
                until_done(tx.reserve_many(2), sink.done.subscribe()),
            )
            .await;
            assert!(full.is_err(), "no room yet");
            rx.recv().await.unwrap().unwrap();
            rx.recv().await.unwrap().unwrap();
            let room = until_done(tx.reserve_many(2), sink.done.subscribe()).await;
            assert!(matches!(room, Some(Ok(_))));
            sink.abort();
            let ended = until_done(std::future::pending::<()>(), sink.done.subscribe()).await;
            assert!(ended.is_none());
        });
    }
}
