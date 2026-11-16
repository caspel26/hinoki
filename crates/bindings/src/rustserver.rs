//! The socket side of the Rust server: `RustServer` binds a listener on the
//! shared tokio runtime and serves HTTP/1.1 with `hinoki-server`, handing every
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

use hinoki_server::{serve, Answer, Backend, Closed, Early, Incoming, ServerConfig};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use socket2::{Socket, Type};
use tokio::net::TcpListener;
use tokio::sync::{watch, Semaphore};
use tokio::task::JoinHandle;

use crate::server::{Core, Head, Outcome, ServerCore};

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
            refusal,
            body,
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
            peer: Some(peer),
            refusal,
            body,
        };
        match self.core.classify(head) {
            Ok(outcome) => self.reply_of(outcome).await,
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

/// A listening HTTP/1.1 server on the shared tokio runtime.
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
        fd=None, write_timeout=30.0, tls_cert=None, tls_key=None
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
    ) -> PyResult<Self> {
        // First, so that every later error closes the descriptor with it.
        let inherited = fd.map(inherited_listener).transpose()?;
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
            ..ServerConfig::default()
        };
        let tls = match (tls_cert, tls_key) {
            (Some(cert), Some(key)) => Some(
                hinoki_server::tls::acceptor(cert.as_ref(), key.as_ref())
                    .map_err(|e| PyValueError::new_err(e.to_string()))?,
            ),
            (None, None) => None,
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
