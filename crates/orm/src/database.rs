//! Connection pool, statements and transactions.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
#[cfg(feature = "sql-trace")]
use std::sync::Mutex;
use std::time::Duration;

use deadpool_postgres::{Config, Object, PoolConfig, Runtime, SslMode, Timeouts};
use tokio_postgres::types::{FromSql, ToSql, Type};
use tokio_postgres::NoTls;
use tokio_postgres_rustls::MakeRustlsConnect;

use crate::error::{lease_released, DbError, ErrorKind};
use crate::mysql::{MysqlLease, MysqlPool};
use crate::pgrows::{extract_pg_count, PgRows};
use crate::sqlite::{connect_path, SqliteLease, SqlitePool};
use crate::tls::{connector, extract_options};
use crate::value::Value;

/// One result row: column names with decoded values, in column order.
pub type Row = Vec<(String, Value)>;

/// Result rows sharing one column-name slice so decoding allocates column names
/// once per query instead of once per row.
#[derive(Debug, Clone, Default)]
pub struct RowSet {
    pub columns: Arc<[String]>,
    pub rows: Vec<Vec<Value>>,
}

impl RowSet {
    pub fn is_empty(&self) -> bool {
        self.rows.is_empty()
    }

    pub fn len(&self) -> usize {
        self.rows.len()
    }

    /// Append another chunk's rows into this set, adopting its column names if
    /// this set is currently empty.
    pub fn extend_from(&mut self, other: Self) {
        if self.columns.is_empty() && !other.columns.is_empty() {
            self.columns = other.columns;
        }
        self.rows.extend(other.rows);
    }
}

/// The rows and count queries for one paginated page.
pub struct PageQueries<'a> {
    pub rows_sql: &'a str,
    pub rows_params: &'a [Value],
    pub count_sql: &'a str,
    pub count_params: &'a [Value],
}

fn extract_count(rows: &[Row]) -> Result<i64, DbError> {
    let value = rows
        .first()
        .and_then(|row| row.first())
        .map(|(_, value)| value);
    match value {
        Some(Value::Int(count)) => Ok(*count),
        _ => Err(DbError::new(
            ErrorKind::Type,
            "expected an integer count from the count query",
        )),
    }
}

fn extract_count_set(set: &RowSet) -> Result<i64, DbError> {
    let value = set.rows.first().and_then(|row| row.first());
    match value {
        Some(Value::Int(count)) => Ok(*count),
        _ => Err(DbError::new(
            ErrorKind::Type,
            "expected an integer count from the count query",
        )),
    }
}

#[cfg(feature = "sql-trace")]
#[derive(Clone, Debug, PartialEq)]
pub struct SqlTraceEntry {
    pub kind: String,
    pub sql: String,
    pub values: Vec<Value>,
}

#[cfg(feature = "sql-trace")]
#[derive(Clone, Default)]
struct SqlTrace(Arc<Mutex<Vec<SqlTraceEntry>>>);

#[cfg(feature = "sql-trace")]
impl SqlTrace {
    fn record(&self, kind: &str, sql: &str, values: &[Value]) {
        self.0
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(SqlTraceEntry {
                kind: kind.to_owned(),
                sql: sql.to_owned(),
                values: values.to_vec(),
            });
    }

    fn clear(&self) {
        self.0
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clear();
    }

    fn take(&self) -> Vec<SqlTraceEntry> {
        std::mem::take(&mut *self.0.lock().unwrap_or_else(|error| error.into_inner()))
    }
}

#[derive(Clone)]
pub struct Database {
    backend: DatabaseBackend,
    max_size: usize,
    #[cfg(feature = "sql-trace")]
    trace: SqlTrace,
}

#[derive(Clone)]
enum DatabaseBackend {
    Postgres {
        pool: deadpool_postgres::Pool,
        tls: Option<MakeRustlsConnect>,
    },
    Sqlite(SqlitePool),
    Mysql {
        pool: MysqlPool,
        dialect: &'static str,
    },
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

pub(crate) fn decode_values(
    row: &tokio_postgres::Row,
    types: &[&Type],
) -> Result<Vec<Value>, DbError> {
    types
        .iter()
        .enumerate()
        .map(|(i, ty)| decode_cell(row, i, ty))
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
        Type::TIMESTAMPTZ => cell!(PgTimestamp, |v: PgTimestamp| Value::timestamp(v.0)),
        Type::NUMERIC => cell!(PgNumeric, |v: PgNumeric| Value::Text(v.0)),
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
    tls: Option<MakeRustlsConnect>,
}

impl Lease {
    fn new(conn: Object, tls: Option<MakeRustlsConnect>) -> Self {
        Self {
            conn: Some(conn),
            tls,
        }
    }

    fn get(&self) -> Result<&Object, DbError> {
        self.conn.as_ref().ok_or_else(lease_released)
    }

    /// Hand the connection to a longer-lived owner (a transaction).
    fn into_inner(mut self) -> Result<(Object, Option<MakeRustlsConnect>), DbError> {
        let conn = self.conn.take().ok_or_else(lease_released)?;
        Ok((conn, self.tls.take()))
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
            abandon(conn, self.tls.clone());
        }
    }
}

const CANCEL_TIMEOUT: Duration = Duration::from_secs(2);

fn spawn_cancel(token: tokio_postgres::CancelToken, tls: Option<MakeRustlsConnect>) {
    if let Ok(handle) = tokio::runtime::Handle::try_current() {
        handle.spawn(async move {
            let cancel = async move {
                if let Some(tls) = tls {
                    let _ = token.cancel_query(tls).await;
                } else {
                    let _ = token.cancel_query(NoTls).await;
                }
            };
            let _ = tokio::time::timeout(CANCEL_TIMEOUT, cancel).await;
        });
    }
}

