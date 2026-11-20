//! Glue between `kiso` and Python: the request classifier (`Core`, held
//! by the Python `ServerCore`), the request handed to the pipeline
//! (`PreparedRequest`) and the handle a test holds to see the answer
//! (`Submission`).
//!
//! Python objects exist only on the calling thread: `PreparedRequest` holds Rust
//! data and a oneshot sender, so it can be dropped on any thread without the GIL.
//! `Core` is shared with the tokio side behind an `Arc` and holds no Python
//! object; `kiso.rs` drives `Core::classify` from the connection layer, and
//! the tests call `submit` the same way without a socket.

use std::borrow::Cow;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use hinoki_orm::{Database, Value};
use kiso::cors::{Cors, PreflightError};
use kiso::early::TemplateError;
use kiso::headers::{final_headers, Header};
use kiso::hosts::Hosts;
use kiso::queue::Push;
use kiso::{
    convert, proxy, requestid, Converted, Early, EarlyBodies, InFlight, InputKind, InputParam,
    InputValue, Limits, NativeJobs, Networks, Permit, Plan, Queue, RoutePlan, Trace,
};
use kiso::{Answer, BodyError, BodyReader};
use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use tokio::sync::oneshot;

use crate::completion;
use crate::crudplan::{CrudPlan, PyCrudPlan};
use crate::db::PyDatabase;
use crate::request;
use crate::router::{Match, Router};
use kiso::redirect;

// `args[0]` is `too_large`, `disconnected` or `timeout`.
pyo3::create_exception!(core, BodyReadError, PyException);

const ALLOW_ORIGIN: &[u8] = b"access-control-allow-origin";

pub type Reply = Answer;
type NativeArgs = Vec<(String, InputValue)>;
type NativeDetails = Vec<(String, String, String)>;
type NativeErrorData = (String, String, Option<String>, NativeDetails);
type DecodedPathParams = Vec<(String, String)>;
type HostsSpec = (Vec<Vec<u8>>, Vec<Vec<u8>>);

enum PreparedPathParams {
    Decoded(DecodedPathParams),
    InvalidEncoding,
}

fn reply_to_py<'py>(py: Python<'py>, reply: &Reply) -> PyResult<Bound<'py, PyAny>> {
    let headers = PyList::empty(py);
    for (name, value) in &reply.headers {
        headers.append((PyBytes::new(py, name), PyBytes::new(py, value)))?;
    }
    Ok((reply.status, headers, PyBytes::new(py, &reply.body))
        .into_pyobject(py)?
        .into_any())
}

/// Where a request's body is.
enum BodyState {
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
    method: String,
    path: String,
    query: Vec<u8>,
    headers: Vec<Header>,
    request_id: String,
    id_ignored: bool,
    action: Option<u32>,
    native_handler: bool,
    native_args: NativeArgs,
    native_error: Option<NativeInputError>,
    native_policy: Vec<Header>,
    /// The CORS headers for the request's `Origin`, added to a native handler's
    /// answer unless it sets its own `access-control-allow-origin`.
    native_cors: Vec<Header>,
    route_headers: Vec<(String, String)>,
    native_crud: bool,
    request_id_name: Vec<u8>,
    needs_headers: bool,
    params: Vec<(String, String)>,
    prepared_path_params: Option<PreparedPathParams>,
    client: Option<(String, u16)>,
    http_version: &'static str,
    reply: Mutex<Option<oneshot::Sender<Reply>>>,
    permit: Mutex<Option<Permit>>,
    body: Mutex<BodyState>,
    native_body_consumed: Arc<AtomicBool>,
    native_failure: Mutex<Option<NativeFailure>>,
    native_rows: Mutex<Option<NativeRows>>,
}

impl PreparedRequest {
    pub(crate) fn request_id_str(&self) -> &str {
        &self.request_id
    }

    pub(crate) fn header_value(&self, name: &[u8]) -> Option<Vec<u8>> {
        joined_value(&self.headers, name).map(Cow::into_owned)
    }

    /// The request header `name` when it appears exactly once (several
    /// occurrences are not read as one value).
    pub(crate) fn single_header(&self, name: &[u8]) -> Option<&[u8]> {
        let mut found = self
            .headers
            .iter()
            .filter(|(header, _)| header.eq_ignore_ascii_case(name));
        match (found.next(), found.next()) {
            (Some((_, value)), None) => Some(value.as_slice()),
            _ => None,
        }
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

    fn restore_admission(&self, permit: Permit) -> Result<(), Permit> {
        let Ok(mut slot) = self.permit.lock() else {
            return Err(permit);
        };
        if slot.is_some() {
            return Err(permit);
        }
        *slot = Some(permit);
        Ok(())
    }

    fn has_admission(&self) -> bool {
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
        self.client.clone()
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
        let out = PyList::empty(py);
        for (name, value) in &self.headers {
            out.append((PyBytes::new(py, name), PyBytes::new(py, value)))?;
        }
        Ok(out)
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
        // The slot is free as soon as the answer is given, not when Python lets
        // go of this object. So is an unread body: hyper cannot finish a
        // connection while the body is still held.
        if let Ok(mut permit) = self.permit.lock() {
            permit.take();
        }
        if let Ok(mut state) = self.body.lock() {
            *state = BodyState::Taken;
        }
        let sender = self.reply.lock().ok().and_then(|mut slot| slot.take());
        sender.is_some_and(|tx| {
            tx.send(Reply {
                status,
                headers,
                body: body.to_vec(),
            })
            .is_ok()
        })
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
        if !self.native_handler && !self.native_crud {
            return false;
        }
        self.finish(status, headers, body, &self.route_headers)
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
        self.finish(status, headers, body, &route_headers)
    }
}

impl PreparedRequest {
    fn finish(
        &self,
        status: u16,
        own: Vec<(Vec<u8>, Vec<u8>)>,
        body: &[u8],
        route_headers: &[(String, String)],
    ) -> bool {
        // An answer that sets its own CORS headers keeps them.
        let sets_origin = own
            .iter()
            .chain(&self.native_policy)
            .any(|(name, _)| name.eq_ignore_ascii_case(ALLOW_ORIGIN))
            || route_headers
                .iter()
                .any(|(name, _)| name.eq_ignore_ascii_case("access-control-allow-origin"));
        let cors: &[Header] = if sets_origin { &[] } else { &self.native_cors };
        let mut final_headers = final_headers(
            &own,
            &self.native_policy,
            route_headers,
            (&self.request_id_name, self.request_id.as_bytes()),
            cors,
            status,
            body.len(),
        );
        let mut final_body = body.to_vec();
        if self.method == "HEAD" {
            final_body.clear();
        }
        if let Ok(mut permit) = self.permit.lock() {
            permit.take();
        }
        if let Ok(mut state) = self.body.lock() {
            *state = BodyState::Taken;
        }
        let sender = self.reply.lock().ok().and_then(|mut slot| slot.take());
        sender.is_some_and(|tx| {
            tx.send(Reply {
                status,
                headers: std::mem::take(&mut final_headers),
                body: final_body,
            })
            .is_ok()
        })
    }
}

/// What `ServerCore.submit` returns: an immediate answer, or a queued request.
#[pyclass(name = "Submission", module = "hinoki.core", frozen)]
pub struct Submission {
    early: Option<Reply>,
    wake: bool,
    receiver: Mutex<Option<oneshot::Receiver<Reply>>>,
    delivered: Mutex<Option<Reply>>,
}

#[pymethods]
impl Submission {
    /// `(status, headers, body)` when the answer needed no Python.
    #[getter]
    fn early<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        self.early.as_ref().map(|r| reply_to_py(py, r)).transpose()
    }

