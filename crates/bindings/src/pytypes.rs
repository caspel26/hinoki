//! Python classes the bindings look at for every value, looked up once, and a
//! faster way to build and print `uuid.UUID` than going through its Python code.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyFloat, PyInt, PyString};

static UUID: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATETIME: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static UTC: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIME: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIMEDELTA: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DECIMAL: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static PATH: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static PURE_PATH: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static SECRET: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static ENUM: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static PAGE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static PROJECTED_LIST: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static PROJECTED_PAGE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static PROJECTED_ITEM: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static OBJECT_NEW: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static COPY_DEEPCOPY: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static FAST_UUID: PyOnceLock<Option<FastUuid>> = PyOnceLock::new();

pub fn uuid_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    UUID.import(py, "uuid", "UUID")
}

/// `hinoki.pagination.Page`: only asked for a value that has a `dump()` method,
/// so the module is already imported.
pub fn page_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    PAGE.import(py, "hinoki.pagination", "Page")
}

pub fn projected_list_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    PROJECTED_LIST.import(py, "hinoki.orm.projection", "_ProjectedList")
}

pub fn projected_page_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    PROJECTED_PAGE.import(py, "hinoki.orm.projection", "_ProjectedPage")
}

pub fn projected_item_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    PROJECTED_ITEM.import(py, "hinoki.orm.projection", "_ProjectedItem")
}

pub fn object_new<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    OBJECT_NEW
        .get_or_try_init(py, || {
            Ok(py
                .import("builtins")?
                .getattr("object")?
                .getattr("__new__")?
                .unbind())
        })
        .map(|obj| obj.bind(py))
}

pub fn copy_deepcopy<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    COPY_DEEPCOPY.import(py, "copy", "deepcopy")
}

pub fn datetime_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    DATETIME.import(py, "datetime", "datetime")
}

pub fn utc_tz<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    UTC.import(py, "datetime", "UTC")
}

/// Build a UTC `datetime.datetime` directly from a database timestamp without
/// formatting and re-parsing an ISO string.
pub fn new_timestamp<'py>(
    py: Python<'py>,
    value: &time::OffsetDateTime,
) -> PyResult<Bound<'py, PyAny>> {
    let utc = value.to_offset(time::UtcOffset::UTC);
    datetime_class(py)?.call1((
        utc.year(),
        u8::from(utc.month()),
        utc.day(),
        utc.hour(),
        utc.minute(),
        utc.second(),
        utc.microsecond(),
        utc_tz(py)?,
    ))
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

pub fn path_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    PATH.import(py, "pathlib", "Path")
}

pub fn pure_path_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    PURE_PATH.import(py, "pathlib", "PurePath")
}

pub fn secret_class<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    SECRET.import(py, "hinoki.fields", "Secret")
}

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

#[cfg(test)]
mod tests {
    use super::*;
    use time::{Date, Month, OffsetDateTime, PrimitiveDateTime, Time, UtcOffset};

    #[test]
    fn new_timestamp_preserves_utc_micros_and_rejects_out_of_range() {
        Python::initialize();
        Python::attach(|py| {
            let offset = UtcOffset::from_hms(2, 0, 0).unwrap();
            let date = Date::from_calendar_date(2026, Month::May, 15).unwrap();
            let time = Time::from_hms_micro(14, 30, 45, 123_456).unwrap();
            let ts = PrimitiveDateTime::new(date, time).assume_offset(offset);
            let py_dt = new_timestamp(py, &ts).unwrap();
            assert_eq!(
                py_dt
                    .call_method0("isoformat")
                    .unwrap()
                    .extract::<String>()
                    .unwrap(),
                "2026-05-15T12:30:45.123456+00:00"
            );
            assert!(py_dt.getattr("tzinfo").unwrap().is(utc_tz(py).unwrap()));

            let out_of_range = OffsetDateTime::from_unix_timestamp(-62_167_219_200).unwrap();
            assert!(new_timestamp(py, &out_of_range).is_err());
            let db_err =
                crate::db::from_value(py, &hinoki_orm::Value::Timestamp(out_of_range)).unwrap_err();
            assert!(db_err.is_instance_of::<crate::db::DatabaseError>(py));
        });
    }
}
