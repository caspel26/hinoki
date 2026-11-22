//! The Python side of a WebSocket: the route specs the server is built with, the
//! handshake a queued request carries, and the `WebSocket` object the handler
//! talks through. Framing, limits, ping/pong and close all run in Rust (see
//! `kiso::wsrun`); Python only moves whole messages through two bounded channels.

use std::sync::Arc;
use std::time::Duration;

use kiso::headers::Header;
use kiso::wsrun::{Accepted, Config, Message, Outbound, Session, Shared};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use tokio::sync::{mpsc, Mutex, OwnedSemaphorePermit};

use crate::completion;

/// What the server enforces for one WebSocket route.
#[derive(Debug, Clone)]
pub(crate) struct WsPlan {
    /// Allowed `Origin` values in lower case; `*` allows any.
    pub(crate) origins: Vec<Vec<u8>>,
    pub(crate) config: Config,
    /// Depth of each message queue.
    pub(crate) queue: usize,
}

/// A request that matched a WebSocket route, until Python accepts it.
#[derive(Debug)]
pub(crate) struct WsHandshake {
    pub(crate) config: Config,
    pub(crate) queue: usize,
    /// The connection slot, held until the session ends or the request is dropped.
    pub(crate) hold: Option<OwnedSemaphorePermit>,
}

/// A WebSocket route as the Python runtime declares it.
#[pyclass(name = "WebSocketSpec", module = "hinoki.core", frozen)]
pub struct WebSocketSpec {
    path: String,
    origins: Vec<Vec<u8>>,
    max_message: usize,
    queue: usize,
    ping_interval: Option<f64>,
    pong_timeout: f64,
    idle_timeout: Option<f64>,
    close_timeout: f64,
    write_timeout: f64,
}

fn period(name: &str, value: f64) -> PyResult<Duration> {
    Duration::try_from_secs_f64(value)
        .ok()
        .filter(|period| !period.is_zero())
        .ok_or_else(|| PyValueError::new_err(format!("{name} must be a positive number")))
}

fn optional_period(name: &str, value: Option<f64>) -> PyResult<Option<Duration>> {
    value.map(|secs| period(name, secs)).transpose()
}

#[pymethods]
impl WebSocketSpec {
    #[new]
    #[pyo3(signature = (
        path, origins, max_message, queue, ping_interval, pong_timeout, idle_timeout,
        close_timeout, write_timeout
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        path: String,
        origins: Vec<Vec<u8>>,
        max_message: usize,
        queue: usize,
        ping_interval: Option<f64>,
        pong_timeout: f64,
        idle_timeout: Option<f64>,
        close_timeout: f64,
        write_timeout: f64,
    ) -> PyResult<Self> {
        if max_message == 0 || queue == 0 {
            return Err(PyValueError::new_err(
                "max_message and queue must be positive",
            ));
        }
        let spec = Self {
            path,
            origins: origins
                .into_iter()
                .map(|origin| origin.to_ascii_lowercase())
                .collect(),
            max_message,
            queue,
            ping_interval,
            pong_timeout,
            idle_timeout,
            close_timeout,
            write_timeout,
        };
        spec.plan()?;
        Ok(spec)
    }

    #[getter]
    fn path(&self) -> &str {
        &self.path
    }
}

impl WebSocketSpec {
    pub(crate) fn template(&self) -> &str {
        &self.path
    }

    pub(crate) fn plan(&self) -> PyResult<WsPlan> {
        Ok(WsPlan {
            origins: self.origins.clone(),
            config: Config {
                max_message: self.max_message,
                ping_interval: optional_period("ping_interval", self.ping_interval)?,
                pong_timeout: period("pong_timeout", self.pong_timeout)?,
                idle_timeout: optional_period("idle_timeout", self.idle_timeout)?,
                close_timeout: period("close_timeout", self.close_timeout)?,
                write_timeout: period("write_timeout", self.write_timeout)?,
            },
            queue: self.queue,
        })
    }
}

