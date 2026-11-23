//! Method-preserving HTTPS redirects for the configured host allow-list.

const PATH_SAFE: &[u8] = b"/%:@!$&'()*+,;=-._~";
const QUERY_SAFE: &[u8] = b"!$&'()*+,-./:;=?@_%~";

/// `None` for an unsafe host, and for a target that is not a path (`OPTIONS *`).
pub fn location(host: &[u8], path: &[u8], query: &[u8]) -> Option<Vec<u8>> {
    if !safe_host(host) || path.first().is_some_and(|byte| *byte != b'/') {
        return None;
    }
    let host = strip_http_port(host);
    let mut target = b"https://".to_vec();
    target.extend_from_slice(host);
    let path = if path.is_empty() { b"/" } else { path };
    target.extend_from_slice(&quote(path, PATH_SAFE));
    if !query.is_empty() {
        target.push(b'?');
        target.extend_from_slice(&quote(query, QUERY_SAFE));
    }
    Some(target)
}

pub fn safe_host(host: &[u8]) -> bool {
    !host.is_empty() && host.iter().all(|byte| (0x21..0x7f).contains(byte))
}

fn strip_http_port(host: &[u8]) -> &[u8] {
    host.strip_suffix(b":80").unwrap_or(host)
}

fn quote(value: &[u8], safe: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(value.len());
    for byte in value {
        if byte.is_ascii_alphanumeric() || safe.contains(byte) {
            out.push(*byte);
        } else {
            out.extend_from_slice(format!("%{byte:02X}").as_bytes());
        }
    }
    out
}
