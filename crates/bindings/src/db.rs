//! Python bridge for the database engine: awaitables on the running asyncio
//! loop, backed by one Tokio runtime per worker process.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Once};
use std::time::Duration;

use hinoki_orm::links::{apply, Canon, LinkOp, LinkWrite};
use hinoki_orm::{
    Database, DbError, ErrorKind, PageQueries, PgRows, PgSession, RawRows, RowSet, Transaction,
    Value,
};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString};
use tokio::sync::Mutex;

use crate::cursor::{CursorQuery, CursorWire, PyCursorCodec};
use crate::links::{outcome_to_py, raw_keys, scope_of, PyLinkSpec, RawScope};
use crate::modelrows::{models_to_py, PyModelRows};
use crate::rows::{
    encode_item_pg_with, encode_list_pg_with, encode_page_pg_with, group_prefetch_rows,
    group_prefetch_rows_pg, group_prefetch_rows_pg_single, group_prefetch_rows_single,
    PageWireResult, PrefetchPlan, PrefetchSlot, PrefetchedJson, PrefetchedRows,
    ProjectedListResult, ProjectedOneResult, ProjectedPageResult, PyProjectedRows, PyRowPlan,
    RowPlan,
};

pyo3::create_exception!(core, DatabaseError, pyo3::exceptions::PyException);
pyo3::create_exception!(core, UniqueViolation, DatabaseError);
pyo3::create_exception!(core, ForeignKeyViolation, DatabaseError);
pyo3::create_exception!(core, NotNullViolation, DatabaseError);
pyo3::create_exception!(core, CheckViolation, DatabaseError);

static WORKER_THREADS: AtomicUsize = AtomicUsize::new(1);
static RUNTIME: Once = Once::new();

/// Create the Tokio runtime on first use (never at import). The thread count
/// can be set with `configure_runtime` before that point.
pub(crate) fn ensure_runtime() {
    RUNTIME.call_once(|| {
        let mut builder = tokio::runtime::Builder::new_multi_thread();
        builder
            .worker_threads(WORKER_THREADS.load(Ordering::SeqCst))
            .enable_all();
        pyo3_async_runtimes::tokio::init(builder);
    });
}

/// Set the number of Tokio worker threads; only valid before the first
/// database call in this process.
#[pyfunction]
pub fn configure_runtime(worker_threads: usize) -> PyResult<()> {
    if RUNTIME.is_completed() {
        return Err(PyValueError::new_err(
            "the native runtime is already running",
        ));
    }
    if worker_threads == 0 {
        return Err(PyValueError::new_err("worker_threads must be >= 1"));
    }
    WORKER_THREADS.store(worker_threads, Ordering::SeqCst);
    Ok(())
}

pub(crate) fn db_error(py: Python<'_>, err: &DbError) -> PyErr {
    let kind = py.get_type::<DatabaseError>();
    let class = match err.kind {
        ErrorKind::Unique => py.get_type::<UniqueViolation>(),
        ErrorKind::ForeignKey => py.get_type::<ForeignKeyViolation>(),
        ErrorKind::NotNull => py.get_type::<NotNullViolation>(),
        ErrorKind::Check => py.get_type::<CheckViolation>(),
        _ => kind,
    };
    match class.call1((err.message.clone(),)) {
        Ok(instance) => {
            let _ = instance.setattr("constraint", err.constraint.clone());
            let _ = instance.setattr("table", err.table.clone());
            let _ = instance.setattr("sqlstate", err.sqlstate.clone());
            PyErr::from_value(instance)
        }
        Err(e) => e,
    }
}

const MICROS_PER_SECOND: i128 = 1_000_000;
const MICROS_PER_DAY: i128 = 86_400 * MICROS_PER_SECOND;

/// The column form of the values a `Model` column stores in a plain type: a
/// `date` and a `Decimal` as text, a `time` as fixed-width text, a `timedelta`
/// as whole microseconds and an `Enum` member as its value.
fn stored_scalar(obj: &Bound<'_, PyAny>) -> PyResult<Option<Value>> {
    let py = obj.py();
    if obj.is_instance(crate::pytypes::date_class(py)?)? {
        return Ok(Some(Value::Text(obj.call_method0("isoformat")?.extract()?)));
    }
    if obj.is_instance(crate::pytypes::time_class(py)?)? {
        if !obj.getattr("tzinfo")?.is_none() {
            return Err(PyValueError::new_err(
                "time parameters must not carry a timezone",
            ));
        }
        let kwargs = pyo3::types::PyDict::new(py);
        kwargs.set_item("timespec", "microseconds")?;
        let text = obj.call_method("isoformat", (), Some(&kwargs))?;
        return Ok(Some(Value::Text(text.extract()?)));
    }
    if obj.is_instance(crate::pytypes::timedelta_class(py)?)? {
        return duration_micros(obj).map(|micros| Some(Value::Int(micros)));
    }
    if obj.is_instance(crate::pytypes::decimal_class(py)?)? {
        if !obj.call_method0("is_finite")?.extract::<bool>()? {
            return Err(PyValueError::new_err("a Decimal parameter must be finite"));
        }
        return Ok(Some(Value::Text(obj.str()?.extract()?)));
    }
    match crate::pytypes::enum_value(obj)? {
        Some(value) => to_value(&value).map(Some),
        None => Ok(None),
    }
}

fn duration_micros(obj: &Bound<'_, PyAny>) -> PyResult<i64> {
    let days: i128 = obj.getattr("days")?.extract()?;
    let seconds: i128 = obj.getattr("seconds")?.extract()?;
    let micros: i128 = obj.getattr("microseconds")?.extract()?;
    let total = days * MICROS_PER_DAY + seconds * MICROS_PER_SECOND + micros;
    i64::try_from(total)
        .map_err(|_| PyValueError::new_err("timedelta out of range for a 64-bit column"))
}

