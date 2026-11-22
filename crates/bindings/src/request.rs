//! Request decoding and JSON, in Rust: query strings, cookies, repeated
//! headers, JSON into Python objects and Python objects into JSON bytes.
//!
//! The pure functions at the top have no Python dependency and carry the unit
//! tests; the `#[pyfunction]`s below only convert between Python and them. The
//! ASGI scope itself is still a Python object, so this removes the parsing and
//! serialising work from Python, not the crossing.

use std::borrow::Cow;
use std::collections::HashMap;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

const MAX_DEPTH: usize = 200;

pyo3::create_exception!(core, MultipartLimitError, PyValueError);
pyo3::create_exception!(core, JsonDecodeError, PyValueError);

fn hex(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

/// Percent-decode `raw` (`+` is a space when `plus`); `None` if the result is
/// not UTF-8. A `%` that is not followed by two hex digits stays literal.
fn unquote(raw: &[u8], plus: bool) -> Option<String> {
    let mut out = Vec::with_capacity(raw.len());
    let mut i = 0;
    while i < raw.len() {
        match raw[i] {
            b'+' if plus => out.push(b' '),
            b'%' if i + 2 < raw.len() && hex(raw[i + 1]).is_some() && hex(raw[i + 2]).is_some() => {
                out.push(hex(raw[i + 1])? * 16 + hex(raw[i + 2])?);
                i += 2;
            }
            byte => out.push(byte),
        }
        i += 1;
    }
    String::from_utf8(out).ok()
}

/// Decode one router-matched path parameter. A plus remains a plus and a
/// percent-encoded slash stays within the segment selected by the router.
pub fn decode_path_parameter(raw: &str) -> Option<String> {
    unquote(raw.as_bytes(), false)
}

/// `a=1&b=2&a=3` into ordered pairs. Blank values are kept, empty pairs are
/// skipped, the input must be ASCII, every decoded part valid UTF-8 and there
/// are at most `max_pairs` pairs.
pub fn parse_query(raw: &[u8], max_pairs: usize) -> Result<Vec<(String, String)>, &'static str> {
    if !raw.is_ascii() {
        return Err("query string is not ASCII");
    }
    let mut pairs = Vec::new();
    for part in raw.split(|&b| b == b'&').filter(|p| !p.is_empty()) {
        if pairs.len() >= max_pairs {
            return Err("too many query or form fields");
        }
        let (name, value) = match part.iter().position(|&b| b == b'=') {
            Some(at) => (&part[..at], &part[at + 1..]),
            None => (part, &part[..0]),
        };
        let name = unquote(name, true).ok_or("query string is not valid UTF-8")?;
        let value = unquote(value, true).ok_or("query string is not valid UTF-8")?;
        pairs.push((name, value));
    }
    Ok(pairs)
}

/// `Cookie` header into a map; the first value of a repeated name wins and
/// surrounding quotes are stripped. Malformed parts are ignored.
pub fn parse_cookies(header: &str) -> Vec<(String, String)> {
    let mut seen = HashMap::new();
    let mut cookies = Vec::new();
    for part in header.split(';') {
        let Some((name, value)) = part.trim().split_once('=') else {
            continue;
        };
        if name.is_empty() || seen.contains_key(name) {
            continue;
        }
        seen.insert(name.to_owned(), ());
        let value = value.trim();
        let value = value
            .strip_prefix('"')
            .and_then(|v| v.strip_suffix('"'))
            .unwrap_or(value);
        cookies.push((name.to_owned(), value.to_owned()));
    }
    cookies
}

/// Lower-case header names with repeated headers joined by `", "`. Header
/// bytes are Latin-1, as in ASGI.
pub fn join_headers<N: AsRef<[u8]>, V: AsRef<[u8]>>(headers: &[(N, V)]) -> Vec<(String, String)> {
    let mut joined: Vec<(String, String)> = Vec::with_capacity(headers.len());
    let mut index: HashMap<String, usize> = HashMap::with_capacity(headers.len());
    for (name, value) in headers {
        let name = latin1(name.as_ref()).to_lowercase();
        let value = latin1(value.as_ref());
        match index.get(&name) {
            Some(&at) => {
                // HTTP/2 splits Cookie into several fields: rejoin with "; ".
                joined[at]
                    .1
                    .push_str(if name == "cookie" { "; " } else { ", " });
                joined[at].1.push_str(&value);
            }
            None => {
                index.insert(name.clone(), joined.len());
                joined.push((name, value));
            }
        }
    }
    joined
}

/// Latin-1 bytes as a `String`; plain ASCII (every real header) skips the
/// per-byte widening.
fn latin1(bytes: &[u8]) -> String {
    match std::str::from_utf8(bytes) {
        Ok(text) if text.is_ascii() => text.to_owned(),
        _ => bytes.iter().map(|&b| char::from(b)).collect(),
    }
}

/// One part of a `multipart/form-data` body.
#[derive(Debug, PartialEq)]
pub struct Part {
    pub name: String,
    pub filename: Option<String>,
    pub content_type: String,
    pub data: Vec<u8>,
}

const MAX_HEADER_BLOCK: usize = 8192;
const MAX_BOUNDARY: usize = 70;

fn find(hay: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if from > hay.len() || needle.is_empty() {
        return None;
    }
    hay[from..]
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|at| at + from)
}

/// `;`-separated parameters of a header value, honouring quoted strings with
/// backslash escapes. Names are lower-cased.
fn header_params(value: &str) -> Vec<(String, String)> {
    let mut params = Vec::new();
    let mut current = String::new();
    let mut quoted = false;
    let mut escaped = false;
    let mut pieces = Vec::new();
    for ch in value.chars() {
        if escaped {
            current.push(ch);
            escaped = false;
        } else if quoted && ch == '\\' {
            escaped = true;
        } else if ch == '"' {
            quoted = !quoted;
            current.push(ch);
        } else if ch == ';' && !quoted {
            pieces.push(std::mem::take(&mut current));
        } else {
            current.push(ch);
        }
    }
    pieces.push(current);
    for piece in pieces.iter().skip(1) {
        if let Some((name, raw)) = piece.split_once('=') {
            let raw = raw.trim();
            let raw = raw
                .strip_prefix('"')
                .and_then(|v| v.strip_suffix('"'))
                .unwrap_or(raw);
            params.push((name.trim().to_lowercase(), raw.to_owned()));
        }
    }
    params
}

fn part_headers(block: &[u8]) -> Result<(String, Option<String>, String), &'static str> {
    let text = std::str::from_utf8(block).map_err(|_| "part headers are not UTF-8")?;
    let (mut name, mut filename, mut content_type) = (None, None, String::from("text/plain"));
    let mut content_type_seen = false;
    for line in text.split("\r\n") {
        check_header_line(line)?;
        let Some((key, value)) = line.split_once(':') else {
            return Err("malformed part header");
        };
        match key.trim().to_lowercase().as_str() {
            "content-disposition" => parse_content_disposition(value, &mut name, &mut filename)?,
            "content-type" => {
                if content_type_seen {
                    return Err("duplicate Content-Type part header");
                }
                content_type_seen = true;
                content_type = value.trim().to_owned();
            }
            _ => {}
        }
    }
    Ok((name.ok_or("part has no name")?, filename, content_type))
}

fn check_header_line(line: &str) -> Result<(), &'static str> {
    if line.chars().any(|c| c.is_control() && c != '\t') {
        return Err("control character in a part header");
    }
    Ok(())
}

fn parse_content_disposition(
    value: &str,
    name: &mut Option<String>,
    filename: &mut Option<String>,
) -> Result<(), &'static str> {
    if !value
        .split(';')
        .next()
        .is_some_and(|v| v.trim().eq_ignore_ascii_case("form-data"))
    {
        return Err("part is not form-data");
    }
    for (param, found) in header_params(value) {
        let slot = match param.as_str() {
            "name" => &mut *name,
            "filename" => &mut *filename,
            _ => continue,
        };
        if slot.replace(found).is_some() {
            return Err("duplicate parameter in a part header");
        }
    }
    Ok(())
}

