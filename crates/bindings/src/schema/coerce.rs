//! Text-source coercion: turns the text leaves of a form, query or XML tree
//! into the scalar types a schema declares, so the strict validator can then
//! accept them. Only the sources that carry nothing but text use it.
//!
//! The rules are the ones that convert path and query parameters
//! (`hinoki_server::inputs`). A value that does not convert is left as it is:
//! the validator then reports it with its ordinary type error.

use hinoki_server::inputs::{convert, Converted, InputValue};
use hinoki_server::plan::InputKind;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

use super::{Definition, Kind, Node, PyRules, MAX_DEPTH};

type Defs = [Definition<PyRules>];

pub(super) fn coerce_object<'py>(
    py: Python<'py>,
    defs: &Defs,
    index: usize,
    data: &Bound<'py, PyAny>,
    use_aliases: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let Ok(dict) = data.cast::<PyDict>() else {
        return Ok(data.clone());
    };
    if depth > MAX_DEPTH {
        return Ok(data.clone());
    }
    let out = PyDict::new(py);
    for (key, value) in dict.iter() {
        let field = key.extract::<String>().ok().and_then(|name| {
            defs[index].fields.iter().find(|f| {
                name.as_str()
                    == if use_aliases {
                        f.wire_name.as_str()
                    } else {
                        f.name.as_str()
                    }
            })
        });
        let value = match field {
            Some(field) => coerce_node(py, defs, &field.node, &value, use_aliases, depth)?,
            None => value,
        };
        out.set_item(key, value)?;
    }
    Ok(out.into_any())
}

fn coerce_node<'py>(
    py: Python<'py>,
    defs: &Defs,
    node: &Node<PyRules>,
    value: &Bound<'py, PyAny>,
    use_aliases: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    match &node.kind {
        Kind::List(item) => coerce_list(py, defs, item, value, use_aliases, depth),
        Kind::Schema(index) => coerce_object(py, defs, *index, value, use_aliases, depth + 1),
        Kind::TaggedUnion {
            discriminator_wire_name,
            variants,
            ..
        } => {
            let tag = value
                .cast::<PyDict>()
                .ok()
                .and_then(|dict| dict.get_item(discriminator_wire_name).ok().flatten())
                .and_then(|tag| tag.extract::<String>().ok());
            match variants.iter().find(|(name, _)| Some(name) == tag.as_ref()) {
                Some((_, index)) => coerce_object(py, defs, *index, value, use_aliases, depth + 1),
                None => Ok(value.clone()),
            }
        }
        scalar => coerce_scalar(py, scalar, &single(value)),
    }
}

fn coerce_list<'py>(
    py: Python<'py>,
    defs: &Defs,
    item: &Node<PyRules>,
    value: &Bound<'py, PyAny>,
    use_aliases: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    if value.is_none() {
        return Ok(value.clone());
    }
    let items = PyList::empty(py);
    if let Ok(list) = value.cast::<PyList>() {
        for entry in list.iter() {
            items.append(coerce_node(py, defs, item, &entry, use_aliases, depth + 1)?)?;
        }
    } else {
        items.append(coerce_node(py, defs, item, value, use_aliases, depth + 1)?)?;
    }
    Ok(items.into_any())
}

/// A one-item list stands for its item: a form field is always a list of text.
fn single<'py>(value: &Bound<'py, PyAny>) -> Bound<'py, PyAny> {
    match value.cast::<PyList>() {
        Ok(list) if list.len() == 1 => list.get_item(0).unwrap_or_else(|_| value.clone()),
        _ => value.clone(),
    }
}

fn coerce_scalar<'py>(
    py: Python<'py>,
    kind: &Kind<PyRules>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let input = match kind {
        Kind::Int => InputKind::Int,
        Kind::Float => InputKind::Float,
        Kind::Bool => InputKind::Bool,
        _ => return Ok(value.clone()),
    };
    let Ok(text) = value.cast::<PyString>() else {
        return Ok(value.clone());
    };
    let raw = text.to_cow()?.into_owned();
    match convert(input, raw) {
        Converted::Value(InputValue::Int(number)) => Ok(PyInt::new(py, number).into_any()),
        Converted::Value(InputValue::Float(number)) => Ok(PyFloat::new(py, number).into_any()),
        Converted::Value(InputValue::Bool(flag)) => Ok(PyBool::new(py, flag).to_owned().into_any()),
        // Python's own integer limits decide an integer too long for `i64`.
        Converted::Defer => Ok(py
            .get_type::<PyInt>()
            .call1((text,))
            .unwrap_or_else(|_| value.clone())),
        _ => Ok(value.clone()),
    }
}