    /// Whether the queue went from empty to non-empty (the consumer needs a wake).
    #[getter]
    fn wake(&self) -> bool {
        self.wake
    }

    /// The answer given through `respond`, once it has arrived (never blocks);
    /// the same answer on every later call. `None` while it is pending. Raises
    /// `RuntimeError` when the request was dropped without being answered, so a
    /// poller does not wait for an answer that will never come.
    fn reply<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
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

fn poisoned() -> PyErr {
    PyRuntimeError::new_err("server state is poisoned")
}

fn valid_startup_path(path: &str) -> bool {
    !path.is_empty()
        && path.starts_with('/')
        && path != "/"
        && !path.contains("//")
        && path.is_ascii()
        && path
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"/-._~".contains(&byte))
        && !path
            .split('/')
            .skip(1)
            .any(|part| part == "." || part == "..")
}

/// A route as Python describes it: the plan fields plus the router entry.
type RouteSpec = (
    u32,                                         // action
    String,                                      // method
    String,                                      // template
    bool,                                        // has body
    bool,                                        // has access
    Vec<String>,                                 // media types
    u16,                                         // status
    Vec<(String, String)>,                       // route headers
    bool,                                        // transaction
    bool,                                        // takes request
    (bool, Vec<(String, String, bool, String)>), // native handler and source/name/default/kind
    Option<Py<PyCrudPlan>>,                      // dark generated CRUD plan
);

#[derive(Clone)]
struct NativeInputError {
    code: String,
    message: String,
    available: Option<String>,
    details: NativeDetails,
}

#[derive(Default)]
struct NativeInput {
    enabled: bool,
    args: NativeArgs,
    error: Option<NativeInputError>,
}

impl NativeInput {
    fn failed(error: NativeInputError) -> Self {
        Self {
            enabled: true,
            error: Some(error),
            ..Self::default()
        }
    }
}

fn native_error(code: &str, message: &str) -> NativeInputError {
    NativeInputError {
        code: code.to_owned(),
        message: message.to_owned(),
        available: None,
        details: Vec::new(),
    }
}

fn prepare_native_inputs(
    plan: &RoutePlan,
    path_params: &[(String, String)],
    query: &[u8],
    headers: &[Header],
) -> Option<NativeInput> {
    let decoded_path = match decode_native_path(plan, path_params) {
        Ok(path) => path,
        Err(error) => return Some(NativeInput::failed(error)),
    };
    let accept = joined_value(headers, b"accept").map_or_else(String::new, |v| latin1(&v));
    if request::negotiate(&accept, &plan.media_types).is_none() {
        let mut error = native_error("not_acceptable", "not acceptable");
        error.available = Some(plan.media_types.join(", "));
        return Some(NativeInput::failed(error));
    }
    let parsed = match request::parse_query(query, 1000) {
        Ok(parsed) => parsed,
        Err(_) => {
            return Some(NativeInput::failed(native_error(
                "invalid_query",
                "malformed query string",
            )));
        }
    };
    let values = query_values(parsed);
    let (args, errors) = collect_native_args(plan, &decoded_path, &values)?;
    Some(if errors.is_empty() {
        NativeInput {
            enabled: true,
            args,
            error: None,
        }
    } else {
        let mut error = native_error("validation_error", "invalid request parameters");
        error.details = errors;
        NativeInput::failed(error)
    })
}

fn decode_native_path(
    plan: &RoutePlan,
    params: &[(String, String)],
) -> Result<HashMap<String, String>, NativeInputError> {
    let raw: HashMap<_, _> = params
        .iter()
        .map(|(name, value)| (name.clone(), value.clone()))
        .collect();
    let mut decoded = HashMap::new();
    for input in &plan.input_params {
        let name = &input.name;
        if input.source != "path" {
            continue;
        }
        let Some(value) = raw.get(name) else {
            return Err(native_error(
                "internal_error",
                "missing matched path parameter",
            ));
        };
        let Some(value) = request::decode_path_parameter(value) else {
            return Err(native_error("bad_path_encoding", "invalid path encoding"));
        };
        decoded.insert(name.clone(), value);
    }
    Ok(decoded)
}

fn prepare_python_path_params(params: &[(String, String)]) -> PreparedPathParams {
    let mut decoded = Vec::with_capacity(params.len());
    for (name, value) in params {
        let Some(value) = request::decode_path_parameter(value) else {
            return PreparedPathParams::InvalidEncoding;
        };
        decoded.push((name.clone(), value));
    }
    PreparedPathParams::Decoded(decoded)
}

fn query_values(pairs: Vec<(String, String)>) -> HashMap<String, Vec<String>> {
    let mut values = HashMap::new();
    for (name, value) in pairs {
        values.entry(name).or_insert_with(Vec::new).push(value);
    }
    values
}

enum Lookup<'a> {
    Found(&'a String),
    Absent,
    Duplicate,
}

