//! Connection pool, statements and transactions.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use deadpool_postgres::{Config, Object, PoolConfig, Runtime, Timeouts};
use tokio_postgres::types::{ToSql, Type};
use tokio_postgres::NoTls;

use crate::error::{DbError, ErrorKind};
use crate::value::Value;

/// One result row: column names with decoded values, in column order.
pub type Row = Vec<(String, Value)>;

#[derive(Clone)]
pub struct Database {
    pool: deadpool_postgres::Pool,
}

fn params(values: &[Value]) -> Vec<&(dyn ToSql + Sync)> {
    values.iter().map(|v| v as &(dyn ToSql + Sync)).collect()
}

fn decode(row: &tokio_postgres::Row) -> Result<Row, DbError> {
    row.columns()
        .iter()
        .enumerate()
        .map(|(i, col)| Ok((col.name().to_owned(), decode_cell(row, i, col.type_())?)))
        .collect()
}

fn decode_cell(row: &tokio_postgres::Row, i: usize, ty: &Type) -> Result<Value, DbError> {
    fn get<'a, T: tokio_postgres::types::FromSql<'a>>(
        row: &'a tokio_postgres::Row,
        i: usize,
    ) -> Result<Option<T>, DbError> {
        row.try_get(i)
            .map_err(|_| DbError::new(ErrorKind::Type, "could not decode a column"))
    }
    macro_rules! cell {
        ($t:ty, $variant:expr) => {
            get::<$t>(row, i)?.map_or(Value::Null, $variant)
        };
    }
    Ok(match *ty {
        Type::VOID => Value::Null,
        Type::BOOL => cell!(bool, Value::Bool),
        Type::INT2 => cell!(i16, |v| Value::Int(i64::from(v))),
        Type::INT4 => cell!(i32, |v| Value::Int(i64::from(v))),
        Type::INT8 => cell!(i64, Value::Int),
        Type::FLOAT4 => cell!(f32, |v| Value::Float(f64::from(v))),
        Type::FLOAT8 => cell!(f64, Value::Float),
        Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME => cell!(String, Value::Text),
        Type::BYTEA => cell!(Vec<u8>, Value::Bytes),
        Type::UUID => cell!(uuid::Uuid, Value::Uuid),
        Type::TIMESTAMPTZ => cell!(chrono::DateTime<chrono::Utc>, Value::Timestamp),
        Type::JSON | Type::JSONB => cell!(serde_json::Value, Value::Json),
        _ => {
            return Err(DbError::new(
                ErrorKind::Type,
                format!("unsupported column type {}", ty.name()),
            ))
        }
    })
}

/// A pooled connection lent for one operation.
///
/// If the operation's future is dropped before it finishes (cancellation,
/// timeout) the server may still be running the statement, so the connection
/// is *abandoned*: a cancel request is sent and the connection is discarded
/// instead of returning to the pool with work queued behind it.
struct Lease {
    conn: Option<Object>,
}

impl Lease {
    fn new(conn: Object) -> Self {
        Self { conn: Some(conn) }
    }

    fn get(&self) -> &Object {
        self.conn.as_ref().expect("lease already released")
    }

    /// Hand the connection to a longer-lived owner (a transaction).
    fn into_inner(mut self) -> Object {
        self.conn.take().expect("lease already released")
    }

    /// The operation completed (successfully or with a statement error): the
    /// connection state is known, so it goes back to the pool.
    fn release(mut self) {
        drop(self.conn.take());
    }
}

impl Drop for Lease {
    fn drop(&mut self) {
        if let Some(conn) = self.conn.take() {
            abandon(conn);
        }
    }
}

fn spawn_cancel(token: tokio_postgres::CancelToken) {
    if let Ok(handle) = tokio::runtime::Handle::try_current() {
        handle.spawn(async move {
            let _ = token.cancel_query(NoTls).await;
        });
    }
}

fn abandon(conn: Object) {
    spawn_cancel(conn.cancel_token());
    drop(Object::take(conn));
}

fn is_stale_plan(err: &DbError) -> bool {
    matches!(err.sqlstate.as_deref(), Some("0A000" | "26000"))
}

