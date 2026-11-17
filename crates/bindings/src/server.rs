//! Glue between `hinoki-server` and Python: the request classifier (`Core`, held
//! by the Python `ServerCore`), the request handed to the pipeline
//! (`PreparedRequest`) and the handle a test holds to see the answer
//! (`Submission`).
//!
//! Python objects exist only on the calling thread: `PreparedRequest` holds Rust
//! data and a oneshot sender, so it can be dropped on any thread without the GIL.
//! `Core` is shared with the tokio side behind an `Arc` and holds no Python
//! object; `rustserver.rs` drives `Core::classify` from the connection layer, and
//! the tests call `submit` the same way without a socket.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use hinoki_server::cors::{Cors, PreflightError};
use hinoki_server::early::TemplateError;
use hinoki_server::headers::{final_headers, Header};
use hinoki_server::hosts::Hosts;
use hinoki_server::queue::Push;
use hinoki_server::{
    proxy, requestid, Early, EarlyBodies, InFlight, Limits, Networks, Permit, Plan, Queue,
    RoutePlan,
};
use hinoki_server::{Answer, BodyError, BodyReader};
use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use tokio::sync::oneshot;

use crate::completion;
use crate::request;
use crate::router::{Match, Router};
use hinoki_server::redirect;

// `args[0]` is `too_large`, `disconnected` or `timeout`.
pyo3::create_exception!(core, BodyReadError, PyException);

