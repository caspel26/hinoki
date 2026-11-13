//! Request decoding and JSON, in Rust: query strings, cookies, repeated
//! headers, JSON into Python objects and Python objects into JSON bytes.
//!
//! The pure functions at the top have no Python dependency and carry the unit
//! tests; the `#[pyfunction]`s below only convert between Python and them. The
//! ASGI scope itself is still a Python object, so this removes the parsing and
//! serialising work from Python, not the crossing.

use std::collections::HashMap;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

const MAX_DEPTH: usize = 200;

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

/// `a=1&b=2&a=3` into ordered pairs. Blank values are kept, empty pairs are
/// skipped, the input must be ASCII and every decoded part valid UTF-8.
pub fn parse_query(raw: &[u8]) -> Result<Vec<(String, String)>, &'static str> {
    if !raw.is_ascii() {
        return Err("query string is not ASCII");
    }
    let mut pairs = Vec::new();
    for part in raw.split(|&b| b == b'&').filter(|p| !p.is_empty()) {
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
pub fn join_headers(headers: &[(Vec<u8>, Vec<u8>)]) -> Vec<(String, String)> {
    let mut joined: Vec<(String, String)> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    for (name, value) in headers {
        let name: String = name
            .iter()
            .map(|&b| char::from(b))
            .collect::<String>()
            .to_lowercase();
        let value: String = value.iter().map(|&b| char::from(b)).collect();
        match index.get(&name) {
            Some(&at) => {
                joined[at].1.push_str(", ");
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
    for line in text.split("\r\n") {
        let Some((key, value)) = line.split_once(':') else {
            return Err("malformed part header");
        };
        match key.trim().to_lowercase().as_str() {
            "content-disposition" => {
                if value.split(';').next().map(str::trim) != Some("form-data") {
                    return Err("part is not form-data");
                }
                for (param, found) in header_params(value) {
                    match param.as_str() {
                        "name" => name = Some(found),
                        "filename" => filename = Some(found),
                        _ => {}
                    }
                }
            }
            "content-type" => content_type = value.trim().to_owned(),
            _ => {}
        }
    }
    Ok((name.ok_or("part has no name")?, filename, content_type))
}

/// Split a `multipart/form-data` body (RFC 7578). Bounded: at most `max_parts`
/// parts and `max_part_bytes` per part; the whole body is already capped by the
/// caller. A file name is returned as sent and must never be used as a path.
pub fn parse_multipart(
    body: &[u8],
    boundary: &str,
    max_parts: usize,
    max_part_bytes: usize,
) -> Result<Vec<Part>, &'static str> {
    if boundary.is_empty() || boundary.len() > MAX_BOUNDARY || boundary.bytes().any(|b| b < 0x20) {
        return Err("invalid multipart boundary");
    }
    let opening = [b"--".as_slice(), boundary.as_bytes()].concat();
    let delimiter = [b"\r\n".as_slice(), &opening].concat();
    if !body.starts_with(&opening) {
        return Err("multipart body does not start with the boundary");
    }
    let mut at = opening.len();
    let mut parts = Vec::new();
    loop {
        if body[at..].starts_with(b"--") {
            return Ok(parts);
        }
        if !body[at..].starts_with(b"\r\n") {
            return Err("malformed multipart delimiter");
        }
        at += 2;
        if parts.len() >= max_parts {
            return Err("too many multipart parts");
        }
        let end = find(body, b"\r\n\r\n", at).ok_or("unterminated part headers")?;
        if end - at > MAX_HEADER_BLOCK {
            return Err("part headers are too large");
        }
        let (name, filename, content_type) = part_headers(&body[at..end])?;
        at = end + 4;
        let stop = find(body, &delimiter, at).ok_or("multipart body is not terminated")?;
        if stop - at > max_part_bytes {
            return Err("multipart part is too large");
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
        parse_multipart(raw, boundary, max_parts, max_part_bytes).map_err(PyValueError::new_err)?;
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
    let ranges: Vec<(String, f32)> = accept
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
        .collect();
    let mut best: Option<(f32, u8, usize)> = None;
    for (index, offered) in available.iter().enumerate() {
        let offered = offered.to_lowercase();
        let group = offered.split('/').next().unwrap_or("");
        let matched = ranges
            .iter()
            .filter_map(|(media, quality)| {
                let specificity = if *media == offered {
                    3
                } else if media.strip_suffix("/*") == Some(group) {
                    2
                } else if media == "*/*" {
                    1
                } else {
                    return None;
                };
                Some((specificity, *quality))
            })
            .max_by_key(|(specificity, _)| *specificity);
        if let Some((specificity, quality)) = matched {
            let better = match best {
                None => true,
                Some((q, spec, _)) => quality > q || (quality == q && specificity > spec),
            };
            if quality > 0.0 && better {
                best = Some((quality, specificity, index));
            }
        }
    }
    best.map(|(_, _, index)| index)
}

/// Index into `available` of the best match for an `Accept` header, or `None`.
#[pyfunction]
pub fn negotiate_media(accept: &str, available: Vec<String>) -> Option<usize> {
    negotiate(accept, &available)
}

#[pyfunction]
pub fn parse_query_string(py: Python<'_>, raw: &[u8]) -> PyResult<Py<PyDict>> {
    let pairs = parse_query(raw).map_err(PyValueError::new_err)?;
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
pub fn header_dict(py: Python<'_>, headers: Vec<(Vec<u8>, Vec<u8>)>) -> PyResult<Py<PyDict>> {
    let result = PyDict::new(py);
    for (name, value) in join_headers(&headers) {
        result.set_item(name, value)?;
    }
    Ok(result.unbind())
}

const MAX_PARSE_DEPTH: usize = 128;

/// Recursive-descent JSON parser that builds Python objects directly. Strict
/// RFC 8259: no comments, no trailing commas, no `NaN`/`Infinity`, no BOM,
/// valid UTF-8 and paired surrogates only. Integers of any size are exact.
struct Parser<'a, 'py> {
    py: Python<'py>,
    input: &'a [u8],
    at: usize,
}

type Parsed<T> = Result<T, String>;

impl<'a, 'py> Parser<'a, 'py> {
    fn fail<T>(&self, what: &str) -> Parsed<T> {
        Err(format!("{what} at byte {}", self.at))
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

    fn value(&mut self, depth: usize) -> Parsed<Bound<'py, PyAny>> {
        if depth > MAX_PARSE_DEPTH {
            return self.fail("nesting too deep");
        }
        self.skip_space();
        match self.peek() {
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            Some(b'"') => Ok(PyString::new(self.py, &self.string()?).into_any()),
            Some(b't') => self
                .expect(b"true")
                .map(|()| PyBool::new(self.py, true).to_owned().into_any()),
            Some(b'f') => self
                .expect(b"false")
                .map(|()| PyBool::new(self.py, false).to_owned().into_any()),
            Some(b'n') => self
                .expect(b"null")
                .map(|()| self.py.None().into_bound(self.py)),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => self.fail("unexpected character"),
        }
    }

    fn object(&mut self, depth: usize) -> Parsed<Bound<'py, PyAny>> {
        self.at += 1;
        let dict = PyDict::new(self.py);
        self.skip_space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(dict.into_any());
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
            dict.set_item(key, item).map_err(|e| e.to_string())?;
            self.skip_space();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b'}') => {
                    self.at += 1;
                    return Ok(dict.into_any());
                }
                _ => return self.fail("expected ',' or '}'"),
            }
        }
    }

    fn array(&mut self, depth: usize) -> Parsed<Bound<'py, PyAny>> {
        self.at += 1;
        let list = PyList::empty(self.py);
        self.skip_space();
        if self.peek() == Some(b']') {
            self.at += 1;
            return Ok(list.into_any());
        }
        loop {
            let item = self.value(depth + 1)?;
            list.append(item).map_err(|e| e.to_string())?;
            self.skip_space();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b']') => {
                    self.at += 1;
                    return Ok(list.into_any());
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

    fn string(&mut self) -> Parsed<String> {
        self.at += 1;
        let mut out = String::new();
        loop {
            let start = self.at;
            while let Some(byte) = self.peek() {
                if byte == b'"' || byte == b'\\' || byte < 0x20 {
                    break;
                }
                self.at += 1;
            }
            out.push_str(
                std::str::from_utf8(&self.input[start..self.at]).map_err(|_| "invalid UTF-8")?,
            );
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
        }
    }

    fn digits(&mut self) -> Parsed<()> {
        let start = self.at;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.at += 1;
        }
        if self.at == start {
            return self.fail("expected digits");
        }
        Ok(())
    }

    fn number(&mut self) -> Parsed<Bound<'py, PyAny>> {
        let start = self.at;
        if self.peek() == Some(b'-') {
            self.at += 1;
        }
        if self.peek() == Some(b'0') {
            self.at += 1;
        } else {
            self.digits()?;
        }
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
        let text =
            std::str::from_utf8(&self.input[start..self.at]).map_err(|_| "invalid number")?;
        if real {
            let float = text.parse::<f64>().map_err(|e| e.to_string())?;
            return Ok(PyFloat::new(self.py, float).into_any());
        }
        if let Ok(int) = text.parse::<i64>() {
            return Ok(PyInt::new(self.py, int).into_any());
        }
        self.py
            .get_type::<PyInt>()
            .call1((text,))
            .map_err(|e| e.to_string())
    }
}

/// Parse a JSON document into Python objects. Raises `ValueError` for syntax
/// errors, invalid UTF-8, `NaN`/`Infinity`, unpaired surrogates and nesting
/// deeper than 128 levels.
#[pyfunction]
pub fn decode_json<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let mut parser = Parser {
        py,
        input: raw,
        at: 0,
    };
    let value = parser.value(0).map_err(PyValueError::new_err)?;
    parser.skip_space();
    if parser.at != raw.len() {
        return Err(PyValueError::new_err(format!(
            "trailing data at byte {}",
            parser.at
        )));
    }
    Ok(value)
}

