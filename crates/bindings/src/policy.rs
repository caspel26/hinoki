//! PyO3 bindings for pure request-path policy evaluators backed by `kiso`.
//!
//! Exposes pre-configured `#[pyclass]` evaluators (`ProxyPolicy`, `HostPolicy`,
//! `CorsPolicy`) built once at `Pipeline` startup so per-request checks in
//! Granian / ASGI share the exact same Rust logic as the native Kiso server
//! without re-converting policy tables on every request.

use std::net::IpAddr;

use kiso::cors::{Cors, PreflightError};
use kiso::headers::Header;
use kiso::hosts::{self, Hosts};
use kiso::proxy::{parse_forwarded_proto, Forwarded, Networks};
use kiso::{redirect, requestid};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

type PyHeader<'py> = (Bound<'py, PyBytes>, Bound<'py, PyBytes>);
type ProxyResolution = (
    bool,
    Option<&'static str>,
    Option<String>,
    Option<&'static str>,
    Option<&'static str>,
);

/// Extract a `(name, value)` byte pair from an ASGI header entry (tuple or list).
fn byte_pair<'py>(
    item: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyBytes>, Bound<'py, PyBytes>)> {
    item.extract()
}

fn to_py_headers<'py>(py: Python<'py>, headers: Vec<Header>) -> Vec<PyHeader<'py>> {
    headers
        .into_iter()
        .map(|(name, value)| (PyBytes::new(py, &name), PyBytes::new(py, &value)))
        .collect()
}

fn forwarded_warning<T>(outcome: &Forwarded<T>) -> Option<&'static str> {
    match outcome {
        Forwarded::Repeated => Some("repeated"),
        Forwarded::Invalid => Some("invalid"),
        Forwarded::Missing | Forwarded::Valid(_) => None,
    }
}

/// Pre-parsed trusted proxy networks, built once at `Pipeline` startup.
#[pyclass(frozen, name = "ProxyPolicy", module = "hinoki.core")]
pub struct PyProxyPolicy {
    networks: Networks,
}

#[pymethods]
impl PyProxyPolicy {
    #[new]
    fn new(networks: Vec<String>) -> PyResult<Self> {
        let parsed = Networks::parse(&networks).map_err(PyValueError::new_err)?;
        Ok(Self { networks: parsed })
    }

    fn is_empty(&self) -> bool {
        self.networks.is_empty()
    }

    /// Whether `peer` parses as an IP address belonging to a trusted network.
    fn trusts(&self, peer: &str) -> bool {
        self.networks.contains_str(peer)
    }

    /// Walk one `X-Forwarded-For` header value from `peer` toward the client.
    fn forwarded_client(&self, raw: &[u8], peer: &str) -> Option<String> {
        let peer_ip: IpAddr = peer.parse().ok()?;
        self.networks
            .forwarded_client(raw, peer_ip)
            .map(|addr| addr.to_string())
    }

    /// Single-pass evaluation of `X-Forwarded-Proto` and `X-Forwarded-For` for a
    /// socket peer. Returns `(trusted, proto, client, proto_warn, for_warn)`.
    fn resolve(&self, peer: Option<&str>, headers: &Bound<'_, PyAny>) -> PyResult<ProxyResolution> {
        let Some(peer_ip) = self.trusted_peer_ip(peer) else {
            return Ok((false, None, None, None, None));
        };
        let mut proto_kept = Vec::new();
        let mut for_kept = Vec::new();
        for item in headers.try_iter()? {
            let (name, value) = byte_pair(&item?)?;
            let key = name.as_bytes();
            if key.eq_ignore_ascii_case(b"x-forwarded-proto") {
                proto_kept.push(value);
            } else if key.eq_ignore_ascii_case(b"x-forwarded-for") {
                for_kept.push(value);
            }
        }
        let proto_refs: Vec<&[u8]> = proto_kept.iter().map(|v| v.as_bytes()).collect();
        let for_refs: Vec<&[u8]> = for_kept.iter().map(|v| v.as_bytes()).collect();
        let proto_out = parse_forwarded_proto(&proto_refs);
        let for_out = self.networks.forwarded_address(&for_refs, peer_ip);
        let proto_warn = forwarded_warning(&proto_out);
        let for_warn = forwarded_warning(&for_out);
        let proto = match proto_out {
            Forwarded::Valid(value) => Some(value),
            _ => None,
        };
        let client = match for_out {
            Forwarded::Valid(addr) => Some(addr.to_string()),
            _ => None,
        };
        Ok((true, proto, client, proto_warn, for_warn))
    }

