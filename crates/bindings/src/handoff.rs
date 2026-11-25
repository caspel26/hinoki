//! Batching what the event-loop thread hands back to Tokio.
//!
//! Every reply sent and every task spawned from outside the runtime wakes a
//! Tokio worker, and on a busy machine each wake can cost a context switch.
//! Inside [`batch`], the loop thread collects its replies and spawns and hands
//! them over together when the batch ends: one wake for the whole batch.
//! Once a server called [`per_iteration`] for its loop, what that loop's thread
//! hands over outside a batch is collected until a callback queued with
//! `call_soon` runs, at the start of the next loop iteration (or when the loop
//! closes): one wake per iteration instead of one per response or database
//! call.

use std::cell::RefCell;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Waker};

use pyo3::prelude::*;
use tokio::sync::oneshot;

use crate::server::Sent;

type Task = Pin<Box<dyn Future<Output = ()> + Send>>;

enum Deferred {
    Reply(oneshot::Sender<Sent>, Sent),
    Spawn(Task),
}

impl Deferred {
    fn run(self) {
        match self {
            Self::Reply(sender, sent) => {
                let _ = sender.send(sent);
            }
            Self::Spawn(task) => {
                tokio::spawn(task);
            }
        }
    }
}

thread_local! {
    static PENDING: RefCell<Option<Vec<Deferred>>> = const { RefCell::new(None) };
    /// `call_soon` of the event loop that runs on this thread, once a server
    /// asked for hand-offs per loop iteration.
    static ITERATION: RefCell<Option<Py<PyAny>>> = const { RefCell::new(None) };
}

#[cfg(test)]
thread_local! {
    static HANDOFFS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

/// Ends the batch it opened, also when the batch unwinds.
struct Scope;

impl Drop for Scope {
    fn drop(&mut self) {
        end_batch();
    }
}

/// Run `body` with replies and spawns collected, then hand them to Tokio at
/// once. A nested call joins the batch already open.
pub(crate) fn batch<R>(body: impl FnOnce() -> R) -> R {
    let opened = PENDING.with(|pending| {
        let mut pending = pending.borrow_mut();
        if pending.is_some() {
            return false;
        }
        *pending = Some(Vec::new());
        true
    });
    let _scope = opened.then(|| Scope);
    body()
}

/// Run a Python callable inside [`batch`] so replies and spawns queued during
/// the call flush immediately when it returns or raises.
#[pyfunction]
pub(crate) fn handoff_batch(body: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    batch(|| body.call0().map(Bound::unbind))
}

/// Give `sent` to the connection waiting on `sender`. `false` when nobody
/// waits any more; within a batch that is known when the reply is queued.
pub(crate) fn reply(sender: oneshot::Sender<Sent>, sent: Sent) -> bool {
    match defer(Deferred::Reply(sender, sent)) {
        Ok(waiting) => waiting,
        Err(Deferred::Reply(sender, sent)) => sender.send(sent).is_ok(),
        Err(other) => {
            other.run();
            true
        }
    }
}

/// Spawn `task` on the shared runtime.
pub(crate) fn spawn(task: impl Future<Output = ()> + Send + 'static) {
    let task: Task = Box::pin(task);
    if let Err(Deferred::Spawn(task)) = defer(Deferred::Spawn(task)) {
        pyo3_async_runtimes::tokio::get_runtime().spawn(task);
    }
}

/// [`spawn`] a task that [`Abort::abort`] drops, whether it is still queued in
/// a batch or already running.
pub(crate) fn spawn_abortable(task: impl Future<Output = ()> + Send + 'static) -> Arc<Abort> {
    let abort = Arc::new(Abort::default());
    spawn(Abortable {
        task: Box::pin(task),
        abort: Arc::clone(&abort),
    });
    abort
}

/// Stops a task started by [`spawn_abortable`].
#[derive(Default)]
pub(crate) struct Abort {
    aborted: AtomicBool,
    waker: Mutex<Option<Waker>>,
}

impl Abort {
    /// Drop the task at its next poll, as `tokio::task::AbortHandle::abort`.
    pub(crate) fn abort(&self) {
        self.aborted.store(true, Ordering::Release);
        let waker = self.waker.lock().unwrap_or_else(|e| e.into_inner()).take();
        if let Some(waker) = waker {
            waker.wake();
        }
    }
}

struct Abortable {
    task: Task,
    abort: Arc<Abort>,
}

impl Future for Abortable {
    type Output = ();

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        if self.abort.aborted.load(Ordering::Acquire) {
            return Poll::Ready(());
        }
        *self.abort.waker.lock().unwrap_or_else(|e| e.into_inner()) = Some(cx.waker().clone());
        // An abort between the check and the registration found no waker.
        if self.abort.aborted.load(Ordering::Acquire) {
            return Poll::Ready(());
        }
        self.task.as_mut().poll(cx)
    }
}

