use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList};

use super::compile::instance_fields;
use super::model::SchemaWireNames;
use super::scalars::wire_text;

const DUMP_DEPTH: usize = 1000;

/// How nested values are written; shared by every level of one dump.
#[derive(Clone, Copy)]
struct Mode {
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    /// Storage dumps (the cache) keep only declared fields, at every depth,
    /// ignore `dump()` overrides and refuse `Secret` values, so the result
    /// validates back into the same schema.
    storage: bool,
}

struct SerializationOptions<'a> {
    mode: Mode,
    exclude: &'a [String],
    include: Option<&'a [String]>,
}

/// A value as `Schema.dump()` (JSON-compatible) or `Schema.to_dict()` (Python
/// values) shows it: nested schemas become dicts, lists and tuples lists, dicts
/// are copied; in JSON mode exact `UUID`/`datetime` values become text and any
/// object with a `dump()` method is replaced by what it returns.
fn convert<'py>(
    value: &Bound<'py, PyAny>,
    mode: Mode,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    if depth > DUMP_DEPTH {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "value is nested too deeply",
        ));
    }
    if crate::pytypes::is_plain_scalar(value) {
        return Ok(value.clone());
    }
    if value.is_exact_instance_of::<PyList>() {
        if let Ok(list) = value.cast::<PyList>() {
            return convert_list(py, list.iter(), mode, depth);
        }
    } else if value.is_instance_of::<PyList>() {
        let items: Vec<Bound<'py, PyAny>> = value.try_iter()?.collect::<PyResult<_>>()?;
        return convert_list(py, items.into_iter(), mode, depth);
    }
    if let Some(grouped) = convert_grouped(value, mode, depth)? {
        return Ok(grouped);
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        return convert_dict(py, dict, mode, depth);
    }
    if value
        .get_type()
        .is(crate::pytypes::projected_item_class(py)?)
    {
        let inner = value.call_method0(pyo3::intern!(py, "_ensure"))?;
        return convert(&inner, mode, depth);
    }
    if instance_fields(value, false, mode.by_alias, false)?.is_some() {
        return convert_schema(value, mode, depth);
    }
    if mode.json_mode {
        return convert_json_object(value, mode.storage);
    }
    Ok(value.clone())
}

/// A tuple, set or frozenset: JSON mode writes a list (a set sorted when its
/// members compare); Python mode keeps the container.
fn convert_grouped<'py>(
    value: &Bound<'py, PyAny>,
    mode: Mode,
    depth: usize,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = value.py();
    let is_set = value.is_instance_of::<pyo3::types::PySet>()
        || value.is_instance_of::<pyo3::types::PyFrozenSet>();
    if !is_set && !value.is_instance_of::<pyo3::types::PyTuple>() {
        return Ok(None);
    }
    if is_set && !mode.json_mode {
        return Ok(Some(value.clone()));
    }
    let members = if is_set {
        crate::pytypes::sorted_members(value)?
    } else {
        value.try_iter()?.collect::<PyResult<Vec<_>>>()?
    };
    let items = convert_list(py, members.into_iter(), mode, depth)?;
    if mode.json_mode {
        return Ok(Some(items));
    }
    Ok(Some(py.get_type::<pyo3::types::PyTuple>().call1((items,))?))
}

fn convert_list<'py>(
    py: Python<'py>,
    items: impl Iterator<Item = Bound<'py, PyAny>>,
    mode: Mode,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let out = PyList::empty(py);
    for item in items {
        out.append(convert(&item, mode, depth + 1)?)?;
    }
    Ok(out.into_any())
}

fn convert_dict<'py>(
    py: Python<'py>,
    items: &Bound<'py, PyDict>,
    mode: Mode,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let out = PyDict::new(py);
    for (key, item) in items.iter() {
        out.set_item(key, convert(&item, mode, depth + 1)?)?;
    }
    Ok(out.into_any())
}

fn convert_schema<'py>(
    value: &Bound<'py, PyAny>,
    mode: Mode,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    if mode.json_mode
        && !mode.storage
        && instance_fields(value, true, mode.by_alias, false)?.is_none()
    {
        if mode.controlled {
            let class_name = value.get_type().name()?.to_string();
            return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                "controlled serialization cannot inspect {class_name} because it overrides dump(); use its dump() or a projection Schema"
            )));
        }
        return value.call_method0("dump"); // the class decides what it exposes
    }
    Ok(dump_instance_at(
        value,
        SerializationOptions {
            mode,
            exclude: &[],
            include: None,
        },
        depth + 1,
    )?
    .into_any())
}

fn convert_json_object<'py>(
    value: &Bound<'py, PyAny>,
    storage: bool,
) -> PyResult<Bound<'py, PyAny>> {
    if storage && value.is_instance(crate::pytypes::secret_class(value.py())?)? {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "Secret values cannot be stored: they would be written masked",
        ));
    }
    if let Some(text) = crate::pytypes::exact_uuid_text(value)? {
        return Ok(pyo3::types::PyString::new(value.py(), &text).into_any());
    }
    if value
        .get_type()
        .is(crate::pytypes::datetime_class(value.py())?)
    {
        return value.call_method0(pyo3::intern!(value.py(), "isoformat"));
    }
    if let Some(text) = wire_text(value)? {
        return Ok(pyo3::types::PyString::new(value.py(), &text).into_any());
    }
    if let Some(member) = crate::pytypes::enum_value(value)? {
        return Ok(member);
    }
    let dump_name = pyo3::intern!(value.py(), "dump");
    if value.hasattr(dump_name)? {
        return value.call_method0(dump_name);
    }
    Ok(value.clone())
}

