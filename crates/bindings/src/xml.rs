//! Safe XML request-body parsing: no DTDs, no custom entities, only characters
//! XML 1.0 allows, UTF-8 only, bounded depth and element count. Elements map to
//! a tree of `dict`, `list`, `str` and `None`.
//!
//! The document is parsed without the GIL into a flat list of nodes in
//! post-order (every child before its parent), so neither building the Python
//! values nor dropping the tree recurses, however deep the nesting.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};
use quick_xml::events::{BytesDecl, Event};
use quick_xml::Reader;

type Outcome<T> = Result<T, &'static str>;

enum Node {
    Empty,
    Text(String),
    /// Child element names with the index of their node.
    Map(Vec<(String, usize)>),
}

struct Frame {
    name: String,
    children: Vec<(String, usize)>,
    text: String,
}

fn finish(frame: Frame) -> Outcome<Node> {
    if !frame.children.is_empty() {
        if !frame.text.trim().is_empty() {
            return Err("an element mixes text and child elements");
        }
        return Ok(Node::Map(frame.children));
    }
    Ok(if frame.text.is_empty() {
        Node::Empty
    } else {
        Node::Text(frame.text)
    })
}

fn entity(name: &str) -> Option<&'static str> {
    match name {
        "lt" => Some("<"),
        "gt" => Some(">"),
        "amp" => Some("&"),
        "apos" => Some("'"),
        "quot" => Some("\""),
        _ => None,
    }
}

/// Outside XML 1.0's `Char` production (Rust strings hold no surrogates).
fn forbidden(found: char) -> bool {
    matches!(
        found,
        '\u{0}'..='\u{8}' | '\u{B}' | '\u{C}' | '\u{E}'..='\u{1F}' | '\u{FFFE}' | '\u{FFFF}'
    )
}

fn allowed(text: &str) -> Outcome<&str> {
    if text.chars().any(forbidden) {
        return Err("XML 1.0 does not allow this character");
    }
    Ok(text)
}

/// Bodies are read as UTF-8, so a declaration naming another encoding is refused
/// rather than ignored.
fn utf8_declared(declaration: &BytesDecl<'_>) -> Outcome<()> {
    match declaration.encoding() {
        None => Ok(()),
        Some(Ok(name)) if name.eq_ignore_ascii_case("utf-8") => Ok(()),
        Some(_) => Err("only UTF-8 XML is supported"),
    }
}

struct Builder {
    stack: Vec<Frame>,
    nodes: Vec<Node>,
    root: Option<usize>,
    max_depth: usize,
    max_elements: usize,
}

impl Builder {
    fn start(&mut self, name: &str) -> Outcome<()> {
        if self.nodes.len() + self.stack.len() >= self.max_elements {
            return Err("too many elements");
        }
        if self.stack.len() >= self.max_depth || (self.stack.is_empty() && self.root.is_some()) {
            return Err("XML is nested too deeply or has several roots");
        }
        self.stack.push(Frame {
            name: name.to_owned(),
            children: Vec::new(),
            text: String::new(),
        });
        Ok(())
    }

    fn end(&mut self) -> Outcome<()> {
        let frame = self.stack.pop().ok_or("malformed XML")?;
        let name = frame.name.clone();
        let index = self.nodes.len();
        self.nodes.push(finish(frame)?);
        match self.stack.last_mut() {
            Some(parent) => parent.children.push((name, index)),
            None => self.root = Some(index),
        }
        Ok(())
    }

    fn text(&mut self, piece: &str) -> Outcome<()> {
        let piece = allowed(piece)?;
        match self.stack.last_mut() {
            Some(frame) => frame.text.push_str(piece),
            None if !piece.trim().is_empty() => return Err("text outside the root"),
            None => {}
        }
        Ok(())
    }

    fn reference(&mut self, name: &str, as_char: Option<char>) -> Outcome<()> {
        let Some(frame) = self.stack.last_mut() else {
            return Err("entity outside the root");
        };
        match (as_char, entity(name)) {
            (Some(found), _) if forbidden(found) => {
                return Err("XML 1.0 does not allow this character")
            }
            (Some(found), _) => frame.text.push(found),
            (None, Some(text)) => frame.text.push_str(text),
            (None, None) => return Err("custom entities are not supported"),
        }
        Ok(())
    }

