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
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

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

/// The part shared with Tokio tasks: Rust data only. It owns both ends of the
/// wake socket, so a late write never meets a closed peer (no SIGPIPE).
struct Shared {
    queue: Queue<Completion>,
    writer: UnixStream,
    reader: UnixStream,
}

impl Shared {
    fn push(&self, completion: Completion) {
        // The queue is unbounded: a result must never be refused, and the
        // number of pending calls is already bounded by callers and pool.
        if let Ok(Push::Wake) = self.queue.push(completion) {
            // `WouldBlock` means a wake byte is already pending.
            let _ = (&self.writer).write(&[1]);
        }
    }

    fn clear_wake(&self) {
        let mut buffer = [0_u8; 64];
        let mut reader = &self.reader;
        while matches!(reader.read(&mut buffer), Ok(n) if n > 0) {}
    }
}

struct Pending {
    future: Py<PyAny>,
    abort: AbortHandle,
    cancelled: Arc<AtomicBool>,
}

/// One per asyncio loop. Its slab is only touched on that loop's thread; the
/// mutex is never contended and exists so the type is `Send` for `#[pyclass]`.
struct Completer {
    event_loop: Py<PyAny>,
    shared: Arc<Shared>,
    slab: Mutex<HashMap<u64, Pending>>,
    next_id: AtomicU64,
}

impl Completer {
    fn take(&self, id: u64) -> Option<Pending> {
        self.slab
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&id)
    }
}

enum Entry {
    Ready(Arc<Completer>),
    /// This loop has no `add_reader`: use the library path.
    Unsupported(Py<PyAny>),
}

thread_local! {
    /// The loops that made database calls on this thread. Closed loops are
    /// dropped at the next call.
    static LOOPS: RefCell<Vec<Entry>> = const { RefCell::new(Vec::new()) };
}

/// Runs on the loop thread when the wake socket becomes readable.
#[pyclass(module = "hinoki.core")]
struct Drain {
    completer: Arc<Completer>,
}

#[pymethods]
impl Drain {
    fn __call__(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let completer = slf.borrow().completer.clone();
        completer.shared.clear_wake();
        let batch = completer.shared.queue.drain(BATCH);
        let full = batch.len() == BATCH;
        for completion in batch {
            resolve(py, &completer, completion);
        }
        if full {
            // Items may remain and the wake byte is already consumed.
            completer.event_loop.call_method1(py, "call_soon", (slf,))?;
        }
        Ok(())
    }
}

/// Cancels the Tokio task when its asyncio future is cancelled.
#[pyclass(module = "hinoki.core")]
struct Cancel {
    id: u64,
    completer: Arc<Completer>,
}

#[pymethods]
impl Cancel {
    fn __call__(&self, _future: &Bound<'_, PyAny>) {
        if let Some(pending) = self.completer.take(self.id) {
            // Set the flag first: an abort must not look like a panic.
            pending.cancelled.store(true, Ordering::Release);
            pending.abort.abort();
        }
    }
}

fn resolve(py: Python<'_>, completer: &Completer, completion: Completion) {
    // No entry: the future was cancelled and its result is dropped here.
    let Some(pending) = completer.take(completion.id) else {
        return;
    };
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

fn new_entry(py: Python<'_>, event_loop: &Bound<'_, PyAny>) -> PyResult<Entry> {
    let (reader, writer) = UnixStream::pair()?;
    reader.set_nonblocking(true)?;
    writer.set_nonblocking(true)?;
    let fd = reader.as_raw_fd();
    let completer = Arc::new(Completer {
        event_loop: event_loop.clone().unbind(),
        shared: Arc::new(Shared {
            queue: Queue::new(usize::MAX),
            writer,
            reader,
        }),
        slab: Mutex::new(HashMap::new()),
        next_id: AtomicU64::new(0),
    });
    let drain = Bound::new(
        py,
        Drain {
            completer: completer.clone(),
        },
    )?;
    if event_loop.call_method1("add_reader", (fd, drain)).is_err() {
        return Ok(Entry::Unsupported(event_loop.clone().unbind()));
    }
    Ok(Entry::Ready(completer))
}

fn is_closed(py: Python<'_>, event_loop: &Py<PyAny>) -> bool {
    event_loop
        .bind(py)
        .call_method0("is_closed")
        .and_then(|c| c.is_truthy())
        .unwrap_or(true)
}

/// The entry of the running loop; entries of closed loops are dropped here,
/// with the GIL held.
fn entry_for(py: Python<'_>, event_loop: &Bound<'_, PyAny>) -> PyResult<Option<Arc<Completer>>> {
    let mut loops = LOOPS.with(|l| std::mem::take(&mut *l.borrow_mut()));
    loops.retain(|entry| match entry {
        Entry::Ready(c) => !is_closed(py, &c.event_loop),
        Entry::Unsupported(l) => !is_closed(py, l),
    });
    let found = loops.iter().find_map(|entry| match entry {
        Entry::Ready(c) if c.event_loop.bind(py).is(event_loop) => Some(Some(c.clone())),
        Entry::Unsupported(l) if l.bind(py).is(event_loop) => Some(None),
        _ => None,
    });
    let result = match found {
        Some(known) => Ok(known),
        None => new_entry(py, event_loop).map(|entry| {
            let known = match &entry {
                Entry::Ready(c) => Some(c.clone()),
                Entry::Unsupported(_) => None,
            };
            loops.push(entry);
            known
        }),
    };
    LOOPS.with(|l| *l.borrow_mut() = loops);
    result
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
    let Some(completer) = entry_for(py, &event_loop)? else {
        return pyo3_async_runtimes::tokio::future_into_py(py, async move {
            match fut.await {
                Ok(value) => Python::attach(|py| convert(py, value)),
                Err(e) => Python::attach(|py| Err(db_error(py, &e))),
            }
        });
    };
    let future = event_loop.call_method0("create_future")?;
    let cancelled = Arc::new(AtomicBool::new(false));
    let id = completer.next_id.fetch_add(1, Ordering::Relaxed);
    let shared = completer.shared.clone();
    let task = {
        let (shared, cancelled) = (shared.clone(), cancelled.clone());
        async move {
            let mut guard = Guard {
                shared: shared.clone(),
                id,
                cancelled,
                sent: false,
            };
            let convert: Convert = match fut.await {
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
    completer
        .slab
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .insert(
            id,
            Pending {
                future: future.clone().unbind(),
                abort,
                cancelled,
            },
        );
    future.call_method1("add_done_callback", (Cancel { id, completer },))?;
    Ok(future)
}