fn lookup<'a>(
    input: &InputParam,
    path: &'a HashMap<String, String>,
    query: &'a HashMap<String, Vec<String>>,
) -> Lookup<'a> {
    let found = if input.source == "path" {
        return path.get(&input.name).map_or(Lookup::Absent, Lookup::Found);
    } else {
        query.get(&input.name).map_or(&[][..], Vec::as_slice)
    };
    match found {
        [] => Lookup::Absent,
        [one] => Lookup::Found(one),
        _ => Lookup::Duplicate,
    }
}

fn collect_native_args(
    plan: &RoutePlan,
    path: &HashMap<String, String>,
    query: &HashMap<String, Vec<String>>,
) -> Option<(NativeArgs, NativeDetails)> {
    let mut args = Vec::new();
    let mut errors = Vec::new();
    for input in &plan.input_params {
        let name = &input.name;
        match lookup(input, path, query) {
            Lookup::Found(value) => match convert(input.kind, value.clone()) {
                Converted::Value(value) => args.push((name.clone(), value)),
                Converted::Invalid => {
                    errors.push((name.clone(), "type".into(), "invalid value".into()));
                }
                Converted::Defer => return None,
            },
            Lookup::Duplicate => errors.push((
                name.clone(),
                "duplicate".into(),
                "given more than once".into(),
            )),
            Lookup::Absent if input.source == "query" && !input.has_default => errors.push((
                name.clone(),
                "required".into(),
                "query parameter is required".into(),
            )),
            Lookup::Absent => {}
        }
    }
    Some((args, errors))
}

/// `(any_origin, origins, credentials, expose, preflight headers)` of the CORS policy.
type CorsSpec = (bool, Vec<Vec<u8>>, bool, Vec<u8>, Vec<Header>);

fn kind_of(name: &str) -> PyResult<Early> {
    Ok(match name {
        "uri_too_long" => Early::UriTooLong,
        "headers_too_large" => Early::HeadersTooLarge,
        "not_ready" => Early::NotReady,
        "overloaded" => Early::Overloaded,
        "not_found" => Early::NotFound,
        "method_not_allowed" => Early::MethodNotAllowed,
        "invalid_query" => Early::InvalidQuery,
        "not_acceptable" => Early::NotAcceptable,
        "bad_host" => Early::BadHost,
        "disallowed_host" => Early::DisallowedHost,
        "cors_origin_denied" => Early::CorsOriginDenied,
        other => {
            return Err(PyValueError::new_err(format!(
                "unknown early kind {other:?}"
            )))
        }
    })
}

enum Resolved {
    Route(u32, Vec<(String, String)>),
    /// No route: answer natively, with extra headers (`allow`).
    Missing(Early, Vec<Header>),
}

/// The classifier a connection layer drives: ready check, limits, request id,
/// routing, early answers, then the bounded queue towards the pipeline. It holds
/// no Python object, so the tokio side shares it behind an `Arc`.
pub struct Core {
    router: Router,
    plan: Plan,
    #[allow(
        dead_code,
        reason = "consumed by native CRUD execution in the next phase"
    )]
    crud_plans: HashMap<u32, Arc<CrudPlan>>,
    /// Route pattern by action number, for the access log.
    templates: HashMap<u32, Arc<str>>,
    database: Mutex<Option<Database>>,
    native_jobs: Arc<NativeJobs>,
    request_timeout: Duration,
    limits: Limits,
    bodies: EarlyBodies,
    queue: Queue<PreparedRequest>,
    in_flight: InFlight,
    ready: AtomicBool,
    policies: [Vec<Header>; 2],
    cors: Option<Cors>,
    hosts: Option<Hosts>,
    redirect_https: bool,
    id_name: Vec<u8>,
    prefix: String,
    counter: AtomicU64,
    /// Invalid trusted request ids seen on natively answered requests; the
    /// loop reads and clears it (queued requests log from Python instead).
    ignored_ids: AtomicU64,
    trust_request_id: bool,
    proxies: Networks,
    python_paths: Vec<String>,
    static_pages: Vec<StaticPage>,
    startup_probe: Option<StartupProbe>,
}

impl Core {
    pub(crate) fn native_jobs(&self) -> Arc<NativeJobs> {
        Arc::clone(&self.native_jobs)
    }

    pub(crate) fn request_timeout(&self) -> Duration {
        self.request_timeout
    }

    pub(crate) fn max_body_bytes(&self) -> usize {
        self.limits.max_body_bytes
    }

    pub(crate) fn requeue_native(
        &self,
        request: PreparedRequest,
        asked: Asked,
    ) -> Result<bool, Box<(PreparedRequest, Reply)>> {
        if !self.ready.load(Ordering::Acquire) {
            let reply = self
                .early_reply(&asked, Early::NotReady, Vec::new())
                .unwrap_or_else(|_| Reply {
                    status: 503,
                    headers: vec![(b"content-length".to_vec(), b"0".to_vec())],
                    body: Vec::new(),
                });
            return Err(Box::new((request, reply)));
        }
        if !request.has_admission() {
            let Some(permit) = self.in_flight.try_acquire() else {
                return Err(Box::new((request, self.overloaded_reply(&asked))));
            };
            if let Err(permit) = request.restore_admission(permit) {
                drop(permit);
                return Err(Box::new((request, self.overloaded_reply(&asked))));
            }
        }
        match self.queue.push(request) {
            Ok(pushed) => Ok(pushed == Push::Wake),
            Err(request) => {
                let reply = self.overloaded_reply(&asked);
                Err(Box::new((request, reply)))
            }
        }
    }

    fn overloaded_reply(&self, asked: &Asked) -> Reply {
        self.early_reply(asked, Early::Overloaded, Vec::new())
            .unwrap_or_else(|_| Reply {
                status: 503,
                headers: vec![(b"content-length".to_vec(), b"0".to_vec())],
                body: Vec::new(),
            })
    }
}

struct StartupProbe {
    path: String,
    body: Vec<u8>,
}

/// A fixed answer to `GET`/`HEAD` of one path: the OpenAPI document, a
/// documentation page or one of its assets, built once at startup.
struct StaticPage {
    path: String,
    headers: Vec<Header>,
    body: Vec<u8>,
}

/// `(path, headers, body)` of a [`StaticPage`].
type StaticSpec = (String, Vec<Header>, Vec<u8>);