fn abandon(conn: Object, tls: Option<MakeRustlsConnect>) {
    spawn_cancel(conn.cancel_token(), tls);
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

async fn run_fetch_pg(conn: &Object, sql: &str, values: &[Value]) -> Result<PgRows, DbError> {
    let stmt = conn
        .prepare_cached(sql)
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    let raw_rows = conn
        .query(&stmt, &params(values))
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    Ok(PgRows::new(raw_rows))
}

async fn fetch_pg_with_recovery(
    conn: &Object,
    sql: &str,
    values: &[Value],
) -> Result<PgRows, DbError> {
    match run_fetch_pg(conn, sql, values).await {
        Err(err) if is_stale_plan(&err) => {
            conn.statement_cache.remove(sql, &[]);
            run_fetch_pg(conn, sql, values).await
        }
        other => other,
    }
}

async fn run_fetch_pg_pair(
    conn: &Object,
    a: (&str, &[Value]),
    b: (&str, &[Value]),
) -> Result<(PgRows, PgRows), DbError> {
    let stmt_a = conn
        .prepare_cached(a.0)
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    let stmt_b = conn
        .prepare_cached(b.0)
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    let pa = params(a.1);
    let pb = params(b.1);
    // Use `tokio::join!` (not `try_join!`) so both pipelined responses drain to
    // ReadyForQuery on the wire before inspecting errors. If the outer future is
    // dropped/cancelled mid-flight, `Lease::drop` cancels and discards the conn.
    let (res_a, res_b) = tokio::join!(conn.query(&stmt_a, &pa), conn.query(&stmt_b, &pb));
    let err_a = res_a.as_ref().err().map(DbError::from_driver);
    let err_b = res_b.as_ref().err().map(DbError::from_driver);
    let stale_a = err_a.as_ref().is_some_and(is_stale_plan);
    let stale_b = err_b.as_ref().is_some_and(is_stale_plan);
    if stale_a || stale_b {
        if stale_a {
            conn.statement_cache.remove(a.0, &[]);
        }
        if stale_b {
            conn.statement_cache.remove(b.0, &[]);
        }
        let rows_a = fetch_pg_with_recovery(conn, a.0, a.1).await?;
        let rows_b = fetch_pg_with_recovery(conn, b.0, b.1).await?;
        return Ok((rows_a, rows_b));
    }
    if let Some(err) = err_a.or(err_b) {
        return Err(err);
    }
    Ok((
        PgRows::new(res_a.unwrap_or_default()),
        PgRows::new(res_b.unwrap_or_default()),
    ))
}

/// One pooled PostgreSQL connection held for a multi-query read operation
/// (such as a paginated page plus its prefetches).
///
/// Only calling [`PgSession::release`] returns the connection to the pool; any
/// early return on error or future cancellation drops the inner [`Lease`],
/// which sends a cancel request and discards the connection so the pool never
/// receives a dirty or in-flight connection.
pub struct PgSession {
    lease: Lease,
    #[cfg(feature = "sql-trace")]
    trace: SqlTrace,
}

impl PgSession {
    pub async fn fetch_pg(&self, sql: &str, values: &[Value]) -> Result<PgRows, DbError> {
        #[cfg(feature = "sql-trace")]
        self.trace.record("fetch", sql, values);
        fetch_pg_with_recovery(self.lease.get()?, sql, values).await
    }

    pub async fn fetch_pg_pair(
        &self,
        a: (&str, &[Value]),
        b: (&str, &[Value]),
    ) -> Result<(PgRows, PgRows), DbError> {
        #[cfg(feature = "sql-trace")]
        {
            self.trace.record("fetch", a.0, a.1);
            self.trace.record("fetch", b.0, b.1);
        }
        run_fetch_pg_pair(self.lease.get()?, a, b).await
    }

    pub async fn fetch_pg_page(
        &self,
        queries: &PageQueries<'_>,
        concurrent: bool,
    ) -> Result<(PgRows, i64), DbError> {
        if concurrent {
            let (count_rows, rows) = self
                .fetch_pg_pair(
                    (queries.count_sql, queries.count_params),
                    (queries.rows_sql, queries.rows_params),
                )
                .await?;
            return Ok((rows, extract_pg_count(&count_rows)?));
        }
        let count_rows = self
            .fetch_pg(queries.count_sql, queries.count_params)
            .await?;
        let total = extract_pg_count(&count_rows)?;
        let rows = self.fetch_pg(queries.rows_sql, queries.rows_params).await?;
        Ok((rows, total))
    }

    /// Return the clean connection to the pool after all queries succeeded.
    pub fn release(self) {
        self.lease.release();
    }
}

async fn run_fetch_set(conn: &Object, sql: &str, values: &[Value]) -> Result<RowSet, DbError> {
    let stmt = conn
        .prepare_cached(sql)
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    let raw_rows = conn
        .query(&stmt, &params(values))
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    let stmt_cols = stmt.columns();
    let columns: Arc<[String]> = stmt_cols.iter().map(|c| c.name().to_owned()).collect();
    let types: Vec<&Type> = stmt_cols.iter().map(|c| c.type_()).collect();
    let rows = raw_rows
        .iter()
        .map(|row| decode_values(row, &types))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(RowSet { columns, rows })
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

async fn run_scalar(conn: &Object, sql: &str, values: &[Value]) -> Result<Option<i64>, DbError> {
    let stmt = conn
        .prepare_cached(sql)
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    let row = conn
        .query_opt(&stmt, &params(values))
        .await
        .map_err(|e| DbError::from_driver(&e))?;
    row.map(|row| {
        row.try_get::<_, i64>(0)
            .map_err(|_| DbError::new(ErrorKind::Type, "could not decode a column"))
    })
    .transpose()
}

async fn scalar_with_recovery(
    conn: &Object,
    sql: &str,
    values: &[Value],
) -> Result<Option<i64>, DbError> {
    match run_scalar(conn, sql, values).await {
        Err(err) if is_stale_plan(&err) => {
            conn.statement_cache.remove(sql, &[]);
            run_scalar(conn, sql, values).await
        }
        other => other,
    }
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

async fn fetch_set_with_recovery(
    conn: &Object,
    sql: &str,
    values: &[Value],
) -> Result<RowSet, DbError> {
    match run_fetch_set(conn, sql, values).await {
        Err(err) if is_stale_plan(&err) => {
            conn.statement_cache.remove(sql, &[]);
            run_fetch_set(conn, sql, values).await
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

/// A finite `numeric` as its decimal text, scale kept; NaN and infinity are refused.
pub(crate) struct PgNumeric(pub(crate) String);

impl<'a> FromSql<'a> for PgNumeric {
    fn from_sql(
        _ty: &Type,
        raw: &'a [u8],
    ) -> Result<Self, Box<dyn std::error::Error + Sync + Send>> {
        crate::numeric::decode(raw)
            .map(Self)
            .map_err(|e| e.0.into())
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::NUMERIC
    }
}

/// A finite `timestamptz`. PostgreSQL sends `infinity`/`-infinity` as the i64
/// extremes; they are refused instead of decoding to far-future instants.
pub(crate) struct PgTimestamp(pub(crate) time::OffsetDateTime);

impl<'a> FromSql<'a> for PgTimestamp {
    fn from_sql(
        ty: &Type,
        raw: &'a [u8],
    ) -> Result<Self, Box<dyn std::error::Error + Sync + Send>> {
        let micros = i64::from_sql(&Type::INT8, raw)?;
        if micros == i64::MAX || micros == i64::MIN {
            return Err("infinite timestamps are not supported".into());
        }
        time::OffsetDateTime::from_sql(ty, raw).map(Self)
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::TIMESTAMPTZ
    }
}

/// Reject a URL whose scheme names no supported backend before it reaches the
/// PostgreSQL parser. A key/value connection string has no scheme and passes.
/// The URL itself is never echoed: it may carry credentials.
const URL_SCHEMES: [&str; 5] = ["sqlite", "postgres", "postgresql", "mysql", "mariadb"];

fn check_postgres_scheme(url: &str) -> Result<(), DbError> {
    let Some((scheme, _)) = url.split_once("://") else {
        return check_double_slash(url);
    };
    let is_scheme = !scheme.is_empty()
        && scheme
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'));
    if !is_scheme || scheme == "postgres" || scheme == "postgresql" {
        return Ok(());
    }
    Err(DbError::new(
        ErrorKind::Connect,
        "unsupported database URL scheme; use sqlite://, postgres://, postgresql://, \
         mysql:// or mariadb://",
    ))
}

/// A supported scheme written without `//` would otherwise be read as a
/// PostgreSQL key-value string and fail with a misleading TLS message.
fn check_double_slash(url: &str) -> Result<(), DbError> {
    let named = url
        .split_once(':')
        .is_some_and(|(scheme, _)| URL_SCHEMES.contains(&scheme));
    if named {
        return Err(DbError::new(
            ErrorKind::Connect,
            "invalid database URL: the scheme needs `://` (for example \
             sqlite://./app.db, with two slashes)",
        ));
    }
    Ok(())
}

impl Database {
    /// Create a pool. No connection is opened until first use, so call
    /// [`Database::check`] to fail fast at startup.
    pub fn connect(url: &str, max_size: usize, wait: Duration) -> Result<Self, DbError> {
        if url.starts_with("sqlite://") {
            let path = connect_path(url)?;
            return Ok(Self {
                backend: DatabaseBackend::Sqlite(SqlitePool::open(&path, max_size, wait)?),
                max_size,
                #[cfg(feature = "sql-trace")]
                trace: SqlTrace::default(),
            });
        }
        if url.starts_with("mysql://") || url.starts_with("mariadb://") {
            let (driver_url, dialect) = if let Some(rest) = url.strip_prefix("mariadb://") {
                (format!("mysql://{rest}"), "mariadb")
            } else {
                (url.to_owned(), "mysql")
            };
            return Ok(Self {
                backend: DatabaseBackend::Mysql {
                    pool: MysqlPool::connect(&driver_url, max_size, wait)?,
                    dialect,
                },
                max_size,
                #[cfg(feature = "sql-trace")]
                trace: SqlTrace::default(),
            });
        }
        check_postgres_scheme(url)?;
        let (url, tls_options) = extract_options(url)
            .map_err(|_| DbError::new(ErrorKind::Connect, "invalid database TLS configuration"))?;
        let mut config = Config::new();
        config.url = Some(url);
        config.ssl_mode = Some(tls_options.ssl_mode);
        config.pool = Some(PoolConfig {
            max_size,
            timeouts: Timeouts {
                wait: Some(wait),
                ..Timeouts::default()
            },
            ..PoolConfig::default()
        });
        let tls = if tls_options.ssl_mode == SslMode::Disable {
            None
        } else {
            Some(connector(&tls_options).map_err(|_| {
                DbError::new(ErrorKind::Connect, "invalid database TLS configuration")
            })?)
        };
        let pool = match &tls {
            Some(tls) => config.create_pool(Some(Runtime::Tokio1), tls.clone()),
            None => config.create_pool(Some(Runtime::Tokio1), NoTls),
        }
        .map_err(|_| DbError::new(ErrorKind::Connect, "invalid database configuration"))?;
        Ok(Self {
            backend: DatabaseBackend::Postgres { pool, tls },
            max_size,
            #[cfg(feature = "sql-trace")]
            trace: SqlTrace::default(),
        })
    }

    async fn acquire(&self) -> Result<Lease, DbError> {
        let DatabaseBackend::Postgres { pool, tls } = &self.backend else {
            return Err(DbError::new(
                ErrorKind::Other,
                "PostgreSQL pool is unavailable for this backend",
            ));
        };
        pool.get()
            .await
            .map(|conn| Lease::new(conn, tls.clone()))
            .map_err(|e| match e {
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
        #[cfg(feature = "sql-trace")]
        self.trace.record("fetch", sql, values);
        if let DatabaseBackend::Sqlite(sqlite) = &self.backend {
            return sqlite.acquire().await?.fetch(sql, values).await;
        }
        if let DatabaseBackend::Mysql { pool: mysql, .. } = &self.backend {
            let mut lease = mysql.acquire().await?;
            let outcome = lease.fetch(sql, values).await;
            if outcome.is_ok() {
                lease.release();
            }
            return outcome;
        }
        let lease = self.acquire().await?;
        let outcome = fetch_with_recovery(lease.get()?, sql, values).await;
        lease.release();
        outcome
    }

    pub async fn fetch_set(&self, sql: &str, values: &[Value]) -> Result<RowSet, DbError> {
        #[cfg(feature = "sql-trace")]
        self.trace.record("fetch", sql, values);
        if let DatabaseBackend::Sqlite(sqlite) = &self.backend {
            return sqlite.acquire().await?.fetch_set(sql, values).await;
        }
        if let DatabaseBackend::Mysql { pool: mysql, .. } = &self.backend {
            let mut lease = mysql.acquire().await?;
            let outcome = lease.fetch_set(sql, values).await;
            if outcome.is_ok() {
                lease.release();
            }
            return outcome;
        }
        let lease = self.acquire().await?;
        let outcome = fetch_set_with_recovery(lease.get()?, sql, values).await;
        lease.release();
        outcome
    }

    /// Acquire a single PostgreSQL connection for pipelined/reused reads.
    /// Returns `Ok(None)` for SQLite and MySQL backends.
    pub async fn acquire_pg(&self) -> Result<Option<PgSession>, DbError> {
        if !matches!(&self.backend, DatabaseBackend::Postgres { .. }) {
            return Ok(None);
        }
        let lease = self.acquire().await?;
        Ok(Some(PgSession {
            lease,
            #[cfg(feature = "sql-trace")]
            trace: self.trace.clone(),
        }))
    }

    pub async fn fetch_page_set(
        &self,
        queries: &PageQueries<'_>,
        concurrent: bool,
    ) -> Result<(RowSet, i64), DbError> {
        if let Some(session) = self.acquire_pg().await? {
            let (rows, total) = session.fetch_pg_page(queries, concurrent).await?;
            session.release();
            return Ok((rows.to_row_set()?, total));
        }
        if concurrent && self.supports_concurrent_reads() {
            let (count_set, rows) = tokio::try_join!(
                self.fetch_set(queries.count_sql, queries.count_params),
                self.fetch_set(queries.rows_sql, queries.rows_params),
            )?;
            return Ok((rows, extract_count_set(&count_set)?));
        }
        let count_set = self
            .fetch_set(queries.count_sql, queries.count_params)
            .await?;
        let total = extract_count_set(&count_set)?;
        let rows = self
            .fetch_set(queries.rows_sql, queries.rows_params)
            .await?;
        Ok((rows, total))
    }

    /// Whether the pool can safely run two read statements concurrently.
    /// SQLite runs sequentially to avoid file-lock contention, and any pool
    /// with `max_size < 2` runs sequentially so one request never deadlocks
    /// waiting for a second connection.
    pub fn supports_concurrent_reads(&self) -> bool {
        self.max_size >= 2 && !matches!(&self.backend, DatabaseBackend::Sqlite(_))
    }

    /// Run a page's count and rows queries, returning `(rows, total)`.
    ///
    /// When `concurrent` is true and [`Self::supports_concurrent_reads`] holds,
    /// both queries run concurrently via `tokio::try_join!`, dropping the
    /// sibling future on error so no connection leaks. Otherwise they run
    /// sequentially (count then rows).
    pub async fn fetch_page(
        &self,
        queries: &PageQueries<'_>,
        concurrent: bool,
    ) -> Result<(Vec<Row>, i64), DbError> {
        if concurrent && self.supports_concurrent_reads() {
            let (count_rows, rows) = tokio::try_join!(
                self.fetch(queries.count_sql, queries.count_params),
                self.fetch(queries.rows_sql, queries.rows_params),
            )?;
            return Ok((rows, extract_count(&count_rows)?));
        }
        let count_rows = self.fetch(queries.count_sql, queries.count_params).await?;
        let total = extract_count(&count_rows)?;
        let rows = self.fetch(queries.rows_sql, queries.rows_params).await?;
        Ok((rows, total))
    }

    pub async fn execute(&self, sql: &str, values: &[Value]) -> Result<u64, DbError> {
        #[cfg(feature = "sql-trace")]
        self.trace.record("execute", sql, values);
        if let DatabaseBackend::Sqlite(sqlite) = &self.backend {
            return sqlite.acquire().await?.execute(sql, values).await;
        }
        if let DatabaseBackend::Mysql { pool: mysql, .. } = &self.backend {
            let mut lease = mysql.acquire().await?;
            let outcome = lease.execute(sql, values).await;
            if outcome.is_ok() {
                lease.release();
            }
            return outcome;
        }
        let lease = self.acquire().await?;
        let outcome = execute_with_recovery(lease.get()?, sql, values).await;
        lease.release();
        outcome
    }

    /// Run one statement that produces an integer, outside any transaction: the
    /// first column of the first `RETURNING` row (PostgreSQL, SQLite) or the
    /// `last_insert_id` of the OK packet (MySQL, MariaDB; the statement passes
    /// its value through `LAST_INSERT_ID(expr)`). `None` when there is no row or
    /// the last insert id is 0. One round trip on a statement the driver caches.
    pub async fn execute_scalar(
        &self,
        sql: &str,
        values: &[Value],
    ) -> Result<Option<i64>, DbError> {
        #[cfg(feature = "sql-trace")]
        self.trace.record("scalar", sql, values);
        if let DatabaseBackend::Sqlite(sqlite) = &self.backend {
            return sqlite.acquire().await?.scalar(sql, values).await;
        }
        if let DatabaseBackend::Mysql { pool: mysql, .. } = &self.backend {
            let mut lease = mysql.acquire().await?;
            let outcome = lease.exec_last_insert_id(sql, values).await;
            if outcome.is_ok() {
                lease.release();
            }
            return outcome;
        }
        let lease = self.acquire().await?;
        let outcome = scalar_with_recovery(lease.get()?, sql, values).await;
        lease.release();
        outcome
    }

    /// Start a transaction that owns one pooled connection until it ends. If
    /// this future is dropped while `BEGIN` is in flight the connection is
    /// abandoned, never returned to the pool inside a transaction.
    pub async fn begin(&self) -> Result<Transaction, DbError> {
        if let DatabaseBackend::Sqlite(sqlite) = &self.backend {
            let mut lease = sqlite.acquire().await?;
            lease.begin("BEGIN IMMEDIATE").await?;
            #[cfg(feature = "sql-trace")]
            self.trace.record("boundary", "BEGIN IMMEDIATE", &[]);
            return Ok(Transaction::new_sqlite(
                lease,
                #[cfg(feature = "sql-trace")]
                self.trace.clone(),
            ));
        }
        if let DatabaseBackend::Mysql { pool: mysql, .. } = &self.backend {
            let mut lease = mysql.acquire().await?;
            lease.begin().await?;
            #[cfg(feature = "sql-trace")]
            self.trace.record("boundary", "BEGIN", &[]);
            return Ok(Transaction::new_mysql(
                lease,
                #[cfg(feature = "sql-trace")]
                self.trace.clone(),
            ));
        }
        let lease = self.acquire().await?;
        lease
            .get()?
            .batch_execute("BEGIN")
            .await
            .map_err(|e| DbError::from_driver(&e))?;
        let (conn, tls) = lease.into_inner()?;
        #[cfg(feature = "sql-trace")]
        self.trace.record("boundary", "BEGIN", &[]);
        Ok(Transaction::new(
            conn,
            tls,
            #[cfg(feature = "sql-trace")]
            self.trace.clone(),
        ))
    }

    pub async fn begin_migration(&self) -> Result<Transaction, DbError> {
        if let DatabaseBackend::Sqlite(sqlite) = &self.backend {
            let (mut primary, reserved) = sqlite.acquire_for_migration().await?;
            primary.begin("BEGIN IMMEDIATE").await?;
            return Ok(Transaction::new_sqlite_migration(primary, reserved));
        }
        if matches!(&self.backend, DatabaseBackend::Mysql { .. }) {
            let mut transaction = self.begin().await?;
            transaction.lock_mysql_migrations().await?;
            return Ok(transaction);
        }
        self.begin().await
    }

    pub fn close(&self) {
        match &self.backend {
            DatabaseBackend::Postgres { pool, .. } => pool.close(),
            DatabaseBackend::Sqlite(pool) => pool.close(),
            DatabaseBackend::Mysql { pool, .. } => pool.close(),
        }
    }

    pub fn dialect(&self) -> &'static str {
        match &self.backend {
            DatabaseBackend::Postgres { .. } => "postgres",
            DatabaseBackend::Sqlite(_) => "sqlite",
            DatabaseBackend::Mysql { dialect, .. } => dialect,
        }
    }

    pub fn status(&self) -> (usize, usize) {
        match &self.backend {
            DatabaseBackend::Sqlite(pool) => pool.status(),
            DatabaseBackend::Mysql { pool, .. } => pool.status(),
            DatabaseBackend::Postgres { pool, .. } => {
                let status = pool.status();
                (status.size, status.available)
            }
        }
    }

    #[cfg(feature = "sql-trace")]
    pub fn clear_sql_trace(&self) {
        self.trace.clear();
    }

    #[cfg(feature = "sql-trace")]
    pub fn take_sql_trace(&self) -> Vec<SqlTraceEntry> {
        self.trace.take()
    }
}

/// Marks a statement as in flight; if it is dropped before completing, the
/// transaction is poisoned (its connection is in an unknown state) and the
/// server is asked to cancel the statement.
struct Flight<'a> {
    poisoned: &'a AtomicBool,
    token: tokio_postgres::CancelToken,
    tls: Option<MakeRustlsConnect>,
    armed: bool,
}

impl Drop for Flight<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.poisoned.store(true, Ordering::SeqCst);
            spawn_cancel(self.token.clone(), self.tls.clone());
        }
    }
}

/// The SQLite counterpart of `Flight`: a statement is already queued on the
/// database thread, so abandoning it leaves a transaction whose state the caller
/// can no longer know, and it must not be committed.
struct SqliteFlight<'a> {
    poisoned: &'a AtomicBool,
    armed: bool,
}

impl Drop for SqliteFlight<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.poisoned.store(true, Ordering::SeqCst);
        }
    }
}

