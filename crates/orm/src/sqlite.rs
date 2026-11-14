//! SQLite connection pool, value codec and statement execution.

use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::Duration;

use rusqlite::params_from_iter;
use rusqlite::types::{Value as SqliteValue, ValueRef};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio_rusqlite::{rusqlite, Connection};

use crate::error::{DbError, ErrorKind};
use crate::value::Value;
use crate::Row;

#[derive(Debug)]
enum SqliteCallError {
    Driver(rusqlite::Error),
    Hinoki(DbError),
}

impl From<rusqlite::Error> for SqliteCallError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Driver(error)
    }
}

struct Inner {
    path: PathBuf,
    available: Mutex<Vec<Connection>>,
    permits: Arc<Semaphore>,
    closed: AtomicBool,
    max_size: usize,
    wait: Duration,
}

#[derive(Clone)]
pub(crate) struct SqlitePool(Arc<Inner>);

pub(crate) struct SqliteLease {
    connection: Option<Connection>,
    inner: Arc<Inner>,
    _permit: OwnedSemaphorePermit,
    in_transaction: bool,
    foreign_keys_disabled: bool,
}

impl SqlitePool {
    pub(crate) fn open(path: &Path, max_size: usize, wait: Duration) -> Result<Self, DbError> {
        if max_size == 0 {
            return Err(DbError::new(
                ErrorKind::Connect,
                "pool size must be positive",
            ));
        }
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|_| {
                DbError::new(ErrorKind::Connect, "could not create database directory")
            })?;
        }
        if rusqlite::version_number() < 3_035_000 {
            return Err(DbError::new(
                ErrorKind::Connect,
                "SQLite 3.35 or newer is required for RETURNING",
            ));
        }
        let mut connections = Vec::with_capacity(max_size);
        for _ in 0..max_size {
            let connection = rusqlite::Connection::open(path).map_err(DbError::from_sqlite)?;
            connection
                .busy_timeout(Duration::from_secs(5))
                .map_err(DbError::from_sqlite)?;
            connection
                .pragma_update(None, "foreign_keys", "ON")
                .map_err(DbError::from_sqlite)?;
            connection
                .pragma_update(None, "journal_mode", "WAL")
                .map_err(DbError::from_sqlite)?;
            connections.push(Connection::from(connection));
        }
        Ok(Self(Arc::new(Inner {
            path: path.to_owned(),
            available: Mutex::new(connections),
            permits: Arc::new(Semaphore::new(max_size)),
            closed: AtomicBool::new(false),
            max_size,
            wait,
        })))
    }

    pub(crate) async fn acquire(&self) -> Result<SqliteLease, DbError> {
        if self.0.closed.load(Ordering::Acquire) {
            return Err(DbError::new(ErrorKind::Connect, "database pool is closed"));
        }
        let permit = tokio::time::timeout(self.0.wait, self.0.permits.clone().acquire_owned())
            .await
            .map_err(|_| {
                DbError::new(
                    ErrorKind::Pool,
                    "timed out waiting for a database connection",
                )
            })?
            .map_err(|_| DbError::new(ErrorKind::Connect, "database pool is closed"))?;
        let connection = self
            .0
            .available
            .lock()
            .map_err(|_| DbError::new(ErrorKind::Other, "database pool is unavailable"))?
            .pop();
        let connection = match connection {
            Some(connection) => connection,
            None if !self.0.closed.load(Ordering::Acquire) => {
                let connection = rusqlite::Connection::open(&self.0.path)
                    .and_then(|conn| {
                        conn.busy_timeout(Duration::from_secs(5))?;
                        conn.pragma_update(None, "foreign_keys", "ON")?;
                        conn.pragma_update(None, "journal_mode", "WAL")?;
                        Ok(conn)
                    })
                    .map_err(DbError::from_sqlite)?;
                Connection::from(connection)
            }
            None => return Err(DbError::new(ErrorKind::Connect, "database pool is closed")),
        };
        Ok(SqliteLease {
            connection: Some(connection),
            inner: self.0.clone(),
            _permit: permit,
            in_transaction: false,
            foreign_keys_disabled: false,
        })
    }

    pub(crate) async fn acquire_for_migration(
        &self,
    ) -> Result<(SqliteLease, Vec<SqliteLease>), DbError> {
        let mut leases = Vec::with_capacity(self.0.max_size);
        for _ in 0..self.0.max_size {
            leases.push(self.acquire().await?);
        }
        for lease in &mut leases {
            lease.set_foreign_keys(false).await?;
        }
        let mut leases = leases.into_iter();
        let primary = leases
            .next()
            .ok_or_else(|| DbError::new(ErrorKind::Other, "SQLite pool is empty"))?;
        Ok((primary, leases.collect()))
    }

    pub(crate) fn close(&self) {
        self.0.closed.store(true, Ordering::Release);
        if let Ok(mut available) = self.0.available.lock() {
            available.clear();
        }
        self.0.permits.close();
    }

    pub(crate) fn status(&self) -> (usize, usize) {
        (
            self.0.max_size,
            self.0.permits.available_permits().min(self.0.max_size),
        )
    }
}

