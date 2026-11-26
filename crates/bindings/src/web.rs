//! HTTP header/cookie validation, path/scalar parameter decoding, multipart
//! boundary extraction, browser origin checks, and pagination parameter
//! validation — moved from Python into Rust for parity and zero-regex speed.

use pyo3::exceptions::{PyUnicodeEncodeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyInt, PyList};

use crate::cursor::MAX_TOKEN_CHARS;

const MAX_PAGE_DIGITS: usize = 18;
const MAX_PAGE_NUMBER: i64 = 1_000_000_000;

fn is_token_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric()
        || matches!(
            b,
            b'!' | b'#'
                | b'$'
                | b'%'
                | b'&'
                | b'\''
                | b'*'
                | b'+'
                | b'-'
                | b'.'
                | b'^'
                | b'_'
                | b'`'
                | b'|'
                | b'~'
        )
}

fn is_http_token(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(is_token_byte)
}

fn is_cookie_value(s: &str) -> bool {
    s.bytes().all(|b| {
        b == 0x21
            || (0x23..=0x2b).contains(&b)
            || (0x2d..=0x3a).contains(&b)
            || (0x3c..=0x5b).contains(&b)
            || (0x5d..=0x7e).contains(&b)
    })
}

fn is_reserved_header(lowered: &str) -> bool {
    matches!(
        lowered,
        "content-length" | "transfer-encoding" | "connection" | "x-request-id"
    )
}

fn has_control_char(value: &str) -> bool {
    value.chars().any(|c| {
        let cp = c as u32;
        cp <= 0x08 || (0x0a..=0x1f).contains(&cp) || cp == 0x7f
    })
}

fn to_latin1(value: &str) -> Option<Vec<u8>> {
    if value.is_ascii() {
        return Some(value.as_bytes().to_vec());
    }
    let mut out = Vec::with_capacity(value.len());
    for c in value.chars() {
        let cp = c as u32;
        if cp > 0xff {
            return None;
        }
        out.push(cp as u8);
    }
    Some(out)
}

fn encode_header_inner(name: &str, value: &str) -> Result<(Vec<u8>, Vec<u8>), String> {
    if !is_http_token(name) {
        return Err(format!("invalid header name '{name}'"));
    }
    let lowered = name.to_ascii_lowercase();
    if is_reserved_header(&lowered) {
        return Err(format!("header '{name}' is set by the server"));
    }
    if has_control_char(value) {
        return Err(format!(
            "invalid characters in the value of header '{name}'"
        ));
    }
    let Some(encoded_val) = to_latin1(value) else {
        return Err(format!("header '{name}' must be Latin-1"));
    };
    Ok((lowered.into_bytes(), encoded_val))
}

type EncodedHeader<'py> = (Bound<'py, PyBytes>, Bound<'py, PyBytes>);

#[pyfunction]
pub fn encode_header<'py>(
    py: Python<'py>,
    name: &str,
    value: &str,
) -> PyResult<EncodedHeader<'py>> {
    let (k, v) = encode_header_inner(name, value).map_err(PyValueError::new_err)?;
    Ok((PyBytes::new(py, &k), PyBytes::new(py, &v)))
}

fn extract_header_str(
    py: Python<'_>,
    obj: &Bound<'_, PyAny>,
    err_msg: impl FnOnce() -> String,
) -> PyResult<Result<String, String>> {
    match obj.extract::<String>() {
        Ok(s) => Ok(Ok(s)),
        Err(err) if err.is_instance_of::<PyUnicodeEncodeError>(py) => Ok(Err(err_msg())),
        Err(err) => Err(err),
    }
}

#[pyfunction]
pub fn checked_headers<'py>(
    py: Python<'py>,
    headers: &Bound<'py, PyDict>,
) -> PyResult<(Vec<EncodedHeader<'py>>, Vec<String>)> {
    let mut kept = Vec::with_capacity(headers.len());
    let mut dropped = Vec::new();
    for (k, v) in headers.iter() {
        let name = match extract_header_str(py, &k, || "invalid header name".to_owned())? {
            Ok(s) => s,
            Err(msg) => {
                dropped.push(msg);
                continue;
            }
        };
        let value = match extract_header_str(py, &v, || format!("header '{name}' must be Latin-1"))?
        {
            Ok(s) => s,
            Err(msg) => {
                dropped.push(msg);
                continue;
            }
        };
        match encode_header_inner(&name, &value) {
            Ok((ek, ev)) => kept.push((PyBytes::new(py, &ek), PyBytes::new(py, &ev))),
            Err(problem) => dropped.push(problem),
        }
    }
    Ok((kept, dropped))
}