/// A transaction. Dropping it without `commit`/`rollback`, or after a
/// statement was abandoned mid-flight, discards the connection instead of
/// returning one in an unknown state to the pool.
pub struct Transaction {
    backend: TransactionBackend,
    /// A statement failed: PostgreSQL will refuse to commit (until a
    /// `ROLLBACK TO SAVEPOINT` recovers it).
    failed: AtomicBool,
    /// A statement was abandoned mid-flight.
    poisoned: AtomicBool,
    mysql_migration_lock: bool,
    #[cfg(feature = "sql-trace")]
    trace: SqlTrace,
}

enum TransactionBackend {
    Postgres {
        conn: Option<Object>,
        tls: Option<MakeRustlsConnect>,
    },
    Sqlite {
        primary: SqliteLease,
        reserved: Vec<SqliteLease>,
    },
    Mysql(Arc<tokio::sync::Mutex<MysqlLease>>),
    Finished,
}

impl Transaction {
    fn new(
        conn: Object,
        tls: Option<MakeRustlsConnect>,
        #[cfg(feature = "sql-trace")] trace: SqlTrace,
    ) -> Self {
        Self {
            backend: TransactionBackend::Postgres {
                conn: Some(conn),
                tls,
            },
            failed: AtomicBool::new(false),
            poisoned: AtomicBool::new(false),
            mysql_migration_lock: false,
            #[cfg(feature = "sql-trace")]
            trace,
        }
    }