pub(crate) fn to_value(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    let py = obj.py();
    if obj.is_none() {
        return Ok(Value::Null);
    }
    if obj.is_instance_of::<PyBool>() {
        return Ok(Value::Bool(obj.extract()?));
    }
    if obj.is_instance_of::<PyInt>() {
        return obj
            .extract::<i64>()
            .map(Value::Int)
            .map_err(|_| PyValueError::new_err("integer out of range for a 64-bit column"));
    }
    if obj.is_instance_of::<PyFloat>() {
        return Ok(Value::Float(obj.extract()?));
    }
    if obj.is_instance_of::<PyString>() {
        return Ok(Value::Text(obj.extract()?));
    }
    if obj.is_instance_of::<PyBytes>() {
        return Ok(Value::Bytes(obj.extract()?));
    }
    if let Some(id) = crate::pytypes::exact_uuid(obj)? {
        return Ok(Value::Uuid(id));
    }
    if obj.is_instance(crate::pytypes::uuid_class(py)?)? {
        let text: String = obj.str()?.extract()?;
        return uuid::Uuid::parse_str(&text)
            .map(Value::Uuid)
            .map_err(|e| PyValueError::new_err(e.to_string()));
    }
    if crate::pytypes::is_ip_object(obj)? {
        return Ok(Value::Text(obj.str()?.extract()?));
    }
    if obj.is_instance(crate::pytypes::datetime_class(py)?)? {
        if obj.getattr(pyo3::intern!(py, "tzinfo"))?.is_none() {
            return Err(PyValueError::new_err(
                "datetime parameters must be timezone-aware",
            ));
        }
        let text: String = obj
            .call_method0(pyo3::intern!(py, "isoformat"))?
            .extract()?;
        return hinoki_orm::parse_timestamp(&text)
            .map(Value::Timestamp)
            .ok_or_else(|| PyValueError::new_err("invalid datetime parameter"));
    }
    if let Some(value) = stored_scalar(obj)? {
        return Ok(value);
    }
    if obj.is_instance_of::<PyDict>() || obj.is_instance_of::<PyList>() {
        let text = crate::request::encode_json(py, obj, None, false, None, false)?;
        return serde_json::from_slice(text.as_bytes())
            .map(Value::Json)
            .map_err(|e| PyValueError::new_err(e.to_string()));
    }
    Err(PyTypeError::new_err(format!(
        "unsupported parameter type {}",
        obj.get_type().name()?
    )))
}

fn to_values(params: &Bound<'_, PyList>) -> PyResult<Vec<Value>> {
    params.iter().map(|p| to_value(&p)).collect()
}

pub(crate) fn from_value(py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::Null => py.None(),
        Value::Bool(b) => b.into_pyobject(py)?.to_owned().into_any().unbind(),
        Value::Int(i) => i.into_pyobject(py)?.into_any().unbind(),
        Value::Float(f) => f.into_pyobject(py)?.into_any().unbind(),
        Value::Text(s) => s.into_pyobject(py)?.into_any().unbind(),
        Value::Bytes(b) => PyBytes::new(py, b).into_any().unbind(),
        Value::Uuid(u) => crate::pytypes::new_uuid(py, *u)?.unbind(),
        Value::Timestamp(t) => crate::pytypes::new_timestamp(py, t)
            .map_err(|_| {
                let message = "timestamp cannot be represented as a Python datetime";
                db_error(py, &DbError::new(ErrorKind::Type, message))
            })?
            .unbind(),
        Value::Json(v) => json_value_to_py(py, v)?,
    })
}

pub(crate) fn json_value_to_py(py: Python<'_>, value: &serde_json::Value) -> PyResult<Py<PyAny>> {
    match value {
        serde_json::Value::Null => Ok(py.None()),
        serde_json::Value::Bool(b) => Ok(b.into_pyobject(py)?.to_owned().into_any().unbind()),
        serde_json::Value::Number(n) => json_number_to_py(py, n),
        serde_json::Value::String(s) => Ok(s.into_pyobject(py)?.into_any().unbind()),
        serde_json::Value::Array(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(json_value_to_py(py, item)?)?;
            }
            Ok(list.into_any().unbind())
        }
        serde_json::Value::Object(entries) => {
            let dict = PyDict::new(py);
            for (k, v) in entries {
                dict.set_item(k, json_value_to_py(py, v)?)?;
            }
            Ok(dict.into_any().unbind())
        }
    }
}

fn json_number_to_py(py: Python<'_>, n: &serde_json::Number) -> PyResult<Py<PyAny>> {
    if let Some(i) = n.as_i64() {
        return Ok(i.into_pyobject(py)?.into_any().unbind());
    }
    if let Some(u) = n.as_u64() {
        return Ok(u.into_pyobject(py)?.into_any().unbind());
    }
    let raw = n.as_str();
    if !raw.contains(['.', 'e', 'E']) {
        return Ok(py.get_type::<PyInt>().call1((raw,))?.unbind());
    }
    let f = n
        .as_f64()
        .ok_or_else(|| PyValueError::new_err("JSON number out of range"))?;
    Ok(f.into_pyobject(py)?.into_any().unbind())
}

pub(crate) fn rows_to_py(py: Python<'_>, rows: &[Vec<(String, Value)>]) -> PyResult<Py<PyAny>> {
    let out = PyList::empty(py);
    for row in rows {
        let dict = PyDict::new(py);
        for (name, value) in row {
            dict.set_item(name, from_value(py, value)?)?;
        }
        out.append(dict)?;
    }
    Ok(out.into_any().unbind())
}

pub(crate) fn row_set_to_py(py: Python<'_>, set: &RowSet) -> PyResult<Py<PyAny>> {
    let keys: Vec<Bound<'_, PyString>> = set
        .columns
        .iter()
        .map(|name| PyString::new(py, name))
        .collect();
    let out = PyList::empty(py);
    for row in &set.rows {
        let dict = PyDict::new(py);
        for (key, value) in keys.iter().zip(row) {
            dict.set_item(key, from_value(py, value)?)?;
        }
        out.append(dict)?;
    }
    Ok(out.into_any().unbind())
}

/// The encoded page as bytes, or `(rows, has_more)` for Python to finish.
fn cursor_wire_to_py(py: Python<'_>, result: CursorWire) -> PyResult<Py<PyAny>> {
    match result {
        CursorWire::Encoded(bytes) => Ok(PyBytes::new(py, &bytes).into_any().unbind()),
        CursorWire::Fallback(rows, has_more) => {
            let rows = rows_to_py(py, &rows)?;
            Ok((rows, has_more).into_pyobject(py)?.into_any().unbind())
        }
    }
}

/// Run a database future off the loop and convert its outcome on the loop
/// thread (see `completion`).
macro_rules! bridge {
    ($py:expr, $fut:expr, |$py2:ident, $ok:ident| $convert:expr) => {
        crate::completion::submit($py, $fut, move |$py2, $ok| $convert)
    };
}