#[pyfunction]
#[pyo3(signature = (name, value, path="/", max_age=None, http_only=true, secure=true, same_site="Lax"))]
pub fn set_cookie_header(
    name: &str,
    value: &str,
    path: &str,
    max_age: Option<i64>,
    http_only: bool,
    secure: bool,
    same_site: &str,
) -> PyResult<(&'static str, String)> {
    if !is_http_token(name) || !is_cookie_value(value) {
        return Err(PyValueError::new_err(
            "cookie name or value contains forbidden characters",
        ));
    }
    let valid_site = matches!(same_site, "Strict" | "Lax" | "None");
    if !valid_site || (same_site == "None" && !secure) {
        return Err(PyValueError::new_err(
            "same_site must be Strict, Lax, or None together with secure",
        ));
    }
    if !path.starts_with('/') || path.bytes().any(|b| matches!(b, b';' | b'\r' | b'\n')) {
        return Err(PyValueError::new_err("invalid cookie path"));
    }
    let mut out = format!("{name}={value}; Path={path}; SameSite={same_site}");
    if let Some(age) = max_age {
        out.push_str(&format!("; Max-Age={age}"));
    }
    if http_only {
        out.push_str("; HttpOnly");
    }
    if secure {
        out.push_str("; Secure");
    }
    Ok(("set-cookie", out))
}

#[pyfunction]
pub fn decode_path_params<'py>(
    py: Python<'py>,
    params: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyDict>> {
    let out = PyDict::new(py);
    for (k, v) in params.iter() {
        let raw: String = v.extract()?;
        let Some(decoded) = crate::request::decode_path_parameter(&raw) else {
            return Err(PyValueError::new_err("bad_path_encoding"));
        };
        out.set_item(k, decoded)?;
    }
    Ok(out)
}

#[pyfunction]
pub fn parse_bool(raw: &str) -> PyResult<bool> {
    match raw {
        "true" | "1" => Ok(true),
        "false" | "0" => Ok(false),
        _ => Err(PyValueError::new_err(raw.to_owned())),
    }
}

fn is_integer_text(raw: &str) -> bool {
    let digits = raw.strip_prefix('-').unwrap_or(raw);
    !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit())
}

#[pyfunction]
pub fn parse_int(py: Python<'_>, raw: &str) -> PyResult<Py<PyAny>> {
    if !is_integer_text(raw) {
        return Err(PyValueError::new_err(raw.to_owned()));
    }
    if let Ok(n) = raw.parse::<i64>() {
        return Ok(n.into_pyobject(py)?.into_any().unbind());
    }
    Ok(py.get_type::<PyInt>().call1((raw,))?.unbind())
}

fn take_digits(bytes: &[u8], i: &mut usize) -> bool {
    let start = *i;
    while *i < bytes.len() && bytes[*i].is_ascii_digit() {
        *i += 1;
    }
    *i > start
}

fn is_decimal_text(raw: &str) -> bool {
    let bytes = raw.as_bytes();
    let mut i = usize::from(bytes.first() == Some(&b'-'));
    if !take_digits(bytes, &mut i) {
        return false;
    }
    if bytes.get(i) == Some(&b'.') {
        i += 1;
        if !take_digits(bytes, &mut i) {
            return false;
        }
    }
    if matches!(bytes.get(i), Some(b'e' | b'E')) {
        i += 1;
        if matches!(bytes.get(i), Some(b'+' | b'-')) {
            i += 1;
        }
        if !take_digits(bytes, &mut i) {
            return false;
        }
    }
    i == bytes.len()
}

