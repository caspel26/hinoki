//! Hosting a WSGI callable on Kiso.
//!
//! Tokio threads accept connections, read the request body (bounded) and queue
//! a job; a fixed set of plain threads takes the GIL, calls the Python bridge
//! (`hinoki.runtime.wsgibridge.serve`) and passes the response back through a
//! bounded channel, so the client receives the head before the body is complete
//! and a slow client slows the application down instead of filling memory.
//!
//! This path is not native: the application and every request and response
//! chunk are Python objects. Rust owns what surrounds them: the sockets, TLS,
//! the access log, the admission bound, the request body limit and timeouts,
//! and the end of the response when the client leaves.
//!
//! Bounds: at most `threads` requests run and `queue` wait; the next is
//! answered `503`. The request body is read in full before the application is
//! called, up to `max_body_bytes` (`413` above it). The application has
//! `request_timeout` to start its response (`504` after it; a sync call cannot be
//! stopped, so it keeps its thread until it returns).

use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{mpsc as std_mpsc, Arc, Mutex};
use std::time::{Duration, Instant};

use bytes::Bytes;
use kiso::headers::Header;
use kiso::{
    serve, Aborted, Answer, Backend, BodyError, Chunk, Closed, Delivery, Incoming, Lane, Streamed,
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use tokio::net::TcpListener;
use tokio::sync::{mpsc, oneshot, watch, Semaphore};
use tokio::task::JoinHandle;
use tokio::time::timeout;

use crate::kiso::Shared;

/// Chunks the application may be ahead of the client by.
const STREAM_DEPTH: usize = 8;

/// The status and headers the application started its response with.
struct Started {
    status: u16,
    headers: Vec<Header>,
}

/// One request waiting for a thread.
struct Job {
    call: CallData,
    _admission: tokio::sync::OwnedSemaphorePermit,
}

struct CallData {
    method: String,
    path: String,
    query: Vec<u8>,
    headers: Vec<Header>,
    peer: SocketAddr,
    secure: bool,
    http_version: &'static str,
    body: Vec<u8>,
    head: oneshot::Sender<Started>,
    chunks: mpsc::Sender<Chunk>,
}

/// The request a worker thread hands to the Python bridge, and where the
/// bridge writes the response.
#[pyclass(name = "WsgiCall", module = "hinoki.core", frozen)]
pub struct WsgiCall {
    method: String,
    path: String,
    query: Vec<u8>,
    headers: Vec<Header>,
    peer: SocketAddr,
    secure: bool,
    http_version: &'static str,
    body: Vec<u8>,
    head: Mutex<Option<oneshot::Sender<Started>>>,
    chunks: Mutex<Option<mpsc::Sender<Chunk>>>,
}

fn poisoned() -> PyErr {
    PyRuntimeError::new_err("the response state is poisoned")
}

impl From<CallData> for WsgiCall {
    fn from(data: CallData) -> Self {
        Self {
            method: data.method,
            path: data.path,
            query: data.query,
            headers: data.headers,
            peer: data.peer,
            secure: data.secure,
            http_version: data.http_version,
            body: data.body,
            head: Mutex::new(Some(data.head)),
            chunks: Mutex::new(Some(data.chunks)),
        }
    }
}

impl WsgiCall {
    fn sender(&self) -> PyResult<Option<mpsc::Sender<Chunk>>> {
        Ok(self.chunks.lock().map_err(|_| poisoned())?.clone())
    }

    fn started(&self) -> bool {
        self.head.lock().map_or(true, |slot| slot.is_none())
    }

    fn push(&self, py: Python<'_>, chunk: Chunk) -> PyResult<bool> {
        let Some(sender) = self.sender()? else {
            return Ok(false);
        };
        // Waits for room in the channel without the GIL: other requests run
        // while this one waits for a slow client.
        Ok(py.detach(|| sender.blocking_send(chunk)).is_ok())
    }
}

#[pymethods]
impl WsgiCall {
    #[getter]
    fn method(&self) -> &str {
        &self.method
    }

    #[getter]
    fn path(&self) -> &str {
        &self.path
    }

    #[getter]
    fn query<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.query)
    }

    #[getter]
    fn headers(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        self.headers.clone()
    }

    #[getter]
    fn client(&self) -> (String, u16) {
        (self.peer.ip().to_string(), self.peer.port())
    }

    #[getter]
    fn secure(&self) -> bool {
        self.secure
    }

    #[getter]
    fn http_version(&self) -> &str {
        self.http_version
    }

    #[getter]
    fn body<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.body)
    }

    /// Begin the response. `False` when nobody waits for it any more (the
    /// request timed out or the client left): stop and close the iterable.
    fn start(&self, status: u16, headers: Vec<(Vec<u8>, Vec<u8>)>) -> PyResult<bool> {
        let sender = self.head.lock().map_err(|_| poisoned())?.take();
        let Some(sender) = sender else {
            return Err(PyRuntimeError::new_err("the response was already started"));
        };
        Ok(sender.send(Started { status, headers }).is_ok())
    }

    /// Hand one piece of the body to the client, waiting while it is slow.
    /// `False` when the client is gone: stop and close the iterable.
    fn send(&self, py: Python<'_>, chunk: &[u8]) -> PyResult<bool> {
        if chunk.is_empty() {
            return Ok(self.sender()?.is_some_and(|sender| !sender.is_closed()));
        }
        self.push(py, Ok(Bytes::copy_from_slice(chunk)))
    }

    /// The response is incomplete: reset the stream rather than end it as done.
    fn abort(&self, py: Python<'_>) -> PyResult<()> {
        if self.started() {
            self.push(py, Err(Aborted))?;
        }
        self.finish();
        Ok(())
    }

    /// The body is complete.
    fn finish(&self) {
        if let Ok(mut slot) = self.chunks.lock() {
            slot.take();
        }
    }
}