/// A connection pool. Create it with `await Database.connect(...)` inside the
/// worker (module lifespan), never at import.
#[pyclass(name = "Database", module = "hinoki.core")]
pub struct PyDatabase {
    inner: Database,
}

impl PyDatabase {
    pub(crate) fn clone_inner(&self) -> Database {
        self.inner.clone()
    }
}

#[pymethods]
impl PyDatabase {
    #[getter]
    fn dialect(&self) -> &'static str {
        self.inner.dialect()
    }

    #[staticmethod]
    #[pyo3(signature = (url, max_size=8, wait_ms=5000))]
    fn connect<'py>(
        py: Python<'py>,
        url: String,
        max_size: usize,
        wait_ms: u64,
    ) -> PyResult<Bound<'py, PyAny>> {
        ensure_runtime();
        let created = Database::connect(&url, max_size, Duration::from_millis(wait_ms));
        let fut = async move {
            let db = created?;
            db.check().await?;
            Ok::<_, DbError>(db)
        };
        bridge!(py, fut, |py2, db| Ok(Py::new(
            py2,
            PyDatabase { inner: db }
        )?
        .into_any()))
    }

    fn fetch<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (db, values) = (self.inner.clone(), to_values(params)?);
        let fut = async move { db.fetch(&sql, &values).await };
        bridge!(py, fut, |py2, rows| rows_to_py(py2, &rows))
    }

    fn fetch_models<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyModelRows>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (db, values, plan) = (self.inner.clone(), to_values(params)?, plan.inner.clone());
        let fut = async move { db.fetch(&sql, &values).await };
        bridge!(py, fut, |py2, rows| models_to_py(py2, &rows, &plan))
    }

    #[pyo3(signature = (rows_sql, rows_params, count_sql, count_params, plan, concurrent=false))]
    #[allow(clippy::too_many_arguments)]
    fn fetch_page_models<'py>(
        &self,
        py: Python<'py>,
        rows_sql: String,
        rows_params: &Bound<'py, PyList>,
        count_sql: String,
        count_params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyModelRows>,
        concurrent: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let db = self.inner.clone();
        let rows_values = to_values(rows_params)?;
        let count_values = to_values(count_params)?;
        let plan = plan.inner.clone();
        let fut = async move {
            let queries = PageQueries {
                rows_sql: &rows_sql,
                rows_params: &rows_values,
                count_sql: &count_sql,
                count_params: &count_values,
            };
            db.fetch_page(&queries, concurrent).await
        };
        bridge!(py, fut, |py2, result| {
            let (rows, total) = result;
            let models = models_to_py(py2, &rows, &plan)?;
            Ok((models, total).into_pyobject(py2)?.into_any().unbind())
        })
    }

    #[pyo3(signature = (rows_sql, rows_params, count_sql, count_params, concurrent=false))]
    fn fetch_page_rows<'py>(
        &self,
        py: Python<'py>,
        rows_sql: String,
        rows_params: &Bound<'py, PyList>,
        count_sql: String,
        count_params: &Bound<'py, PyList>,
        concurrent: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let db = self.inner.clone();
        let rows_values = to_values(rows_params)?;
        let count_values = to_values(count_params)?;
        let fut = async move {
            let queries = PageQueries {
                rows_sql: &rows_sql,
                rows_params: &rows_values,
                count_sql: &count_sql,
                count_params: &count_values,
            };
            db.fetch_page(&queries, concurrent).await
        };
        bridge!(py, fut, |py2, result| {
            let (rows, total) = result;
            let py_rows = rows_to_py(py2, &rows)?;
            Ok((py_rows, total).into_pyobject(py2)?.into_any().unbind())
        })
    }

    #[pyo3(signature = (sql, params, plan))]
    fn fetch_projected_list<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyRowPlan>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (db, values, plan) = (self.inner.clone(), to_values(params)?, plan.inner.clone());
        let fut = async move {
            if let Some(session) = db.acquire_pg().await? {
                let rows = session.fetch_pg(&sql, &values).await?;
                let result = finish_projected_list_pg(&session, rows, &plan).await?;
                session.release();
                return Ok(result);
            }
            let rows = db.fetch_set(&sql, &values).await?;
            finish_projected_list_db(&db, rows, &plan).await
        };
        bridge!(py, fut, |py2, result| projected_list_to_py(py2, result))
    }

    #[pyo3(signature = (sql, params, plan))]
    fn fetch_projected_one<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyRowPlan>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (db, values, plan) = (self.inner.clone(), to_values(params)?, plan.inner.clone());
        let fut = async move {
            if let Some(session) = db.acquire_pg().await? {
                let rows = session.fetch_pg(&sql, &values).await?;
                let result = finish_projected_one_pg(&session, rows, &plan).await?;
                session.release();
                return Ok(result);
            }
            let rows = db.fetch_set(&sql, &values).await?;
            finish_projected_one_db(&db, rows, &plan).await
        };
        bridge!(py, fut, |py2, result| projected_one_to_py(py2, result))
    }

    #[pyo3(signature = (rows_sql, rows_params, count_sql, count_params, plan, page, page_size, concurrent=false))]
    #[allow(clippy::too_many_arguments)]
    fn fetch_projected_page<'py>(
        &self,
        py: Python<'py>,
        rows_sql: String,
        rows_params: &Bound<'py, PyList>,
        count_sql: String,
        count_params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyRowPlan>,
        page: u64,
        page_size: u64,
        concurrent: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let db = self.inner.clone();
        let rows_values = to_values(rows_params)?;
        let count_values = to_values(count_params)?;
        let plan = plan.inner.clone();
        let fut = async move {
            let queries = PageQueries {
                rows_sql: &rows_sql,
                rows_params: &rows_values,
                count_sql: &count_sql,
                count_params: &count_values,
            };
            if let Some(session) = db.acquire_pg().await? {
                let (rows, total) = session.fetch_pg_page(&queries, concurrent).await?;
                let result =
                    finish_projected_page_pg(&session, rows, total, &plan, page, page_size).await?;
                session.release();
                return Ok(result);
            }
            let (rows, total) = db.fetch_page_set(&queries, concurrent).await?;
            finish_projected_page_db(&db, rows, total, &plan, page, page_size).await
        };
        bridge!(py, fut, |py2, result| projected_page_to_py(
            py2, result, page, page_size
        ))
    }

    #[pyo3(signature = (sql, params, plan, page_info))]
    fn _fetch_page_wire<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyRowPlan>,
        page_info: (u64, u64, u64),
    ) -> PyResult<Bound<'py, PyAny>> {
        let (page, page_size, total) = page_info;
        let (db, values, plan) = (self.inner.clone(), to_values(params)?, plan.inner.clone());
        let fut = async move {
            let rows = db.fetch(&sql, &values).await?;
            Ok::<_, DbError>(
                match crate::rows::encode_page(&rows, &plan, page, page_size, total) {
                    Some(bytes) => PageWireResult::Encoded(bytes),
                    None => PageWireResult::Fallback(rows),
                },
            )
        };
        bridge!(py, fut, |py2, result| match result {
            PageWireResult::Encoded(bytes) => Ok(PyBytes::new(py2, &bytes).into_any().unbind()),
            PageWireResult::Fallback(rows) => rows_to_py(py2, &rows),
        })
    }

    #[pyo3(signature = (sql, params, plan, codec, keys, limit, mode))]
    #[allow(clippy::too_many_arguments)]
    fn _fetch_cursor_wire<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyRowPlan>,
        codec: PyRef<'py, PyCursorCodec>,
        keys: Vec<String>,
        limit: u64,
        mode: u8,
    ) -> PyResult<Bound<'py, PyAny>> {
        let query = CursorQuery::from_python(&codec, keys, limit, mode)?;
        let (db, values, plan) = (self.inner.clone(), to_values(params)?, plan.inner.clone());
        let fut = async move {
            let rows = db.fetch(&sql, &values).await?;
            Ok::<_, DbError>(query.finish(rows, &plan))
        };
        bridge!(py, fut, |py2, result| cursor_wire_to_py(py2, result))
    }

    fn execute<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (db, values) = (self.inner.clone(), to_values(params)?);
        let fut = async move { db.execute(&sql, &values).await };
        bridge!(py, fut, |py2, count| Ok(count
            .into_pyobject(py2)?
            .into_any()
            .unbind()))
    }

    fn begin<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let db = self.inner.clone();
        let fut = async move { db.begin().await };
        bridge!(py, fut, |py2, tx| Ok(Py::new(
            py2,
            PyTransaction {
                inner: Arc::new(Mutex::new(Some(tx)))
            }
        )?
        .into_any()))
    }

    fn begin_migration<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let db = self.inner.clone();
        let fut = async move { db.begin_migration().await };
        bridge!(py, fut, |py2, tx| Ok(Py::new(
            py2,
            PyTransaction {
                inner: Arc::new(Mutex::new(Some(tx)))
            }
        )?
        .into_any()))
    }

    fn close(&self) {
        self.inner.close();
    }

    /// `(connections, idle)` currently in the pool.
    fn status(&self) -> (usize, usize) {
        self.inner.status()
    }

    #[cfg(feature = "sql-trace")]
    fn _clear_sql_trace(&self) {
        self.inner.clear_sql_trace();
    }

    #[cfg(feature = "sql-trace")]
    fn _take_sql_trace<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let out = PyList::empty(py);
        for entry in self.inner.take_sql_trace() {
            let values = PyList::empty(py);
            for value in &entry.values {
                values.append(from_value(py, value)?)?;
            }
            out.append((entry.kind, entry.sql, values))?;
        }
        Ok(out.into_any())
    }
}