/// Split a `multipart/form-data` body (RFC 7578). Bounded: at most `max_parts`
/// parts and `max_part_bytes` per part; the whole body is already capped by the
/// caller. A file name is returned as sent and must never be used as a path.
pub fn parse_multipart(
    body: &[u8],
    boundary: &str,
    max_parts: usize,
    max_part_bytes: usize,
) -> Result<Vec<Part>, MultipartError> {
    if boundary.is_empty() || boundary.len() > MAX_BOUNDARY || boundary.bytes().any(|b| b < 0x20) {
        return Err(MultipartError::Invalid("invalid multipart boundary"));
    }
    let opening = [b"--".as_slice(), boundary.as_bytes()].concat();
    let delimiter = [b"\r\n".as_slice(), &opening].concat();
    if !body.starts_with(&opening) {
        return Err(MultipartError::Invalid(
            "multipart body does not start with the boundary",
        ));
    }
    let mut at = opening.len();
    let mut parts = Vec::new();
    loop {
        if body[at..].starts_with(b"--") {
            return Ok(parts);
        }
        if !body[at..].starts_with(b"\r\n") {
            return Err(MultipartError::Invalid("malformed multipart delimiter"));
        }
        at += 2;
        if parts.len() >= max_parts {
            return Err(MultipartError::Invalid("too many multipart parts"));
        }
        let end = find(body, b"\r\n\r\n", at)
            .ok_or(MultipartError::Invalid("unterminated part headers"))?;
        if end - at > MAX_HEADER_BLOCK {
            return Err(MultipartError::Invalid("part headers are too large"));
        }
        let (name, filename, content_type) =
            part_headers(&body[at..end]).map_err(MultipartError::Invalid)?;
        at = end + 4;
        let stop = find(body, &delimiter, at)
            .ok_or(MultipartError::Invalid("multipart body is not terminated"))?;
        if stop - at > max_part_bytes {
            return Err(MultipartError::LimitExceeded);
        }
        parts.push(Part {
            name,
            filename,
            content_type,
            data: body[at..stop].to_vec(),
        });
        at = stop + delimiter.len();
    }
}

#[derive(Debug, PartialEq)]
pub enum MultipartError {
    Invalid(&'static str),
    LimitExceeded,
}

/// Split a multipart body. Returns `(fields, files)`: fields are
/// `(name, text)`, files are `(name, filename, content_type, bytes)`. Raises
/// `ValueError` for malformed or over-limit bodies and non-UTF-8 text fields.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn parse_multipart_body<'py>(
    py: Python<'py>,
    raw: &[u8],
    boundary: &str,
    max_parts: usize,
    max_part_bytes: usize,
) -> PyResult<(
    Vec<(String, String)>,
    Vec<(String, String, String, Bound<'py, PyBytes>)>,
)> {
    let parts =
        parse_multipart(raw, boundary, max_parts, max_part_bytes).map_err(|error| match error {
            MultipartError::Invalid(message) => PyValueError::new_err(message),
            MultipartError::LimitExceeded => {
                MultipartLimitError::new_err("multipart part is too large")
            }
        })?;
    let (mut fields, mut files) = (Vec::new(), Vec::new());
    for part in parts {
        match part.filename {
            Some(filename) => files.push((
                part.name,
                filename,
                part.content_type,
                PyBytes::new(py, &part.data),
            )),
            None => {
                let text = String::from_utf8(part.data)
                    .map_err(|_| PyValueError::new_err("a form field is not valid UTF-8"))?;
                fields.push((part.name, text));
            }
        }
    }
    Ok((fields, files))
}

/// Pick the best of `available` for an `Accept` header (RFC 9110 §12.5.1):
/// highest quality, then most specific matching range, then earliest in
/// `available`. `None` when nothing is acceptable; an empty header accepts
/// anything, so the first entry wins.
pub fn negotiate(accept: &str, available: &[String]) -> Option<usize> {
    if accept.trim().is_empty() {
        return if available.is_empty() { None } else { Some(0) };
    }
    let ranges = parse_accept_ranges(accept);
    let mut best: Option<(f32, u8, usize)> = None;
    for (index, offered) in available.iter().enumerate() {
        let offered = normalize_offered(offered);
        if let Some((specificity, quality)) = best_range(&ranges, &offered) {
            if quality > 0.0 && is_better_match(best, quality, specificity) {
                best = Some((quality, specificity, index));
            }
        }
    }
    best.map(|(_, _, index)| index)
}

fn parse_accept_ranges(accept: &str) -> Vec<(String, f32)> {
    accept
        .split(',')
        .filter_map(|item| {
            let mut pieces = item.split(';');
            let media = pieces.next()?.trim().to_lowercase();
            if media.is_empty() {
                return None;
            }
            let mut quality = 1.0_f32;
            for param in pieces {
                if let Some((name, value)) = param.split_once('=') {
                    if name.trim().eq_ignore_ascii_case("q") {
                        quality = value.trim().parse::<f32>().ok()?.clamp(0.0, 1.0);
                    }
                }
            }
            Some((media, quality))
        })
        .collect()
}

fn normalize_offered(offered: &str) -> String {
    offered
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase()
}

fn best_range(ranges: &[(String, f32)], offered: &str) -> Option<(u8, f32)> {
    let group = offered.split('/').next().unwrap_or("");
    ranges
        .iter()
        .filter_map(|(media, quality)| {
            let specificity = media_specificity(media, offered, group)?;
            Some((specificity, *quality))
        })
        .fold(None::<(u8, f32)>, |best, found| match best {
            Some((spec, _)) if spec >= found.0 => best, // first range wins a tie
            _ => Some(found),
        })
}

fn media_specificity(media: &str, offered: &str, group: &str) -> Option<u8> {
    if media == offered {
        Some(3)
    } else if media.strip_suffix("/*") == Some(group) {
        Some(2)
    } else if media == "*/*" {
        Some(1)
    } else {
        None
    }
}

fn is_better_match(best: Option<(f32, u8, usize)>, quality: f32, specificity: u8) -> bool {
    match best {
        None => true,
        Some((previous_quality, previous_specificity, _)) => {
            quality > previous_quality
                || (quality == previous_quality && specificity > previous_specificity)
        }
    }
}

/// Index into `available` of the best match for an `Accept` header, or `None`.
#[pyfunction]
pub fn negotiate_media(accept: &str, available: Vec<String>) -> Option<usize> {
    negotiate(accept, &available)
}

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
    let result = PyDict::new(py);
    for (name, value) in join_headers(&pairs) {
        result.set_item(name, value)?;
    }
    Ok((result.unbind(), size))
}

use kiso::headers::is_policy;

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

const MAX_PARSE_DEPTH: usize = 128;

/// What the JSON parser builds a document into: Python objects for
/// `decode_json`, or the GIL-free [`Json`] tree. The parser decides the
/// grammar, the order of the calls and every error message; a builder only
/// stores values.
trait Builder {
    type Value;
    type Object;
    type Array;

    fn null(&mut self) -> Self::Value;
    fn boolean(&mut self, value: bool) -> Self::Value;
    fn string(&mut self, text: &str) -> Self::Value;
    fn integer(&mut self, value: i64) -> Self::Value;
    /// An integer outside `i64`, as its decimal JSON text.
    fn big_integer(&mut self, text: &str) -> Parsed<Self::Value>;
    fn float(&mut self, value: f64) -> Self::Value;
    fn object(&mut self) -> Self::Object;
    /// Store `key`; a repeated key keeps its first position and takes the
    /// last value (Python `dict` semantics).
    fn insert(&mut self, object: &mut Self::Object, key: &str, value: Self::Value) -> Parsed<()>;
    fn close_object(&mut self, object: Self::Object) -> Self::Value;
    fn array(&mut self) -> Self::Array;
    fn push(&mut self, array: &mut Self::Array, value: Self::Value) -> Parsed<()>;
    fn close_array(&mut self, array: Self::Array) -> Self::Value;
}

/// Slots in the per-call key cache. A fixed size bounds its memory.
const KEY_CACHE_SLOTS: usize = 128;
/// Longer keys are rarely repeated, so they are never cached.
const KEY_CACHE_MAX_LEN: usize = 32;
/// Keys seen before the cache starts, so a tiny document pays nothing for it.
const KEY_CACHE_AFTER: usize = 16;

/// Builds Python objects (`decode_json`). Like `json.loads`, it reuses one
/// `str` object for a key repeated within a call. The cache is direct-mapped
/// by a hash of the key (a colliding key replaces the older one) and dies
/// with the call, so it is not an intern table and holds at most
/// `KEY_CACHE_SLOTS` short keys.
struct PyBuilder<'py> {
    py: Python<'py>,
    keys: Vec<Option<(Box<str>, Bound<'py, PyString>)>>,
    seen: usize,
}

