//! Path router: the first real unit of the native request core.
//!
//! Pure Rust, no I/O and no Python types, so it is tested with plain
//! `cargo test`. Templates use `{name}` segments; literal segments win over
//! parameter segments, with backtracking when a literal branch dead-ends.

use std::collections::HashMap;
use std::fmt;

#[derive(Debug, PartialEq, Eq)]
pub enum RouteError {
    /// Template must start with `/` and contain no empty or malformed segments.
    InvalidTemplate(String),
    /// The same method + equivalent path shape was already registered.
    Duplicate { method: String, template: String },
    /// Two templates use different parameter names at the same position.
    ParamConflict { template: String, existing: String },
}

impl fmt::Display for RouteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidTemplate(t) => write!(f, "invalid path template {t:?}"),
            Self::Duplicate { method, template } => {
                write!(f, "duplicate route {method} {template}")
            }
            Self::ParamConflict { template, existing } => write!(
                f,
                "path parameter in {template:?} conflicts with existing parameter {{{existing}}}"
            ),
        }
    }
}

impl std::error::Error for RouteError {}

#[derive(Debug, PartialEq, Eq)]
pub enum Match {
    Found {
        action: u32,
        params: Vec<(String, String)>,
    },
    /// The path exists for other methods only.
    MethodNotAllowed {
        allowed: Vec<String>,
    },
    NotFound,
}

#[derive(Default)]
struct Node {
    literals: HashMap<String, Node>,
    param: Option<(String, Box<Node>)>,
    actions: HashMap<String, u32>,
}

#[derive(Default)]
pub struct Router {
    root: Node,
}

fn segments(template: &str) -> Option<Vec<&str>> {
    let rest = template.strip_prefix('/')?;
    if rest.is_empty() {
        return Some(Vec::new());
    }
    let parts: Vec<&str> = rest.split('/').collect();
    parts.iter().all(|s| !s.is_empty()).then_some(parts)
}

fn param_name(seg: &str) -> Result<Option<&str>, ()> {
    match (seg.starts_with('{'), seg.ends_with('}')) {
        (true, true) if seg.len() > 2 && !seg[1..seg.len() - 1].contains(['{', '}']) => {
            Ok(Some(&seg[1..seg.len() - 1]))
        }
        (false, false) if !seg.contains(['{', '}']) => Ok(None),
        _ => Err(()),
    }
}

impl Router {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn add(&mut self, method: &str, template: &str, action: u32) -> Result<(), RouteError> {
        let invalid = || RouteError::InvalidTemplate(template.to_owned());
        let segs = segments(template).ok_or_else(invalid)?;
        let method = method.to_ascii_uppercase();
        let mut node = &mut self.root;
        for seg in segs {
            node = match param_name(seg).map_err(|()| invalid())? {
                None => node.literals.entry(seg.to_owned()).or_default(),
                Some(name) => {
                    let (existing, child) = node
                        .param
                        .get_or_insert_with(|| (name.to_owned(), Box::default()));
                    if existing != name {
                        return Err(RouteError::ParamConflict {
                            template: template.to_owned(),
                            existing: existing.clone(),
                        });
                    }
                    child
                }
            };
        }
        if node.actions.contains_key(&method) {
            return Err(RouteError::Duplicate {
                method,
                template: template.to_owned(),
            });
        }
        node.actions.insert(method, action);
        Ok(())
    }

    pub fn find(&self, method: &str, path: &str) -> Match {
        let Some(segs) = segments(path) else {
            return Match::NotFound;
        };
        let method = method.to_ascii_uppercase();
        let mut params = Vec::new();
        match Self::walk(&self.root, &segs, &mut params) {
            None => Match::NotFound,
            Some(node) => match node.actions.get(&method) {
                Some(&action) => Match::Found { action, params },
                None => {
                    let mut allowed: Vec<String> = node.actions.keys().cloned().collect();
                    allowed.sort();
                    Match::MethodNotAllowed { allowed }
                }
            },
        }
    }

    fn walk<'a>(
        node: &'a Node,
        segs: &[&str],
        params: &mut Vec<(String, String)>,
    ) -> Option<&'a Node> {
        let Some((head, tail)) = segs.split_first() else {
            return (!node.actions.is_empty()).then_some(node);
        };
        if let Some(found) = node
            .literals
            .get(*head)
            .and_then(|child| Self::walk(child, tail, params))
        {
            return Some(found);
        }
        let (name, child) = node.param.as_ref()?;
        params.push((name.clone(), (*head).to_owned()));
        let found = Self::walk(child, tail, params);
        if found.is_none() {
            params.pop();
        }
        found
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn router() -> Router {
        let mut r = Router::new();
        r.add("GET", "/posts", 1).unwrap();
        r.add("POST", "/posts", 2).unwrap();
        r.add("GET", "/posts/{post_id}", 3).unwrap();
        r.add("POST", "/posts/{post_id}/publish", 4).unwrap();
        r.add("GET", "/posts/featured", 5).unwrap();
        r.add("GET", "/", 6).unwrap();
        r
    }

    fn found(action: u32, params: &[(&str, &str)]) -> Match {
        Match::Found {
            action,
            params: params
                .iter()
                .map(|(k, v)| ((*k).into(), (*v).into()))
                .collect(),
        }
    }

    #[test]
    fn matches_static_and_param_routes() {
        let r = router();
        assert_eq!(r.find("GET", "/"), found(6, &[]));
        assert_eq!(r.find("get", "/posts"), found(1, &[]));
        assert_eq!(r.find("GET", "/posts/42"), found(3, &[("post_id", "42")]));
        assert_eq!(
            r.find("POST", "/posts/42/publish"),
            found(4, &[("post_id", "42")])
        );
    }

    #[test]
    fn literal_wins_over_parameter() {
        assert_eq!(router().find("GET", "/posts/featured"), found(5, &[]));
    }

    #[test]
    fn backtracks_when_literal_branch_dead_ends() {
        // `/posts/featured` has no `/publish` child, the param branch does.
        assert_eq!(
            router().find("POST", "/posts/featured/publish"),
            found(4, &[("post_id", "featured")])
        );
    }

    #[test]
    fn reports_method_not_allowed_with_sorted_methods() {
        assert_eq!(
            router().find("DELETE", "/posts"),
            Match::MethodNotAllowed {
                allowed: vec!["GET".into(), "POST".into()]
            }
        );
    }

    #[test]
    fn unknown_or_malformed_paths_are_not_found() {
        let r = router();
        assert_eq!(r.find("GET", "/nope"), Match::NotFound);
        assert_eq!(r.find("GET", "/posts//1"), Match::NotFound);
        assert_eq!(r.find("GET", "posts"), Match::NotFound);
        // An intermediate node without handlers is not a route.
        assert_eq!(r.find("GET", "/posts/1/"), Match::NotFound);
    }

    #[test]
    fn rejects_duplicates_conflicts_and_bad_templates() {
        let mut r = router();
        assert!(matches!(
            r.add("GET", "/posts/{other}", 9),
            Err(RouteError::ParamConflict { .. })
        ));
        assert!(matches!(
            r.add("get", "/posts", 9),
            Err(RouteError::Duplicate { .. })
        ));
        for bad in ["posts", "/a//b", "/a/{}", "/a/{x", "/a/x}y", "/a/{b{c}}"] {
            assert!(
                matches!(r.add("GET", bad, 9), Err(RouteError::InvalidTemplate(_))),
                "{bad}"
            );
        }
    }
}
