//! Answers produced without Python.
//!
//! Bodies carry the request id, so they are not constant. The Python side
//! renders each once at startup with a sentinel id ([`SENTINEL`]); the crate
//! splits the bytes around the sentinel and splices the real id per request.

use std::collections::HashMap;

/// Placeholder request id used when the bodies are rendered at startup.
pub const SENTINEL: &str = "0123456789abcdeffedcba9876543210";

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Early {
    UriTooLong,
    HeadersTooLarge,
    NotReady,
    Overloaded,
    NotFound,
    MethodNotAllowed,
    InvalidQuery,
    NotAcceptable,
    /// A missing or repeated `Host` on HTTP/1.1 (RFC 9112 section 3.2).
    BadHost,
    DisallowedHost,
    CorsOriginDenied,
    /// No credentials on a route that needs them.
    Unauthorized,
    /// Credentials that do not authenticate.
    InvalidCredentials,
    /// An authenticated caller without the route's permissions.
    Forbidden,
}

impl Early {
    pub const fn status(&self) -> u16 {
        match self {
            Early::UriTooLong => 414,
            Early::HeadersTooLarge => 431,
            Early::NotReady | Early::Overloaded => 503,
            Early::NotFound => 404,
            Early::MethodNotAllowed => 405,
            Early::InvalidQuery => 400,
            Early::NotAcceptable => 406,
            Early::BadHost => 400,
            Early::DisallowedHost => 400,
            Early::CorsOriginDenied | Early::Forbidden => 403,
            Early::Unauthorized | Early::InvalidCredentials => 401,
        }
    }

    /// Fixed extra headers (`Retry-After` on the 503s, like the Python runtime).
    pub fn headers(&self) -> &'static [(&'static str, &'static str)] {
        match self {
            Early::NotReady | Early::Overloaded => &[("retry-after", "1")],
            _ => &[],
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum TemplateError {
    /// The rendered body does not contain the sentinel exactly once.
    Sentinel(usize),
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

/// A body template: bytes before and after the request id.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Template {
    prefix: Vec<u8>,
    suffix: Vec<u8>,
}

impl Template {
    fn split(rendered: &[u8]) -> Result<Self, TemplateError> {
        let needle = SENTINEL.as_bytes();
        let mut found = Vec::new();
        let mut from = 0;
        while let Some(at) = find(&rendered[from..], needle) {
            found.push(from + at);
            from += at + needle.len();
        }
        match found.as_slice() {
            [at] => Ok(Self {
                prefix: rendered[..*at].to_vec(),
                suffix: rendered[at + needle.len()..].to_vec(),
            }),
            other => Err(TemplateError::Sentinel(other.len())),
        }
    }

    fn fill(&self, request_id: &str) -> Vec<u8> {
        let mut body = Vec::with_capacity(self.prefix.len() + request_id.len() + self.suffix.len());
        body.extend_from_slice(&self.prefix);
        for byte in request_id.bytes() {
            // A trusted inbound id may hold a quote or backslash: keep the JSON valid.
            if byte == b'"' || byte == b'\\' {
                body.push(b'\\');
            }
            body.push(byte);
        }
        body.extend_from_slice(&self.suffix);
        body
    }
}

/// The templates of every early answer, built once at startup.
#[derive(Debug, Default)]
pub struct EarlyBodies {
    templates: HashMap<Early, Template>,
}

impl EarlyBodies {
    /// `rendered` maps each kind to the Python-rendered JSON body (with the
    /// sentinel id). A kind without a body, or with a body lacking exactly one
    /// sentinel, is an error: a wrong body must not be served.
    pub fn new(rendered: &[(Early, Vec<u8>)]) -> Result<Self, TemplateError> {
        let mut bodies = Self::default();
        for (kind, body) in rendered {
            bodies
                .templates
                .insert(kind.clone(), Template::split(body)?);
        }
        Ok(bodies)
    }

    /// The body for `kind` with `request_id` spliced in; `None` when no template
    /// was registered for it.
    pub fn render(&self, kind: &Early, request_id: &str) -> Option<Vec<u8>> {
        self.templates.get(kind).map(|t| t.fill(request_id))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn body(error: &str) -> Vec<u8> {
        format!("{{\"error\": \"{error}\", \"request_id\": \"{SENTINEL}\"}}").into_bytes()
    }

    #[test]
    fn splices_the_request_id() {
        let bodies = EarlyBodies::new(&[(Early::UriTooLong, body("uri_too_long"))]).unwrap();
        let out = bodies.render(&Early::UriTooLong, "abcd").unwrap();
        assert_eq!(
            out,
            b"{\"error\": \"uri_too_long\", \"request_id\": \"abcd\"}"
        );
    }

    #[test]
    fn escapes_quotes_and_backslashes_in_the_id() {
        let bodies = EarlyBodies::new(&[(Early::NotFound, body("not_found"))]).unwrap();
        let out = bodies.render(&Early::NotFound, "a\"}\\").unwrap();
        let text = String::from_utf8(out).unwrap();
        assert!(text.ends_with(r#""request_id": "a\"}\\"}"#), "{text}");
    }

    #[test]
    fn distinct_kinds_with_one_status_stay_apart() {
        let bodies = EarlyBodies::new(&[
            (Early::NotReady, body("not_ready")),
            (Early::Overloaded, body("overloaded")),
        ])
        .unwrap();
        let ready = bodies.render(&Early::NotReady, "x").unwrap();
        let busy = bodies.render(&Early::Overloaded, "x").unwrap();
        assert!(String::from_utf8(ready).unwrap().contains("not_ready"));
        assert!(String::from_utf8(busy).unwrap().contains("overloaded"));
    }

    #[test]
    fn refuses_a_body_without_exactly_one_sentinel() {
        assert_eq!(
            EarlyBodies::new(&[(Early::NotFound, b"{}".to_vec())]).unwrap_err(),
            TemplateError::Sentinel(0)
        );
        let twice = format!("{SENTINEL}{SENTINEL}").into_bytes();
        assert_eq!(
            EarlyBodies::new(&[(Early::NotFound, twice)]).unwrap_err(),
            TemplateError::Sentinel(2)
        );
    }

    #[test]
    fn unregistered_kind_has_no_body() {
        assert!(EarlyBodies::default()
            .render(&Early::NotFound, "x")
            .is_none());
    }

    #[test]
    fn statuses_and_retry_after() {
        assert_eq!(Early::UriTooLong.status(), 414);
        assert_eq!(Early::HeadersTooLarge.status(), 431);
        assert_eq!(Early::NotReady.headers(), &[("retry-after", "1")]);
        assert!(Early::NotFound.headers().is_empty());
    }
}