    fn finished(self) -> Outcome<Vec<Node>> {
        // Nothing may start after the root ends, so the root is the last node.
        match (self.stack.is_empty(), self.root) {
            (true, Some(_)) => Ok(self.nodes),
            _ => Err("malformed XML"),
        }
    }
}

/// The document's nodes in post-order; the root is the last one.
fn parse(raw: &[u8], max_depth: usize, max_elements: usize) -> Outcome<Vec<Node>> {
    let mut reader = Reader::from_reader(raw);
    let config = reader.config_mut();
    config.expand_empty_elements = true;
    config.check_end_names = true;
    let mut builder = Builder {
        stack: Vec::new(),
        nodes: Vec::new(),
        root: None,
        max_depth,
        max_elements,
    };
    loop {
        let event = reader.read_event().map_err(|_| "malformed XML")?;
        match event {
            Event::Start(start) => builder.start(start.local_name().into_inner())?,
            Event::End(_) => builder.end()?,
            Event::Text(text) => builder.text(&text.xml10_content())?,
            Event::CData(data) => builder.text(&data.xml10_content())?,
            Event::GeneralRef(reference) => {
                builder.reference(&reference, reference.resolve_char_ref().ok().flatten())?
            }
            Event::Decl(declaration) => utf8_declared(&declaration)?,
            Event::DocType(_) => return Err("DTDs are not supported"),
            Event::Eof => return builder.finished(),
            _ => {}
        }
    }
}

/// Children grouped by name in first-seen order; a repeated name becomes a list.
fn grouped(children: Vec<(String, usize)>) -> Vec<(String, Vec<usize>)> {
    let mut groups: Vec<(String, Vec<usize>)> = Vec::new();
    let mut seen: HashMap<String, usize> = HashMap::with_capacity(children.len());
    for (name, child) in children {
        match seen.get(&name) {
            Some(&group) => groups[group].1.push(child),
            None => {
                seen.insert(name.clone(), groups.len());
                groups.push((name, vec![child]));
            }
        }
    }
    groups
}

fn taken<'py>(built: &mut [Option<Bound<'py, PyAny>>], index: usize) -> Bound<'py, PyAny> {
    built[index]
        .take()
        .expect("a child node is built before its parent and used once")
}

fn mapping<'py>(
    py: Python<'py>,
    children: Vec<(String, usize)>,
    built: &mut [Option<Bound<'py, PyAny>>],
) -> PyResult<Bound<'py, PyAny>> {
    let result = PyDict::new(py);
    for (name, indexes) in grouped(children) {
        if let [only] = indexes[..] {
            result.set_item(&name, taken(built, only))?;
            continue;
        }
        let list = PyList::empty(py);
        for index in indexes {
            list.append(taken(built, index))?;
        }
        result.set_item(&name, list)?;
    }
    Ok(result.into_any())
}

/// Python values for every node in order, so each child exists before its parent.
fn to_python(py: Python<'_>, nodes: Vec<Node>) -> PyResult<Bound<'_, PyAny>> {
    let mut built: Vec<Option<Bound<'_, PyAny>>> = Vec::with_capacity(nodes.len());
    for node in nodes {
        let value = match node {
            Node::Empty => py.None().into_bound(py),
            Node::Text(text) => PyString::new(py, &text).into_any(),
            Node::Map(children) => mapping(py, children, &mut built)?,
        };
        built.push(Some(value));
    }
    let root = built
        .pop()
        .flatten()
        .ok_or_else(|| PyValueError::new_err("malformed XML"))?;
    Ok(root)
}

/// The tree under the root element of an XML document, as Python values. The
/// text is parsed with the GIL released; only building the values holds it.
#[pyfunction]
pub fn parse_xml_body(
    py: Python<'_>,
    raw: &[u8],
    max_depth: usize,
    max_elements: usize,
) -> PyResult<Py<PyAny>> {
    let nodes = py
        .detach(|| parse(raw, max_depth, max_elements))
        .map_err(PyValueError::new_err)?;
    Ok(to_python(py, nodes)?.unbind())
}
