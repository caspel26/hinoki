use std::borrow::Cow;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use hinoki_orm::Database;
use kiso::cors::{Cors, PreflightError};
use kiso::headers::{final_headers, Header, HeaderMap, Method, Uri};
use kiso::queue::Push;
use kiso::redirect;
use kiso::ws::Upgrade;
use kiso::{proxy, requestid, BodyReader, Early, InputKind, InputParam, Plan, RoutePlan, Trace};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use tokio::sync::oneshot;

use super::core::{
    prepare_native_inputs, prepare_python_path_params, Core, NativeInput, RouteSpec, StaticPage,
};
use super::prepared::{BodyState, PreparedPathParams, PreparedRequest};
use super::reply::{Reply, Sent};
use crate::crudplan::CrudPlan;
use crate::jwt::Admission;
use crate::request;
use crate::router::{Match, Router};
use crate::web::websocket_origin_allowed;
use crate::websocket::{WsHandshake, WsPlan};

enum Resolved {
    Route(u32, Vec<(String, String)>),
    /// No route: answer natively, with extra headers (`allow`).
    Missing(Early, Vec<Header>),
}

/// HTTP method storage avoiding `String` allocation on the Kiso hot path.
pub enum RequestMethod {
    Hyper(Method),
    Static(&'static str),
    Owned(String),
}

impl RequestMethod {
    pub(crate) fn as_str(&self) -> &str {
        match self {
            Self::Hyper(method) => method.as_str(),
            Self::Static(method) => method,
            Self::Owned(method) => method.as_str(),
        }
    }
}

/// Request URI storage avoiding `path` and `query` clones on the Kiso hot path.
pub enum RequestUri {
    Uri(Uri),
    Parts { path: String, query: Vec<u8> },
}

impl RequestUri {
    pub(crate) fn path(&self) -> &str {
        match self {
            Self::Uri(uri) => uri.path(),
            Self::Parts { path, .. } => path.as_str(),
        }
    }

    pub(crate) fn query(&self) -> &[u8] {
        match self {
            Self::Uri(uri) => uri.query().map_or(&[], str::as_bytes),
            Self::Parts { query, .. } => query.as_slice(),
        }
    }
}

/// Request headers kept in `hyper::HeaderMap` on the Kiso hot path so no
/// `Vec<(Vec<u8>, Vec<u8>)>` is allocated per request.
pub enum RequestHeaders {
    Map(HeaderMap),
    Vec(Vec<Header>),
}

impl RequestHeaders {
    pub(crate) fn joined(&self, name: &str) -> Option<Cow<'_, [u8]>> {
        match self {
            Self::Map(map) => joined_map_value(map, name),
            Self::Vec(vec) => joined_value(vec, name.as_bytes()),
        }
    }

    pub(crate) fn single(&self, name: &str) -> Result<Option<&[u8]>, ()> {
        match self {
            Self::Map(map) => {
                let mut iter = map.get_all(name).iter();
                let first = iter.next().map(|v| v.as_bytes());
                if iter.next().is_some() {
                    return Err(());
                }
                Ok(first)
            }
            Self::Vec(vec) => {
                let mut iter = vec
                    .iter()
                    .filter(|(h, _)| h.eq_ignore_ascii_case(name.as_bytes()))
                    .map(|(_, v)| v.as_slice());
                let first = iter.next();
                if iter.next().is_some() {
                    return Err(());
                }
                Ok(first)
            }
        }
    }

    pub(crate) fn all_values<'a>(&'a self, name: &'a [u8]) -> Vec<&'a [u8]> {
        match self {
            Self::Map(map) => std::str::from_utf8(name)
                .ok()
                .map(|key| map.get_all(key).iter().map(|v| v.as_bytes()).collect())
                .unwrap_or_default(),
            Self::Vec(vec) => vec
                .iter()
                .filter(|(h, _)| h.eq_ignore_ascii_case(name))
                .map(|(_, v)| v.as_slice())
                .collect(),
        }
    }

    pub(crate) fn header_total(&self) -> usize {
        match self {
            Self::Map(map) => kiso::limits::header_total(
                map.iter()
                    .map(|(n, v)| (n.as_str().as_bytes(), v.as_bytes())),
            ),
            Self::Vec(vec) => {
                kiso::limits::header_total(vec.iter().map(|(n, v)| (n.as_slice(), v.as_slice())))
            }
        }
    }