    fn new_sqlite(sqlite: SqliteLease, #[cfg(feature = "sql-trace")] trace: SqlTrace) -> Self {
        Self {
            backend: TransactionBackend::Sqlite {
                primary: sqlite,
                reserved: Vec::new(),
            },
            failed: AtomicBool::new(false),
            poisoned: AtomicBool::new(false),
            mysql_migration_lock: false,
            #[cfg(feature = "sql-trace")]
            trace,
        }
    }

    fn new_mysql(mysql: MysqlLease, #[cfg(feature = "sql-trace")] trace: SqlTrace) -> Self {
        Self {
            backend: TransactionBackend::Mysql(Arc::new(tokio::sync::Mutex::new(mysql))),
            failed: AtomicBool::new(false),
            poisoned: AtomicBool::new(false),
            mysql_migration_lock: false,
            #[cfg(feature = "sql-trace")]
            trace,
        }
    }

    fn new_sqlite_migration(sqlite: SqliteLease, reserved: Vec<SqliteLease>) -> Self {
        Self {
            backend: TransactionBackend::Sqlite {
                primary: sqlite,
                reserved,
            },
            failed: AtomicBool::new(false),
            poisoned: AtomicBool::new(false),
            mysql_migration_lock: false,
            #[cfg(feature = "sql-trace")]
            trace: SqlTrace::default(),
        }
    }

