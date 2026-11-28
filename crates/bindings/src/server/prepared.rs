use std::borrow::Cow;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use hinoki_orm::Value;
use kiso::headers::{final_headers, head_headers, is_bodyless, Header};
use kiso::{BodyError, BodyReader, InputValue, Pace, Permit, Streamed};
use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use tokio::sync::{mpsc, oneshot};

use super::reply::{answer_to_py, reply_to_py, FileReply, Reply, Sent};
use super::routes::{RequestHeaders, RequestMethod, RequestUri};
use crate::completion;
use crate::handoff;
use crate::sink::{ResponseSink, CHANNEL_SLOTS};
use crate::websocket::{self, WsHandshake};

// `args[0]` is `too_large`, `disconnected` or `timeout`.
pyo3::create_exception!(core, BodyReadError, PyException);

const ALLOW_ORIGIN: &[u8] = b"access-control-allow-origin";

pub(super) type NativeArgs = Vec<(String, InputValue)>;
pub(super) type NativeDetails = Vec<(String, String, String)>;
type NativeErrorData = (String, String, Option<String>, NativeDetails);
pub(super) type DecodedPathParams = Vec<(String, String)>;

pub(super) enum PreparedPathParams {
    Decoded(DecodedPathParams),
    InvalidEncoding,
}

#[derive(Clone)]
pub(super) struct NativeInputError {
    pub(super) code: String,
    pub(super) message: String,
    pub(super) available: Option<String>,
    pub(super) details: NativeDetails,
}

/// Where a request's body is.
pub(super) enum BodyState {
    /// The request has none.
    Absent,
    Unread(BodyReader),
    /// Read by a native attempt and retained for a deferred Python request.
    Buffered(Vec<u8>),
    /// Handed to `body()`, or dropped by `respond`.
    Taken,
}

pub(crate) struct NativeBody {
    reader: Option<BodyReader>,
    consumed: Arc<AtomicBool>,
}

pub(crate) enum NativeBodyError {
    Poisoned,
    AlreadyTaken,
}

#[derive(Clone)]
pub(crate) struct NativeFailure {
    pub(crate) kind: String,
    pub(crate) message: String,
    pub(crate) constraint: Option<String>,
    pub(crate) table: Option<String>,
    pub(crate) sqlstate: Option<String>,
}

type NativeFailureTuple = (
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
);
type NativeRowsObject = (Py<PyAny>, Option<(u64, u64, u64)>);

pub(crate) struct NativeRows {
    pub(crate) rows: Vec<Vec<(String, Value)>>,
    pub(crate) page_info: Option<(u64, u64, u64)>,
}

impl NativeBody {
    pub(crate) async fn read(self, limit: usize) -> Result<Vec<u8>, BodyError> {
        let result = match self.reader {
            Some(reader) => reader.read_all(limit).await,
            None => Ok(Vec::new()),
        };
        if result.is_ok() {
            self.consumed.store(true, Ordering::Release);
        }
        result
    }
}

/// A request accepted for the Python pipeline. `respond` completes it once.
#[pyclass(name = "PreparedRequest", module = "hinoki.core", frozen)]
pub struct PreparedRequest {
    pub(super) method: RequestMethod,
    pub(super) uri: RequestUri,
    pub(super) headers: RequestHeaders,
    pub(super) request_id: String,
    pub(super) id_ignored: bool,
    pub(super) action: Option<u32>,
    pub(super) native_handler: bool,
    pub(super) native_args: NativeArgs,
    pub(super) native_error: Option<NativeInputError>,
    pub(super) native_principal: Option<super::core::NativePrincipal>,
    pub(super) native_policy: Arc<[Header]>,
    pub(super) compiled_policy: Option<Arc<[kiso::headers::CompiledHeader]>>,
    /// The CORS headers for the request's `Origin`, added to a native handler's
    /// answer unless it sets its own `access-control-allow-origin`.
    pub(super) native_cors: Vec<Header>,
    pub(super) route_headers: Vec<(String, String)>,
    pub(super) native_crud: bool,
    pub(super) request_id_name: Arc<[u8]>,
    pub(super) compiled_id_name: Option<kiso::headers::HeaderName>,
    pub(super) needs_headers: bool,
    pub(super) params: Vec<(String, String)>,
    pub(super) prepared_path_params: Option<PreparedPathParams>,
    pub(super) client: Option<std::net::SocketAddr>,
    pub(super) http_version: &'static str,
    pub(super) reply: Mutex<Option<oneshot::Sender<Sent>>>,
    pub(super) permit: Mutex<Option<Permit>>,
    pub(super) body: Mutex<BodyState>,
    pub(super) native_body_consumed: Arc<AtomicBool>,
    pub(super) native_failure: Mutex<Option<NativeFailure>>,
    pub(super) native_rows: Mutex<Option<NativeRows>>,
    pub(super) websocket: Mutex<Option<WsHandshake>>,
}