pub type Reply = Answer;
type NativeArgs = Vec<(String, String)>;
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
    /// Handed to `body()`, or dropped by `respond`.
    Taken,
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
    request_id_name: Vec<u8>,
    needs_headers: bool,
    params: Vec<(String, String)>,
    prepared_path_params: Option<PreparedPathParams>,
    client: Option<(String, u16)>,
    http_version: &'static str,
    reply: Mutex<Option<oneshot::Sender<Reply>>>,
    permit: Mutex<Option<Permit>>,
    body: Mutex<BodyState>,
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
            args.set_item(name, value)?;
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
        let reader = {
            let mut state = self.body.lock().map_err(|_| poisoned())?;
            match std::mem::replace(&mut *state, BodyState::Taken) {
                BodyState::Unread(reader) => Some(reader),
                BodyState::Absent => {
                    *state = BodyState::Absent;
                    None
                }
                BodyState::Taken => {
                    return Err(PyRuntimeError::new_err(
                        "the request body was already taken",
                    ))
                }
            }
        };
        let read = async move {
            match reader {
                Some(reader) => reader.read_all(limit).await,
                None => Ok(Vec::new()),
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

    /// Complete the request with the final status, headers and body. Returns
    /// `False` when it was already completed or nobody is waiting any more.
    fn respond(&self, status: u16, headers: Vec<(Vec<u8>, Vec<u8>)>, body: &[u8]) -> bool {
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
    /// native policy and request-id headers. HEAD retains GET's content length.
    fn respond_final(&self, status: u16, headers: Vec<(Vec<u8>, Vec<u8>)>, body: &[u8]) -> bool {
        if !self.native_handler {
            return false;
        }
        let own = headers;
        let mut final_headers = final_headers(
            &own,
            &self.native_policy,
            &[],
            (&self.request_id_name, self.request_id.as_bytes()),
            &[],
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
    u32,                                 // action
    String,                              // method
    String,                              // template
    bool,                                // has body
    bool,                                // has access
    Vec<String>,                         // media types
    u16,                                 // status
    Vec<(String, String)>,               // route headers
    bool,                                // transaction
    bool,                                // takes request
    (bool, Vec<(String, String, bool)>), // native handler and source/name/default
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
) -> NativeInput {
    let decoded_path = match decode_native_path(plan, path_params) {
        Ok(path) => path,
        Err(error) => return NativeInput::failed(error),
    };
    let joined = request::join_headers(headers);
    let accept = joined
        .iter()
        .find(|(name, _)| name == "accept")
        .map_or("", |(_, value)| value.as_str());
    if request::negotiate(accept, &plan.media_types).is_none() {
        let mut error = native_error("not_acceptable", "not acceptable");
        error.available = Some(plan.media_types.join(", "));
        return NativeInput::failed(error);
    }
    let parsed = match request::parse_query(query, 1000) {
        Ok(parsed) => parsed,
        Err(_) => {
            return NativeInput::failed(native_error("invalid_query", "malformed query string"));
        }
    };
    let values = query_values(parsed);
    let (args, errors) = collect_native_args(plan, &decoded_path, &values);
    if errors.is_empty() {
        NativeInput {
            enabled: true,
            args,
            error: None,
        }
    } else {
        let mut error = native_error("validation_error", "invalid request parameters");
        error.details = errors;
        NativeInput::failed(error)
    }
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
    for (source, name, _) in &plan.input_params {
        if source != "path" {
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

fn collect_native_args(
    plan: &RoutePlan,
    path: &HashMap<String, String>,
    query: &HashMap<String, Vec<String>>,
) -> (NativeArgs, NativeDetails) {
    let mut args = Vec::new();
    let mut errors = Vec::new();
    for (source, name, defaulted) in &plan.input_params {
        let value = if source == "path" {
            path.get(name)
        } else {
            query.get(name).and_then(|values| match values.len() {
                0 => None,
                1 => values.first(),
                _ => {
                    errors.push((
                        name.clone(),
                        "duplicate".into(),
                        "given more than once".into(),
                    ));
                    None
                }
            })
        };
        if let Some(value) = value {
            args.push((name.clone(), value.clone()));
        } else if source == "query" && !defaulted && !errors.iter().any(|(path, _, _)| path == name)
        {
            errors.push((
                name.clone(),
                "required".into(),
                "query parameter is required".into(),
            ));
        }
    }
    (args, errors)
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
    startup_probe: Option<StartupProbe>,
}

struct StartupProbe {
    path: String,
    body: Vec<u8>,
}

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
        trust_request_id=false, python_paths=Vec::new(),
        cors=None, trusted_proxies=Vec::new(), startup_path=None, startup_body=Vec::new(),
        hosts=None, redirect_https=false
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        routes: Vec<RouteSpec>,
        early_bodies: Vec<(String, Vec<u8>)>,
        policies: (Vec<Header>, Vec<Header>),
        request_id_name: Vec<u8>,
        request_id_prefix: String,
        limits: (usize, usize, usize, usize),
        queue_capacity: usize,
        trust_request_id: bool,
        python_paths: Vec<String>,
        cors: Option<CorsSpec>,
        trusted_proxies: Vec<String>,
        startup_path: Option<String>,
        startup_body: Vec<u8>,
        hosts: Option<HostsSpec>,
        redirect_https: bool,
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
        let (router, plan) = build_routes(routes)?;
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
        let outcome = self.core.classify(head).map_err(PyRuntimeError::new_err)?;
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
pub enum Outcome {
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
}

fn build_routes(routes: Vec<RouteSpec>) -> PyResult<(Router, Plan)> {
    let mut router = Router::new();
    let mut planned = Vec::with_capacity(routes.len());
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
        (native_handler, input_params),
    ) in routes
    {
        let path_params = Router::parameter_names(&template)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        router
            .add(&method, &template, action)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
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
        });
    }
    let plan = Plan::new(planned).map_err(|e| PyValueError::new_err(format!("{e:?}")))?;
    Ok((router, plan))
}

impl Core {
    /// Classify one request: refuse it, answer it natively, or queue it for the
    /// Python pipeline. The `Err` is an internal inconsistency (a missing plan or
    /// early body), never a client mistake.
    pub fn classify(&self, head: Head<'_>) -> Result<Outcome, String> {
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
        let (action, params) = match self.resolve(head.method, head.path) {
            // A raw target the server cannot read as a plain path, and the
            // documentation pages, are the Python pipeline's to answer.
            _ if !head.path.is_ascii() || self.is_python_path(head.method, head.path) => {
                (None, Vec::new())
            }
            Resolved::Route(action, params) => (Some(action), params),
            Resolved::Missing(_, _) if self.has_policies() => {
                return self.enqueue(
                    head,
                    asked,
                    (None, Vec::new()),
                    true,
                    None,
                    NativeInput::default(),
                );
            }
            Resolved::Missing(early, extra) => return self.answer(&asked, early, extra),
        };
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
                    NativeInput::default(),
                )
            }
        };
        let native_handler = plan.native_handler;
        let native_input = if native_handler {
            prepare_native_inputs(plan, &params, head.query, &head.headers)
        } else {
            NativeInput::default()
        };
        let prepared_path_params = (!native_handler).then(|| prepare_python_path_params(&params));
        self.enqueue(
            head,
            asked,
            (action, params),
            plan.needs_headers(),
            prepared_path_params,
            native_input,
        )
    }

    fn policy_response(&self, head: &Head<'_>, asked: &Asked) -> Result<Option<Outcome>, String> {
        let joined = request::join_headers(&head.headers);
        let origin = header_value(&joined, "origin");
        let host = header_value(&joined, "host");
        let host_bytes = host.as_deref().unwrap_or_default().as_bytes();
        if self
            .hosts
            .as_ref()
            .is_some_and(|policy| !policy.allows(host_bytes))
        {
            return Ok(Some(self.answer(
                asked,
                Early::DisallowedHost,
                Vec::new(),
            )?));
        }
        if let Some(cors) = self.cors.as_ref() {
            match cors.preflight(
                head.method,
                origin.as_deref().map(str::as_bytes),
                has_header(&joined, "access-control-request-method"),
            ) {
                Ok(Some(own)) => {
                    return Ok(Some(Outcome::Early(self.policy_reply(
                        asked,
                        204,
                        own,
                        Vec::new(),
                    )?)))
                }
                Err(PreflightError::OriginDenied) => {
                    return Ok(Some(self.answer(
                        asked,
                        Early::CorsOriginDenied,
                        Vec::new(),
                    )?));
                }
                Ok(None) => {}
            }
        }
        if self.redirect_https && !asked.https {
            if let Some(location) = redirect::location(host_bytes, head.path.as_bytes(), head.query)
            {
                return Ok(Some(Outcome::Early(self.policy_reply(
                    asked,
                    308,
                    vec![(b"location".to_vec(), location)],
                    Vec::new(),
                )?)));
            }
        }
        Ok(None)
    }

    fn has_policies(&self) -> bool {
        self.hosts.is_some() || self.cors.is_some() || self.redirect_https
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
        native_input: NativeInput,
    ) -> Result<Outcome, String> {
        let Some(permit) = self.in_flight.try_acquire() else {
            return self.answer(&asked, Early::Overloaded, Vec::new());
        };
        let (sender, receiver) = oneshot::channel();
        let native_policy = if native_input.enabled {
            self.policies[usize::from(asked.https)].clone()
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
            request_id_name: self.id_name.clone(),
            needs_headers,
            params,
            prepared_path_params,
            client: head.peer.map(|peer| (peer.ip().to_string(), peer.port())),
            http_version: head.http_version,
            reply: Mutex::new(Some(sender)),
            permit: Mutex::new(Some(permit)),
            body: Mutex::new(head.body.map_or(BodyState::Absent, BodyState::Unread)),
        };
        match self.queue.push(request) {
            Ok(pushed) => Ok(Outcome::Queued {
                wake: pushed == Push::Wake,
                receiver,
                asked,
            }),
            Err(_full) => self.answer(&asked, Early::Overloaded, Vec::new()),
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
        let total = hinoki_server::limits::header_total(
            headers.iter().map(|(n, v)| (n.as_slice(), v.as_slice())),
        );
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
    pub fn answer(
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

fn header_value(headers: &[(String, String)], name: &str) -> Option<String> {
    headers
        .iter()
        .find(|(header, _)| header == name)
        .map(|(_, value)| value.clone())
}

fn has_header(headers: &[(String, String)], name: &str) -> bool {
    headers.iter().any(|(header, _)| header == name)
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
