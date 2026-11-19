//! Python classes the bindings look at for every value, looked up once, and a
//! faster way to build and print `uuid.UUID` than going through its Python code.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyFloat, PyInt, PyString};

static UUID: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATETIME: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIME: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIMEDELTA: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DECIMAL: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static ENUM: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static FAST_UUID: PyOnceLock<Option<FastUuid>> = PyOnceLock::new();

pub fn uuid_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    UUID.import(py, "uuid", "UUID")
}

pub fn datetime_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    DATETIME.import(py, "datetime", "datetime")
}

pub fn date_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    DATE.import(py, "datetime", "date")
}

pub fn time_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    TIME.import(py, "datetime", "time")
}

pub fn timedelta_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    TIMEDELTA.import(py, "datetime", "timedelta")
}

pub fn decimal_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    DECIMAL.import(py, "decimal", "Decimal")
}

static IP_ADDRESS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static IP_NETWORK: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Whether `value` is any `ipaddress` address or network; the four concrete
/// classes share these two bases.
pub fn is_ip_object(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = value.py();
    Ok(
        value.is_instance(IP_ADDRESS.import(py, "ipaddress", "_BaseAddress")?)?
            || value.is_instance(IP_NETWORK.import(py, "ipaddress", "_BaseNetwork")?)?,
    )
}

pub fn enum_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    ENUM.import(py, "enum", "Enum")
}

/// The value of an `enum.Enum` member, `None` for any other object.
pub fn enum_value<'py>(value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    if !value.is_instance(enum_class(value.py())?)? {
        return Ok(None);
    }
    value.getattr("value").map(Some)
}

/// The members of a set in the fixed order `hinoki.schema.sorted_items` gives.
pub fn sorted_members<'py>(set: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let ordered = set
        .py()
        .import("hinoki.schema")?
        .getattr("sorted_items")?
        .call1((set,))?;
    ordered.try_iter()?.collect()
}

/// `None`, `bool`, `int`, `float` and `str` of exactly these types: values that
/// are never objects with a `dump()` method and never schema instances.
pub fn is_plain_scalar(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyString>()
}

/// The canonical text of an exact `uuid.UUID`, read from its integer value.
/// `None` for anything else, so callers keep their general path.
pub fn exact_uuid_text(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if !value.get_type().is(uuid_class(value.py())?) {
        return Ok(None);
    }
    let number: u128 = value.getattr("int")?.extract()?;
    Ok(Some(uuid::Uuid::from_u128(number).hyphenated().to_string()))
}

/// What is needed to build a `UUID` without running `UUID.__init__`: a blank
/// instance and the two slots the class keeps (`int` and `is_safe`).
struct FastUuid {
    new: Py<PyAny>,
    set_attribute: Py<PyAny>,
    unknown: Py<PyAny>,
}

fn fast_uuid(py: Python<'_>) -> Option<&'static FastUuid> {
    FAST_UUID
        .get_or_init(py, || {
            let built = (|| -> PyResult<FastUuid> {
                let object = py.import("builtins")?.getattr("object")?;
                Ok(FastUuid {
                    new: object.getattr("__new__")?.unbind(),
                    set_attribute: object.getattr("__setattr__")?.unbind(),
                    unknown: py
                        .import("uuid")?
                        .getattr("SafeUUID")?
                        .getattr("unknown")?
                        .unbind(),
                })
            })()
            .ok()?;
            // Use the shortcut only if it gives exactly what `UUID(text)` gives.
            let sample = 0x1234_5678_9abc_def0_1234_5678_9abc_def0_u128;
            let made = build(py, &built, sample).ok()?;
            let text = uuid::Uuid::from_u128(sample).hyphenated().to_string();
            let wanted = uuid_class(py).ok()?.call1((text,)).ok()?;
            let same = made.eq(&wanted).ok()?
                && made.hash().ok()? == wanted.hash().ok()?
                && made.str().ok()?.to_string() == wanted.str().ok()?.to_string()
                && made
                    .getattr("version")
                    .ok()?
                    .eq(wanted.getattr("version").ok()?)
                    .ok()?;
            same.then_some(built)
        })
        .as_ref()
}

fn build<'py>(py: Python<'py>, fast: &FastUuid, number: u128) -> PyResult<Bound<'py, PyAny>> {
    let made = fast.new.bind(py).call1((uuid_class(py)?,))?;
    let set = fast.set_attribute.bind(py);
    set.call1((&made, "int", number))?;
    set.call1((&made, "is_safe", fast.unknown.bind(py)))?;
    Ok(made)
}

/// A `uuid.UUID` for a database value.
pub fn new_uuid<'py>(py: Python<'py>, value: uuid::Uuid) -> PyResult<Bound<'py, PyAny>> {
    match fast_uuid(py) {
        Some(fast) => build(py, fast, value.as_u128()),
        None => uuid_class(py)?.call1((value.to_string(),)),
    }
}