#[pyfunction]
pub fn parse_float(raw: &str) -> PyResult<f64> {
    if !is_decimal_text(raw) {
        return Err(PyValueError::new_err(raw.to_owned()));
    }
    let value = raw
        .parse::<f64>()
        .map_err(|_| PyValueError::new_err(raw.to_owned()))?;
    if !value.is_finite() {
        return Err(PyValueError::new_err(raw.to_owned()));
    }
    Ok(value)
}

fn is_canonical_uuid_text(raw: &str) -> bool {
    let bytes = raw.as_bytes();
    if bytes.len() != 36 {
        return false;
    }
    bytes.iter().enumerate().all(|(idx, &b)| {
        if matches!(idx, 8 | 13 | 18 | 23) {
            b == b'-'
        } else {
            b.is_ascii_hexdigit()
        }
    })
}

#[pyfunction]
pub fn parse_uuid<'py>(py: Python<'py>, raw: &str) -> PyResult<Bound<'py, PyAny>> {
    if !is_canonical_uuid_text(raw) {
        return Err(PyValueError::new_err(raw.to_owned()));
    }
    let parsed = uuid::Uuid::parse_str(raw).map_err(|_| PyValueError::new_err(raw.to_owned()))?;
    crate::pytypes::new_uuid(py, parsed)
}

fn is_py_whitespace(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\r' | b'\n' | 0x0b | 0x0c)
}

/// Extract the `boundary` parameter from a `Content-Type` header (quoted or
/// unquoted), matching `r';\s*boundary=(?:"([^"]*)"|([^;\s]*))'` (case-insensitive).
#[pyfunction]
pub fn extract_boundary(content_type: &str) -> String {
    let bytes = content_type.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] != b';' {
            i += 1;
            continue;
        }
        i += 1;
        while i < bytes.len() && is_py_whitespace(bytes[i]) {
            i += 1;
        }
        if bytes.len().saturating_sub(i) < 9 || !bytes[i..i + 9].eq_ignore_ascii_case(b"boundary=")
        {
            continue;
        }
        let rest = &content_type[i + 9..];
        if let Some(after_quote) = rest.strip_prefix('"') {
            if let Some(end) = after_quote.find('"') {
                return after_quote[..end].to_owned();
            }
        }
        let end = rest
            .as_bytes()
            .iter()
            .position(|&b| b == b';' || is_py_whitespace(b))
            .unwrap_or(rest.len());
        return rest[..end].to_owned();
    }
    String::new()
}

fn default_port(scheme: &str) -> Option<u16> {
    if scheme.eq_ignore_ascii_case("http") {
        Some(80)
    } else if scheme.eq_ignore_ascii_case("https") {
        Some(443)
    } else {
        None
    }
}

fn parse_authority(authority: &str, default: u16) -> Option<(String, u16)> {
    if authority.is_empty() || authority.contains('@') {
        return None;
    }
    if let Some(rest) = authority.strip_prefix('[') {
        let close = rest.find(']')?;
        let host = &rest[..close];
        if host.is_empty() {
            return None;
        }
        let after = &rest[close + 1..];
        let port = parse_optional_port(after, default)?;
        return Some((host.to_ascii_lowercase(), port));
    }
    let (host, after) = match authority.find(':') {
        Some(idx) => (&authority[..idx], &authority[idx..]),
        None => (authority, ""),
    };
    if host.is_empty() || host.contains(['[', ']']) {
        return None;
    }
    let port = parse_optional_port(after, default)?;
    Some((host.to_ascii_lowercase(), port))
}

fn parse_optional_port(after: &str, default: u16) -> Option<u16> {
    if after.is_empty() {
        return Some(default);
    }
    let digits = after.strip_prefix(':')?;
    if digits.is_empty() {
        return Some(default);
    }
    if !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    digits.parse::<u16>().ok()
}