    pub(crate) fn forwarded_scheme(&self) -> Option<bool> {
        match self {
            Self::Map(map) => proxy::forwarded_scheme_values(
                map.get_all("x-forwarded-proto")
                    .iter()
                    .map(|v| v.as_bytes()),
            ),
            Self::Vec(vec) => proxy::forwarded_scheme(vec),
        }
    }

    pub(crate) fn to_py_list<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let out = PyList::empty(py);
        match self {
            Self::Map(map) => {
                for (name, value) in map {
                    out.append((
                        PyBytes::new(py, name.as_str().as_bytes()),
                        PyBytes::new(py, value.as_bytes()),
                    ))?;
                }
            }
            Self::Vec(vec) => {
                for (name, value) in vec {
                    out.append((PyBytes::new(py, name), PyBytes::new(py, value)))?;
                }
            }
        }
        Ok(out)
    }

    pub(crate) fn to_joined_py_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        match self {
            Self::Map(map) => {
                let pairs: Vec<(&[u8], &[u8])> = map
                    .iter()
                    .map(|(n, v)| (n.as_str().as_bytes(), v.as_bytes()))
                    .collect();
                crate::request::joined_header_dict(py, &pairs)
            }
            Self::Vec(vec) => crate::request::joined_header_dict(py, vec),
        }
    }

    pub(crate) fn lookup(&self, name: &str) -> Option<String> {
        match self {
            Self::Map(map) => lookup_map_header(map, name),
            Self::Vec(vec) => crate::request::lookup_header(vec, name),
        }
    }
}

fn joined_map_value<'a>(map: &'a HeaderMap, name: &str) -> Option<Cow<'a, [u8]>> {
    let mut iter = map.get_all(name).iter();
    let first = iter.next()?.as_bytes();
    let Some(second) = iter.next() else {
        return Some(Cow::Borrowed(first));
    };
    let mut joined = Vec::with_capacity(first.len() + 2 + second.as_bytes().len());
    joined.extend_from_slice(first);
    joined.extend_from_slice(b", ");
    joined.extend_from_slice(second.as_bytes());
    for extra in iter {
        joined.extend_from_slice(b", ");
        joined.extend_from_slice(extra.as_bytes());
    }
    Some(Cow::Owned(joined))
}

fn lookup_map_header(map: &HeaderMap, name: &str) -> Option<String> {
    let mut iter = map.get_all(name).iter();
    let first = iter.next()?.as_bytes();
    let separator = if name.eq_ignore_ascii_case("cookie") {
        "; "
    } else {
        ", "
    };
    let mut out = latin1(first);
    for extra in iter {
        out.push_str(separator);
        out.push_str(&latin1(extra.as_bytes()));
    }
    Some(out)
}

/// A request head as the classifier takes it.
pub struct Head {
    pub method: RequestMethod,
    pub uri: RequestUri,
    pub headers: RequestHeaders,
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
    /// What the request asks for in the way of a WebSocket upgrade (HTTP/1 only).
    pub upgrade: Upgrade,
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
        receiver: oneshot::Receiver<Sent>,
        asked: Asked,
    },
    /// Dispatched inline to `NativeLane.start` on the Tokio thread to avoid
    /// socketpair / epoll_wait ping-pong for direct native-lane routes.
    Inline {
        request: Box<PreparedRequest>,
        receiver: oneshot::Receiver<Sent>,
        asked: Asked,
    },
    /// Eligible read request whose database work runs on the Rust worker.
    NativeCrud {
        request: Box<PreparedRequest>,
        receiver: oneshot::Receiver<Sent>,
        asked: Asked,
        plan: Arc<CrudPlan>,
        database: Database,
        params: Vec<(String, String)>,
        query: Vec<u8>,
    },
}