fn write_string(out: &mut Vec<u8>, text: &str) -> PyResult<()> {
    serde_json::to_writer(out, text).map_err(|e| PyValueError::new_err(e.to_string()))
}

fn write_key(out: &mut Vec<u8>, key: &Bound<'_, PyAny>) -> PyResult<()> {
    if let Ok(text) = key.cast::<PyString>() {
        return write_string(out, text.to_str()?);
    }
    if key.is_instance_of::<PyBool>() {
        return write_string(out, if key.is_truthy()? { "true" } else { "false" });
    }
    if key.is_instance_of::<PyInt>() {
        return write_string(out, &key.str()?.to_string());
    }
    if key.is_none() {
        return write_string(out, "null");
    }
    Err(PyTypeError::new_err("keys must be str, int, bool or None"))
}

fn write_value(out: &mut Vec<u8>, value: &Bound<'_, PyAny>, depth: usize) -> PyResult<()> {
    if depth > MAX_DEPTH {
        return Err(PyValueError::new_err("value is nested too deeply"));
    }
    if value.is_none() {
        out.extend_from_slice(b"null");
    } else if let Ok(flag) = value.cast::<PyBool>() {
        out.extend_from_slice(if flag.is_true() { b"true" } else { b"false" });
    } else if value.is_instance_of::<PyInt>() {
        out.extend_from_slice(value.str()?.to_string().as_bytes());
    } else if let Ok(float) = value.cast::<PyFloat>() {
        let number = serde_json::Number::from_f64(float.value()).ok_or_else(|| {
            PyValueError::new_err("out of range float values are not JSON compliant")
        })?;
        out.extend_from_slice(number.to_string().as_bytes());
    } else if let Ok(text) = value.cast::<PyString>() {
        write_string(out, text.to_str()?)?;
    } else if let Ok(dict) = value.cast::<PyDict>() {
        out.push(b'{');
        for (index, (key, item)) in dict.iter().enumerate() {
            if index > 0 {
                out.push(b',');
            }
            write_key(out, &key)?;
            out.push(b':');
            write_value(out, &item, depth + 1)?;
        }
        out.push(b'}');
    } else if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        out.push(b'[');
        for (index, item) in value.try_iter()?.enumerate() {
            if index > 0 {
                out.push(b',');
            }
            write_value(out, &item?, depth + 1)?;
        }
        out.push(b']');
    } else {
        let name = value.get_type().name()?;
        return Err(PyTypeError::new_err(format!(
            "{name} is not JSON serializable"
        )));
    }
    Ok(())
}