/// The Python handle of a [`Core`].
#[pyclass(name = "ServerCore", module = "hinoki.core", frozen)]
pub struct ServerCore {
    core: Arc<Core>,
}

impl ServerCore {
    pub fn shared(&self) -> Arc<Core> {
        Arc::clone(&self.core)
    }
}

#[pymethods]
impl ServerCore {
    #[new]
    #[pyo3(signature = (
        routes, early_bodies, policies, request_id_name, request_id_prefix,
        limits=(8192, 32_768, 1_048_576, 288), queue_capacity=4096,
        request_timeout_ms=30000,
        trust_request_id=false, python_paths=Vec::new(),
        cors=None, trusted_proxies=Vec::new(), startup_path=None, startup_body=Vec::new(),
        hosts=None, redirect_https=false, static_pages=Vec::new()
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        routes: Vec<RouteSpec>,
        early_bodies: Vec<(String, Vec<u8>)>,
        policies: (Vec<Header>, Vec<Header>),
        request_id_name: Vec<u8>,
        request_id_prefix: String,
        limits: (usize, usize, usize, usize),
        queue_capacity: usize,
        request_timeout_ms: u64,
        trust_request_id: bool,
        python_paths: Vec<String>,
        cors: Option<CorsSpec>,
        trusted_proxies: Vec<String>,
        startup_path: Option<String>,
        startup_body: Vec<u8>,
        hosts: Option<HostsSpec>,
        redirect_https: bool,
        static_pages: Vec<StaticSpec>,
    ) -> PyResult<Self> {
        if redirect_https && hosts.is_none() {
            return Err(PyValueError::new_err(
                "HTTPS redirect requires a configured host allow-list",
            ));
        }
        let startup_probe = match startup_path {
            Some(path) if valid_startup_path(&path) && !startup_body.is_empty() => {
                Some(StartupProbe {
                    path,
                    body: startup_body,
                })
            }
            None if startup_body.is_empty() => None,
            _ => {
                return Err(PyValueError::new_err(
                    "startup path and non-empty response body must be configured together",
                ));
            }
        };
        let proxies = Networks::parse(&trusted_proxies).map_err(PyValueError::new_err)?;
        let templates = routes
            .iter()
            .map(|route| (route.0, Arc::from(route.2.as_str())))
            .collect();
        let (router, plan, crud_plans) = build_routes(py, routes)?;
        let rendered = early_bodies
            .into_iter()
            .map(|(kind, body)| kind_of(&kind).map(|k| (k, body)))
            .collect::<PyResult<Vec<_>>>()?;
        let bodies = EarlyBodies::new(&rendered).map_err(|TemplateError::Sentinel(n)| {
            PyValueError::new_err(format!("early body has {n} sentinels, expected 1"))
        })?;
        let (max_query_bytes, max_header_bytes, max_body_bytes, max_in_flight) = limits;
        Ok(Self {
            core: Arc::new(Core {
                router,
                plan,
                crud_plans,
                templates,
                database: Mutex::new(None),
                native_jobs: Arc::new(NativeJobs::new(max_in_flight)),
                request_timeout: Duration::from_millis(request_timeout_ms),
                limits: Limits {
                    max_query_bytes,
                    max_header_bytes,
                    max_body_bytes,
                    max_in_flight,
                },
                bodies,
                queue: Queue::new(queue_capacity),
                in_flight: InFlight::new(max_in_flight),
                ready: AtomicBool::new(false),
                policies: [policies.0, policies.1],
                cors: cors.map(
                    |(any_origin, origins, credentials, expose, preflight)| Cors {
                        any_origin,
                        origins,
                        credentials,
                        expose,
                        preflight,
                    },
                ),
                hosts: hosts.map(|(exact, suffixes)| Hosts { exact, suffixes }),
                redirect_https,
                id_name: request_id_name,
                prefix: request_id_prefix,
                counter: AtomicU64::new(0),
                ignored_ids: AtomicU64::new(0),
                trust_request_id,
                proxies,
                python_paths,
                static_pages: static_pages
                    .into_iter()
                    .map(|(path, headers, body)| StaticPage {
                        path,
                        headers,
                        body,
                    })
                    .collect(),
                startup_probe,
            }),
        })
    }

    /// Whether a socket peer address belongs to `trusted_proxies`.
    fn trusts(&self, address: &str) -> PyResult<bool> {
        let ip = address
            .parse()
            .map_err(|_| PyValueError::new_err(format!("not an IP address: {address:?}")))?;
        Ok(self.core.proxies.contains(ip))
    }

    fn set_ready(&self, ready: bool) {
        self.core.ready.store(ready, Ordering::Release);
    }

    fn _attach_database(&self, database: PyRef<'_, PyDatabase>) -> PyResult<()> {
        let mut attached = self.core.database.lock().map_err(|_| poisoned())?;
        if attached.is_some() {
            return Err(PyValueError::new_err("a database is already attached"));
        }
        *attached = Some(database.clone_inner());
        Ok(())
    }

    fn _detach_database(&self) -> PyResult<bool> {
        let mut attached = self.core.database.lock().map_err(|_| poisoned())?;
        Ok(attached.take().is_some())
    }

    fn _arm_crud(&self, action: u32) -> bool {
        if !self
            .core
            .database
            .lock()
            .is_ok_and(|database| database.is_some())
        {
            return false;
        }
        let Some(route) = self.core.plan.get(action) else {
            return false;
        };
        let Some(slot) = &route.crud_plan else {
            return false;
        };
        slot.arm();
        true
    }

    fn _database_attached(&self) -> bool {
        self.core
            .database
            .lock()
            .is_ok_and(|database| database.is_some())
    }

    fn _disarm_crud_plans(&self) {
        for action in self.core.crud_plans.keys() {
            self._disarm_crud(*action);
        }
    }

    #[getter]
    fn _native_in_flight(&self) -> usize {
        self.core.native_jobs.in_flight()
    }

    fn _disarm_crud(&self, action: u32) -> bool {
        let Some(route) = self.core.plan.get(action) else {
            return false;
        };
        let Some(slot) = &route.crud_plan else {
            return false;
        };
        slot.disarm();
        true
    }

    fn _crud_armed(&self, action: u32) -> bool {
        self.core
            .plan
            .get(action)
            .and_then(|route| route.crud_plan.as_ref())
            .is_some_and(|slot| slot.is_armed())
    }

    fn _drain_native<'py>(&self, py: Python<'py>, timeout_ms: u64) -> PyResult<Bound<'py, PyAny>> {
        let jobs = self.core.native_jobs.clone();
        crate::db::ensure_runtime();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            Ok::<usize, PyErr>(jobs.drain(Duration::from_millis(timeout_ms)).await)
        })
    }

    /// How many invalid trusted request ids native answers ignored since the
    /// last call; resets the count. Queued requests report it per request.
    fn take_ignored_ids(&self) -> u64 {
        self.core.ignored_ids.swap(0, Ordering::Relaxed)
    }

    #[getter]
    fn ready(&self) -> bool {
        self.core.ready.load(Ordering::Acquire)
    }

    #[getter]
    fn pending(&self) -> usize {
        self.core.queue.len()
    }

    #[getter]
    fn in_flight(&self) -> usize {
        self.core.in_flight.current()
    }

    /// Take up to `max` queued requests in arrival order.
    fn drain<'py>(&self, py: Python<'py>, max: usize) -> PyResult<Bound<'py, PyList>> {
        let out = PyList::empty(py);
        for request in self.core.queue.drain(max) {
            out.append(Py::new(py, request)?)?;
        }
        Ok(out)
    }

    /// Classify one request. `path` is the raw target path (percent-encoded),
    /// `query` the raw query bytes. `trusted` says the peer is a trusted proxy
    /// (only then may an inbound request id be used) and `https` that the
    /// effective scheme is https (it selects the policy headers).
    #[pyo3(signature = (method, path, query, headers, trusted=false, https=false))]
    fn submit(
        &self,
        method: &str,
        path: &str,
        query: &[u8],
        headers: Vec<Header>,
        trusted: bool,
        https: bool,
    ) -> PyResult<Submission> {
        let head = Head {
            method,
            path,
            query,
            headers,
            trusted,
            https,
            http_version: "1.1",
            peer: None,
            refusal: None,
            body: None,
        };
        let outcome = self
            .core
            .classify(head, false, &Trace::default())
            .map_err(PyRuntimeError::new_err)?;
        Ok(match outcome {
            Outcome::Early(reply) => Submission {
                early: Some(reply),
                wake: false,
                receiver: Mutex::new(None),
                delivered: Mutex::new(None),
            },
            Outcome::Queued { wake, receiver, .. } => Submission {
                early: None,
                wake,
                receiver: Mutex::new(Some(receiver)),
                delivered: Mutex::new(None),
            },
            Outcome::NativeCrud {
                request,
                receiver,
                asked,
                ..
            } => {
                let reply = self
                    .core
                    .early_reply(&asked, Early::NotReady, Vec::new())
                    .unwrap_or_else(|_| Reply {
                        status: 503,
                        headers: vec![(b"content-length".to_vec(), b"0".to_vec())],
                        body: Vec::new(),
                    });
                request.respond(reply.status, reply.headers, &reply.body);
                Submission {
                    early: None,
                    wake: false,
                    receiver: Mutex::new(Some(receiver)),
                    delivered: Mutex::new(None),
                }
            }
        })
    }
}

