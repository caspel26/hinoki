//! The stores and the limiter that drives them. A hit is one statement (or one
//! lock on a map); the decision is computed from the state it returns.

use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use tokio::time::Instant;

use hinoki_orm::{Database, DbError, Value};

use crate::gcra::{self, Decision, Quota, QuotaError, Step, Verdict};
use crate::key::{self, Bucket, KeyError, KeyParts};
use crate::observer::{Observer, Outcome};
use crate::sql::{dialect_of, statements, Statements, TableName, SWEEP_BATCH};

/// Longest storage timeout a limiter accepts.
pub const MAX_TIMEOUT: Duration = Duration::from_secs(5);
/// How often (in hits) a full table is swept, at most.
const FULL_SWEEP_EVERY: u32 = 64;

#[derive(Debug)]
pub enum StoreError {
    Timeout,
    Database(DbError),
}

impl StoreError {
    /// The statement failed because the table or its schema is not there.
    fn is_schema(&self) -> bool {
        let Self::Database(error) = self else {
            return false;
        };
        matches!(
            error.sqlstate.as_deref(),
            Some("42P01" | "42S02" | "SQLITE_1")
        )
    }

    fn is_deadlock(&self) -> bool {
        let Self::Database(error) = self else {
            return false;
        };
        matches!(error.sqlstate.as_deref(), Some("40001" | "40P01"))
    }
}

/// `Unavailable` never carries the key or the database message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LimitError {
    Unavailable { schema: bool },
    Key(KeyError),
    Quota(QuotaError),
}

#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub max_rows: u64,
    pub timeout: Duration,
    pub sweep_every: u32,
}

/// A validated policy: the quota, the wider one for unknown clients, the cost of
/// a hit and what to do when the store is down.
#[derive(Debug, Clone, Copy)]
pub struct Rule {
    pub quota: Quota,
    pub unknown: Quota,
    pub cost: u32,
    pub fail_open: bool,
}

pub trait Store: Send + Sync {
    /// One hit: the resulting state, or `None` for a new key refused because the
    /// table is full (`full`).
    fn hit(
        &self,
        bucket: &Bucket,
        step: &Step,
        full: bool,
    ) -> impl Future<Output = Result<Option<i64>, StoreError>> + Send;

    /// Remove a batch of expired buckets and say whether the table is at its cap.
    fn sweep(
        &self,
        now_us: i64,
        max_rows: u64,
    ) -> impl Future<Output = Result<bool, StoreError>> + Send;
}

pub struct DatabaseStore {
    db: Database,
    sql: Statements,
}

impl DatabaseStore {
    async fn hit_once(
        &self,
        bucket: &Bucket,
        step: &Step,
        full: bool,
    ) -> Result<Option<i64>, StoreError> {
        let (sql, values) = self.sql.hit_call(bucket, step, full);
        self.db
            .execute_scalar(sql, &values)
            .await
            .map_err(StoreError::Database)
    }
}

impl Store for DatabaseStore {
    async fn hit(
        &self,
        bucket: &Bucket,
        step: &Step,
        full: bool,
    ) -> Result<Option<i64>, StoreError> {
        match self.hit_once(bucket, step, full).await {
            Err(error) if error.is_deadlock() => self.hit_once(bucket, step, full).await,
            other => other,
        }
    }

    async fn sweep(&self, now_us: i64, max_rows: u64) -> Result<bool, StoreError> {
        let expired = Value::Int(now_us.saturating_mul(2).saturating_add(2));
        self.db
            .execute(&self.sql.sweep, &[expired, Value::Int(SWEEP_BATCH)])
            .await
            .map_err(StoreError::Database)?;
        let cap = i64::try_from(max_rows).unwrap_or(i64::MAX);
        let rows = self
            .db
            .fetch(&self.sql.count, &[Value::Int(cap)])
            .await
            .map_err(StoreError::Database)?;
        let counted = rows
            .first()
            .and_then(|row| row.first())
            .and_then(|(_, value)| match value {
                Value::Int(count) => Some(*count),
                _ => None,
            });
        Ok(counted.is_some_and(|count| count >= cap))
    }
}

/// Process-local buckets. Never shared between workers.
#[derive(Default)]
pub struct MemoryStore {
    map: Mutex<HashMap<[u8; 32], i64>>,
}

impl Store for MemoryStore {
    async fn hit(
        &self,
        bucket: &Bucket,
        step: &Step,
        full: bool,
    ) -> Result<Option<i64>, StoreError> {
        let mut map = self.map.lock().unwrap_or_else(PoisonError::into_inner);
        let current = map.get(&bucket.0).copied();
        if full && current.is_none() {
            return Ok(None);
        }
        let next = gcra::advance(current, step);
        map.insert(bucket.0, next);
        Ok(Some(next))
    }

    async fn sweep(&self, now_us: i64, max_rows: u64) -> Result<bool, StoreError> {
        let mut map = self.map.lock().unwrap_or_else(PoisonError::into_inner);
        let expired = now_us.saturating_mul(2).saturating_add(2);
        let doomed: Vec<[u8; 32]> = map
            .iter()
            .filter(|(_, state)| **state < expired)
            .map(|(key, _)| *key)
            .take(usize::try_from(SWEEP_BATCH).unwrap_or(500))
            .collect();
        for key in doomed {
            map.remove(&key);
        }
        Ok(u64::try_from(map.len()).unwrap_or(u64::MAX) >= max_rows)
    }
}

pub enum Backend {
    Database(DatabaseStore),
    Memory(MemoryStore),
}

impl Store for Backend {
    async fn hit(
        &self,
        bucket: &Bucket,
        step: &Step,
        full: bool,
    ) -> Result<Option<i64>, StoreError> {
        match self {
            Self::Database(store) => store.hit(bucket, step, full).await,
            Self::Memory(store) => store.hit(bucket, step, full).await,
        }
    }