impl<'py> PyBuilder<'py> {
    fn new(py: Python<'py>) -> Self {
        Self {
            py,
            keys: Vec::new(),
            seen: 0,
        }
    }

    fn key(&mut self, key: &str) -> Bound<'py, PyString> {
        self.seen += 1;
        if key.len() > KEY_CACHE_MAX_LEN || self.seen <= KEY_CACHE_AFTER {
            return PyString::new(self.py, key);
        }
        if self.keys.is_empty() {
            self.keys.resize_with(KEY_CACHE_SLOTS, || None);
        }
        let hash = key.bytes().fold(0xcbf2_9ce4_u32, |h, b| {
            (h ^ u32::from(b)).wrapping_mul(0x0100_0193)
        });
        let slot = &mut self.keys[hash as usize % KEY_CACHE_SLOTS];
        if let Some((cached, object)) = slot {
            if &**cached == key {
                return object.clone();
            }
        }
        let object = PyString::new(self.py, key);
        *slot = Some((key.into(), object.clone()));
        object
    }
}

impl<'py> Builder for PyBuilder<'py> {
    type Value = Bound<'py, PyAny>;
    type Object = Bound<'py, PyDict>;
    type Array = Bound<'py, PyList>;

    fn null(&mut self) -> Self::Value {
        self.py.None().into_bound(self.py)
    }

    fn boolean(&mut self, value: bool) -> Self::Value {
        PyBool::new(self.py, value).to_owned().into_any()
    }

    fn string(&mut self, text: &str) -> Self::Value {
        PyString::new(self.py, text).into_any()
    }

    fn integer(&mut self, value: i64) -> Self::Value {
        PyInt::new(self.py, value).into_any()
    }

    fn big_integer(&mut self, text: &str) -> Parsed<Self::Value> {
        self.py
            .get_type::<PyInt>()
            .call1((text,))
            .map_err(|e| ParseError::python(&e))
    }

    fn float(&mut self, value: f64) -> Self::Value {
        PyFloat::new(self.py, value).into_any()
    }

    fn object(&mut self) -> Self::Object {
        PyDict::new(self.py)
    }

    fn insert(&mut self, object: &mut Self::Object, key: &str, value: Self::Value) -> Parsed<()> {
        object
            .set_item(self.key(key), value)
            .map_err(|e| ParseError::python(&e))
    }

    fn close_object(&mut self, object: Self::Object) -> Self::Value {
        object.into_any()
    }

    fn array(&mut self) -> Self::Array {
        PyList::empty(self.py)
    }

    fn push(&mut self, array: &mut Self::Array, value: Self::Value) -> Parsed<()> {
        array.append(value).map_err(|e| ParseError::python(&e))
    }

    fn close_array(&mut self, array: Self::Array) -> Self::Value {
        array.into_any()
    }
}

/// A decoded JSON value that holds no Python object, so it can be built and
/// validated without the GIL. It keeps what `decode_json` would produce:
/// integers in `i64` as [`Json::Int`], larger ones as their text, floats
/// as `f64`, object keys in first-seen order with the last value of a
/// repeated key.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Json {
    Null,
    Bool(bool),
    Int(i64),
    /// An integer outside `i64`, as its decimal JSON text.
    BigInt(String),
    Float(f64),
    Str(String),
    List(Vec<Json>),
    Object(JsonObject),
}

/// The members of a JSON object in first-seen key order.
#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct JsonObject {
    entries: Vec<(String, Json)>,
    index: HashMap<String, usize>,
}

impl JsonObject {
    /// Python `dict` assignment: a new key is appended, a known key keeps its
    /// position and takes the new value.
    pub(crate) fn insert(&mut self, key: String, value: Json) {
        if let Some(&at) = self.index.get(&key) {
            self.entries[at].1 = value;
            return;
        }
        self.index.insert(key.clone(), self.entries.len());
        self.entries.push((key, value));
    }

    pub(crate) fn get(&self, key: &str) -> Option<&Json> {
        self.index.get(key).map(|&at| &self.entries[at].1)
    }

    pub(crate) fn contains(&self, key: &str) -> bool {
        self.index.contains_key(key)
    }

    pub(crate) fn keys(&self) -> impl Iterator<Item = &str> {
        self.entries.iter().map(|(key, _)| key.as_str())
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = (&str, &Json)> {
        self.entries
            .iter()
            .map(|(key, value)| (key.as_str(), value))
    }
}

/// The Python object `decode_json` builds for the same document.
#[cfg(test)]
pub(crate) fn json_to_python<'py>(py: Python<'py>, value: &Json) -> PyResult<Bound<'py, PyAny>> {
    let mut builder = PyBuilder::new(py);
    Ok(match value {
        Json::Null => builder.null(),
        Json::Bool(flag) => builder.boolean(*flag),
        Json::Int(int) => builder.integer(*int),
        Json::BigInt(text) => builder.big_integer(text).map_err(ParseError::into_py)?,
        Json::Float(float) => builder.float(*float),
        Json::Str(text) => builder.string(text),
        Json::List(items) => {
            let list = builder.array();
            for item in items {
                list.append(json_to_python(py, item)?)?;
            }
            builder.close_array(list)
        }
        Json::Object(object) => {
            let dict = builder.object();
            for (key, item) in object.iter() {
                dict.set_item(key, json_to_python(py, item)?)?;
            }
            builder.close_object(dict)
        }
    })
}

/// A whole decoded body. `big_integers` says that some integer did not fit
/// `i64`: Python decodes such text with `int()`, which has its own digit
/// limit, so a caller that must match `decode_json` exactly treats such a
/// document as uncertain.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct JsonDocument {
    pub(crate) root: Json,
    pub(crate) big_integers: bool,
}

/// Builds the GIL-free [`Json`] tree.
#[derive(Default)]
struct DomBuilder {
    big_integers: bool,
}

impl Builder for DomBuilder {
    type Value = Json;
    type Object = JsonObject;
    type Array = Vec<Json>;

    fn null(&mut self) -> Json {
        Json::Null
    }

    fn boolean(&mut self, value: bool) -> Json {
        Json::Bool(value)
    }

    fn string(&mut self, text: &str) -> Json {
        Json::Str(text.to_owned())
    }

    fn integer(&mut self, value: i64) -> Json {
        Json::Int(value)
    }

    fn big_integer(&mut self, text: &str) -> Parsed<Json> {
        self.big_integers = true;
        Ok(Json::BigInt(text.to_owned()))
    }

    fn float(&mut self, value: f64) -> Json {
        Json::Float(value)
    }

    fn object(&mut self) -> JsonObject {
        JsonObject::default()
    }

    fn insert(&mut self, object: &mut JsonObject, key: &str, value: Json) -> Parsed<()> {
        object.insert(key.to_owned(), value);
        Ok(())
    }

    fn close_object(&mut self, object: JsonObject) -> Json {
        Json::Object(object)
    }

    fn array(&mut self) -> Vec<Json> {
        Vec::new()
    }

    fn push(&mut self, array: &mut Vec<Json>, value: Json) -> Parsed<()> {
        array.push(value);
        Ok(())
    }

    fn close_array(&mut self, array: Vec<Json>) -> Json {
        Json::List(array)
    }
}

/// Recursive-descent JSON parser over a [`Builder`]. Strict RFC 8259: no
/// comments, no trailing commas, no `NaN`/`Infinity`, no BOM, valid UTF-8
/// and paired surrogates only. Integers of any size are exact.
struct Parser<'a, B> {
    builder: B,
    input: &'a [u8],
    at: usize,
}

/// Why a document was refused. `syntax` is false when Python itself rejected a
/// value (an integer over its digit limit): that error is not the document's fault.
#[derive(Debug)]
pub(crate) struct ParseError {
    message: String,
    syntax: bool,
}

impl<S: Into<String>> From<S> for ParseError {
    fn from(message: S) -> Self {
        Self {
            message: message.into(),
            syntax: true,
        }
    }
}

impl ParseError {
    fn into_py(self) -> PyErr {
        if self.syntax {
            JsonDecodeError::new_err(self.message)
        } else {
            PyValueError::new_err(self.message)
        }
    }

    fn python(error: &PyErr) -> Self {
        Self {
            message: error.to_string(),
            syntax: false,
        }
    }
}

type Parsed<T> = Result<T, ParseError>;

impl<'a, B: Builder> Parser<'a, B> {
    fn fail<T>(&self, what: &str) -> Parsed<T> {
        Err(format!("{what} at byte {}", self.at).into())
    }

