//! Safe XML request-body parsing: no DTDs, no custom entities, bounded depth and
//! element count. Elements map to a tree of `dict`, `list`, `str` and `None`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};
use quick_xml::events::Event;
use quick_xml::Reader;

enum Value {
    Empty,
    Text(String),
    Map(Vec<(String, Value)>),
}

struct Frame {
    name: String,
    children: Vec<(String, Value)>,
    text: String,
}

fn fail<T>(message: &str) -> Result<T, PyErr> {
    Err(PyValueError::new_err(message.to_owned()))
}

fn finish(frame: Frame) -> Result<Value, PyErr> {
    if !frame.children.is_empty() {
        if !frame.text.trim().is_empty() {
            return fail("an element mixes text and child elements");
        }
        return Ok(Value::Map(frame.children));
    }
    Ok(if frame.text.is_empty() {
        Value::Empty
    } else {
        Value::Text(frame.text)
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

struct Builder {
    stack: Vec<Frame>,
    root: Option<Value>,
    elements: usize,
    max_depth: usize,
    max_elements: usize,
}

impl Builder {
    fn start(&mut self, name: &str) -> Result<(), PyErr> {
        self.elements += 1;
        if self.elements > self.max_elements {
            return fail("too many elements");
        }
        if self.stack.len() >= self.max_depth || (self.stack.is_empty() && self.root.is_some()) {
            return fail("XML is nested too deeply or has several roots");
        }
        self.stack.push(Frame {
            name: name.to_owned(),
            children: Vec::new(),
            text: String::new(),
        });
        Ok(())
    }

    fn end(&mut self) -> Result<(), PyErr> {
        let frame = self
            .stack
            .pop()
            .ok_or_else(|| PyValueError::new_err("malformed XML"))?;
        let name = frame.name.clone();
        let value = finish(frame)?;
        match self.stack.last_mut() {
            Some(parent) => parent.children.push((name, value)),
            None => self.root = Some(value),
        }
        Ok(())
    }

    fn text(&mut self, piece: &str) -> Result<(), PyErr> {
        match self.stack.last_mut() {
            Some(frame) => frame.text.push_str(piece),
            None if !piece.trim().is_empty() => return fail("text outside the root"),
            None => {}
        }
        Ok(())
    }

    fn reference(&mut self, name: &str, as_char: Option<char>) -> Result<(), PyErr> {
        let Some(frame) = self.stack.last_mut() else {
            return fail("entity outside the root");
        };
        if let Some(found) = as_char {
            frame.text.push(found);
        } else if let Some(text) = entity(name) {
            frame.text.push_str(text);
        } else {
            return fail("custom entities are not supported");
        }
        Ok(())
    }

    fn finished(self) -> Result<Value, PyErr> {
        match (self.stack.is_empty(), self.root) {
            (true, Some(value)) => Ok(value),
            _ => fail("malformed XML"),
        }
    }
}

fn parse(raw: &[u8], max_depth: usize, max_elements: usize) -> Result<Value, PyErr> {
    let mut reader = Reader::from_reader(raw);
    let config = reader.config_mut();
    config.expand_empty_elements = true;
    config.check_end_names = true;
    let mut builder = Builder {
        stack: Vec::new(),
        root: None,
        elements: 0,
        max_depth,
        max_elements,
    };
    loop {
        let event = reader
            .read_event()
            .map_err(|_| PyValueError::new_err("malformed XML"))?;
        match event {
            Event::Start(start) => builder.start(start.local_name().into_inner())?,
            Event::End(_) => builder.end()?,
            Event::Text(text) => builder.text(&text.xml10_content())?,
            Event::CData(data) => builder.text(&data.xml10_content())?,
            Event::GeneralRef(reference) => {
                builder.reference(&reference, reference.resolve_char_ref().ok().flatten())?
            }
            Event::DocType(_) => return fail("DTDs are not supported"),
            Event::Eof => return builder.finished(),
            _ => {}
        }
    }
}

/// Children grouped by name in first-seen order; a repeated name becomes a list.
fn grouped(children: Vec<(String, Value)>) -> Vec<(String, Vec<Value>)> {
    let mut groups: Vec<(String, Vec<Value>)> = Vec::new();
    for (name, value) in children {
        match groups.iter_mut().find(|(seen, _)| *seen == name) {
            Some((_, values)) => values.push(value),
            None => groups.push((name, vec![value])),
        }
    }
    groups
}

fn to_python<'py>(py: Python<'py>, value: Value) -> PyResult<Bound<'py, PyAny>> {
    Ok(match value {
        Value::Empty => py.None().into_bound(py),
        Value::Text(text) => PyString::new(py, &text).into_any(),
        Value::Map(children) => {
            let result = PyDict::new(py);
            for (name, mut values) in grouped(children) {
                if values.len() == 1 {
                    result.set_item(&name, to_python(py, values.remove(0))?)?;
                } else {
                    let list = PyList::empty(py);
                    for item in values {
                        list.append(to_python(py, item)?)?;
                    }
                    result.set_item(&name, list)?;
                }
            }
            result.into_any()
        }
    })
}

/// The tree under the root element of an XML document, as Python values.
#[pyfunction]
pub fn parse_xml_body(
    py: Python<'_>,
    raw: &[u8],
    max_depth: usize,
    max_elements: usize,
) -> PyResult<Py<PyAny>> {
    let value = parse(raw, max_depth, max_elements)?;
    Ok(to_python(py, value)?.unbind())
}
