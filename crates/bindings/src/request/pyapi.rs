use kiso::headers::is_policy;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

use super::parse::{join_headers, parse_cookies, parse_query};

#[pyfunction]
pub fn parse_query_string(py: Python<'_>, raw: &[u8], max_pairs: usize) -> PyResult<Py<PyDict>> {
    let pairs = parse_query(raw, max_pairs).map_err(PyValueError::new_err)?;
    let result = PyDict::new(py);
    for (name, value) in pairs {
        match result.get_item(&name)? {
            Some(list) => list.cast::<PyList>()?.append(value)?,
            None => result.set_item(name, PyList::new(py, [value])?)?,
        }
    }
    Ok(result.unbind())
}

#[pyfunction]
pub fn parse_cookie_header(py: Python<'_>, header: &str) -> PyResult<Py<PyDict>> {
    let result = PyDict::new(py);
    for (name, value) in parse_cookies(header) {
        result.set_item(name, value)?;
    }
    Ok(result.unbind())
}

#[pyfunction]
pub fn header_dict(py: Python<'_>, headers: &Bound<'_, PyAny>) -> PyResult<Py<PyDict>> {
    Ok(headers_and_size(py, headers)?.0)
}

/// The header dict of `header_dict` and the byte total of `header_bytes` from one
/// pass over the headers.
pub fn headers_and_size(
    py: Python<'_>,
    headers: &Bound<'_, PyAny>,
) -> PyResult<(Py<PyDict>, usize)> {
    let mut pairs = Vec::new();
    for item in headers.try_iter()? {
        pairs.push(byte_pair(&item?)?);
    }
    let pairs: Vec<(&[u8], &[u8])> = pairs
        .iter()
        .map(|(name, value)| (name.as_bytes(), value.as_bytes()))
        .collect();
    let size = pairs.iter().map(|(n, v)| n.len() + v.len() + 4).sum();
    Ok((joined_header_dict(py, &pairs)?.unbind(), size))
}

/// A header the dict can take as is: the name is lower-case ASCII and the
/// value ASCII, which is what hyper delivers.
fn plain_header<'a>(name: &'a [u8], value: &'a [u8]) -> Option<(&'a str, &'a str)> {
    if name.iter().any(u8::is_ascii_uppercase) || !name.is_ascii() || !value.is_ascii() {
        return None;
    }
    Some((
        std::str::from_utf8(name).ok()?,
        std::str::from_utf8(value).ok()?,
    ))
}

/// The dict `join_headers` describes: lower-case names, repeats joined (`"; "`
/// for `cookie`, `", "` otherwise) at the first occurrence. Plain headers go
/// straight into the dict; any other input takes the `join_headers` path, which
/// owns the Latin-1 and Unicode-lowercase rules.
pub fn joined_header_dict<'py, N: AsRef<[u8]>, V: AsRef<[u8]>>(
    py: Python<'py>,
    headers: &[(N, V)],
) -> PyResult<Bound<'py, PyDict>> {
    let result = PyDict::new(py);
    if !headers
        .iter()
        .all(|(n, v)| plain_header(n.as_ref(), v.as_ref()).is_some())
    {
        for (name, value) in join_headers(headers) {
            result.set_item(name, value)?;
        }
        return Ok(result);
    }
    for (name, value) in headers {
        let Some((name, value)) = plain_header(name.as_ref(), value.as_ref()) else {
            continue;
        };
        match result.get_item(name)? {
            Some(first) => {
                let separator = if name == "cookie" { "; " } else { ", " };
                result.set_item(
                    name,
                    format!("{}{separator}{value}", first.extract::<&str>()?),
                )?;
            }
            None => result.set_item(name, value)?,
        }
    }
    Ok(result)
}

/// A `(name, value)` pair of bytes; ASGI servers may send lists or tuples.
fn byte_pair<'py>(
    item: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyBytes>, Bound<'py, PyBytes>)> {
    if item.len()? != 2 {
        return Err(PyValueError::new_err(
            "a header must be a (name, value) pair",
        ));
    }
    Ok((
        item.get_item(0)?.cast_into::<PyBytes>()?,
        item.get_item(1)?.cast_into::<PyBytes>()?,
    ))
}

/// `(header dict, header bytes)` in one pass: the dict of `header_dict` and the
/// total the size limit counts (name + value + 4 per header).
#[pyfunction]
pub fn request_headers(
    py: Python<'_>,
    headers: &Bound<'_, PyAny>,
) -> PyResult<(Py<PyDict>, usize)> {
    headers_and_size(py, headers)
}

/// The final header list of a response: the handler's headers without the
/// framework's policy headers and request id, then the policy headers, the
/// route's own headers that nothing above already set, and the request id.
/// Returns the list and whether the handler's own headers carried
/// `allow_origin`, which decides whether CORS headers are added afterwards.
#[pyfunction]
pub fn response_headers<'py>(
    py: Python<'py>,
    headers: &Bound<'py, PyList>,
    policy: &Bound<'py, PyList>,
    route_headers: &Bound<'py, PyAny>,
    request_id_name: &Bound<'py, PyBytes>,
    request_id: &Bound<'py, PyBytes>,
    allow_origin: &[u8],
) -> PyResult<(Bound<'py, PyList>, bool)> {
    let rid_name = request_id_name.as_bytes();
    let mut present: Vec<Vec<u8>> = Vec::with_capacity(headers.len() + policy.len());
    let out = PyList::empty(py);
    let mut saw_allow_origin = false;
    for item in headers.iter() {
        let (name, value) = byte_pair(&item)?;
        let lower = name.as_bytes().to_ascii_lowercase();
        if is_policy(&lower) {
            continue;
        }
        saw_allow_origin |= lower == allow_origin;
        if lower != rid_name {
            out.append((name, value))?;
        }
        present.push(lower);
    }
    for item in policy.iter() {
        let (name, value) = byte_pair(&item)?;
        present.push(name.as_bytes().to_ascii_lowercase());
        saw_allow_origin |= name.as_bytes().eq_ignore_ascii_case(allow_origin);
        out.append((name, value))?;
    }
    for item in route_headers.try_iter()? {
        let (name, value) = item?.extract::<(String, String)>()?;
        let lower = name.as_bytes().to_ascii_lowercase();
        if lower == rid_name || is_policy(&lower) || present.contains(&lower) {
            continue;
        }
        out.append((
            PyBytes::new(py, name.as_bytes()),
            PyBytes::new(py, value.as_bytes()),
        ))?;
    }
    out.append((request_id_name, request_id))?;
    Ok((out, saw_allow_origin))
}