impl PreparedRequest {
    pub(crate) fn request_id_str(&self) -> &str {
        &self.request_id
    }

    pub(crate) fn header_value(&self, name: &[u8]) -> Option<Vec<u8>> {
        std::str::from_utf8(name)
            .ok()
            .and_then(|name| self.headers.joined(name))
            .map(Cow::into_owned)
    }

    /// The request header `name` when it appears exactly once (several
    /// occurrences are not read as one value).
    pub(crate) fn single_header(&self, name: &[u8]) -> Option<&[u8]> {
        std::str::from_utf8(name)
            .ok()
            .and_then(|name| self.headers.single(name).ok().flatten())
    }

    pub(crate) fn buffer_native_body(&self, bytes: Vec<u8>) -> bool {
        if !self.native_body_consumed.swap(false, Ordering::AcqRel) {
            return false;
        }
        let Ok(mut state) = self.body.lock() else {
            return false;
        };
        if !matches!(&*state, BodyState::Taken) {
            return false;
        }
        *state = BodyState::Buffered(bytes);
        true
    }

    pub(crate) fn release_admission(&self) {
        if let Ok(mut permit) = self.permit.lock() {
            permit.take();
        }
    }

    pub(super) fn restore_admission(&self, permit: Permit) -> Result<(), Permit> {
        let Ok(mut slot) = self.permit.lock() else {
            return Err(permit);
        };
        if slot.is_some() {
            return Err(permit);
        }
        *slot = Some(permit);
        Ok(())
    }

    pub(super) fn has_admission(&self) -> bool {
        self.permit.lock().is_ok_and(|slot| slot.is_some())
    }

    pub(crate) fn take_native_body(&self) -> Result<NativeBody, NativeBodyError> {
        let reader = {
            let mut state = self.body.lock().map_err(|_| NativeBodyError::Poisoned)?;
            match std::mem::replace(&mut *state, BodyState::Taken) {
                BodyState::Unread(reader) => Some(reader),
                BodyState::Absent => None,
                BodyState::Buffered(_) | BodyState::Taken => {
                    return Err(NativeBodyError::AlreadyTaken);
                }
            }
        };
        Ok(NativeBody {
            reader,
            consumed: Arc::clone(&self.native_body_consumed),
        })
    }

    pub(crate) fn set_native_failure(&self, failure: NativeFailure) -> PyResult<()> {
        if !self.native_crud {
            return Err(PyRuntimeError::new_err(
                "native failures require an armed CRUD request",
            ));
        }
        let mut slot = self.native_failure.lock().map_err(|_| poisoned())?;
        if slot.is_some() {
            return Err(PyRuntimeError::new_err(
                "a native failure is already set for this request",
            ));
        }
        if self.native_rows.lock().map_err(|_| poisoned())?.is_some() {
            return Err(PyRuntimeError::new_err(
                "native failure and rows fallback cannot both be set",
            ));
        }
        *slot = Some(failure);
        Ok(())
    }

