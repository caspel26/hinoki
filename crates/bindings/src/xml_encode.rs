//! XML response encoding. A value is classified like `encode_json` classifies it
//! (plain values, schema instances, UUIDs, datetimes, `dump()` objects, and the
//! wire forms of `date`, `Decimal`, sets and enums inside a schema instance), so
//! the two encoders accept the same set of types. The writer holds the GIL: it
//! reads Python objects as it goes.
//!
//! Element names are ASCII; a mapping key that is not a name is written as
//! `<item key="...">`. Characters XML 1.0 cannot carry become U+FFFD, so the
//! output is always well-formed.

use std::borrow::Cow;
use std::rc::Rc;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::request::{
    float_text, int_text, is_instance_of_named, key_text, string_bytes, MAX_DEPTH,
};

const DECLARATION: &[u8] = br#"<?xml version="1.0" encoding="UTF-8"?>"#;

/// ASCII `[A-Za-z_][A-Za-z0-9_.-]*` that does not start with `xml` in any case.
fn name_ok(name: &str) -> bool {
    let mut bytes = name.bytes();
    let first = matches!(bytes.next(), Some(b'A'..=b'Z' | b'a'..=b'z' | b'_'));
    first
        && bytes.all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
        && !name.get(..3).is_some_and(|p| p.eq_ignore_ascii_case("xml"))
}

/// Whether `name` is usable as the `root` or `item` element name.
#[pyfunction]
pub fn xml_name_ok(name: &str) -> bool {
    name_ok(name)
}

/// A character XML 1.0 cannot carry: C0 controls other than tab, line feed and
/// carriage return, and U+FFFE and U+FFFF. (Surrogates cannot be a `char`.)
fn unwritable(c: char) -> bool {
    (c < ' ' && !matches!(c, '\t' | '\n' | '\r')) || matches!(c, '\u{fffe}' | '\u{ffff}')
}

fn reference(c: char) -> Option<&'static str> {
    Some(match c {
        '&' => "&amp;",
        '<' => "&lt;",
        '>' => "&gt;",
        '"' => "&quot;",
        '\t' => "&#9;",
        '\n' => "&#10;",
        '\r' => "&#13;",
        _ => return None,
    })
}

/// Escaped text, safe in content and in attributes. Tab and line breaks are
/// written as references so a parser's whitespace normalisation keeps them.
fn push_escaped(out: &mut Vec<u8>, text: &str) {
    let mut run = 0;
    for (at, c) in text.char_indices() {
        let replacement = if unwritable(c) {
            "\u{fffd}"
        } else if let Some(entity) = reference(c) {
            entity
        } else {
            continue;
        };
        out.extend_from_slice(&text.as_bytes()[run..at]);
        out.extend_from_slice(replacement.as_bytes());
        run = at + c.len_utf8();
    }
    out.extend_from_slice(&text.as_bytes()[run..]);
}

/// UTF-8 that may hold lone surrogates in their 3-byte form (what
/// `string_bytes` returns), each one replaced by U+FFFD.
fn lossy(bytes: &[u8]) -> Cow<'_, str> {
    let Err(first) = std::str::from_utf8(bytes) else {
        return String::from_utf8_lossy(bytes);
    };
    let mut out = String::with_capacity(bytes.len());
    let (mut rest, mut error) = (bytes, Some(first));
    while let Some(found) = error {
        let (valid, tail) = rest.split_at(found.valid_up_to());
        out.push_str(&String::from_utf8_lossy(valid));
        out.push('\u{fffd}');
        rest = tail.get(3..).unwrap_or_default();
        error = std::str::from_utf8(rest).err();
    }
    out.push_str(&String::from_utf8_lossy(rest));
    Cow::Owned(out)
}

fn text_of<'a>(text: &'a Bound<'_, PyString>) -> PyResult<Cow<'a, str>> {
    match text.to_str() {
        Ok(valid) => Ok(Cow::Borrowed(valid)),
        Err(_) => Ok(Cow::Owned(lossy(&string_bytes(text)?).into_owned())),
    }
}

fn integer_text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    int_text(value).map_err(|_| PyValueError::new_err("the integer is too large to write as XML"))
}

fn number_text(value: f64) -> PyResult<String> {
    if value.is_finite() {
        return float_text(value);
    }
    Ok(match value {
        v if v.is_nan() => "NaN",
        v if v > 0.0 => "INF",
        _ => "-INF",
    }
    .to_owned())
}

/// The text of a scalar: `None` for anything that is not one.
fn scalar_text<'a>(value: &'a Bound<'_, PyAny>) -> PyResult<Option<Cow<'a, str>>> {
    if let Ok(flag) = value.cast::<PyBool>() {
        return Ok(Some(Cow::Borrowed(if flag.is_true() {
            "true"
        } else {
            "false"
        })));
    }
    if value.is_instance_of::<PyInt>() {
        return Ok(Some(Cow::Owned(integer_text(value)?)));
    }
    if let Ok(float) = value.cast::<PyFloat>() {
        return Ok(Some(Cow::Owned(number_text(float.value())?)));
    }
    match value.cast::<PyString>() {
        Ok(text) => text_of(text).map(Some),
        Err(_) => Ok(None),
    }
}

