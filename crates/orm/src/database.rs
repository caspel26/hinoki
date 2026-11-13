//! Connection pool, statements and transactions.

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

    async fn acquire(&self) -> Result<Object, DbError> {
        self.pool.get().await.map_err(|e| match e {
            deadpool_postgres::PoolError::Timeout(_) => DbError::new(
                ErrorKind::Pool,
                "timed out waiting for a database connection",
            ),
            _ => DbError::new(ErrorKind::Connect, "could not obtain a database connection"),
        })
    }

    /// Open one connection and run `SELECT 1`.
    pub async fn check(&self) -> Result<(), DbError> {
        run_fetch(&self.acquire().await?, "SELECT 1", &[])
            .await
            .map(drop)
    }

    pub async fn fetch(&self, sql: &str, values: &[Value]) -> Result<Vec<Row>, DbError> {
        run_fetch(&self.acquire().await?, sql, values).await
    }

    pub async fn execute(&self, sql: &str, values: &[Value]) -> Result<u64, DbError> {
        run_execute(&self.acquire().await?, sql, values).await
    }

    /// Start a transaction that owns one pooled connection until it ends.
    pub async fn begin(&self) -> Result<Transaction, DbError> {
        let conn = self.acquire().await?;
        conn.batch_execute("BEGIN")
            .await
            .map_err(|e| DbError::from_driver(&e))?;
        Ok(Transaction { conn: Some(conn) })
    }

    pub fn close(&self) {
        self.pool.close();
    }

    pub fn status(&self) -> (usize, usize) {
        let status = self.pool.status();
        (status.size, status.available)
    }
}

/// A transaction. Dropping it without `commit`/`rollback` discards the
/// connection instead of returning one in an unknown state to the pool.
pub struct Transaction {
    conn: Option<Object>,
}

impl Transaction {
    fn conn(&self) -> Result<&Object, DbError> {
        self.conn
            .as_ref()
            .ok_or_else(|| DbError::new(ErrorKind::Other, "transaction already finished"))
    }

    pub async fn fetch(&self, sql: &str, values: &[Value]) -> Result<Vec<Row>, DbError> {
        run_fetch(self.conn()?, sql, values).await
    }

    pub async fn execute(&self, sql: &str, values: &[Value]) -> Result<u64, DbError> {
        run_execute(self.conn()?, sql, values).await
    }

    pub async fn commit(mut self) -> Result<(), DbError> {
        self.finish("COMMIT").await
    }

    pub async fn rollback(mut self) -> Result<(), DbError> {
        self.finish("ROLLBACK").await
    }

    async fn finish(&mut self, statement: &str) -> Result<(), DbError> {
        let conn = self.conn()?;
        let outcome = conn
            .batch_execute(statement)
            .await
            .map_err(|e| DbError::from_driver(&e));
        if outcome.is_err() {
            // The state is unknown: leave `conn` set so Drop discards it.
            return outcome;
        }
        self.conn = None; // back to the pool
        Ok(())
    }
}

impl Drop for Transaction {
    fn drop(&mut self) {
        if let Some(conn) = self.conn.take() {
            drop(Object::take(conn));
        }
    }
}
