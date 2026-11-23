use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList};

use super::compile::instance_fields;
use super::model::SchemaWireNames;
use super::scalars::wire_text;

const DUMP_DEPTH: usize = 1000;

struct SerializationOptions<'a> {
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    exclude: &'a [String],
    include: Option<&'a [String]>,
}

/// A value as `Schema.dump()` (JSON-compatible) or `Schema.to_dict()` (Python
/// values) shows it: nested schemas become dicts, lists and tuples lists, dicts
/// are copied; in JSON mode exact `UUID`/`datetime` values become text and any
/// object with a `dump()` method is replaced by what it returns.
fn convert<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
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
    if let Ok(list) = value.cast::<PyList>() {
        return convert_list(
            py,
            list.iter(),
            json_mode,
            by_alias,
            exclude_unset,
            controlled,
            depth,
        );
    }
    if let Some(grouped) =
        convert_grouped(value, json_mode, by_alias, exclude_unset, controlled, depth)?
    {
        return Ok(grouped);
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        return convert_dict(
            py,
            dict,
            json_mode,
            by_alias,
            exclude_unset,
            controlled,
            depth,
        );
    }
    if instance_fields(value, false, by_alias, false)?.is_some() {
        return convert_schema(value, json_mode, by_alias, exclude_unset, controlled, depth);
    }
    if json_mode {
        return convert_json_object(value);
    }
    Ok(value.clone())
}

/// A tuple, set or frozenset: JSON mode writes a list (a set sorted when its
/// members compare); Python mode keeps the container.
fn convert_grouped<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    depth: usize,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = value.py();
    let is_set = value.is_instance_of::<pyo3::types::PySet>()
        || value.is_instance_of::<pyo3::types::PyFrozenSet>();
    if !is_set && !value.is_instance_of::<pyo3::types::PyTuple>() {
        return Ok(None);
    }
    if is_set && !json_mode {
        return Ok(Some(value.clone()));
    }
    let members = if is_set {
        crate::pytypes::sorted_members(value)?
    } else {
        value.try_iter()?.collect::<PyResult<Vec<_>>>()?
    };
    let items = convert_list(
        py,
        members.into_iter(),
        json_mode,
        by_alias,
        exclude_unset,
        controlled,
        depth,
    )?;
    if json_mode {
        return Ok(Some(items));
    }
    Ok(Some(py.get_type::<pyo3::types::PyTuple>().call1((items,))?))
}

fn convert_list<'py>(
    py: Python<'py>,
    items: impl Iterator<Item = Bound<'py, PyAny>>,
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let out = PyList::empty(py);
    for item in items {
        out.append(convert(
            &item,
            json_mode,
            by_alias,
            exclude_unset,
            controlled,
            depth + 1,
        )?)?;
    }
    Ok(out.into_any())
}

fn convert_dict<'py>(
    py: Python<'py>,
    items: &Bound<'py, PyDict>,
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let out = PyDict::new(py);
    for (key, item) in items.iter() {
        out.set_item(
            key,
            convert(
                &item,
                json_mode,
                by_alias,
                exclude_unset,
                controlled,
                depth + 1,
            )?,
        )?;
    }
    Ok(out.into_any())
}

fn convert_schema<'py>(
    value: &Bound<'py, PyAny>,
    json_mode: bool,
    by_alias: bool,
    exclude_unset: bool,
    controlled: bool,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    if json_mode && instance_fields(value, true, by_alias, false)?.is_none() {
        if controlled {
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
            json_mode,
            by_alias,
            exclude_unset,
            controlled,
            exclude: &[],
            include: None,
        },
        depth + 1,
    )?
    .into_any())
}

fn convert_json_object<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if let Some(text) = crate::pytypes::exact_uuid_text(value)? {
        return Ok(pyo3::types::PyString::new(value.py(), &text).into_any());
    }
    if value
        .get_type()
        .is(crate::pytypes::datetime_class(value.py())?)
    {
        return value.call_method0("isoformat");
    }
    if let Some(text) = wire_text(value)? {
        return Ok(pyo3::types::PyString::new(value.py(), &text).into_any());
    }
    if let Some(member) = crate::pytypes::enum_value(value)? {
        return Ok(member);
    }
    if value.hasattr("dump")? {
        return value.call_method0("dump");
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
    let supplied = supplied_fields(value, options.exclude_unset)?;
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
        out.set_item(
            &wire_name,
            convert(
                &item,
                options.json_mode,
                options.by_alias,
                options.exclude_unset,
                options.controlled,
                depth,
            )?,
        )?;
    }
    Ok(out)
}

fn dump_schema_fields(
    value: &Bound<'_, PyAny>,
    options: &SerializationOptions<'_>,
) -> PyResult<(SchemaWireNames, Py<PyAny>, usize)> {
    if let Some(fields) = instance_fields(
        value,
        options.controlled,
        options.by_alias,
        options.json_mode,
    )? {
        return Ok(fields);
    }
    if options.controlled {
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
            json_mode,
            by_alias,
            exclude_unset: false,
            controlled: false,
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
            json_mode: true,
            by_alias,
            exclude_unset,
            controlled: exclude_unset || !by_alias,
            exclude: &exclude,
            include: include.as_deref(),
        },
        0,
    )
}