#[pyfunction]
pub fn same_site_origin(origin: Option<&str>, fetch_site: &str, host: &str, scheme: &str) -> bool {
    if matches!(fetch_site, "cross-site" | "same-site") || origin == Some("null") {
        return false;
    }
    let Some(origin) = origin else {
        return true;
    };
    let Some(port) = default_port(scheme) else {
        return false;
    };
    if host.contains(['/', '?', '#']) {
        return false;
    }
    let Some((orig_scheme, rest)) = origin.split_once("://") else {
        return false;
    };
    if !orig_scheme.eq_ignore_ascii_case(scheme) || rest.contains(['?', '#']) {
        return false;
    }
    let orig_authority = rest.strip_suffix('/').unwrap_or(rest);
    if orig_authority.contains('/') {
        return false;
    }
    let Some(a) = parse_authority(orig_authority, port) else {
        return false;
    };
    let Some(b) = parse_authority(host, port) else {
        return false;
    };
    a == b
}

/// The WebSocket origin rule both lanes share. An absent (empty) `Origin` is not
/// a browser and passes. Otherwise the origin must be one the route lists (`*`
/// opens the route), one the application's CORS policy lists by name (a CORS
/// `*` is never honoured: a handshake carries cookies and a browser does not
/// hide its answer), or exactly the scheme the connection has (`https` for TLS
/// or a trusted proxy reporting it, otherwise `http`) followed by the request
/// `Host`. Comparison is ASCII case-insensitive; the lists are lower case.
pub fn websocket_origin_allowed(
    origin: &[u8],
    host: Option<&[u8]>,
    https: bool,
    route_origins: &[Vec<u8>],
    cors_origins: &[Vec<u8>],
) -> bool {
    if origin.is_empty() {
        return true;
    }
    let origin = origin.to_ascii_lowercase();
    if route_origins.iter().any(|o| o == b"*" || *o == origin)
        || cors_origins.iter().any(|o| o != b"*" && *o == origin)
    {
        return true;
    }
    let scheme: &[u8] = if https { b"https://" } else { b"http://" };
    match (host, origin.strip_prefix(scheme)) {
        (Some(host), Some(authority)) => !host.is_empty() && host.eq_ignore_ascii_case(authority),
        _ => false,
    }
}

#[pyfunction]
#[pyo3(name = "websocket_origin_allowed")]
pub fn py_websocket_origin_allowed(
    origin: Option<&str>,
    host: Option<&str>,
    https: bool,
    route_origins: Vec<String>,
    cors_origins: Vec<String>,
) -> bool {
    let bytes = |items: Vec<String>| -> Vec<Vec<u8>> {
        items.into_iter().map(String::into_bytes).collect()
    };
    websocket_origin_allowed(
        origin.unwrap_or("").as_bytes(),
        host.map(str::as_bytes),
        https,
        &bytes(route_origins),
        &bytes(cors_origins),
    )
}

type ParamError = (&'static str, &'static str, String);

fn parse_positive_param(
    query: &Bound<'_, PyDict>,
    name: &'static str,
    default: i64,
    upper: Option<i64>,
    errors: &mut Vec<ParamError>,
) -> PyResult<i64> {
    let Some(entry) = query.get_item(name)? else {
        return Ok(default);
    };
    let values = entry.cast::<PyList>()?;
    let limit = upper.unwrap_or(MAX_PAGE_NUMBER);
    if values.len() != 1 {
        errors.push((name, "type", "expected a positive integer".to_owned()));
        return Ok(default);
    }
    let raw: String = values.get_item(0)?.extract()?;
    let digits = !raw.is_empty() && raw.bytes().all(|b| b.is_ascii_digit());
    let short = digits && raw.len() <= MAX_PAGE_DIGITS;
    let parsed = if short { raw.parse::<i64>().ok() } else { None };
    if !digits || parsed.is_some_and(|v| v < 1) {
        errors.push((name, "type", "expected a positive integer".to_owned()));
        Ok(default)
    } else if !short || parsed.is_none_or(|v| v > limit) {
        errors.push((name, "le", format!("must be at most {limit}")));
        Ok(default)
    } else {
        Ok(parsed.unwrap_or(default))
    }
}

#[pyfunction]
pub fn parse_page_params(
    query: &Bound<'_, PyDict>,
    default_size: i64,
    upper: i64,
) -> PyResult<(i64, i64, Vec<ParamError>)> {
    let mut errors = Vec::new();
    let page = parse_positive_param(query, "page", 1, None, &mut errors)?;
    let size = parse_positive_param(query, "page_size", default_size, Some(upper), &mut errors)?;
    Ok((page, size, errors))
}