    pub(crate) fn set_native_rows(&self, rows: NativeRows) -> PyResult<()> {
        if !self.native_crud {
            return Err(PyRuntimeError::new_err(
                "native rows require an armed CRUD request",
            ));
        }
        let failure = self.native_failure.lock().map_err(|_| poisoned())?;
        if failure.is_some() {
            return Err(PyRuntimeError::new_err(
                "native failure and rows fallback cannot both be set",
            ));
        }
        let mut slot = self.native_rows.lock().map_err(|_| poisoned())?;
        if slot.is_some() {
            return Err(PyRuntimeError::new_err(
                "native rows fallback is already set for this request",
            ));
        }
        *slot = Some(rows);
        Ok(())
    }
}

#[pymethods]
impl PreparedRequest {
    #[getter]
    fn method(&self) -> &str {
        self.method.as_str()
    }

    #[getter]
    fn path(&self) -> &str {
        self.uri.path()
    }

    #[getter]
    fn query<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, self.uri.query())
    }

    #[getter]
    fn request_id(&self) -> &str {
        &self.request_id
    }

    /// A trusted peer sent a request id header that was not valid, so a new id
    /// was generated; the Python side logs it (the classifier has no logger).
    #[getter]
    fn id_ignored(&self) -> bool {
        self.id_ignored
    }

    /// The action number, or `None` when no route matched and the Python
    /// pipeline must produce the answer (a policy is configured).
    #[getter]
    fn action(&self) -> Option<u32> {
        self.action
    }

    #[getter]
    fn native_handler(&self) -> bool {
        self.native_handler
    }

    #[getter]
    fn has_native_args(&self) -> bool {
        !self.native_args.is_empty()
    }

    #[getter]
    fn native_args<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let args = PyDict::new(py);
        for (name, value) in &self.native_args {
            match value {
                InputValue::Str(text) => args.set_item(name, text)?,
                InputValue::Int(number) => args.set_item(name, number)?,
                InputValue::Float(number) => args.set_item(name, number)?,
                InputValue::Bool(flag) => args.set_item(name, flag)?,
                InputValue::Uuid(text) => {
                    let parsed = uuid::Uuid::parse_str(text)
                        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
                    args.set_item(name, crate::pytypes::new_uuid(py, parsed)?)?;
                }
            }
        }
        Ok(args)
    }

    /// The caller Rust authenticated (`sub`, string scopes, and verified claims), if it did.
    #[getter]
    #[allow(clippy::type_complexity)]
    fn native_principal<'py>(
        &self,
        py: Python<'py>,
    ) -> PyResult<Option<(String, Vec<String>, Bound<'py, pyo3::types::PyDict>)>> {
        self.native_principal
            .as_ref()
            .map(|(subject, scopes, raw_claims)| {
                Ok((
                    subject.clone(),
                    scopes.clone(),
                    crate::jwt::map_to_py_dict(py, raw_claims)?,
                ))
            })
            .transpose()
    }

    #[getter]
    fn native_error(&self) -> Option<NativeErrorData> {
        self.native_error.as_ref().map(|error| {
            (
                error.code.clone(),
                error.message.clone(),
                error.available.clone(),
                error.details.clone(),
            )
        })
    }

    /// Whether the route reads the headers as a mapping (access, negotiation
    /// among several renderers, or a `Request` parameter). `True` when unrouted.
    #[getter]
    fn needs_headers(&self) -> bool {
        self.needs_headers
    }

    /// The HTTP version the request arrived with: `"2"` or `"1.1"`.
    #[getter]
    fn http_version(&self) -> &str {
        self.http_version
    }

    /// The socket peer as `(ip, port)`, when the request came over a socket.
    #[getter]
    fn client(&self) -> Option<(String, u16)> {
        self.client.map(|peer| (peer.ip().to_string(), peer.port()))
    }

    /// Path parameters as matched, still percent-encoded.
    #[getter]
    fn params<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        for (name, value) in &self.params {
            out.set_item(name, value)?;
        }
        Ok(out)
    }

    /// Decoded path captures for the Python pipeline, or `None` when decoding
    /// failed. Native handlers do not prepare this value because their input
    /// plan already decodes the captures.
    #[getter]
    fn prepared_path_params<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyDict>>> {
        let Some(prepared) = &self.prepared_path_params else {
            return Err(PyRuntimeError::new_err(
                "path parameters were not prepared for the Python pipeline",
            ));
        };
        let PreparedPathParams::Decoded(values) = prepared else {
            return Ok(None);
        };
        let out = PyDict::new(py);
        for (name, value) in values {
            out.set_item(name, value)?;
        }
        Ok(Some(out))
    }

    #[getter]
    fn headers<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        self.headers.to_py_list(py)
    }

    /// Lower-cased, comma-joined header dict built directly from the native
    /// header storage without first materializing a Python list of byte pairs.
    #[getter]
    fn header_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.headers.to_joined_py_dict(py)
    }

    /// Look up one lower-case header value without materializing `header_dict`.
    fn header(&self, name: &str) -> Option<String> {
        self.headers.lookup(name)
    }

    /// Read the request body (empty bytes when there is none), refusing more
    /// than `limit` bytes. The bytes are read on the runtime and the Python
    /// object is built on the loop thread. Nothing is read, and no `100
    /// Continue` sent, before this is called. Raises `BodyReadError` (`args[0]`:
    /// `too_large`, `disconnected` or `timeout`). Calling it twice raises
    /// `RuntimeError`.
    fn body<'py>(&self, py: Python<'py>, limit: usize) -> PyResult<Bound<'py, PyAny>> {
        let (reader, buffered) = {
            let mut state = self.body.lock().map_err(|_| poisoned())?;
            match std::mem::replace(&mut *state, BodyState::Taken) {
                BodyState::Unread(reader) => (Some(reader), None),
                BodyState::Buffered(bytes) => (None, Some(bytes)),
                BodyState::Absent => {
                    *state = BodyState::Absent;
                    (None, None)
                }
                BodyState::Taken => {
                    return Err(PyRuntimeError::new_err(
                        "the request body was already taken",
                    ))
                }
            }
        };
        let read = async move {
            match buffered {
                Some(bytes) if bytes.len() > limit => Err(BodyError::TooLarge),
                Some(bytes) => Ok(bytes),
                None => match reader {
                    Some(reader) => reader.read_all(limit).await,
                    None => Ok(Vec::new()),
                },
            }
        };
        let fail = |_: Python<'_>, e: BodyError| BodyReadError::new_err(e.to_string());
        let panicked = |_: Python<'_>| PyRuntimeError::new_err("the native body task failed");
        completion::submit_with(
            py,
            read,
            |py, bytes: Vec<u8>| Ok(PyBytes::new(py, &bytes).into_any().unbind()),
            (fail, panicked),
        )
    }

    /// Retain bytes consumed by a native attempt for the Python fallback.
    fn _buffer_body(&self, bytes: &[u8]) -> PyResult<()> {
        if !self.native_body_consumed.swap(false, Ordering::AcqRel) {
            return Err(PyRuntimeError::new_err(
                "only a completed native body read can be buffered",
            ));
        }
        let mut state = self.body.lock().map_err(|_| poisoned())?;
        if matches!(&*state, BodyState::Taken) {
            *state = BodyState::Buffered(bytes.to_vec());
            Ok(())
        } else {
            Err(PyRuntimeError::new_err(
                "the request body cannot be buffered in its current state",
            ))
        }
    }

    fn _take_native_failure(&self) -> PyResult<Option<NativeFailureTuple>> {
        let mut slot = self.native_failure.lock().map_err(|_| poisoned())?;
        Ok(slot.take().map(|failure| {
            (
                failure.kind.clone(),
                failure.message.clone(),
                failure.constraint.clone(),
                failure.table.clone(),
                failure.sqlstate.clone(),
            )
        }))
    }

    #[pyo3(signature = (kind, message, constraint=None, table=None, sqlstate=None))]
    fn _set_native_failure(
        &self,
        kind: String,
        message: String,
        constraint: Option<String>,
        table: Option<String>,
        sqlstate: Option<String>,
    ) -> PyResult<()> {
        self.set_native_failure(NativeFailure {
            kind,
            message,
            constraint,
            table,
            sqlstate,
        })
    }

    fn _take_native_rows(&self, py: Python<'_>) -> PyResult<Option<NativeRowsObject>> {
        let mut slot = self.native_rows.lock().map_err(|_| poisoned())?;
        slot.take()
            .map(|rows| Ok((crate::db::rows_to_py(py, &rows.rows)?, rows.page_info)))
            .transpose()
    }

    #[pyo3(signature = (rows, page_info=None))]
    fn _set_native_rows(
        &self,
        rows: &Bound<'_, PyList>,
        page_info: Option<(u64, u64, u64)>,
    ) -> PyResult<()> {
        let mut values = Vec::with_capacity(rows.len());
        for row in rows.iter() {
            let dictionary = row.cast::<PyDict>()?;
            let mut fields = Vec::with_capacity(dictionary.len());
            for (name, value) in dictionary.iter() {
                fields.push((name.extract()?, crate::db::to_value(&value)?));
            }
            values.push(fields);
        }
        self.set_native_rows(NativeRows {
            rows: values,
            page_info,
        })
    }

    /// Read a body for a native attempt and mark it reusable only after the
    /// read has completed successfully.
    fn _native_body<'py>(&self, py: Python<'py>, limit: usize) -> PyResult<Bound<'py, PyAny>> {
        let body = self.take_native_body().map_err(|error| match error {
            NativeBodyError::Poisoned => poisoned(),
            NativeBodyError::AlreadyTaken => {
                PyRuntimeError::new_err("the request body was already taken")
            }
        })?;
        let read = body.read(limit);
        let fail = |_: Python<'_>, e: BodyError| BodyReadError::new_err(e.to_string());
        let panicked = |_: Python<'_>| PyRuntimeError::new_err("the native body task failed");
        completion::submit_with(
            py,
            read,
            |py, bytes: Vec<u8>| Ok(PyBytes::new(py, &bytes).into_any().unbind()),
            (fail, panicked),
        )
    }

    /// Complete the request with the final status, headers and body. Returns
    /// `False` when it was already completed or nobody is waiting any more.
    pub(crate) fn respond(
        &self,
        status: u16,
        headers: Vec<(Vec<u8>, Vec<u8>)>,
        body: &[u8],
    ) -> bool {
        self.respond_owned(status, headers, bytes::Bytes::copy_from_slice(body))
    }

    /// Answer with a head now and the body as it is produced. The returned
    /// sink feeds the body; `None` when the request was already answered or
    /// nobody is waiting. With `length` the body must be exactly that long,
    /// otherwise it ends chunked (HTTP/1.1) or by the end of the stream
    /// (HTTP/2). `idle_timeout` cuts the response when the producer sends
    /// nothing for that many seconds; `keepalive` sends an SSE comment after
    /// that many seconds of silence. Both timers run in Rust.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (status, headers, route_headers, idle_timeout=None, keepalive=None, length=None))]
    pub(crate) fn respond_stream(
        &self,
        py: Python<'_>,
        status: u16,
        headers: Vec<(Vec<u8>, Vec<u8>)>,
        route_headers: Vec<(String, String)>,
        idle_timeout: Option<f64>,
        keepalive: Option<f64>,
        length: Option<u64>,
    ) -> PyResult<Option<Py<ResponseSink>>> {
        refuse_bodyless(status)?;
        let pace = Pace {
            idle: seconds("idle_timeout", idle_timeout)?,
            keepalive: seconds("keepalive", keepalive)?,
        };
        let mut head = self.head_of(headers, &route_headers);
        if let Some(length) = length {
            head.push((b"content-length".to_vec(), length.to_string().into_bytes()));
        }
        let Some(tx) = self.release() else {
            return Ok(None);
        };
        let (sender, chunks) = mpsc::channel(CHANNEL_SLOTS);
        let streamed = Streamed {
            status,
            headers: head,
            chunks,
            pace,
        };
        if tx.send(Sent::Streamed(streamed)).is_err() {
            return Ok(None);
        }
        Py::new(py, ResponseSink::new(sender)).map(Some)
    }

    /// Accept the WebSocket handshake: the connection answers 101 with the
    /// negotiated `subprotocol` and `headers`, and the returned object carries
    /// the messages. `None` when the request was answered already or nobody
    /// waits for it.
    #[pyo3(signature = (subprotocol=None, headers=Vec::new()))]
    pub(crate) fn accept_websocket(
        &self,
        subprotocol: Option<String>,
        headers: Vec<(Vec<u8>, Vec<u8>)>,
    ) -> PyResult<Option<websocket::WebSocket>> {
        let handshake = self
            .websocket
            .lock()
            .map_err(|_| poisoned())?
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("not a WebSocket handshake"))?;
        let Some(tx) = self.reply.lock().ok().and_then(|mut slot| slot.take()) else {
            return Ok(None);
        };
        let (accepted, socket) = websocket::open(handshake, subprotocol, headers);
        self.release_admission();
        Ok(tx.send(Sent::Upgrade(accepted)).ok().map(|()| socket))
    }

    /// Answer with the file at `path`, whose size becomes the `content-length`.
    /// The connection opens it: a missing or non-regular file answers 404 and
    /// any other failure 500. Returns `False` when the request was already
    /// answered or nobody is waiting.
    pub(crate) fn respond_file(
        &self,
        status: u16,
        headers: Vec<(Vec<u8>, Vec<u8>)>,
        route_headers: Vec<(String, String)>,
        path: PathBuf,
    ) -> PyResult<bool> {
        refuse_bodyless(status)?;
        let head = self.head_of(headers, &route_headers);
        let sent = Sent::File(FileReply {
            status,
            headers: head,
            path,
        });
        Ok(self.release().is_some_and(|tx| handoff::reply(tx, sent)))
    }

    /// Finalize an automatically eligible handler response with the route's
    /// native policy, request-id and CORS headers, as `Pipeline.with_headers`
    /// does. HEAD retains GET's content length.
    pub(crate) fn respond_final(
        &self,
        status: u16,
        headers: Vec<(Vec<u8>, Vec<u8>)>,
        body: &[u8],
    ) -> bool {
        self.respond_final_owned(status, headers, bytes::Bytes::copy_from_slice(body))
    }

    /// Encode `value` directly into the owned response buffer and complete the
    /// native request without creating an intermediate Python `bytes` object.
    /// With `strict`, the value must also be a plain `list[dict[str, object]]`
    /// (see `encode_json_bytes_strict`); any other value is an error that sends
    /// nothing, and the caller then runs the checked path.
    #[pyo3(signature = (status, headers, value, strict=false))]
    pub(crate) fn respond_final_json(
        &self,
        status: u16,
        headers: Option<Vec<(Vec<u8>, Vec<u8>)>>,
        value: &Bound<'_, PyAny>,
        strict: bool,
    ) -> PyResult<bool> {
        let body = if strict {
            crate::request::encode_json_bytes_strict(value)?
        } else {
            crate::request::encode_json_bytes(value)?
        };
        if headers.is_none() && (self.native_handler || self.native_crud) {
            if let Some(sent) = self.finish_fast_json(status, body.clone()) {
                return Ok(sent);
            }
        }
        let own = headers
            .unwrap_or_else(|| vec![(b"content-type".to_vec(), b"application/json".to_vec())]);
        Ok(self.respond_final_owned(status, own, body))
    }

    /// The same finalization for an answer the Python pipeline built: the
    /// route's own headers are the ones that pipeline resolved for the request.
    pub(crate) fn respond_routed(
        &self,
        status: u16,
        headers: Vec<(Vec<u8>, Vec<u8>)>,
        body: &[u8],
        route_headers: Vec<(String, String)>,
    ) -> bool {
        self.finish(
            status,
            headers,
            bytes::Bytes::copy_from_slice(body),
            &route_headers,
        )
    }
}

