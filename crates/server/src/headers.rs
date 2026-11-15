//! The final header list of an answer, in the order the Python runtime builds
//! it (`core.response_headers`, then `content-length`).

/// Header names the framework's security policy owns (lowercase). Mirrors
/// `hinoki.runtime.security.POLICY_HEADERS`.
pub const POLICY_NAMES: [&[u8]; 4] = [
    b"x-frame-options",
    b"referrer-policy",
    b"x-content-type-options",
    b"strict-transport-security",
];

pub type Header = (Vec<u8>, Vec<u8>);

pub fn is_policy(lower: &[u8]) -> bool {
    POLICY_NAMES.contains(&lower)
}

/// Statuses that carry no `content-length` (`BODYLESS` in the Python runtime).
pub const fn is_bodyless(status: u16) -> bool {
    status == 204 || status == 304
}

/// The answer's own headers without policy headers and the request id, then the
/// policy headers, the route headers nothing above already set, the request id,
/// and `content-length` unless the status is bodyless.
pub fn final_headers(
    own: &[Header],
    policy: &[Header],
    route: &[(String, String)],
    request_id_name: &[u8],
    request_id: &[u8],
    status: u16,
    body_len: usize,
) -> Vec<Header> {
    let mut present: Vec<Vec<u8>> = Vec::with_capacity(own.len() + policy.len());
    let mut out: Vec<Header> = Vec::with_capacity(own.len() + policy.len() + route.len() + 2);
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
    if !is_bodyless(status) {
        out.push((
            b"content-length".to_vec(),
            body_len.to_string().into_bytes(),
        ));
    }
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
            &[
                h("content-type", "application/json"),
                h("X-Frame-Options", "bad"),
            ],
            &[h("x-frame-options", "DENY")],
            &[
                ("x-route".into(), "1".into()),
                ("content-type".into(), "x".into()),
            ],
            b"x-request-id",
            b"abc",
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
                h("content-length", "7"),
            ]
        );
    }

    #[test]
    fn own_request_id_is_replaced_and_bodyless_has_no_length() {
        let out = final_headers(
            &[h("X-Request-Id", "old")],
            &[],
            &[],
            b"x-request-id",
            b"new",
            204,
            0,
        );
        assert_eq!(out, vec![h("x-request-id", "new")]);
    }
}