impl SqliteLease {
    fn conn(&self) -> Result<&Connection, DbError> {
        self.connection
            .as_ref()
            .ok_or_else(|| DbError::new(ErrorKind::Other, "database connection was released"))
    }

    pub(crate) async fn fetch(&self, sql: &str, values: &[Value]) -> Result<Vec<Row>, DbError> {
        let sql = sql.to_owned();
        let values = values.iter().map(encode).collect::<Result<Vec<_>, _>>()?;
        self.conn()?
            .call(move |conn| {
                let mut statement = conn.prepare_cached(&sql)?;
                let mut cursor = statement.query(params_from_iter(values.iter()))?;
                let mut rows = Vec::new();
                while let Some(row) = cursor.next()? {
                    let mut decoded = Vec::with_capacity(row.as_ref().column_count());
                    let columns = row.as_ref().columns();
                    for (index, column) in columns.enumerate() {
                        let name = row.as_ref().column_name(index)?.to_owned();
                        let declared = column.decl_type();
                        let value = decode(row.get_ref(index)?, declared)
                            .map_err(SqliteCallError::Hinoki)?;
                        decoded.push((name, value));
                    }
                    rows.push(decoded);
                }
                Ok(rows)
            })
            .await
            .map_err(call_error)
    }

    pub(crate) async fn execute(&self, sql: &str, values: &[Value]) -> Result<u64, DbError> {
        let sql = sql.to_owned();
        let values = values.iter().map(encode).collect::<Result<Vec<_>, _>>()?;
        self.conn()?
            .call(move |conn| {
                let changed = conn
                    .execute(&sql, params_from_iter(values.iter()))
                    .map_err(SqliteCallError::from)?;
                Ok(changed as u64)
            })
            .await
            .map_err(call_error)
    }

    pub(crate) async fn batch(&self, sql: &str) -> Result<(), DbError> {
        let sql = sql.to_owned();
        self.conn()?
            .call(move |conn| conn.execute_batch(&sql).map_err(SqliteCallError::from))
            .await
            .map_err(call_error)
    }

    pub(crate) async fn begin(&mut self, statement: &str) -> Result<(), DbError> {
        self.in_transaction = true;
        self.batch(statement).await
    }

    pub(crate) async fn finish(&mut self, statement: &str) -> Result<(), DbError> {
        self.batch(statement).await?;
        if self.foreign_keys_disabled {
            self.set_foreign_keys(true).await?;
        }
        self.in_transaction = false;
        Ok(())
    }

    pub(crate) async fn set_foreign_keys(&mut self, enabled: bool) -> Result<(), DbError> {
        self.batch(if enabled {
            "PRAGMA foreign_keys=ON"
        } else {
            "PRAGMA foreign_keys=OFF"
        })
        .await?;
        self.foreign_keys_disabled = !enabled;
        Ok(())
    }

    pub(crate) fn discard(&mut self) {
        self.connection.take();
    }
}