impl PreparedRequest {
    /// `respond` for a body Rust already owns: the bytes move into the reply.
    pub(crate) fn respond_owned(
        &self,
        status: u16,
        headers: Vec<(Vec<u8>, Vec<u8>)>,
        body: impl Into<bytes::Bytes>,
    ) -> bool {
        let sender = self.release();
        sender.is_some_and(|tx| {
            handoff::reply(
                tx,
                Sent::Whole(Reply {
                    status,
                    headers,
                    body: body.into(),
                }),
            )
        })
    }

    /// `respond_final` for a body Rust already owns: the bytes move into the reply.
    pub(crate) fn respond_final_owned(
        &self,
        status: u16,
        headers: Vec<(Vec<u8>, Vec<u8>)>,
        body: impl Into<bytes::Bytes>,
    ) -> bool {
        if !self.native_handler && !self.native_crud {
            return false;
        }
        self.finish(status, headers, body.into(), &self.route_headers)
    }

    /// The CORS headers to add: none when the answer or the route sets its own.
    fn cors_for(&self, own: &[Header], route_headers: &[(String, String)]) -> &[Header] {
        if self.native_cors.is_empty() {
            return &[];
        }
        let sets_origin = own
            .iter()
            .chain(self.native_policy.iter())
            .any(|(name, _)| name.eq_ignore_ascii_case(ALLOW_ORIGIN))
            || route_headers
                .iter()
                .any(|(name, _)| name.eq_ignore_ascii_case("access-control-allow-origin"));
        if sets_origin {
            &[]
        } else {
            &self.native_cors
        }
    }