/// The accepted handshake for the connection layer, and the object Python holds.
pub(crate) fn open(
    handshake: WsHandshake,
    subprotocol: Option<String>,
    headers: Vec<Header>,
) -> (Box<Accepted>, WebSocket) {
    let (inbound_tx, inbound_rx) = mpsc::channel(handshake.queue);
    let (outbound_tx, outbound_rx) = mpsc::channel(handshake.queue);
    let shared = Arc::new(Shared::default());
    let accepted = Box::new(Accepted {
        subprotocol,
        headers,
        session: Session {
            config: handshake.config,
            inbound: inbound_tx,
            outbound: outbound_rx,
            shared: Arc::clone(&shared),
            hold: handshake.hold,
        },
    });
    let socket = WebSocket {
        inbound: Arc::new(Mutex::new(inbound_rx)),
        outbound: outbound_tx,
        shared,
    };
    (accepted, socket)
}

/// One open WebSocket. Every method but `closed` returns an awaitable.
#[pyclass(name = "WebSocket", module = "hinoki.core", frozen)]
pub struct WebSocket {
    inbound: Arc<Mutex<mpsc::Receiver<Message>>>,
    outbound: mpsc::Sender<Outbound>,
    shared: Arc<Shared>,
}

fn failed(_: Python<'_>, (): ()) -> PyErr {
    PyRuntimeError::new_err("the WebSocket failed")
}

fn panicked(_: Python<'_>) -> PyErr {
    PyRuntimeError::new_err("the WebSocket task failed")
}

fn sent<'py>(
    py: Python<'py>,
    sender: mpsc::Sender<Outbound>,
    item: Outbound,
) -> PyResult<Bound<'py, PyAny>> {
    let wait = async move { Ok::<_, ()>(sender.send(item).await.is_ok()) };
    completion::submit_with(
        py,
        wait,
        |py, ok: bool| Ok(ok.into_pyobject(py)?.to_owned().into_any().unbind()),
        (failed, panicked),
    )
}

#[pymethods]
impl WebSocket {
    /// The next message: `("text", str)`, `("bytes", bytes)`, or `None` once the
    /// connection is closed and everything it delivered was read.
    fn receive<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inbound = Arc::clone(&self.inbound);
        let wait = async move { Ok::<_, ()>(inbound.lock().await.recv().await) };
        completion::submit_with(
            py,
            wait,
            |py, message: Option<Message>| {
                Ok(match message {
                    Some(Message::Text(text)) => {
                        ("text", text).into_pyobject(py)?.into_any().unbind()
                    }
                    Some(Message::Binary(data)) => ("bytes", PyBytes::new(py, &data))
                        .into_pyobject(py)?
                        .into_any()
                        .unbind(),
                    None => py.None(),
                })
            },
            (failed, panicked),
        )
    }

    /// Queue a text message; waits for room. `False`: the connection is closed.
    fn send_text<'py>(&self, py: Python<'py>, text: String) -> PyResult<Bound<'py, PyAny>> {
        sent(
            py,
            self.outbound.clone(),
            Outbound::Message(Message::Text(text)),
        )
    }

    /// Queue a binary message; waits for room. `False`: the connection is closed.
    fn send_bytes<'py>(&self, py: Python<'py>, data: Vec<u8>) -> PyResult<Bound<'py, PyAny>> {
        sent(
            py,
            self.outbound.clone(),
            Outbound::Message(Message::Binary(data)),
        )
    }

    /// Close after the messages queued before it. Raises `ValueError` for a
    /// code that may not be sent. `False`: the connection is closed already.
    #[pyo3(signature = (code=1000, reason=String::new()))]
    fn close<'py>(
        &self,
        py: Python<'py>,
        code: u16,
        reason: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        if !kiso::ws::sendable_code(code) {
            return Err(PyValueError::new_err(format!(
                "close code {code} cannot be sent"
            )));
        }
        sent(py, self.outbound.clone(), Outbound::Close(code, reason))
    }

    /// How the connection ended, as `(code, reason)`; `None` while it is open.
    #[getter]
    fn closed(&self) -> Option<(u16, String)> {
        self.shared
            .closed()
            .map(|closed| (closed.code, closed.reason))
    }
}