    fn peek(&self) -> Option<u8> {
        self.input.get(self.at).copied()
    }

    fn skip_space(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    fn expect(&mut self, word: &[u8]) -> Parsed<()> {
        if self.input[self.at..].starts_with(word) {
            self.at += word.len();
            Ok(())
        } else {
            self.fail("invalid literal")
        }
    }

    /// A container at `depth` is the `depth + 1`th around the innermost value.
    fn enter(&self, depth: usize) -> Parsed<()> {
        if depth >= MAX_PARSE_DEPTH {
            return self.fail("nesting too deep");
        }
        Ok(())
    }

    fn value(&mut self, depth: usize) -> Parsed<B::Value> {
        self.skip_space();
        match self.peek() {
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            Some(b'"') => {
                let text = self.string()?;
                Ok(self.builder.string(&text))
            }
            Some(b't') => self.expect(b"true").map(|()| self.builder.boolean(true)),
            Some(b'f') => self.expect(b"false").map(|()| self.builder.boolean(false)),
            Some(b'n') => self.expect(b"null").map(|()| self.builder.null()),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => self.fail("unexpected character"),
        }
    }

    fn object(&mut self, depth: usize) -> Parsed<B::Value> {
        self.enter(depth)?;
        self.at += 1;
        let mut object = self.builder.object();
        self.skip_space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(self.builder.close_object(object));
        }
        loop {
            self.skip_space();
            if self.peek() != Some(b'"') {
                return self.fail("expected a string key");
            }
            let key = self.string()?;
            self.skip_space();
            if self.peek() != Some(b':') {
                return self.fail("expected ':'");
            }
            self.at += 1;
            let item = self.value(depth + 1)?;
            self.builder.insert(&mut object, &key, item)?;
            self.skip_space();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b'}') => {
                    self.at += 1;
                    return Ok(self.builder.close_object(object));
                }
                _ => return self.fail("expected ',' or '}'"),
            }
        }
    }

    fn array(&mut self, depth: usize) -> Parsed<B::Value> {
        self.enter(depth)?;
        self.at += 1;
        let mut array = self.builder.array();
        self.skip_space();
        if self.peek() == Some(b']') {
            self.at += 1;
            return Ok(self.builder.close_array(array));
        }
        loop {
            let item = self.value(depth + 1)?;
            self.builder.push(&mut array, item)?;
            self.skip_space();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b']') => {
                    self.at += 1;
                    return Ok(self.builder.close_array(array));
                }
                _ => return self.fail("expected ',' or ']'"),
            }
        }
    }

    fn hex4(&mut self) -> Parsed<u32> {
        let digits = self
            .input
            .get(self.at..self.at + 4)
            .ok_or("truncated \\u escape")?;
        let mut code = 0;
        for &digit in digits {
            code = code * 16 + u32::from(hex(digit).ok_or("bad \\u escape")?);
        }
        self.at += 4;
        Ok(code)
    }

    fn escape(&mut self, out: &mut String) -> Parsed<()> {
        let Some(letter) = self.peek() else {
            return self.fail("truncated escape");
        };
        self.at += 1;
        match letter {
            b'"' => out.push('"'),
            b'\\' => out.push('\\'),
            b'/' => out.push('/'),
            b'b' => out.push('\u{8}'),
            b'f' => out.push('\u{c}'),
            b'n' => out.push('\n'),
            b'r' => out.push('\r'),
            b't' => out.push('\t'),
            b'u' => {
                let first = self.hex4()?;
                let code = if (0xD800..0xDC00).contains(&first) {
                    if !self.input[self.at..].starts_with(b"\\u") {
                        return self.fail("unpaired surrogate");
                    }
                    self.at += 2;
                    let second = self.hex4()?;
                    if !(0xDC00..0xE000).contains(&second) {
                        return self.fail("unpaired surrogate");
                    }
                    0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00)
                } else {
                    first
                };
                out.push(char::from_u32(code).ok_or("unpaired surrogate")?);
            }
            _ => return self.fail("invalid escape"),
        }
        Ok(())
    }

    /// The next run of plain string bytes, validated as UTF-8.
    fn plain_run(&mut self) -> Parsed<&'a str> {
        let input = self.input;
        let start = self.at;
        self.at += special_at(&input[start..]);
        std::str::from_utf8(&input[start..self.at]).map_err(|_| "invalid UTF-8".into())
    }

    /// A string without escapes borrows the input; one with escapes is copied.
    fn string(&mut self) -> Parsed<Cow<'a, str>> {
        self.at += 1;
        let run = self.plain_run()?;
        match self.peek() {
            Some(b'"') => {
                self.at += 1;
                Ok(Cow::Borrowed(run))
            }
            _ => self.escaped_string(run).map(Cow::Owned),
        }
    }

    fn escaped_string(&mut self, first: &str) -> Parsed<String> {
        let mut out = String::from(first);
        loop {
            match self.peek() {
                Some(b'"') => {
                    self.at += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.at += 1;
                    self.escape(&mut out)?;
                }
                Some(_) => return self.fail("control character in string"),
                None => return self.fail("unterminated string"),
            }
            out.push_str(self.plain_run()?);
        }
    }

    /// Digits to the end of the run, with their value when it fits `u64`.
    fn digits(&mut self) -> Parsed<Option<u64>> {
        let start = self.at;
        let mut value = Some(0u64);
        while let Some(digit @ b'0'..=b'9') = self.peek() {
            value = value
                .and_then(|so_far| so_far.checked_mul(10))
                .and_then(|so_far| so_far.checked_add(u64::from(digit - b'0')));
            self.at += 1;
        }
        if self.at == start {
            return self.fail("expected digits");
        }
        Ok(value)
    }

    /// The `.fraction` and `e±exponent` parts; true when either is present.
    fn fraction_and_exponent(&mut self) -> Parsed<bool> {
        let mut real = false;
        if self.peek() == Some(b'.') {
            real = true;
            self.at += 1;
            self.digits()?;
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            real = true;
            self.at += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.at += 1;
            }
            self.digits()?;
        }
        Ok(real)
    }

    fn number(&mut self) -> Parsed<B::Value> {
        let start = self.at;
        let negative = self.peek() == Some(b'-');
        if negative {
            self.at += 1;
        }
        let magnitude = if self.peek() == Some(b'0') {
            self.at += 1;
            Some(0)
        } else {
            self.digits()?
        };
        let real = self.fraction_and_exponent()?;
        if !real {
            let signed = magnitude.map(|m| {
                if negative {
                    -i128::from(m)
                } else {
                    i128::from(m)
                }
            });
            if let Some(int) = signed.and_then(|n| i64::try_from(n).ok()) {
                return Ok(self.builder.integer(int));
            }
        }
        let text =
            std::str::from_utf8(&self.input[start..self.at]).map_err(|_| "invalid number")?;
        if real {
            let float = text.parse::<f64>().map_err(|e| e.to_string())?;
            if !float.is_finite() {
                return self.fail("number out of range");
            }
            return Ok(self.builder.float(float));
        }
        self.builder.big_integer(text)
    }
}

/// Parse one whole document (trailing whitespace only) with `builder`.
fn parse_document<B: Builder>(builder: B, raw: &[u8]) -> Parsed<(B::Value, B)> {
    let mut parser = Parser {
        builder,
        input: raw,
        at: 0,
    };
    let value = parser.value(0)?;
    parser.skip_space();
    if parser.at != raw.len() {
        return Err(format!("trailing data at byte {}", parser.at).into());
    }
    Ok((value, parser.builder))
}

// The GIL-free entry points have no caller in the extension yet: the native
// CRUD executor uses them. Allowing them keeps everything they reach checked.
/// Parse a JSON document into the GIL-free [`Json`] tree. Accepts and
/// refuses exactly what `decode_json` does, with the same error messages.
#[cfg_attr(
    not(test),
    allow(dead_code, reason = "consumed by the native CRUD executor")
)]
pub(crate) fn decode_json_document(raw: &[u8]) -> Result<JsonDocument, String> {
    let (root, builder) = parse_document(DomBuilder::default(), raw).map_err(|e| e.message)?;
    Ok(JsonDocument {
        root,
        big_integers: builder.big_integers,
    })
}

