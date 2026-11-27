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

use std::io::{ErrorKind, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio::sync::{oneshot, Notify};

use crate::driver::{advance, Resume, Step};
use crate::handoff;

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

/// Where the steps after the first run.
enum Scheduler {
    /// A callable such as `loop.call_soon_threadsafe`, called once per step.
    Call(Py<PyAny>),
    /// The loop's [`NativeSteps`] queue.
    Steps(Arc<Steps>),
}

/// What one drive shares between its steps.
struct Run {
    coro: Py<PyAny>,
    ctx: Option<Py<PyAny>>,
    on_done: Py<PyAny>,
    on_foreign: Py<PyAny>,
    scheduler: Option<Scheduler>,
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
    /// left before the deadline and the reason it was cancelled, if it was: a
    /// cleanup that awaits an asyncio-only object continues there.
    fn hand_over(&self, py: Python<'_>, yielded: Py<PyAny>, reason: Option<Reason>) {
        let remaining = self
            .deadline
            .map(|d| d.saturating_duration_since(Instant::now()).as_secs_f64());
        let ctx = self.ctx.as_ref().map(|c| c.clone_ref(py));
        let handed = self.on_foreign.call1(
            py,
            (
                self.coro.clone_ref(py),
                ctx,
                yielded,
                remaining,
                reason.map(Reason::name),
            ),
        );
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
/// coroutine was cancelled the cleanup it runs is not cut short again, except
/// that a shutdown cancel still interrupts the cleanup of a missed deadline.
async fn next_event(run: &Run, work: &mut Work, reason: Option<Reason>) -> Event {
    match reason {
        Some(Reason::Cancelled) => return Event::Ready(work.await),
        Some(Reason::Deadline) => {
            return tokio::select! {
                convert = work => Event::Ready(convert),
                () = run.stop.notified() => Event::Stopped(Reason::Cancelled),
            };
        }
        None => {}
    }
    tokio::select! {
        convert = work => Event::Ready(convert),
        () = until(run.deadline) => Event::Stopped(Reason::Deadline),
        () = run.stop.notified() => Event::Stopped(Reason::Cancelled),
    }
}

/// Resume the coroutine with what `event` brought, on this thread. Returns
/// the next wait and whether the coroutine was cancelled.
fn resume_here(run: &Run, event: Event, reason: Option<Reason>) -> (Next, Option<Reason>) {
    Python::attach(|py| resume_on(py, run, event, reason))
}

fn resume_on(
    py: Python<'_>,
    run: &Run,
    event: Event,
    reason: Option<Reason>,
) -> (Next, Option<Reason>) {
    let mut reason = reason;
    let resume = match event {
        Event::Ready(convert) => match convert(py) {
            Ok(value) => Resume::Value(value),
            Err(error) => Resume::Error(error),
        },
        Event::Stopped(why) => {
            reason = Some(why);
            Resume::Error(Run::cancelled_error(py).unwrap_or_else(|error| error))
        }
    };
    (run.step(py, resume, reason), reason)
}

type Resumed = (Next, Option<Reason>);

/// The callback that runs one step on the thread of the event loop.
#[pyclass(module = "hinoki.core", name = "_NativeStep")]
struct StepCall {
    run: Arc<Run>,
    pending: Mutex<Option<(Event, Option<Reason>)>>,
    reply: Mutex<Option<oneshot::Sender<Resumed>>>,
}

#[pymethods]
impl StepCall {
    fn __call__(&self) {
        let pending = self
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take();
        let reply = self.reply.lock().unwrap_or_else(|e| e.into_inner()).take();
        if let (Some((event, reason)), Some(reply)) = (pending, reply) {
            let _ = reply.send(resume_here(&self.run, event, reason));
        }
    }
}

/// Resume the coroutine where the caller asked: on the loop thread through
/// the scheduler, else here. `None` means the step never ran and the drive
/// was already answered.
async fn resume_with(
    run: &Arc<Run>,
    scheduler: Option<&Py<PyAny>>,
    event: Event,
    reason: Option<Reason>,
) -> Option<Resumed> {
    let Some(scheduler) = scheduler else {
        return Some(resume_here(run, event, reason));
    };
    let (reply, wait) = oneshot::channel();
    let call = StepCall {
        run: run.clone(),
        pending: Mutex::new(Some((event, reason))),
        reply: Mutex::new(Some(reply)),
    };
    let scheduled = Python::attach(|py| {
        let call = Bound::new(py, call)?;
        scheduler.call1(py, (call,)).map(|_| ())
    });
    if let Err(error) = scheduled {
        Python::attach(|py| run.finish(py, Err(error), reason));
        return None;
    }
    match wait.await {
        Ok(resumed) => Some(resumed),
        Err(_) => {
            let error = PyRuntimeError::new_err("the event loop dropped a native step");
            Python::attach(|py| run.finish(py, Err(error), reason));
            None
        }
    }
}

/// One drive between its steps. Answers `on_done` if it is dropped before the
/// coroutine ended, so a request never hangs on a bug or on a loop that went
/// away with steps still queued.
struct Drive {
    run: Arc<Run>,
    reason: Option<Reason>,
    done: bool,
}

impl Drop for Drive {
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

/// Wait for `work` and resume the coroutine, until it ends. With a
/// [`NativeSteps`] queue the step goes there and this task ends: the loop
/// thread spawns the next wait, if there is one.
async fn drive_waits(mut drive: Drive, mut work: Work) {
    loop {
        let event = next_event(&drive.run, &mut work, drive.reason).await;
        let scheduler = match &drive.run.scheduler {
            Some(Scheduler::Steps(steps)) => {
                let steps = Arc::clone(steps);
                steps.push(Job { drive, event });
                return;
            }
            Some(Scheduler::Call(call)) => Some(call),
            None => None,
        };
        match resume_with(&drive.run, scheduler, event, drive.reason).await {
            Some((Next::Wait(next), now)) => {
                work = next;
                drive.reason = now;
            }
            _ => {
                drive.done = true;
                return;
            }
        }
    }
}

/// A step waiting for the loop thread.
struct Job {
    drive: Drive,
    event: Event,
}

impl Job {
    fn run(self, py: Python<'_>) {
        let Job { mut drive, event } = self;
        let (next, reason) = resume_on(py, &drive.run, event, drive.reason);
        drive.reason = reason;
        match next {
            Next::Wait(work) => handoff::spawn(drive_waits(drive, work)),
            Next::Over => drive.done = true,
        }
    }
}

/// Steps for one event loop. Tokio threads queue them without the GIL and
/// write one byte to a socket pair the loop watches only when the queue goes
/// from empty to non-empty; the loop's reader runs every queued step in one
/// callback. The queue holds at most one step per running drive. `None` once
/// the loop dropped the reader: a later step is dropped, which answers its
/// drive with an error.
struct Steps {
    jobs: Mutex<Option<Vec<Job>>>,
    wake: UnixStream,
    read: UnixStream,
    #[cfg(test)]
    wakes: std::sync::atomic::AtomicUsize,
}

impl Steps {
    fn new() -> std::io::Result<Self> {
        let (wake, read) = UnixStream::pair()?;
        wake.set_nonblocking(true)?;
        read.set_nonblocking(true)?;
        Ok(Self {
            jobs: Mutex::new(Some(Vec::new())),
            wake,
            read,
            #[cfg(test)]
            wakes: std::sync::atomic::AtomicUsize::new(0),
        })
    }

    fn jobs(&self) -> MutexGuard<'_, Option<Vec<Job>>> {
        self.jobs.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn push(&self, job: Job) {
        let mut jobs = self.jobs();
        let Some(queued) = jobs.as_mut() else {
            drop(jobs);
            drop(job);
            return;
        };
        let wake = queued.is_empty();
        queued.push(job);
        drop(jobs);
        if wake {
            #[cfg(test)]
            self.wakes.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            // `WouldBlock` means a wake is already pending.
            let _ = (&self.wake).write(&[1]);
        }
    }

    /// Empty the wake socket; call it before taking the steps, never after.
    fn clear(&self) {
        let mut buf = [0u8; 64];
        loop {
            match (&self.read).read(&mut buf) {
                Ok(0) => return,
                Ok(n) if n < buf.len() => return,
                Ok(_) => {}
                Err(error) if error.kind() == ErrorKind::Interrupted => {}
                Err(_) => return,
            }
        }
    }

    fn take(&self) -> Vec<Job> {
        self.jobs().as_mut().map(std::mem::take).unwrap_or_default()
    }

    fn close(&self) -> Vec<Job> {
        self.jobs().take().unwrap_or_default()
    }
}

/// The loop's reader callback for a [`NativeSteps`] queue.
#[pyclass(module = "hinoki.core", name = "_NativeStepsReader")]
struct StepsReader {
    steps: Arc<Steps>,
}

#[pymethods]
impl StepsReader {
    /// Run every queued step. Replies and the next waits go to Tokio together
    /// once the steps ran. Steps queued meanwhile wake the loop again.
    fn __call__(&self, py: Python<'_>) {
        self.steps.clear();
        let jobs = self.steps.take();
        handoff::batch(|| {
            for job in jobs {
                job.run(py);
            }
        });
    }
}

impl Drop for StepsReader {
    fn drop(&mut self) {
        // The loop let go of the reader (it closed): answer the steps it will
        // never run and refuse later ones.
        drop(self.steps.close());
    }
}

/// The steps of every drive started with it run on the thread of `loop`,
/// as with `scheduler=loop.call_soon_threadsafe`, but a Tokio thread queues a
/// step without taking the GIL and the loop runs all the queued steps in one
/// callback. It registers a reader on `loop`, kept as long as the loop is.
#[pyclass(module = "hinoki.core", name = "_NativeSteps", frozen)]
pub struct NativeSteps {
    steps: Arc<Steps>,
}

#[pymethods]
impl NativeSteps {
    #[new]
    fn new(py: Python<'_>, event_loop: &Bound<'_, PyAny>) -> PyResult<Self> {
        let steps = Arc::new(Steps::new().map_err(|e| PyRuntimeError::new_err(e.to_string()))?);
        let reader = Bound::new(
            py,
            StepsReader {
                steps: Arc::clone(&steps),
            },
        )?;
        event_loop.call_method1("add_reader", (steps.read.as_raw_fd(), reader))?;
        Ok(Self { steps })
    }
}

/// Perform only the first step of `coro` (`coro.send(None)` inside `ctx`)
/// without allocating `Arc<Run>`, `Arc<Notify>`, or `NativeRun`.
///
/// Returns `(0, value)` when the coroutine finished synchronously,
/// `(1, wait)` when it yielded a `NativeWait`, or `(2, yielded)` when it
/// yielded a foreign awaitable. Exceptions raised by the coroutine propagate.
#[pyfunction]
#[pyo3(signature = (coro, ctx=None))]
pub fn step_native_first<'py>(
    py: Python<'py>,
    coro: &Bound<'py, PyAny>,
    ctx: Option<&Bound<'py, PyAny>>,
) -> PyResult<(u8, Py<PyAny>)> {
    match advance(py, coro, ctx, Resume::Value(py.None()))? {
        Step::Done(value) => Ok((0, value)),
        Step::Wait(wait) => Ok((1, wait.into_any())),
        Step::Foreign(yielded) => Ok((2, yielded)),
    }
}

/// Continue a coroutine whose first step already yielded `wait` (from
/// [`step_native_first`]) on the shared Tokio runtime.
#[pyfunction]
#[pyo3(signature = (coro, wait, on_done, on_foreign, ctx=None, deadline_ms=None, scheduler=None))]
#[allow(clippy::too_many_arguments)]
pub fn continue_native(
    py: Python<'_>,
    coro: Py<PyAny>,
    wait: &Bound<'_, crate::driver::NativeWait>,
    on_done: Py<PyAny>,
    on_foreign: Py<PyAny>,
    ctx: Option<Py<PyAny>>,
    deadline_ms: Option<u64>,
    scheduler: Option<Bound<'_, PyAny>>,
) -> NativeRun {
    let scheduler = scheduler.map(|given| match given.cast::<NativeSteps>() {
        Ok(steps) => Scheduler::Steps(Arc::clone(&steps.get().steps)),
        Err(_) => Scheduler::Call(given.unbind()),
    });
    let stop = Arc::new(Notify::new());
    let run = Arc::new(Run {
        coro,
        ctx,
        on_done,
        on_foreign,
        scheduler,
        deadline: deadline_ms.map(|ms| Instant::now() + Duration::from_millis(ms)),
        stop: stop.clone(),
    });
    match wait.get().take() {
        Some(work) => {
            let drive = Drive {
                run,
                reason: None,
                done: false,
            };
            handoff::spawn(drive_waits(drive, work));
        }
        None => {
            let error = PyRuntimeError::new_err("a native wait was awaited twice");
            run.finish(py, Err(error), None);
        }
    }
    NativeRun { stop }
}

/// Start driving `coro`.
///
/// `on_done(value, error, reason)` runs exactly once, on whichever thread
/// finished the drive: `error` is the exception the coroutine ended with (or
/// `None`), `reason` is `"deadline"` or `"cancelled"` when the coroutine was
/// cancelled on the way, else `None`. `on_foreign(coro, ctx, yielded,
/// remaining, reason)` takes over a coroutine that awaited a non-native object
/// (`reason` as for `on_done`, when it was awaited during cleanup);
/// `on_done` is then the caller's to arrange. `ctx` is a
/// `contextvars.Context` every step runs in. With `scheduler` (for instance
/// `loop.call_soon_threadsafe`, or a `_NativeSteps` made for the loop), every
/// step after the first runs through it, so the coroutine always runs on the
/// loop's thread and code that needs a running loop keeps working; without
/// it, steps run on the runtime thread.
#[pyfunction]
#[pyo3(signature = (coro, on_done, on_foreign, ctx=None, deadline_ms=None, scheduler=None))]
pub fn run_native(
    py: Python<'_>,
    coro: Py<PyAny>,
    on_done: Py<PyAny>,
    on_foreign: Py<PyAny>,
    ctx: Option<Py<PyAny>>,
    deadline_ms: Option<u64>,
    scheduler: Option<Bound<'_, PyAny>>,
) -> NativeRun {
    let scheduler = scheduler.map(|given| match given.cast::<NativeSteps>() {
        Ok(steps) => Scheduler::Steps(Arc::clone(&steps.get().steps)),
        Err(_) => Scheduler::Call(given.unbind()),
    });
    let stop = Arc::new(Notify::new());
    let run = Arc::new(Run {
        coro,
        ctx,
        on_done,
        on_foreign,
        scheduler,
        deadline: deadline_ms.map(|ms| Instant::now() + Duration::from_millis(ms)),
        stop: stop.clone(),
    });
    let first = run.step(py, Resume::Value(py.None()), None);
    if let Next::Wait(work) = first {
        let drive = Drive {
            run,
            reason: None,
            done: false,
        };
        handoff::spawn(drive_waits(drive, work));
    }
    NativeRun { stop }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::driver::NativeWait;
    use pyo3::types::PyList;
    use std::sync::atomic::Ordering;

    const HELPERS: &std::ffi::CStr = c"
import asyncio, threading

async def handler(wait):
    return await wait

def recorder(out):
    def on_done(value, error, reason):
        out.append((value, None if error is None else type(error).__name__,
                    threading.get_ident()))
    return on_done

def refuse(*args):
    raise AssertionError('no foreign await expected')
";

    fn ready_wait(py: Python<'_>, value: i64) -> Py<NativeWait> {
        let wait = NativeWait::new(
            async move { Ok::<i64, ()>(value) },
            |py, value| Ok(value.into_pyobject(py)?.into_any().unbind()),
            |_, ()| PyRuntimeError::new_err("unreachable"),
        );
        Py::new(py, wait).unwrap()
    }

    /// Start `count` drives whose only wait is already finished, and wait,
    /// with the GIL released, until all their steps are queued.
    fn start_drives<'py>(
        py: Python<'py>,
        helpers: &Bound<'py, PyModule>,
        steps: &Bound<'py, NativeSteps>,
        count: i64,
    ) -> Bound<'py, PyList> {
        let done = PyList::empty(py);
        let on_done = helpers.call_method1("recorder", (&done,)).unwrap();
        let refuse = helpers.getattr("refuse").unwrap();
        for n in 0..count {
            let coro = helpers
                .call_method1("handler", (ready_wait(py, n),))
                .unwrap();
            run_native(
                py,
                coro.unbind(),
                on_done.clone().unbind(),
                refuse.clone().unbind(),
                None,
                None,
                Some(steps.clone().into_any()),
            );
        }
        let queued = Arc::clone(&steps.get().steps);
        py.detach(|| {
            let started = Instant::now();
            while queued.jobs().as_ref().map_or(0, Vec::len) < count as usize {
                assert!(started.elapsed() < Duration::from_secs(5), "steps queued");
                std::thread::sleep(Duration::from_millis(1));
            }
        });
        done
    }

    #[test]
    fn queued_steps_wake_the_loop_once_and_run_on_its_thread() {
        Python::initialize();
        Python::attach(|py| {
            let helpers =
                PyModule::from_code(py, HELPERS, c"steps_helpers.py", c"steps_helpers").unwrap();
            let asyncio = py.import("asyncio").unwrap();
            let event_loop = asyncio.call_method0("new_event_loop").unwrap();
            let steps = Bound::new(py, NativeSteps::new(py, &event_loop).unwrap()).unwrap();
            let done = start_drives(py, &helpers, &steps, 5);
            assert_eq!(steps.get().steps.wakes.load(Ordering::SeqCst), 1);
            assert_eq!(done.len(), 0, "no step runs before the loop does");
            let pause = asyncio.call_method1("sleep", (0.05,)).unwrap();
            event_loop
                .call_method1("run_until_complete", (pause,))
                .unwrap();
            let me: u64 = py
                .import("threading")
                .unwrap()
                .call_method0("get_ident")
                .unwrap()
                .extract()
                .unwrap();
            let mut values: Vec<i64> = Vec::new();
            for entry in done.iter() {
                let (value, error, thread): (i64, Option<String>, u64) = entry.extract().unwrap();
                assert_eq!((error, thread), (None, me));
                values.push(value);
            }
            values.sort_unstable();
            assert_eq!(values, vec![0, 1, 2, 3, 4]);
            event_loop.call_method0("close").unwrap();
        });
    }

    #[test]
    fn a_closed_loop_answers_the_steps_it_never_ran() {
        Python::initialize();
        Python::attach(|py| {
            let helpers =
                PyModule::from_code(py, HELPERS, c"steps_helpers.py", c"steps_helpers").unwrap();
            let event_loop = py
                .import("asyncio")
                .unwrap()
                .call_method0("new_event_loop")
                .unwrap();
            let steps = Bound::new(py, NativeSteps::new(py, &event_loop).unwrap()).unwrap();
            let done = start_drives(py, &helpers, &steps, 2);
            event_loop.call_method0("close").unwrap();
            py.import("gc").unwrap().call_method0("collect").unwrap();
            assert_eq!(done.len(), 2);
            for entry in done.iter() {
                let (_, error, _): (Option<i64>, Option<String>, u64) = entry.extract().unwrap();
                assert_eq!(error.as_deref(), Some("RuntimeError"));
            }
        });
    }
}
