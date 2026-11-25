use std::borrow::Cow;
use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};

use bytes::Bytes;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use super::parse::hex;

pub(crate) const MAX_DEPTH: usize = 200;

pyo3::create_exception!(core, JsonDecodeError, PyValueError);

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

fn quoted_json_key(text: &str) -> Box<[u8]> {
    let mut out = Vec::with_capacity(text.len() + 2);
    out.push(b'"');
    push_escaped(&mut out, text);
    out.push(b'"');
    out.into_boxed_slice()
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
pub(crate) fn string_bytes<'a>(text: &'a Bound<'_, PyString>) -> PyResult<Cow<'a, [u8]>> {
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

/// The text of a finite float, as `serde_json` writes a `Number`; the XML
/// encoder shares it.
pub(crate) fn float_text(value: f64) -> PyResult<String> {
    serde_json::Number::from_f64(value)
        .map(|number| number.to_string())
        .ok_or_else(|| PyValueError::new_err("out of range float values are not JSON compliant"))
}

/// Decimal text of a Python int, immune to a subclass overriding `__str__`.
pub(crate) fn int_text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let repr = value.py().get_type::<PyInt>().getattr("__repr__")?;
    repr.call1((value,))?.extract()
}

/// The bytes a dict key is written as; `sort_keys` on string keys orders by
/// them, which is the code point order `json.dumps` uses.
pub(crate) fn key_text(key: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
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
    /// Schema instances being written. Inside one, `date`, `Decimal`, sets,
    /// enum members... always take their wire form; outside, a `default` hook
    /// decides first, and without one they take it too.
    schemas: usize,
    /// Fused validate-and-encode of a `list[dict[str, object]]` (see
    /// [`encode_json_vec_strict`]): the items, every dict and every key must be
    /// exact builtins; `str`/`int`/`float` subclass values are accepted (no user
    /// code runs), anything else is a miss.
    strict: bool,
    /// Dict entries being written, one slice per open dict.
    pairs: Vec<Option<(Bound<'py, PyAny>, Bound<'py, PyAny>)>>,
    /// One-entry cache of the last schema class encoded, avoiding repeated
    /// plan lookups when encoding homogeneous lists of `Schema` instances.
    last_schema: Option<(
        Bound<'py, pyo3::types::PyType>,
        std::sync::Arc<crate::schema::JsonFieldPlan>,
    )>,
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

    /// An exact `int` that fits `i64` is formatted here; big ints take the
    /// `int.__repr__` path.
    fn int_exact(&mut self, value: &Bound<'py, PyAny>) -> PyResult<()> {
        if let Ok(number) = value.extract::<i64>() {
            push_i64(&mut self.out, number);
            return Ok(());
        }
        self.out.extend_from_slice(int_text(value)?.as_bytes());
        Ok(())
    }

    /// An exact `int` that fits `i64` is formatted here; big ints and subclasses
    /// (which may override `__str__`) take the `int.__repr__` path.
    fn int(&mut self, value: &Bound<'py, PyAny>) -> PyResult<()> {
        if value.is_exact_instance_of::<PyInt>() {
            return self.int_exact(value);
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
            self.int_exact(value)
        } else if value.is_exact_instance_of::<PyFloat>() {
            push_float(&mut self.out, value.cast::<PyFloat>()?.value())
        } else if let Ok(dict) = value.cast::<PyDict>() {
            self.mapping(dict, depth)
        } else if value.is_exact_instance_of::<PyList>() || value.is_exact_instance_of::<PyTuple>()
        {
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
        if self.strict && !dict.is_exact_instance_of::<PyDict>() {
            return Err(strict_miss());
        }
        if self.sort_keys {
            return self.sorted_mapping(dict, depth);
        }
        // Entries go on a shared stack first: writing a value may run Python
        // code (`default`, `dump()`) that mutates the dict being walked.
        let start = self.pairs.len();
        self.pairs.extend(dict.iter().map(Some));
        let end = self.pairs.len();
        self.open(b'{');
        let result = self.mapping_entries(start, end, depth);
        self.pairs.truncate(start);
        result?;
        self.close(b'}', depth, start == end);
        Ok(())
    }

    fn mapping_entries(&mut self, start: usize, end: usize, depth: usize) -> PyResult<()> {
        for at in start..end {
            let Some((key, item)) = self.pairs[at].take() else {
                continue;
            };
            if at > start {
                self.out.push(b',');
            }
            self.newline(depth + 1);
            self.write_key(&key)?;
            self.colon();
            self.write(&item, depth + 1)?;
        }
        Ok(())
    }

    fn write_key(&mut self, key: &Bound<'py, PyAny>) -> PyResult<()> {
        if self.strict && !key.is_exact_instance_of::<PyString>() {
            return Err(strict_miss());
        }
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
            // Read by position with an explicit length check: avoids raising
            // and discarding a Python IndexError at the end of every list while
            // staying safe if a hook mutates the list mid-walk.
            let list = value.cast::<PyList>()?;
            while count < list.len() {
                let item = list.get_item(count)?;
                self.element(&item, count, depth)?;
                count += 1;
            }
        } else if value.is_exact_instance_of::<PyTuple>() {
            let tuple = value.cast::<PyTuple>()?;
            let len = tuple.len();
            while count < len {
                let item = tuple.get_item(count)?;
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
        if self.strict && depth == 0 && !item.is_exact_instance_of::<PyDict>() {
            return Err(strict_miss());
        }
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

    fn write_isoformat(&mut self, value: &Bound<'py, PyAny>) -> PyResult<()> {
        let iso = value.call_method0(intern!(value.py(), "isoformat"))?;
        match iso.cast::<PyString>() {
            Ok(text) => write_pystring(&mut self.out, text),
            Err(_) => write_string(&mut self.out, &iso.str()?.to_string()),
        }
    }

    fn try_preencoded(&mut self, value: &Bound<'py, PyAny>) -> PyResult<bool> {
        if self.indent.is_some() || self.sort_keys || self.default.is_some() {
            return Ok(false);
        }
        if let Some(handle) = preencoded_handle(value)? {
            self.out.extend_from_slice(&handle.json);
            return Ok(true);
        }
        Ok(false)
    }

    /// Objects that are not plain JSON values: schema instances, UUIDs, datetimes,
    /// anything with a `dump()` method, then the caller's `default` hook.
    fn object(&mut self, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        if self.strict {
            return Err(strict_miss());
        }
        if self.try_preencoded(value)? {
            return Ok(());
        }
        let value_type = value.get_type();
        if let Some((cached_type, plan)) = &self.last_schema {
            if value_type.is(cached_type) {
                let plan = std::sync::Arc::clone(plan);
                return self.schema_object(value, &plan, depth);
            }
        }
        if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
            return self.sequence(value, depth);
        }
        // Exact UUIDs and datetimes cannot be schema instances: skip that lookup.
        if let Some(text) = crate::pytypes::exact_uuid_text(value)? {
            return write_string(&mut self.out, &text);
        }
        if value_type.is(crate::pytypes::datetime_class(value.py())?) {
            return self.write_isoformat(value);
        }
        if let Some(plan) = crate::schema::json_field_plan(value, quoted_json_key)? {
            self.last_schema = Some((value_type, std::sync::Arc::clone(&plan)));
            return self.schema_object(value, &plan, depth);
        }
        self.fallback_object(value, depth)
    }

    fn fallback_object(&mut self, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        if value.is_instance(crate::pytypes::uuid_class(value.py())?)? {
            write_string(&mut self.out, &value.str()?.to_string())?;
        } else if value.is_instance(crate::pytypes::datetime_class(value.py())?)? {
            self.write_isoformat(value)?;
        } else if let Some(text) = self.wire_text(value)? {
            write_string(&mut self.out, &text)?;
        } else if let Some(members) = self.set_members(value)? {
            self.replace(&members, depth)?;
        } else if let Some(member) = self.member_value(value)? {
            self.replace(&member, depth)?;
        } else if value.hasattr("dump")? {
            if !self.page(value, depth)? {
                self.replace(&value.call_method0("dump")?, depth)?;
            }
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

    fn wire_forms(&self) -> bool {
        self.schemas > 0 || self.default.is_none()
    }

    /// The wire text of a `date`, `time`, `timedelta`, `Decimal`, `bytes` and
    /// the other text scalars, at any depth, the top level included. A
    /// `Secret` is always masked: it never reaches the `default` hook.
    fn wire_text(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<String>> {
        if value.is_instance(crate::pytypes::secret_class(value.py())?)? {
            return Ok(Some(crate::schema::SECRET_MASK.to_owned()));
        }
        if !self.wire_forms() {
            return Ok(None);
        }
        crate::schema::wire_text(value)
    }

    /// The members of a set or frozenset as a sorted list.
    fn set_members(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let is_set = value.is_instance_of::<pyo3::types::PySet>()
            || value.is_instance_of::<pyo3::types::PyFrozenSet>();
        if !is_set || !self.wire_forms() {
            return Ok(None);
        }
        let members = crate::pytypes::sorted_members(value)?;
        Ok(Some(PyList::new(value.py(), members)?.into_any()))
    }

    /// The value an `Enum` member is written as.
    fn member_value(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        if !self.wire_forms() {
            return Ok(None);
        }
        crate::pytypes::enum_value(value)
    }

    /// A `Page` (exactly that class) whose items are all schema instances the
    /// encoder writes itself: its `dump()` keys with the items left as they are,
    /// so they are written once instead of first being dumped to dicts. The
    /// bytes are the same; anything else answers `false` and is dumped.
    fn page(&mut self, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<bool> {
        let py = value.py();
        if !value.get_type().is(crate::pytypes::page_class(py)?) {
            return Ok(false);
        }
        let items = value.getattr(intern!(py, "items"))?;
        let Ok(rows) = items.cast::<PyList>() else {
            return Ok(false);
        };
        if !all_native_schemas(rows)? {
            return Ok(false);
        }
        let shown = PyDict::new(py);
        shown.set_item(intern!(py, "items"), rows)?;
        for key in ["page", "page_size", "total", "pages"] {
            shown.set_item(key, value.getattr(key)?)?;
        }
        self.replace(shown.as_any(), depth)?;
        Ok(true)
    }

    fn schema_object(
        &mut self,
        value: &Bound<'py, PyAny>,
        plan: &crate::schema::JsonFieldPlan,
        depth: usize,
    ) -> PyResult<()> {
        self.schemas += 1;
        let result = self.schema_fields(value, plan, depth);
        self.schemas -= 1;
        result
    }

    fn schema_fields(
        &mut self,
        value: &Bound<'py, PyAny>,
        plan: &crate::schema::JsonFieldPlan,
        depth: usize,
    ) -> PyResult<()> {
        Self::enter(depth)?;
        if plan.streamable && !self.sort_keys && self.default.is_none() {
            self.schema_fields_streaming(value, plan, depth)
        } else {
            self.schema_fields_staged(value, plan, depth)
        }
    }

    fn schema_fields_streaming(
        &mut self,
        value: &Bound<'py, PyAny>,
        plan: &crate::schema::JsonFieldPlan,
        depth: usize,
    ) -> PyResult<()> {
        let py = value.py();
        self.open(b'{');
        let mut written = false;
        for entry in &plan.entries {
            let item = value.getattr(entry.attr.bind(py))?;
            if item.is(&plan.unset) {
                continue;
            }
            if written {
                self.out.push(b',');
            }
            self.newline(depth + 1);
            self.out.extend_from_slice(&entry.quoted_key);
            self.colon();
            self.write(&item, depth + 1)?;
            written = true;
        }
        self.close(b'}', depth, !written);
        Ok(())
    }

    fn schema_fields_staged(
        &mut self,
        value: &Bound<'py, PyAny>,
        plan: &crate::schema::JsonFieldPlan,
        depth: usize,
    ) -> PyResult<()> {
        let py = value.py();
        let mut present = Vec::with_capacity(plan.entries.len());
        for entry in &plan.entries {
            let item = value.getattr(entry.attr.bind(py))?;
            if !item.is(&plan.unset) {
                present.push((entry, item));
            }
        }
        if self.sort_keys {
            present.sort_by(|a, b| a.0.wire_name.cmp(&b.0.wire_name));
        }
        let empty = present.is_empty();
        self.open(b'{');
        for (index, (entry, item)) in present.into_iter().enumerate() {
            if index > 0 {
                self.out.push(b',');
            }
            self.newline(depth + 1);
            self.out.extend_from_slice(&entry.quoted_key);
            self.colon();
            self.write(&item, depth + 1)?;
        }
        self.close(b'}', depth, empty);
        Ok(())
    }
}

fn all_native_schemas(rows: &Bound<'_, PyList>) -> PyResult<bool> {
    let mut verified_type: Option<Bound<'_, pyo3::types::PyType>> = None;
    for row in rows.iter() {
        let row_type = row.get_type();
        if verified_type.as_ref().is_some_and(|seen| row_type.is(seen)) {
            continue;
        }
        if crate::schema::json_field_plan(&row, quoted_json_key)?.is_none() {
            return Ok(false);
        }
        verified_type = Some(row_type);
    }
    Ok(true)
}

/// Whether `value` is an instance of `module.class`, imported on demand; the
/// XML encoder uses it for UUIDs and datetimes.
pub(crate) fn is_instance_of_named(
    value: &Bound<'_, PyAny>,
    module: &str,
    class: &str,
) -> PyResult<bool> {
    let wanted = value.py().import(module)?.getattr(class)?;
    value.is_instance(&wanted)
}

fn preencoded_handle<'py>(
    value: &Bound<'py, PyAny>,
) -> PyResult<Option<PyRef<'py, crate::rows::PyProjectedRows>>> {
    if let Ok(handle) = value.extract::<PyRef<'py, crate::rows::PyProjectedRows>>() {
        return Ok(Some(handle));
    }
    let py = value.py();
    let ty = value.get_type();
    if !ty.is(crate::pytypes::projected_list_class(py)?)
        && !ty.is(crate::pytypes::projected_page_class(py)?)
        && !ty.is(crate::pytypes::projected_item_class(py)?)
    {
        return Ok(None);
    }
    let found = value.getattr(intern!(py, "_projected_handle"))?;
    if found.is_none() {
        return Ok(None);
    }
    Ok(found
        .extract::<PyRef<'py, crate::rows::PyProjectedRows>>()
        .ok())
}

fn is_plain_builtin(value: &Bound<'_, PyAny>) -> bool {
    value.is_exact_instance_of::<PyDict>()
        || value.is_exact_instance_of::<PyList>()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_none()
}

fn strict_miss() -> PyErr {
    PyTypeError::new_err("the value is not a plain list of dicts with str keys")
}

fn encode_into<'py>(
    value: &Bound<'py, PyAny>,
    indent: Option<usize>,
    sort_keys: bool,
    default: Option<Bound<'py, PyAny>>,
    strict: bool,
) -> PyResult<Bytes> {
    if indent.is_none() && !sort_keys && default.is_none() && !is_plain_builtin(value) {
        if let Some(handle) = preencoded_handle(value)? {
            return Ok(handle.json.clone());
        }
    }
    static LAST_LEN: AtomicUsize = AtomicUsize::new(512);
    let capacity = LAST_LEN.load(Ordering::Relaxed).clamp(512, 64 * 1024);
    let mut encoder = Encoder {
        out: Vec::with_capacity(capacity),
        indent,
        sort_keys,
        default,
        hops: 0,
        schemas: 0,
        strict,
        pairs: Vec::new(),
        last_schema: None,
    };
    encoder.write(value, 0)?;
    LAST_LEN.store(encoder.out.len(), Ordering::Relaxed);
    Ok(Bytes::from(encoder.out))
}

pub(crate) fn encode_json_bytes(value: &Bound<'_, PyAny>) -> PyResult<Bytes> {
    encode_into(value, None, false, None, false)
}

/// One-pass check and encode of a `list[dict[str, object]]` response: the same
/// bytes as [`encode_json_bytes`] when the value is an exact `list` of exact `dict`
/// items whose keys, at every depth, are exact `str`, and no value needs Python
/// code to be written. Anything else, or any encode error, is a miss: an error
/// the caller answers by running the checked, non-strict path.
pub(crate) fn encode_json_bytes_strict(value: &Bound<'_, PyAny>) -> PyResult<Bytes> {
    if !value.is_exact_instance_of::<PyList>() {
        return Err(strict_miss());
    }
    encode_into(value, None, false, None, true)
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
    let out = encode_into(value, indent, sort_keys, default, false)?;
    if ensure_ascii {
        return Ok(PyBytes::new(py, &escape_non_ascii(&out)));
    }
    Ok(PyBytes::new(py, &out))
}

#[cfg(test)]
mod tests {
    use super::*;

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

    #[test]
    fn encode_json_bytes_matches_encode_json_and_recovers_from_errors() {
        Python::initialize();
        Python::attach(|py| {
            let nested = PyDict::new(py);
            nested.set_item("k", "v").unwrap();
            let plain = PyDict::new(py);
            plain.set_item("a", 1).unwrap();
            plain.set_item("b", "text").unwrap();
            plain.set_item("nested", nested).unwrap();
            let via_bytes = encode_json_bytes(plain.as_any()).unwrap();
            let via_py = encode_json(py, plain.as_any(), None, false, None, false).unwrap();
            assert_eq!(via_bytes.as_ref(), via_py.as_bytes());

            let failing = PyDict::new(py);
            failing.set_item("ok", 1).unwrap();
            failing.set_item("bad", f64::NAN).unwrap();
            assert!(encode_json_bytes(failing.as_any()).is_err());
            assert_eq!(encode_json_bytes(plain.as_any()).unwrap(), via_bytes);
        });
    }

    #[test]
    fn preencoded_projected_rows_share_underlying_buffer_without_copy() {
        Python::initialize();
        Python::attach(|py| {
            let payload = Bytes::from(vec![b'['; 4096]);
            let original_ptr = payload.as_ptr();
            let rows = Bound::new(
                py,
                crate::rows::PyProjectedRows::new(
                    payload,
                    hinoki_orm::RowSet {
                        columns: std::sync::Arc::from([]),
                        rows: Vec::new(),
                    },
                    Vec::new(),
                    None,
                ),
            )
            .unwrap();
            let encoded = encode_json_bytes(rows.as_any()).unwrap();
            assert_eq!(encoded.len(), 4096);
            assert_eq!(encoded.as_ptr(), original_ptr);
        });
    }

    #[test]
    fn strict_encoding_matches_the_plain_bytes_and_misses_everything_else() {
        Python::initialize();
        Python::attach(|py| {
            let first = PyDict::new(py);
            first.set_item("id", 1).unwrap();
            first.set_item("name", "caf\u{e9} \"x\"").unwrap();
            first.set_item("score", 1.5).unwrap();
            first
                .set_item("tags", PyList::new(py, ["a", "b"]).unwrap())
                .unwrap();
            first.set_item("none", py.None()).unwrap();
            let rows = PyList::new(py, [first, PyDict::new(py)]).unwrap();
            let plain = encode_json_bytes(rows.as_any()).unwrap();
            assert_eq!(encode_json_bytes_strict(rows.as_any()).unwrap(), plain);
            let empty = PyList::empty(py);
            assert_eq!(
                encode_json_bytes_strict(empty.as_any()).unwrap().as_ref(),
                b"[]"
            );

            let copy = rows.call_method0("copy").unwrap();
            assert!(encode_json_bytes_strict(&copy).is_ok());
            let not_list = pyo3::types::PyTuple::new(py, [PyDict::new(py)]).unwrap();
            assert!(encode_json_bytes_strict(not_list.as_any()).is_err());
            assert!(encode_json_bytes_strict(PyDict::new(py).as_any()).is_err());

            let int_key = PyDict::new(py);
            int_key.set_item(1, "v").unwrap();
            let nested_key = PyDict::new(py);
            nested_key.set_item("in", int_key.clone()).unwrap();
            for bad in [
                PyList::new(py, [1]).unwrap(),
                PyList::new(py, [int_key]).unwrap(),
                PyList::new(py, [nested_key]).unwrap(),
            ] {
                assert!(encode_json_bytes_strict(bad.as_any()).is_err());
            }
            let nan = PyDict::new(py);
            nan.set_item("x", f64::NAN).unwrap();
            let nan_rows = PyList::new(py, [nan]).unwrap();
            assert!(encode_json_bytes_strict(nan_rows.as_any()).is_err());
        });
    }
}