/// Parse a JSON document into Python objects. Raises `JsonDecodeError` (a
/// `ValueError`) for syntax errors, invalid UTF-8, `NaN`/`Infinity`, unpaired
/// surrogates and more than 128 nested arrays or objects, and a plain `ValueError`
/// when Python refuses a value (an integer over its digit limit).
#[pyfunction]
pub fn decode_json<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    parse_document(PyBuilder::new(py), raw)
        .map(|(value, _)| value)
        .map_err(ParseError::into_py)
}

fn write_string(out: &mut Vec<u8>, text: &str) -> PyResult<()> {
    out.push(b'"');
    push_escaped(out, text);
    out.push(b'"');
    Ok(())
}

/// The JSON-escaped contents of `text`, without quotes: `"`, `\`, and control
/// characters are escaped (`\n`-style where JSON has one, `\u00xx` otherwise);
/// everything else, DEL and `/` included, is copied as is.
fn push_escaped(out: &mut Vec<u8>, text: &str) {
    let bytes = text.as_bytes();
    let mut start = 0;
    while start < bytes.len() {
        let at = start + special_at(&bytes[start..]);
        out.extend_from_slice(&bytes[start..at]);
        let Some(&byte) = bytes.get(at) else { return };
        push_escape(out, byte);
        start = at + 1;
    }
}

/// The offset of the first byte of `bytes` that a JSON string cannot hold
/// as is (`"`, `\` or a control character), or `bytes.len()`. Eight bytes are
/// tested per step with plain integer arithmetic.
fn special_at(bytes: &[u8]) -> usize {
    const ONES: u64 = 0x0101_0101_0101_0101;
    const HIGH: u64 = 0x8080_8080_8080_8080;
    let mut at = 0;
    for chunk in bytes.as_chunks::<8>().0 {
        let word = u64::from_le_bytes(*chunk);
        let quote = word ^ (ONES * 0x22);
        let slash = word ^ (ONES * 0x5c);
        let found = (word.wrapping_sub(ONES * 0x20) & !word)
            | (quote.wrapping_sub(ONES) & !quote)
            | (slash.wrapping_sub(ONES) & !slash);
        if found & HIGH != 0 {
            return at + (found & HIGH).trailing_zeros() as usize / 8;
        }
        at += 8;
    }
    bytes[at..]
        .iter()
        .position(|&byte| byte < 0x20 || byte == b'"' || byte == b'\\')
        .map_or(bytes.len(), |found| at + found)
}

fn push_escape(out: &mut Vec<u8>, byte: u8) {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let short = match byte {
        b'"' => b'"',
        b'\\' => b'\\',
        0x08 => b'b',
        0x0c => b'f',
        b'\n' => b'n',
        b'\r' => b'r',
        b'\t' => b't',
        _ => {
            out.extend_from_slice(b"\\u00");
            out.push(HEX[usize::from(byte >> 4)]);
            out.push(HEX[usize::from(byte & 0x0f)]);
            return;
        }
    };
    out.extend_from_slice(&[b'\\', short]);
}

/// The bytes of a Python `str`: UTF-8, or with `surrogatepass` when it holds
/// lone surrogates. That form sorts like code points.
fn string_bytes<'a>(text: &'a Bound<'_, PyString>) -> PyResult<Cow<'a, [u8]>> {
    match text.to_str() {
        Ok(valid) => Ok(Cow::Borrowed(valid.as_bytes())),
        Err(_) => text
            .call_method1("encode", ("utf-8", "surrogatepass"))?
            .extract::<Vec<u8>>()
            .map(Cow::Owned),
    }
}

/// Write [`string_bytes`] output as a JSON string; each lone surrogate becomes
/// `\udXXX`, as `json.dumps` writes it.
fn write_text(out: &mut Vec<u8>, bytes: &[u8]) -> PyResult<()> {
    let first = match std::str::from_utf8(bytes) {
        Ok(valid) => return write_string(out, valid),
        Err(error) => error,
    };
    out.push(b'"');
    let mut rest = bytes;
    let mut error = Some(first);
    while let Some(found) = error {
        let (valid, tail) = rest.split_at(found.valid_up_to());
        write_body(out, valid)?;
        let [a, b, c, tail @ ..] = tail else {
            return Err(PyValueError::new_err("invalid surrogate encoding"));
        };
        let unit = (u32::from(a & 0x0f) << 12) | (u32::from(b & 0x3f) << 6) | u32::from(c & 0x3f);
        out.extend_from_slice(format!("\\u{unit:04x}").as_bytes());
        rest = tail;
        error = std::str::from_utf8(rest).err();
    }
    write_body(out, rest)?;
    out.push(b'"');
    Ok(())
}

/// The escaped contents of a valid UTF-8 run, without the quotes.
fn write_body(out: &mut Vec<u8>, valid: &[u8]) -> PyResult<()> {
    let text = std::str::from_utf8(valid).map_err(|e| PyValueError::new_err(e.to_string()))?;
    push_escaped(out, text);
    Ok(())
}

/// A Python `str` as a JSON string. A valid one is written from the borrowed
/// UTF-8; one with lone surrogates takes the `surrogatepass` path.
fn write_pystring(out: &mut Vec<u8>, text: &Bound<'_, PyString>) -> PyResult<()> {
    match text.to_str() {
        Ok(valid) => write_string(out, valid),
        Err(_) => write_text(out, &string_bytes(text)?),
    }
}

/// Append a finite float in the same text `serde_json` gives a `Number`
/// (shortest round-trip digits, `1e+21`-style exponents, `1.0` for whole values).
fn push_float(out: &mut Vec<u8>, value: f64) -> PyResult<()> {
    if !value.is_finite() {
        return Err(PyValueError::new_err(
            "out of range float values are not JSON compliant",
        ));
    }
    serde_json::to_writer(out, &value).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Append the decimal text of `number`.
fn push_i64(out: &mut Vec<u8>, number: i64) {
    let mut digits = [0u8; 20];
    let mut at = digits.len();
    let mut rest = number.unsigned_abs();
    loop {
        at -= 1;
        digits[at] = b'0' + (rest % 10) as u8;
        rest /= 10;
        if rest == 0 {
            break;
        }
    }
    if number < 0 {
        out.push(b'-');
    }
    out.extend_from_slice(&digits[at..]);
}

/// Decimal text of a Python int, immune to a subclass overriding `__str__`.
fn int_text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let repr = value.py().get_type::<PyInt>().getattr("__repr__")?;
    repr.call1((value,))?.extract()
}

/// The bytes a dict key is written as; `sort_keys` on string keys orders by
/// them, which is the code point order `json.dumps` uses.
fn key_text(key: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    if let Ok(text) = key.cast::<PyString>() {
        return Ok(string_bytes(text)?.into_owned());
    }
    if key.is_instance_of::<PyBool>() {
        return Ok(if key.is_truthy()? { "true" } else { "false" }.into());
    }
    if key.is_instance_of::<PyInt>() {
        return Ok(int_text(key)?.into_bytes());
    }
    if key.is_none() {
        return Ok(b"null".to_vec());
    }
    Err(PyTypeError::new_err("keys must be str, int, bool or None"))
}

/// Rewrite every character outside printable ASCII (DEL and all non-ASCII) of
/// valid UTF-8 JSON as `\uXXXX` (a surrogate pair above the BMP), as `json.dumps`
/// does by default. Only string contents can hold such characters, so the
/// structure is untouched.
fn escape_non_ascii(json: &[u8]) -> Vec<u8> {
    let text = String::from_utf8_lossy(json);
    let mut out = Vec::with_capacity(json.len());
    let mut units = [0u16; 2];
    for ch in text.chars() {
        if ch.is_ascii() && ch != '\u{7f}' {
            out.push(ch as u8);
            continue;
        }
        for unit in ch.encode_utf16(&mut units) {
            out.extend_from_slice(format!("\\u{unit:04x}").as_bytes());
        }
    }
    out
}

/// How `encode_json` writes: compact by default; `indent` pretty-prints with that
/// many spaces, `sort_keys` orders object keys, and `default` is called for a
/// value that is not JSON (it must return something that is).
const MAX_INDENT: usize = 256;

struct Encoder<'py> {
    out: Vec<u8>,
    indent: Option<usize>,
    sort_keys: bool,
    default: Option<Bound<'py, PyAny>>,
    hops: usize,
    /// Schema instances being written: only inside one do `date`, `time`,
    /// `timedelta`, `Decimal` and `bytes` values have a wire form.
    schemas: usize,
    /// Dict entries being written, one slice per open dict.
    pairs: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>,
}

