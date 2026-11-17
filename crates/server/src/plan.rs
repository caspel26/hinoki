//! The per-route plan, built once at startup from the Python bindings and
//! indexed by action number, so a request needs no Python to be classified.

use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

/// A route's startup-frozen CRUD plan slot. Plans remain disarmed until the
/// worker lifecycle attaches the database and verifies its snapshot.
#[derive(Debug)]
pub struct CrudPlanSlot {
    pub action: u32,
    armed: AtomicBool,
}

impl CrudPlanSlot {
    pub fn new(action: u32) -> Self {
        Self {
            action,
            armed: AtomicBool::new(false),
        }
    }

    pub fn is_armed(&self) -> bool {
        self.armed.load(Ordering::Acquire)
    }

    pub fn arm(&self) {
        self.armed.store(true, Ordering::Release);
    }

    pub fn disarm(&self) {
        self.armed.store(false, Ordering::Release);
    }
}

impl PartialEq for CrudPlanSlot {
    fn eq(&self, other: &Self) -> bool {
        self.action == other.action && self.is_armed() == other.is_armed()
    }
}

impl Eq for CrudPlanSlot {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoutePlan {
    /// Index of the action in the application's binding table.
    pub action: u32,
    pub method: String,
    pub path_params: Vec<String>,
    /// `(source, name, has_default)` for string inputs carried by the native
    /// handler bridge. Only query strings may have defaults.
    pub input_params: Vec<(String, String, bool)>,
    pub has_body: bool,
    pub has_access: bool,
    pub media_types: Vec<String>,
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub transaction: bool,
    pub takes_request: bool,
    /// This action can skip Python request binding and response policy work.
    pub native_handler: bool,
    /// The CRUD plan exists but is not executed before the lifecycle phase.
    pub crud_plan: Option<Arc<CrudPlanSlot>>,
}

impl RoutePlan {
    /// Whether the handler needs the request headers as a mapping: access
    /// checks, negotiation among several renderers, or an explicit `Request`.
    pub fn needs_headers(&self) -> bool {
        self.has_access || self.media_types.len() > 1 || self.takes_request
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum PlanError {
    DuplicateAction(u32),
    NoMediaType(u32),
    BadStatus(u32, u16),
    InvalidNativeHandler(u32),
}

#[derive(Debug, Default)]
pub struct Plan {
    routes: Vec<RoutePlan>,
}

impl Plan {
    /// Validate and index the routes; the position equals the action number.
    pub fn new(mut routes: Vec<RoutePlan>) -> Result<Self, PlanError> {
        let mut seen = HashSet::new();
        for route in &routes {
            if !seen.insert(route.action) {
                return Err(PlanError::DuplicateAction(route.action));
            }
            if route.media_types.is_empty() {
                return Err(PlanError::NoMediaType(route.action));
            }
            if !(100..=599).contains(&route.status) {
                return Err(PlanError::BadStatus(route.action, route.status));
            }
            if route.native_handler && !native_handler_is_valid(route) {
                return Err(PlanError::InvalidNativeHandler(route.action));
            }
        }
        routes.sort_by_key(|route| route.action);
        Ok(Self { routes })
    }

    pub fn get(&self, action: u32) -> Option<&RoutePlan> {
        self.routes
            .binary_search_by_key(&action, |route| route.action)
            .ok()
            .map(|at| &self.routes[at])
    }

    pub fn len(&self) -> usize {
        self.routes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.routes.is_empty()
    }
}

fn native_handler_is_valid(route: &RoutePlan) -> bool {
    let mut names = HashSet::new();
    let mut path_inputs = HashSet::new();
    let inputs_are_supported = route.input_params.iter().all(|(source, name, defaulted)| {
        let unique = names.insert(name);
        if source == "path" {
            path_inputs.insert(name);
        }
        unique
            && !name.is_empty()
            && matches!(source.as_str(), "path" | "query")
            && (source == "query" || !defaulted)
    });
    let route_path_params: HashSet<_> = route.path_params.iter().collect();
    let has_supported_request_inputs = inputs_are_supported
        && path_inputs == route_path_params
        && path_inputs.len() == route.path_params.len();
    let has_no_unsupported_inputs = !route.has_body && !route.has_access && !route.takes_request;
    let uses_default_json = route.media_types.as_slice() == ["application/json"];
    let has_no_response_hooks = route.headers.is_empty() && !route.transaction;
    route.method == "GET"
        && has_supported_request_inputs
        && has_no_unsupported_inputs
        && uses_default_json
        && has_no_response_hooks
}

#[cfg(test)]
mod tests {
    use super::*;

    fn route(action: u32) -> RoutePlan {
        RoutePlan {
            action,
            method: "GET".into(),
            path_params: vec![],
            input_params: vec![],
            has_body: false,
            has_access: false,
            media_types: vec!["application/json".into()],
            status: 200,
            headers: vec![],
            transaction: false,
            takes_request: false,
            native_handler: false,
            crud_plan: None,
        }
    }

    #[test]
    fn looks_up_by_action_in_any_input_order() {
        let plan = Plan::new(vec![route(7), route(2), route(5)]).unwrap();
        assert_eq!(plan.get(5).unwrap().action, 5);
        assert!(plan.get(3).is_none());
        assert_eq!(plan.len(), 3);
    }

    #[test]
    fn refuses_invalid_routes() {
        assert_eq!(
            Plan::new(vec![route(1), route(1)]).unwrap_err(),
            PlanError::DuplicateAction(1)
        );
        let mut none = route(2);
        none.media_types.clear();
        assert_eq!(
            Plan::new(vec![none]).unwrap_err(),
            PlanError::NoMediaType(2)
        );
        let mut bad = route(3);
        bad.status = 99;
        assert_eq!(
            Plan::new(vec![bad]).unwrap_err(),
            PlanError::BadStatus(3, 99)
        );
        let mut invalid_native = route(4);
        invalid_native.native_handler = true;
        invalid_native.has_access = true;
        assert_eq!(
            Plan::new(vec![invalid_native]).unwrap_err(),
            PlanError::InvalidNativeHandler(4)
        );
    }

    #[test]
    fn headers_are_needed_only_when_something_reads_them() {
        let mut r = route(0);
        assert!(!r.needs_headers());
        r.has_access = true;
        assert!(r.needs_headers());
        r.has_access = false;
        r.media_types.push("text/html".into());
        assert!(r.needs_headers());
        r.media_types.pop();
        r.takes_request = true;
        assert!(r.needs_headers());
    }

    #[test]
    fn native_handler_accepts_required_string_path_and_query_inputs() {
        let mut r = route(0);
        r.path_params = vec!["item_id".into()];
        r.input_params = vec![
            ("path".into(), "item_id".into(), false),
            ("query".into(), "term".into(), false),
        ];
        r.native_handler = true;
        assert!(Plan::new(vec![r]).is_ok());
    }

    #[test]
    fn native_handler_rejects_incomplete_or_unsupported_input_plans() {
        let mut missing = route(0);
        missing.path_params = vec!["item_id".into()];
        missing.native_handler = true;
        assert_eq!(
            Plan::new(vec![missing]).unwrap_err(),
            PlanError::InvalidNativeHandler(0)
        );

        let mut unsupported = route(1);
        unsupported.input_params = vec![("body".into(), "payload".into(), false)];
        unsupported.native_handler = true;
        assert_eq!(
            Plan::new(vec![unsupported]).unwrap_err(),
            PlanError::InvalidNativeHandler(1)
        );
    }

    #[test]
    fn native_handler_accepts_defaulted_query_strings_but_not_path_defaults() {
        let mut query_default = route(0);
        query_default.input_params = vec![("query".into(), "view".into(), true)];
        query_default.native_handler = true;
        assert!(Plan::new(vec![query_default]).is_ok());

        let mut path_default = route(1);
        path_default.path_params = vec!["item_id".into()];
        path_default.input_params = vec![("path".into(), "item_id".into(), true)];
        path_default.native_handler = true;
        assert_eq!(
            Plan::new(vec![path_default]).unwrap_err(),
            PlanError::InvalidNativeHandler(1)
        );
    }
}