    /// Single-pass `X-Forwarded-For` resolution without inspecting `X-Forwarded-Proto`.
    /// Returns `(trusted, client, for_warn)`.
    fn resolve_client(
        &self,
        peer: Option<&str>,
        headers: &Bound<'_, PyAny>,
    ) -> PyResult<(bool, Option<String>, Option<&'static str>)> {
        let Some(peer_ip) = self.trusted_peer_ip(peer) else {
            return Ok((false, None, None));
        };
        let mut for_kept = Vec::new();
        for item in headers.try_iter()? {
            let (name, value) = byte_pair(&item?)?;
            if name.as_bytes().eq_ignore_ascii_case(b"x-forwarded-for") {
                for_kept.push(value);
            }
        }
        let for_refs: Vec<&[u8]> = for_kept.iter().map(|v| v.as_bytes()).collect();
        let for_out = self.networks.forwarded_address(&for_refs, peer_ip);
        let for_warn = forwarded_warning(&for_out);
        let client = match for_out {
            Forwarded::Valid(addr) => Some(addr.to_string()),
            _ => None,
        };
        Ok((true, client, for_warn))
    }
}

impl PyProxyPolicy {
    fn trusted_peer_ip(&self, peer: Option<&str>) -> Option<IpAddr> {
        if self.networks.is_empty() {
            return None;
        }
        let ip: IpAddr = peer?.parse().ok()?;
        self.networks.contains(ip).then_some(ip)
    }
}

/// Validate an inbound request ID header from raw ASGI headers.
/// Returns `(valid_id, saw_header)` from a single pass.
#[pyfunction]
pub fn inbound_request_id(
    headers: &Bound<'_, PyAny>,
    name: &[u8],
) -> PyResult<(Option<String>, bool)> {
    let mut kept = Vec::new();
    for item in headers.try_iter()? {
        let (key, value) = byte_pair(&item?)?;
        if key.as_bytes().eq_ignore_ascii_case(name) {
            kept.push(value);
        }
    }
    if kept.is_empty() {
        return Ok((None, false));
    }
    let refs: Vec<&[u8]> = kept.iter().map(|v| v.as_bytes()).collect();
    Ok((requestid::inbound(&refs).map(str::to_owned), true))
}

/// Normalize a `Host` header to its lowercase host name (without port, IPv6
/// brackets, or trailing dot), or `""` when invalid.
#[pyfunction]
pub fn host_name(value: &str) -> String {
    hosts::host_name(value.as_bytes())
        .and_then(|bytes| String::from_utf8(bytes).ok())
        .unwrap_or_default()
}

/// Pre-compiled host allowlist, built once at `Pipeline` startup.
#[pyclass(frozen, name = "HostPolicy", module = "hinoki.core")]
pub struct PyHostPolicy {
    hosts: Hosts,
}

#[pymethods]
impl PyHostPolicy {
    #[new]
    fn new(allowed_hosts: Vec<String>) -> Self {
        Self {
            hosts: Hosts::from_entries(&allowed_hosts),
        }
    }

    fn allows(&self, header: &str) -> bool {
        self.hosts.allows(header.as_bytes())
    }

    #[getter]
    fn exact(&self) -> Vec<String> {
        self.hosts
            .exact
            .iter()
            .filter_map(|b| String::from_utf8(b.clone()).ok())
            .collect()
    }

    #[getter]
    fn suffixes(&self) -> Vec<String> {
        self.hosts
            .suffixes
            .iter()
            .filter_map(|b| String::from_utf8(b.clone()).ok())
            .collect()
    }
}

/// Pre-compiled CORS policy, built once at `Pipeline` startup.
#[pyclass(frozen, name = "CorsPolicy", module = "hinoki.core")]
pub struct PyCorsPolicy {
    cors: Cors,
}

#[pymethods]
impl PyCorsPolicy {
    #[new]
    fn new(
        origins: Vec<String>,
        any_origin: bool,
        credentials: bool,
        expose: Vec<u8>,
        preflight: Vec<(Vec<u8>, Vec<u8>)>,
    ) -> Self {
        let lowered = origins
            .into_iter()
            .map(|origin| origin.to_ascii_lowercase().into_bytes())
            .collect();
        Self {
            cors: Cors {
                any_origin,
                origins: lowered,
                credentials,
                expose,
                preflight,
            },
        }
    }