#[pyfunction]
pub fn parse_cursor_params(
    query: &Bound<'_, PyDict>,
    default_size: i64,
    upper: i64,
) -> PyResult<(i64, Option<String>, Vec<ParamError>)> {
    let mut errors = Vec::new();
    let size = parse_positive_param(query, "limit", default_size, Some(upper), &mut errors)?;
    let mut token = None;
    if let Some(entry) = query.get_item("cursor")? {
        let values = entry.cast::<PyList>()?;
        let single = if values.len() == 1 {
            Some(values.get_item(0)?.extract::<String>()?)
        } else {
            None
        };
        match single {
            Some(raw) if !raw.is_empty() && raw.len() <= MAX_TOKEN_CHARS => token = Some(raw),
            _ => errors.push(("cursor", "invalid", "invalid cursor".to_owned())),
        }
    }
    Ok((size, token, errors))
}

#[pyfunction]
pub fn parse_dotenv(text: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for raw in text.split(['\n', '\r']) {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') || !line.contains('=') {
            continue;
        }
        let stripped = line.strip_prefix("export ").map_or(line, str::trim);
        let Some((key, val)) = stripped.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let val = val.trim();
        let cleaned = if val.len() >= 2
            && ((val.starts_with('"') && val.ends_with('"'))
                || (val.starts_with('\'') && val.ends_with('\'')))
        {
            &val[1..val.len() - 1]
        } else {
            val.split_once(" #")
                .map_or(val, |(head, _)| head)
                .trim_end()
        };
        out.push((key.to_owned(), cleaned.to_owned()));
    }
    out
}

fn is_host_label(label: &str) -> bool {
    let bytes = label.as_bytes();
    if bytes.is_empty() || bytes.len() > 63 {
        return false;
    }
    let ok_edge = |b: u8| b.is_ascii_alphanumeric() || b == b'_';
    let ok_mid = |b: u8| ok_edge(b) || b == b'-';
    ok_edge(bytes[0]) && ok_edge(bytes[bytes.len() - 1]) && bytes.iter().all(|&b| ok_mid(b))
}

#[pyfunction]
pub fn is_host_pattern(entry: &str) -> bool {
    if entry == "*" {
        return true;
    }
    if entry.contains(':') {
        if !entry.is_ascii() {
            return false;
        }
        let addr = entry.split_once('%').map_or(entry, |(ip, zone)| {
            if zone.is_empty() || zone.contains(['%', '/']) {
                ""
            } else {
                ip
            }
        });
        return addr.parse::<std::net::Ipv6Addr>().is_ok();
    }
    let host = entry.strip_prefix('.').unwrap_or(entry);
    !host.is_empty() && host.split('.').all(is_host_label)
}

fn is_origin_scheme(scheme: &str) -> bool {
    let bytes = scheme.as_bytes();
    !bytes.is_empty()
        && bytes[0].is_ascii_alphabetic()
        && bytes
            .iter()
            .all(|&b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'.' | b'-'))
}

fn is_origin_port(port: &str) -> bool {
    if port.is_empty() {
        return true;
    }
    let Some(digits) = port.strip_prefix(':') else {
        return false;
    };
    !digits.is_empty() && digits.len() <= 5 && digits.bytes().all(|b| b.is_ascii_digit())
}

#[pyfunction]
pub fn is_valid_origin(entry: &str) -> bool {
    let Some((scheme, rest)) = entry.split_once("://") else {
        return false;
    };
    if !is_origin_scheme(scheme) {
        return false;
    }
    if let Some(after_open) = rest.strip_prefix('[') {
        let Some((ip, port)) = after_open.split_once(']') else {
            return false;
        };
        return !ip.is_empty()
            && ip
                .bytes()
                .all(|b| b.is_ascii_hexdigit() || b == b':' || b == b'.')
            && is_origin_port(port);
    }
    let (host, port) = match rest.find(':') {
        Some(idx) => (&rest[..idx], &rest[idx..]),
        None => (rest, ""),
    };
    !host.is_empty()
        && host
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
        && is_origin_port(port)
}