    /// A head without `content-length`, for a body whose size the connection or
    /// the producer decides.
    fn head_of(&self, own: Vec<Header>, route_headers: &[(String, String)]) -> Vec<Header> {
        let cors = self.cors_for(&own, route_headers);
        head_headers(
            own,
            &self.native_policy,
            route_headers,
            (&self.request_id_name, self.request_id.as_bytes()),
            cors,
        )
    }

    /// Give up the in-flight slot and the unread body, and take the reply
    /// sender. The slot is free as soon as the answer is given, not when Python
    /// lets go of this object, and so is an unread body: hyper cannot finish a
    /// connection while the body is still held.
    fn release(&self) -> Option<oneshot::Sender<Sent>> {
        if let Ok(mut permit) = self.permit.lock() {
            permit.take();
        }
        if let Ok(mut state) = self.body.lock() {
            *state = BodyState::Taken;
        }
        self.reply.lock().ok().and_then(|mut slot| slot.take())
    }

    fn finish_fast_json(&self, status: u16, body: bytes::Bytes) -> Option<bool> {
        if !self.native_cors.is_empty() || !self.route_headers.is_empty() {
            return None;
        }
        let policy = self.compiled_policy.as_deref()?;
        let id_name = self.compiled_id_name.as_ref()?;
        let headers = kiso::headers::final_json_header_map(
            policy,
            id_name,
            self.request_id.as_bytes(),
            status,
            body.len(),
        )?;
        let final_body = if self.method.as_str() == "HEAD" {
            bytes::Bytes::new()
        } else {
            body
        };
        let sender = self.release();
        Some(sender.is_some_and(|tx| {
            handoff::reply(
                tx,
                Sent::Fast(Box::new(kiso::FastAnswer {
                    status,
                    headers,
                    body: final_body,
                })),
            )
        }))
    }