    fn allowed(&self, origin: &str) -> bool {
        self.cors.allowed(origin.as_bytes())
    }

    fn headers<'py>(&self, py: Python<'py>, origin: &str) -> Vec<PyHeader<'py>> {
        to_py_headers(py, self.cors.headers(origin.as_bytes()))
    }

    /// Compute response CORS headers directly from raw ASGI scope headers,
    /// joining repeated `origin` headers with `", "` in one Rust pass.
    fn headers_from_scope<'py>(
        &self,
        py: Python<'py>,
        raw_headers: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<PyHeader<'py>>> {
        let mut origins = Vec::new();
        for item in raw_headers.try_iter()? {
            let (name, value) = byte_pair(&item?)?;
            if name.as_bytes() == b"origin" {
                origins.push(value);
            }
        }
        let joined: Vec<u8> = match origins.as_slice() {
            [] => Vec::new(),
            [single] => return Ok(to_py_headers(py, self.cors.headers(single.as_bytes()))),
            many => {
                let mut out = Vec::new();
                for (index, item) in many.iter().enumerate() {
                    if index > 0 {
                        out.extend_from_slice(b", ");
                    }
                    out.extend_from_slice(item.as_bytes());
                }
                out
            }
        };
        Ok(to_py_headers(py, self.cors.headers(&joined)))
    }

    /// Evaluate a CORS preflight request.
    /// Returns `(denied, preflight_headers)`.
    fn preflight<'py>(
        &self,
        py: Python<'py>,
        method: &str,
        origin: Option<&str>,
        requests_method: bool,
    ) -> (bool, Option<Vec<PyHeader<'py>>>) {
        match self
            .cors
            .preflight(method, origin.map(str::as_bytes), requests_method)
        {
            Ok(None) => (false, None),
            Ok(Some(headers)) => (false, Some(to_py_headers(py, headers))),
            Err(PreflightError::OriginDenied) => (true, None),
        }
    }
}

/// Build a canonical HTTPS redirect target URL, or `None` when the host or path
/// cannot be safely redirected.
#[pyfunction]
pub fn redirect_location<'py>(
    py: Python<'py>,
    host: &str,
    path: &[u8],
    query: &[u8],
) -> Option<Bound<'py, PyBytes>> {
    redirect::location(host.as_bytes(), path, query).map(|target| PyBytes::new(py, &target))
}

/// Whether `host` is a non-empty visible-ASCII value safe for a `Location` header.
#[pyfunction]
pub fn header_safe(host: &str) -> bool {
    redirect::safe_host(host.as_bytes())
}

/// Build the framework-owned security response headers for one effective scheme.
#[pyfunction]
pub fn security_headers<'py>(
    py: Python<'py>,
    frame_options: &str,
    referrer_policy: &str,
    hsts_seconds: u64,
    hsts_include_subdomains: bool,
    hsts_preload: bool,
    scheme: &str,
) -> Vec<PyHeader<'py>> {
    let mut headers = Vec::with_capacity(4);
    headers.push((
        PyBytes::new(py, b"x-frame-options"),
        PyBytes::new(py, frame_options.to_ascii_uppercase().as_bytes()),
    ));
    headers.push((
        PyBytes::new(py, b"referrer-policy"),
        PyBytes::new(py, referrer_policy.as_bytes()),
    ));
    headers.push((
        PyBytes::new(py, b"x-content-type-options"),
        PyBytes::new(py, b"nosniff"),
    ));
    if scheme == "https" && hsts_seconds > 0 {
        let mut value = format!("max-age={hsts_seconds}");
        if hsts_include_subdomains {
            value.push_str("; includeSubDomains");
        }
        if hsts_preload {
            value.push_str("; preload");
        }
        headers.push((
            PyBytes::new(py, b"strict-transport-security"),
            PyBytes::new(py, value.as_bytes()),
        ));
    }
    headers
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyProxyPolicy>()?;
    m.add_class::<PyHostPolicy>()?;
    m.add_class::<PyCorsPolicy>()?;
    m.add_function(wrap_pyfunction!(inbound_request_id, m)?)?;
    m.add_function(wrap_pyfunction!(host_name, m)?)?;
    m.add_function(wrap_pyfunction!(redirect_location, m)?)?;
    m.add_function(wrap_pyfunction!(header_safe, m)?)?;
    m.add_function(wrap_pyfunction!(security_headers, m)?)?;
    Ok(())
}
