//! Driving a coroutine without an event loop.
//!
//! Inside a driven coroutine, a database call yields a [`NativeWait`] to
//! whoever calls `send`; the driver recognises it by type (an
//! `isinstance(Future)` test cannot tell it from `asyncio.sleep`), runs the
//! Rust future it carries, and resumes the coroutine with the converted result
//! or the converted error. [`advance`] reports anything else the coroutine
//! yields as [`Step::Foreign`], so the caller decides: the test oracle
//! [`_drive`] refuses it, the serving driver hands the coroutine to asyncio.

use std::cell::Cell;
use std::future::Future;
use std::pin::Pin;
use std::sync::Mutex;
use std::time::Duration;

use pyo3::exceptions::{PyRuntimeError, PyStopIteration};
use pyo3::prelude::*;

/// Builds the Python outcome of finished work, with the GIL held.
type Convert = Box<dyn FnOnce(Python<'_>) -> PyResult<Py<PyAny>> + Send>;
type Work = Pin<Box<dyn Future<Output = Convert> + Send>>;

/// A pending piece of Rust work a coroutine awaits.
#[pyclass(module = "hinoki.core", name = "_NativeWait", frozen)]
pub struct NativeWait {
    work: Mutex<Option<Work>>,
    yielded: std::sync::atomic::AtomicBool,
}

impl NativeWait {
    /// Wrap `fut`; `convert` and `fail` run later, with the GIL held, on
    /// whichever thread resumes the coroutine.
    pub fn new<F, T, E, C, X>(fut: F, convert: C, fail: X) -> Self
    where
        F: Future<Output = Result<T, E>> + Send + 'static,
        T: Send + 'static,
        E: Send + 'static,
        C: FnOnce(Python<'_>, T) -> PyResult<Py<PyAny>> + Send + 'static,
        X: FnOnce(Python<'_>, E) -> PyErr + Send + 'static,
    {
        let work = async move {
            let convert: Convert = match fut.await {
                Ok(value) => Box::new(move |py| convert(py, value)),
                Err(error) => Box::new(move |py| Err(fail(py, error))),
            };
            convert
        };
        Self {
            work: Mutex::new(Some(Box::pin(work))),
            yielded: std::sync::atomic::AtomicBool::new(false),
        }
    }

    /// The work, once: a second take means the coroutine yielded it twice.
    pub fn take(&self) -> Option<Work> {
        self.work.lock().unwrap_or_else(|e| e.into_inner()).take()
    }
}

#[pymethods]
impl NativeWait {
    fn __await__(slf: Bound<'_, Self>) -> Bound<'_, Self> {
        slf
    }

    fn __iter__(slf: Bound<'_, Self>) -> Bound<'_, Self> {
        slf
    }

    fn __next__(slf: Bound<'_, Self>, py: Python<'_>) -> PyResult<Option<Py<Self>>> {
        Self::send(slf, py.None().into_bound(py))
    }

    fn send(slf: Bound<'_, Self>, value: Bound<'_, PyAny>) -> PyResult<Option<Py<Self>>> {
        if !slf
            .get()
            .yielded
            .swap(true, std::sync::atomic::Ordering::Relaxed)
        {
            return Ok(Some(slf.unbind()));
        }
        Err(PyStopIteration::new_err((value.unbind(),)))
    }

    fn throw(&self, error: Bound<'_, PyAny>) -> PyResult<Option<Py<Self>>> {
        self.yielded
            .store(true, std::sync::atomic::Ordering::Relaxed);
        Err(PyErr::from_value(error))
    }

    fn close(&self) {
        self.yielded
            .store(true, std::sync::atomic::Ordering::Relaxed);
    }
}

thread_local! {
    static DRIVING: Cell<usize> = const { Cell::new(0) };
}

/// Marks the code a coroutine step runs: database calls made meanwhile return
/// a [`NativeWait`] instead of an asyncio future.
struct Driving;

impl Driving {
    fn enter() -> Self {
        DRIVING.with(|d| d.set(d.get() + 1));
        Self
    }
}

impl Drop for Driving {
    fn drop(&mut self) {
        DRIVING.with(|d| d.set(d.get() - 1));
    }
}

/// Whether this thread is inside a step of [`advance`].
#[pyfunction]
pub fn driving() -> bool {
    DRIVING.with(|d| d.get() > 0)
}

/// How a coroutine resumes.
pub enum Resume {
    Value(Py<PyAny>),
    Error(PyErr),
}

/// Where a coroutine stands after one step.
pub enum Step {
    Done(Py<PyAny>),
    Wait(Py<NativeWait>),
    /// The coroutine awaited something that is not a [`NativeWait`].
    Foreign(Py<PyAny>),
}

/// Call `method` of `coro`, inside `ctx` when there is one.
fn call_in<'py>(
    py: Python<'py>,
    coro: &Bound<'py, PyAny>,
    ctx: Option<&Bound<'py, PyAny>>,
    method: &Bound<'py, pyo3::types::PyString>,
    arg: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let bound = coro.getattr(method)?;
    match ctx {
        Some(ctx) => ctx.call_method1(pyo3::intern!(py, "run"), (bound, arg)),
        None => bound.call1((arg,)),
    }
}

/// Advance `coro` once. A first step resumes with `None`.
pub fn advance<'py>(
    py: Python<'py>,
    coro: &Bound<'py, PyAny>,
    ctx: Option<&Bound<'py, PyAny>>,
    resume: Resume,
) -> PyResult<Step> {
    let _driving = Driving::enter();
    let outcome = match resume {
        Resume::Value(value) => call_in(
            py,
            coro,
            ctx,
            pyo3::intern!(py, "send"),
            value.into_bound(py),
        ),
        Resume::Error(error) => call_in(
            py,
            coro,
            ctx,
            pyo3::intern!(py, "throw"),
            error.into_value(py).into_bound(py).into_any(),
        ),
    };
    match outcome {
        Ok(yielded) => Ok(match yielded.cast_into::<NativeWait>() {
            Ok(wait) => Step::Wait(wait.unbind()),
            Err(other) => Step::Foreign(other.into_inner().unbind()),
        }),
        Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
            let value = error.value(py).getattr(pyo3::intern!(py, "value"))?;
            Ok(Step::Done(value.unbind()))
        }
        Err(error) => Err(error),
    }
}

/// The error of a coroutine that awaited a non-native object where only
/// native ones are allowed. The coroutine is closed so `finally` runs.
fn refuse(py: Python<'_>, coro: &Bound<'_, PyAny>, yielded: &Py<PyAny>) -> PyErr {
    let _ = coro.call_method0("close");
    match yielded.bind(py).get_type().name() {
        Ok(name) => PyRuntimeError::new_err(format!(
            "a native handler awaited a {name}, which is not a native awaitable"
        )),
        Err(error) => error,
    }
}

/// Run `coro` to its end on the calling thread, blocking on each wait with
/// the GIL released. With `cancel_after = n`, the coroutine gets
/// `asyncio.CancelledError` instead of the result of its `n`-th wait (counting
/// from zero). A test oracle for the driver, not a serving path.
#[pyfunction]
#[pyo3(signature = (coro, cancel_after=None))]
pub fn _drive(
    py: Python<'_>,
    coro: Bound<'_, PyAny>,
    cancel_after: Option<usize>,
) -> PyResult<Py<PyAny>> {
    let cancelled = py.import("asyncio")?.getattr("CancelledError")?;
    let mut resume = Resume::Value(py.None());
    let mut waits = 0_usize;
    loop {
        let wait = match advance(py, &coro, None, resume)? {
            Step::Done(value) => return Ok(value),
            Step::Wait(wait) => wait,
            Step::Foreign(yielded) => return Err(refuse(py, &coro, &yielded)),
        };
        let work = wait
            .bind(py)
            .borrow()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("a native wait was awaited twice"))?;
        if cancel_after == Some(waits) {
            resume = Resume::Error(PyErr::from_value(cancelled.call0()?));
            continue;
        }
        waits += 1;
        let convert = py.detach(|| pyo3_async_runtimes::tokio::get_runtime().block_on(work));
        resume = match convert(py) {
            Ok(value) => Resume::Value(value),
            Err(error) => Resume::Error(error),
        };
    }
}

/// A wait that finishes after `millis` with `value`, or fails with
/// `RuntimeError(value)` when `fail` is set. For tests of the driver.
#[pyfunction]
pub fn _native_wait(millis: u64, value: Py<PyAny>, fail: bool) -> NativeWait {
    let work = async move {
        tokio::time::sleep(Duration::from_millis(millis)).await;
        if fail {
            Err(())
        } else {
            Ok(value)
        }
    };
    NativeWait::new(
        work,
        |_py, value: Py<PyAny>| Ok(value),
        |_py, ()| PyRuntimeError::new_err("native wait failed"),
    )
}
