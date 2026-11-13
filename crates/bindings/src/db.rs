//! Python bridge for the database engine: awaitables on the running asyncio
//! loop, backed by one Tokio runtime per worker process.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Once};
use std::time::Duration;

use hinoki_orm::{Database, DbError, ErrorKind, Transaction, Value};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString};
use tokio::sync::Mutex;

pyo3::create_exception!(native, DatabaseError, pyo3::exceptions::PyException);
pyo3::create_exception!(native, UniqueViolation, DatabaseError);
pyo3::create_exception!(native, ForeignKeyViolation, DatabaseError);
pyo3::create_exception!(native, NotNullViolation, DatabaseError);
pyo3::create_exception!(native, CheckViolation, DatabaseError);

static WORKER_THREADS: AtomicUsize = AtomicUsize::new(1);
static RUNTIME: Once = Once::new();

/// Create the Tokio runtime on first use (never at import). The thread count
/// can be set with `configure_runtime` before that point.
fn ensure_runtime() {
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

fn db_error(py: Python<'_>, err: &DbError) -> PyErr {
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

fn to_value(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
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
    if obj.is_instance(&py.import("uuid")?.getattr("UUID")?)? {
        let text: String = obj.str()?.extract()?;
        return uuid::Uuid::parse_str(&text)
            .map(Value::Uuid)
            .map_err(|e| PyValueError::new_err(e.to_string()));
    }
    if obj.is_instance(&py.import("datetime")?.getattr("datetime")?)? {
        if obj.getattr("tzinfo")?.is_none() {
            return Err(PyValueError::new_err(
                "datetime parameters must be timezone-aware",
            ));
        }
        let text: String = obj.call_method0("isoformat")?.extract()?;
        return chrono::DateTime::parse_from_rfc3339(&text)
            .map(|t| Value::Timestamp(t.with_timezone(&chrono::Utc)))
            .map_err(|e| PyValueError::new_err(e.to_string()));
    }
    if obj.is_instance_of::<PyDict>() || obj.is_instance_of::<PyList>() {
        let text: String = py
            .import("json")?
            .call_method1("dumps", (obj,))?
            .extract()?;
        return serde_json::from_str(&text)
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

fn from_value(py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::Null => py.None(),
        Value::Bool(b) => b.into_pyobject(py)?.to_owned().into_any().unbind(),
        Value::Int(i) => i.into_pyobject(py)?.into_any().unbind(),
        Value::Float(f) => f.into_pyobject(py)?.into_any().unbind(),
        Value::Text(s) => s.into_pyobject(py)?.into_any().unbind(),
        Value::Bytes(b) => PyBytes::new(py, b).into_any().unbind(),
        Value::Uuid(u) => py
            .import("uuid")?
            .getattr("UUID")?
            .call1((u.to_string(),))?
            .unbind(),
        Value::Timestamp(t) => py
            .import("datetime")?
            .getattr("datetime")?
            .call_method1("fromisoformat", (t.to_rfc3339(),))
            .map_err(|_| {
                let message = "timestamp cannot be represented as a Python datetime";
                db_error(py, &DbError::new(ErrorKind::Type, message))
            })?
            .unbind(),
        Value::Json(v) => py
            .import("json")?
            .call_method1("loads", (v.to_string(),))?
            .unbind(),
    })
}

fn rows_to_py(py: Python<'_>, rows: &[Vec<(String, Value)>]) -> PyResult<Py<PyAny>> {
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

/// Wait for a database future and convert its outcome to Python.
macro_rules! bridge {
    ($py:expr, $fut:expr, |$py2:ident, $ok:ident| $convert:expr) => {
        pyo3_async_runtimes::tokio::future_into_py($py, async move {
            match $fut.await {
                Ok($ok) => Python::attach(|$py2| $convert),
                Err(e) => Python::attach(|py| Err(db_error(py, &e))),
            }
        })
    };
}

/// A connection pool. Create it with `await Database.connect(...)` inside the
/// worker (module lifespan), never at import.
#[pyclass(name = "Database", module = "hinoki.native")]
pub struct PyDatabase {
    inner: Database,
}

#[pymethods]
impl PyDatabase {
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

    fn close(&self) {
        self.inner.close();
    }

    /// `(connections, idle)` currently in the pool.
    fn status(&self) -> (usize, usize) {
        self.inner.status()
    }
}

/// A transaction owning one connection. One statement at a time: concurrent
/// use raises instead of interleaving. Dropping it unfinished discards the
/// connection.
#[pyclass(name = "Transaction", module = "hinoki.native")]
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
    m.add_function(wrap_pyfunction!(configure_runtime, m)?)?;
    m.add("DatabaseError", py.get_type::<DatabaseError>())?;
    m.add("UniqueViolation", py.get_type::<UniqueViolation>())?;
    m.add("ForeignKeyViolation", py.get_type::<ForeignKeyViolation>())?;
    m.add("NotNullViolation", py.get_type::<NotNullViolation>())?;
    m.add("CheckViolation", py.get_type::<CheckViolation>())?;
    Ok(())
}