fn dump_instance_at<'py>(
    value: &Bound<'py, PyAny>,
    options: SerializationOptions<'_>,
    depth: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let py = value.py();
    let (names, unset, computed) = dump_schema_fields(value, &options)?;
    let mode = options.mode;
    let supplied = supplied_fields(value, mode.exclude_unset)?;
    let out = PyDict::new(py);
    let first_computed = names.len() - computed;
    for (index, (name, wire_name)) in names.into_iter().enumerate() {
        // Computed values are never "unset": only include/exclude apply.
        let tracked = if index < first_computed {
            supplied.as_ref()
        } else {
            None
        };
        if skip_serialized_field(py, &name, &options, tracked)? {
            continue;
        }
        let item = value.getattr(name.as_str())?;
        if item.is(&unset) {
            continue;
        }
        out.set_item(&wire_name, convert(&item, mode, depth)?)?;
    }
    Ok(out)
}

fn dump_schema_fields(
    value: &Bound<'_, PyAny>,
    options: &SerializationOptions<'_>,
) -> PyResult<(SchemaWireNames, Py<PyAny>, usize)> {
    let mode = options.mode;
    if let Some(fields) = instance_fields(
        value,
        mode.controlled && !mode.storage,
        mode.by_alias,
        mode.json_mode && !mode.storage,
    )? {
        return Ok(fields);
    }
    if mode.controlled {
        let class_name = value.get_type().name()?.to_string();
        return Err(pyo3::exceptions::PyTypeError::new_err(format!(
            "controlled serialization cannot inspect {class_name} because it overrides dump(); use its dump() or a projection Schema"
        )));
    }
    Err(pyo3::exceptions::PyTypeError::new_err(
        "not a schema instance",
    ))
}

fn supplied_fields<'py>(
    value: &Bound<'py, PyAny>,
    exclude_unset: bool,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if !exclude_unset {
        return Ok(None);
    }
    let marker = value.getattr("__hinoki_supplied_fields__").map_err(|_| {
        pyo3::exceptions::PyTypeError::new_err(format!(
            "{} has no reliable supplied-field tracking; exclude_unset requires the generated Schema constructor",
            value
                .get_type()
                .name()
                .map(|name| name.to_string())
                .unwrap_or_else(|_| "Schema".to_owned())
        ))
    })?;
    Ok(Some(marker))
}

fn skip_serialized_field<'py>(
    py: Python<'py>,
    name: &str,
    options: &SerializationOptions<'_>,
    supplied: Option<&Bound<'py, PyAny>>,
) -> PyResult<bool> {
    if options.exclude.iter().any(|field| field == name) {
        return Ok(true);
    }
    if options
        .include
        .is_some_and(|selected| !selected.iter().any(|field| field == name))
    {
        return Ok(true);
    }
    if let Some(fields) = supplied {
        if !fields.is(PyBool::new(py, true))
            && !fields.call_method1("__contains__", (name,))?.is_truthy()?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// The fields of a schema instance as a dict (UNSET omitted): JSON-compatible
/// when `json_mode`, Python values otherwise. `exclude` names Python fields to
/// skip; `by_alias` selects their wire names in the resulting mapping.
#[pyfunction]
#[pyo3(signature = (value, json_mode, exclude=Vec::new(), by_alias=true))]
pub fn dump_instance<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    exclude: Vec<String>,
    by_alias: bool,
) -> PyResult<Bound<'py, PyDict>> {
    dump_instance_at(
        value,
        SerializationOptions {
            mode: Mode {
                json_mode,
                by_alias,
                exclude_unset: false,
                controlled: false,
                storage: false,
            },
            exclude: &exclude,
            include: None,
        },
        0,
    )
}

/// JSON-compatible, explicitly selected Schema projection.
#[pyfunction]
#[pyo3(signature = (value, include=None, exclude=Vec::new(), exclude_unset=false, by_alias=true))]
pub fn serialize_instance<'py>(
    value: &Bound<'py, PyAny>,
    include: Option<Vec<String>>,
    exclude: Vec<String>,
    exclude_unset: bool,
    by_alias: bool,
) -> PyResult<Bound<'py, PyDict>> {
    dump_instance_at(
        value,
        SerializationOptions {
            mode: Mode {
                json_mode: true,
                by_alias,
                exclude_unset,
                controlled: exclude_unset || !by_alias,
                storage: false,
            },
            exclude: &exclude,
            include: include.as_deref(),
        },
        0,
    )
}

/// JSON-compatible declared fields of a schema instance, for storage that is
/// validated back into the same schema (the cache): no computed values at any
/// depth, `dump()` overrides ignored, and `ValueError` for a `Secret`
/// value, which would otherwise be written as its mask.
#[pyfunction]
pub fn dump_for_storage<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    dump_instance_at(
        value,
        SerializationOptions {
            mode: Mode {
                json_mode: true,
                by_alias: true,
                exclude_unset: false,
                controlled: false,
                storage: true,
            },
            exclude: &[],
            include: None,
        },
        0,
    )
}