/// A request head as the classifier takes it.
pub struct Head<'a> {
    pub method: &'a str,
    pub path: &'a str,
    pub query: &'a [u8],
    pub headers: Vec<Header>,
    pub trusted: bool,
    pub https: bool,
    /// `"2"` or `"1.1"`, as the transport saw the request.
    pub http_version: &'static str,
    pub peer: Option<SocketAddr>,
    /// The transport's own verdict on the request, answered right after the
    /// ready and size checks.
    pub refusal: Option<Early>,
    /// The request body, still unread.
    pub body: Option<BodyReader>,
}

/// What the classifier decided.
pub(crate) enum Outcome {
    /// Answered without Python.
    Early(Reply),
    /// Queued for the pipeline. `wake` says the queue went from empty to
    /// non-empty, so the consumer must be woken; `asked` lets the caller still
    /// answer natively if the request is dropped without a reply.
    Queued {
        wake: bool,
        receiver: oneshot::Receiver<Reply>,
        asked: Asked,
    },
    /// Eligible read request whose database work runs on the Rust worker.
    NativeCrud {
        request: Box<PreparedRequest>,
        receiver: oneshot::Receiver<Reply>,
        asked: Asked,
        plan: Arc<CrudPlan>,
        database: Database,
        params: Vec<(String, String)>,
        query: Vec<u8>,
    },
}