    async fn lock_mysql_migrations(&mut self) -> Result<(), DbError> {
        let TransactionBackend::Mysql(mysql) = &self.backend else {
            return Ok(());
        };
        let rows = mysql
            .lock()
            .await
            .fetch(
                "SELECT GET_LOCK(CONCAT('h', LEFT(SHA2(DATABASE(), 256), 63)), 5) AS acquired",
                &[],
            )
            .await?;
        let acquired = rows
            .first()
            .and_then(|row| row.first())
            .map(|(_, value)| value);
        if !matches!(acquired, Some(Value::Int(1)) | Some(Value::Bool(true))) {
            return Err(DbError::new(
                ErrorKind::Other,
                "could not acquire the database migration lock",
            ));
        }
        self.mysql_migration_lock = true;
        Ok(())
    }

    fn ensure_not_interrupted(&self) -> Result<(), DbError> {
        if self.poisoned.load(Ordering::SeqCst) {
            return Err(DbError::new(
                ErrorKind::Other,
                "transaction was interrupted",
            ));
        }
        Ok(())
    }

    fn conn(&self) -> Result<&Object, DbError> {
        self.ensure_not_interrupted()?;
        match &self.backend {
            TransactionBackend::Postgres {
                conn: Some(conn), ..
            } => Ok(conn),
            TransactionBackend::Postgres { conn: None, .. }
            | TransactionBackend::Sqlite { .. }
            | TransactionBackend::Mysql(_)
            | TransactionBackend::Finished => Err(DbError::new(
                ErrorKind::Other,
                "transaction connection is unavailable for this operation",
            )),
        }
    }