    fn finish(
        &self,
        status: u16,
        own: Vec<(Vec<u8>, Vec<u8>)>,
        body: bytes::Bytes,
        route_headers: &[(String, String)],
    ) -> bool {
        if route_headers.is_empty()
            && self.native_cors.is_empty()
            && own.len() == 1
            && own[0].0.eq_ignore_ascii_case(b"content-type")
            && own[0].1 == b"application/json"
        {
            if let Some(sent) = self.finish_fast_json(status, body.clone()) {
                return sent;
            }
        }
        let body_len = body.len();
        let cors = self.cors_for(&own, route_headers);
        let final_headers = final_headers(
            own,
            &self.native_policy,
            route_headers,
            (&self.request_id_name, self.request_id.as_bytes()),
            cors,
            status,
            body_len,
        );
        let final_body = if self.method.as_str() == "HEAD" {
            bytes::Bytes::new()
        } else {
            body
        };
        let sender = self.release();
        sender.is_some_and(|tx| {
            handoff::reply(
                tx,
                Sent::Whole(Reply {
                    status,
                    headers: final_headers,
                    body: final_body,
                }),
            )
        })
    }
}

/// Bodyless statuses (204, 304) cannot carry a streamed or file body.
fn refuse_bodyless(status: u16) -> PyResult<()> {
    if is_bodyless(status) {
        return Err(PyValueError::new_err(format!(
            "status {status} cannot have a body"
        )));
    }
    Ok(())
}

