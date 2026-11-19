//! The socket side of the Rust server: `RustServer` binds a listener on the
//! shared tokio runtime and serves HTTP/1.1 (and HTTP/2 over TLS) with `hinoki-server`, handing every
//! request the classifier does not answer to the asyncio loop.
//!
//! Handoff: the connection layer queues a `PreparedRequest` on the core's bounded
//! queue and, only when the queue went from empty to non-empty, writes one byte
//! to a socket pair whose read end the loop watches with `add_reader`. The
//! callback calls `clear_wake` and then drains the queue until it is empty (the
//! `Queue::drain` contract). Tokio threads never take the GIL on this path.

use std::io::{ErrorKind, Read, Write};
use std::net::SocketAddr;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use hinoki_server::{
    serve, AccessLog, Answer, Backend, Closed, Early, Incoming, Lane, LogFilter, ServerConfig, Sink,
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use socket2::{Socket, Type};
use tokio::net::TcpListener;
use tokio::sync::{watch, Semaphore};
use tokio::task::{AbortHandle, JoinHandle};

use crate::crudplan::CrudPlan;
use crate::nativecrud::{NativeRead, NativeWrite};
use crate::server::{
    Asked, Core, Head, NativeFailure, NativeRows, Outcome, PreparedRequest, Reply, ServerCore,
};
use hinoki_orm::{Database, DbError, ErrorKind as DbErrorKind};

/// Answer for an internal inconsistency in the classifier (a bug, not a client error).
fn internal_error() -> Answer {
    Answer {
        status: 500,
        headers: vec![(b"content-length".to_vec(), b"0".to_vec())],
        body: Vec::new(),
    }
}

/// Connects the connection layer to the classifier and the wake socket.
struct CoreBackend {
    core: Arc<Core>,
    wake: UnixStream,
    /// A second handle to the read end: while the backend can still write, the
    /// socket pair stays open, so a late wake never meets a closed peer.
    _reader: UnixStream,
}

impl CoreBackend {
    /// One byte tells the loop to drain. `WouldBlock` means a wake is already
    /// pending, which is as good as ours.
    fn ring(&self) {
        let mut wake = &self.wake;
        let _ = wake.write(&[1]);
    }

    async fn reply_of(&self, outcome: Outcome) -> Answer {
        match outcome {
            Outcome::Early(reply) => reply,
            Outcome::Queued {
                wake,
                receiver,
                asked,
            } => {
                if wake {
                    self.ring();
                }
                match receiver.await {
                    Ok(reply) => reply,
                    // Dropped unanswered (shutdown cancelled the task): the
                    // server is going away, so say so.
                    Err(_) => self
                        .core
                        .early_reply(&asked, Early::NotReady, Vec::new())
                        .unwrap_or_else(|_| internal_error()),
                }
            }
            Outcome::NativeCrud {
                request,
                receiver,
                asked,
                plan,
                database,
                params,
                query,
            } => {
                self.native_crud_reply(*request, receiver, asked, plan, database, params, query)
                    .await
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn native_crud_reply(
        &self,
        request: PreparedRequest,
        receiver: tokio::sync::oneshot::Receiver<Reply>,
        asked: Asked,
        plan: Arc<CrudPlan>,
        database: Database,
        params: Vec<(String, String)>,
        query: Vec<u8>,
    ) -> Answer {
        let Ok(mut job_wake) = self.wake.try_clone() else {
            let mut wake = &self.wake;
            requeue_native(&self.core, &mut wake, request, asked.clone());
            return receiver.await.unwrap_or_else(|_| {
                self.core
                    .early_reply(&asked, Early::NotReady, Vec::new())
                    .unwrap_or_else(|_| internal_error())
            });
        };
        let pending = Arc::new(Mutex::new(Some((request, asked.clone()))));
        let job_pending = Arc::clone(&pending);
        let core = Arc::clone(&self.core);
        let abort_on_disconnect = !is_write_action(&plan.action);
        let job = async move {
            let pending_request = job_pending
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .take();
            let Some((request, asked)) = pending_request else {
                return;
            };
            request.release_admission();
            let Some(caller) = admitted(&plan, &request) else {
                requeue_native(&core, &mut job_wake, request, asked);
                return;
            };
            let write_action = matches!(plan.action.as_str(), "create" | "update" | "delete");
            let fallback = if write_action {
                let result = crate::nativecrud::execute_write(
                    &plan,
                    &database,
                    &request,
                    &params,
                    &query,
                    core.max_body_bytes(),
                    core.request_timeout(),
                    caller.as_deref(),
                )
                .await;
                finish_native_write(&request, result)
            } else {
                let result = match tokio::time::timeout(
                    core.request_timeout(),
                    crate::nativecrud::execute_read(&plan, &database, &params, &query),
                )
                .await
                {
                    Ok(result) => result,
                    Err(_) => NativeRead::Timeout,
                };
                finish_native_read(&request, &plan, result)
            };
            if fallback {
                requeue_native(&core, &mut job_wake, request, asked);
            }
        };
        match self.core.native_jobs().try_spawn_with_handle(job).await {
            Ok(handle) => {
                let _abort_on_drop = if abort_on_disconnect {
                    Some(AbortOnDrop(Some(handle)))
                } else {
                    None
                };
                let reply = receiver.await.unwrap_or_else(|_| {
                    self.core
                        .early_reply(&asked, Early::NotReady, Vec::new())
                        .unwrap_or_else(|_| internal_error())
                });
                reply
            }
            Err(job) => {
                drop(job);
                let pending = pending
                    .lock()
                    .unwrap_or_else(|error| error.into_inner())
                    .take();
                if let Some((request, asked)) = pending {
                    let mut wake = &self.wake;
                    requeue_native(&self.core, &mut wake, request, asked);
                }
                receiver.await.unwrap_or_else(|_| {
                    self.core
                        .early_reply(&asked, Early::NotReady, Vec::new())
                        .unwrap_or_else(|_| internal_error())
                })
            }
        }
    }
}

/// Whether the request may run natively, and as which caller: a public route
/// admits everyone, an authenticated one only a request Rust verified itself.
/// `None` sends the request to the Python pipeline.
fn admitted(plan: &CrudPlan, request: &PreparedRequest) -> Option<Option<String>> {
    let Some(auth) = &plan.auth else {
        return Some(None);
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |elapsed| i64::try_from(elapsed.as_secs()).unwrap_or(0));
    let subject = request
        .single_header(b"authorization")
        .and_then(|value| auth.subject(value, now))?;
    Some(Some(subject))
}

fn is_write_action(action: &str) -> bool {
    matches!(action, "create" | "update" | "delete")
}

fn finish_native_read(request: &PreparedRequest, plan: &CrudPlan, result: NativeRead) -> bool {
    match result {
        NativeRead::Reply { status, body } => {
            request.respond_final(status, json_headers(status), &body);
            false
        }
        NativeRead::Rows(rows, page_info) => {
            let _ = request.set_native_rows(NativeRows { rows, page_info });
            true
        }
        NativeRead::Failure(error) => {
            let _ = request.set_native_failure(native_failure(error));
            true
        }
        NativeRead::NotFound => {
            let _ = request.set_native_failure(not_found_failure(plan));
            true
        }
        NativeRead::Timeout => {
            let _ = request.set_native_failure(NativeFailure {
                kind: "timeout".to_owned(),
                message: "request timed out".to_owned(),
                constraint: None,
                table: None,
                sqlstate: None,
            });
            true
        }
        NativeRead::Defer => true,
    }
}

fn finish_native_write(request: &PreparedRequest, result: NativeWrite) -> bool {
    match result {
        NativeWrite::Reply { status, body } => {
            request.respond_final(status, json_headers(status), &body);
            false
        }
        NativeWrite::Failure(error) => {
            let _ = request.set_native_failure(native_failure(error));
            true
        }
        NativeWrite::NotFound(message) => {
            let _ = request.set_native_failure(NativeFailure {
                kind: "not_found".to_owned(),
                message,
                constraint: None,
                table: None,
                sqlstate: None,
            });
            true
        }
        NativeWrite::Timeout => {
            let _ = request.set_native_failure(NativeFailure {
                kind: "timeout".to_owned(),
                message: "request timed out".to_owned(),
                constraint: None,
                table: None,
                sqlstate: None,
            });
            true
        }
        NativeWrite::Defer(body) => {
            if body.is_some_and(|bytes| !request.buffer_native_body(bytes)) {
                request.respond_final(500, vec![(b"content-length".to_vec(), b"0".to_vec())], &[]);
                return false;
            }
            true
        }
    }
}

fn not_found_failure(plan: &CrudPlan) -> NativeFailure {
    NativeFailure {
        kind: "not_found".to_owned(),
        message: format!("{} not found", plan.model_name),
        constraint: None,
        table: None,
        sqlstate: None,
    }
}

fn json_headers(status: u16) -> Vec<(Vec<u8>, Vec<u8>)> {
    if status == 204 {
        Vec::new()
    } else {
        vec![(b"content-type".to_vec(), b"application/json".to_vec())]
    }
}

struct AbortOnDrop(Option<AbortHandle>);

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        if let Some(handle) = self.0.take() {
            handle.abort();
        }
    }
}

fn native_failure(error: DbError) -> NativeFailure {
    let kind = match error.kind {
        DbErrorKind::Unique => "unique",
        DbErrorKind::ForeignKey => "foreign_key",
        DbErrorKind::NotNull => "not_null",
        DbErrorKind::Check => "check",
        _ => "database",
    };
    NativeFailure {
        kind: kind.to_owned(),
        message: error.message,
        constraint: error.constraint,
        table: error.table,
        sqlstate: error.sqlstate,
    }
}

fn requeue_native(core: &Core, wake: &mut impl Write, request: PreparedRequest, asked: Asked) {
    match core.requeue_native(request, asked) {
        Ok(true) => {
            let _ = wake.write(&[1]);
        }
        Ok(false) => {}
        Err(error) => {
            let (request, reply) = *error;
            request.respond(reply.status, reply.headers, &reply.body);
        }
    }
}

impl Backend for CoreBackend {
    async fn handle(&self, request: Incoming) -> Answer {
        let Incoming {
            method,
            path,
            query,
            headers,
            peer,
            secure,
            http_version,
            refusal,
            body,
            trace,
        } = request;
        let (trusted, forwarded) = self.core.peer_view(peer, &headers);
        // A trusted proxy's scheme wins, like the Python resolution does.
        let https = forwarded.unwrap_or(secure);
        let head = Head {
            method: &method,
            path: &path,
            query: &query,
            headers,
            trusted,
            https,
            http_version,
            peer: Some(peer),
            refusal,
            body,
        };
        match self.core.classify(head, true) {
            Ok(outcome) => {
                trace.set(match outcome {
                    Outcome::Early(_) => Lane::Early,
                    Outcome::Queued { .. } => Lane::Python,
                    Outcome::NativeCrud { .. } => Lane::Native,
                });
                self.reply_of(outcome).await
            }
            Err(_) => internal_error(),
        }
    }
}

fn io_error(err: std::io::Error) -> PyErr {
    PyRuntimeError::new_err(err.to_string())
}

/// The listening socket behind an inherited descriptor, taking ownership of it.
///
/// Whatever happens next, the descriptor is closed with the returned error or
/// with the listener: once `fd` is passed in, the caller must treat it as gone
/// (a negative number is the one value that is not taken, it names nothing).
/// A descriptor that is not an IP stream socket is refused, and so is one that
/// is not listening where the platform lets this crate tell (Linux).
fn inherited_listener(fd: i32) -> PyResult<std::net::TcpListener> {
    if fd < 0 {
        return Err(PyValueError::new_err("fd must be a descriptor"));
    }
    // SAFETY: the caller hands over a descriptor it no longer uses and nobody
    // else owns (the Python side detaches the socket first), so this is its
    // only owner. The descriptor is then only used through the owned handle,
    // which closes it on every path below. Its kind is checked before use.
    let owned = unsafe { OwnedFd::from_raw_fd(fd) };
    let socket = Socket::from(owned);
    if socket.r#type().map_err(io_error)? != Type::STREAM {
        return Err(PyValueError::new_err("fd is not a stream socket"));
    }
    // `SO_ACCEPTCONN` is only exposed here on Linux; the Python helper that
    // prepares the descriptor checks it on every platform.
    #[cfg(target_os = "linux")]
    if !socket.is_listener().map_err(io_error)? {
        return Err(PyValueError::new_err("fd is not a listening socket"));
    }
    if socket.local_addr().map_err(io_error)?.as_socket().is_none() {
        return Err(PyValueError::new_err("fd is not an IP socket"));
    }
    Ok(socket.into())
}

fn seconds(name: &str, value: f64) -> PyResult<Duration> {
    Duration::try_from_secs_f64(value)
        .map_err(|_| PyValueError::new_err(format!("{name} must be a non-negative number")))
}

/// Records waiting for the Python logger; a full queue drops new ones.
const ACCESS_QUEUE: usize = 4096;
/// Most records handed to Python in one call, so the GIL is taken once per batch.
const ACCESS_BATCH: usize = 256;

/// For a log with a callback: a queue plus a thread that hands batches of JSON
/// lines to `callback`. The thread ends when the server drops its last sender.
fn access_sink(style: Option<AccessLog>, callback: Option<Py<PyAny>>) -> PyResult<Option<Sink>> {
    match (style, callback) {
        (Some(_), Some(callback)) => {
            let (sink, receiver) = Sink::new(ACCESS_QUEUE);
            std::thread::Builder::new()
                .name("kiso-access-log".into())
                .spawn(move || {
                    while let Ok(first) = receiver.recv() {
                        let mut batch = vec![first];
                        while batch.len() < ACCESS_BATCH {
                            let Ok(next) = receiver.try_recv() else { break };
                            batch.push(next);
                        }
                        Python::attach(|py| {
                            if let Err(error) = callback.call1(py, (batch,)) {
                                error.print(py);
                            }
                        });
                    }
                })
                .map_err(io_error)?;
            Ok(Some(sink))
        }
        (Some(AccessLog::Logger), None) => Err(PyValueError::new_err(
            "access_log 'logger' needs an access_log_sink callable",
        )),
        _ => Ok(None),
    }
}

fn access_style(name: Option<&str>) -> PyResult<Option<AccessLog>> {
    match name {
        None => Ok(None),
        Some("plain") => Ok(Some(AccessLog::Plain)),
        Some("pretty") => Ok(Some(AccessLog::Pretty)),
        Some("json") => Ok(Some(AccessLog::Json)),
        Some("logger") => Ok(Some(AccessLog::Logger)),
        Some(other) => Err(PyValueError::new_err(format!(
            "access_log must be 'plain', 'pretty', 'json' or 'logger', not {other:?}"
        ))),
    }
}

/// A listening HTTP/1.1 (and, over TLS, HTTP/2) server on the shared tokio runtime.
#[pyclass(name = "RustServer", module = "hinoki.core", frozen)]
pub struct RustServer {
    addr: SocketAddr,
    reader: UnixStream,
    stop: watch::Sender<bool>,
    task: Mutex<Option<JoinHandle<Closed>>>,
}

#[pymethods]
impl RustServer {
    /// Serve on the listening socket `fd`, which the caller created and detached
    /// (this takes ownership of it as soon as it runs: on any error the
    /// descriptor is closed, so the caller must not use it again), or bind `host:port` (port 0 picks a free
    /// one) when there is none. Errors are raised here, before anything is
    /// accepted.
    #[staticmethod]
    #[pyo3(signature = (
        core, host, port, max_connections=10_000, header_read_timeout=10.0,
        shutdown_timeout=10.0, max_header_bytes=32_768, body_read_timeout=10.0,
        fd=None, write_timeout=30.0, tls_cert=None, tls_key=None, http2=false,
        access_log=None, access_log_skip=None, access_log_slow_ms=None,
        access_log_sink=None
    ))]
    #[allow(clippy::too_many_arguments)]
    fn start(
        core: &ServerCore,
        host: &str,
        port: u16,
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
        access_log: Option<&str>,
        access_log_skip: Option<Vec<String>>,
        access_log_slow_ms: Option<f64>,
        access_log_sink: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        // First, so that every later error closes the descriptor with it.
        let inherited = fd.map(inherited_listener).transpose()?;
        let style = access_style(access_log)?;
        let sink = access_sink(style, access_log_sink)?;
        if max_connections == 0 {
            return Err(PyValueError::new_err("max_connections must be >= 1"));
        }
        if max_connections > Semaphore::MAX_PERMITS {
            return Err(PyValueError::new_err(format!(
                "max_connections must be <= {}",
                Semaphore::MAX_PERMITS
            )));
        }
        let config = ServerConfig {
            max_connections,
            header_read_timeout: seconds("header_read_timeout", header_read_timeout)?,
            body_read_timeout: seconds("body_read_timeout", body_read_timeout)?,
            write_timeout: seconds("write_timeout", write_timeout)?,
            shutdown_timeout: seconds("shutdown_timeout", shutdown_timeout)?,
            max_header_bytes,
            access_log: style,
            log_filter: Arc::new(LogFilter {
                skip: access_log_skip.unwrap_or_default(),
                slow: access_log_slow_ms
                    .map(|ms| seconds("access_log_slow_ms", ms / 1000.0))
                    .transpose()?,
                sink,
            }),
            ..ServerConfig::default()
        };
        let tls = match (tls_cert, tls_key) {
            (Some(cert), Some(key)) => Some(
                hinoki_server::tls::acceptor(cert.as_ref(), key.as_ref(), http2)
                    .map_err(|e| PyValueError::new_err(e.to_string()))?,
            ),
            (None, None) if !http2 => None,
            (None, None) => {
                return Err(PyValueError::new_err(
                    "http2 needs TLS: set tls_cert and tls_key",
                ))
            }
            _ => {
                return Err(PyValueError::new_err(
                    "tls_cert and tls_key must be given together",
                ))
            }
        };
        let listener = match inherited {
            Some(listener) => listener,
            None => std::net::TcpListener::bind((host, port)).map_err(io_error)?,
        };
        listener.set_nonblocking(true).map_err(io_error)?;
        let addr = listener.local_addr().map_err(io_error)?;
        let (reader, writer) = UnixStream::pair().map_err(io_error)?;
        reader.set_nonblocking(true).map_err(io_error)?;
        writer.set_nonblocking(true).map_err(io_error)?;
        let backend = Arc::new(CoreBackend {
            core: core.shared(),
            wake: writer,
            _reader: reader.try_clone().map_err(io_error)?,
        });
        crate::db::ensure_runtime();
        let runtime = pyo3_async_runtimes::tokio::get_runtime();
        let _enter = runtime.enter();
        let listener = TcpListener::from_std(listener).map_err(io_error)?;
        let (stop, receiver) = watch::channel(false);
        let task = runtime.spawn(serve(listener, backend, config, tls, receiver));
        Ok(Self {
            addr,
            reader,
            stop,
            task: Mutex::new(Some(task)),
        })
    }

    #[getter]
    fn port(&self) -> u16 {
        self.addr.port()
    }

    /// The descriptor to watch with `loop.add_reader`.
    #[getter]
    fn wake_fd(&self) -> i32 {
        self.reader.as_raw_fd()
    }

    /// Empty the wake socket. Call it before draining the queue, never after:
    /// a wake written between the two steps would otherwise be lost.
    /// Returns `false` once the socket is at EOF (the server ended and nothing
    /// will ever write again): the caller must stop watching it.
    fn clear_wake(&self) -> bool {
        let mut reader = &self.reader;
        let mut sink = [0u8; 256];
        loop {
            match reader.read(&mut sink) {
                Ok(0) => return false,
                Ok(_) => continue,
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(_) => return true,
            }
        }
    }

    /// Stop accepting, let running requests finish (up to `shutdown_timeout`)
    /// and resolve to the number of connections that had to be aborted. The loop
    /// must keep draining the queue while this is pending.
    fn shutdown<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let _ = self.stop.send(true);
        let task = self.task.lock().ok().and_then(|mut slot| slot.take());
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            match task {
                Some(task) => task
                    .await
                    .map(|closed| closed.aborted)
                    .map_err(|e| PyRuntimeError::new_err(e.to_string())),
                None => Ok(0),
            }
        })
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<RustServer>()?;
    Ok(())
}