impl<'py> Encoder<'py> {
    fn newline(&mut self, depth: usize) {
        if let Some(width) = self.indent {
            self.out.push(b'\n');
            self.out.resize(self.out.len() + width * depth, b' ');
        }
    }

    fn colon(&mut self) {
        self.out
            .extend_from_slice(if self.indent.is_some() { b": " } else { b":" });
    }

    fn open(&mut self, bracket: u8) {
        self.out.push(bracket);
    }

    fn close(&mut self, bracket: u8, depth: usize, empty: bool) {
        if !empty {
            self.newline(depth);
        }
        self.out.push(bracket);
    }

    /// An exact `int` that fits `i64` is formatted here; big ints and subclasses
    /// (which may override `__str__`) take the `int.__repr__` path.
    fn int(&mut self, value: &Bound<'py, PyAny>) -> PyResult<()> {
        if value.is_exact_instance_of::<PyInt>() {
            if let Ok(number) = value.extract::<i64>() {
                push_i64(&mut self.out, number);
                return Ok(());
            }
        }
        self.out.extend_from_slice(int_text(value)?.as_bytes());
        Ok(())
    }

    /// A container at `depth` is the `depth + 1`th around the innermost value.
    fn enter(depth: usize) -> PyResult<()> {
        if depth >= MAX_DEPTH {
            return Err(PyValueError::new_err("value is nested too deeply"));
        }
        Ok(())
    }

    fn write(&mut self, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        if let Ok(text) = value.cast::<PyString>() {
            write_pystring(&mut self.out, text)
        } else if value.is_exact_instance_of::<PyInt>() {
            self.int(value)
        } else if value.is_exact_instance_of::<PyFloat>() {
            push_float(&mut self.out, value.cast::<PyFloat>()?.value())
        } else if let Ok(dict) = value.cast::<PyDict>() {
            self.mapping(dict, depth)
        } else if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
            self.sequence(value, depth)
        } else {
            self.scalar(value, depth)
        }
    }

    /// `None`, `bool`, `int` and `float`, then anything else through [`Self::object`].
    fn scalar(&mut self, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        if value.is_none() {
            self.out.extend_from_slice(b"null");
        } else if let Ok(flag) = value.cast::<PyBool>() {
            self.out
                .extend_from_slice(if flag.is_true() { b"true" } else { b"false" });
        } else if value.is_instance_of::<PyInt>() {
            self.int(value)?;
        } else if let Ok(float) = value.cast::<PyFloat>() {
            push_float(&mut self.out, float.value())?;
        } else {
            self.object(value, depth)?;
        }
        Ok(())
    }

    /// Entries in key order for `sort_keys`: string keys by code point, any other
    /// keys by Python comparison of the keys themselves, so `{10: 1, 2: 1}` puts
    /// `2` first and mixed key types raise `TypeError`, as in `json.dumps`.
    fn sorted_entries(
        &self,
        mut items: Vec<(Vec<u8>, Bound<'py, PyAny>, Bound<'py, PyAny>)>,
    ) -> PyResult<Vec<(Vec<u8>, Bound<'py, PyAny>)>> {
        if items
            .iter()
            .all(|entry| entry.1.is_instance_of::<PyString>())
        {
            items.sort_by(|a, b| a.0.cmp(&b.0));
        } else if let Some(py) = items.first().map(|entry| entry.1.py()) {
            let tagged = PyList::empty(py);
            for (at, entry) in items.iter().enumerate() {
                tagged.append((&entry.1, at))?;
            }
            tagged.sort()?;
            let mut order = Vec::with_capacity(items.len());
            for tag in tagged.iter() {
                order.push(tag.get_item(1)?.extract::<usize>()?);
            }
            let mut slots: Vec<_> = items.into_iter().map(Some).collect();
            items = order
                .into_iter()
                .filter_map(|at| slots[at].take())
                .collect();
        }
        Ok(items
            .into_iter()
            .map(|(text, _, item)| (text, item))
            .collect())
    }

    fn mapping(&mut self, dict: &Bound<'py, PyDict>, depth: usize) -> PyResult<()> {
        Self::enter(depth)?;
        if self.sort_keys {
            return self.sorted_mapping(dict, depth);
        }
        // Entries go on a shared stack first: writing a value may run Python
        // code (`default`, `dump()`) that mutates the dict being walked.
        let start = self.pairs.len();
        self.pairs.extend(dict.iter());
        let end = self.pairs.len();
        self.open(b'{');
        for at in start..end {
            let (key, item) = self.pairs[at].clone();
            if at > start {
                self.out.push(b',');
            }
            self.newline(depth + 1);
            self.write_key(&key)?;
            self.colon();
            self.write(&item, depth + 1)?;
        }
        self.pairs.truncate(start);
        self.close(b'}', depth, start == end);
        Ok(())
    }

    fn write_key(&mut self, key: &Bound<'py, PyAny>) -> PyResult<()> {
        match key.cast::<PyString>() {
            Ok(text) => write_pystring(&mut self.out, text),
            Err(_) => write_text(&mut self.out, &key_text(key)?),
        }
    }

    fn sorted_mapping(&mut self, dict: &Bound<'py, PyDict>, depth: usize) -> PyResult<()> {
        let mut items = Vec::with_capacity(dict.len());
        for (key, item) in dict.iter() {
            items.push((key_text(&key)?, key, item));
        }
        let items = self.sorted_entries(items)?;
        self.open(b'{');
        let empty = items.is_empty();
        for (index, (key, item)) in items.into_iter().enumerate() {
            if index > 0 {
                self.out.push(b',');
            }
            self.newline(depth + 1);
            write_text(&mut self.out, &key)?;
            self.colon();
            self.write(&item, depth + 1)?;
        }
        self.close(b'}', depth, empty);
        Ok(())
    }

    /// A list or tuple, read by position; a list's length is re-read on every
    /// step, so a hook that resizes it cannot make the walk run out of bounds.
    fn sequence(&mut self, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        Self::enter(depth)?;
        self.open(b'[');
        let mut count = 0;
        if value.is_exact_instance_of::<PyList>() {
            // Read by position: a hook that mutates the list cannot invalidate it.
            let list = value.cast::<PyList>()?;
            while let Ok(item) = list.get_item(count) {
                self.element(&item, count, depth)?;
                count += 1;
            }
        } else if value.is_exact_instance_of::<PyTuple>() {
            let tuple = value.cast::<PyTuple>()?;
            while let Ok(item) = tuple.get_item(count) {
                self.element(&item, count, depth)?;
                count += 1;
            }
        } else {
            // Subclasses keep their own `__iter__`, as `json.dumps` does.
            for item in value.try_iter()? {
                self.element(&item?, count, depth)?;
                count += 1;
            }
        }
        self.close(b']', depth, count == 0);
        Ok(())
    }

    fn element(&mut self, item: &Bound<'py, PyAny>, index: usize, depth: usize) -> PyResult<()> {
        if index > 0 {
            self.out.push(b',');
        }
        self.newline(depth + 1);
        self.write(item, depth + 1)
    }

    /// Write what `dump()` or the `default` hook returned in place of a value: at
    /// the same indentation depth, with a separate budget so a hook that keeps
    /// producing new objects ends in an error instead of overflowing the stack.
    fn replace(&mut self, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        self.hops += 1;
        if self.hops > MAX_DEPTH {
            return Err(PyValueError::new_err("value is nested too deeply"));
        }
        let result = self.write(value, depth);
        self.hops -= 1;
        result
    }

    /// Objects that are not plain JSON values: schema instances, UUIDs, datetimes,
    /// anything with a `dump()` method, then the caller's `default` hook.
    fn object(&mut self, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        // Exact UUIDs and datetimes cannot be schema instances: skip that lookup.
        if let Some(text) = crate::pytypes::exact_uuid_text(value)? {
            return write_string(&mut self.out, &text);
        }
        if value
            .get_type()
            .is(crate::pytypes::datetime_class(value.py())?)
        {
            return write_string(
                &mut self.out,
                &value.call_method0("isoformat")?.str()?.to_string(),
            );
        }
        if let Some((names, unset, _)) = crate::schema::instance_fields(value, true, true, true)? {
            self.schema_object(value, names, &unset, depth)?;
        } else if value.is_instance(crate::pytypes::uuid_class(value.py())?)? {
            write_string(&mut self.out, &value.str()?.to_string())?;
        } else if value.is_instance(crate::pytypes::datetime_class(value.py())?)? {
            write_string(
                &mut self.out,
                &value.call_method0("isoformat")?.str()?.to_string(),
            )?;
        } else if let Some(text) = self.wire_text(value)? {
            write_string(&mut self.out, &text)?;
        } else if let Some(members) = self.set_members(value)? {
            self.replace(&members, depth)?;
        } else if let Some(member) = self.member_value(value)? {
            self.replace(&member, depth)?;
        } else if value.hasattr("dump")? {
            self.replace(&value.call_method0("dump")?, depth)?;
        } else if let Some(hook) = self.default.clone() {
            let replacement = hook.call1((value,))?;
            if replacement.is(value) {
                return Err(PyValueError::new_err(
                    "the default hook returned the same object",
                ));
            }
            self.replace(&replacement, depth)?;
        } else {
            let name = value.get_type().name()?;
            return Err(PyTypeError::new_err(format!(
                "{name} is not JSON serializable"
            )));
        }
        Ok(())
    }

    fn wire_text(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<String>> {
        if self.schemas == 0 {
            return Ok(None);
        }
        crate::schema::wire_text(value)
    }

    /// The members of a set or frozenset as a list, inside a schema instance.
    fn set_members(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let is_set = value.is_instance_of::<pyo3::types::PySet>()
            || value.is_instance_of::<pyo3::types::PyFrozenSet>();
        if self.schemas == 0 || !is_set {
            return Ok(None);
        }
        let members = crate::pytypes::sorted_members(value)?;
        Ok(Some(PyList::new(value.py(), members)?.into_any()))
    }

    /// The value an `Enum` member is written as, inside a schema instance.
    fn member_value(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        if self.schemas == 0 {
            return Ok(None);
        }
        crate::pytypes::enum_value(value)
    }

    fn schema_object(
        &mut self,
        value: &Bound<'py, PyAny>,
        names: Vec<(String, String)>,
        unset: &Py<PyAny>,
        depth: usize,
    ) -> PyResult<()> {
        self.schemas += 1;
        let result = self.schema_fields(value, names, unset, depth);
        self.schemas -= 1;
        result
    }

    fn schema_fields(
        &mut self,
        value: &Bound<'py, PyAny>,
        names: Vec<(String, String)>,
        unset: &Py<PyAny>,
        depth: usize,
    ) -> PyResult<()> {
        Self::enter(depth)?;
        let mut present = Vec::new();
        for (name, wire_name) in names {
            let item = value.getattr(name.as_str())?;
            if !item.is(unset) {
                present.push((wire_name, item));
            }
        }
        if self.sort_keys {
            present.sort_by(|a, b| a.0.cmp(&b.0));
        }
        let empty = present.is_empty();
        self.open(b'{');
        for (index, (name, item)) in present.into_iter().enumerate() {
            if index > 0 {
                self.out.push(b',');
            }
            self.newline(depth + 1);
            write_string(&mut self.out, &name)?;
            self.colon();
            self.write(&item, depth + 1)?;
        }
        self.close(b'}', depth, empty);
        Ok(())
    }
}