#[pyfunction]
pub fn is_http_token_str(entry: &str) -> bool {
    is_http_token(entry)
}

fn is_media_rule_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric()
        || matches!(
            b,
            b'!' | b'#' | b'$' | b'&' | b'^' | b'_' | b'.' | b'+' | b'-'
        )
}

#[pyfunction]
pub fn is_media_rule(rule: &str) -> bool {
    if let Some(suffix) = rule.strip_prefix("*/*+") {
        return !suffix.is_empty()
            && suffix
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'+' | b'-'));
    }
    let Some((kind, sub)) = rule.split_once('/') else {
        return false;
    };
    if kind.is_empty() || !kind.bytes().all(is_media_rule_byte) {
        return false;
    }
    sub == "*" || (!sub.is_empty() && sub.bytes().all(is_media_rule_byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_and_validates_headers() {
        let (k, v) = encode_header_inner("X-Test", "hello").unwrap();
        assert_eq!(k, b"x-test");
        assert_eq!(v, b"hello");
        assert!(encode_header_inner("Content-Length", "5").is_err());
        assert!(encode_header_inner("Bad Name", "x").is_err());
        assert!(encode_header_inner("X-Test", "a\r\nb").is_err());
        assert!(encode_header_inner("X-Test", "é").is_ok());
        assert!(encode_header_inner("X-Test", "😀").is_err());
    }

    #[test]
    fn extracts_multipart_boundary() {
        assert_eq!(
            extract_boundary("multipart/form-data; boundary=abc123"),
            "abc123"
        );
        assert_eq!(
            extract_boundary("multipart/form-data; BOUNDARY=\"a;b\""),
            "a;b"
        );
        assert_eq!(extract_boundary("application/json"), "");
    }

    #[test]
    fn websocket_origin_follows_the_connection_scheme() {
        let none: [Vec<u8>; 0] = [];
        let allowed = |origin: &str, host: &str, https: bool| {
            websocket_origin_allowed(
                origin.as_bytes(),
                Some(host.as_bytes()),
                https,
                &none,
                &none,
            )
        };
        assert!(allowed("http://app", "app", false));
        assert!(!allowed("https://app", "app", false));
        assert!(allowed("https://app", "app", true));
        assert!(!allowed("http://app", "app", true));
        assert!(allowed("HTTPS://App:8443", "app:8443", true));
        assert!(!allowed("https://app", "app:443", true));
        assert!(!allowed("https://app.", "app", true));
        assert!(!allowed("https://app", "", true));
        assert!(websocket_origin_allowed(b"", None, true, &none, &none));
        let star = [b"*".to_vec()];
        assert!(!websocket_origin_allowed(
            b"https://x",
            None,
            true,
            &none,
            &star
        ));
        assert!(websocket_origin_allowed(
            b"https://x",
            None,
            true,
            &star,
            &none
        ));
    }

    #[test]
    fn validates_same_site_origin() {
        assert!(same_site_origin(
            None,
            "same-origin",
            "example.com",
            "https"
        ));
        assert!(!same_site_origin(
            None,
            "cross-site",
            "example.com",
            "https"
        ));
        assert!(!same_site_origin(
            Some("null"),
            "same-origin",
            "example.com",
            "https"
        ));
        assert!(same_site_origin(
            Some("https://Example.COM:443"),
            "same-origin",
            "example.com",
            "https"
        ));
        assert!(same_site_origin(
            Some("http://[::1]:80/"),
            "same-origin",
            "[::1]",
            "http"
        ));
        assert!(!same_site_origin(
            Some("https://example.com/path"),
            "same-origin",
            "example.com",
            "https"
        ));
    }

    #[test]
    fn parses_decimal_and_uuid_text() {
        assert!(is_decimal_text("-12.34e+5"));
        assert!(!is_decimal_text("12."));
        assert!(!is_decimal_text("+1"));
        assert!(is_canonical_uuid_text(
            "01234567-89ab-cdef-0123-456789abcdef"
        ));
        assert!(!is_canonical_uuid_text("0123456789abcdef0123456789abcdef"));
    }
}