const PREFETCH_IN_CHUNK: usize = 500;

async fn fetch_one_prefetch_db(
    db: &Database,
    prefetch: &PrefetchPlan,
    owners: &[Value],
) -> Result<RowSet, DbError> {
    if owners.len() <= PREFETCH_IN_CHUNK {
        let (sql, params) = prefetch
            .compile_chunk(owners)
            .map_err(|e| DbError::new(ErrorKind::Other, e.to_string()))?;
        return db.fetch_set(&sql, &params).await;
    }
    let mut all = RowSet::default();
    for chunk in owners.chunks(PREFETCH_IN_CHUNK) {
        let (sql, params) = prefetch
            .compile_chunk(chunk)
            .map_err(|e| DbError::new(ErrorKind::Other, e.to_string()))?;
        all.extend_from(db.fetch_set(&sql, &params).await?);
    }
    Ok(all)
}

async fn fetch_one_prefetch_tx(
    tx: &Transaction,
    prefetch: &PrefetchPlan,
    owners: &[Value],
) -> Result<RowSet, DbError> {
    if owners.len() <= PREFETCH_IN_CHUNK {
        let (sql, params) = prefetch
            .compile_chunk(owners)
            .map_err(|e| DbError::new(ErrorKind::Other, e.to_string()))?;
        return tx.fetch_set(&sql, &params).await;
    }
    let mut all = RowSet::default();
    for chunk in owners.chunks(PREFETCH_IN_CHUNK) {
        let (sql, params) = prefetch
            .compile_chunk(chunk)
            .map_err(|e| DbError::new(ErrorKind::Other, e.to_string()))?;
        all.extend_from(tx.fetch_set(&sql, &params).await?);
    }
    Ok(all)
}

fn group_all_prefetches(
    plan: &RowPlan,
    raw: Vec<RowSet>,
    single_owner: Option<&Canon>,
) -> Option<(PrefetchedJson, PrefetchedRows)> {
    let mut encoded = Vec::with_capacity(raw.len());
    let mut retained = Vec::with_capacity(raw.len());
    for (prefetch, rows) in plan.prefetches().iter().zip(raw) {
        let grouped = match single_owner {
            Some(owner) => {
                group_prefetch_rows_single(prefetch, plan.key_kind(), owner.clone(), &rows)?
            }
            None => group_prefetch_rows(prefetch, plan.key_kind(), &rows)?,
        };
        encoded.push(grouped);
        retained.push((prefetch.name.clone(), RawRows::Decoded(rows)));
    }
    Some((encoded, retained))
}

fn empty_prefetches(plan: &RowPlan) -> (PrefetchedJson, PrefetchedRows) {
    let encoded = vec![PrefetchSlot::empty(); plan.prefetches().len()];
    let retained = plan
        .prefetches()
        .iter()
        .map(|p| (p.name.clone(), RawRows::Decoded(RowSet::default())))
        .collect();
    (encoded, retained)
}

