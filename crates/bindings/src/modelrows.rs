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
    linked: Option<Linked>,
}

struct Linked {
    alias: String,
    key: usize,
    max_items: usize,
}

/// One joined relation: its model's plan, the aliased column names it is read
/// from and the position of its primary key (a NULL key means no related row).
#[derive(Clone)]
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
                linked: None,
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
                linked: None,
            }),
        })
    }

    /// Construct a plan for rows with a trailing bridge owner column.
    fn linked(&self, py: Python<'_>, alias: String, max_items: usize) -> PyResult<Self> {
        let base = &self.inner;
        let primary_key: String = base.cls.bind(py).getattr("__primary_key__")?.extract()?;
        let key = base
            .columns
            .iter()
            .position(|column| column == &primary_key)
            .ok_or_else(|| PyValueError::new_err("primary key is not in the model columns"))?;
        Ok(Self {
            inner: Arc::new(ModelRows {
                cls: base.cls.clone_ref(py),
                name: base.name.clone(),
                columns: base.columns.clone(),
                json: base.json.clone(),
                snapshot_key: base.snapshot_key.clone(),
                joins: base.joins.clone(),
                store_key: base.store_key.clone(),
                linked: Some(Linked {
                    alias,
                    key,
                    max_items,
                }),
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
    if let Some(linked) = &plan.linked {
        return linked_models(py, rows, plan, linked, &new, &deepcopy);
    }
    let out = PyList::empty(py);
    for row in rows {
        out.append(build_row(py, plan, row, &new, &deepcopy)?)?;
    }
    Ok(out.into_any().unbind())
}

/// Return owner-keyed lists, or None when any owner's link limit is exceeded.
fn linked_models(
    py: Python<'_>,
    rows: &[Vec<(String, Value)>],
    plan: &ModelRows,
    linked: &Linked,
    new: &Bound<'_, PyAny>,
    deepcopy: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let owners = PyDict::new(py);
    let targets = PyDict::new(py);
    for row in rows {
        let (owner, cells) = row.split_last().ok_or_else(|| mismatch(py, plan))?;
        if owner.0 != linked.alias || matches!(owner.1, Value::Null) {
            return Err(mismatch(py, plan));
        }
        validate_linked_row(py, plan, cells)?;
        let owner_key = from_value(py, &owner.1)?.into_bound(py).str()?.to_string();
        let key = from_value(py, &cells[linked.key].1)?
            .into_bound(py)
            .str()?
            .to_string();
        let target = if let Some(existing) = targets.get_item(&key)? {
            existing
        } else {
            let target = build_row(py, plan, cells, new, deepcopy)?;
            targets.set_item(key, &target)?;
            target
        };
        let group = match owners.get_item(&owner_key)? {
            Some(group) => group.cast_into::<PyList>()?,
            None => {
                let group = PyList::empty(py);
                owners.set_item(owner_key, &group)?;
                group
            }
        };
        group.append(target)?;
        if group.len() > linked.max_items {
            return Ok(py.None());
        }
    }
    Ok(owners.into_any().unbind())
}

fn validate_linked_row(
    py: Python<'_>,
    plan: &ModelRows,
    cells: &[(String, Value)],
) -> PyResult<()> {
    let names = plan
        .columns
        .iter()
        .chain(plan.joins.iter().flat_map(|join| &join.aliases));
    let expected = plan.columns.len()
        + plan
            .joins
            .iter()
            .map(|join| join.aliases.len())
            .sum::<usize>();
    if cells.len() != expected || cells.iter().zip(names).any(|(cell, name)| &cell.0 != name) {
        return Err(mismatch(py, plan));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyModule;

    fn row(id: Value, owner: Value) -> Vec<(String, Value)> {
        vec![("id".into(), id), ("link__0".into(), owner)]
    }

    #[test]
    fn linked_constructs_each_distinct_target_only_once() {
        Python::initialize();
        Python::attach(|py| {
            let module = PyModule::from_code(
                py,
                c"count = 0\nclass Target:\n    __primary_key__ = 'id'\ndef make(cls):\n    global count\n    count += 1\n    return object.__new__(cls)\n",
                c"linked_count.py",
                c"linked_count",
            ).unwrap();
            let cls = module.getattr("Target").unwrap();
            let plan = PyModelRows::new(&cls, vec!["id".into()], vec![], "snapshot".into())
                .unwrap()
                .linked(py, "link__0".into(), 3)
                .unwrap();
            let rows = vec![
                row(Value::Int(1), Value::Int(1)),
                row(Value::Int(2), Value::Int(1)),
                row(Value::Int(1), Value::Int(2)),
            ];
            let new = module.getattr("make").unwrap();
            let deepcopy = py.import("copy").unwrap().getattr("deepcopy").unwrap();
            linked_models(
                py,
                &rows,
                &plan.inner,
                plan.inner.linked.as_ref().unwrap(),
                &new,
                &deepcopy,
            )
            .unwrap();
            assert_eq!(
                module.getattr("count").unwrap().extract::<usize>().unwrap(),
                2
            );
        });
    }

    #[test]
    fn linked_rows_group_in_arrival_order_and_share_instances() {
        Python::initialize();
        Python::attach(|py| {
            let module = PyModule::from_code(
                py,
                c"class Target:\n    __primary_key__ = 'id'\n",
                c"test.py",
                c"test",
            )
            .unwrap();
            let cls = module.getattr("Target").unwrap();
            let plan = PyModelRows::new(&cls, vec!["id".into()], vec![], "snapshot".into())
                .unwrap()
                .linked(py, "link__0".into(), 2)
                .unwrap();
            let rows = vec![
                row(Value::Int(2), Value::Text("a".into())),
                row(Value::Int(1), Value::Text("a".into())),
                row(Value::Int(2), Value::Text("b".into())),
            ];
            let out = models_to_py(py, &rows, &plan.inner)
                .unwrap()
                .into_bound(py)
                .cast_into::<PyDict>()
                .unwrap();
            let a = out
                .get_item("a")
                .unwrap()
                .unwrap()
                .cast_into::<PyList>()
                .unwrap();
            let b = out
                .get_item("b")
                .unwrap()
                .unwrap()
                .cast_into::<PyList>()
                .unwrap();
            assert_eq!(
                a.get_item(0)
                    .unwrap()
                    .getattr("id")
                    .unwrap()
                    .extract::<i64>()
                    .unwrap(),
                2
            );
            assert_eq!(
                a.get_item(1)
                    .unwrap()
                    .getattr("id")
                    .unwrap()
                    .extract::<i64>()
                    .unwrap(),
                1
            );
            assert!(a.get_item(0).unwrap().is(b.get_item(0).unwrap()));
            let too_many = vec![
                row(Value::Int(1), Value::Int(4)),
                row(Value::Int(2), Value::Int(4)),
                row(Value::Int(3), Value::Int(4)),
            ];
            assert!(models_to_py(py, &too_many, &plan.inner)
                .unwrap()
                .is_none(py));
            assert!(models_to_py(py, &[row(Value::Int(1), Value::Null)], &plan.inner).is_err());
            assert!(models_to_py(py, &[vec![("id".into(), Value::Int(1))]], &plan.inner).is_err());
            let empty = models_to_py(py, &[], &plan.inner).unwrap();
            assert_eq!(empty.bind(py).cast::<PyDict>().unwrap().len(), 0);
            let id = uuid::Uuid::parse_str("12345678-1234-4234-8234-123456789abc").unwrap();
            let varied = vec![
                row(Value::Text("target".into()), Value::Uuid(id)),
                row(Value::Uuid(id), Value::Int(7)),
            ];
            let found = models_to_py(py, &varied, &plan.inner).unwrap();
            let found = found.bind(py).cast::<PyDict>().unwrap();
            assert!(found.contains(id.to_string()).unwrap());
            assert!(found.contains("7").unwrap());
        });
    }
}
