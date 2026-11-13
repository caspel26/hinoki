//! Python entry point for SQL compilation. Values stay Python objects: the
//! compiler works on indices into a value store and the placeholders' values
//! are mapped back afterwards, so nothing is converted or interpolated here.

use hinoki_orm::query::{
    compile_delete, compile_insert, compile_select, compile_update, Delete, Filter, Insert, Op,
    QueryError, Select, Update,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

type RawFilter = (String, String, Vec<Py<PyAny>>);

fn error(err: QueryError) -> PyErr {
    PyValueError::new_err(err.to_string())
}

/// Collects Python values and hands out stable indices for the compiler.
#[derive(Default)]
struct Store {
    items: Vec<Py<PyAny>>,
}

impl Store {
    fn add(&mut self, value: Py<PyAny>) -> usize {
        self.items.push(value);
        self.items.len() - 1
    }

    fn filters(&mut self, raw: Vec<RawFilter>) -> PyResult<Vec<Filter<usize>>> {
        raw.into_iter()
            .map(|(column, op, values)| {
                let op = Op::parse(&op)
                    .ok_or_else(|| PyValueError::new_err(format!("unknown operator {op:?}")))?;
                let values = values.into_iter().map(|v| self.add(v)).collect();
                Ok(Filter { column, op, values })
            })
            .collect()
    }

    fn assignments(&mut self, raw: Vec<(String, Py<PyAny>)>) -> Vec<(String, usize)> {
        raw.into_iter().map(|(c, v)| (c, self.add(v))).collect()
    }

    fn resolve(
        &self,
        py: Python<'_>,
        sql: String,
        indices: Vec<usize>,
    ) -> (String, Vec<Py<PyAny>>) {
        let params = indices
            .iter()
            .map(|&i| self.items[i].clone_ref(py))
            .collect();
        (sql, params)
    }
}

/// Compile one statement. `kind` is `select`, `count`, `insert`, `update` or
/// `delete`; unused arguments are ignored. Returns `(sql, params)` with
/// `$n` placeholders in the order of `params`.
#[pyfunction]
#[pyo3(signature = (kind, table, *, columns=None, filters=None, assignments=None, returning=None, order=None, limit=None, offset=None))]
#[allow(clippy::too_many_arguments)]
pub fn compile_query(
    py: Python<'_>,
    kind: &str,
    table: String,
    columns: Option<Vec<String>>,
    filters: Option<Vec<RawFilter>>,
    assignments: Option<Vec<(String, Py<PyAny>)>>,
    returning: Option<Vec<String>>,
    order: Option<Vec<(String, bool)>>,
    limit: Option<u64>,
    offset: Option<u64>,
) -> PyResult<(String, Vec<Py<PyAny>>)> {
    let mut store = Store::default();
    let filters = store.filters(filters.unwrap_or_default())?;
    let assignments = store.assignments(assignments.unwrap_or_default());
    let returning = returning.unwrap_or_default();
    let compiled = match kind {
        "select" | "count" => compile_select(&Select {
            table,
            columns: columns.unwrap_or_default(),
            filters,
            order: order.unwrap_or_default(),
            limit,
            offset,
            count: kind == "count",
        }),
        "insert" => compile_insert(&Insert {
            table,
            assignments,
            returning,
        }),
        "update" => compile_update(&Update {
            table,
            assignments,
            filters,
            returning,
        }),
        "delete" => compile_delete(&Delete { table, filters }),
        other => {
            return Err(PyValueError::new_err(format!(
                "unknown statement kind {other:?}"
            )))
        }
    };
    let (sql, indices) = compiled.map_err(error)?;
    Ok(store.resolve(py, sql, indices))
}