/// An element's name, and the mapping key it stands for when that key is not a
/// valid name.
struct Tag<'a> {
    name: &'a str,
    key: Option<&'a str>,
}

struct XmlEncoder<'py> {
    out: Vec<u8>,
    item: Rc<str>,
    hops: usize,
    _py: std::marker::PhantomData<Bound<'py, PyAny>>,
}

impl<'py> XmlEncoder<'py> {
    fn start(&mut self, tag: &Tag<'_>) {
        self.out.push(b'<');
        self.out.extend_from_slice(tag.name.as_bytes());
        if let Some(key) = tag.key {
            self.out.extend_from_slice(b" key=\"");
            push_escaped(&mut self.out, key);
            self.out.push(b'"');
        }
    }

    fn open(&mut self, tag: &Tag<'_>) {
        self.start(tag);
        self.out.push(b'>');
    }

    fn close(&mut self, tag: &Tag<'_>) {
        self.out.extend_from_slice(b"</");
        self.out.extend_from_slice(tag.name.as_bytes());
        self.out.push(b'>');
    }

    fn leaf(&mut self, tag: &Tag<'_>, text: &str) {
        self.open(tag);
        push_escaped(&mut self.out, text);
        self.close(tag);
    }

    fn element(&mut self, tag: &Tag<'_>, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        if depth > MAX_DEPTH {
            return Err(PyValueError::new_err(
                "the value is nested too deeply for XML",
            ));
        }
        if value.is_none() {
            self.start(tag);
            self.out.extend_from_slice(b"/>");
        } else if let Some(text) = scalar_text(value)? {
            self.leaf(tag, &text);
        } else if let Ok(dict) = value.cast::<PyDict>() {
            self.mapping(tag, dict, depth)?;
        } else if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
            self.sequence(tag, value, depth)?;
        } else {
            self.object(tag, value, depth)?;
        }
        Ok(())
    }

    fn mapping(&mut self, tag: &Tag<'_>, dict: &Bound<'py, PyDict>, depth: usize) -> PyResult<()> {
        let mut members = Vec::with_capacity(dict.len());
        for (key, item) in dict.iter() {
            members.push((lossy(&key_text(&key)?).into_owned(), item));
        }
        self.members(tag, members, depth)
    }

    /// Children named by their key, or `<item key="...">` when it is no name.
    fn members(
        &mut self,
        tag: &Tag<'_>,
        members: Vec<(String, Bound<'py, PyAny>)>,
        depth: usize,
    ) -> PyResult<()> {
        let item = Rc::clone(&self.item);
        self.open(tag);
        for (key, value) in &members {
            let child = if name_ok(key) {
                Tag {
                    name: key,
                    key: None,
                }
            } else {
                Tag {
                    name: &item,
                    key: Some(key),
                }
            };
            self.element(&child, value, depth + 1)?;
        }
        self.close(tag);
        Ok(())
    }

    fn sequence(&mut self, tag: &Tag<'_>, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        let item = Rc::clone(&self.item);
        let child = Tag {
            name: &item,
            key: None,
        };
        self.open(tag);
        for member in value.try_iter()? {
            self.element(&child, &member?, depth + 1)?;
        }
        self.close(tag);
        Ok(())
    }

    /// Write what `dump()` or a wire form returned in place of a value, with a
    /// separate budget so a `dump()` that keeps producing new objects ends in an
    /// error instead of overflowing the stack.
    fn replace(&mut self, tag: &Tag<'_>, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        self.hops += 1;
        if self.hops > MAX_DEPTH {
            return Err(PyValueError::new_err(
                "the value is nested too deeply for XML",
            ));
        }
        let result = self.element(tag, value, depth);
        self.hops -= 1;
        result
    }

    /// Objects that are not plain values, as `encode_json` orders them.
    fn object(&mut self, tag: &Tag<'_>, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        if let Some(text) = exact_text(value)? {
            self.leaf(tag, &text);
        } else if let Some((source, declared)) = crate::schema::narrowed_parts(value) {
            // A subclass instance where its base class is declared.
            match crate::schema::class_fields(&declared, true, true, true)? {
                Some(fields) => self.schema_object(tag, &source, fields, depth)?,
                None => {
                    // The declared class decides its own shape, not the subclass.
                    let dumped =
                        crate::schema::declared_dump(&source, &declared, None, &[], false, true)?;
                    self.replace(tag, &dumped, depth)?;
                }
            }
        } else if let Some(fields) =
            crate::schema::class_fields(&value.get_type(), true, true, true)?
        {
            self.schema_object(tag, value, fields, depth)?;
        } else if let Some(text) = self.instance_text(value)? {
            self.leaf(tag, &text);
        } else if let Some(other) = self.substitute(value)? {
            self.replace(tag, &other, depth)?;
        } else {
            let name = value.get_type().name()?;
            return Err(PyTypeError::new_err(format!(
                "{name} cannot be rendered as XML"
            )));
        }
        Ok(())
    }

    /// UUID and datetime subclasses, then the wire text of `date`, `Decimal`...
    fn instance_text(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<String>> {
        if is_instance_of_named(value, "uuid", "UUID")? {
            return Ok(Some(value.str()?.to_string()));
        }
        if is_instance_of_named(value, "datetime", "datetime")? {
            return Ok(Some(value.call_method0("isoformat")?.str()?.to_string()));
        }
        crate::schema::wire_text(value)
    }

    /// What stands in for a set, an enum member or a `dump()` object.
    fn substitute(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let is_set = value.is_instance_of::<pyo3::types::PySet>()
            || value.is_instance_of::<pyo3::types::PyFrozenSet>();
        if is_set {
            let members = crate::pytypes::sorted_members(value)?;
            return Ok(Some(PyList::new(value.py(), members)?.into_any()));
        }
        if let Some(member) = crate::pytypes::enum_value(value)? {
            return Ok(Some(member));
        }
        if value.hasattr("dump")? {
            return value.call_method0("dump").map(Some);
        }
        Ok(None)
    }

    fn schema_object(
        &mut self,
        tag: &Tag<'_>,
        value: &Bound<'py, PyAny>,
        (names, unset, _, guides): (
            Vec<(String, String)>,
            Py<PyAny>,
            usize,
            crate::schema::FieldGuides,
        ),
        depth: usize,
    ) -> PyResult<()> {
        let mut present = Vec::with_capacity(names.len());
        for (index, (name, wire_name)) in names.into_iter().enumerate() {
            let mut item = value.getattr(name.as_str())?;
            if item.is(&unset) {
                continue;
            }
            if let Some(Some(guide)) = guides.get(index) {
                item = crate::schema::narrow(&item, guide, false)?.unwrap_or(item);
            }
            present.push((wire_name, item));
        }
        self.members(tag, present, depth)
    }
}