/// A positive finite number of seconds, or `None` for no timer.
fn seconds(name: &str, value: Option<f64>) -> PyResult<Option<Duration>> {
    value
        .map(|secs| {
            Duration::try_from_secs_f64(secs)
                .ok()
                .filter(|period| !period.is_zero())
                .ok_or_else(|| PyValueError::new_err(format!("{name} must be a positive number")))
        })
        .transpose()
}

/// What `ServerCore.submit` returns: an immediate answer, or a queued request.
#[pyclass(name = "Submission", module = "hinoki.core", frozen)]
pub struct Submission {
    pub(super) early: Option<Reply>,
    pub(super) wake: bool,
    pub(super) receiver: Mutex<Option<oneshot::Receiver<Sent>>>,
    pub(super) delivered: Mutex<Option<Sent>>,
}

#[pymethods]
impl Submission {
    /// `(status, headers, body)` when the answer needed no Python.
    #[getter]
    fn early<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        self.early
            .as_ref()
            .map(|r| answer_to_py(py, r.status, &r.headers, &r.body))
            .transpose()
    }

    /// Whether the queue went from empty to non-empty (the consumer needs a wake).
    #[getter]
    fn wake(&self) -> bool {
        self.wake
    }

    /// The answer given through `respond`, once it has arrived (never blocks);
    /// the same answer on every later call. `None` while it is pending. Raises
    /// `RuntimeError` when the request was dropped without being answered, so a
    /// poller does not wait for an answer that will never come. Replies this
    /// thread still holds for a batched hand-off go over first.
    fn reply<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        crate::handoff::deliver_queued();
        let mut delivered = self.delivered.lock().map_err(|_| poisoned())?;
        if delivered.is_none() {
            let mut slot = self.receiver.lock().map_err(|_| poisoned())?;
            let Some(receiver) = slot.as_mut() else {
                return Ok(None);
            };
            match receiver.try_recv() {
                Ok(reply) => *delivered = Some(reply),
                Err(oneshot::error::TryRecvError::Empty) => return Ok(None),
                Err(oneshot::error::TryRecvError::Closed) => {
                    return Err(PyRuntimeError::new_err(
                        "the request was dropped without an answer",
                    ))
                }
            }
        }
        delivered.as_ref().map(|r| reply_to_py(py, r)).transpose()
    }

    /// Stop waiting, as a client that went away does.
    fn cancel(&self) {
        if let Ok(mut slot) = self.receiver.lock() {
            slot.take();
        }
    }
}

pub(super) fn poisoned() -> PyErr {
    PyRuntimeError::new_err("server state is poisoned")
}
