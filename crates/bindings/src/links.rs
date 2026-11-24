//! Python entry points for many-to-many links: the relation description, key
//! coercion and the conversion of a write request and its outcome. The write
//! itself runs in `hinoki_orm::links`.

use hinoki_orm::links::{
    coerce_keys, KeyFailure, KeyKind, LinkOutcome, LinkTable, RawKey, Scope, MAX_LINK_ITEMS,
};
use hinoki_orm::query::{map_filters, map_subfilters, Dialect, Join};
use hinoki_orm::Value;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyInt, PyList, PyString};

use crate::db::{from_value, to_value};
use crate::query::{join_of, RawFilter, RawJoin, RawSub, Store};

fn key_kind(name: &str, length: Option<usize>) -> PyResult<KeyKind> {
    match (name, length) {
        ("uuid", _) => Ok(KeyKind::Uuid),
        ("int", _) => Ok(KeyKind::Int),
        ("text", Some(max_length)) => Ok(KeyKind::Text { max_length }),
        _ => Err(PyValueError::new_err(format!(
            "unsupported key kind {name:?}"
        ))),
    }
}

/// The tables, columns and key types of one many-to-many relation. Names are
/// validated once, here.
#[pyclass(frozen, name = "LinkSpec", module = "hinoki.core")]
pub struct PyLinkSpec {
    pub(crate) table: LinkTable,
}

#[pymethods]
impl PyLinkSpec {
    #[new]
    #[pyo3(signature = (bridge, bridge_pk, owner, target, max_items, declared_max=None, target_live=None))]
    fn new(
        bridge: String,
        bridge_pk: String,
        owner: (String, String, String, String, Option<usize>),
        target: (String, String, String, String, Option<usize>),
        max_items: usize,
        declared_max: Option<usize>,
        target_live: Option<String>,
    ) -> PyResult<Self> {
        let (owner_table, owner_pk, owner_column, owner_kind, owner_length) = owner;
        let (target_table, target_pk, target_column, target_kind, target_length) = target;
        let table = LinkTable {
            bridge,
            bridge_pk,
            owner_table,
            owner_pk,
            owner_column,
            target_table,
            target_pk,
            target_column,
            owner_kind: key_kind(&owner_kind, owner_length)?,
            target_kind: key_kind(&target_kind, target_length)?,
            max_items,
            declared_max,
            target_live,
        };
        table
            .validate()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        Ok(Self { table })
    }
}

fn raw_key(key: &Bound<'_, PyAny>) -> PyResult<RawKey> {
    let py = key.py();
    if key.is_instance_of::<PyBool>() {
        return Ok(RawKey::Invalid);
    }
    if key.is_instance_of::<PyInt>() {
        return Ok(key.extract::<i64>().map_or(RawKey::Invalid, RawKey::Int));
    }
    if key.is_instance_of::<PyString>() {
        return Ok(RawKey::Text(key.extract()?));
    }
    if key.is_instance(crate::pytypes::uuid_class(py)?)? {
        let text: String = key.str()?.extract()?;
        return Ok(uuid::Uuid::parse_str(&text).map_or(RawKey::Invalid, RawKey::Uuid));
    }
    Ok(RawKey::Invalid)
}

pub(crate) fn raw_keys(keys: &Bound<'_, PyList>) -> PyResult<Vec<RawKey>> {
    keys.iter().map(|key| raw_key(&key)).collect()
}

/// Coerce `keys` to the key type of the owner (`target=False`) or the target.
/// Returns `("ok", keys)` with the keys unique and sorted, `("invalid", index)`
/// or `("too_many",)`; values never appear in the result.
#[pyfunction]
pub fn coerce_link_keys(
    py: Python<'_>,
    spec: &PyLinkSpec,
    keys: &Bound<'_, PyList>,
    target: bool,
) -> PyResult<Py<PyAny>> {
    let table = &spec.table;
    let (kind, limit) = if target {
        (table.target_kind, table.max_items)
    } else {
        (table.owner_kind, MAX_LINK_ITEMS)
    };
    let raw = raw_keys(keys)?;
    Ok(match coerce_keys(kind, &raw, limit) {
        Ok(found) => {
            let out = PyList::empty(py);
            for key in &found {
                out.append(from_value(py, &key.to_value())?)?;
            }
            ("ok", out).into_pyobject(py)?.into_any().unbind()
        }
        Err(KeyFailure::Invalid(index)) => {
            ("invalid", index).into_pyobject(py)?.into_any().unbind()
        }
        Err(KeyFailure::TooMany) => ("too_many",).into_pyobject(py)?.into_any().unbind(),
    })
}

/// `(joins, filters, subfilters)` as Python builds them.
pub type RawScope = (Vec<RawJoin>, Vec<RawFilter>, Vec<RawSub>);

pub(crate) fn scope_of(
    py: Python<'_>,
    dialect: Dialect,
    (joins, filters, subfilters): RawScope,
) -> PyResult<Scope> {
    let mut store = Store::default();
    let filters = store.filters(py, dialect, filters)?;
    let subfilters = store.subfilters(py, dialect, subfilters)?;
    let mut convert = |index: usize| to_value(store.item(index).bind(py));
    Ok(Scope {
        joins: joins.into_iter().map(join_of).collect::<Vec<Join>>(),
        filters: map_filters(filters, &mut convert)?,
        subfilters: map_subfilters(subfilters, &mut convert)?,
    })
}

pub(crate) fn outcome_to_py(py: Python<'_>, outcome: &LinkOutcome) -> PyResult<Py<PyAny>> {
    let keys = |values: &[Value]| -> PyResult<Py<PyAny>> {
        let out = PyList::empty(py);
        for value in values {
            out.append(from_value(py, value)?)?;
        }
        Ok(out.into_any().unbind())
    };
    Ok(match outcome {
        LinkOutcome::Done { added, removed } => ("done", keys(added)?, keys(removed)?)
            .into_pyobject(py)?
            .into_any()
            .unbind(),
        LinkOutcome::OwnerMissing => ("owner_missing",).into_pyobject(py)?.into_any().unbind(),
        LinkOutcome::OwnerGone => ("owner_gone",).into_pyobject(py)?.into_any().unbind(),
        LinkOutcome::Missing(count) => ("missing", *count).into_pyobject(py)?.into_any().unbind(),
        LinkOutcome::LimitExceeded => ("limit",).into_pyobject(py)?.into_any().unbind(),
    })
}
