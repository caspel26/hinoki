//! Rows to finished model instances, built in one pass on the loop thread:
//! the generated constructor, the JSON decode and the stored-value snapshot
//! of `Model.from_row` / `remember`, without the intermediate row dicts.

use std::sync::Arc;

use hinoki_orm::{DbError, ErrorKind, Value};
use pyo3::exceptions::PyValueError;
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
    /// `select_related` relations whose columns follow the model's own, and
    /// the instance `__dict__` key their instances are stored under.
    joins: Vec<Join>,
    store_key: Option<String>,
}

/// One joined relation: its model's plan, the aliased column names it is read
/// from and the position of its primary key (a NULL key means no related row).
struct Join {
    name: String,
    plan: Arc<ModelRows>,
    aliases: Vec<String>,
    key: usize,
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
                joins: Vec::new(),
                store_key: None,
            }),
        })
    }

    /// This plan followed by joined relations: `(name, plan, alias prefix,
    /// primary key position)` each, in the order the query selects them. The
    /// related instances are stored in a dict under `store_key`.
    fn joined(
        &self,
        py: Python<'_>,
        joins: Vec<(String, PyRef<'_, PyModelRows>, String, usize)>,
        store_key: String,
    ) -> PyResult<Self> {
        let joins = joins
            .into_iter()
            .map(|(name, plan, prefix, key)| {
                let plan = plan.inner.clone();
                if key >= plan.columns.len() {
                    return Err(PyValueError::new_err("joined primary key out of range"));
                }
                let aliases = (0..plan.columns.len())
                    .map(|index| format!("{prefix}{index}"))
                    .collect();
                Ok(Join {
                    name,
                    plan,
                    aliases,
                    key,
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        let base = &self.inner;
        Ok(Self {
            inner: Arc::new(ModelRows {
                cls: base.cls.clone_ref(py),
                name: base.name.clone(),
                columns: base.columns.clone(),
                json: base.json.clone(),
                snapshot_key: base.snapshot_key.clone(),
                joins,
                store_key: Some(store_key),
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
    names: &[String],
    new: &Bound<'py, PyAny>,
    deepcopy: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if row.len() != names.len() {
        return Err(mismatch(py, plan));
    }
    let instance = new.call1((plan.cls.bind(py),))?;
    let stored = instance.getattr("__dict__")?.cast_into::<PyDict>()?;
    let snapshot = PyDict::new(py);
    let columns = row.iter().zip(names).zip(&plan.columns).zip(&plan.json);
    for ((((name, value), wanted), column), is_json) in columns {
        if name != wanted {
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

/// One result row: the model's own columns, then each joined relation's.
fn build_row<'py>(
    py: Python<'py>,
    plan: &ModelRows,
    row: &[(String, Value)],
    new: &Bound<'py, PyAny>,
    deepcopy: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let width = plan.columns.len();
    let joined: usize = plan.joins.iter().map(|join| join.aliases.len()).sum();
    if row.len() != width + joined {
        return Err(mismatch(py, plan));
    }
    let instance = build_one(py, plan, &row[..width], &plan.columns, new, deepcopy)?;
    if let Some(store_key) = &plan.store_key {
        let related = PyDict::new(py);
        let mut at = width;
        for join in &plan.joins {
            let cells = &row[at..at + join.aliases.len()];
            at += join.aliases.len();
            related.set_item(&join.name, build_related(py, join, cells, new, deepcopy)?)?;
        }
        instance.getattr("__dict__")?.set_item(store_key, related)?;
    }
    Ok(instance)
}

/// The related instance of one join, or `None` when its primary key is NULL
/// (the LEFT JOIN found no row), as `QuerySet.build` decides.
fn build_related<'py>(
    py: Python<'py>,
    join: &Join,
    cells: &[(String, Value)],
    new: &Bound<'py, PyAny>,
    deepcopy: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if matches!(cells[join.key].1, Value::Null) {
        if cells
            .iter()
            .zip(&join.aliases)
            .any(|(cell, alias)| &cell.0 != alias)
        {
            return Err(mismatch(py, &join.plan));
        }
        return Ok(py.None().into_bound(py));
    }
    build_one(py, &join.plan, cells, &join.aliases, new, deepcopy)
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
        out.append(build_row(py, plan, row, &new, &deepcopy)?)?;
    }
    Ok(out.into_any().unbind())
}