fn call_error(error: tokio_rusqlite::Error<SqliteCallError>) -> DbError {
    match error {
        tokio_rusqlite::Error::Error(SqliteCallError::Driver(error)) => DbError::from_sqlite(error),
        tokio_rusqlite::Error::Error(SqliteCallError::Hinoki(error)) => error,
        tokio_rusqlite::Error::ConnectionClosed | tokio_rusqlite::Error::Close(_) => {
            DbError::new(ErrorKind::Other, "database connection is unavailable")
        }
        _ => DbError::new(ErrorKind::Other, "database request failed"),
    }
}

impl Drop for SqliteLease {
    fn drop(&mut self) {
        let Some(connection) = self.connection.take() else {
            return;
        };
        if !self.in_transaction
            && !self.foreign_keys_disabled
            && !self.inner.closed.load(Ordering::Acquire)
        {
            if let Ok(mut available) = self.inner.available.lock() {
                available.push(connection);
            }
        }
    }
}

fn encode(value: &Value) -> Result<SqliteValue, DbError> {
    Ok(match value {
        Value::Null => SqliteValue::Null,
        Value::Bool(value) => SqliteValue::Integer(i64::from(*value)),
        Value::Int(value) => SqliteValue::Integer(*value),
        Value::Float(value) => SqliteValue::Real(*value),
        Value::Text(value) => SqliteValue::Text(value.clone()),
        Value::Bytes(value) => SqliteValue::Blob(value.clone()),
        Value::Uuid(value) => SqliteValue::Text(value.to_string()),
        Value::Timestamp(value) => {
            SqliteValue::Text(value.to_rfc3339_opts(chrono::SecondsFormat::Micros, true))
        }
        Value::Json(value) => SqliteValue::Text(
            serde_json::to_string(value)
                .map_err(|_| DbError::new(ErrorKind::Type, "could not encode JSON value"))?,
        ),
    })
}

fn decode(value: ValueRef<'_>, declared: Option<&str>) -> Result<Value, DbError> {
    let declared = declared.unwrap_or_default().to_ascii_uppercase();
    match value {
        ValueRef::Null => Ok(Value::Null),
        ValueRef::Integer(value) if declared.contains("BOOL") => Ok(Value::Bool(value != 0)),
        ValueRef::Integer(value) => Ok(Value::Int(value)),
        ValueRef::Real(value) => Ok(Value::Float(value)),
        ValueRef::Blob(value) => Ok(Value::Bytes(value.to_vec())),
        ValueRef::Text(value) => {
            let text = std::str::from_utf8(value)
                .map_err(|_| DbError::new(ErrorKind::Type, "invalid UTF-8 in database text"))?;
            if declared.contains("UUID") {
                return uuid::Uuid::parse_str(text)
                    .map(Value::Uuid)
                    .map_err(|_| DbError::new(ErrorKind::Type, "invalid UUID in database"));
            }
            if declared.contains("TIMESTAMP") || declared.contains("DATETIME") {
                return chrono::DateTime::parse_from_rfc3339(text)
                    .map(|value| Value::Timestamp(value.with_timezone(&chrono::Utc)))
                    .map_err(|_| DbError::new(ErrorKind::Type, "invalid timestamp in database"));
            }
            if declared.contains("JSON") {
                return serde_json::from_str(text)
                    .map(Value::Json)
                    .map_err(|_| DbError::new(ErrorKind::Type, "invalid JSON in database"));
            }
            Ok(Value::Text(text.to_owned()))
        }
    }
}

pub(crate) fn connect_path(url: &str) -> Result<PathBuf, DbError> {
    let path = url
        .strip_prefix("sqlite://")
        .ok_or_else(|| DbError::new(ErrorKind::Connect, "invalid SQLite URL"))?;
    if path.is_empty() || path == ":memory:" || path.contains('?') || path.contains('#') {
        return Err(DbError::new(
            ErrorKind::Connect,
            "SQLite requires a file URL without query parameters",
        ));
    }
    Ok(PathBuf::from(path))
}
