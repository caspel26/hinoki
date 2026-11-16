//! Completion of native awaitables on the asyncio loop thread.
//!
//! A database call runs on the shared Tokio runtime and never touches Python:
//! its outcome is pushed, as plain Rust data plus a conversion closure, onto a
//! per-loop queue. One byte written to a socket pair wakes the loop, whose
//! reader callback converts the outcomes and resolves the asyncio futures
//! directly. The futures live in a slab that only the loop thread touches, so no
//! Python object is created, held or dropped on a Tokio thread.
//!
//! A loop without `add_reader` (for example the Windows proactor loop) uses
//! `pyo3_async_runtimes` unchanged: the fast path is an optimization, never a
//! requirement.

use std::cell::RefCell;
use std::collections::HashMap;
use std::future::Future;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use hinoki_orm::{DbError, ErrorKind};
use hinoki_server::queue::Push;
use hinoki_server::Queue;
use pyo3::prelude::*;
use tokio::task::AbortHandle;

use crate::db::db_error;

/// Completions resolved per callback before yielding back to the loop.
const BATCH: usize = 256;

/// Builds the Python result on the loop thread, from data produced off it.
type Convert = Box<dyn FnOnce(Python<'_>) -> PyResult<Py<PyAny>> + Send>;

struct Completion {
    id: u64,
    convert: Convert,
}

/// The part shared with Tokio tasks: Rust data only.
struct Shared {
    queue: Queue<Completion>,
    writer: UnixStream,
}

impl Shared {
    fn push(&self, completion: Completion) {
        // The queue is unbounded: a result must never be refused, and the
        // number of pending calls is already bounded by callers and pool.
        if let Ok(Push::Wake) = self.queue.push(completion) {
            // `WouldBlock` means a wake byte is already pending; any other
            // error means the loop is gone and nobody will read the result.
            let _ = (&self.writer).write(&[1]);
        }
    }
}

struct Pending {
    future: Py<PyAny>,
    abort: AbortHandle,
    cancelled: Arc<AtomicBool>,
}

struct LoopCompleter {
    event_loop: Py<PyAny>,
    shared: Arc<Shared>,
    reader: UnixStream,
    slab: HashMap<u64, Pending>,
    next_id: u64,
}

enum State {
    Ready(LoopCompleter),
    /// This loop has no `add_reader`: use the library path.
    Unsupported(Py<PyAny>),
}

thread_local! {
    static STATE: RefCell<Option<State>> = const { RefCell::new(None) };
}

/// Runs on the loop thread when the wake socket becomes readable.
#[pyclass(module = "hinoki.core")]
struct Drain;

#[pymethods]
impl Drain {
    fn __call__(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let more = STATE.with(|state| match state.borrow_mut().as_mut() {
            Some(State::Ready(completer)) => {
                clear_wake(&completer.reader);
                Some(completer.shared.clone())
            }
            _ => None,
        });
        let Some(shared) = more else { return Ok(()) };
        let batch = shared.queue.drain(BATCH);
        let full = batch.len() == BATCH;
        for completion in batch {
            resolve(py, completion);
        }
        if full {
            // Items may remain and the wake byte is already consumed.
            let event_loop = STATE.with(|s| match s.borrow().as_ref() {
                Some(State::Ready(c)) => Some(c.event_loop.clone_ref(py)),
                _ => None,
            });
            if let Some(event_loop) = event_loop {
                event_loop.call_method1(py, "call_soon", (slf,))?;
            }
        }
        Ok(())
    }
}

/// Cancels the Tokio task when its asyncio future is cancelled.
#[pyclass(module = "hinoki.core")]
struct Cancel {
    id: u64,
}

#[pymethods]
impl Cancel {
    fn __call__(&self, _future: &Bound<'_, PyAny>) {
        let pending = STATE.with(|state| match state.borrow_mut().as_mut() {
            Some(State::Ready(c)) => c.slab.remove(&self.id),
            _ => None,
        });
        if let Some(pending) = pending {
            // Set the flag first: an abort must not look like a panic.
            pending.cancelled.store(true, Ordering::Release);
            pending.abort.abort();
        }
    }
}

fn clear_wake(reader: &UnixStream) {
    let mut buffer = [0_u8; 64];
    let mut reader = reader;
    while matches!(reader.read(&mut buffer), Ok(n) if n > 0) {}
}

fn resolve(py: Python<'_>, completion: Completion) {
    let pending = STATE.with(|state| match state.borrow_mut().as_mut() {
        Some(State::Ready(c)) => c.slab.remove(&completion.id),
        _ => None,
    });
    // No entry: the future was cancelled and its result is dropped here.
    let Some(pending) = pending else { return };
    let future = pending.future.bind(py);
    if !matches!(
        future.call_method0("done").and_then(|d| d.is_truthy()),
        Ok(false)
    ) {
        return;
    }
    let outcome = match (completion.convert)(py) {
        Ok(value) => future.call_method1("set_result", (value,)),
        Err(err) => future.call_method1("set_exception", (err.into_value(py),)),
    };
    if let Err(err) = outcome {
        err.print(py);
    }
}

/// Pushes an error if the task ends without a result and was not cancelled
/// (a panic unwinding through the task).
struct Guard {
    shared: Arc<Shared>,
    id: u64,
    cancelled: Arc<AtomicBool>,
    sent: bool,
}

impl Drop for Guard {
    fn drop(&mut self) {
        if self.sent || self.cancelled.load(Ordering::Acquire) {
            return;
        }
        let convert: Convert = Box::new(|py| {
            let err = DbError::new(ErrorKind::Other, "the native database task failed");
            Err(db_error(py, &err))
        });
        self.shared.push(Completion {
            id: self.id,
            convert,
        });
    }
}

fn new_completer(py: Python<'_>, event_loop: Bound<'_, PyAny>) -> PyResult<State> {
    let (reader, writer) = UnixStream::pair()?;
    reader.set_nonblocking(true)?;
    writer.set_nonblocking(true)?;
    let drain = Bound::new(py, Drain)?;
    if event_loop
        .call_method1("add_reader", (reader.as_raw_fd(), drain))
        .is_err()
    {
        return Ok(State::Unsupported(event_loop.unbind()));
    }
    Ok(State::Ready(LoopCompleter {
        event_loop: event_loop.unbind(),
        shared: Arc::new(Shared {
            queue: Queue::new(usize::MAX),
            writer,
        }),
        reader,
        slab: HashMap::new(),
        next_id: 0,
    }))
}

/// Replace the state when the running loop changed; dropped futures are
/// released here, with the GIL held.
fn ensure_state(py: Python<'_>, event_loop: &Bound<'_, PyAny>) -> PyResult<()> {
    let current = STATE.with(|state| match state.borrow().as_ref() {
        Some(State::Ready(c)) => c.event_loop.bind(py).is(event_loop),
        Some(State::Unsupported(l)) => l.bind(py).is(event_loop),
        None => false,
    });
    if current {
        return Ok(());
    }
    let old = STATE.with(|state| state.borrow_mut().take());
    if let Some(State::Ready(old)) = old {
        // Best effort: the old loop may already be closed.
        let _ = old
            .event_loop
            .bind(py)
            .call_method1("remove_reader", (old.reader.as_raw_fd(),));
    }
    let fresh = new_completer(py, event_loop.clone())?;
    STATE.with(|state| *state.borrow_mut() = Some(fresh));
    Ok(())
}

/// Run `fut` on the shared runtime and return an asyncio future for it.
/// `convert` turns its success value into a Python object on the loop thread.
pub fn submit<F, T, C>(py: Python<'_>, fut: F, convert: C) -> PyResult<Bound<'_, PyAny>>
where
    F: Future<Output = Result<T, DbError>> + Send + 'static,
    T: Send + 'static,
    C: FnOnce(Python<'_>, T) -> PyResult<Py<PyAny>> + Send + 'static,
{
    let event_loop = py.import("asyncio")?.call_method0("get_running_loop")?;
    ensure_state(py, &event_loop)?;
    let unsupported = STATE.with(|s| matches!(s.borrow().as_ref(), Some(State::Unsupported(_))));
    if unsupported {
        return pyo3_async_runtimes::tokio::future_into_py(py, async move {
            match fut.await {
                Ok(value) => Python::attach(|py| convert(py, value)),
                Err(e) => Python::attach(|py| Err(db_error(py, &e))),
            }
        });
    }
    let future = event_loop.call_method0("create_future")?;
    let cancelled = Arc::new(AtomicBool::new(false));
    let (id, shared) = STATE.with(|state| match state.borrow_mut().as_mut() {
        Some(State::Ready(c)) => {
            c.next_id += 1;
            (c.next_id, c.shared.clone())
        }
        _ => unreachable!("state was just ensured"),
    });
    let task = {
        let (shared, cancelled) = (shared.clone(), cancelled.clone());
        async move {
            let mut guard = Guard {
                shared: shared.clone(),
                id,
                cancelled,
                sent: false,
            };
            let outcome = fut.await;
            let convert: Convert = match outcome {
                Ok(value) => Box::new(move |py| convert(py, value)),
                Err(e) => Box::new(move |py| Err(db_error(py, &e))),
            };
            guard.sent = true;
            shared.push(Completion { id, convert });
        }
    };
    let abort = pyo3_async_runtimes::tokio::get_runtime()
        .spawn(task)
        .abort_handle();
    STATE.with(|state| {
        if let Some(State::Ready(c)) = state.borrow_mut().as_mut() {
            c.slab.insert(
                id,
                Pending {
                    future: future.clone().unbind(),
                    abort,
                    cancelled,
                },
            );
        }
    });
    future.call_method1("add_done_callback", (Cancel { id },))?;
    Ok(future)
}
