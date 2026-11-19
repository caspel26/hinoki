//! Serving a coroutine from Rust.
//!
//! [`run_native`] takes a coroutine that awaits [`NativeWait`] objects and
//! carries it to its end without an event loop: the first steps run on the
//! calling thread, every wait runs on the shared Tokio runtime, and each later
//! step runs on the runtime thread that finished the wait, with the GIL held.
//!
//! Three things can end the drive early, and all of them act at a suspension
//! point, as asyncio cancellation does: the deadline, [`NativeRun::cancel`],
//! and a coroutine that awaits something that is not a native wait. The first
//! two throw `asyncio.CancelledError` into the coroutine, so `finally` blocks
//! and `async with` exits (a transaction rollback) still run, and the drive
//! goes on until the coroutine ends. The third hands the coroutine over to
//! `on_foreign`, which continues it under asyncio.

use std::sync::Arc;
use std::time::{Duration, Instant};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio::sync::Notify;

use crate::driver::{advance, Resume, Step};

type Work = std::pin::Pin<Box<dyn std::future::Future<Output = Convert> + Send>>;
type Convert = Box<dyn FnOnce(Python<'_>) -> PyResult<Py<PyAny>> + Send>;

/// What stopped a drive early.
#[derive(Clone, Copy)]
enum Reason {
    Deadline,
    Cancelled,
}

impl Reason {
    fn name(self) -> &'static str {
        match self {
            Self::Deadline => "deadline",
            Self::Cancelled => "cancelled",
        }
    }
}

/// A handle on a running drive.
#[pyclass(module = "hinoki.core", name = "_NativeRun")]
pub struct NativeRun {
    stop: Arc<Notify>,
}

#[pymethods]
impl NativeRun {
    /// Cancel the coroutine at its next suspension. Safe from any thread and
    /// after the drive ended.
    fn cancel(&self) {
        self.stop.notify_one();
    }
}

/// What one drive shares between its steps.
struct Run {
    coro: Py<PyAny>,
    ctx: Option<Py<PyAny>>,
    on_done: Py<PyAny>,
    on_foreign: Py<PyAny>,
    deadline: Option<Instant>,
    stop: Arc<Notify>,
}

/// Where a drive goes after a step.
enum Next {
    Wait(Work),
    Over,
}

impl Run {
    /// Tell `on_done` how the drive ended; exactly once per drive.
    fn finish(&self, py: Python<'_>, outcome: PyResult<Py<PyAny>>, reason: Option<Reason>) {
        let reason = reason.map(Reason::name);
        let args = match outcome {
            Ok(value) => (value, py.None(), reason),
            Err(error) => (py.None(), error.into_value(py).into_any(), reason),
        };
        if let Err(error) = self.on_done.call1(py, args) {
            error.print(py);
        }
    }

    /// Advance once and decide what follows. The GIL is held.
    fn step(&self, py: Python<'_>, resume: Resume, reason: Option<Reason>) -> Next {
        let coro = self.coro.bind(py);
        let ctx = self.ctx.as_ref().map(|c| c.bind(py));
        match advance(py, coro, ctx, resume) {
            Ok(Step::Wait(wait)) => match wait.bind(py).borrow().take() {
                Some(work) => Next::Wait(work),
                None => {
                    let error = PyRuntimeError::new_err("a native wait was awaited twice");
                    self.finish(py, Err(error), reason);
                    Next::Over
                }
            },
            Ok(Step::Done(value)) => {
                self.finish(py, Ok(value), reason);
                Next::Over
            }
            Ok(Step::Foreign(yielded)) => {
                self.hand_over(py, yielded, reason);
                Next::Over
            }
            Err(error) => {
                self.finish(py, Err(error), reason);
                Next::Over
            }
        }
    }

    /// Give the coroutine and what it awaited to `on_foreign`, with the time
    /// left before the deadline. A coroutine that was already cancelled has
    /// nothing to continue: it ends as cancelled.
    fn hand_over(&self, py: Python<'_>, yielded: Py<PyAny>, reason: Option<Reason>) {
        let remaining = self
            .deadline
            .map(|d| d.saturating_duration_since(Instant::now()).as_secs_f64());
        let ctx = self.ctx.as_ref().map(|c| c.clone_ref(py));
        let handed = self
            .on_foreign
            .call1(py, (self.coro.clone_ref(py), ctx, yielded, remaining));
        if let Err(error) = handed {
            let _ = self.coro.call_method0(py, "close");
            self.finish(py, Err(error), reason);
        }
    }