    fn settle<T>(&self, sql: &str, outcome: &Result<T, DbError>) {
        if matches!(&self.backend, TransactionBackend::Mysql(_)) {
            return;
        }
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
        #[cfg(feature = "sql-trace")]
        self.trace.record("fetch", sql, values);
        match &self.backend {
            TransactionBackend::Sqlite { primary, .. } => {
                self.ensure_not_interrupted()?;
                let mut flight = SqliteFlight {
                    poisoned: &self.poisoned,
                    armed: true,
                };
                let outcome = primary.fetch(sql, values).await;
                flight.armed = false;
                self.settle(sql, &outcome);
                outcome
            }
            TransactionBackend::Finished => Err(DbError::new(
                ErrorKind::Other,
                "transaction already finished",
            )),
            TransactionBackend::Mysql(mysql) => {
                let outcome = mysql.lock().await.fetch(sql, values).await;
                self.settle(sql, &outcome);
                outcome
            }
            TransactionBackend::Postgres { tls, .. } => {
                let conn = self.conn()?;
                let mut flight = Flight {
                    poisoned: &self.poisoned,
                    token: conn.cancel_token(),
                    tls: tls.clone(),
                    armed: true,
                };
                let outcome = run_fetch(conn, sql, values).await;
                flight.armed = false;
                self.settle(sql, &outcome);
                outcome
            }
        }
    }

    /// Run a page's count and rows queries sequentially on this transaction's
    /// single connection. Never runs concurrently.
    pub async fn fetch_page(&self, queries: &PageQueries<'_>) -> Result<(Vec<Row>, i64), DbError> {
        let count_rows = self.fetch(queries.count_sql, queries.count_params).await?;
        let total = extract_count(&count_rows)?;
        let rows = self.fetch(queries.rows_sql, queries.rows_params).await?;
        Ok((rows, total))
    }

