//! The per-route plan, built once at startup from the Python bindings and
//! indexed by action number, so a request needs no Python to be classified.

use std::collections::HashSet;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoutePlan {
    /// Index of the action in the application's binding table.
    pub action: u32,
    pub method: String,
    pub path_params: Vec<String>,
    pub has_body: bool,
    pub has_access: bool,
    pub media_types: Vec<String>,
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub transaction: bool,
    pub is_async: bool,
    pub takes_request: bool,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn route(action: u32) -> RoutePlan {
        RoutePlan {
            action,
            method: "GET".into(),
            path_params: vec![],
            has_body: false,
            has_access: false,
            media_types: vec!["application/json".into()],
            status: 200,
            headers: vec![],
            transaction: false,
            is_async: true,
            takes_request: false,
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
}