    async fn sweep(&self, now_us: i64, max_rows: u64) -> Result<bool, StoreError> {
        match self {
            Self::Database(store) => store.sweep(now_us, max_rows).await,
            Self::Memory(store) => store.sweep(now_us, max_rows).await,
        }
    }
}

/// The quota and step a hit was counted with, and the store's answer.
type Counted<'r> = (&'r Quota, Step, Result<Option<i64>, StoreError>);

pub struct Limiter {
    backend: Backend,
    limits: Limits,
    hits: AtomicU32,
    full: AtomicBool,
    observer: Mutex<Option<Arc<dyn Observer>>>,
}

impl Limiter {
    fn new(backend: Backend, limits: Limits) -> Self {
        Self {
            backend,
            limits,
            hits: AtomicU32::new(0),
            full: AtomicBool::new(false),
            observer: Mutex::new(None),
        }
    }

    pub fn database(db: Database, table: &TableName, limits: Limits) -> Self {
        let sql = statements(dialect_of(db.dialect()), table);
        Self::new(Backend::Database(DatabaseStore { db, sql }), limits)
    }

    pub fn memory(limits: Limits) -> Self {
        Self::new(Backend::Memory(MemoryStore::default()), limits)
    }

    /// Record decisions into `observer` from now on, replacing the one before: a
    /// worker started later in the same process counts into its own registry.
    pub fn attach(&self, observer: Arc<dyn Observer>) {
        *self.observer.lock().unwrap_or_else(PoisonError::into_inner) = Some(observer);
    }

    fn record(&self, outcome: Outcome) {
        let observer = self
            .observer
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();
        if let Some(observer) = observer {
            observer.decided(outcome);
        }
    }

    async fn run_hit(
        &self,
        bucket: &Bucket,
        step: &Step,
        full: bool,
        deadline: Instant,
    ) -> Result<Option<i64>, StoreError> {
        tokio::time::timeout_at(deadline, self.backend.hit(bucket, step, full))
            .await
            .unwrap_or(Err(StoreError::Timeout))
    }

    /// Every `sweep_every` hits (every 64 once the table is full) remove expired
    /// buckets and refresh the cap flag, before the hit that follows reads it, so a
    /// stale `full` never refuses the first new key after expiry. The sweep gets
    /// half of the timeout at most and a failed one leaves the hit to its own
    /// outcome, so a check never waits longer than the timeout.
    async fn maybe_sweep(&self, now_us: i64, deadline: Instant) {
        let every = if self.full.load(Ordering::Relaxed) {
            self.limits.sweep_every.min(FULL_SWEEP_EVERY)
        } else {
            self.limits.sweep_every
        };
        let hits = self.hits.fetch_add(1, Ordering::Relaxed).wrapping_add(1);
        if every == 0 || !hits.is_multiple_of(every) {
            return;
        }
        let sweep = self.backend.sweep(now_us, self.limits.max_rows);
        if let Ok(Ok(full)) = tokio::time::timeout_at(deadline, sweep).await {
            self.full.store(full, Ordering::Relaxed);
        }
    }

    fn failure(
        &self,
        error: &StoreError,
        rule: &Rule,
        quota: &Quota,
    ) -> Result<Decision, LimitError> {
        if rule.fail_open {
            self.record(Outcome::FailedOpen);
            return Ok(gcra::fail_open(quota));
        }
        self.record(Outcome::Unavailable);
        Err(LimitError::Unavailable {
            schema: error.is_schema(),
        })
    }

    /// Count a new key refused by the full table against its scope's overflow
    /// bucket, with the wider `unknown` quota: every such client shares it, so the
    /// full table neither refuses all of them nor admits them unbounded.
    async fn overflow<'r>(
        &self,
        rule: &'r Rule,
        scope: &str,
        now_us: i64,
        deadline: Instant,
    ) -> Result<Counted<'r>, LimitError> {
        let quota = &rule.unknown;
        let step = quota.step(now_us, rule.cost).map_err(LimitError::Quota)?;
        let bucket = key::overflow(scope).map_err(LimitError::Key)?;
        let hit = self.run_hit(&bucket, &step, false, deadline).await;
        Ok((quota, step, hit))
    }

    /// Count one hit. A store failure is a decision too: admitted (`fail_open`) or
    /// `Unavailable`.
    pub async fn check(
        &self,
        rule: &Rule,
        parts: &KeyParts<'_>,
        now_us: i64,
    ) -> Result<Decision, LimitError> {
        let keyed = key::bucket(parts).map_err(LimitError::Key)?;
        let quota = if keyed.unknown {
            &rule.unknown
        } else {
            &rule.quota
        };
        let step = quota.step(now_us, rule.cost).map_err(LimitError::Quota)?;
        let started = Instant::now();
        self.maybe_sweep(now_us, started + self.limits.timeout / 2)
            .await;
        let deadline = started + self.limits.timeout;
        let full = self.full.load(Ordering::Relaxed);
        let (quota, step, hit) = match self.run_hit(&keyed.bucket, &step, full, deadline).await {
            Ok(None) => self.overflow(rule, parts.scope, now_us, deadline).await?,
            hit => (quota, step, hit),
        };
        let decision = match hit {
            Ok(Some(state)) => gcra::decide(quota, &step, state),
            Ok(None) => gcra::refuse_full(quota),
            Err(error) => return self.failure(&error, rule, quota),
        };
        self.record(match decision.verdict {
            Verdict::Allowed => Outcome::Allowed,
            Verdict::Refused => Outcome::Refused,
        });
        Ok(decision)
    }
}
