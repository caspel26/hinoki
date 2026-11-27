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
    serve, Aborted, Answer, Backend, BodyError, Chunk, Closed, Delivery, FileAnswer, Incoming,
    Lane, Pace, Streamed,
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
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
    // Nobody waits for the head: the request timed out or the client left
    // while the job was queued, and running it would repeat side effects the
    // client was told did not happen.
    if job.call.head.is_closed() {
        return;
    }
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
    /// Bytes of request body being read, so many slow uploads cannot hold more
    /// memory than the admitted requests could.
    reading: Arc<Semaphore>,
    limits: WsgiLimits,
    active: Arc<AtomicUsize>,
}

/// Most body bytes read at once: what the threads and the queue could hold.
fn read_budget(limits: &WsgiLimits) -> usize {
    let slots = limits.threads.saturating_add(limits.queue);
    slots
        .saturating_mul(limits.max_body_bytes)
        .clamp(1, u32::MAX as usize)
}

/// The bytes a request reserves: its declared length, or the largest body when
/// it is chunked or declares nothing parseable.
fn claimed_bytes(headers: &[(Vec<u8>, Vec<u8>)], max_body_bytes: usize) -> u32 {
    let declared = headers
        .iter()
        .find(|(name, _)| name == b"content-length")
        .and_then(|(_, value)| {
            std::str::from_utf8(value)
                .ok()?
                .trim()
                .parse::<usize>()
                .ok()
        });
    let claim = declared.map_or(max_body_bytes, |bytes| bytes.min(max_body_bytes));
    u32::try_from(claim.max(1)).unwrap_or(u32::MAX)
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
        body: bytes::Bytes::from_static(text.as_bytes()),
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

/// Whether the decoded path holds a NUL byte or a `.`/`..` segment, which no
/// application should have to guard against (`%2e%2e` and `%2f` included).
fn unsafe_path(path: &str) -> bool {
    let bytes = path.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut at = 0;
    while at < bytes.len() {
        let pair = bytes.get(at + 1..at + 3).and_then(|hex| {
            let text = std::str::from_utf8(hex).ok()?;
            u8::from_str_radix(text, 16).ok()
        });
        match (bytes[at], pair) {
            (b'%', Some(byte)) => {
                decoded.push(byte);
                at += 3;
            }
            (byte, _) => {
                decoded.push(byte);
                at += 1;
            }
        }
    }
    decoded.contains(&0)
        || decoded
            .split(|byte| *byte == b'/')
            .any(|part| part == b"." || part == b"..")
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
            reading: Arc::new(Semaphore::new(read_budget(&limits))),
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
        if unsafe_path(&request.path) {
            return plain(400, "the request path is invalid\n");
        }
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
            Some(reader) => {
                // The bytes this body may take are reserved while it is read; a
                // client still sending holds no thread or queue slot, only this.
                let claim = claimed_bytes(&headers, self.limits.max_body_bytes);
                let Ok(_reserved) = Arc::clone(&self.reading).try_acquire_many_owned(claim) else {
                    return overloaded();
                };
                match reader.read_all(self.limits.max_body_bytes).await {
                    Ok(bytes) => bytes,
                    Err(error) => return body_failure(&error),
                }
            }
            None => Vec::new(),
        };
        let Ok(admission) = Arc::clone(&self.admission).try_acquire_owned() else {
            return overloaded();
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
                // A stalled iterable would hold the connection open for ever:
                // the connection resets the stream after a silent gap, and the
                // application's next write then reports nobody is listening.
                pace: Pace {
                    idle: Some(self.limits.request_timeout),
                    keepalive: None,
                },
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
            Delivery::Fast(fast) => Answer {
                status: fast.status,
                headers: fast
                    .headers
                    .iter()
                    .map(|(n, v)| (n.as_str().as_bytes().to_vec(), v.as_bytes().to_vec()))
                    .collect(),
                body: fast.body,
            },
            // Callers that cannot stream get the status and an empty body.
            Delivery::Streamed(Streamed {
                status, headers, ..
            })
            | Delivery::File(FileAnswer {
                status, headers, ..
            }) => Answer {
                status,
                headers,
                body: bytes::Bytes::new(),
            },
            Delivery::Upgrade(_) => Answer {
                status: 500,
                headers: Vec::new(),
                body: bytes::Bytes::new(),
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
        access_log_sink=None, server_timing=false, compression=None, metrics=None,
        reuse_port=false
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
        compression: Option<&Bound<'_, PyDict>>,
        metrics: Option<&Bound<'_, PyDict>>,
        reuse_port: bool,
    ) -> PyResult<Self> {
        let compression = crate::compression::parse(compression)?;
        // A WSGI callable has no Hinoki routes: every answer is route "".
        let metrics = crate::metrics::parse(metrics, &[])?;
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
            mut config,
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
            compression,
            reuse_port,
        )?;
        config.metrics = metrics.as_ref().map(|m| Arc::clone(&m.registry));
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
        let task = runtime.spawn(crate::metrics::alongside(
            serve(
                listener,
                Arc::clone(&backend),
                config,
                tls,
                receiver.clone(),
            ),
            metrics,
            receiver,
        ));
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

#[cfg(test)]
mod tests {
    use super::*;
    use kiso::accesslog::Trace;

    fn status_of(delivery: &Delivery) -> u16 {
        delivery.status()
    }

    fn incoming() -> Incoming {
        Incoming {
            method: "GET".into(),
            path: "/".into(),
            query: Vec::new(),
            headers: Vec::new(),
            peer: "127.0.0.1:1".parse().unwrap(),
            secure: false,
            http_version: "1.1",
            refusal: None,
            body: None,
            trace: Arc::new(Trace::default()),
            upgrade: kiso::ws::Upgrade::Absent,
        }
    }

    fn backend(code: &std::ffi::CStr, threads: usize, queue: usize) -> WsgiBackend {
        Python::initialize();
        let bridge = Python::attach(|py| {
            let module =
                PyModule::from_code(py, code, c"bridge.py", c"bridge").expect("bridge compiles");
            module.getattr("bridge").expect("bridge exists").unbind()
        });
        let limits = WsgiLimits {
            threads,
            queue,
            max_body_bytes: 1024,
            request_timeout: Duration::from_millis(600),
        };
        WsgiBackend::start(bridge, limits).expect("threads start")
    }

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
    }

    const ANSWERS: &std::ffi::CStr = c"
def bridge(call):
    call.start(201, [(b'x-a', b'1')])
    call.send(b'one')
    call.send(b'two')
    call.finish()
";

    const FAILS: &std::ffi::CStr = c"
def bridge(call):
    raise RuntimeError('boom')
";

    const FAILS_LATE: &std::ffi::CStr = c"
def bridge(call):
    call.start(200, [])
    call.send(b'part')
    raise RuntimeError('late')
";

    const SLOW: &std::ffi::CStr = c"
import time
def bridge(call):
    time.sleep(2)
    call.start(200, [])
    call.finish()
";

    const STALLS: &std::ffi::CStr = c"
import time
def bridge(call):
    call.start(200, [])
    call.send(b'one')
    time.sleep(2)
    call.send(b'two')
    call.finish()
";

    async fn next_piece(live: &mut kiso::Live) -> Option<Chunk> {
        std::future::poll_fn(|cx| live.poll_piece(cx, usize::MAX)).await
    }

    #[test]
    fn a_body_claims_its_declared_length_or_the_largest_body() {
        let length = |value: &[u8]| vec![(b"content-length".to_vec(), value.to_vec())];
        assert_eq!(claimed_bytes(&length(b"10"), 1024), 10);
        assert_eq!(claimed_bytes(&length(b"99999"), 1024), 1024);
        assert_eq!(claimed_bytes(&length(b"x"), 1024), 1024);
        assert_eq!(claimed_bytes(&[], 1024), 1024);
        let limits = WsgiLimits {
            threads: 2,
            queue: 3,
            max_body_bytes: 1024,
            request_timeout: Duration::from_secs(1),
        };
        assert_eq!(read_budget(&limits), 5 * 1024);
    }

    #[test]
    fn a_producer_silent_past_the_timeout_resets_the_stream() {
        let backend = backend(STALLS, 1, 1);
        runtime().block_on(async {
            let Delivery::Streamed(streamed) = backend.deliver(incoming()).await else {
                panic!("a streamed response")
            };
            assert!(
                streamed.pace.idle.is_some(),
                "the timer is the connection's"
            );
            let mut live = kiso::Live::new(streamed.chunks).paced(streamed.pace);
            assert_eq!(next_piece(&mut live).await, Some(Ok(Bytes::from("one"))));
            let silent = timeout(Duration::from_millis(1500), next_piece(&mut live)).await;
            assert_eq!(silent, Ok(Some(Err(Aborted))));
        });
    }

    #[test]
    fn a_decoded_path_with_a_nul_or_a_dot_segment_is_refused() {
        for path in [
            "/x%00y",
            "/a/../b",
            "/a/%2e%2E/b",
            "/a%2f..%2fb",
            "/./a",
            "/a/..",
            "/..",
        ] {
            assert!(unsafe_path(path), "{path} must be refused");
        }
        for path in [
            "/", "/a/b", "/a..b", "/a%2fb", "/.hidden", "/a%20b", "/a/...", "/%zz",
        ] {
            assert!(!unsafe_path(path), "{path} must pass");
        }
    }

    #[test]
    fn body_failures_map_to_client_errors() {
        assert_eq!(status_of(&body_failure(&BodyError::TooLarge)), 413);
        assert_eq!(status_of(&body_failure(&BodyError::Timeout)), 408);
        assert_eq!(status_of(&body_failure(&BodyError::Malformed)), 400);
        assert_eq!(status_of(&body_failure(&BodyError::Disconnected)), 400);
    }

    #[test]
    fn overload_asks_the_client_to_retry() {
        let Delivery::Whole(answer) = overloaded() else {
            panic!("a whole answer")
        };
        assert_eq!(answer.status, 503);
        assert!(answer
            .headers
            .contains(&(b"retry-after".to_vec(), b"1".to_vec())));
    }

    #[test]
    fn limits_must_be_positive() {
        assert!(positive("sync_workers", 0).is_err());
        assert_eq!(positive("sync_workers", 3).unwrap(), 3);
    }

    #[test]
    fn a_response_streams_its_pieces_in_order() {
        let backend = backend(ANSWERS, 1, 1);
        runtime().block_on(async {
            let Delivery::Streamed(mut streamed) = backend.deliver(incoming()).await else {
                panic!("a streamed response")
            };
            assert_eq!(streamed.status, 201);
            assert_eq!(streamed.chunks.recv().await, Some(Ok(Bytes::from("one"))));
            assert_eq!(streamed.chunks.recv().await, Some(Ok(Bytes::from("two"))));
            assert_eq!(streamed.chunks.recv().await, None);
        });
    }

    #[test]
    fn an_unsafe_path_answers_400_without_calling_the_application() {
        let backend = backend(ANSWERS, 1, 1);
        for path in ["/a/../b", "/x%00y"] {
            let mut request = incoming();
            request.path = path.into();
            assert_eq!(runtime().block_on(backend.deliver(request)).status(), 400);
        }
    }

    #[test]
    fn a_failure_before_the_head_answers_500() {
        let backend = backend(FAILS, 1, 1);
        let delivery = runtime().block_on(backend.deliver(incoming()));
        assert_eq!(delivery.status(), 500);
    }

    #[test]
    fn a_failure_after_the_head_resets_the_stream() {
        let backend = backend(FAILS_LATE, 1, 1);
        runtime().block_on(async {
            let Delivery::Streamed(mut streamed) = backend.deliver(incoming()).await else {
                panic!("a streamed response")
            };
            assert_eq!(streamed.chunks.recv().await, Some(Ok(Bytes::from("part"))));
            assert_eq!(streamed.chunks.recv().await, Some(Err(Aborted)));
        });
    }

    #[test]
    fn a_job_nobody_waits_for_does_not_run_the_application() {
        Python::initialize();
        let module = Python::attach(|py| {
            PyModule::from_code(
                py,
                c"
import time
ran = []
def bridge(call):
    ran.append(call)
    time.sleep(1)
    call.start(200, [])
    call.finish()
",
                c"counting.py",
                c"counting",
            )
            .expect("bridge compiles")
            .unbind()
        });
        let bridge = Python::attach(|py| module.bind(py).getattr("bridge").unwrap().unbind());
        let limits = WsgiLimits {
            threads: 1,
            queue: 1,
            max_body_bytes: 1024,
            request_timeout: Duration::from_millis(300),
        };
        let backend = Arc::new(WsgiBackend::start(bridge, limits).expect("threads start"));
        runtime().block_on(async {
            let first = tokio::spawn({
                let backend = Arc::clone(&backend);
                async move { backend.deliver(incoming()).await.status() }
            });
            tokio::time::sleep(Duration::from_millis(50)).await;
            // Queued behind the first call, then answered 504 while it waits.
            assert_eq!(backend.deliver(incoming()).await.status(), 504);
            assert_eq!(first.await.unwrap(), 504);
        });
        std::thread::sleep(Duration::from_millis(1500));
        let ran = Python::attach(|py| module.bind(py).getattr("ran").unwrap().len().unwrap());
        assert_eq!(ran, 1, "the abandoned request ran");
    }

    /// Wait for a state the test depends on instead of guessing how long a
    /// busy machine takes to reach it.
    async fn until(mut condition: impl FnMut() -> bool) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        while !condition() {
            assert!(
                tokio::time::Instant::now() < deadline,
                "the state was not reached"
            );
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }

    #[test]
    fn a_full_pool_and_queue_refuse_and_a_slow_call_times_out() {
        let backend = Arc::new(backend(SLOW, 1, 1));
        runtime().block_on(async {
            let first = tokio::spawn({
                let backend = Arc::clone(&backend);
                async move { backend.deliver(incoming()).await.status() }
            });
            // The first call must hold the only thread before the second one
            // queues, or the second finds the queue still occupied.
            until(|| backend.running() == 1).await;
            let second = tokio::spawn({
                let backend = Arc::clone(&backend);
                async move { backend.deliver(incoming()).await.status() }
            });
            until(|| backend.admission.available_permits() == 0).await;
            assert_eq!(backend.deliver(incoming()).await.status(), 503);
            // The pool is busy past request_timeout: the waiting ones answer 504.
            assert_eq!(first.await.unwrap(), 504);
            assert_eq!(second.await.unwrap(), 504);
        });
    }
}
