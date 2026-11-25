//! MySQL connection leasing and value conversion.

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::Duration;

use mysql_async::{
    prelude::Queryable, Conn, Opts, OptsBuilder, Params, Pool, PoolConstraints, PoolOpts,
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::error::{lease_released, DbError, ErrorKind};
use crate::value::Value;
use crate::{Row, RowSet};

struct Inner {
    pool: Mutex<Option<Pool>>,
    metrics: Arc<mysql_async::Metrics>,
    permits: Arc<Semaphore>,
    closed: AtomicBool,
    wait: Duration,
}

#[derive(Clone)]
pub(crate) struct MysqlPool(Arc<Inner>);

pub(crate) struct MysqlLease {
    conn: Option<Conn>,
    _permit: OwnedSemaphorePermit,
    interrupted: Arc<AtomicBool>,
}

struct Flight {
    interrupted: Arc<AtomicBool>,
    armed: bool,
}

impl Drop for Flight {
    fn drop(&mut self) {
        if self.armed {
            self.interrupted.store(true, Ordering::Release);
        }
    }
}

impl Flight {
    /// A request that ended with an error from the server is finished, and the
    /// connection stays usable (a duplicate key does not abort the transaction);
    /// any other outcome leaves the connection unusable.
    fn settle<T>(&mut self, result: &Result<T, mysql_async::Error>) {
        self.armed = matches!(result, Err(e) if !matches!(e, mysql_async::Error::Server(_)));
    }
}

/// The URL with `require_ssl=true` unless the caller chose a value. A
/// `#fragment` ends the URL, so the option goes before it.
fn require_ssl(url: &str) -> String {
    let url = url.split_once('#').map_or(url, |(base, _)| base);
    let query = url.split_once('?').map_or("", |(_, query)| query);
    if query
        .split('&')
        .any(|option| option.starts_with("require_ssl="))
    {
        return url.to_owned();
    }
    let separator = if url.contains('?') { '&' } else { '?' };
    format!("{url}{separator}require_ssl=true")
}

impl MysqlPool {
    pub(crate) fn connect(url: &str, max_size: usize, wait: Duration) -> Result<Self, DbError> {
        if max_size == 0 {
            return Err(DbError::new(
                ErrorKind::Connect,
                "pool size must be positive",
            ));
        }
        if !url.starts_with("mysql://") {
            return Err(DbError::new(
                ErrorKind::Connect,
                "invalid MySQL database URL",
            ));
        }
        let url = require_ssl(url);
        let opts = Opts::from_url(&url)
            .map_err(|_| DbError::new(ErrorKind::Connect, "invalid MySQL database URL"))?;
        let constraints = PoolConstraints::new(0, max_size)
            .ok_or_else(|| DbError::new(ErrorKind::Connect, "invalid pool size"))?;
        // mysql_async only retains idle connections when the idle TTL is
        // enabled; keep them bounded and periodically expire them.
        let pool_opts = PoolOpts::default()
            .with_constraints(constraints)
            .with_inactive_connection_ttl(Duration::from_secs(60))
            .with_ttl_check_interval(Duration::from_secs(30));
        let opts: Opts = OptsBuilder::from_opts(opts).pool_opts(pool_opts).into();
        let pool = Pool::new(opts);
        let metrics = pool.metrics();
        Ok(Self(Arc::new(Inner {
            pool: Mutex::new(Some(pool)),
            metrics,
            permits: Arc::new(Semaphore::new(max_size)),
            closed: AtomicBool::new(false),
            wait,
        })))
    }

    pub(crate) async fn acquire(&self) -> Result<MysqlLease, DbError> {
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
        let pool = self
            .0
            .pool
            .lock()
            .map_err(|_| DbError::new(ErrorKind::Connect, "database pool is unavailable"))?
            .as_ref()
            .cloned()
            .ok_or_else(|| DbError::new(ErrorKind::Connect, "database pool is closed"))?;
        let conn = tokio::time::timeout(self.0.wait, pool.get_conn())
            .await
            .map_err(|_| {
                DbError::new(
                    ErrorKind::Pool,
                    "timed out waiting for a database connection",
                )
            })?
            .map_err(|e| error(&e, ErrorKind::Connect))?;
        Ok(MysqlLease {
            conn: Some(conn),
            _permit: permit,
            interrupted: Arc::new(AtomicBool::new(false)),
        })
    }

    pub(crate) fn close(&self) {
        self.0.closed.store(true, Ordering::Release);
        self.0.permits.close();
        let pool = self.0.pool.lock().ok().and_then(|mut pool| pool.take());
        if let Some(pool) = pool {
            if let Ok(runtime) = tokio::runtime::Handle::try_current() {
                runtime.spawn(async move {
                    let _ = pool.disconnect().await;
                });
            }
        }
    }

    pub(crate) fn status(&self) -> (usize, usize) {
        (
            self.0.metrics.connection_count.load(Ordering::Relaxed),
            self.0.metrics.connections_in_pool.load(Ordering::Relaxed),
        )
    }
}

impl MysqlLease {
    pub(crate) async fn begin(&mut self) -> Result<(), DbError> {
        self.batch("START TRANSACTION").await
    }

    pub(crate) async fn batch(&mut self, sql: &str) -> Result<(), DbError> {
        if self.interrupted.load(Ordering::Acquire) {
            return Err(DbError::new(
                ErrorKind::Other,
                "database request was interrupted",
            ));
        }
        let conn = self.conn.as_mut().ok_or_else(lease_released)?;
        let mut flight = Flight {
            interrupted: self.interrupted.clone(),
            armed: true,
        };
        let result = conn.query_drop(sql).await;
        flight.settle(&result);
        result.map_err(|e| error(&e, ErrorKind::Other))?;
        Ok(())
    }

    pub(crate) async fn fetch(&mut self, sql: &str, values: &[Value]) -> Result<Vec<Row>, DbError> {
        if self.interrupted.load(Ordering::Acquire) {
            return Err(DbError::new(
                ErrorKind::Other,
                "database request was interrupted",
            ));
        }
        let params = params(values)?;
        let conn = self.conn.as_mut().ok_or_else(lease_released)?;
        let mut flight = Flight {
            interrupted: self.interrupted.clone(),
            armed: true,
        };
        let result: Result<Vec<mysql_async::Row>, _> = conn.exec(sql, params).await;
        flight.settle(&result);
        let rows = result.map_err(|e| error(&e, ErrorKind::Other))?;
        rows.iter().map(decode).collect()
    }

    pub(crate) async fn fetch_set(
        &mut self,
        sql: &str,
        values: &[Value],
    ) -> Result<RowSet, DbError> {
        if self.interrupted.load(Ordering::Acquire) {
            return Err(DbError::new(
                ErrorKind::Other,
                "database request was interrupted",
            ));
        }
        let params = params(values)?;
        let conn = self.conn.as_mut().ok_or_else(lease_released)?;
        let mut flight = Flight {
            interrupted: self.interrupted.clone(),
            armed: true,
        };
        let result: Result<Vec<mysql_async::Row>, _> = conn.exec(sql, params).await;
        flight.settle(&result);
        let raw_rows = result.map_err(|e| error(&e, ErrorKind::Other))?;
        let columns: Arc<[String]> = match raw_rows.first() {
            Some(first) => first
                .columns_ref()
                .iter()
                .map(|c| c.name_str().into_owned())
                .collect(),
            None => Arc::from([]),
        };
        let rows = raw_rows
            .iter()
            .map(decode_values)
            .collect::<Result<Vec<_>, _>>()?;
        Ok(RowSet { columns, rows })
    }

    pub(crate) async fn execute(&mut self, sql: &str, values: &[Value]) -> Result<u64, DbError> {
        if self.interrupted.load(Ordering::Acquire) {
            return Err(DbError::new(
                ErrorKind::Other,
                "database request was interrupted",
            ));
        }
        let params = params(values)?;
        let conn = self.conn.as_mut().ok_or_else(lease_released)?;
        let mut flight = Flight {
            interrupted: self.interrupted.clone(),
            armed: true,
        };
        // MySQL does not accept SAVEPOINT, ROLLBACK TO and RELEASE in the
        // prepared-statement protocol; a statement without values has no
        // reason to be prepared.
        let result = if values.is_empty() {
            conn.query_drop(sql).await
        } else {
            conn.exec_drop(sql, params).await
        };
        flight.settle(&result);
        result.map_err(|e| error(&e, ErrorKind::Other))?;
        Ok(conn.affected_rows())
    }

    /// Execute a statement and return the `last_insert_id` of its OK packet;
    /// `None` when it is 0 (`LAST_INSERT_ID(expr)` was not evaluated).
    pub(crate) async fn exec_last_insert_id(
        &mut self,
        sql: &str,
        values: &[Value],
    ) -> Result<Option<i64>, DbError> {
        if self.interrupted.load(Ordering::Acquire) {
            return Err(DbError::new(
                ErrorKind::Other,
                "database request was interrupted",
            ));
        }
        let mut flight = Flight {
            interrupted: self.interrupted.clone(),
            armed: true,
        };
        let params = params(values)?;
        let conn = self.conn.as_mut().expect("lease already released");
        let result = conn.exec_drop(sql, params).await;
        flight.settle(&result);
        result.map_err(|e| error(&e, ErrorKind::Other))?;
        match conn.last_insert_id() {
            None | Some(0) => Ok(None),
            Some(id) => i64::try_from(id).map(Some).map_err(|_| {
                DbError::new(ErrorKind::Type, "integer exceeds Hinoki's 64-bit range")
            }),
        }
    }

    /// Whether a request was interrupted mid-flight; dropping the lease then
    /// disconnects, which rolls back any open transaction on the server.
    pub(crate) fn is_interrupted(&self) -> bool {
        self.interrupted.load(Ordering::Acquire)
    }

    pub(crate) fn release(mut self) {
        self.conn.take();
    }

    pub(crate) fn abandon(&mut self) {
        self.interrupted.store(true, Ordering::Release);
        self.disconnect();
    }

    fn disconnect(&mut self) {
        if let Some(conn) = self.conn.take() {
            if let Ok(runtime) = tokio::runtime::Handle::try_current() {
                runtime.spawn(async move {
                    let _ = conn.disconnect().await;
                });
            }
        }
    }
}

impl Drop for MysqlLease {
    fn drop(&mut self) {
        if self.interrupted.load(Ordering::Acquire) {
            self.disconnect();
        }
    }
}

fn params(values: &[Value]) -> Result<Params, DbError> {
    values
        .iter()
        .map(|value| match value {
            Value::Null => Ok(mysql_async::Value::NULL),
            Value::Bool(value) => Ok(mysql_async::Value::Int(i64::from(*value))),
            Value::Int(value) => Ok(mysql_async::Value::Int(*value)),
            Value::Float(value) => Ok(mysql_async::Value::Double(*value)),
            Value::Text(value) => Ok(mysql_async::Value::Bytes(value.as_bytes().to_vec())),
            Value::Bytes(value) => Ok(mysql_async::Value::Bytes(value.clone())),
            Value::Uuid(value) => Ok(mysql_async::Value::Bytes(value.as_bytes().to_vec())),
            Value::Timestamp(value) => {
                let value = value.to_offset(time::UtcOffset::UTC);
                let year = u16::try_from(value.year()).map_err(|_| {
                    DbError::new(ErrorKind::Type, "timestamp year is outside 0-65535")
                })?;
                Ok(mysql_async::Value::Date(
                    year,
                    u8::from(value.month()),
                    value.day(),
                    value.hour(),
                    value.minute(),
                    value.second(),
                    value.microsecond(),
                ))
            }
            Value::Json(value) => serde_json::to_vec(value)
                .map(mysql_async::Value::Bytes)
                .map_err(|_| DbError::new(ErrorKind::Type, "could not encode JSON parameter")),
        })
        .collect::<Result<Vec<_>, _>>()
        .map(Params::Positional)
}

fn decode_values(row: &mysql_async::Row) -> Result<Vec<Value>, DbError> {
    row.columns_ref()
        .iter()
        .enumerate()
        .map(|(index, column)| {
            let value = row
                .as_ref(index)
                .ok_or_else(|| DbError::new(ErrorKind::Type, "could not decode a MySQL column"))?;
            decode_cell(
                value,
                column.column_type(),
                column
                    .flags()
                    .contains(mysql_async::consts::ColumnFlags::BINARY_FLAG),
                column.column_length(),
            )
        })
        .collect()
}

fn decode(row: &mysql_async::Row) -> Result<Row, DbError> {
    row.columns_ref()
        .iter()
        .enumerate()
        .map(|(index, column)| {
            let name = column.name_str().into_owned();
            let value = row
                .as_ref(index)
                .ok_or_else(|| DbError::new(ErrorKind::Type, "could not decode a MySQL column"))?;
            Ok((
                name,
                decode_cell(
                    value,
                    column.column_type(),
                    column
                        .flags()
                        .contains(mysql_async::consts::ColumnFlags::BINARY_FLAG),
                    column.column_length(),
                )?,
            ))
        })
        .collect()
}

fn decode_cell(
    value: &mysql_async::Value,
    ty: mysql_async::consts::ColumnType,
    binary: bool,
    length: u32,
) -> Result<Value, DbError> {
    use mysql_async::{consts::ColumnType as T, Value as M};
    Ok(match value {
        M::NULL => Value::Null,
        M::Int(value) => match ty {
            T::MYSQL_TYPE_TINY => Value::Bool(*value != 0),
            _ => Value::Int(*value),
        },
        M::UInt(value) if ty == T::MYSQL_TYPE_TINY => Value::Bool(*value != 0),
        M::UInt(value) => i64::try_from(*value)
            .map(Value::Int)
            .map_err(|_| DbError::new(ErrorKind::Type, "integer exceeds Hinoki's 64-bit range"))?,
        M::Float(value) => Value::Float(f64::from(*value)),
        M::Double(value) => Value::Float(*value),
        M::Bytes(value) if ty == T::MYSQL_TYPE_JSON => {
            let text = String::from_utf8(value.clone())
                .map_err(|_| DbError::new(ErrorKind::Type, "could not decode a JSON column"))?;
            Value::Json(
                serde_json::from_str(&text)
                    .map_err(|_| DbError::new(ErrorKind::Type, "could not decode a JSON column"))?,
            )
        }
        M::Bytes(value) if binary && length == 16 => Value::Uuid(
            uuid::Uuid::from_slice(value)
                .map_err(|_| DbError::new(ErrorKind::Type, "could not decode a UUID column"))?,
        ),
        M::Bytes(value) if binary => Value::Bytes(value.clone()),
        M::Bytes(value) => {
            let text = String::from_utf8(value.clone())
                .map_err(|_| DbError::new(ErrorKind::Type, "could not decode a text column"))?;
            Value::Text(text)
        }
        M::Date(year, month, day, hour, minute, second, micros) => {
            let invalid = || DbError::new(ErrorKind::Type, "could not decode a timestamp column");
            let month = time::Month::try_from(*month).map_err(|_| invalid())?;
            let date = time::Date::from_calendar_date(i32::from(*year), month, *day)
                .map_err(|_| invalid())?;
            let clock = time::Time::from_hms_micro(*hour, *minute, *second, *micros)
                .map_err(|_| invalid())?;
            Value::Timestamp(time::PrimitiveDateTime::new(date, clock).assume_utc())
        }
        M::Time(..) => {
            return Err(DbError::new(
                ErrorKind::Type,
                "time columns are not supported",
            ))
        }
    })
}

fn error(error: &mysql_async::Error, fallback: ErrorKind) -> DbError {
    if let mysql_async::Error::Server(server) = error {
        let (kind, message) = match server.code {
            1062 => (ErrorKind::Unique, "unique constraint violated"),
            1451 | 1452 => (ErrorKind::ForeignKey, "foreign key constraint violated"),
            1048 => (ErrorKind::NotNull, "not-null constraint violated"),
            3819 => (ErrorKind::Check, "check constraint violated"),
            _ => (ErrorKind::Other, "database error"),
        };
        let mut result = DbError::new(kind, message);
        result.sqlstate = Some(server.state.clone());
        return result;
    }
    DbError::new(fallback, "database request failed")
}

#[cfg(test)]
mod require_ssl_tests {
    use std::sync::{atomic::AtomicBool, Arc};

    use tokio::sync::Semaphore;

    use super::{require_ssl, MysqlLease};
    use crate::error::ErrorKind;

    #[test]
    fn tls_is_required_before_any_fragment() {
        assert_eq!(
            require_ssl("mysql://u@h/d"),
            "mysql://u@h/d?require_ssl=true"
        );
        assert_eq!(
            require_ssl("mysql://u@h/d?a=1"),
            "mysql://u@h/d?a=1&require_ssl=true"
        );
        assert_eq!(
            require_ssl("mysql://u@h/d#x"),
            "mysql://u@h/d?require_ssl=true"
        );
        assert_eq!(
            require_ssl("mysql://u@h/d?require_ssl=false"),
            "mysql://u@h/d?require_ssl=false"
        );
    }

    #[tokio::test]
    async fn released_lease_returns_error_instead_of_panicking() {
        let permit = Arc::new(Semaphore::new(1))
            .try_acquire_owned()
            .expect("test semaphore permit");
        let mut lease = MysqlLease {
            conn: None,
            _permit: permit,
            interrupted: Arc::new(AtomicBool::new(false)),
        };

        let batch_err = lease.batch("SELECT 1").await.unwrap_err();
        assert_eq!(batch_err.kind, ErrorKind::Other);
        assert_eq!(
            batch_err.message,
            "database connection lease already released"
        );

        let fetch_err = lease.fetch("SELECT 1", &[]).await.unwrap_err();
        assert_eq!(fetch_err.kind, ErrorKind::Other);
        assert_eq!(
            fetch_err.message,
            "database connection lease already released"
        );

        let exec_err = lease.execute("SELECT 1", &[]).await.unwrap_err();
        assert_eq!(exec_err.kind, ErrorKind::Other);
        assert_eq!(
            exec_err.message,
            "database connection lease already released"
        );
    }
}