    pub async fn fetch_set(&self, sql: &str, values: &[Value]) -> Result<RowSet, DbError> {
        #[cfg(feature = "sql-trace")]
        self.trace.record("fetch", sql, values);
        match &self.backend {
            TransactionBackend::Sqlite { primary, .. } => {
                self.ensure_not_interrupted()?;
                let mut flight = SqliteFlight {
                    poisoned: &self.poisoned,
                    armed: true,
                };
                let outcome = primary.fetch_set(sql, values).await;
                flight.armed = false;
                self.settle(sql, &outcome);
                outcome
            }
            TransactionBackend::Finished => Err(DbError::new(
                ErrorKind::Other,
                "transaction already finished",
            )),
            TransactionBackend::Mysql(mysql) => {
                let outcome = mysql.lock().await.fetch_set(sql, values).await;
                self.settle(sql, &outcome);
                outcome
            }
            TransactionBackend::Postgres { tls, .. } => {
                let conn = self.conn()?;
                let mut flight = Flight {
                    poisoned: &self.poisoned,
                    token: conn.cancel_token(),
                    tls: tls.clone(),
                    armed: true,
                };
                let outcome = run_fetch_set(conn, sql, values).await;
                flight.armed = false;
                self.settle(sql, &outcome);
                outcome
            }
        }
    }

    pub async fn fetch_page_set(
        &self,
        queries: &PageQueries<'_>,
    ) -> Result<(RowSet, i64), DbError> {
        let count_set = self
            .fetch_set(queries.count_sql, queries.count_params)
            .await?;
        let total = extract_count_set(&count_set)?;
        let rows = self
            .fetch_set(queries.rows_sql, queries.rows_params)
            .await?;
        Ok((rows, total))
    }

    pub async fn execute(&self, sql: &str, values: &[Value]) -> Result<u64, DbError> {
        #[cfg(feature = "sql-trace")]
        self.trace.record("execute", sql, values);
        match &self.backend {
            TransactionBackend::Sqlite { primary, .. } => {
                self.ensure_not_interrupted()?;
                let mut flight = SqliteFlight {
                    poisoned: &self.poisoned,
                    armed: true,
                };
                let outcome = primary.execute(sql, values).await;
                flight.armed = false;
                self.settle(sql, &outcome);
                outcome
            }
            TransactionBackend::Finished => Err(DbError::new(
                ErrorKind::Other,
                "transaction already finished",
            )),
            TransactionBackend::Mysql(mysql) => {
                let outcome = mysql.lock().await.execute(sql, values).await;
                self.settle(sql, &outcome);
                outcome
            }
            TransactionBackend::Postgres { tls, .. } => {
                let conn = self.conn()?;
                let mut flight = Flight {
                    poisoned: &self.poisoned,
                    token: conn.cancel_token(),
                    tls: tls.clone(),
                    armed: true,
                };
                let outcome = run_execute(conn, sql, values).await;
                flight.armed = false;
                self.settle(sql, &outcome);
                outcome
            }
        }
    }

    /// Commit. A transaction aborted by an earlier statement error is rolled
    /// back and reported as an error: PostgreSQL would silently answer
    /// `COMMIT` with a rollback, which must never look like success.
    pub async fn commit(mut self) -> Result<(), DbError> {
        self.ensure_not_interrupted()?;
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
        if self.poisoned.load(Ordering::SeqCst) || self.mysql_interrupted().await {
            return Ok(()); // Drop discards the connection, which ends the transaction
        }
        self.finish("ROLLBACK").await
    }

    /// Whether a MySQL request of this transaction was cancelled or timed out
    /// mid-flight: the wire is unusable, so `ROLLBACK` cannot be sent on it.
    async fn mysql_interrupted(&self) -> bool {
        match &self.backend {
            TransactionBackend::Mysql(mysql) => mysql.lock().await.is_interrupted(),
            _ => false,
        }
    }

    async fn finish(&mut self, statement: &str) -> Result<(), DbError> {
        #[cfg(feature = "sql-trace")]
        self.trace.record("boundary", statement, &[]);
        match &mut self.backend {
            TransactionBackend::Sqlite { primary, reserved } => {
                // Transactional leases are discarded by Drop unless commit/rollback
                // completes successfully.
                primary.finish(statement).await?;
                for connection in reserved {
                    connection.set_foreign_keys(true).await?;
                }
                self.backend = TransactionBackend::Finished;
                Ok(())
            }
            TransactionBackend::Finished => Err(DbError::new(
                ErrorKind::Other,
                "transaction already finished",
            )),
            TransactionBackend::Mysql(mysql) => {
                let mysql = Arc::clone(mysql);
                let mut lease = mysql.lock().await;
                lease.batch(statement).await?;
                if self.mysql_migration_lock {
                    // The transaction is already settled; a failed release only
                    // costs the session, which the pool discards on drop.
                    let _ = lease
                        .fetch(
                            "SELECT RELEASE_LOCK(CONCAT('h', LEFT(SHA2(DATABASE(), 256), 63))) AS released",
                            &[],
                        )
                        .await;
                }
                self.mysql_migration_lock = false;
                drop(lease);
                self.backend = TransactionBackend::Finished;
                Ok(())
            }
            TransactionBackend::Postgres { conn, .. } => {
                let connection = conn.as_ref().ok_or_else(|| {
                    DbError::new(ErrorKind::Other, "transaction already finished")
                })?;
                // On failure the connection stays owned so Drop discards it.
                connection
                    .batch_execute(statement)
                    .await
                    .map_err(|e| DbError::from_driver(&e))?;
                self.backend = TransactionBackend::Finished;
                Ok(())
            }
        }
    }
}