/// Serialise plain Python values (`dict`, `list`, `tuple`, `str`, `int`,
/// `float`, `bool`, `None`), schema instances, UUIDs and datetimes to UTF-8 JSON:
/// compact by default, `indent` spaces when given, `sort_keys` to order object
/// keys (by their text, like `json.dumps`), `default` to serialise anything
/// else, `ensure_ascii` to escape every non-ASCII character. `NaN` and infinities raise
/// `ValueError`; an unsupported value without a `default` raises `TypeError`.
#[pyfunction]
#[pyo3(signature = (value, indent=None, sort_keys=false, default=None, ensure_ascii=false))]
pub fn encode_json<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    indent: Option<usize>,
    sort_keys: bool,
    default: Option<Bound<'py, PyAny>>,
    ensure_ascii: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    if indent.is_some_and(|width| width > MAX_INDENT) {
        return Err(PyValueError::new_err(format!(
            "indent must be at most {MAX_INDENT}"
        )));
    }
    let mut encoder = Encoder {
        out: Vec::with_capacity(512),
        indent,
        sort_keys,
        default,
        hops: 0,
        schemas: 0,
        pairs: Vec::new(),
    };
    encoder.write(value, 0)?;
    if ensure_ascii {
        return Ok(PyBytes::new(py, &escape_non_ascii(&encoder.out)));
    }
    Ok(PyBytes::new(py, &encoder.out))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(a: &str, b: &str) -> (String, String) {
        (a.to_owned(), b.to_owned())
    }

    #[test]
    fn the_word_scan_finds_the_first_special_byte_like_a_byte_loop() {
        let naive = |bytes: &[u8]| {
            bytes
                .iter()
                .position(|&b| b < 0x20 || b == b'"' || b == b'\\')
                .unwrap_or(bytes.len())
        };
        for length in 0..20 {
            for position in 0..length {
                for byte in 0..=255u8 {
                    let mut bytes = vec![b'a'; length];
                    bytes[position] = byte;
                    bytes[length - 1] = if position + 1 < length { b'\\' } else { byte };
                    assert_eq!(special_at(&bytes), naive(&bytes), "{bytes:?}");
                }
            }
        }
        assert_eq!(special_at(b""), 0);
        assert_eq!(
            special_at("é日本語 plain".as_bytes()),
            "é日本語 plain".len()
        );
    }

    #[test]
    fn query_keeps_blank_values_decodes_and_orders() {
        let got = parse_query(b"a=1&b&c=&a=x%20y+z&&d=%zz", 100).unwrap();
        assert_eq!(
            got,
            vec![
                pair("a", "1"),
                pair("b", ""),
                pair("c", ""),
                pair("a", "x y z"),
                pair("d", "%zz")
            ]
        );
    }

    #[test]
    fn path_parameters_decode_without_form_query_rules() {
        assert_eq!(decode_path_parameter("a%2Fb+c"), Some("a/b+c".into()));
        assert_eq!(decode_path_parameter("bad%ff"), None);
    }

    #[test]
    fn query_rejects_non_ascii_and_bad_utf8() {
        assert!(parse_query("é=1".as_bytes(), 9).is_err());
        assert!(parse_query(b"a=%ff", 9).is_err());
        assert!(parse_query(b"%ff=1", 9).is_err());
    }

    #[test]
    fn a_trailing_percent_stays_literal() {
        assert_eq!(parse_query(b"a=%4", 9).unwrap(), vec![pair("a", "%4")]);
        assert_eq!(parse_query(b"a=%41", 9).unwrap(), vec![pair("a", "A")]);
    }

    #[test]
    fn cookies_first_wins_and_junk_is_ignored() {
        let got = parse_cookies("a=1; b=\"two\"; a=3; junk; =x");
        assert_eq!(got, vec![pair("a", "1"), pair("b", "two")]);
    }

    #[test]
    fn repeated_headers_are_joined_in_order() {
        let got = join_headers(&[
            (b"X-A".to_vec(), b"1".to_vec()),
            (b"x-b".to_vec(), b"2".to_vec()),
            (b"x-a".to_vec(), b"3".to_vec()),
        ]);
        assert_eq!(got, vec![pair("x-a", "1, 3"), pair("x-b", "2")]);
    }

    #[test]
    fn multipart_splits_fields_and_files() {
        let body = b"--XX\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nHello\r\n--XX\r\nContent-Disposition: form-data; name=\"doc\"; filename=\"a \\\"b\\\".txt\"\r\nContent-Type: text/csv\r\n\r\n1,2 a--XX b\r\n--XX--\r\nepilogue";
        let parts = parse_multipart(body, "XX", 10, 100).unwrap();
        assert_eq!(parts.len(), 2);
        assert_eq!(
            (parts[0].name.as_str(), parts[0].data.as_slice()),
            ("title", &b"Hello"[..])
        );
        assert_eq!(parts[0].filename, None);
        assert_eq!(parts[1].filename.as_deref(), Some("a \"b\".txt"));
        assert_eq!(parts[1].content_type, "text/csv");
        assert_eq!(parts[1].data, b"1,2 a--XX b");
    }

    #[test]
    fn multipart_rejects_malformed_and_oversized_bodies() {
        let ok = b"--B\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nxyz\r\n--B--";
        assert!(parse_multipart(ok, "B", 1, 3).is_ok());
        assert_eq!(
            parse_multipart(ok, "B", 1, 2),
            Err(MultipartError::LimitExceeded)
        );
        assert!(parse_multipart(ok, "B", 0, 3).is_err());
        assert!(parse_multipart(b"junk", "B", 1, 3).is_err());
        assert!(parse_multipart(
            b"--B\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nxyz",
            "B",
            1,
            9
        )
        .is_err());
        assert!(parse_multipart(
            b"--B\r\nContent-Disposition: attachment; name=\"a\"\r\n\r\nx\r\n--B--",
            "B",
            1,
            9
        )
        .is_err());
        assert!(parse_multipart(
            b"--B\r\nContent-Disposition: form-data\r\n\r\nx\r\n--B--",
            "B",
            1,
            9
        )
        .is_err());
        assert!(parse_multipart(ok, "", 1, 3).is_err());
        assert!(parse_multipart(ok, &"b".repeat(71), 1, 3).is_err());
    }

    fn offered(types: &[&str]) -> Vec<String> {
        types.iter().map(|t| (*t).to_owned()).collect()
    }

    #[test]
    fn accept_prefers_quality_then_specificity_then_order() {
        let both = offered(&["application/json", "text/csv"]);
        assert_eq!(negotiate("", &both), Some(0));
        assert_eq!(negotiate("*/*", &both), Some(0));
        assert_eq!(negotiate("text/csv", &both), Some(1));
        assert_eq!(
            negotiate("text/*;q=0.5, application/json;q=0.4", &both),
            Some(1)
        );
        assert_eq!(negotiate("*/*;q=0.1, text/csv", &both), Some(1));
        assert_eq!(negotiate("application/*, text/csv", &both), Some(1));
        assert_eq!(negotiate("TEXT/CSV ; Q=0.9", &both), Some(1));
    }

    #[test]
    fn accept_refuses_what_it_excludes() {
        let both = offered(&["application/json", "text/csv"]);
        assert_eq!(negotiate("text/html", &both), None);
        assert_eq!(negotiate("application/json;q=0", &both), None);
        assert_eq!(negotiate("*/*, application/json;q=0", &both), Some(1));
        assert_eq!(
            negotiate("application/json;q=abc, text/csv", &both),
            Some(1)
        );
        assert_eq!(negotiate("", &[]), None);
    }

    #[test]
    fn review_regressions() {
        assert!(parse_query(b"a=1&b=2&c=3", 2).is_err());
        assert!(parse_query(b"a=1&b=2", 2).is_ok());
        let split = join_headers(&[
            (b"Cookie".to_vec(), b"a=1".to_vec()),
            (b"cookie".to_vec(), b"session=abc".to_vec()),
        ]);
        assert_eq!(split, vec![pair("cookie", "a=1; session=abc")]);
        let csv = offered(&["text/csv; charset=utf-8"]);
        assert_eq!(negotiate("text/csv", &csv), Some(0));
        let json = offered(&["application/json"]);
        assert_eq!(
            negotiate("application/json, application/json;q=0", &json),
            Some(0)
        );
        let head = |h: &str| format!("--B\r\n{h}\r\n\r\nx\r\n--B--");
        let parse = |h: &str| parse_multipart(head(h).as_bytes(), "B", 5, 9);
        assert!(parse("Content-Disposition: Form-Data; name=\"a\"").is_ok());
        assert!(parse("Content-Disposition: form-data; name=\"a\"; name=\"b\"").is_err());
        assert!(parse("Content-Disposition: form-data; name=\"a\"; filename=\"x\ny\"").is_err());
        assert!(parse("Content-Disposition: form-data; name=\"a\"\nX: y").is_err());
    }

    #[test]
    fn multipart_rejects_repeated_part_content_type_headers() {
        for headers in [
            "Content-Type: text/plain\r\nContent-Type: text/plain",
            "Content-Type: text/plain\r\ncontent-type: application/octet-stream",
        ] {
            let body = format!(
                "--B\r\nContent-Disposition: form-data; name=\"f\"; filename=\"x\"\r\n{headers}\r\n\r\nx\r\n--B--"
            );
            assert_eq!(
                parse_multipart(body.as_bytes(), "B", 1, 1),
                Err(MultipartError::Invalid(
                    "duplicate Content-Type part header"
                ))
            );
        }
    }

    /// Both builders over the same text: the same error message, or values
    /// with the same Python `repr()` (which tells `1`, `1.0` and `True`
    /// apart and shows key order). `true` when the text was accepted.
    fn same_decoding(py: Python<'_>, text: &[u8]) -> bool {
        let python = decode_json(py, text);
        let native = decode_json_document(text);
        let shown = String::from_utf8_lossy(text);
        match (python, native) {
            (Ok(python), Ok(native)) => {
                let rebuilt = json_to_python(py, &native.root).unwrap();
                let repr = |value: &Bound<'_, PyAny>| value.repr().unwrap().to_string();
                assert_eq!(repr(&rebuilt), repr(&python), "{shown}");
                assert!(rebuilt.eq(&python).unwrap(), "{shown}");
                true
            }
            (Err(python), Err(native)) => {
                assert_eq!(python.value(py).to_string(), native, "{shown}");
                false
            }
            (python, native) => panic!("{shown}: {python:?} vs {native:?}"),
        }
    }

    const ACCEPTED: [&str; 24] = [
        "null",
        " true ",
        "false",
        "0",
        "-0",
        "-0.0",
        "1.5e3",
        "1E-2",
        "9223372036854775807",
        "-9223372036854775808",
        "9223372036854775808",
        "-123456789012345678901234567890",
        "1e308",
        "\"\"",
        r#""a\"b\\c\/d\b\f\n\r\t""#,
        r#""\u00e9\ud83d\ude00\u0000""#,
        "\"日本語 é\"",
        "[]",
        "[1, [2, [3]], {}]",
        r#"{"a": 1, "b": [true, null], "c": {"d": "e"}}"#,
        r#"{"k": 1, "j": 2, "k": 3}"#,
        r#"{"k": {"x": 1}, "k": [2]}"#,
        "\t\n\r [ ] \n",
        r#"{"é": "\u00e9", "": ""}"#,
    ];

    const REFUSED: [&str; 34] = [
        "",
        " ",
        "nul",
        "tru",
        "[1,]",
        "{\"a\":1,}",
        "{'a': 1}",
        "{\"a\" 1}",
        "{1: 2}",
        "[1 2]",
        "01",
        "1.",
        ".5",
        "-",
        "1e",
        "1e+",
        "NaN",
        "Infinity",
        "1e400",
        "-1e400",
        "\"\\ud800\"",
        "\"\\ud800\\u0041\"",
        "\"\\udc00\"",
        "\"\\x41\"",
        "\"\\u12\"",
        "\"\\u12g4\"",
        "\"abc",
        "\"a\\n",
        "\"a\\nb\u{1}\"",
        "\"a\u{1}b\"",
        "\u{feff}{}",
        "{} {}",
        "[1] x",
        "/* c */ 1",
    ];

    #[test]
    fn the_json_tree_decodes_exactly_like_decode_json() {
        Python::initialize();
        Python::attach(|py| {
            for text in ACCEPTED {
                assert!(same_decoding(py, text.as_bytes()), "{text} should decode");
            }
            for text in REFUSED {
                assert!(!same_decoding(py, text.as_bytes()), "{text} is refused");
            }
            let deep = "[".repeat(128) + &"]".repeat(128);
            assert!(same_decoding(py, deep.as_bytes()));
            let deeper = "[".repeat(129) + &"]".repeat(129);
            assert!(!same_decoding(py, deeper.as_bytes()));
            assert!(!same_decoding(py, b"\"\xff\""));
            assert!(!same_decoding(py, b"{\"\xc3\x28\": 1}"));
        });
    }

    #[test]
    fn the_json_tree_keeps_python_dict_semantics_and_flags_big_integers() {
        let document = decode_json_document(br#"{"b": 1, "a": 2, "b": 3}"#).unwrap();
        let Json::Object(object) = &document.root else {
            panic!("an object");
        };
        assert_eq!(object.keys().collect::<Vec<_>>(), vec!["b", "a"]);
        assert_eq!(object.get("b"), Some(&Json::Int(3)));
        assert!(object.contains("a") && !object.contains("c"));
        assert!(!document.big_integers);
        let big = decode_json_document(b"[1, 99999999999999999999]").unwrap();
        assert!(big.big_integers);
        assert_eq!(
            big.root,
            Json::List(vec![
                Json::Int(1),
                Json::BigInt("99999999999999999999".into())
            ])
        );
    }
}
