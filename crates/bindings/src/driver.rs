//! Driving a coroutine without an event loop.
//!
//! A handler marked `@native` may await only [`NativeWait`] objects. Awaiting
//! one yields the object itself to whoever calls `send`; the driver recognises
//! it by type (an `isinstance(Future)` test cannot tell it from
//! `asyncio.sleep`), runs the Rust future it carries, and resumes the
//! coroutine with the converted result or the converted error. Anything else a
//! coroutine yields is refused: once advanced, a coroutine cannot be handed to
//! asyncio, so there is no fallback after the first `send`.

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
#[pyclass(module = "hinoki.core", name = "_NativeWait")]
pub struct NativeWait {
    work: Mutex<Option<Work>>,
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
        }
    }

    /// The work, once: a second take means the coroutine yielded it twice.
    pub fn take(&self) -> Option<Work> {
        self.work.lock().unwrap_or_else(|e| e.into_inner()).take()
    }
}

#[pymethods]
impl NativeWait {
    fn __await__(slf: Bound<'_, Self>) -> PyResult<Py<WaitIter>> {
        Py::new(
            slf.py(),
            WaitIter {
                wait: Some(slf.unbind()),
            },
        )
    }
}

/// The iterator `await` runs: it yields the wait once, then ends with the
/// value the driver sends back, or raises the error it throws.
#[pyclass(module = "hinoki.core", name = "_NativeWaitIter")]
pub struct WaitIter {
    wait: Option<Py<NativeWait>>,
}

#[pymethods]
impl WaitIter {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Option<Py<NativeWait>>> {
        self.send(py.None().into_bound(py))
    }

    fn send(&mut self, value: Bound<'_, PyAny>) -> PyResult<Option<Py<NativeWait>>> {
        if let Some(wait) = self.wait.take() {
            return Ok(Some(wait));
        }
        Err(PyStopIteration::new_err((value.unbind(),)))
    }

    fn throw(&mut self, error: Bound<'_, PyAny>) -> PyResult<Option<Py<NativeWait>>> {
        self.wait = None;
        Err(PyErr::from_value(error))
    }

    fn close(&mut self) {
        self.wait = None;
    }
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
}

/// Advance `coro` once. A first step resumes with `None`.
pub fn advance(py: Python<'_>, coro: &Bound<'_, PyAny>, resume: Resume) -> PyResult<Step> {
    let outcome = match resume {
        Resume::Value(value) => coro.call_method1("send", (value,)),
        Resume::Error(error) => coro.call_method1("throw", (error.into_value(py),)),
    };
    match outcome {
        Ok(yielded) => match yielded.cast_into::<NativeWait>() {
            Ok(wait) => Ok(Step::Wait(wait.unbind())),
            Err(other) => {
                let yielded = other.into_inner();
                let name = yielded.get_type().name()?.to_string();
                // The coroutine cannot go on; close it so `finally` runs.
                let _ = coro.call_method0("close");
                Err(PyRuntimeError::new_err(format!(
                    "a native handler awaited a {name}, which is not a native awaitable"
                )))
            }
        },
        Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
            let value = error.value(py).getattr("value")?;
            Ok(Step::Done(value.unbind()))
        }
        Err(error) => Err(error),
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
        let wait = match advance(py, &coro, resume)? {
            Step::Done(value) => return Ok(value),
            Step::Wait(wait) => wait,
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