/// Queue `item` in the open batch; `Err` gives it back when there is none.
/// `Ok` carries whether a queued reply still has a receiver. On a thread whose
/// loop takes hand-offs per iteration, the first item opens a batch that a
/// callback scheduled on the loop ends.
fn defer(item: Deferred) -> Result<bool, Deferred> {
    let waiting = match &item {
        Deferred::Reply(sender, _) => !sender.is_closed(),
        Deferred::Spawn(_) => true,
    };
    let item = match push(item) {
        Ok(()) => return Ok(waiting),
        Err(item) => item,
    };
    if !open_iteration() {
        return Err(item);
    }
    push(item).map(|()| waiting)
}

fn push(item: Deferred) -> Result<(), Deferred> {
    PENDING.with(|pending| match pending.borrow_mut().as_mut() {
        Some(items) => {
            items.push(item);
            Ok(())
        }
        None => Err(item),
    })
}

/// Hand what this thread queues to Tokio once per iteration of `event_loop`,
/// which must be the loop running on this thread. While another loop runs on
/// the thread instead, hand-offs go at once.
pub(crate) fn per_iteration(event_loop: &Bound<'_, PyAny>) {
    let event_loop = event_loop.clone().unbind();
    ITERATION.with(|slot| *slot.borrow_mut() = Some(event_loop));
}

/// Open a batch ended by [`Flush`] on this thread's loop. `false` without such
/// a loop, while it is not the one running, or once it refuses callbacks (it
/// closed).
fn open_iteration() -> bool {
    if ITERATION.with(|slot| slot.borrow().is_none()) {
        return false;
    }
    Python::attach(|py| {
        let Some(event_loop) =
            ITERATION.with(|slot| slot.borrow().as_ref().map(|l| l.clone_ref(py)))
        else {
            return false;
        };
        let event_loop = event_loop.bind(py);
        let running = event_loop
            .call_method0("is_running")
            .and_then(|r| r.is_truthy());
        if !matches!(running, Ok(true)) {
            let closed = event_loop
                .call_method0("is_closed")
                .and_then(|c| c.is_truthy());
            if !matches!(closed, Ok(false)) {
                ITERATION.with(|slot| slot.borrow_mut().take());
            }
            return false;
        }
        let flush = Flush {
            ran: AtomicBool::new(false),
        };
        PENDING.with(|pending| *pending.borrow_mut() = Some(Vec::new()));
        if event_loop.call_method1("call_soon", (flush,)).is_ok() {
            return true;
        }
        // `flush` was dropped with the refused call and ended the batch.
        ITERATION.with(|slot| slot.borrow_mut().take());
        false
    })
}

/// Ends the batch of a loop iteration: when the loop runs it, or when the loop
/// drops it unrun (it closed), so a queued reply is never lost.
#[pyclass(module = "hinoki.core", name = "_HandoffFlush", frozen)]
struct Flush {
    ran: AtomicBool,
}

#[pymethods]
impl Flush {
    fn __call__(&self) {
        self.ran.store(true, Ordering::Relaxed);
        end_batch();
    }
}

impl Drop for Flush {
    fn drop(&mut self) {
        if !self.ran.load(Ordering::Relaxed) {
            end_batch();
        }
    }
}

fn end_batch() {
    if let Some(items) = PENDING.with(|pending| pending.borrow_mut().take()) {
        hand_over(items);
    }
}

/// Hand over at once what this thread queued, keeping its batch open; for a
/// caller on this thread that is about to look for a reply it sent.
pub(crate) fn deliver_queued() {
    let items = PENDING.with(|pending| pending.borrow_mut().as_mut().map(std::mem::take));
    for item in items.into_iter().flatten() {
        match item {
            Deferred::Spawn(task) => {
                pyo3_async_runtimes::tokio::get_runtime().spawn(task);
            }
            reply => reply.run(),
        }
    }
}