async fn fetch_one_prefetch_pg(
    session: &PgSession,
    prefetch: &PrefetchPlan,
    owners: &[Value],
) -> Result<PgRows, DbError> {
    if owners.len() <= PREFETCH_IN_CHUNK {
        let (sql, params) = prefetch
            .compile_chunk(owners)
            .map_err(|e| DbError::new(ErrorKind::Other, e.to_string()))?;
        return session.fetch_pg(&sql, &params).await;
    }
    let mut all = PgRows::default();
    for chunk in owners.chunks(PREFETCH_IN_CHUNK) {
        let (sql, params) = prefetch
            .compile_chunk(chunk)
            .map_err(|e| DbError::new(ErrorKind::Other, e.to_string()))?;
        all.extend_from(session.fetch_pg(&sql, &params).await?);
    }
    Ok(all)
}

fn group_all_prefetches_pg(
    plan: &RowPlan,
    raw: Vec<PgRows>,
    single_owner: Option<&Canon>,
) -> Option<(PrefetchedJson, PrefetchedRows)> {
    let mut encoded = Vec::with_capacity(raw.len());
    let mut retained = Vec::with_capacity(raw.len());
    for (prefetch, rows) in plan.prefetches().iter().zip(raw) {
        let grouped = match single_owner {
            Some(owner) => {
                group_prefetch_rows_pg_single(prefetch, plan.key_kind(), owner.clone(), &rows)?
            }
            None => group_prefetch_rows_pg(prefetch, plan.key_kind(), &rows)?,
        };
        encoded.push(grouped);
        retained.push((prefetch.name.clone(), RawRows::Postgres(rows)));
    }
    Some((encoded, retained))
}

async fn load_prefetches_pg(
    session: &PgSession,
    plan: &RowPlan,
    rows: &PgRows,
) -> Result<Option<(PrefetchedJson, PrefetchedRows)>, DbError> {
    let prefetches = plan.prefetches();
    if prefetches.is_empty() {
        return Ok(Some((Vec::new(), Vec::new())));
    }
    if rows.is_empty() {
        return Ok(Some(empty_prefetches(plan)));
    }
    let Some((owners, single_owner)) = plan.owner_values_pg(rows) else {
        return Ok(None);
    };
    let raw = match prefetches {
        [one] => vec![fetch_one_prefetch_pg(session, one, &owners).await?],
        [first, second] if owners.len() <= PREFETCH_IN_CHUNK => {
            let (sql_a, params_a) = first
                .compile_chunk(&owners)
                .map_err(|e| DbError::new(ErrorKind::Other, e.to_string()))?;
            let (sql_b, params_b) = second
                .compile_chunk(&owners)
                .map_err(|e| DbError::new(ErrorKind::Other, e.to_string()))?;
            let (a, b) = session
                .fetch_pg_pair((&sql_a, &params_a), (&sql_b, &params_b))
                .await?;
            vec![a, b]
        }
        many => {
            let mut out = Vec::with_capacity(many.len());
            for prefetch in many {
                out.push(fetch_one_prefetch_pg(session, prefetch, &owners).await?);
            }
            out
        }
    };
    Ok(group_all_prefetches_pg(plan, raw, single_owner.as_ref()))
}

async fn finish_projected_list_pg(
    session: &PgSession,
    rows: PgRows,
    plan: &RowPlan,
) -> Result<ProjectedListResult, DbError> {
    let Some((encoded, retained)) = load_prefetches_pg(session, plan, &rows).await? else {
        return Ok(ProjectedListResult::Fallback(rows.to_row_set()?));
    };
    Ok(match encode_list_pg_with(&rows, plan, &encoded) {
        Some(json) => ProjectedListResult::Encoded(json, RawRows::Postgres(rows), retained),
        None => ProjectedListResult::Fallback(rows.to_row_set()?),
    })
}

async fn finish_projected_one_pg(
    session: &PgSession,
    rows: PgRows,
    plan: &RowPlan,
) -> Result<ProjectedOneResult, DbError> {
    if rows.is_empty() {
        return Ok(ProjectedOneResult::Empty);
    }
    if rows.len() > 1 {
        return Ok(ProjectedOneResult::Multiple);
    }
    let Some((encoded, retained)) = load_prefetches_pg(session, plan, &rows).await? else {
        return Ok(ProjectedOneResult::Fallback(rows.to_row_set()?));
    };
    Ok(match encode_item_pg_with(&rows, plan, &encoded) {
        Some(json) => ProjectedOneResult::Encoded(json, RawRows::Postgres(rows), retained),
        None => ProjectedOneResult::Fallback(rows.to_row_set()?),
    })
}

async fn finish_projected_page_pg(
    session: &PgSession,
    rows: PgRows,
    total: i64,
    plan: &RowPlan,
    page: u64,
    page_size: u64,
) -> Result<ProjectedPageResult, DbError> {
    let Some(count) = u64::try_from(total).ok() else {
        return Ok(ProjectedPageResult::Fallback(rows.to_row_set()?, total));
    };
    let Some((encoded, retained)) = load_prefetches_pg(session, plan, &rows).await? else {
        return Ok(ProjectedPageResult::Fallback(rows.to_row_set()?, total));
    };
    Ok(
        match encode_page_pg_with(&rows, plan, &encoded, page, page_size, count) {
            Some(json) => {
                ProjectedPageResult::Encoded(json, RawRows::Postgres(rows), retained, count)
            }
            None => ProjectedPageResult::Fallback(rows.to_row_set()?, total),
        },
    )
}

async fn load_prefetches_db(
    db: &Database,
    plan: &RowPlan,
    rows: &RowSet,
) -> Result<Option<(PrefetchedJson, PrefetchedRows)>, DbError> {
    let prefetches = plan.prefetches();
    if prefetches.is_empty() {
        return Ok(Some((Vec::new(), Vec::new())));
    }
    if rows.is_empty() {
        return Ok(Some(empty_prefetches(plan)));
    }
    let Some((owners, single_owner)) = plan.owner_values_set(rows) else {
        return Ok(None);
    };
    let raw = match prefetches {
        [one] => vec![fetch_one_prefetch_db(db, one, &owners).await?],
        [first, second] => {
            let (a, b) = tokio::try_join!(
                fetch_one_prefetch_db(db, first, &owners),
                fetch_one_prefetch_db(db, second, &owners),
            )?;
            vec![a, b]
        }
        many => {
            let mut out = Vec::with_capacity(many.len());
            for prefetch in many {
                out.push(fetch_one_prefetch_db(db, prefetch, &owners).await?);
            }
            out
        }
    };
    Ok(group_all_prefetches(plan, raw, single_owner.as_ref()))
}