/// Serialise plain Python values (`dict`, `list`, `tuple`, `str`, `int`,
/// `float`, `bool`, `None`) to compact UTF-8 JSON. `NaN` and infinities raise
/// `ValueError`; anything else raises `TypeError`.
#[pyfunction]
pub fn encode_json<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    let mut out = Vec::with_capacity(128);
    write_value(&mut out, value, 0)?;
    Ok(PyBytes::new(py, &out))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(a: &str, b: &str) -> (String, String) {
        (a.to_owned(), b.to_owned())
    }

    #[test]
    fn query_keeps_blank_values_decodes_and_orders() {
        let got = parse_query(b"a=1&b&c=&a=x%20y+z&&d=%zz").unwrap();
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
    fn query_rejects_non_ascii_and_bad_utf8() {
        assert!(parse_query("é=1".as_bytes()).is_err());
        assert!(parse_query(b"a=%ff").is_err());
        assert!(parse_query(b"%ff=1").is_err());
    }

    #[test]
    fn a_trailing_percent_stays_literal() {
        assert_eq!(parse_query(b"a=%4").unwrap(), vec![pair("a", "%4")]);
        assert_eq!(parse_query(b"a=%41").unwrap(), vec![pair("a", "A")]);
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
        assert!(parse_multipart(ok, "B", 1, 2).is_err());
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
}