type Jobs = Arc<Mutex<std_mpsc::Receiver<Job>>>;

/// Whether the application had answered when its call ended: if it raised
/// before that, the client gets a `500`; after it, the stream is reset.
fn finish_call(py: Python<'_>, call: &Bound<'_, WsgiCall>, outcome: PyResult<Py<PyAny>>) {
    let inner = call.get();
    if let Err(error) = outcome {
        error.print(py);
        if inner.started() {
            let _ = inner.abort(py);
        } else if let Ok(Some(sender)) = inner.head.lock().map(|mut slot| slot.take()) {
            let _ = sender.send(Started {
                status: 500,
                headers: vec![(b"content-length".to_vec(), b"0".to_vec())],
            });
        }
    }
    inner.finish();
}

fn run_job(bridge: &Py<PyAny>, job: Job, active: &AtomicUsize) {
    active.fetch_add(1, Ordering::AcqRel);
    Python::attach(|py| match Bound::new(py, WsgiCall::from(job.call)) {
        Ok(call) => {
            let outcome = bridge.call1(py, (call.clone(),));
            finish_call(py, &call, outcome);
        }
        Err(error) => error.print(py),
    });
    active.fetch_sub(1, Ordering::AcqRel);
}

fn worker(bridge: &Py<PyAny>, jobs: &Jobs, active: &AtomicUsize) {
    loop {
        let next = jobs
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .recv();
        let Ok(job) = next else { return };
        run_job(bridge, job, active);
    }
}

/// The limits of one hosted WSGI application.
#[derive(Clone, Copy)]
pub struct WsgiLimits {
    pub threads: usize,
    pub queue: usize,
    pub max_body_bytes: usize,
    pub request_timeout: Duration,
}

pub struct WsgiBackend {
    jobs: std_mpsc::SyncSender<Job>,
    admission: Arc<Semaphore>,
    limits: WsgiLimits,
    active: Arc<AtomicUsize>,
}

fn plain(status: u16, text: &'static str) -> Delivery {
    Delivery::Whole(Answer {
        status,
        headers: vec![
            (
                b"content-type".to_vec(),
                b"text/plain; charset=utf-8".to_vec(),
            ),
            (
                b"content-length".to_vec(),
                text.len().to_string().into_bytes(),
            ),
        ],
        body: text.as_bytes().to_vec(),
    })
}

fn overloaded() -> Delivery {
    let Delivery::Whole(mut answer) = plain(503, "the server is busy\n") else {
        unreachable!("plain is whole")
    };
    answer
        .headers
        .push((b"retry-after".to_vec(), b"1".to_vec()));
    Delivery::Whole(answer)
}