pub(super) fn build_routes(
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
    /// The WebSocket action a request addresses, if it is theirs to answer. A
    /// plain GET that an HTTP route also matches (`/rooms/stats` beside
    /// `/rooms/{room}`) is that route's, not a refused upgrade.
    fn websocket_target(&self, head: &Head) -> Option<(u32, Vec<(String, String)>)> {
        let method = head.method.as_str();
        let path = head.uri.path();
        if method != "GET" || !path.is_ascii() || self.ws_plans.is_empty() {
            return None;
        }
        let Match::Found { action, params } = self.ws_router.find("GET", path) else {
            return None;
        };
        let plain_http = matches!(head.upgrade, Upgrade::Absent)
            && matches!(self.resolve("GET", path), Resolved::Route(..));
        (!plain_http).then_some((action, params))
    }

    /// Classify one request: refuse it, answer it natively, or queue it for the
    /// Python pipeline. The `Err` is an internal inconsistency (a missing plan or
    /// early body), never a client mistake.
    pub(crate) fn classify(
        &self,
        head: Head,
        native_execution: bool,
        trace: Option<&Trace>,
    ) -> Result<Outcome, String> {
        let (request_id, id_ignored) = self.request_id(&head.headers, head.trusted);
        let mut asked = Asked {
            head_only: head.method.as_str() == "HEAD",
            request_id,
            id_ignored,
            https: head.https,
            origin: origin_of(&head.headers),
            action: None,
        };
        if let Some(early) = self.refuse(head.uri.query(), &head.headers) {
            return self.answer(&asked, early, Vec::new());
        }
        if let Some(early) = head.refusal.clone() {
            return self.answer(&asked, early, Vec::new());
        }
        if let Some(outcome) = self.policy_response(&head, &asked)? {
            return Ok(outcome);
        }
        let method = head.method.as_str();
        let path = head.uri.path();
        let is_startup_probe = self
            .startup_probe
            .as_ref()
            .is_some_and(|probe| probe.path == path);
        if is_startup_probe {
            return self.startup_response(&asked, method, head.uri.query(), &head.headers);
        }
        if let Some(page) = self.static_page(method, path) {
            return Ok(self.static_response(&asked, page));
        }
        if let Some((action, params)) = self.websocket_target(&head) {
            return self.websocket(head, asked, action, params);
        }
        let (action, params) = match self.resolve(method, path) {
            // A raw target the server cannot read as a plain path, and the
            // documentation pages, are the Python pipeline's to answer.
            _ if !path.is_ascii() || self.is_python_path(method, path) => (None, Vec::new()),
            Resolved::Route(action, params) => (Some(action), params),
            Resolved::Missing(early, extra) => return self.answer(&asked, early, extra),
        };
        if let (Some(trace), Some(number)) = (trace, action) {
            if let Some(pattern) = self.templates.get(&number) {
                trace.set_route(Arc::clone(pattern));
            }
            trace.set_action(number);
        }
        asked.action = action;
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
                    (NativeInput::default(), false, None),
                )
            }
        };
        let native_handler = plan.native_handler;
        let mut native_input = if native_handler {
            prepare_native_inputs(plan, &params, head.uri.query(), &head.headers)
                .unwrap_or_default()
        } else {
            NativeInput::default()
        };
        if let Some((early, extra)) =
            action.and_then(|n| self.authenticate(n, &head, &mut native_input))
        {
            return self.answer(&asked, early, extra);
        }
        let prepared_path_params =
            (!native_input.enabled).then(|| prepare_python_path_params(&params));
        self.enqueue(
            head,
            asked,
            (action, params),
            plan.needs_headers(),
            prepared_path_params,
            (native_input, native_execution, None),
        )
    }

    /// A request for a WebSocket route: refuse what is not a valid upgrade or
    /// comes from a disallowed origin, take a connection slot, then queue it for
    /// the handshake that Python (access, subprotocol) decides.
    fn websocket(
        &self,
        head: Head,
        asked: Asked,
        action: u32,
        params: Vec<(String, String)>,
    ) -> Result<Outcome, String> {
        let plan = self
            .ws_plans
            .get(&action)
            .ok_or_else(|| format!("no WebSocket plan for action {action}"))?;
        match &head.upgrade {
            Upgrade::Offer(_) => {}
            Upgrade::BadKey => return self.answer(&asked, Early::BadUpgrade, Vec::new()),
            Upgrade::Absent | Upgrade::BadVersion => {
                return self.answer(&asked, Early::UpgradeRequired, Vec::new())
            }
        }
        if !self.origin_allowed(&head.headers, &asked.origin, asked.https, plan) {
            return self.answer(&asked, Early::CorsOriginDenied, Vec::new());
        }
        let Ok(hold) = Arc::clone(&self.ws_slots).try_acquire_owned() else {
            return self.answer(&asked, Early::Overloaded, Vec::new());
        };
        let handshake = WsHandshake {
            config: plan.config.clone(),
            queue: plan.queue,
            hold: Some(hold),
        };
        let mut asked = asked;
        asked.action = None;
        let prepared = Some(prepare_python_path_params(&params));
        let head = Head {
            method: RequestMethod::Static("WS"),
            ..head
        };
        self.enqueue(
            head,
            asked,
            (Some(action), params),
            true,
            prepared,
            (NativeInput::default(), false, Some(handshake)),
        )
    }

    /// The rule is `web::websocket_origin_allowed`, which the Python lane calls too.
    fn origin_allowed(
        &self,
        headers: &RequestHeaders,
        origin: &[u8],
        https: bool,
        plan: &WsPlan,
    ) -> bool {
        let host = headers.joined("host");
        let cors: &[Vec<u8>] = self.cors.as_ref().map_or(&[], |cors| &cors.origins);
        websocket_origin_allowed(origin, host.as_deref(), https, &plan.origins, cors)
    }

    /// Authenticate a native route that uses a claims-only provider, in the order
    /// the Python pipeline does: after a bad path encoding or malformed query
    /// string (which Python reports first, so those requests stay queued) and
    /// before negotiation and validation. `Some` is the refusal to answer; an
    /// admitted caller is recorded in `input`. Anything Rust and Python might
    /// read differently is left to Python.
    fn authenticate(
        &self,
        action: u32,
        head: &Head,
        input: &mut NativeInput,
    ) -> Option<(Early, Vec<Header>)> {
        let route = self.route_auth.get(&action)?;
        if !route.armed.load(Ordering::Acquire) {
            return None;
        }
        let reported_first = input.error.as_ref().is_some_and(|error| {
            matches!(
                error.code.as_str(),
                "bad_path_encoding" | "invalid_query" | "internal_error"
            )
        });
        if !input.enabled || reported_first || request::parse_query(head.uri.query(), 1000).is_err()
        {
            return None;
        }
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |elapsed| i64::try_from(elapsed.as_secs()).unwrap_or(0));
        let challenge = || {
            route
                .challenge
                .iter()
                .map(|value| (b"www-authenticate".to_vec(), value.clone()))
                .collect()
        };
        let header_name = std::str::from_utf8(route.auth.credential_header_name()).ok()?;
        let refusal = match route
            .auth
            .admit_single(head.headers.single(header_name), now)
        {
            Admission::Admit {
                subject,
                scopes,
                raw_claims,
                ..
            } => {
                input.principal = Some((subject, scopes, raw_claims));
                return None;
            }
            Admission::Defer => return None,
            Admission::Missing => (Early::Unauthorized, challenge()),
            Admission::Invalid => (Early::InvalidCredentials, challenge()),
            Admission::Forbidden => (Early::Forbidden, Vec::new()),
        };
        Some(refusal)
    }

    fn policy_response(&self, head: &Head, asked: &Asked) -> Result<Option<Outcome>, String> {
        if !self.has_policies() {
            return Ok(None);
        }
        // `Hosts` reads the header as the Latin-1 text `join_headers` makes.
        let host = head.headers.joined("host").map(latin1_bytes);
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
        let Some(location) =
            redirect::location(host_bytes, head.uri.path().as_bytes(), head.uri.query())
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
    fn preflight_response(&self, head: &Head, asked: &Asked) -> Result<Option<Outcome>, String> {
        let origin = head.headers.joined("origin");
        let requested = head.headers.joined("access-control-request-method");
        let Some((action, cors)) = self.preflight_policy(head.uri.path(), requested.as_deref())
        else {
            return Ok(None);
        };
        // The answer, a refusal too, carries the policy that decided it.
        let asked = &Asked {
            action,
            ..asked.clone()
        };
        match cors.preflight(head.method.as_str(), origin.as_deref(), requested.is_some()) {
            Ok(Some(own)) => self
                .policy_reply(asked, 204, own, Vec::new())
                .map(|reply| Some(Outcome::Early(reply))),
            Ok(None) => Ok(None),
            Err(PreflightError::OriginDenied) => self
                .answer(asked, Early::CorsOriginDenied, Vec::new())
                .map(Some),
        }
    }

    /// The policy that answers a preflight: the one of the route the browser asks
    /// about (the method in `Access-Control-Request-Method` on this path) when it
    /// declares its own, else the application's. The action is that route's.
    fn preflight_policy(
        &self,
        path: &str,
        requested: Option<&[u8]>,
    ) -> Option<(Option<u32>, &Cors)> {
        let own = requested
            .and_then(|method| std::str::from_utf8(method).ok())
            .filter(|_| path.is_ascii() && !self.route_cors.is_empty())
            .and_then(|method| match self.resolve(method, path) {
                Resolved::Route(action, _) => self
                    .route_cors
                    .get(&action)
                    .map(|cors| (Some(action), cors)),
                Resolved::Missing(..) => None,
            });
        own.or_else(|| self.cors.as_ref().map(|cors| (None, cors)))
    }

    /// The CORS policy of an answer: its route's own, else the application's.
    fn cors_headers(&self, asked: &Asked) -> Vec<Header> {
        asked
            .action
            .and_then(|action| self.route_cors.get(&action))
            .or(self.cors.as_ref())
            .map(|policy| policy.headers(&asked.origin))
            .unwrap_or_default()
    }

    fn has_policies(&self) -> bool {
        self.hosts.is_some()
            || self.cors.is_some()
            || !self.route_cors.is_empty()
            || self.redirect_https
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
        let cors = self.cors_headers(asked);
        let body = if asked.head_only {
            bytes::Bytes::new()
        } else {
            bytes::Bytes::from(page.body.clone())
        };
        let headers = final_headers(
            page.headers.clone(),
            &self.policies[usize::from(asked.https)],
            &[],
            (&self.id_name, asked.request_id.as_bytes()),
            &cors,
            200,
            page.body.len(),
        );
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
        headers: &RequestHeaders,
    ) -> Result<Outcome, String> {
        if !matches!(method, "GET" | "HEAD") {
            return self.answer(
                asked,
                Early::MethodNotAllowed,
                vec![(b"allow".to_vec(), b"GET, HEAD".to_vec())],
            );
        }
        let accept = headers
            .joined("accept")
            .map_or_else(String::new, |v| latin1(&v));
        if request::negotiate(&accept, &["application/json".to_owned()]).is_none() {
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
        let body = if asked.head_only {
            bytes::Bytes::new()
        } else {
            bytes::Bytes::from(probe.body.clone())
        };
        let own = vec![(b"content-type".to_vec(), b"application/json".to_vec())];
        let cors = self.cors_headers(asked);
        let response_headers = final_headers(
            own,
            &self.policies[usize::from(asked.https)],
            &[],
            (&self.id_name, asked.request_id.as_bytes()),
            &cors,
            200,
            probe.body.len(),
        );
        Ok(Outcome::Early(Reply {
            status: 200,
            headers: response_headers,
            body,
        }))
    }

    /// Take an in-flight slot and queue the request, or answer 503 natively.
    fn enqueue(
        &self,
        head: Head,
        asked: Asked,
        (action, params): (Option<u32>, Vec<(String, String)>),
        needs_headers: bool,
        prepared_path_params: Option<PreparedPathParams>,
        (native_input, native_execution, websocket): (NativeInput, bool, Option<WsHandshake>),
    ) -> Result<Outcome, String> {
        let Some(permit) = self.in_flight.try_acquire() else {
            return self.answer(&asked, Early::Overloaded, Vec::new());
        };
        let (sender, receiver) = oneshot::channel();
        let route_plan = action.and_then(|number| self.plan.get(number));
        let native_crud = route_plan
            .and_then(|route| route.crud_plan.as_ref())
            .is_some_and(|slot| slot.is_armed());
        let https_idx = usize::from(asked.https);
        let native_policy = Arc::clone(&self.policies[https_idx]);
        let compiled_policy = self.compiled_policies[https_idx].clone();
        let native_cors = self.cors_headers(&asked);
        let route_headers = if native_crud {
            route_plan.map_or_else(Vec::new, |route| route.headers.clone())
        } else {
            Vec::new()
        };
        let request = PreparedRequest {
            method: head.method,
            uri: head.uri,
            headers: head.headers,
            request_id: asked.request_id.clone(),
            id_ignored: asked.id_ignored,
            action,
            native_handler: native_input.enabled,
            native_args: native_input.args,
            native_error: native_input.error,
            native_principal: native_input.principal,
            native_policy,
            compiled_policy,
            native_cors,
            route_headers,
            native_crud,
            request_id_name: Arc::clone(&self.id_name),
            compiled_id_name: self.compiled_id_name.clone(),
            needs_headers,
            params,
            prepared_path_params,
            client: head.peer,
            http_version: head.http_version,
            reply: Mutex::new(Some(sender)),
            permit: Mutex::new(Some(permit)),
            body: Mutex::new(head.body.map_or(BodyState::Absent, BodyState::Unread)),
            native_body_consumed: Arc::new(AtomicBool::new(false)),
            native_failure: Mutex::new(None),
            native_rows: Mutex::new(None),
            websocket: Mutex::new(websocket),
        };
        let accepts_native_crud = native_execution
            && native_crud
            && route_plan.is_some_and(|route| {
                let accept = request.headers.joined("accept").unwrap_or_default();
                std::str::from_utf8(&accept)
                    .is_ok_and(|accept| request::negotiate(accept, &route.media_types).is_some())
            });
        if accepts_native_crud {
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
                let crud_params = request.params.clone();
                let crud_query = request.uri.query().to_vec();
                return Ok(Outcome::NativeCrud {
                    request: Box::new(request),
                    receiver,
                    asked,
                    plan: Arc::clone(plan),
                    database,
                    params: crud_params,
                    query: crud_query,
                });
            }
        }
        if native_execution
            && request.native_handler
            && request.native_error.is_none()
            && action.is_some_and(|n| self.is_inline_action(n))
        {
            return Ok(Outcome::Inline {
                request: Box::new(request),
                receiver,
                asked,
            });
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
    pub fn peer_view(&self, peer: SocketAddr, headers: &RequestHeaders) -> (bool, Option<bool>) {
        let trusted = self.proxies.contains(peer.ip());
        (
            trusted,
            trusted.then(|| headers.forwarded_scheme()).flatten(),
        )
    }

    /// The inbound id only from a trusted peer (the caller knows the peer) and
    /// only when the application trusts inbound ids. The flag says a trusted
    /// peer sent an id header that was refused.
    fn request_id(&self, headers: &RequestHeaders, trusted: bool) -> (String, bool) {
        let mut ignored = false;
        if trusted && self.trust_request_id {
            let found = headers.all_values(&self.id_name);
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
    fn refuse(&self, query: &[u8], headers: &RequestHeaders) -> Option<Early> {
        if !self.ready.load(Ordering::Acquire) {
            return Some(Early::NotReady);
        }
        self.limits
            .check_request(query.len(), headers.header_total())
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
        body: Vec<u8>,
    ) -> Result<Reply, String> {
        if asked.id_ignored {
            self.ignored_ids.fetch_add(1, Ordering::Relaxed);
        }
        let cors = self.cors_headers(asked);
        let headers = final_headers(
            own,
            &self.policies[usize::from(asked.https)],
            &[],
            (&self.id_name, asked.request_id.as_bytes()),
            &cors,
            status,
            body.len(),
        );
        let final_body = if asked.head_only {
            bytes::Bytes::new()
        } else {
            bytes::Bytes::from(body)
        };
        Ok(Reply {
            status,
            headers,
            body: final_body,
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
    /// The matched route, once known: its own CORS policy, if any, applies.
    action: Option<u32>,
}

/// The `Origin` header as `origin_of` in the Python pipeline reads it: repeated
/// headers are joined with ", ", so they match no single origin.
fn origin_of(headers: &RequestHeaders) -> Vec<u8> {
    headers
        .joined("origin")
        .map_or_else(Vec::new, Cow::into_owned)
}

/// One request header as `request::join_headers` reads it: the name compared
/// without case, repeated fields joined with ", ". Borrowed unless it repeats.
pub(super) fn joined_value<'a>(headers: &'a [Header], name: &[u8]) -> Option<Cow<'a, [u8]>> {
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
pub(super) fn latin1(bytes: &[u8]) -> String {
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
