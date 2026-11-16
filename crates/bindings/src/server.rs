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

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use hinoki_server::cors::Cors;
use hinoki_server::early::TemplateError;
use hinoki_server::headers::{final_headers, Header};
use hinoki_server::queue::Push;
use hinoki_server::{
    requestid, Early, EarlyBodies, InFlight, Limits, Permit, Plan, Queue, RoutePlan,
};
use hinoki_server::{Answer, BodyError, BodyReader};
use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use tokio::sync::oneshot;

use crate::completion;
use crate::router::{Match, Router};

// `args[0]` is `too_large`, `disconnected` or `timeout`.
pyo3::create_exception!(core, BodyReadError, PyException);

pub type Reply = Answer;

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
    action: Option<u32>,
    needs_headers: bool,
    params: Vec<(String, String)>,
    client: Option<(String, u16)>,
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

    /// The action number, or `None` when no route matched and the Python
    /// pipeline must produce the answer (a policy is configured).
    #[getter]
    fn action(&self) -> Option<u32> {
        self.action
    }

    /// Whether the route reads the headers as a mapping (access, negotiation
    /// among several renderers, or a `Request` parameter). `True` when unrouted.
    #[getter]
    fn needs_headers(&self) -> bool {
        self.needs_headers
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

/// A route as Python describes it: the plan fields plus the router entry.
type RouteSpec = (
    u32,                   // action
    String,                // method
    String,                // template
    Vec<String>,           // path parameter names
    bool,                  // has body
    bool,                  // has access
    Vec<String>,           // media types
    u16,                   // status
    Vec<(String, String)>, // route headers
    bool,                  // transaction
    bool,                  // is async
    bool,                  // takes request
);

/// `(any_origin, origins, credentials, expose)` of the CORS policy.
type CorsSpec = (bool, Vec<Vec<u8>>, bool, Vec<u8>);

fn kind_of(name: &str) -> PyResult<Early> {
    Ok(match name {
        "uri_too_long" => Early::UriTooLong,
        "headers_too_large" => Early::HeadersTooLarge,
        "not_ready" => Early::NotReady,
        "overloaded" => Early::Overloaded,
        "not_found" => Early::NotFound,
        "method_not_allowed" => Early::MethodNotAllowed,
        "bad_host" => Early::BadHost,
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
    id_name: Vec<u8>,
    prefix: String,
    counter: AtomicU64,
    trust_request_id: bool,
    defer_policies: bool,
    python_paths: Vec<String>,
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
        trust_request_id=false, defer_policies=false, python_paths=Vec::new(),
        cors=None
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
        defer_policies: bool,
        python_paths: Vec<String>,
        cors: Option<CorsSpec>,
    ) -> PyResult<Self> {
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
                cors: cors.map(|(any_origin, origins, credentials, expose)| Cors {
                    any_origin,
                    origins,
                    credentials,
                    expose,
                }),
                id_name: request_id_name,
                prefix: request_id_prefix,
                counter: AtomicU64::new(0),
                trust_request_id,
                defer_policies,
                python_paths,
            }),
        })
    }

    fn set_ready(&self, ready: bool) {
        self.core.ready.store(ready, Ordering::Release);
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
        path_params,
        has_body,
        has_access,
        media_types,
        status,
        headers,
        transaction,
        is_async,
        takes_request,
    ) in routes
    {
        router
            .add(&method, &template, action)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        planned.push(RoutePlan {
            action,
            method,
            path_params,
            has_body,
            has_access,
            media_types,
            status,
            headers,
            transaction,
            is_async,
            takes_request,
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
        let asked = Asked {
            head_only: head.method == "HEAD",
            request_id: self.request_id(&head.headers, head.trusted),
            https: head.https,
            origin: origin_of(&head.headers),
        };
        if let Some(early) = self.refuse(head.query, &head.headers) {
            return self.answer(&asked, early, Vec::new());
        }
        if let Some(early) = head.refusal.clone() {
            return self.answer(&asked, early, Vec::new());
        }
        let (action, params) = match self.resolve(head.method, head.path) {
            // A raw target the server cannot read as a plain path, and the
            // documentation pages, are the Python pipeline's to answer.
            _ if !head.path.is_ascii() || self.is_python_path(head.method, head.path) => {
                (None, Vec::new())
            }
            Resolved::Route(action, params) => (Some(action), params),
            Resolved::Missing(_, _) if self.defer_policies => (None, Vec::new()),
            Resolved::Missing(early, extra) => return self.answer(&asked, early, extra),
        };
        let needs_headers = match action {
            Some(number) => self
                .plan
                .get(number)
                .ok_or_else(|| format!("no plan for action {number}"))?
                .needs_headers(),
            None => true,
        };
        self.enqueue(head, asked, (action, params), needs_headers)
    }

    /// Take an in-flight slot and queue the request, or answer 503 natively.
    fn enqueue(
        &self,
        head: Head<'_>,
        asked: Asked,
        (action, params): (Option<u32>, Vec<(String, String)>),
        needs_headers: bool,
    ) -> Result<Outcome, String> {
        let Some(permit) = self.in_flight.try_acquire() else {
            return self.answer(&asked, Early::Overloaded, Vec::new());
        };
        let (sender, receiver) = oneshot::channel();
        let request = PreparedRequest {
            method: head.method.to_owned(),
            path: head.path.to_owned(),
            query: head.query.to_vec(),
            headers: head.headers,
            request_id: asked.request_id.clone(),
            action,
            needs_headers,
            params,
            client: head.peer.map(|peer| (peer.ip().to_string(), peer.port())),
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

    /// The inbound id only from a trusted peer (the caller knows the peer) and
    /// only when the application trusts inbound ids.
    fn request_id(&self, headers: &[Header], trusted: bool) -> String {
        if trusted && self.trust_request_id {
            let found: Vec<&[u8]> = headers
                .iter()
                .filter(|(name, _)| name.eq_ignore_ascii_case(&self.id_name))
                .map(|(_, value)| value.as_slice())
                .collect();
            if let Some(id) = requestid::inbound(&found) {
                return id.to_owned();
            }
        }
        requestid::generated(&self.prefix, self.counter.fetch_add(1, Ordering::Relaxed))
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
        let mut body = self
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
            early.status(),
            body.len(),
        );
        if asked.head_only {
            body.clear();
        }
        Ok(Reply {
            status: early.status(),
            headers,
            body,
        })
    }
}

/// What `answer` needs to know about the request it refuses.
pub struct Asked {
    head_only: bool,
    request_id: String,
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