fn body_failure(error: &BodyError) -> Delivery {
    match error {
        BodyError::TooLarge => plain(413, "the request body is too large\n"),
        BodyError::Timeout => plain(408, "the request body took too long\n"),
        BodyError::Malformed | BodyError::Disconnected => {
            plain(400, "the request body is invalid\n")
        }
    }
}

impl WsgiBackend {
    /// Start the worker threads. Each calls `bridge(call)` for one request at a time.
    pub fn start(bridge: Py<PyAny>, limits: WsgiLimits) -> std::io::Result<Self> {
        let (jobs, receiver) = std_mpsc::sync_channel(limits.queue.max(1));
        let receiver: Jobs = Arc::new(Mutex::new(receiver));
        let active = Arc::new(AtomicUsize::new(0));
        let bridge = Arc::new(bridge);
        for number in 0..limits.threads {
            let (bridge, receiver, active) = (
                Arc::clone(&bridge),
                Arc::clone(&receiver),
                Arc::clone(&active),
            );
            std::thread::Builder::new()
                .name(format!("kiso-wsgi-{number}"))
                .spawn(move || worker(&bridge, &receiver, &active))?;
        }
        Ok(Self {
            jobs,
            admission: Arc::new(Semaphore::new(limits.threads + limits.queue)),
            limits,
            active,
        })
    }

    /// Requests whose application call has not returned.
    pub fn running(&self) -> usize {
        self.active.load(Ordering::Acquire)
    }

    async fn submit(&self, request: Incoming) -> Delivery {
        if let Some(early) = &request.refusal {
            return plain(early.status(), "the request was refused\n");
        }
        let Ok(admission) = Arc::clone(&self.admission).try_acquire_owned() else {
            return overloaded();
        };
        let Incoming {
            method,
            path,
            query,
            headers,
            peer,
            secure,
            http_version,
            body,
            ..
        } = request;
        let body = match body {
            Some(reader) => match reader.read_all(self.limits.max_body_bytes).await {
                Ok(bytes) => bytes,
                Err(error) => return body_failure(&error),
            },
            None => Vec::new(),
        };
        let (head, started) = oneshot::channel();
        let (chunks, receiver) = mpsc::channel(STREAM_DEPTH);
        let call = CallData {
            method,
            path,
            query,
            headers,
            peer,
            secure,
            http_version,
            body,
            head,
            chunks,
        };
        let job = Job {
            call,
            _admission: admission,
        };
        if self.jobs.try_send(job).is_err() {
            return overloaded();
        }
        match timeout(self.limits.request_timeout, started).await {
            Ok(Ok(Started { status, headers })) => Delivery::Streamed(Streamed {
                status,
                headers,
                chunks: receiver,
            }),
            Ok(Err(_)) => plain(500, "the application failed\n"),
            Err(_) => plain(504, "the application took too long\n"),
        }
    }
}

impl Backend for WsgiBackend {
    async fn handle(&self, request: Incoming) -> Answer {
        match self.deliver(request).await {
            Delivery::Whole(answer) => answer,
            // Callers that cannot stream get the status and an empty body.
            Delivery::Streamed(streamed) => Answer {
                status: streamed.status,
                headers: streamed.headers,
                body: Vec::new(),
            },
        }
    }

    async fn deliver(&self, request: Incoming) -> Delivery {
        let trace = Arc::clone(&request.trace);
        let delivery = self.submit(request).await;
        trace.set(match &delivery {
            Delivery::Whole(answer) if answer.status >= 400 && answer.status != 500 => Lane::Early,
            _ => Lane::Python,
        });
        delivery
    }
}

/// A Kiso server hosting one WSGI callable.
#[pyclass(name = "WsgiServer", module = "hinoki.core", frozen)]
pub struct WsgiServer {
    addr: SocketAddr,
    stop: watch::Sender<bool>,
    task: Mutex<Option<JoinHandle<Closed>>>,
    backend: Arc<WsgiBackend>,
    drain: Duration,
}

fn positive(name: &str, value: usize) -> PyResult<usize> {
    if value == 0 {
        return Err(PyValueError::new_err(format!("{name} must be >= 1")));
    }
    Ok(value)
}

