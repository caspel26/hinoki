//! The final header list of an answer, in the order the Python runtime builds
//! it (`core.response_headers`, then `content-length`).

use hyper::header::{CONTENT_LENGTH, CONTENT_TYPE};

pub use hyper::header::{HeaderName, HeaderValue};
pub use hyper::{HeaderMap, Method, Uri};

/// Header names the framework's security policy owns (lowercase). Mirrors
/// `hinoki.runtime.security.POLICY_HEADERS`.
pub const POLICY_NAMES: [&[u8]; 4] = [
    b"x-frame-options",
    b"referrer-policy",
    b"x-content-type-options",
    b"strict-transport-security",
];

pub type Header = (Vec<u8>, Vec<u8>);
pub type CompiledHeader = (HeaderName, HeaderValue);

static APPLICATION_JSON: HeaderValue = HeaderValue::from_static("application/json");

pub fn is_policy(lower: &[u8]) -> bool {
    POLICY_NAMES.contains(&lower)
}

/// Statuses that carry no `content-length` (`BODYLESS` in the Python runtime).
pub const fn is_bodyless(status: u16) -> bool {
    status == 204 || status == 304
}

fn format_usize(mut value: usize, digits: &mut [u8; 20]) -> &[u8] {
    let mut at = digits.len();
    loop {
        at -= 1;
        digits[at] = b'0' + (value % 10) as u8;
        value /= 10;
        if value == 0 {
            break;
        }
    }
    &digits[at..]
}

fn usize_bytes(value: usize) -> Vec<u8> {
    let mut digits = [0u8; 20];
    format_usize(value, &mut digits).to_vec()
}

/// Pre-compile static headers once at startup so hot responses skip validation
/// and `Vec<u8>` allocations per request.
pub fn compile_headers(raw: &[Header]) -> Option<Vec<CompiledHeader>> {
    raw.iter()
        .map(|(name, value)| {
            Some((
                HeaderName::from_bytes(name).ok()?,
                HeaderValue::from_bytes(value).ok()?,
            ))
        })
        .collect()
}

/// Build the final `HeaderMap` directly for a JSON response without route or
/// CORS headers, avoiding intermediate `Vec<(Vec<u8>, Vec<u8>)>` allocations
/// and repeated header parsing.
pub fn final_json_header_map(
    policy: &[CompiledHeader],
    request_id_name: &HeaderName,
    request_id: &[u8],
    status: u16,
    body_len: usize,
) -> Option<HeaderMap> {
    let id_value = HeaderValue::from_bytes(request_id).ok()?;
    let extra = usize::from(!is_bodyless(status));
    let mut map = HeaderMap::with_capacity(policy.len() + 2 + extra);
    map.append(CONTENT_TYPE, APPLICATION_JSON.clone());
    for (name, value) in policy {
        map.append(name.clone(), value.clone());
    }
    map.append(request_id_name.clone(), id_value);
    if !is_bodyless(status) {
        let mut digits = [0u8; 20];
        let len_value = HeaderValue::from_bytes(format_usize(body_len, &mut digits)).ok()?;
        map.append(CONTENT_LENGTH, len_value);
    }
    Some(map)
}

/// The answer's own headers without policy headers and the request id, then the
/// policy headers, the route headers nothing above already set, the request id,
/// `extra` (CORS), and `content-length` unless the status is bodyless.
pub fn final_headers(
    own: Vec<Header>,
    policy: &[Header],
    route: &[(String, String)],
    id: (&[u8], &[u8]),
    extra: &[Header],
    status: u16,
    body_len: usize,
) -> Vec<Header> {
    let mut out = head_headers(own, policy, route, id, extra);
    if !is_bodyless(status) {
        out.push((b"content-length".to_vec(), usize_bytes(body_len)));
    }
    out
}

/// `name` lower-cased equals `lower`, without building the lower-cased copy.
fn lower_eq(name: &[u8], lower: &[u8]) -> bool {
    name.len() == lower.len()
        && name
            .iter()
            .zip(lower)
            .all(|(a, b)| a.to_ascii_lowercase() == *b)
}

fn is_policy_name(name: &[u8]) -> bool {
    POLICY_NAMES.iter().any(|policy| lower_eq(name, policy))
}