async fn run_fetch(conn: &Object, sql: &str, values: &[Value]) -> Result<Vec<Row>, DbError> {
    let stmt = conn
        .prepare_cached(sql)
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    let rows = conn
        .query(&stmt, &params(values))
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    rows.iter().map(decode).collect()
}

async fn run_execute(conn: &Object, sql: &str, values: &[Value]) -> Result<u64, DbError> {
    let stmt = conn
        .prepare_cached(sql)
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    conn.execute(&stmt, &params(values))
        .await
        .map_err(|e| DbError::from_driver(&e))
}

/// Outside a transaction a cached plan invalidated by DDL can be retried once
/// after dropping it from the connection's statement cache.
async fn fetch_with_recovery(
    conn: &Object,
    sql: &str,
    values: &[Value],
) -> Result<Vec<Row>, DbError> {
    match run_fetch(conn, sql, values).await {
        Err(err) if is_stale_plan(&err) => {
            conn.statement_cache.remove(sql, &[]);
            run_fetch(conn, sql, values).await
        }
        other => other,
    }
}

async fn execute_with_recovery(conn: &Object, sql: &str, values: &[Value]) -> Result<u64, DbError> {
    match run_execute(conn, sql, values).await {
        Err(err) if is_stale_plan(&err) => {
            conn.statement_cache.remove(sql, &[]);
            run_execute(conn, sql, values).await
        }
        other => other,
    }
}

impl Database {
    /// Create a pool. No connection is opened until first use, so call
    /// [`Database::check`] to fail fast at startup.
    pub fn connect(url: &str, max_size: usize, wait: Duration) -> Result<Self, DbError> {
        let mut config = Config::new();
        config.url = Some(url.to_owned());
        config.pool = Some(PoolConfig {
            max_size,
            timeouts: Timeouts {
                wait: Some(wait),
                ..Timeouts::default()
            },
            ..PoolConfig::default()
        });
        let pool = config
            .create_pool(Some(Runtime::Tokio1), NoTls)
            .map_err(|_| DbError::new(ErrorKind::Connect, "invalid database configuration"))?;
        Ok(Self { pool })
    }

    async fn acquire(&self) -> Result<Lease, DbError> {
        self.pool.get().await.map(Lease::new).map_err(|e| match e {
            deadpool_postgres::PoolError::Timeout(_) => DbError::new(
                ErrorKind::Pool,
                "timed out waiting for a database connection",
            ),
            _ => DbError::new(ErrorKind::Connect, "could not obtain a database connection"),
        })
    }

    /// Open one connection and run `SELECT 1`.
    pub async fn check(&self) -> Result<(), DbError> {
        self.fetch("SELECT 1", &[]).await.map(drop)
    }

    pub async fn fetch(&self, sql: &str, values: &[Value]) -> Result<Vec<Row>, DbError> {
        let lease = self.acquire().await?;
        let outcome = fetch_with_recovery(lease.get(), sql, values).await;
        lease.release();
        outcome
    }

    pub async fn execute(&self, sql: &str, values: &[Value]) -> Result<u64, DbError> {
        let lease = self.acquire().await?;
        let outcome = execute_with_recovery(lease.get(), sql, values).await;
        lease.release();
        outcome
    }

    /// Start a transaction that owns one pooled connection until it ends. If
    /// this future is dropped while `BEGIN` is in flight the connection is
    /// abandoned, never returned to the pool inside a transaction.
    pub async fn begin(&self) -> Result<Transaction, DbError> {
        let lease = self.acquire().await?;
        lease
            .get()
            .batch_execute("BEGIN")
            .await
            .map_err(|e| DbError::from_driver(&e))?;
        Ok(Transaction::new(lease.into_inner()))
    }

    pub fn close(&self) {
        self.pool.close();
    }

    pub fn status(&self) -> (usize, usize) {
        let status = self.pool.status();
        (status.size, status.available)
    }
}

/// Marks a statement as in flight; if it is dropped before completing, the
/// transaction is poisoned (its connection is in an unknown state) and the
/// server is asked to cancel the statement.
struct Flight<'a> {
    poisoned: &'a AtomicBool,
    token: tokio_postgres::CancelToken,
    armed: bool,
}