    fn cancelled_error(py: Python<'_>) -> PyResult<PyErr> {
        let cancelled = py.import("asyncio")?.getattr("CancelledError")?;
        Ok(PyErr::from_value(cancelled.call0()?))
    }
}

enum Event {
    Ready(Convert),
    Stopped(Reason),
}

async fn until(deadline: Option<Instant>) {
    match deadline {
        Some(at) => tokio::time::sleep_until(at.into()).await,
        None => std::future::pending().await,
    }
}

/// Wait for `work`, unless the deadline or a cancel comes first. Once the
/// coroutine was cancelled the cleanup it runs is not cut short again.
async fn next_event(run: &Run, work: &mut Work, cancelling: bool) -> Event {
    if cancelling {
        return Event::Ready(work.await);
    }
    tokio::select! {
        convert = work => Event::Ready(convert),
        () = until(run.deadline) => Event::Stopped(Reason::Deadline),
        () = run.stop.notified() => Event::Stopped(Reason::Cancelled),
    }
}

/// Resume the coroutine with what `event` brought. Returns the next wait.
fn resume_with(run: &Run, event: Event, reason: &mut Option<Reason>) -> Next {
    Python::attach(|py| {
        let resume = match event {
            Event::Ready(convert) => match convert(py) {
                Ok(value) => Resume::Value(value),
                Err(error) => Resume::Error(error),
            },
            Event::Stopped(why) => {
                *reason = Some(why);
                Resume::Error(Run::cancelled_error(py).unwrap_or_else(|error| error))
            }
        };
        run.step(py, resume, *reason)
    })
}

/// Answers `on_done` if the drive task ends without having done it, so a
/// request never hangs on a bug.
struct Unfinished {
    run: Arc<Run>,
    done: bool,
}

impl Drop for Unfinished {
    fn drop(&mut self) {
        if self.done {
            return;
        }
        let _ = Python::try_attach(|py| {
            let error = PyRuntimeError::new_err("the native drive ended without a result");
            self.run.finish(py, Err(error), None);
        });
    }
}

async fn drive_waits(run: Arc<Run>, mut work: Work) {
    let mut guard = Unfinished {
        run: run.clone(),
        done: false,
    };
    let mut reason: Option<Reason> = None;
    loop {
        let event = next_event(&run, &mut work, reason.is_some()).await;
        match resume_with(&run, event, &mut reason) {
            Next::Wait(next) => work = next,
            Next::Over => {
                guard.done = true;
                return;
            }
        }
    }
}

/// Start driving `coro`.
///
/// `on_done(value, error, reason)` runs exactly once, on whichever thread
/// finished the drive: `error` is the exception the coroutine ended with (or
/// `None`), `reason` is `"deadline"` or `"cancelled"` when the coroutine was
/// cancelled on the way, else `None`. `on_foreign(coro, ctx, yielded,
/// remaining)` takes over a coroutine that awaited a non-native object;
/// `on_done` is then the caller's to arrange. `ctx` is a
/// `contextvars.Context` every step runs in.
#[pyfunction]
#[pyo3(signature = (coro, on_done, on_foreign, ctx=None, deadline_ms=None))]
pub fn run_native(
    py: Python<'_>,
    coro: Py<PyAny>,
    on_done: Py<PyAny>,
    on_foreign: Py<PyAny>,
    ctx: Option<Py<PyAny>>,
    deadline_ms: Option<u64>,
) -> NativeRun {
    let stop = Arc::new(Notify::new());
    let run = Arc::new(Run {
        coro,
        ctx,
        on_done,
        on_foreign,
        deadline: deadline_ms.map(|ms| Instant::now() + Duration::from_millis(ms)),
        stop: stop.clone(),
    });
    let first = run.step(py, Resume::Value(py.None()), None);
    if let Next::Wait(work) = first {
        pyo3_async_runtimes::tokio::get_runtime().spawn(drive_waits(run, work));
    }
    NativeRun { stop }
}