/// [`final_headers`] without the `content-length`, for a body whose length
/// the caller states itself or leaves open. The answer's own headers move into
/// the result.
pub fn head_headers(
    own: Vec<Header>,
    policy: &[Header],
    route: &[(String, String)],
    (request_id_name, request_id): (&[u8], &[u8]),
    extra: &[Header],
) -> Vec<Header> {
    let mut out: Vec<Header> = Vec::with_capacity(own.len() + policy.len() + route.len() + 2);
    // Own policy names are dropped (the policy list replaces them) and the
    // own request id is replaced by the one pushed below.
    out.extend(
        own.into_iter()
            .filter(|(name, _)| !is_policy_name(name) && !lower_eq(name, request_id_name)),
    );
    out.extend(policy.iter().cloned());
    // A route header yields to every own and policy name; `out` holds exactly
    // those until a route header is pushed. An own request id and an own policy
    // name were dropped above, but a route header with either name is skipped
    // on its own.
    let fixed = out.len();
    for (name, value) in route {
        let name = name.as_bytes();
        if lower_eq(name, request_id_name)
            || is_policy_name(name)
            || out[..fixed]
                .iter()
                .any(|(present, _)| present.eq_ignore_ascii_case(name))
        {
            continue;
        }
        out.push((name.to_vec(), value.as_bytes().to_vec()));
    }
    out.push((request_id_name.to_vec(), request_id.to_vec()));
    out.extend(extra.iter().cloned());
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn h(name: &str, value: &str) -> Header {
        (name.as_bytes().to_vec(), value.as_bytes().to_vec())
    }

    #[test]
    fn order_is_own_policy_route_id_length() {
        let out = final_headers(
            vec![
                h("content-type", "application/json"),
                h("X-Frame-Options", "bad"),
            ],
            &[h("x-frame-options", "DENY")],
            &[
                ("x-route".into(), "1".into()),
                ("content-type".into(), "x".into()),
            ],
            (b"x-request-id", b"abc"),
            &[h("vary", "origin")],
            200,
            7,
        );
        assert_eq!(
            out,
            vec![
                h("content-type", "application/json"),
                h("x-frame-options", "DENY"),
                h("x-route", "1"),
                h("x-request-id", "abc"),
                h("vary", "origin"),
                h("content-length", "7"),
            ]
        );
    }

    #[test]
    fn own_request_id_is_replaced_and_bodyless_has_no_length() {
        let out = final_headers(
            vec![h("X-Request-Id", "old")],
            &[],
            &[],
            (b"x-request-id", b"new"),
            &[],
            204,
            0,
        );
        assert_eq!(out, vec![h("x-request-id", "new")]);
    }

    /// The algorithm before it stopped allocating a lower-case copy per name.
    fn reference(
        own: &[Header],
        policy: &[Header],
        route: &[(String, String)],
        (request_id_name, request_id): (&[u8], &[u8]),
        extra: &[Header],
    ) -> Vec<Header> {
        let mut present: Vec<Vec<u8>> = Vec::new();
        let mut out: Vec<Header> = Vec::new();
        for (name, value) in own {
            let lower = name.to_ascii_lowercase();
            if is_policy(&lower) {
                continue;
            }
            if lower != request_id_name {
                out.push((name.clone(), value.clone()));
            }
            present.push(lower);
        }
        for (name, value) in policy {
            present.push(name.to_ascii_lowercase());
            out.push((name.clone(), value.clone()));
        }
        for (name, value) in route {
            let lower = name.as_bytes().to_ascii_lowercase();
            if lower == request_id_name || is_policy(&lower) || present.contains(&lower) {
                continue;
            }
            out.push((name.as_bytes().to_vec(), value.as_bytes().to_vec()));
        }
        out.push((request_id_name.to_vec(), request_id.to_vec()));
        out.extend(extra.iter().cloned());
        out
    }

    fn route_of(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs
            .iter()
            .map(|(n, v)| ((*n).to_owned(), (*v).to_owned()))
            .collect()
    }

    #[test]
    fn head_headers_matches_the_allocating_algorithm() {
        let own_sets: Vec<Vec<Header>> = vec![
            vec![],
            vec![h("Content-Type", "a"), h("content-type", "b")],
            vec![
                h("X-Frame-Options", "bad"),
                h("X-Request-ID", "old"),
                h("X-A", "1"),
            ],
            vec![h("x-request-id", "o"), h("Vary", "x"), h("VARY", "y")],
        ];
        let policy_sets: Vec<Vec<Header>> = vec![
            vec![],
            vec![h("x-frame-options", "DENY"), h("referrer-policy", "r")],
            vec![h("x-frame-options", "DENY"), h("x-a", "policy")],
        ];
        let routes = route_of(&[
            ("X-A", "r"),
            ("x-route", "1"),
            ("X-Route", "2"),
            ("Vary", "r"),
            ("X-Request-Id", "r"),
            ("Referrer-Policy", "r"),
            ("Strict-Transport-Security", "r"),
        ]);
        let extra = vec![h("access-control-allow-origin", "*")];
        for rid_name in [&b"x-request-id"[..], b"X-Request-ID"] {
            for own in &own_sets {
                for policy in &policy_sets {
                    for route in [&routes[..], &[]] {
                        let id = (rid_name, &b"new"[..]);
                        let want = reference(own, policy, route, id, &extra);
                        let got = head_headers(own.clone(), policy, route, id, &extra);
                        assert_eq!(got, want, "own={own:?} policy={policy:?} rid={rid_name:?}");
                        let with_length =
                            final_headers(own.clone(), policy, route, id, &extra, 200, 5);
                        let mut want = want;
                        want.push(h("content-length", "5"));
                        assert_eq!(with_length, want);
                    }
                }
            }
        }
    }

    #[test]
    fn route_header_yields_to_own_and_policy_names_case_insensitively() {
        let out = head_headers(
            vec![h("X-Own", "1")],
            &[h("x-policy", "p")],
            &route_of(&[
                ("x-own", "r"),
                ("X-POLICY", "r"),
                ("x-new", "n"),
                ("X-New", "m"),
            ]),
            (b"x-request-id", b"i"),
            &[],
        );
        assert_eq!(
            out,
            vec![
                h("X-Own", "1"),
                h("x-policy", "p"),
                h("x-new", "n"),
                h("X-New", "m"),
                h("x-request-id", "i"),
            ]
        );
    }

    #[test]
    fn final_json_header_map_sets_json_content_type_length_and_policy_headers() {
        let policy = compile_headers(&[
            h("x-frame-options", "DENY"),
            h("content-type", "text/plain"),
        ])
        .unwrap();
        let id_name = HeaderName::from_static("x-request-id");
        let map = final_json_header_map(&policy, &id_name, b"req-42", 200, 123).unwrap();
        assert_eq!(map.get("content-type").unwrap(), "application/json");
        assert_eq!(map.get("x-frame-options").unwrap(), "DENY");
        assert_eq!(map.get("x-request-id").unwrap(), "req-42");
        assert_eq!(map.get("content-length").unwrap(), "123");

        let no_body = final_json_header_map(&policy, &id_name, b"", 204, 0).unwrap();
        assert!(no_body.get("content-length").is_none());
        assert_eq!(no_body.get("x-request-id").unwrap(), "");
    }
}
