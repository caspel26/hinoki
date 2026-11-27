use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

use hinoki_orm::Database;
use kiso::cors::Cors;
use kiso::early::TemplateError;
use kiso::headers::Header;
use kiso::hosts::Hosts;
use kiso::queue::Push;
use kiso::ws::Upgrade;
use kiso::{
    convert, Converted, Early, EarlyBodies, InFlight, InputParam, Limits, NativeJobs, Networks,
    Plan, Queue, RoutePlan, Trace,
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;
use tokio::sync::Semaphore;

use super::prepared::{
    poisoned, NativeArgs, NativeDetails, NativeInputError, PreparedPathParams, PreparedRequest,
    Submission,
};
use super::reply::Reply;
use super::routes::{build_routes, joined_value, latin1, Asked, Head, Outcome};
use crate::crudplan::{CrudPlan, PyCrudPlan};
use crate::db::PyDatabase;
use crate::jwt::{ClaimsAuth, PyClaimsAuth};
use crate::request;
use crate::router::Router;
use crate::websocket::{WebSocketSpec, WsPlan};

type HostsSpec = (Vec<Vec<u8>>, Vec<Vec<u8>>);

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
pub(super) type RouteSpec = (
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

pub(super) type NativePrincipal = (
    String,
    Vec<String>,
    serde_json::Map<String, serde_json::Value>,
);

#[derive(Default)]
pub(super) struct NativeInput {
    pub(super) enabled: bool,
    pub(super) args: NativeArgs,
    pub(super) error: Option<NativeInputError>,
    /// The caller Rust authenticated: `sub`, string scopes, and verified claims.
    pub(super) principal: Option<NativePrincipal>,
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

pub(super) fn prepare_native_inputs(
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
    let parsed = if query.is_empty() {
        Vec::new()
    } else {
        match request::parse_query(query, 1000) {
            Ok(parsed) => parsed,
            Err(_) => {
                return Some(NativeInput::failed(native_error(
                    "invalid_query",
                    "malformed query string",
                )));
            }
        }
    };
    let values = if parsed.is_empty() || !plan.input_params.iter().any(|p| p.source == "query") {
        HashMap::new()
    } else {
        query_values(parsed)
    };
    let (args, errors) = collect_native_args(plan, &decoded_path, &values)?;
    Some(if errors.is_empty() {
        NativeInput {
            enabled: true,
            args,
            error: None,
            principal: None,
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
    let mut decoded = HashMap::with_capacity(params.len());
    for input in &plan.input_params {
        if input.source != "path" {
            continue;
        }
        let name = &input.name;
        let Some((_, value)) = params.iter().find(|(k, _)| k == name) else {
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

pub(super) fn prepare_python_path_params(params: &[(String, String)]) -> PreparedPathParams {
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
        "unauthorized" => Early::Unauthorized,
        "invalid_credentials" => Early::InvalidCredentials,
        "forbidden" => Early::Forbidden,
        "bad_upgrade" => Early::BadUpgrade,
        "upgrade_required" => Early::UpgradeRequired,
        other => {
            return Err(PyValueError::new_err(format!(
                "unknown early kind {other:?}"
            )))
        }
    })
}

/// The WebSocket routes by declaration order, which is their action number.
fn build_websockets(specs: &[Py<WebSocketSpec>]) -> PyResult<(Router, HashMap<u32, WsPlan>)> {
    let mut router = Router::new();
    let mut plans = HashMap::new();
    for (number, spec) in specs.iter().enumerate() {
        let spec = spec.get();
        let action = u32::try_from(number)
            .map_err(|_| PyValueError::new_err("too many WebSocket routes"))?;
        router
            .add("GET", spec.template(), action)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        plans.insert(action, spec.plan()?);
    }
    Ok((router, plans))
}

/// The classifier a connection layer drives: ready check, limits, request id,
/// routing, early answers, then the bounded queue towards the pipeline. It holds
/// no Python object, so the tokio side shares it behind an `Arc`.
pub struct Core {
    pub(super) router: Router,
    pub(super) plan: Plan,
    #[allow(
        dead_code,
        reason = "consumed by native CRUD execution in the next phase"
    )]
    pub(super) crud_plans: HashMap<u32, Arc<CrudPlan>>,
    /// Route pattern by action number, for the access log.
    pub(super) templates: HashMap<u32, Arc<str>>,
    pub(super) database: Mutex<Option<Database>>,
    pub(super) native_jobs: Arc<NativeJobs>,
    pub(super) request_timeout: Duration,
    pub(super) limits: Limits,
    pub(super) bodies: EarlyBodies,
    pub(super) queue: Queue<PreparedRequest>,
    pub(super) in_flight: InFlight,
    pub(super) ready: AtomicBool,
    pub(super) policies: [Arc<[Header]>; 2],
    pub(super) compiled_policies: [Option<Arc<[kiso::headers::CompiledHeader]>>; 2],
    pub(super) cors: Option<Cors>,
    /// Policies of routes that declare their own CORS, by action number; each
    /// replaces `cors` for that route.
    pub(super) route_cors: HashMap<u32, Cors>,
    pub(super) hosts: Option<Hosts>,
    pub(super) redirect_https: bool,
    pub(super) id_name: Arc<[u8]>,
    pub(super) compiled_id_name: Option<kiso::headers::HeaderName>,
    pub(super) prefix: String,
    pub(super) counter: AtomicU64,
    /// Invalid trusted request ids seen on natively answered requests; the
    /// loop reads and clears it (queued requests log from Python instead).
    pub(super) ignored_ids: AtomicU64,
    pub(super) trust_request_id: bool,
    pub(super) proxies: Networks,
    pub(super) python_paths: Vec<String>,
    pub(super) static_pages: Vec<StaticPage>,
    pub(super) route_auth: HashMap<u32, RouteAuth>,
    pub(super) startup_probe: Option<StartupProbe>,
    pub(super) ws_router: Router,
    pub(super) ws_plans: HashMap<u32, WsPlan>,
    /// Open WebSocket connections the worker allows; a session holds one permit.
    pub(super) ws_slots: Arc<Semaphore>,
    /// NativeLane object + eligible direct action numbers for zero-wakeup inline
    /// dispatch from the Tokio thread (eliminates socketpair ping-pong).
    pub(super) inline_lane: RwLock<Option<(Py<PyAny>, HashSet<u32>)>>,
}

impl Core {
    pub(crate) fn is_inline_action(&self, action: u32) -> bool {
        self.inline_lane
            .read()
            .ok()
            .is_some_and(|slot| slot.as_ref().is_some_and(|(_, set)| set.contains(&action)))
    }

    pub(crate) fn inline_lane(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.inline_lane
            .read()
            .ok()
            .and_then(|slot| slot.as_ref().map(|(lane, _)| lane.clone_ref(py)))
    }
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
                    body: bytes::Bytes::new(),
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
                body: bytes::Bytes::new(),
            })
    }
}

pub(super) struct StartupProbe {
    pub(super) path: String,
    pub(super) body: Vec<u8>,
}

/// A fixed answer to `GET`/`HEAD` of one path: the OpenAPI document, a
/// documentation page or one of its assets, built once at startup.
pub(super) struct StaticPage {
    pub(super) path: String,
    pub(super) headers: Vec<Header>,
    pub(super) body: Vec<u8>,
}

/// `(path, headers, body)` of a [`StaticPage`].
type StaticSpec = (String, Vec<Header>, Vec<u8>);

/// The claims-only authentication of one route, and the `www-authenticate`
/// challenge Python adds to its `401`.
pub(super) struct RouteAuth {
    pub(super) auth: Arc<ClaimsAuth>,
    pub(super) challenge: Option<Vec<u8>>,
    pub(super) armed: AtomicBool,
}

/// `(action, authentication, challenge)` of a [`RouteAuth`].
type RouteAuthSpec = (u32, Py<PyClaimsAuth>, Option<Vec<u8>>);

/// `(action, policy)` of a route's own CORS policy.
type RouteCorsSpec = (u32, CorsSpec);

fn cors_from((any_origin, origins, credentials, expose, preflight): CorsSpec) -> Cors {
    Cors {
        any_origin,
        origins,
        credentials,
        expose,
        preflight,
    }
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

    /// The action number and route pattern of every route, for the metrics.
    pub fn route_patterns(&self) -> Vec<(u32, Arc<str>)> {
        let mut routes: Vec<_> = self
            .core
            .templates
            .iter()
            .map(|(action, pattern)| (*action, Arc::clone(pattern)))
            .collect();
        routes.sort_by_key(|route| route.0);
        routes
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
        hosts=None, redirect_https=false, static_pages=Vec::new(), route_auth=Vec::new(),
        route_cors=Vec::new(), websockets=Vec::new(), ws_connections=1000
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
        route_auth: Vec<RouteAuthSpec>,
        route_cors: Vec<RouteCorsSpec>,
        websockets: Vec<Py<WebSocketSpec>>,
        ws_connections: usize,
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
        let compiled_policies = [
            kiso::headers::compile_headers(&policies.0).map(Arc::from),
            kiso::headers::compile_headers(&policies.1).map(Arc::from),
        ];
        let compiled_id_name = kiso::headers::HeaderName::from_bytes(&request_id_name).ok();
        let (max_query_bytes, max_header_bytes, max_body_bytes, max_in_flight) = limits;
        let (ws_router, ws_plans) = build_websockets(&websockets)?;
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
                policies: [policies.0.into(), policies.1.into()],
                compiled_policies,
                cors: cors.map(cors_from),
                route_cors: route_cors
                    .into_iter()
                    .map(|(action, spec)| (action, cors_from(spec)))
                    .collect(),
                hosts: hosts.map(|(exact, suffixes)| Hosts { exact, suffixes }),
                redirect_https,
                id_name: request_id_name.into(),
                compiled_id_name,
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
                route_auth: route_auth
                    .into_iter()
                    .map(|(action, auth, challenge)| {
                        let auth = Arc::clone(&auth.get().inner);
                        (
                            action,
                            RouteAuth {
                                auth,
                                challenge,
                                armed: AtomicBool::new(true),
                            },
                        )
                    })
                    .collect(),
                startup_probe,
                ws_router,
                ws_plans,
                ws_slots: Arc::new(Semaphore::new(ws_connections)),
                inline_lane: RwLock::new(None),
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

    fn _set_inline_lane(&self, lane: Py<PyAny>, actions: Vec<u32>) -> PyResult<()> {
        let mut slot = self.core.inline_lane.write().map_err(|_| poisoned())?;
        let set: HashSet<u32> = actions.into_iter().collect();
        *slot = if set.is_empty() {
            None
        } else {
            Some((lane, set))
        };
        Ok(())
    }

    fn _clear_inline_lane(&self) -> PyResult<()> {
        let mut slot = self.core.inline_lane.write().map_err(|_| poisoned())?;
        slot.take();
        Ok(())
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

    /// Stop authenticating a route natively once its provider changed: its
    /// requests go to Python again.
    fn _disarm_route_auth(&self, action: u32) {
        if let Some(route) = self.core.route_auth.get(&action) {
            route.armed.store(false, Ordering::Release);
        }
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
            upgrade: Upgrade::Absent,
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
            }
            | Outcome::Inline {
                request,
                receiver,
                asked,
            } => {
                let reply = self
                    .core
                    .early_reply(&asked, Early::NotReady, Vec::new())
                    .unwrap_or_else(|_| Reply {
                        status: 503,
                        headers: vec![(b"content-length".to_vec(), b"0".to_vec())],
                        body: bytes::Bytes::new(),
                    });
                request.respond_owned(reply.status, reply.headers, reply.body);
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
