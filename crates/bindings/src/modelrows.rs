//! Rows to finished model instances, built in one pass on the loop thread:
//! the generated constructor, the JSON decode and the stored-value snapshot
//! of `Model.from_row` / `remember`, without the intermediate row dicts.

use std::sync::Arc;

use hinoki_orm::{DbError, ErrorKind, Value};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList};

use crate::db::{db_error, from_value};
use crate::request::{decode_json, JsonDecodeError};

pub(crate) struct ModelRows {
    cls: Py<PyAny>,
    name: String,
    columns: Vec<String>,
    json: Vec<bool>,
    snapshot_key: String,
}

#[pyclass(name = "_ModelRows", module = "hinoki.core")]
pub(crate) struct PyModelRows {
    pub(crate) inner: Arc<ModelRows>,
}

#[pymethods]
impl PyModelRows {
    /// `columns` is the model's complete SQL projection, in order.
    #[new]
    fn new(
        cls: &Bound<'_, PyAny>,
        columns: Vec<String>,
        json_columns: Vec<String>,
        snapshot_key: String,
    ) -> PyResult<Self> {
        let json = columns.iter().map(|c| json_columns.contains(c)).collect();
        Ok(Self {
            inner: Arc::new(ModelRows {
                cls: cls.clone().unbind(),
                name: cls.getattr("__name__")?.extract()?,
                columns,
                json,
                snapshot_key,
            }),
        })
    }
}

fn mismatch(py: Python<'_>, plan: &ModelRows) -> PyErr {
    let message = format!(
        "{}: the result does not match the model's columns",
        plan.name
    );
    db_error(py, &DbError::new(ErrorKind::Type, message))
}

fn field_error(py: Python<'_>, plan: &ModelRows, column: &str) -> PyErr {
    let message = format!("{}.{column}: database value is not valid JSON", plan.name);
    match py
        .import("hinoki.errors")
        .and_then(|m| m.getattr("FieldError"))
        .and_then(|class| class.call1((message,)))
    {
        Ok(error) => PyErr::from_value(error),
        Err(err) => err,
    }
}

/// The Python value of a JSON column: text and bytes are decoded, any other
/// cell (already structured) is converted as usual.
fn json_cell<'py>(
    py: Python<'py>,
    plan: &ModelRows,
    column: &str,
    value: &Value,
) -> PyResult<Bound<'py, PyAny>> {
    let decoded = match value {
        Value::Text(text) => decode_json(py, text.as_bytes()),
        Value::Bytes(bytes) => decode_json(py, bytes),
        other => return Ok(from_value(py, other)?.into_bound(py)),
    };
    match decoded {
        Ok(found) => Ok(found),
        Err(err) if err.is_instance_of::<JsonDecodeError>(py) => Err(field_error(py, plan, column)),
        Err(err) => Err(err),
    }
}

fn is_scalar(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<pyo3::types::PyString>()
        || value.is_exact_instance_of::<pyo3::types::PyBool>()
        || value.is_exact_instance_of::<pyo3::types::PyInt>()
        || value.is_exact_instance_of::<pyo3::types::PyFloat>()
}

fn build_one<'py>(
    py: Python<'py>,
    plan: &ModelRows,
    row: &[(String, Value)],
    new: &Bound<'py, PyAny>,
    deepcopy: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if row.len() != plan.columns.len() {
        return Err(mismatch(py, plan));
    }
    let instance = new.call1((plan.cls.bind(py),))?;
    let stored = instance.getattr("__dict__")?.cast_into::<PyDict>()?;
    let snapshot = PyDict::new(py);
    for (((name, value), column), is_json) in row.iter().zip(&plan.columns).zip(&plan.json) {
        if name != column {
            return Err(mismatch(py, plan));
        }
        let cell = if *is_json {
            json_cell(py, plan, column, value)?
        } else {
            from_value(py, value)?.into_bound(py)
        };
        // Only decoded JSON can hold a list or dict; every other cell is an
        // immutable scalar, UUID, datetime or bytes.
        let shared = !(*is_json || matches!(value, Value::Json(_))) || is_scalar(&cell);
        stored.set_item(column, &cell)?;
        if shared {
            snapshot.set_item(column, &cell)?;
        } else {
            snapshot.set_item(column, deepcopy.call1((&cell,))?)?;
        }
    }
    stored.set_item(&plan.snapshot_key, snapshot)?;
    Ok(instance)
}

pub(crate) fn models_to_py(
    py: Python<'_>,
    rows: &[Vec<(String, Value)>],
    plan: &ModelRows,
) -> PyResult<Py<PyAny>> {
    let new = py
        .import("builtins")?
        .getattr("object")?
        .getattr("__new__")?;
    let deepcopy = py.import("copy")?.getattr("deepcopy")?;
    let out = PyList::empty(py);
    for row in rows {
        out.append(build_one(py, plan, row, &new, &deepcopy)?)?;
    }
    Ok(out.into_any().unbind())
}