async fn load_prefetches_tx(
    tx: &Transaction,
    plan: &RowPlan,
    rows: &RowSet,
) -> Result<Option<(PrefetchedJson, PrefetchedRows)>, DbError> {
    let prefetches = plan.prefetches();
    if prefetches.is_empty() {
        return Ok(Some((Vec::new(), Vec::new())));
    }
    if rows.is_empty() {
        return Ok(Some(empty_prefetches(plan)));
    }
    let Some((owners, single_owner)) = plan.owner_values_set(rows) else {
        return Ok(None);
    };
    let mut raw = Vec::with_capacity(prefetches.len());
    for prefetch in prefetches {
        raw.push(fetch_one_prefetch_tx(tx, prefetch, &owners).await?);
    }
    Ok(group_all_prefetches(plan, raw, single_owner.as_ref()))
}

async fn finish_projected_list_db(
    db: &Database,
    rows: RowSet,
    plan: &RowPlan,
) -> Result<ProjectedListResult, DbError> {
    let Some((encoded, retained)) = load_prefetches_db(db, plan, &rows).await? else {
        return Ok(ProjectedListResult::Fallback(rows));
    };
    Ok(
        match crate::rows::encode_list_set_with(&rows, plan, &encoded) {
            Some(json) => ProjectedListResult::Encoded(json, RawRows::Decoded(rows), retained),
            None => ProjectedListResult::Fallback(rows),
        },
    )
}

async fn finish_projected_list_tx(
    tx: &Transaction,
    rows: RowSet,
    plan: &RowPlan,
) -> Result<ProjectedListResult, DbError> {
    let Some((encoded, retained)) = load_prefetches_tx(tx, plan, &rows).await? else {
        return Ok(ProjectedListResult::Fallback(rows));
    };
    Ok(
        match crate::rows::encode_list_set_with(&rows, plan, &encoded) {
            Some(json) => ProjectedListResult::Encoded(json, RawRows::Decoded(rows), retained),
            None => ProjectedListResult::Fallback(rows),
        },
    )
}

async fn finish_projected_one_db(
    db: &Database,
    rows: RowSet,
    plan: &RowPlan,
) -> Result<ProjectedOneResult, DbError> {
    if rows.is_empty() {
        return Ok(ProjectedOneResult::Empty);
    }
    if rows.len() > 1 {
        return Ok(ProjectedOneResult::Multiple);
    }
    let Some((encoded, retained)) = load_prefetches_db(db, plan, &rows).await? else {
        return Ok(ProjectedOneResult::Fallback(rows));
    };
    Ok(
        match crate::rows::encode_item_set_with_prefetches(&rows, plan, &encoded) {
            Some(json) => ProjectedOneResult::Encoded(json, RawRows::Decoded(rows), retained),
            None => ProjectedOneResult::Fallback(rows),
        },
    )
}

async fn finish_projected_one_tx(
    tx: &Transaction,
    rows: RowSet,
    plan: &RowPlan,
) -> Result<ProjectedOneResult, DbError> {
    if rows.is_empty() {
        return Ok(ProjectedOneResult::Empty);
    }
    if rows.len() > 1 {
        return Ok(ProjectedOneResult::Multiple);
    }
    let Some((encoded, retained)) = load_prefetches_tx(tx, plan, &rows).await? else {
        return Ok(ProjectedOneResult::Fallback(rows));
    };
    Ok(
        match crate::rows::encode_item_set_with_prefetches(&rows, plan, &encoded) {
            Some(json) => ProjectedOneResult::Encoded(json, RawRows::Decoded(rows), retained),
            None => ProjectedOneResult::Fallback(rows),
        },
    )
}

async fn finish_projected_page_db(
    db: &Database,
    rows: RowSet,
    total: i64,
    plan: &RowPlan,
    page: u64,
    page_size: u64,
) -> Result<ProjectedPageResult, DbError> {
    let Some(count) = u64::try_from(total).ok() else {
        return Ok(ProjectedPageResult::Fallback(rows, total));
    };
    let Some((encoded, retained)) = load_prefetches_db(db, plan, &rows).await? else {
        return Ok(ProjectedPageResult::Fallback(rows, total));
    };
    Ok(
        match crate::rows::encode_page_set_with_prefetches(
            &rows,
            plan,
            &encoded,
            (page, page_size, count),
        ) {
            Some(json) => {
                ProjectedPageResult::Encoded(json, RawRows::Decoded(rows), retained, count)
            }
            None => ProjectedPageResult::Fallback(rows, total),
        },
    )
}

async fn finish_projected_page_tx(
    tx: &Transaction,
    rows: RowSet,
    total: i64,
    plan: &RowPlan,
    page: u64,
    page_size: u64,
) -> Result<ProjectedPageResult, DbError> {
    let Some(count) = u64::try_from(total).ok() else {
        return Ok(ProjectedPageResult::Fallback(rows, total));
    };
    let Some((encoded, retained)) = load_prefetches_tx(tx, plan, &rows).await? else {
        return Ok(ProjectedPageResult::Fallback(rows, total));
    };
    Ok(
        match crate::rows::encode_page_set_with_prefetches(
            &rows,
            plan,
            &encoded,
            (page, page_size, count),
        ) {
            Some(json) => {
                ProjectedPageResult::Encoded(json, RawRows::Decoded(rows), retained, count)
            }
            None => ProjectedPageResult::Fallback(rows, total),
        },
    )
}

fn projected_list_to_py(py: Python<'_>, result: ProjectedListResult) -> PyResult<Py<PyAny>> {
    match result {
        ProjectedListResult::Encoded(json, rows, prefetched) => {
            Ok(Py::new(py, PyProjectedRows::new(json, rows, prefetched, None))?.into_any())
        }
        ProjectedListResult::Fallback(rows) => row_set_to_py(py, &rows),
    }
}

fn projected_one_to_py(py: Python<'_>, result: ProjectedOneResult) -> PyResult<Py<PyAny>> {
    match result {
        ProjectedOneResult::Encoded(json, rows, prefetched) => {
            Ok(Py::new(py, PyProjectedRows::new(json, rows, prefetched, None))?.into_any())
        }
        ProjectedOneResult::Empty => Ok(py.None()),
        ProjectedOneResult::Multiple => Ok(PyString::new(py, "multiple").into_any().unbind()),
        ProjectedOneResult::Fallback(rows) => row_set_to_py(py, &rows),
    }
}