impl Drop for Transaction {
    fn drop(&mut self) {
        match &mut self.backend {
            TransactionBackend::Postgres { conn, tls } => {
                if let Some(conn) = conn.take() {
                    abandon(conn, tls.clone());
                }
            }
            TransactionBackend::Sqlite { primary, reserved } => {
                primary.discard();
                for connection in reserved {
                    connection.discard();
                }
            }
            TransactionBackend::Mysql(mysql) => {
                if let Some(mysql) = Arc::get_mut(mysql) {
                    // A dropped transaction must not return an open transaction
                    // to the pool; terminate the session instead.
                    mysql.get_mut().abandon();
                }
            }
            TransactionBackend::Finished => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{check_postgres_scheme, Lease};
    use crate::error::ErrorKind;

    #[test]
    fn released_lease_returns_error_instead_of_panicking() {
        let lease = Lease {
            conn: None,
            tls: None,
        };
        let get_err = lease.get().unwrap_err();
        assert_eq!(get_err.kind, ErrorKind::Other);
        assert_eq!(
            get_err.message,
            "database connection lease already released"
        );

        let Err(into_err) = lease.into_inner() else {
            panic!("expected released lease error");
        };
        assert_eq!(into_err.kind, ErrorKind::Other);
        assert_eq!(
            into_err.message,
            "database connection lease already released"
        );
    }

    #[test]
    fn unsupported_url_schemes_are_named_without_the_url() {
        for url in ["oracle://user:secret@db/app", "mssql://db/app", "http://db"] {
            let error = check_postgres_scheme(url).unwrap_err();
            assert!(error
                .to_string()
                .contains("unsupported database URL scheme"));
            assert!(!error.to_string().contains("secret"));
        }
    }

    #[test]
    fn a_known_scheme_without_the_double_slash_is_named_without_the_url() {
        for url in [
            "sqlite:./a.db",
            "sqlite:/tmp/a.db",
            "postgres:user:secret@db/app",
            "mysql:user:secret@db/app",
        ] {
            let message = check_postgres_scheme(url).unwrap_err().to_string();
            assert!(message.contains("needs `://`"), "{message}");
            assert!(!message.contains("TLS"), "{message}");
            assert!(!message.contains("secret"), "{message}");
        }
    }

    #[test]
    fn postgres_urls_and_key_value_strings_pass_the_scheme_check() {
        for url in [
            "postgres://user@db/app",
            "postgresql://user@db/app?sslmode=require",
            "host=db user=app password=a://b",
            "://missing-scheme",
        ] {
            assert!(check_postgres_scheme(url).is_ok(), "{url}");
        }
    }

    #[test]
    fn extract_count_reads_integer_and_rejects_bad_rows() {
        use super::extract_count;
        use crate::Value;

        let ok = vec![vec![("count".to_owned(), Value::Int(42))]];
        assert_eq!(extract_count(&ok).unwrap(), 42);

        let empty: Vec<super::Row> = vec![];
        assert_eq!(extract_count(&empty).unwrap_err().kind, ErrorKind::Type);

        let wrong = vec![vec![("count".to_owned(), Value::Text("42".to_owned()))]];
        assert_eq!(extract_count(&wrong).unwrap_err().kind, ErrorKind::Type);
    }

    fn temp_sqlite_url(tag: &str) -> (String, std::path::PathBuf) {
        let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target");
        let path = dir.join(format!("hinoki-orm-{tag}-{}.sqlite3", uuid::Uuid::new_v4()));
        (format!("sqlite://{}", path.display()), path)
    }

    fn cleanup_sqlite(path: &std::path::Path) {
        for suffix in ["", "-wal", "-shm"] {
            let mut os = path.as_os_str().to_os_string();
            os.push(suffix);
            let _ = std::fs::remove_file(os);
        }
    }

    #[test]
    fn supports_concurrent_reads_respects_backend_and_pool_size() {
        use super::Database;
        use std::time::Duration;

        let wait = Duration::from_secs(1);
        let (url, path) = temp_sqlite_url("concurrent-cap");
        let sqlite_small = Database::connect(&url, 1, wait).unwrap();
        let sqlite_large = Database::connect(&url, 4, wait).unwrap();
        assert!(!sqlite_small.supports_concurrent_reads());
        assert!(!sqlite_large.supports_concurrent_reads());
        sqlite_small.close();
        sqlite_large.close();
        cleanup_sqlite(&path);

        let pg_one =
            Database::connect("postgres://localhost/test?sslmode=disable", 1, wait).unwrap();
        let pg_two =
            Database::connect("postgres://localhost/test?sslmode=disable", 2, wait).unwrap();
        assert!(!pg_one.supports_concurrent_reads());
        assert!(pg_two.supports_concurrent_reads());
    }

    #[tokio::test]
    async fn sqlite_fetch_page_runs_without_deadlock_on_pool_size_one() {
        use super::{Database, PageQueries};
        use crate::Value;
        use std::time::Duration;

        let (url, path) = temp_sqlite_url("fetch-page");
        let db = Database::connect(&url, 1, Duration::from_secs(1)).unwrap();
        db.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)",
            &[],
        )
        .await
        .unwrap();
        db.execute(
            "INSERT INTO items (id, name) VALUES (1, 'a'), (2, 'b')",
            &[],
        )
        .await
        .unwrap();

        let queries = PageQueries {
            rows_sql: "SELECT id, name FROM items ORDER BY id ASC LIMIT ?1 OFFSET ?2",
            rows_params: &[Value::Int(1), Value::Int(1)],
            count_sql: "SELECT count(*) AS count FROM items",
            count_params: &[],
        };
        let (rows, total) = db.fetch_page(&queries, true).await.unwrap();
        assert_eq!(total, 2);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0][1].1, Value::Text("b".to_owned()));

        let tx = db.begin().await.unwrap();
        tx.execute("INSERT INTO items (id, name) VALUES (3, 'c')", &[])
            .await
            .unwrap();
        let (tx_rows, tx_total) = tx.fetch_page(&queries).await.unwrap();
        assert_eq!(tx_total, 3);
        assert_eq!(tx_rows.len(), 1);
        tx.rollback().await.unwrap();
        db.close();
        cleanup_sqlite(&path);
    }
}