fn build_routes(
    py: Python<'_>,
    routes: Vec<RouteSpec>,
) -> PyResult<(Router, Plan, HashMap<u32, Arc<CrudPlan>>)> {
    let mut router = Router::new();
    let mut planned = Vec::with_capacity(routes.len());
    let mut crud_plans = HashMap::new();
    for (
        action,
        method,
        template,
        has_body,
        has_access,
        media_types,
        status,
        headers,
        transaction,
        takes_request,
        (native_handler, raw_inputs),
        py_crud_plan,
    ) in routes
    {
        let path_params = Router::parameter_names(&template)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        router
            .add(&method, &template, action)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let input_params = raw_inputs
            .into_iter()
            .map(|(source, name, has_default, kind)| {
                let kind = InputKind::parse(&kind)
                    .ok_or_else(|| PyValueError::new_err(format!("unknown input kind {kind:?}")))?;
                Ok(InputParam {
                    source,
                    name,
                    has_default,
                    kind,
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        let crud_plan = py_crud_plan.map(|plan| Arc::clone(&plan.borrow(py).inner));
        if let Some(plan) = &crud_plan {
            crud_plans.insert(action, Arc::clone(plan));
        }
        planned.push(RoutePlan {
            action,
            method,
            path_params,
            input_params,
            has_body,
            has_access,
            media_types,
            status,
            headers,
            transaction,
            takes_request,
            native_handler,
            crud_plan: crud_plan.map(|_| Arc::new(kiso::CrudPlanSlot::new(action))),
        });
    }
    let plan = Plan::new(planned).map_err(|e| PyValueError::new_err(format!("{e:?}")))?;
    Ok((router, plan, crud_plans))
}

impl Core {
    /// Classify one request: refuse it, answer it natively, or queue it for the
    /// Python pipeline. The `Err` is an internal inconsistency (a missing plan or
    /// early body), never a client mistake.
    pub(crate) fn classify(
        &self,
        head: Head<'_>,
        native_execution: bool,
        trace: &Trace,
    ) -> Result<Outcome, String> {
        let (request_id, id_ignored) = self.request_id(&head.headers, head.trusted);
        let asked = Asked {
            head_only: head.method == "HEAD",
            request_id,
            id_ignored,
            https: head.https,
            origin: origin_of(&head.headers),
        };
        if let Some(early) = self.refuse(head.query, &head.headers) {
            return self.answer(&asked, early, Vec::new());
        }
        if let Some(early) = head.refusal.clone() {
            return self.answer(&asked, early, Vec::new());
        }
        if let Some(outcome) = self.policy_response(&head, &asked)? {
            return Ok(outcome);
        }
        let is_startup_probe = self
            .startup_probe
            .as_ref()
            .is_some_and(|probe| probe.path == head.path);
        if is_startup_probe {
            return self.startup_response(&asked, head.method, head.query, &head.headers);
        }
        if let Some(page) = self.static_page(head.method, head.path) {
            return Ok(self.static_response(&asked, page));
        }
        let (action, params) = match self.resolve(head.method, head.path) {
            // A raw target the server cannot read as a plain path, and the
            // documentation pages, are the Python pipeline's to answer.
            _ if !head.path.is_ascii() || self.is_python_path(head.method, head.path) => {
                (None, Vec::new())
            }
            Resolved::Route(action, params) => (Some(action), params),
            Resolved::Missing(early, extra) => return self.answer(&asked, early, extra),
        };
        if let Some(pattern) = action.and_then(|number| self.templates.get(&number)) {
            trace.set_route(Arc::clone(pattern));
        }
        let plan = match action {
            Some(number) => self
                .plan
                .get(number)
                .ok_or_else(|| format!("no plan for action {number}"))?,
            None => {
                return self.enqueue(
                    head,
                    asked,
                    (action, Vec::new()),
                    true,
                    None,
                    (NativeInput::default(), false),
                )
            }
        };
        let native_handler = plan.native_handler;
        let native_input = if native_handler {
            prepare_native_inputs(plan, &params, head.query, &head.headers).unwrap_or_default()
        } else {
            NativeInput::default()
        };
        let prepared_path_params =
            (!native_input.enabled).then(|| prepare_python_path_params(&params));
        self.enqueue(
            head,
            asked,
            (action, params),
            plan.needs_headers(),
            prepared_path_params,
            (native_input, native_execution),
        )
    }

    fn policy_response(&self, head: &Head<'_>, asked: &Asked) -> Result<Option<Outcome>, String> {
        if !self.has_policies() {
            return Ok(None);
        }
        // `Hosts` reads the header as the Latin-1 text `join_headers` makes.
        let host = joined_value(&head.headers, b"host").map(latin1_bytes);
        let host_bytes = host.as_deref().unwrap_or_default();
        if self
            .hosts
            .as_ref()
            .is_some_and(|policy| !policy.allows(host_bytes))
        {
            return self
                .answer(asked, Early::DisallowedHost, Vec::new())
                .map(Some);
        }
        if let Some(outcome) = self.preflight_response(head, asked)? {
            return Ok(Some(outcome));
        }
        if !self.redirect_https || asked.https {
            return Ok(None);
        }
        let Some(location) = redirect::location(host_bytes, head.path.as_bytes(), head.query)
        else {
            return Ok(None);
        };
        self.policy_reply(
            asked,
            308,
            vec![(b"location".to_vec(), location)],
            Vec::new(),
        )
        .map(|reply| Some(Outcome::Early(reply)))
    }

    /// The answer to a CORS preflight, or its refusal; `None` for any other request.
    fn preflight_response(
        &self,
        head: &Head<'_>,
        asked: &Asked,
    ) -> Result<Option<Outcome>, String> {
        let Some(cors) = self.cors.as_ref() else {
            return Ok(None);
        };
        let origin = joined_value(&head.headers, b"origin");
        let requests_method = head
            .headers
            .iter()
            .any(|(name, _)| name.eq_ignore_ascii_case(b"access-control-request-method"));
        match cors.preflight(head.method, origin.as_deref(), requests_method) {
            Ok(Some(own)) => self
                .policy_reply(asked, 204, own, Vec::new())
                .map(|reply| Some(Outcome::Early(reply))),
            Ok(None) => Ok(None),
            Err(PreflightError::OriginDenied) => self
                .answer(asked, Early::CorsOriginDenied, Vec::new())
                .map(Some),
        }
    }

    fn has_policies(&self) -> bool {
        self.hosts.is_some() || self.cors.is_some() || self.redirect_https
    }

    /// The prebuilt page for a `GET`/`HEAD` of its path. Python compares the
    /// decoded path, so `/openapi%2Ejson` is the document too.
    fn static_page(&self, method: &str, path: &str) -> Option<&StaticPage> {
        if !matches!(method, "GET" | "HEAD") || self.static_pages.is_empty() {
            return None;
        }
        let decoded = percent_decode(path);
        self.static_pages
            .iter()
            .find(|page| page.path.as_bytes() == decoded.as_slice())
    }

    fn static_response(&self, asked: &Asked, page: &StaticPage) -> Outcome {
        if asked.id_ignored {
            self.ignored_ids.fetch_add(1, Ordering::Relaxed);
        }
        let cors = self
            .cors
            .as_ref()
            .map(|policy| policy.headers(&asked.origin))
            .unwrap_or_default();
        let mut body = page.body.clone();
        let headers = final_headers(
            &page.headers,
            &self.policies[usize::from(asked.https)],
            &[],
            (&self.id_name, asked.request_id.as_bytes()),
            &cors,
            200,
            body.len(),
        );
        if asked.head_only {
            body.clear();
        }
        Outcome::Early(Reply {
            status: 200,
            headers,
            body,
        })
    }

    fn startup_response(
        &self,
        asked: &Asked,
        method: &str,
        query: &[u8],
        headers: &[Header],
    ) -> Result<Outcome, String> {
        if !matches!(method, "GET" | "HEAD") {
            return self.answer(
                asked,
                Early::MethodNotAllowed,
                vec![(b"allow".to_vec(), b"GET, HEAD".to_vec())],
            );
        }
        let joined = request::join_headers(headers);
        let accept = joined
            .iter()
            .find(|(name, _)| name == "accept")
            .map_or("", |(_, value)| value.as_str());
        if request::negotiate(accept, &["application/json".to_owned()]).is_none() {
            return self.answer(asked, Early::NotAcceptable, Vec::new());
        }
        if request::parse_query(query, 1000).is_err() {
            return self.answer(asked, Early::InvalidQuery, Vec::new());
        }
        if asked.id_ignored {
            self.ignored_ids.fetch_add(1, Ordering::Relaxed);
        }
        let probe = self
            .startup_probe
            .as_ref()
            .ok_or_else(|| "startup probe is not configured".to_owned())?;
        let mut body = probe.body.clone();
        let own = vec![(b"content-type".to_vec(), b"application/json".to_vec())];
        let cors = self
            .cors
            .as_ref()
            .map(|policy| policy.headers(&asked.origin))
            .unwrap_or_default();
        let response_headers = final_headers(
            &own,
            &self.policies[usize::from(asked.https)],
            &[],
            (&self.id_name, asked.request_id.as_bytes()),
            &cors,
            200,
            body.len(),
        );
        if asked.head_only {
            body.clear();
        }
        Ok(Outcome::Early(Reply {
            status: 200,
            headers: response_headers,
            body,
        }))
    }

    /// Take an in-flight slot and queue the request, or answer 503 natively.
    fn enqueue(
        &self,
        head: Head<'_>,
        asked: Asked,
        (action, params): (Option<u32>, Vec<(String, String)>),
        needs_headers: bool,
        prepared_path_params: Option<PreparedPathParams>,
        (native_input, native_execution): (NativeInput, bool),
    ) -> Result<Outcome, String> {
        let Some(permit) = self.in_flight.try_acquire() else {
            return self.answer(&asked, Early::Overloaded, Vec::new());
        };
        let (sender, receiver) = oneshot::channel();
        let route_plan = action.and_then(|number| self.plan.get(number));
        let native_crud = route_plan
            .and_then(|route| route.crud_plan.as_ref())
            .is_some_and(|slot| slot.is_armed());
        let native_policy = self.policies[usize::from(asked.https)].clone();
        let native_cors = self
            .cors
            .as_ref()
            .map_or_else(Vec::new, |cors| cors.headers(&asked.origin));
        let route_headers = if native_crud {
            route_plan.map_or_else(Vec::new, |route| route.headers.clone())
        } else {
            Vec::new()
        };
        let request = PreparedRequest {
            method: head.method.to_owned(),
            path: head.path.to_owned(),
            query: head.query.to_vec(),
            headers: head.headers,
            request_id: asked.request_id.clone(),
            id_ignored: asked.id_ignored,
            action,
            native_handler: native_input.enabled,
            native_args: native_input.args,
            native_error: native_input.error,
            native_policy,
            native_cors,
            route_headers,
            native_crud,
            request_id_name: self.id_name.clone(),
            needs_headers,
            params: params.clone(),
            prepared_path_params,
            client: head.peer.map(|peer| (peer.ip().to_string(), peer.port())),
            http_version: head.http_version,
            reply: Mutex::new(Some(sender)),
            permit: Mutex::new(Some(permit)),
            body: Mutex::new(head.body.map_or(BodyState::Absent, BodyState::Unread)),
            native_body_consumed: Arc::new(AtomicBool::new(false)),
            native_failure: Mutex::new(None),
            native_rows: Mutex::new(None),
        };
        let accepts_native_crud = route_plan.is_some_and(|route| {
            let accept = joined_value(&request.headers, b"accept").unwrap_or_default();
            std::str::from_utf8(&accept)
                .is_ok_and(|accept| request::negotiate(accept, &route.media_types).is_some())
        });
        if native_execution && native_crud && accepts_native_crud {
            let native_plan = action
                .and_then(|number| self.crud_plans.get(&number))
                .filter(|plan| {
                    matches!(
                        plan.action.as_str(),
                        "list" | "retrieve" | "create" | "update" | "delete"
                    )
                });
            let database = self
                .database
                .lock()
                .ok()
                .and_then(|attached| attached.clone());
            if let (Some(plan), Some(database)) = (native_plan, database) {
                return Ok(Outcome::NativeCrud {
                    request: Box::new(request),
                    receiver,
                    asked,
                    plan: Arc::clone(plan),
                    database,
                    params,
                    query: head.query.to_vec(),
                });
            }
        }
        match self.queue.push_cold_path(request) {
            Ok(pushed) => Ok(Outcome::Queued {
                wake: pushed == Push::Wake,
                receiver,
                asked,
            }),
            Err(early) => self.answer(&asked, early, Vec::new()),
        }
    }

    /// Whether the socket peer is a trusted proxy and, only then, the scheme it
    /// forwards (`None` when it names none). A forwarded scheme from anyone else
    /// is ignored.
    pub fn peer_view(&self, peer: SocketAddr, headers: &[Header]) -> (bool, Option<bool>) {
        let trusted = self.proxies.contains(peer.ip());
        (
            trusted,
            trusted.then(|| proxy::forwarded_scheme(headers)).flatten(),
        )
    }

    /// The inbound id only from a trusted peer (the caller knows the peer) and
    /// only when the application trusts inbound ids. The flag says a trusted
    /// peer sent an id header that was refused.
    fn request_id(&self, headers: &[Header], trusted: bool) -> (String, bool) {
        let mut ignored = false;
        if trusted && self.trust_request_id {
            let found: Vec<&[u8]> = headers
                .iter()
                .filter(|(name, _)| name.eq_ignore_ascii_case(&self.id_name))
                .map(|(_, value)| value.as_slice())
                .collect();
            if let Some(id) = requestid::inbound(&found) {
                return (id.to_owned(), false);
            }
            ignored = !found.is_empty();
        }
        let id = requestid::generated(&self.prefix, self.counter.fetch_add(1, Ordering::Relaxed));
        (id, ignored)
    }

    /// Documentation and OpenAPI paths are answered by the Python pipeline.
    fn is_python_path(&self, method: &str, path: &str) -> bool {
        if !matches!(method, "GET" | "HEAD") {
            return false;
        }
        // Python compares the decoded path (`/openapi%2Ejson` is the document).
        let decoded = percent_decode(path);
        self.python_paths
            .iter()
            .any(|p| p.as_bytes() == decoded.as_slice())
    }

    /// Not-ready and oversized requests are refused before routing, in this order.
    fn refuse(&self, query: &[u8], headers: &[Header]) -> Option<Early> {
        if !self.ready.load(Ordering::Acquire) {
            return Some(Early::NotReady);
        }
        let total =
            kiso::limits::header_total(headers.iter().map(|(n, v)| (n.as_slice(), v.as_slice())));
        self.limits.check_request(query.len(), total)
    }

    /// Route lookup with the Python `Hinoki.lookup` rules: HEAD borrows a GET
    /// route, and a wrong method advertises HEAD whenever GET is allowed.
    fn resolve(&self, method: &str, path: &str) -> Resolved {
        match self.router.find(method, path) {
            Match::Found { action, params } => Resolved::Route(action, params),
            Match::NotFound => Resolved::Missing(Early::NotFound, Vec::new()),
            Match::MethodNotAllowed { allowed } => {
                let has_get = allowed.iter().any(|m| m == "GET");
                if method == "HEAD" && has_get {
                    return self.resolve("GET", path);
                }
                let mut allowed = allowed;
                if has_get && !allowed.iter().any(|m| m == "HEAD") {
                    allowed.push("HEAD".to_owned());
                    allowed.sort();
                }
                let allow = (b"allow".to_vec(), allowed.join(", ").into_bytes());
                Resolved::Missing(Early::MethodNotAllowed, vec![allow])
            }
        }
    }

    /// An answer without Python: final headers (policy for the effective
    /// scheme, request id, CORS), body with the request id, and no body for HEAD
    /// (the length stays).
    pub(crate) fn answer(
        &self,
        asked: &Asked,
        early: Early,
        extra: Vec<Header>,
    ) -> Result<Outcome, String> {
        let reply = self.early_reply(asked, early, extra)?;
        Ok(Outcome::Early(reply))
    }

    /// The same answer as a bare reply.
    pub fn early_reply(
        &self,
        asked: &Asked,
        early: Early,
        extra: Vec<Header>,
    ) -> Result<Reply, String> {
        let body = self
            .bodies
            .render(&early, &asked.request_id)
            .ok_or_else(|| format!("no early body registered for {early:?}"))?;
        let mut own: Vec<Header> = vec![(b"content-type".to_vec(), b"application/json".to_vec())];
        own.extend(
            early
                .headers()
                .iter()
                .map(|(n, v)| (n.as_bytes().to_vec(), v.as_bytes().to_vec())),
        );
        own.extend(extra);
        self.policy_reply(asked, early.status(), own, body)
    }

    fn policy_reply(
        &self,
        asked: &Asked,
        status: u16,
        own: Vec<Header>,
        mut body: Vec<u8>,
    ) -> Result<Reply, String> {
        if asked.id_ignored {
            self.ignored_ids.fetch_add(1, Ordering::Relaxed);
        }
        let cors = self
            .cors
            .as_ref()
            .map(|c| c.headers(&asked.origin))
            .unwrap_or_default();
        let headers = final_headers(
            &own,
            &self.policies[usize::from(asked.https)],
            &[],
            (&self.id_name, asked.request_id.as_bytes()),
            &cors,
            status,
            body.len(),
        );
        if asked.head_only {
            body.clear();
        }
        Ok(Reply {
            status,
            headers,
            body,
        })
    }
}

/// What `answer` needs to know about the request it refuses.
#[derive(Clone)]
pub struct Asked {
    head_only: bool,
    request_id: String,
    id_ignored: bool,
    https: bool,
    origin: Vec<u8>,
}

/// The `Origin` header as `origin_of` in the Python pipeline reads it: repeated
/// headers are joined with ", ", so they match no single origin.
fn origin_of(headers: &[Header]) -> Vec<u8> {
    let found: Vec<&[u8]> = headers
        .iter()
        .filter(|(name, _)| name == b"origin")
        .map(|(_, value)| value.as_slice())
        .collect();
    found.join(&b", "[..])
}

/// One request header as `request::join_headers` reads it: the name compared
/// without case, repeated fields joined with ", ". Borrowed unless it repeats.
fn joined_value<'a>(headers: &'a [Header], name: &[u8]) -> Option<Cow<'a, [u8]>> {
    let mut found: Option<Cow<'a, [u8]>> = None;
    for (header, value) in headers {
        if !header.eq_ignore_ascii_case(name) {
            continue;
        }
        found = Some(match found {
            None => Cow::Borrowed(value.as_slice()),
            Some(seen) => {
                let mut joined = seen.into_owned();
                joined.extend_from_slice(b", ");
                joined.extend_from_slice(value);
                Cow::Owned(joined)
            }
        });
    }
    found
}

/// Latin-1 header bytes as text, as in ASGI.
fn latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|&byte| char::from(byte)).collect()
}

/// The UTF-8 bytes of that text; plain ASCII is returned as it came.
fn latin1_bytes(bytes: Cow<'_, [u8]>) -> Cow<'_, [u8]> {
    if bytes.is_ascii() {
        bytes
    } else {
        Cow::Owned(latin1(&bytes).into_bytes())
    }
}

fn percent_decode(path: &str) -> Vec<u8> {
    let bytes = path.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut at = 0;
    while at < bytes.len() {
        let hex = |b: u8| char::from(b).to_digit(16);
        let decoded = match (bytes[at], bytes.get(at + 1), bytes.get(at + 2)) {
            (b'%', Some(&hi), Some(&lo)) => hex(hi).zip(hex(lo)).map(|(h, l)| (h * 16 + l) as u8),
            _ => None,
        };
        match decoded {
            Some(byte) => {
                out.push(byte);
                at += 3;
            }
            None => {
                out.push(bytes[at]);
                at += 1;
            }
        }
    }
    out
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<ServerCore>()?;
    m.add_class::<PreparedRequest>()?;
    m.add_class::<Submission>()?;
    m.add("BodyReadError", m.py().get_type::<BodyReadError>())?;
    Ok(())
}