fn projected_page_to_py(
    py: Python<'_>,
    result: ProjectedPageResult,
    page: u64,
    page_size: u64,
) -> PyResult<Py<PyAny>> {
    match result {
        ProjectedPageResult::Encoded(json, rows, prefetched, total) => Ok(Py::new(
            py,
            PyProjectedRows::new(json, rows, prefetched, Some((page, page_size, total))),
        )?
        .into_any()),
        ProjectedPageResult::Fallback(rows, total) => {
            let py_rows = row_set_to_py(py, &rows)?;
            Ok((py_rows, total).into_pyobject(py)?.into_any().unbind())
        }
    }
}

fn coerce_one(kind: &hinoki_orm::links::KeyKind, key: &Bound<'_, PyAny>) -> PyResult<Canon> {
    let list = PyList::new(key.py(), [key])?;
    coerce_all(kind, &list)?
        .pop()
        .ok_or_else(|| PyValueError::new_err("invalid link key"))
}

/// Keys already coerced by `coerce_link_keys`, checked again against `kind`.
fn coerce_all(kind: &hinoki_orm::links::KeyKind, keys: &Bound<'_, PyList>) -> PyResult<Vec<Canon>> {
    hinoki_orm::links::coerce_keys(*kind, &raw_keys(keys)?, hinoki_orm::links::MAX_LINK_ITEMS)
        .map_err(|_| PyValueError::new_err("invalid link keys"))
}

/// A transaction owning one connection. One statement at a time: concurrent
/// use raises instead of interleaving. Dropping it unfinished discards the
/// connection.
#[pyclass(name = "Transaction", module = "hinoki.core")]
pub struct PyTransaction {
    inner: Arc<Mutex<Option<Transaction>>>,
}

#[pymethods]
impl PyTransaction {
    fn fetch<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (inner, values) = (self.inner.clone(), to_values(params)?);
        let fut = async move {
            let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
            let tx = guard.as_ref().ok_or_else(busy_error)?;
            tx.fetch(&sql, &values).await
        };
        bridge!(py, fut, |py2, rows| rows_to_py(py2, &rows))
    }

    fn fetch_models<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyModelRows>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (inner, values, plan) = (self.inner.clone(), to_values(params)?, plan.inner.clone());
        let fut = async move {
            let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
            let tx = guard.as_ref().ok_or_else(busy_error)?;
            tx.fetch(&sql, &values).await
        };
        bridge!(py, fut, |py2, rows| models_to_py(py2, &rows, &plan))
    }

    #[pyo3(signature = (rows_sql, rows_params, count_sql, count_params, plan, concurrent=false))]
    #[allow(clippy::too_many_arguments)]
    fn fetch_page_models<'py>(
        &self,
        py: Python<'py>,
        rows_sql: String,
        rows_params: &Bound<'py, PyList>,
        count_sql: String,
        count_params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyModelRows>,
        concurrent: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let _ = concurrent; // Transactions own one connection and always execute sequentially.
        let inner = self.inner.clone();
        let rows_values = to_values(rows_params)?;
        let count_values = to_values(count_params)?;
        let plan = plan.inner.clone();
        let fut = async move {
            let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
            let tx = guard.as_ref().ok_or_else(busy_error)?;
            let queries = PageQueries {
                rows_sql: &rows_sql,
                rows_params: &rows_values,
                count_sql: &count_sql,
                count_params: &count_values,
            };
            tx.fetch_page(&queries).await
        };
        bridge!(py, fut, |py2, result| {
            let (rows, total) = result;
            let models = models_to_py(py2, &rows, &plan)?;
            Ok((models, total).into_pyobject(py2)?.into_any().unbind())
        })
    }

    #[pyo3(signature = (rows_sql, rows_params, count_sql, count_params, concurrent=false))]
    fn fetch_page_rows<'py>(
        &self,
        py: Python<'py>,
        rows_sql: String,
        rows_params: &Bound<'py, PyList>,
        count_sql: String,
        count_params: &Bound<'py, PyList>,
        concurrent: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let _ = concurrent; // Transactions own one connection and always execute sequentially.
        let inner = self.inner.clone();
        let rows_values = to_values(rows_params)?;
        let count_values = to_values(count_params)?;
        let fut = async move {
            let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
            let tx = guard.as_ref().ok_or_else(busy_error)?;
            let queries = PageQueries {
                rows_sql: &rows_sql,
                rows_params: &rows_values,
                count_sql: &count_sql,
                count_params: &count_values,
            };
            tx.fetch_page(&queries).await
        };
        bridge!(py, fut, |py2, result| {
            let (rows, total) = result;
            let py_rows = rows_to_py(py2, &rows)?;
            Ok((py_rows, total).into_pyobject(py2)?.into_any().unbind())
        })
    }

    #[pyo3(signature = (sql, params, plan))]
    fn fetch_projected_list<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyRowPlan>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (inner, values, plan) = (self.inner.clone(), to_values(params)?, plan.inner.clone());
        let fut = async move {
            let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
            let tx = guard.as_ref().ok_or_else(busy_error)?;
            let rows = tx.fetch_set(&sql, &values).await?;
            finish_projected_list_tx(tx, rows, &plan).await
        };
        bridge!(py, fut, |py2, result| projected_list_to_py(py2, result))
    }

    #[pyo3(signature = (sql, params, plan))]
    fn fetch_projected_one<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyRowPlan>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (inner, values, plan) = (self.inner.clone(), to_values(params)?, plan.inner.clone());
        let fut = async move {
            let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
            let tx = guard.as_ref().ok_or_else(busy_error)?;
            let rows = tx.fetch_set(&sql, &values).await?;
            finish_projected_one_tx(tx, rows, &plan).await
        };
        bridge!(py, fut, |py2, result| projected_one_to_py(py2, result))
    }

    #[pyo3(signature = (rows_sql, rows_params, count_sql, count_params, plan, page, page_size, concurrent=false))]
    #[allow(clippy::too_many_arguments)]
    fn fetch_projected_page<'py>(
        &self,
        py: Python<'py>,
        rows_sql: String,
        rows_params: &Bound<'py, PyList>,
        count_sql: String,
        count_params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyRowPlan>,
        page: u64,
        page_size: u64,
        concurrent: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let _ = concurrent; // Transactions own one connection and always execute sequentially.
        let inner = self.inner.clone();
        let rows_values = to_values(rows_params)?;
        let count_values = to_values(count_params)?;
        let plan = plan.inner.clone();
        let fut = async move {
            let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
            let tx = guard.as_ref().ok_or_else(busy_error)?;
            let queries = PageQueries {
                rows_sql: &rows_sql,
                rows_params: &rows_values,
                count_sql: &count_sql,
                count_params: &count_values,
            };
            let (rows, total) = tx.fetch_page_set(&queries).await?;
            finish_projected_page_tx(tx, rows, total, &plan, page, page_size).await
        };
        bridge!(py, fut, |py2, result| projected_page_to_py(
            py2, result, page, page_size
        ))
    }

    /// Add, remove or replace links of one owner (see `hinoki_orm::links`).
    /// `op` is `add`, `remove` or `set`; `keys` are the target keys already
    /// coerced by `coerce_link_keys`; `scope` is `(joins, filters, subfilters)`.
    /// Resolves to `("done", added, removed)`, `("owner_missing",)`,
    /// `("missing", count)` or `("limit",)`; only `done` may be committed.
    #[pyo3(signature = (spec, dialect, op, request, scope, owner_scope=None))]
    #[allow(clippy::too_many_arguments)]
    fn link_write<'py>(
        &self,
        py: Python<'py>,
        spec: &PyLinkSpec,
        dialect: &str,
        op: &str,
        request: (Bound<'py, PyAny>, Bound<'py, PyList>),
        scope: RawScope,
        owner_scope: Option<RawScope>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (owner, keys) = request;
        let dialect = crate::query::parse_dialect(dialect)?;
        let op = match op {
            "add" => LinkOp::Add,
            "remove" => LinkOp::Remove,
            "set" => LinkOp::Set,
            other => return Err(PyValueError::new_err(format!("unknown link op {other:?}"))),
        };
        let table = spec.table.clone();
        let write = LinkWrite {
            op,
            owner: coerce_one(&table.owner_kind, &owner)?,
            keys: coerce_all(&table.target_kind, &keys)?,
            scope: scope_of(py, dialect, scope)?,
            owner_scope: owner_scope
                .map(|raw| scope_of(py, dialect, raw))
                .transpose()?
                .unwrap_or_default(),
        };
        let inner = self.inner.clone();
        let fut = async move {
            let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
            let tx = guard.as_ref().ok_or_else(busy_error)?;
            apply(tx, dialect, &table, &write, &mut uuid::Uuid::new_v4).await
        };
        bridge!(py, fut, |py2, outcome| outcome_to_py(py2, &outcome))
    }

    #[pyo3(signature = (sql, params, plan, page_info))]
    fn _fetch_page_wire<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyRowPlan>,
        page_info: (u64, u64, u64),
    ) -> PyResult<Bound<'py, PyAny>> {
        let (page, page_size, total) = page_info;
        let (inner, values, plan) = (self.inner.clone(), to_values(params)?, plan.inner.clone());
        let fut = async move {
            let rows = {
                let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
                let tx = guard.as_ref().ok_or_else(busy_error)?;
                tx.fetch(&sql, &values).await?
            };
            Ok::<_, DbError>(
                match crate::rows::encode_page(&rows, &plan, page, page_size, total) {
                    Some(bytes) => PageWireResult::Encoded(bytes),
                    None => PageWireResult::Fallback(rows),
                },
            )
        };
        bridge!(py, fut, |py2, result| match result {
            PageWireResult::Encoded(bytes) => Ok(PyBytes::new(py2, &bytes).into_any().unbind()),
            PageWireResult::Fallback(rows) => rows_to_py(py2, &rows),
        })
    }

    #[pyo3(signature = (sql, params, plan, codec, keys, limit, mode))]
    #[allow(clippy::too_many_arguments)]
    fn _fetch_cursor_wire<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
        plan: PyRef<'py, PyRowPlan>,
        codec: PyRef<'py, PyCursorCodec>,
        keys: Vec<String>,
        limit: u64,
        mode: u8,
    ) -> PyResult<Bound<'py, PyAny>> {
        let query = CursorQuery::from_python(&codec, keys, limit, mode)?;
        let (inner, values, plan) = (self.inner.clone(), to_values(params)?, plan.inner.clone());
        let fut = async move {
            let rows = {
                let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
                let tx = guard.as_ref().ok_or_else(busy_error)?;
                tx.fetch(&sql, &values).await?
            };
            Ok::<_, DbError>(query.finish(rows, &plan))
        };
        bridge!(py, fut, |py2, result| cursor_wire_to_py(py2, result))
    }

    fn execute<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: &Bound<'py, PyList>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (inner, values) = (self.inner.clone(), to_values(params)?);
        let fut = async move {
            let guard = inner.try_lock_owned().map_err(|_| busy_error())?;
            let tx = guard.as_ref().ok_or_else(busy_error)?;
            tx.execute(&sql, &values).await
        };
        bridge!(py, fut, |py2, count| Ok(count
            .into_pyobject(py2)?
            .into_any()
            .unbind()))
    }

    fn commit<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.finish(py, true)
    }

    fn rollback<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.finish(py, false)
    }
}