#[pymethods]
impl WsgiServer {
    /// Serve `bridge` (called with a `WsgiCall` on a worker thread) on `host:port`
    /// or the inherited socket `fd`. Errors are raised here, before anything is
    /// accepted.
    #[staticmethod]
    #[pyo3(signature = (
        bridge, host, port, threads=8, queue=64, max_body_bytes=1_048_576,
        request_timeout=30.0, max_connections=10_000, header_read_timeout=10.0,
        shutdown_timeout=10.0, max_header_bytes=32_768, body_read_timeout=10.0,
        fd=None, write_timeout=30.0, tls_cert=None, tls_key=None, http2=false,
        access_log=None, access_log_skip=None, access_log_slow_ms=None,
        access_log_sink=None, server_timing=false
    ))]
    #[allow(clippy::too_many_arguments)]
    fn start(
        bridge: Py<PyAny>,
        host: &str,
        port: u16,
        threads: usize,
        queue: usize,
        max_body_bytes: usize,
        request_timeout: f64,
        max_connections: usize,
        header_read_timeout: f64,
        shutdown_timeout: f64,
        max_header_bytes: usize,
        body_read_timeout: f64,
        fd: Option<i32>,
        write_timeout: f64,
        tls_cert: Option<String>,
        tls_key: Option<String>,
        http2: bool,
        access_log: Option<String>,
        access_log_skip: Option<Vec<String>>,
        access_log_slow_ms: Option<f64>,
        access_log_sink: Option<Py<PyAny>>,
        server_timing: bool,
    ) -> PyResult<Self> {
        let limits = WsgiLimits {
            threads: positive("threads", threads)?,
            queue: positive("queue", queue)?,
            max_body_bytes,
            request_timeout: Duration::try_from_secs_f64(request_timeout)
                .ok()
                .filter(|wait| !wait.is_zero())
                .ok_or_else(|| PyValueError::new_err("request_timeout must be > 0"))?,
        };
        let Shared {
            config,
            tls,
            listener,
            addr,
        } = Shared::prepare(
            host,
            port,
            max_connections,
            header_read_timeout,
            shutdown_timeout,
            max_header_bytes,
            body_read_timeout,
            fd,
            write_timeout,
            (tls_cert, tls_key, http2),
            (
                access_log,
                access_log_skip,
                access_log_slow_ms,
                access_log_sink,
            ),
            server_timing,
        )?;
        let drain = config.shutdown_timeout;
        let backend = Arc::new(
            WsgiBackend::start(bridge, limits)
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))?,
        );
        let runtime = pyo3_async_runtimes::tokio::get_runtime();
        let _enter = runtime.enter();
        let listener = TcpListener::from_std(listener)
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        let (stop, receiver) = watch::channel(false);
        let task = runtime.spawn(serve(listener, Arc::clone(&backend), config, tls, receiver));
        Ok(Self {
            addr,
            stop,
            task: Mutex::new(Some(task)),
            backend,
            drain,
        })
    }

    #[getter]
    fn port(&self) -> u16 {
        self.addr.port()
    }

    /// Stop accepting, let running requests finish (up to `shutdown_timeout`)
    /// and return the number of connections that had to be aborted. Blocks, so
    /// call it from the thread that owns the process, not from a request.
    fn shutdown(&self, py: Python<'_>) -> PyResult<usize> {
        let _ = self.stop.send(true);
        let task = self.task.lock().ok().and_then(|mut slot| slot.take());
        let backend = Arc::clone(&self.backend);
        let drain = self.drain;
        py.detach(move || {
            let runtime = pyo3_async_runtimes::tokio::get_runtime();
            let aborted = match task {
                Some(task) => runtime
                    .block_on(task)
                    .map(|closed| closed.aborted)
                    .map_err(|error| PyRuntimeError::new_err(error.to_string()))?,
                None => 0,
            };
            // Aborted connections drop their receivers, which ends the
            // applications still writing; give those threads time to return.
            let deadline = Instant::now() + drain;
            while backend.running() > 0 && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
            Ok(aborted)
        })
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<WsgiCall>()?;
    m.add_class::<WsgiServer>()?;
    Ok(())
}