impl Drop for Flight<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.poisoned.store(true, Ordering::SeqCst);
            spawn_cancel(self.token.clone());
        }
    }
}

/// A transaction. Dropping it without `commit`/`rollback`, or after a
/// statement was abandoned mid-flight, discards the connection instead of
/// returning one in an unknown state to the pool.
pub struct Transaction {
    conn: Option<Object>,
    /// A statement failed: PostgreSQL will refuse to commit (until a
    /// `ROLLBACK TO SAVEPOINT` recovers it).
    failed: AtomicBool,
    /// A statement was abandoned mid-flight.
    poisoned: AtomicBool,
}

impl Transaction {
    fn new(conn: Object) -> Self {
        Self {
            conn: Some(conn),
            failed: AtomicBool::new(false),
            poisoned: AtomicBool::new(false),
        }
    }

    fn conn(&self) -> Result<&Object, DbError> {
        if self.poisoned.load(Ordering::SeqCst) {
            return Err(DbError::new(
                ErrorKind::Other,
                "transaction was interrupted",
            ));
        }
        self.conn
            .as_ref()
            .ok_or_else(|| DbError::new(ErrorKind::Other, "transaction already finished"))
    }

    fn settle<T>(&self, sql: &str, outcome: &Result<T, DbError>) {
        match outcome {
            // Only a server-reported error aborts the transaction; a
            // client-side encoding or decoding failure does not.
            Err(err) if err.sqlstate.is_some() => self.failed.store(true, Ordering::SeqCst),
            Err(_) => {}
            Ok(_)
                if sql
                    .trim_start()
                    .to_ascii_uppercase()
                    .starts_with("ROLLBACK TO") =>
            {
                self.failed.store(false, Ordering::SeqCst);
            }
            Ok(_) => {}
        }
    }

    pub async fn fetch(&self, sql: &str, values: &[Value]) -> Result<Vec<Row>, DbError> {
        let conn = self.conn()?;
        let mut flight = Flight {
            poisoned: &self.poisoned,
            token: conn.cancel_token(),
            armed: true,
        };
        let outcome = run_fetch(conn, sql, values).await;
        flight.armed = false;
        self.settle(sql, &outcome);
        outcome
    }

    pub async fn execute(&self, sql: &str, values: &[Value]) -> Result<u64, DbError> {
        let conn = self.conn()?;
        let mut flight = Flight {
            poisoned: &self.poisoned,
            token: conn.cancel_token(),
            armed: true,
        };
        let outcome = run_execute(conn, sql, values).await;
        flight.armed = false;
        self.settle(sql, &outcome);
        outcome
    }

    /// Commit. A transaction aborted by an earlier statement error is rolled
    /// back and reported as an error: PostgreSQL would silently answer
    /// `COMMIT` with a rollback, which must never look like success.
    pub async fn commit(mut self) -> Result<(), DbError> {
        if self.poisoned.load(Ordering::SeqCst) {
            return Err(DbError::new(
                ErrorKind::Other,
                "transaction was interrupted",
            ));
        }
        if self.failed.load(Ordering::SeqCst) {
            self.finish("ROLLBACK").await?;
            let mut err = DbError::new(
                ErrorKind::Other,
                "transaction was aborted by an earlier error and has been rolled back",
            );
            err.sqlstate = Some("25P02".to_owned());
            return Err(err);
        }
        self.finish("COMMIT").await
    }

    pub async fn rollback(mut self) -> Result<(), DbError> {
        if self.poisoned.load(Ordering::SeqCst) {
            return Ok(()); // Drop discards the connection, which ends the transaction
        }
        self.finish("ROLLBACK").await
    }

    async fn finish(&mut self, statement: &str) -> Result<(), DbError> {
        let conn = self.conn()?;
        // On failure the state is unknown: `conn` stays set so Drop discards it.
        conn.batch_execute(statement)
            .await
            .map_err(|e| DbError::from_driver(&e))?;
        self.conn = None; // back to the pool
        Ok(())
    }
}

impl Drop for Transaction {
    fn drop(&mut self) {
        if let Some(conn) = self.conn.take() {
            abandon(conn);
        }
    }
}
