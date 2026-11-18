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

use crate::error::{DbError, ErrorKind};
use crate::mysql::{MysqlLease, MysqlPool};
use crate::sqlite::{connect_path, SqliteLease, SqlitePool};
use crate::tls::{connector, extract_options};
use crate::value::Value;

/// One result row: column names with decoded values, in column order.
pub type Row = Vec<(String, Value)>;

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

    fn get(&self) -> &Object {
        self.conn.as_ref().expect("lease already released")
    }

    /// Hand the connection to a longer-lived owner (a transaction).
    fn into_inner(mut self) -> (Object, Option<MakeRustlsConnect>) {
        (
            self.conn.take().expect("lease already released"),
            self.tls.take(),
        )
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

/// A finite `timestamptz`. PostgreSQL sends `infinity`/`-infinity` as the i64
/// extremes; they are refused instead of decoding to far-future instants.
struct PgTimestamp(time::OffsetDateTime);

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
fn check_postgres_scheme(url: &str) -> Result<(), DbError> {
    let Some((scheme, _)) = url.split_once("://") else {
        return Ok(());
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

impl Database {
    /// Create a pool. No connection is opened until first use, so call
    /// [`Database::check`] to fail fast at startup.
    pub fn connect(url: &str, max_size: usize, wait: Duration) -> Result<Self, DbError> {
        if url.starts_with("sqlite://") {
            let path = connect_path(url)?;
            return Ok(Self {
                backend: DatabaseBackend::Sqlite(SqlitePool::open(&path, max_size, wait)?),
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
        let outcome = fetch_with_recovery(lease.get(), sql, values).await;
        lease.release();
        outcome
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
        let outcome = execute_with_recovery(lease.get(), sql, values).await;
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
            .get()
            .batch_execute("BEGIN")
            .await
            .map_err(|e| DbError::from_driver(&e))?;
        let (conn, tls) = lease.into_inner();
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

    fn conn(&self) -> Result<&Object, DbError> {
        if self.poisoned.load(Ordering::SeqCst) {
            return Err(DbError::new(
                ErrorKind::Other,
                "transaction was interrupted",
            ));
        }
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
                let outcome = primary.fetch(sql, values).await;
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

    pub async fn execute(&self, sql: &str, values: &[Value]) -> Result<u64, DbError> {
        #[cfg(feature = "sql-trace")]
        self.trace.record("execute", sql, values);
        match &self.backend {
            TransactionBackend::Sqlite { primary, .. } => {
                let outcome = primary.execute(sql, values).await;
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
                    lease
                        .fetch(
                            "SELECT RELEASE_LOCK(CONCAT('h', LEFT(SHA2(DATABASE(), 256), 63))) AS released",
                            &[],
                        )
                        .await?;
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
    use super::check_postgres_scheme;

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
}