fn busy_error() -> DbError {
    DbError::new(ErrorKind::Other, "transaction is busy or already finished")
}

impl PyTransaction {
    fn finish<'py>(&self, py: Python<'py>, commit: bool) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let fut = async move {
            // Wait for a statement still running in another task: leaving the
            // transaction open would hold its locks until garbage collection.
            let mut guard = inner.lock_owned().await;
            let tx = guard.take().ok_or_else(busy_error)?;
            if commit {
                tx.commit().await
            } else {
                tx.rollback().await
            }
        };
        bridge!(py, fut, |py2, _done| Ok(py2.None()))
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add_class::<PyDatabase>()?;
    m.add_class::<PyTransaction>()?;
    m.add_class::<PyCursorCodec>()?;
    m.add_function(wrap_pyfunction!(configure_runtime, m)?)?;
    let module_name = m.getattr("__name__")?;
    for exception in [
        py.get_type::<DatabaseError>(),
        py.get_type::<UniqueViolation>(),
        py.get_type::<ForeignKeyViolation>(),
        py.get_type::<NotNullViolation>(),
        py.get_type::<CheckViolation>(),
    ] {
        exception.setattr("__module__", &module_name)?;
    }
    m.add("DatabaseError", py.get_type::<DatabaseError>())?;
    m.add("UniqueViolation", py.get_type::<UniqueViolation>())?;
    m.add("ForeignKeyViolation", py.get_type::<ForeignKeyViolation>())?;
    m.add("NotNullViolation", py.get_type::<NotNullViolation>())?;
    m.add("CheckViolation", py.get_type::<CheckViolation>())?;
    Ok(())
}