/// The text of an exact UUID or datetime, which cannot be schema instances.
fn exact_text(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if let Some(text) = crate::pytypes::exact_uuid_text(value)? {
        return Ok(Some(text.to_string()));
    }
    if value
        .get_type()
        .is(crate::pytypes::datetime_class(value.py())?)
    {
        return Ok(Some(value.call_method0("isoformat")?.str()?.to_string()));
    }
    Ok(None)
}

/// Serialise a value to UTF-8 XML with a declaration: a mapping becomes child
/// elements named by its keys, a list repeated `item` elements and `None` an
/// empty element. Accepts what `encode_json` accepts, without a `default` hook.
/// Raises `ValueError` for an invalid `root` or `item`, nesting deeper than 200
/// levels or an integer of more than 4300 digits, and `TypeError` for a value
/// or key it cannot write.
#[pyfunction]
#[pyo3(signature = (value, root="response", item="item"))]
pub fn encode_xml<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    root: &str,
    item: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    if !(name_ok(root) && name_ok(item)) {
        return Err(PyValueError::new_err(
            "root and item must be valid XML element names",
        ));
    }
    let mut encoder = XmlEncoder {
        out: Vec::with_capacity(128),
        item: Rc::from(item),
        hops: 0,
        _py: std::marker::PhantomData,
    };
    encoder.out.extend_from_slice(DECLARATION);
    encoder.element(
        &Tag {
            name: root,
            key: None,
        },
        value,
        0,
    )?;
    Ok(PyBytes::new(py, &encoder.out))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn escaped(text: &str) -> String {
        let mut out = Vec::new();
        push_escaped(&mut out, text);
        String::from_utf8(out).unwrap()
    }

    #[test]
    fn names_are_ascii_and_never_start_with_xml() {
        for good in ["a", "_a", "a1.b-c", "Order", "xm", "xnl"] {
            assert!(name_ok(good), "{good}");
        }
        for bad in [
            "", "1a", "a b", "a:b", "xml", "XMLx", "xMl1", "é", "a\n", "-a",
        ] {
            assert!(!name_ok(bad), "{bad}");
        }
    }

    #[test]
    fn text_is_escaped_and_unwritable_characters_are_replaced() {
        assert_eq!(escaped("plain é 日本"), "plain é 日本");
        assert_eq!(escaped("<&>\"'"), "&lt;&amp;&gt;&quot;'");
        assert_eq!(escaped("\t\n\r"), "&#9;&#10;&#13;");
        assert_eq!(
            escaped("a\u{0}b\u{1f}\u{fffe}\u{ffff}c"),
            "a\u{fffd}b\u{fffd}\u{fffd}\u{fffd}c"
        );
        assert_eq!(
            escaped("\u{7f}\u{85}\u{fffd}\u{10ffff}"),
            "\u{7f}\u{85}\u{fffd}\u{10ffff}"
        );
    }

    #[test]
    fn each_lone_surrogate_becomes_one_replacement_character() {
        let bytes = [
            b'a', 0xed, 0xa0, 0x80, b'b', 0xed, 0xb0, 0x80, 0xed, 0xa0, 0x80,
        ];
        assert_eq!(lossy(&bytes), "a\u{fffd}b\u{fffd}\u{fffd}");
        assert_eq!(lossy("é".as_bytes()), "é");
    }
}