/// One item goes over as it is (a single wake either way); more go inside a
/// single task, where each reply and spawn is a local, syscall-free wake.
fn hand_over(items: Vec<Deferred>) {
    #[cfg(test)]
    HANDOFFS.with(|count| count.set(count.get() + usize::from(!items.is_empty())));
    if items.len() > 1 {
        pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
            for item in items {
                item.run();
            }
        });
        return;
    }
    match items.into_iter().next() {
        Some(Deferred::Spawn(task)) => {
            pyo3_async_runtimes::tokio::get_runtime().spawn(task);
        }
        Some(reply) => reply.run(),
        None => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::Reply;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    fn answer(status: u16) -> Sent {
        Sent::Whole(Reply {
            status,
            headers: Vec::new(),
            body: bytes::Bytes::new(),
        })
    }

    fn handoffs() -> usize {
        HANDOFFS.with(std::cell::Cell::get)
    }

    fn status_of(sent: Sent) -> u16 {
        match sent {
            Sent::Whole(reply) => reply.status,
            _ => 0,
        }
    }

    #[test]
    fn a_batch_hands_every_reply_over_with_one_wake() {
        let before = handoffs();
        let (senders, receivers): (Vec<_>, Vec<_>) = (0..5).map(|_| oneshot::channel()).unzip();
        let mut receivers = receivers;
        batch(|| {
            for (n, sender) in senders.into_iter().enumerate() {
                assert!(reply(sender, answer(200 + n as u16)));
            }
            assert!(receivers.iter_mut().all(|rx| rx.try_recv().is_err()));
        });
        assert_eq!(handoffs() - before, 1);
        let runtime = pyo3_async_runtimes::tokio::get_runtime();
        let statuses: Vec<u16> = runtime.block_on(async move {
            let mut out = Vec::new();
            for rx in receivers {
                out.push(status_of(rx.await.unwrap()));
            }
            out
        });
        assert_eq!(statuses, vec![200, 201, 202, 203, 204]);
    }

    #[test]
    fn outside_a_batch_a_reply_goes_at_once() {
        let before = handoffs();
        let (sender, mut receiver) = oneshot::channel();
        assert!(reply(sender, answer(204)));
        assert_eq!(status_of(receiver.try_recv().unwrap()), 204);
        assert_eq!(handoffs(), before);
    }

    #[test]
    fn a_reply_nobody_waits_for_reports_false() {
        let (sender, receiver) = oneshot::channel();
        drop(receiver);
        assert!(!batch(|| reply(sender, answer(200))));
    }

    #[test]
    fn nested_batches_and_spawns_share_one_handoff() {
        let before = handoffs();
        let ran = Arc::new(AtomicUsize::new(0));
        let (done, finished) = std::sync::mpsc::channel();
        batch(|| {
            for _ in 0..3 {
                let ran = ran.clone();
                let done = done.clone();
                batch(|| {
                    spawn(async move {
                        ran.fetch_add(1, Ordering::SeqCst);
                        let _ = done.send(());
                    });
                });
            }
            assert_eq!(ran.load(Ordering::SeqCst), 0);
        });
        assert_eq!(handoffs() - before, 1);
        for _ in 0..3 {
            finished
                .recv_timeout(std::time::Duration::from_secs(5))
                .unwrap();
        }
        assert_eq!(ran.load(Ordering::SeqCst), 3);
    }

    #[test]
    fn a_batch_that_panics_still_hands_its_replies_over() {
        let (sender, mut receiver) = oneshot::channel();
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            batch(|| {
                reply(sender, answer(500));
                panic!("handler bug");
            })
        }));
        assert!(outcome.is_err());
        assert_eq!(status_of(receiver.try_recv().unwrap()), 500);
    }

    /// A fresh event loop whose iterations batch this thread's hand-offs.
    fn iterating_loop(py: Python<'_>) -> Bound<'_, PyAny> {
        let event_loop = py
            .import("asyncio")
            .unwrap()
            .call_method0("new_event_loop")
            .unwrap();
        per_iteration(&event_loop);
        event_loop
    }

    /// Run `body` as a callback of `event_loop`, then let the loop go on until
    /// `asyncio.sleep(0)` completes (or the loop is stopped).
    fn in_loop(
        py: Python<'_>,
        event_loop: &Bound<'_, PyAny>,
        body: impl FnOnce() + Send + 'static,
    ) -> PyResult<Py<PyAny>> {
        let body = Mutex::new(Some(body));
        let callback = pyo3::types::PyCFunction::new_closure(py, None, None, move |_, _| {
            if let Some(body) = body.lock().unwrap().take() {
                body();
            }
        })
        .unwrap();
        event_loop.call_method1("call_soon", (callback,)).unwrap();
        let sleep = py.import("asyncio")?.call_method1("sleep", (0,))?;
        event_loop
            .call_method1("run_until_complete", (sleep,))
            .map(Bound::unbind)
    }

    type Receivers = Arc<Mutex<Vec<oneshot::Receiver<Sent>>>>;

    /// Reply to `count` waiting receivers, all still empty when the callback
    /// ends.
    fn reply_to(count: usize, status: u16, receivers: &Receivers) -> impl FnOnce() + Send {
        let receivers = Arc::clone(receivers);
        move || {
            let mut receivers = receivers.lock().unwrap();
            for _ in 0..count {
                let (sender, receiver) = oneshot::channel();
                assert!(reply(sender, answer(status)));
                receivers.push(receiver);
            }
            assert!(receivers.iter_mut().all(|rx| rx.try_recv().is_err()));
        }
    }

    #[test]
    fn replies_of_one_loop_iteration_go_over_together() {
        Python::initialize();
        Python::attach(|py| {
            let event_loop = iterating_loop(py);
            let before = handoffs();
            let receivers = Receivers::default();
            in_loop(py, &event_loop, reply_to(3, 200, &receivers)).unwrap();
            assert_eq!(handoffs() - before, 1);
            let receivers = std::mem::take(&mut *receivers.lock().unwrap());
            let runtime = pyo3_async_runtimes::tokio::get_runtime();
            py.detach(|| {
                runtime.block_on(async move {
                    for rx in receivers {
                        assert_eq!(status_of(rx.await.unwrap()), 200);
                    }
                });
            });
            event_loop.call_method0("close").unwrap();
        });
    }

    #[test]
    fn a_loop_closed_before_its_iteration_ended_still_hands_replies_over() {
        Python::initialize();
        Python::attach(|py| {
            let event_loop = iterating_loop(py);
            let receivers = Receivers::default();
            let stop = event_loop.getattr("stop").unwrap().unbind();
            let answer_one = reply_to(1, 200, &receivers);
            let stopped = in_loop(py, &event_loop, move || {
                answer_one();
                Python::attach(|py| stop.call0(py).unwrap());
            });
            assert!(stopped.is_err(), "the loop stopped before sleep(0) ended");
            let mut receiver = receivers.lock().unwrap().pop().unwrap();
            assert!(receiver.try_recv().is_err());
            event_loop.call_method0("close").unwrap();
            assert_eq!(status_of(receiver.try_recv().unwrap()), 200);
            let (sender, mut receiver) = oneshot::channel();
            assert!(reply(sender, answer(204)));
            assert_eq!(status_of(receiver.try_recv().unwrap()), 204);
            assert!(ITERATION.with(|slot| slot.borrow().is_none()));
        });
    }

    #[test]
    fn an_aborted_task_is_dropped_queued_or_running() {
        struct Dropped(std::sync::mpsc::Sender<&'static str>);
        impl Drop for Dropped {
            fn drop(&mut self) {
                let _ = self.0.send("dropped");
            }
        }
        let (events, seen) = std::sync::mpsc::channel();
        let queued = batch(|| {
            let events = events.clone();
            let abort = spawn_abortable(async move {
                let _ = events.send("ran");
            });
            abort.abort();
            abort
        });
        drop(queued);
        let (started, running) = oneshot::channel::<()>();
        let guard = Dropped(events);
        let abort = spawn_abortable(async move {
            let _guard = guard;
            let _ = started.send(());
            std::future::pending::<()>().await;
        });
        pyo3_async_runtimes::tokio::get_runtime()
            .block_on(running)
            .unwrap();
        abort.abort();
        let timeout = std::time::Duration::from_secs(5);
        assert_eq!(seen.recv_timeout(timeout).unwrap(), "dropped");
        let quiet = std::time::Duration::from_millis(50);
        assert!(seen.recv_timeout(quiet).is_err());
    }

    #[test]
    fn handoff_batch_flushes_on_return_and_on_python_exception() {
        Python::initialize();
        Python::attach(|py| {
            let (sender, mut receiver) = oneshot::channel();
            let sender = Mutex::new(Some(sender));
            let body = pyo3::types::PyCFunction::new_closure(
                py,
                None,
                None,
                move |_args, _kwargs| -> PyResult<i32> {
                    if let Some(tx) = sender.lock().unwrap().take() {
                        assert!(reply(tx, answer(201)));
                    }
                    Ok(42)
                },
            )
            .unwrap();
            let result: i32 = handoff_batch(body.as_any()).unwrap().extract(py).unwrap();
            assert_eq!(result, 42);
            assert_eq!(status_of(receiver.try_recv().unwrap()), 201);

            let (sender, mut receiver) = oneshot::channel();
            let sender = Mutex::new(Some(sender));
            let failing = pyo3::types::PyCFunction::new_closure(
                py,
                None,
                None,
                move |_args, _kwargs| -> PyResult<()> {
                    if let Some(tx) = sender.lock().unwrap().take() {
                        assert!(reply(tx, answer(500)));
                    }
                    Err(pyo3::exceptions::PyRuntimeError::new_err("boom"))
                },
            )
            .unwrap();
            assert!(handoff_batch(failing.as_any()).is_err());
            assert_eq!(status_of(receiver.try_recv().unwrap()), 500);
        });
    }
}
